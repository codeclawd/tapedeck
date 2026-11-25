"""Per-person accounts (owner request 2026-11-03: "everyone has their own feed").

The env owner token is the "owner" account (admin). Extra people get tokens from `tapedeck account add`.
Listening events, likes, history, taste, the Genius feed and privacy rules belong to one account; the
downloaded library is shared. Agents keep seeing the owner account, filtered.
"""
import sqlite3

import pytest
from starlette.testclient import TestClient

from tapedeck import service
from tapedeck.server import create_app
from tapedeck.store import Store

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"
T = {"video_id": "aaaaaaaaaaa", "title": "Song A", "artists": ["Artist A"], "artist_id": "UCa"}
U = {"video_id": "bbbbbbbbbbb", "title": "Song B", "artists": ["Artist B"]}


def h(t):
    return {"Authorization": f"Bearer {t}"}


# ---- store -------------------------------------------------------------------------------------
def test_events_and_likes_belong_to_one_account(tmp_path):
    db = tmp_path / "t.db"
    owner, sara = Store(db), Store(db, account="sara")
    owner.add_event(T, "finish")
    sara.add_event(U, "like")
    assert set(owner.track_scores()) == {"aaaaaaaaaaa"}
    assert set(sara.track_scores()) == {"bbbbbbbbbbb"}
    assert owner.liked() == set() and sara.liked() == {"bbbbbbbbbbb"}
    assert [e["video_id"] for e in sara.history(10)] == ["bbbbbbbbbbb"]
    assert owner.count_events(("finish", "like")) == 1


def test_privacy_rules_are_per_account_and_the_same_rule_can_exist_twice(tmp_path):
    db = tmp_path / "t.db"
    owner, sara = Store(db), Store(db, account="sara")
    owner.add_rule("artist", "Private Person")
    sara.add_rule("artist", "Private Person")
    assert len(owner.rules()) == 1 and len(sara.rules()) == 1
    assert sara.remove_rule(owner.rules()[0]["id"]) is False  # can't remove someone else's rule
    assert len(owner.rules()) == 1


def test_an_existing_database_migrates_to_the_owner_account(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)  # the 0.2.0 schema, before accounts
    con.executescript("""
        CREATE TABLE events (ts REAL, video_id TEXT, title TEXT, artist TEXT, album TEXT,
                             album_id TEXT, artist_id TEXT, cover TEXT, event TEXT);
        CREATE TABLE private (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT,
                              ref TEXT, label TEXT, created REAL, UNIQUE(kind, key));
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO events VALUES (1, 'aaaaaaaaaaa', 'Song A', 'Artist A', NULL, NULL, NULL, NULL, 'finish');
        INSERT INTO private (kind, key, created) VALUES ('artist', 'someone', 1);
    """)
    con.commit()
    con.close()
    owner, sara = Store(db), Store(db, account="sara")
    assert set(owner.track_scores()) == {"aaaaaaaaaaa"}
    assert sara.track_scores() == {}
    assert len(owner.rules()) == 1 and sara.rules() == []
    sara.add_rule("artist", "someone")  # the old UNIQUE(kind, key) would refuse this
    assert len(sara.rules()) == 1
    Store(db)  # opening again is a no-op
    assert len(owner.rules()) == 1


def test_account_tokens_are_stored_hashed(tmp_path):
    db = tmp_path / "t.db"
    s = Store(db)
    tok = s.add_account("sara")
    assert len(tok) >= 24
    assert s.account_for_token(tok) == "sara"
    assert s.account_for_token("nope-nope-nope-nope") is None
    raw = sqlite3.connect(db).execute("SELECT * FROM accounts").fetchall()
    assert tok not in repr(raw)
    with pytest.raises(ValueError):
        s.add_account("sara")  # names are unique
    with pytest.raises(ValueError):
        s.add_account("owner")  # reserved for the env token
    with pytest.raises(ValueError):
        s.add_account("Bad Name!")
    assert [a["name"] for a in s.accounts()] == ["sara"]
    assert s.remove_account("sara") is True
    assert s.account_for_token(tok) is None


# ---- HTTP --------------------------------------------------------------------------------------
@pytest.fixture
def client(deck_env, tmp_path):
    app = create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def sara(client):
    return Store().add_account("sara")


def test_whoami_names_the_account(client, sara):
    assert client.get("/api/whoami", headers=h(OWNER)).json()["account"] == "owner"
    me = client.get("/api/whoami", headers=h(sara)).json()
    assert me["scope"] == "owner" and me["account"] == "sara"
    assert client.get(f"/api/whoami?t={sara}").json()["account"] == "sara"  # media players use ?t=
    assert client.get("/api/whoami", headers=h(AGENT)).json()["account"] == "owner"


def test_history_likes_and_feed_are_separate_per_person(client, sara):
    ev = {"track": T, "event": "like"}
    assert client.post("/api/events", headers=h(sara), json=ev).status_code == 200
    assert [e["video_id"] for e in client.get("/api/history", headers=h(sara)).json()] == ["aaaaaaaaaaa"]
    assert client.get("/api/history", headers=h(OWNER)).json() == []
    assert [t["video_id"] for t in client.get("/api/likes", headers=h(sara)).json()] == ["aaaaaaaaaaa"]
    assert client.get("/api/likes", headers=h(OWNER)).json() == []
    assert client.get("/api/feed", headers=h(OWNER)).json()["cold_start"] is True
    assert client.get("/api/feed", headers=h(sara)).json()["cold_start"] is False


