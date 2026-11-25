"""What tapedeck remembers besides downloaded files: your listening events and your privacy rules.

Everything stays in the local SQLite file. Nothing here is ever sent anywhere.
"""
import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from pathlib import Path

from . import config
from .privacy import key

EVENTS = ("play", "finish", "skip", "like", "unlike", "dislike")
WEIGHT = {"play": 0.3, "finish": 1.0, "skip": -1.0}
HALF_LIFE_DAYS = 14.0
RULE_KINDS = ("song", "artist", "album")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """One short-lived connection per Deck (per request). A request may hop from the event loop to a worker
    thread, but never uses its connection from two threads at once, so cross-thread use is safe."""
    db = sqlite3.connect(path or config.data_dir() / "tapedeck.db", timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def artist_key(name: str | None) -> str:
    return key(name)


OWNER = "owner"
ACCOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# Passwords: scrypt (OpenSSL via hashlib), 16 MiB per check, random 16-byte salt. Stored as
# "scrypt$n$r$p$salt$hash" so the cost can be raised later without breaking old rows.
_SCRYPT = (2 ** 14, 8, 1)
PASSWORD_MIN = 6


def hash_password(password: str) -> str:
    n, r, p = _SCRYPT
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(want) // 2)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(dk.hex(), want)


_DUMMY_HASH = hash_password("timing-equaliser")  # unknown usernames cost the same as wrong passwords


class Store:
    """One person's view of the database. `account` picks whose listening events and privacy rules these are;
    "owner" is the person behind the env owner token. Agent permissions and the account list are shared."""

    def __init__(self, path: Path | None = None, account: str = OWNER):
        self.db = connect(path)
        self.account = account
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events (ts REAL, video_id TEXT, title TEXT, artist TEXT, album TEXT,
                                               album_id TEXT, artist_id TEXT, cover TEXT, event TEXT,
                                               account TEXT NOT NULL DEFAULT 'owner');
            CREATE INDEX IF NOT EXISTS events_vid ON events(video_id);
            CREATE TABLE IF NOT EXISTS private (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT,
                                                ref TEXT, label TEXT, created REAL,
                                                account TEXT NOT NULL DEFAULT 'owner', UNIQUE(account, kind, key));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS accounts (name TEXT PRIMARY KEY, token_sha256 TEXT UNIQUE NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS logins (username TEXT PRIMARY KEY, account TEXT UNIQUE NOT NULL,
                                               pw TEXT NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS sessions (token_sha256 TEXT PRIMARY KEY, account TEXT NOT NULL, created REAL);
            CREATE TABLE IF NOT EXISTS playlist_items (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
                                                       playlist_id TEXT NOT NULL, slot REAL NOT NULL, video_id TEXT NOT NULL,
                                                       track TEXT NOT NULL, added REAL);
            CREATE INDEX IF NOT EXISTS playlist_items_pid ON playlist_items(account, playlist_id);
            CREATE TABLE IF NOT EXISTS xmatch (key TEXT PRIMARY KEY, track TEXT, ts REAL);
        """)
        self._migrate()

    def _migrate(self) -> None:
        """0.2.0 databases had no accounts: their events and rules become the owner's."""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        if "account" not in cols:
            self.db.execute("ALTER TABLE events ADD COLUMN account TEXT NOT NULL DEFAULT 'owner'")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(private)")}
        if "account" not in cols:  # UNIQUE(kind, key) must become per account: rebuild the table
            # one transaction: a crash midway leaves the old table intact, and a stray private_new is cleared
            self.db.executescript("""
                BEGIN IMMEDIATE;
                DROP TABLE IF EXISTS private_new;
                CREATE TABLE private_new (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, ref TEXT,
                                          label TEXT, created REAL, account TEXT NOT NULL DEFAULT 'owner',
                                          UNIQUE(account, kind, key));
                INSERT INTO private_new (id, kind, key, ref, label, created, account)
                    SELECT id, kind, key, ref, label, created, 'owner' FROM private;
                DROP TABLE private;
                ALTER TABLE private_new RENAME TO private;
                COMMIT;
            """)
        self.db.execute("CREATE INDEX IF NOT EXISTS events_account ON events(account, ts)")
        self.db.commit()

    # ---- accounts ---------------------------------------------------------------------------
    def add_account(self, name: str) -> str:
        """Create a person and return their token. Only its SHA-256 is stored; the token is shown once."""
        if not isinstance(name, str) or not ACCOUNT_NAME.match(name):
            raise ValueError("account names are 1-32 lowercase letters, digits, - or _")
        if name in (OWNER, "agent"):
            raise ValueError(f'"{name}" is reserved (the env tokens use it)')
        if self.db.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            raise ValueError(f"account {name!r} already exists")
        token = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO accounts VALUES (?,?,?)", (name, _hash(token), time.time()))
        self.db.commit()
        return token

    def account_for_token(self, token: str) -> str | None:
        """A person's legacy account token, or a sign-in session (any account, the owner included)."""
        if not token:
            return None
        h = _hash(token)
        row = self.db.execute("SELECT name FROM accounts WHERE token_sha256=?", (h,)).fetchone()
        if row:
            return row[0]
        row = self.db.execute("SELECT account FROM sessions WHERE token_sha256=?", (h,)).fetchone()
        return row[0] if row else None

    def accounts(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT a.name, a.created, l.username FROM accounts a LEFT JOIN logins l ON l.account=a.name ORDER BY a.name")]

    def remove_account(self, name: str) -> bool:
        """Revokes the token, the login and every signed-in device. Their listening history stays."""
        cur = self.db.execute("DELETE FROM accounts WHERE name=?", (name,))
        self.db.execute("DELETE FROM logins WHERE account=?", (name,))
        self.db.execute("DELETE FROM sessions WHERE account=?", (name,))
        self.db.commit()
        return cur.rowcount > 0

    # ---- username + password sign-in (like Sonarr/Radarr) -----------------------------------
    def set_login(self, account: str, username: str, password: str) -> None:
        """Give an account (the owner or a person) a username and password, replacing any old one.
        Changing a password signs out that account's devices."""
        if account != OWNER and not self.db.execute("SELECT 1 FROM accounts WHERE name=?", (account,)).fetchone():
            raise LookupError(f"no account {account!r}")
        username = (username or "").strip().lower()
        if not ACCOUNT_NAME.match(username):
            raise ValueError("usernames are 1-32 lowercase letters, digits, - or _")
        if not isinstance(password, str) or len(password) < PASSWORD_MIN:
            raise ValueError(f"passwords need at least {PASSWORD_MIN} characters")
        taken = self.db.execute("SELECT account FROM logins WHERE username=?", (username,)).fetchone()
        if taken and taken[0] != account:
            raise ValueError(f"username {username!r} is taken")
        with self.db:
            self.db.execute("DELETE FROM logins WHERE account=?", (account,))
            self.db.execute("INSERT INTO logins VALUES (?,?,?,?)", (username, account, hash_password(password), time.time()))
            self.db.execute("DELETE FROM sessions WHERE account=?", (account,))

    def login_for(self, account: str) -> str | None:
        row = self.db.execute("SELECT username FROM logins WHERE account=?", (account,)).fetchone()
        return row[0] if row else None

    def check_login(self, username: str, password: str) -> str | None:
        """The account behind a username and password, or None. Same cost whether or not the user exists."""
        row = self.db.execute("SELECT account, pw FROM logins WHERE username=?",
                              ((username or "").strip().lower(),)).fetchone()
        good = verify_password(password or "", row[1] if row else _DUMMY_HASH)
        return row[0] if row and good else None

    def new_session(self, account: str) -> str:
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions VALUES (?,?,?)", (_hash(token), account, time.time()))
        self.db.commit()
        return token

    def end_session(self, token: str) -> bool:
        cur = self.db.execute("DELETE FROM sessions WHERE token_sha256=?", (_hash(token),))
        self.db.commit()
        return cur.rowcount > 0

    # ---- streamed songs inside Navidrome playlists -------------------------------------------
    # A Navidrome playlist stays the source of truth; its streamed (not downloaded) songs live here with the position
    # (slot) they were added at in the mixed list. Owner choice 2026-11-04: "Navidrome + streamed entries".
    def playlist_items(self, playlist_id: str) -> list[dict]:
        rows = self.db.execute("SELECT id, slot, track, added FROM playlist_items WHERE account=? AND playlist_id=?"
                               " ORDER BY slot, id", (self.account, playlist_id))
        return [{"id": r["id"], "slot": r["slot"], "added": r["added"], "track": json.loads(r["track"])} for r in rows]

    def add_playlist_item(self, playlist_id: str, track: dict, slot: float) -> int:
        cur = self.db.execute("INSERT INTO playlist_items (account, playlist_id, slot, video_id, track, added)"
                              " VALUES (?,?,?,?,?,?)",
                              (self.account, playlist_id, float(slot), track["video_id"], json.dumps(track), time.time()))
        self.db.commit()
        return cur.lastrowid

    def remove_playlist_item(self, playlist_id: str, item_id: int) -> bool:
        cur = self.db.execute("DELETE FROM playlist_items WHERE account=? AND playlist_id=? AND id=?",
                              (self.account, playlist_id, item_id))
        self.db.commit()
        return cur.rowcount > 0

    def move_playlist_item(self, playlist_id: str, item_id: int, slot: float) -> bool:
        cur = self.db.execute("UPDATE playlist_items SET slot=? WHERE account=? AND playlist_id=? AND id=?",
                              (float(slot), self.account, playlist_id, item_id))
        self.db.commit()
        return cur.rowcount > 0

    def drop_playlist(self, playlist_id: str) -> int:
        cur = self.db.execute("DELETE FROM playlist_items WHERE account=? AND playlist_id=?", (self.account, playlist_id))
        self.db.commit()
        return cur.rowcount

    # ---- listening events -------------------------------------------------------------------
    def add_event(self, track: dict, event: str, ts: float | None = None) -> None:
        if event not in EVENTS:
            raise ValueError(f"event must be one of {', '.join(EVENTS)}")
        if not track.get("video_id"):
            raise ValueError("event needs a video_id")
        arts = track.get("artists") or []
        artist = ", ".join(arts) if isinstance(arts, list) else str(arts)
        self.db.execute("INSERT INTO events (ts, video_id, title, artist, album, album_id, artist_id, cover, event, account)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ts or time.time(), track["video_id"], track.get("title"), artist or track.get("artist"),
                         track.get("album"), track.get("album_id"), track.get("artist_id"), track.get("cover"), event,
                         self.account))
        self.db.commit()

    def history(self, n: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT rowid AS id, ts, video_id, title, artist, album, album_id, artist_id, cover, event FROM events"
            " WHERE account=? ORDER BY ts DESC LIMIT ?", (self.account, n))]

    LISTENING = ("play", "finish", "skip")  # likes and dislikes are opinions, not history: clearing keeps them

    def delete_history(self, entry_id: int | None = None) -> int:
        """Remove one entry (any kind, by id) or, with no id, all of this account's listening history."""
        if entry_id is not None:
            cur = self.db.execute("DELETE FROM events WHERE account=? AND rowid=?", (self.account, entry_id))
        else:
            cur = self.db.execute(f"DELETE FROM events WHERE account=? AND event IN ({','.join('?' * len(self.LISTENING))})",
                                  (self.account, *self.LISTENING))
        self.db.commit()
        return cur.rowcount

    def recently_played(self, n: int = 50) -> set[str]:
        return {r[0] for r in self.db.execute(
            "SELECT video_id FROM events WHERE account=? AND event IN ('play','finish') ORDER BY ts DESC LIMIT ?",
            (self.account, n))}

    def count_events(self, events: tuple[str, ...] | None = None) -> int:
        if events is None:
            return self.db.execute("SELECT COUNT(*) FROM events WHERE account=?", (self.account,)).fetchone()[0]
        marks = ",".join("?" * len(events))
        return self.db.execute(f"SELECT COUNT(*) FROM events WHERE account=? AND event IN ({marks})",
                               (self.account, *events)).fetchone()[0]

    def _latest(self, events: tuple[str, ...]) -> dict[str, tuple[str, float]]:
        """video_id -> (latest event among `events`, its time). The latest opinion wins."""
        out: dict[str, tuple[str, float]] = {}
        marks = ",".join("?" * len(events))
        for r in self.db.execute(f"SELECT video_id, event, ts FROM events WHERE account=? AND event IN ({marks}) ORDER BY ts",
                                 (self.account, *events)):
            out[r["video_id"]] = (r["event"], r["ts"])
        return out

    def liked(self) -> set[str]:
        return {v for v, (e, _) in self._latest(("like", "unlike", "dislike")).items() if e == "like"}

    # Spotify/SoundCloud song -> its YouTube Music audio track. Shared by every account (it's catalog, not history).
    # A miss is cached too (track NULL), so a song YouTube Music doesn't have isn't searched for on every autoplay.
    XMATCH_TTL = 7 * 86400

    def xmatch_get(self, k: str, now: float | None = None) -> tuple[bool, dict | None]:
        """(known, track). known=False: never looked up, or the answer is older than a week."""
        row = self.db.execute("SELECT track, ts FROM xmatch WHERE key=?", (k,)).fetchone()
        if not row or (now or time.time()) - row["ts"] > self.XMATCH_TTL:
            return False, None
        return True, json.loads(row["track"]) if row["track"] else None

    def xmatch_put(self, k: str, track: dict | None, now: float | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO xmatch (key, track, ts) VALUES (?, ?, ?)",
                        (k, json.dumps(track) if track else None, now or time.time()))
        self.db.commit()

    def titles(self, ids) -> list[dict]:
        """Title and artists last recorded for these songs (any event), to recognise other uploads of them."""
        ids = [v for v in ids if v]
        out = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self.db.execute(
                f"SELECT video_id, title, artist FROM events WHERE account=? AND video_id IN ({','.join('?' * len(chunk))})"
                " AND title IS NOT NULL GROUP BY video_id", (self.account, *chunk))
            out += [{"video_id": r["video_id"], "title": r["title"], "artists": (r["artist"] or "").split(", ")}
                    for r in rows]
        return out

    def disliked(self) -> set[str]:
        return {v for v, (e, _) in self._latest(("like", "unlike", "dislike")).items() if e == "dislike"}

    def recent_skips(self, days: float = 30, now: float | None = None) -> set[str]:
        """Songs whose most recent verdict is a skip within `days`: finishing or liking it later clears that."""
        since = (now or time.time()) - days * 86400
        return {v for v, (e, ts) in self._latest(("skip", "finish", "like")).items() if e == "skip" and ts >= since}

    def track_scores(self, owned: set[str] = frozenset(), now: float | None = None) -> dict[str, dict]:
        """video_id -> {score, track}. Finishes count, starts count a little, skips count against, all decaying
        with a 14-day half-life; a like adds 2 and a download you kept adds 1.5 (both without decay)."""
        now = now or time.time()
        out: dict[str, dict] = {}
        for r in self.db.execute("SELECT * FROM events WHERE account=? ORDER BY ts", (self.account,)):
            w = WEIGHT.get(r["event"])
            entry = out.setdefault(r["video_id"], {"score": 0.0, "track": {}})
            entry["track"] = {"video_id": r["video_id"], "title": r["title"],
                              "artists": [a.strip() for a in (r["artist"] or "").split(",") if a.strip()],
                              "album": r["album"], "album_id": r["album_id"], "artist_id": r["artist_id"],
                              "cover": r["cover"]}
            if w is not None:
                age_days = max(0.0, (now - r["ts"]) / 86400)
                entry["score"] += w * math.pow(0.5, age_days / HALF_LIFE_DAYS)
        for vid in self.liked():
            out.setdefault(vid, {"score": 0.0, "track": {"video_id": vid}})["score"] += 2.0
        for vid in owned:
            if vid in out:
                out[vid]["score"] += 1.5
        for vid in self.disliked():
            out.pop(vid, None)
        return out

    def top_artists(self, owned: set[str] = frozenset(), n: int = 10, exclude=None) -> list[dict]:
        """exclude(track) drops a song before it counts, so a private song can't surface its artist."""
        totals: dict[str, dict] = {}
        for e in self.track_scores(owned).values():
            if exclude and exclude(e["track"]):
                continue
            if e["score"] <= 0:
                continue
            for name in e["track"].get("artists") or []:
                a = totals.setdefault(artist_key(name), {"name": name, "artist_id": None, "score": 0.0})
                a["score"] += e["score"]
                if name == (e["track"].get("artists") or [None])[0]:
                    a["artist_id"] = a["artist_id"] or e["track"].get("artist_id")
        return sorted(totals.values(), key=lambda a: -a["score"])[:n]

    # ---- agent permissions -------------------------------------------------------------------
    def permissions(self) -> dict:
        import json

        from .privacy import PERMISSIONS
        row = self.db.execute("SELECT value FROM settings WHERE key='agent_permissions'").fetchone()
        saved = json.loads(row[0]) if row else {}
        return {k: saved.get(k, v) for k, v in PERMISSIONS.items()}

    def set_permissions(self, changes: dict) -> dict:
        import json

        from .privacy import PERMISSIONS
        perms = self.permissions()
        for k, v in changes.items():
            if k not in PERMISSIONS:
                raise ValueError(f"unknown permission {k!r}")
            ok = v in ("off", "own", "all") if k == "undo" else isinstance(v, bool)
            if not ok:
                raise ValueError(f"bad value for {k}: {v!r}")
            perms[k] = v
        self.db.execute("INSERT OR REPLACE INTO settings VALUES ('agent_permissions', ?)", (json.dumps(perms),))
        self.db.commit()
        return perms

    # ---- privacy rules ----------------------------------------------------------------------
    def rules(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id, kind, key, ref, label, created FROM private WHERE account=? ORDER BY id",
                                                 (self.account,))]

    def add_rule(self, kind: str, key: str, ref: str | None = None, label: str | None = None) -> dict:
        """kind song: key=video_id. artist: key=normalised name, ref=channel id. album: key=album id, ref=normalised title."""
        if kind not in RULE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(RULE_KINDS)}")
        if not isinstance(key, str) or (ref is not None and not isinstance(ref, str)) or (
                label is not None and not isinstance(label, str)):
            raise ValueError("key, ref and label must be strings")
        if kind == "artist":
            key = artist_key(key)
        elif kind == "album" and ref:
            ref = artist_key(ref)  # album titles compare the same way as artist names
        if not key:
            raise ValueError("rule needs a key")
        self.db.execute("INSERT OR IGNORE INTO private (kind, key, ref, label, created, account) VALUES (?,?,?,?,?,?)",
                        (kind, key, ref, label, time.time(), self.account))
        self.db.commit()
        return dict(self.db.execute("SELECT id, kind, key, ref, label, created FROM private WHERE account=? AND kind=? AND key=?",
                                    (self.account, kind, key)).fetchone())

    def remove_rule(self, rule_id: int) -> bool:
        cur = self.db.execute("DELETE FROM private WHERE id=? AND account=?", (rule_id, self.account))
        self.db.commit()
        return cur.rowcount > 0
