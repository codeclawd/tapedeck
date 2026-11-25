"""The one place that decides what an agent may see.

Scopes: "owner" sees everything. "agent" (MCP, the agent token, the CLI run by an agent) never sees a song,
artist or album the owner marked private in anything *personal*: library, feed, likes, taste, download jobs.
It never sees file paths or timestamped history, and can't read or change the rules themselves.

Public catalog results (search, album, artist, radio pages) are NOT filtered: they are the same for everyone,
and an empty search for an artist would itself reveal that the artist is hidden. Instead, catalog items never
carry personal marks (owned/liked) for private things in agent scope. Every read in service.Deck goes through
Filter, so no route or tool can forget it.

Two more rules close probing tricks: a song an agent itself asked to download is never hidden from that agent
(it already knows it, and hiding it would reveal the privacy rule), and what agents may do at all is set by the
owner's permissions (PERMISSIONS below), enforced in service.Deck.
"""
from .match import norm

SCOPES = ("owner", "agent")

# What agents may do, owner-editable on the web Privacy page. Values: True/False, or for undo "off"/"own"/"all".
PERMISSIONS = {
    "library": True,     # see tapedeck's downloads
    "feed": True,        # see the Genius feed and top artists
    "likes": True,       # see liked songs
    "download": True,    # queue downloads into the library
    "jobs": True,        # see download activity
    "undo": "own",       # remove downloads: "off", only jobs an agent started ("own"), or any job ("all")
}


def key(name: str | None) -> str:
    """Compact comparison key for artist/album names: case, accents, punctuation and spaces ignored.
    Names made only of punctuation ("!!!") keep their raw lowercase form so they can still be hidden."""
    k = norm(name or "").replace(" ", "")
    return k or (name or "").strip().lower()


def split_artists(names) -> list[str]:
    """Library rows and events store "A, B" as one string; rules match each credited artist."""
    if isinstance(names, str):
        names = [names]
    return [a.strip() for n in names or [] for a in str(n).split(",") if a.strip()]


class Filter:
    def __init__(self, scope: str, rules: list[dict], exempt: set[str] = frozenset()):
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        self.scope = scope
        self.exempt = set(exempt)  # songs this agent asked for itself
        self.songs = {r["key"] for r in rules if r["kind"] == "song"}
        self.artist_keys = {r["key"] for r in rules if r["kind"] == "artist"}
        self.artist_ids = {r["ref"] for r in rules if r["kind"] == "artist" and r.get("ref")}
        self.album_ids = {r["key"] for r in rules if r["kind"] == "album"}
        self.album_keys = {r["ref"] for r in rules if r["kind"] == "album" and r.get("ref")}

    @property
    def owner(self) -> bool:
        return self.scope == "owner"

    def hidden(self, t: dict) -> bool:
        """t is a Track dict (song/album/artist/playlist card) or a library row."""
        if self.owner or (t.get("video_id") and t["video_id"] in self.exempt):
            return False
        if t.get("video_id") and t["video_id"] in self.songs:
            return True
        names = split_artists(t.get("artists")) + split_artists(t.get("artist"))
        if t.get("kind") == "artist":
            names.append(t.get("title") or "")
        if any(key(n) in self.artist_keys for n in names if n):
            return True
        if t.get("artist_id") and t["artist_id"] in self.artist_ids:
            return True
        if t.get("album_id") and t["album_id"] in self.album_ids:
            return True
        album = t.get("album") or (t.get("title") if t.get("kind") == "album" else None)
        return bool(album and key(album) in self.album_keys)

    def track(self, t: dict) -> dict:
        if self.owner:
            return t
        return {k: v for k, v in t.items() if k not in ("path", "ts", "created")}

    def tracks(self, items: list[dict]) -> list[dict]:
        """Personal lists: private items are dropped entirely."""
        return [self.track(t) for t in items if not self.hidden(t)]

    def catalog(self, items: list[dict], owned: set[str], liked: set[str]) -> list[dict]:
        """Public catalog lists: every item stays, personal marks are added except on private items for agents."""
        out = []
        for t in items:
            t = dict(t)
            if t.get("video_id"):
                visible = not self.hidden(t)  # private items look exactly like ones you don't own: no tell
                t["owned"] = visible and t["video_id"] in owned
                t["liked"] = visible and t["video_id"] in liked
            out.append(t)
        return out

    def feed(self, shelves: list[dict]) -> list[dict]:
        out = []
        for s in shelves:
            if not self.owner and (s.get("owner_only") or (s.get("seed") and self.hidden(s["seed"]))):
                continue  # a shelf built from a private song would reveal it ("Because you played …")
            items = self.tracks(s.get("items") or [])
            if items:
                shelf = {k: v for k, v in s.items() if k not in ("items", "seed")}
                out.append({**shelf, "items": items})
        return out