def test_only_the_owner_account_changes_agent_permissions(client, sara):
    assert client.put("/api/permissions", headers=h(sara), json={"feed": False}).status_code == 403
    assert client.put("/api/permissions", headers=h(OWNER), json={"feed": False}).status_code == 200


def test_a_person_can_only_undo_their_own_downloads(client, sara):
    owner_job = service.Deck("owner").log.new_job(label="owner's", actor="owner", status="done")
    sara_job = service.Deck("owner", account="sara").log.new_job(label="sara's", actor="sara", status="done")
    assert client.post(f"/api/jobs/{owner_job}/undo", headers=h(sara)).status_code == 403
    # allowed through; 404 only because these test jobs recorded no files
    assert client.post(f"/api/jobs/{sara_job}/undo", headers=h(sara)).status_code == 404


def test_a_removed_account_is_locked_out(client, sara):
    Store().remove_account("sara")
    assert client.get("/api/whoami", headers=h(sara)).status_code == 401


# ---- bugs a client app found (2026-11-03) --------------------------------------------------------
def test_listen_again_items_say_they_are_songs(deck_env):
    """Was On Repeat; Genius v3 (2026-11-05) replaced it with Listen again, which needs a few days' gap."""
    import time
    d = service.Deck("owner")
    for _ in range(3):
        d.store.add_event(T, "finish", ts=time.time() - 5 * 86400)
    shelves = d.feed(refresh=True)["shelves"]
    again = next(s for s in shelves if s["id"] == "listen-again")
    assert again["items"] and all(i.get("kind") == "song" for i in again["items"])


def test_cover_proxy_adds_the_missing_jpg_extension(client, monkeypatch):
    from tapedeck import server
    seen = []

    class R:
        status_code = 200
        headers = {"content-type": "image/jpeg"}  # noqa: RUF012 (test fake)

        def iter_content(self, n):
            yield b"\xff\xd8jpeg"

        def close(self):
            pass

    monkeypatch.setattr(server, "_fetch_cover", lambda u: seen.append(u) or R(), raising=False)
    client.get("/api/cover?u=https://i.ytimg.com/vi/SD4yRDY9mek/hq720&s=600", headers=h(OWNER))
    assert seen and seen[0].endswith("/hq720.jpg"), seen


# ---- CLI ---------------------------------------------------------------------------------------
def test_cli_adds_lists_and_removes_people(deck_env, capsys, monkeypatch):
    from tapedeck import cli
    for v in ("CLAUDECODE", "AI_AGENT", "TAPEDECK_SCOPE"):
        monkeypatch.delenv(v, raising=False)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("a-long-enough-password\n"))
    cli.main(["account", "add", "sara", "--password-stdin"])  # since sign-in landed, add sets a password
    assert "sara" in capsys.readouterr().out and "sara" in [a["name"] for a in Store().accounts()]
    cli.main(["account", "list"])
    assert "sara" in capsys.readouterr().out
    cli.main(["account", "remove", "sara"])
    assert Store().accounts() == []
    monkeypatch.setenv("TAPEDECK_SCOPE", "agent")
    with pytest.raises(SystemExit):
        cli.main(["account", "add", "bob", "--password-stdin"])
    assert Store().accounts() == []


# ---- review fixes (2026-11-03) -----------------------------------------------------------------
def test_migration_recovers_from_a_half_finished_rebuild(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)  # 0.2.0 schema plus a stray private_new left by an interrupted run
    con.executescript("""
        CREATE TABLE events (ts REAL, video_id TEXT, title TEXT, artist TEXT, album TEXT,
                             album_id TEXT, artist_id TEXT, cover TEXT, event TEXT);
        CREATE TABLE private (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT,
                              ref TEXT, label TEXT, created REAL, UNIQUE(kind, key));
        CREATE TABLE private_new (id INTEGER);
        INSERT INTO private (kind, key, created) VALUES ('artist', 'someone', 1);
    """)
    con.commit()
    con.close()
    assert [r["key"] for r in Store(db).rules()] == ["someone"]


def test_agent_is_a_reserved_account_name(tmp_path):
    with pytest.raises(ValueError):
        Store(tmp_path / "t.db").add_account("agent")


def test_privacy_rules_are_the_owner_accounts_only(client, sara):
    # rules hide things from agents, and agents only ever see the owner account
    assert client.get("/api/private", headers=h(sara)).status_code == 403
    assert client.post("/api/private", headers=h(sara), json={"kind": "artist", "key": "x"}).status_code == 403
    assert client.get("/api/private", headers=h(OWNER)).status_code == 200


def test_a_person_sees_only_their_own_download_jobs(client, sara):
    service.Deck("owner").log.new_job(label="owner's", actor="owner", status="done")
    service.Deck("owner", account="sara").log.new_job(label="sara's", actor="sara", status="done")
    assert [j["label"] for j in client.get("/api/jobs", headers=h(sara)).json()] == ["sara's"]
    assert len(client.get("/api/jobs", headers=h(OWNER)).json()) == 2
