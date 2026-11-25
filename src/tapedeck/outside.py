"""Other services' recommenders, used anonymously: Spotify's song radio and SoundCloud's related tracks.

tapedeck doesn't try to out-recommend services that train on millions of listeners. It asks each one "what goes
after this song?" and ranks the answers with your own history (service.autoplay, genius.build). No account, no
cookies, nothing about you is sent: only the seed song's title and artist, the same as a search.

- Spotify: the public embed page hands every visitor a short-lived anonymous token. With it, the web player's own
  search (`pathfinder searchTracks`) finds the song, and `inspiredby-mix seed_to_playlist` returns its "Song Radio"
  playlist, read through the embed page like any public playlist (50 songs). The Web API's /recommendations is
  closed to new apps (Nov 2024) and its search answers this token with 429, so neither is used. (Probed 2026-11-20.)
- SoundCloud: the site's public client_id opens `api-v2` search and `/tracks/{id}/related`.

Every failure is logged and returns [], so autoplay and the feed carry on with YouTube Music alone.
Audio never comes from here: each song is matched to its YouTube Music audio track before it can play.
"""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config, spotify
from .match import best
from .models import Track

logger = logging.getLogger("tapedeck")
TIMEOUT = 10
# How much each service's vote counts. SoundCloud's related tracks mix in fan re-uploads, so it counts less.
SOURCE_WEIGHT = {"youtube": 1.0, "spotify": 1.0, "soundcloud": 0.6}


def _artists(names: str) -> list[str]:
    """Spotify playlist rows credit "A, B, C" in one string; the first name leads."""
    return [a.strip() for a in (names or "").split(",") if a.strip()]


class _Http:
    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()

    def get(self, url: str, **kw) -> requests.Response:
        headers = {"User-Agent": config.UA, **kw.pop("headers", {})}
        return self.s.get(url, headers=headers, proxies=config.proxies(), timeout=TIMEOUT, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        headers = {"User-Agent": config.UA, **kw.pop("headers", {})}
        return self.s.post(url, headers=headers, proxies=config.proxies(), timeout=TIMEOUT, **kw)


class SpotifyRadio(_Http):
    name = "spotify"
    EMBED = "https://open.spotify.com/embed/track/4uLU6hMCjMI75M1A2tKUQC"  # any public track page carries the token
    HOME = "https://open.spotify.com/"
    CDN = "https://open.spotifycdn.com/cdn/build/web-player/"
    QUERY = "https://api-partner.spotify.com/pathfinder/v2/query"
    RADIO = "https://spclient.wg.spotify.com/inspiredby-mix/v2/seed_to_playlist/{}?response-format=json"

    def __init__(self, session=None):
        super().__init__(session)
        self._lock = threading.Lock()
        self._token: tuple[str | None, float] = (None, 0.0)
        self._hash: str | None = None
        self._lead_artist: dict[str, str] = {}  # spotify track id -> its lead artist's uri, from search results

    def token(self) -> str:
        with self._lock:
            tok, expires_ms = self._token
            if tok and time.time() * 1000 < expires_ms - 60_000:
                return tok
            html = self.get(self.EMBED).text
            m = re.search(r'"accessToken":"([^"]+)"', html)
            e = re.search(r'"accessTokenExpirationTimestampMs":(\d+)', html)
            if not m:
                raise ValueError("the Spotify embed page carries no anonymous token any more")
            self._token = (m.group(1), float(e.group(1)) if e else time.time() * 1000 + 600_000)
            return m.group(1)

    def search_hash(self, fresh: bool = False) -> str:
        """The persisted-query hash of the web player's track search. It changes with web player releases, so it
        is read from the player's own code: main bundle -> chunk name hash -> `xpui-routes-search` chunk."""
        if self._hash and not fresh:
            return self._hash
        home = self.get(self.HOME).text
        for js in re.findall(r'https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[0-9a-f]+\.js', home):
            main = self.get(js).text
            cid = re.search(r'(\d+):"xpui-routes-search"', main)
            if not cid:
                continue
            for h in re.findall(rf'{cid.group(1)}:"([0-9a-f]{{8}})"', main):  # a JS and a CSS chunk share the id
                chunk = self.get(f"{self.CDN}xpui-routes-search.{h}.js")
                m = chunk.ok and re.search(r'"searchTracks","query","([0-9a-f]{64})"', chunk.text)
                if m:
                    self._hash = m.group(1)
                    return self._hash
        raise ValueError("couldn't find Spotify's track search in the web player code")

    def search(self, query: str, limit: int = 5) -> list[Track]:
        body = lambda h: {"operationName": "searchTracks", "variables": {
            "searchTerm": query, "offset": 0, "limit": limit, "numberOfTopResults": limit,
            "includeAudiobooks": False, "includePreReleases": False},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": h}}}
        headers = lambda: {"Authorization": f"Bearer {self.token()}", "app-platform": "WebPlayer"}
        r = self.post(self.QUERY, json=body(self.search_hash()), headers=headers())
        if r.status_code == 401:  # token revoked early
            self._token = (None, 0.0)
            r = self.post(self.QUERY, json=body(self.search_hash()), headers=headers())
        stale = r.status_code in (400, 412) or (r.ok and "PersistedQueryNotFound" in r.text)
        if stale:  # "Invalid query hash": a new web player release; read it again once
            r = self.post(self.QUERY, json=body(self.search_hash(fresh=True)), headers=headers())
        r.raise_for_status()
        data = r.json()
        if len(self._lead_artist) > 5000:
            self._lead_artist.clear()
        for i in (((data.get("data") or {}).get("searchV2") or {}).get("tracksV2") or {}).get("items") or []:
            d = ((i or {}).get("item") or {}).get("data") or {}
            lead = next(iter((d.get("artists") or {}).get("items") or []), {}).get("uri") or ""
            if d.get("uri") and lead.startswith("spotify:artist:"):
                self._lead_artist[d["uri"].rsplit(":", 1)[-1]] = lead
        return parse_search(data)

    def radio(self, seed_uri: str) -> list[Track]:
        """Spotify's radio for a song (`spotify:track:…`) or an artist (`spotify:artist:…`): 50 songs."""
        r = self.get(self.RADIO.format(seed_uri), headers={"Authorization": f"Bearer {self.token()}"})
        r.raise_for_status()
        uri = next((i.get("uri") for i in r.json().get("mediaItems") or [] if i.get("uri")), None)
        if not uri:
            return []
        _, tracks, _ = spotify.fetch(f"https://open.spotify.com/playlist/{uri.rsplit(':', 1)[-1]}")
        for t in tracks:
            t.artists = _artists(t.artists[0]) if t.artists else []
        return [t for t in tracks if f"spotify:track:{t.spotify_id}" != seed_uri]

    def _find(self, seed: Track) -> Track | None:
        return best(seed, self.search(f"{seed.artist} {seed.title}"))

    def similar(self, seed: Track) -> list[Track]:
        hit = self._find(seed)
        return self.radio(f"spotify:track:{hit.spotify_id}") if hit and hit.spotify_id else []

    def artist_similar(self, seed: Track) -> list[Track]:
        """The radio of the seed song's lead artist: that artist with the artists Spotify's listeners pair them with."""
        hit = self._find(seed)
        artist = hit and self._lead_artist.get(hit.spotify_id)
        return self.radio(artist) if artist else []


def parse_search(data: dict) -> list[Track]:
    out = []
    for i in (((data.get("data") or {}).get("searchV2") or {}).get("tracksV2") or {}).get("items") or []:
        d = ((i or {}).get("item") or {}).get("data") or {}
        uri, name = d.get("uri") or "", d.get("name")
        if not name or not uri.startswith("spotify:track:"):
            continue
        ms = (d.get("duration") or {}).get("totalMilliseconds")
        out.append(Track(title=name, spotify_id=uri.rsplit(":", 1)[-1],
                         artists=[a["profile"]["name"] for a in (d.get("artists") or {}).get("items") or []
                                  if (a.get("profile") or {}).get("name")],
                         album=(d.get("albumOfTrack") or {}).get("name"), duration=round(ms / 1000) if ms else None))
    return out


class SoundCloudRelated(_Http):
    name = "soundcloud"
    HOME = "https://soundcloud.com/"
    API = "https://api-v2.soundcloud.com"

    def __init__(self, session=None):
        super().__init__(session)
        self._lock = threading.Lock()
        self._cid: str | None = None

    def client_id(self, fresh: bool = False) -> str:
        with self._lock:
            if self._cid and not fresh:
                return self._cid
            home = self.get(self.HOME).text
            for js in reversed(re.findall(r'https://a-v2\.sndcdn\.com/assets/[^"]+\.js', home)):  # it's in a late one
                m = re.search(r'client_id:"([A-Za-z0-9]{20,40})"', self.get(js).text)
                if m:
                    self._cid = m.group(1)
                    return self._cid
            raise ValueError("couldn't find SoundCloud's public client_id")

    def _api(self, path: str, **params) -> dict:
        r = self.get(f"{self.API}{path}", params={**params, "client_id": self.client_id()})
        if r.status_code in (401, 403):  # the public id rotates
            r = self.get(f"{self.API}{path}", params={**params, "client_id": self.client_id(fresh=True)})
        r.raise_for_status()
        return r.json()

    def similar(self, seed: Track) -> list[Track]:
        found = [(parse_sc(t), t.get("id")) for t in self._api("/search/tracks", q=f"{seed.artist} {seed.title}",
                                                              limit=5).get("collection") or []]
        hit = best(seed, [t for t, _ in found])
        sid = next((i for t, i in found if t is hit), None)
        if not sid:
            return []
        return [parse_sc(t) for t in self._api(f"/tracks/{sid}/related", limit=30).get("collection") or []
                if t.get("title")]


def parse_sc(t: dict) -> Track:
    """SoundCloud credits the label's artist in publisher_metadata; fan uploads only have "Artist - Title"."""
    title, artist = t.get("title") or "", (t.get("publisher_metadata") or {}).get("artist")
    if not artist and " - " in title:
        artist, title = (s.strip() for s in title.split(" - ", 1))
    artist = artist or (t.get("user") or {}).get("username") or ""
    ms = t.get("full_duration") or t.get("duration")
    return Track(title=title, artists=_artists(artist), duration=round(ms / 1000) if ms else None)


class Sources:
    """Asks every outside service at once; one that fails or is slow just doesn't vote."""

    def __init__(self, sources=None, budget: float = 12.0):
        self.sources = sources if sources is not None else [SpotifyRadio(), SoundCloudRelated()]
        self.budget = budget
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="outside")

    def similar(self, seed: dict) -> dict[str, list[dict]]:
        """Songs like this song, from every service."""
        return self._ask(seed, "similar")

    def artist_similar(self, seed: dict) -> dict[str, list[dict]]:
        """Songs for an artist mix seeded by this song's artist, from the services that have artist radio."""
        return self._ask(seed, "artist_similar")

    def _ask(self, seed: dict, method: str) -> dict[str, list[dict]]:
        want = Track.from_dict(seed)
        if not want.title or not want.artists:
            return {}
        jobs = {s.name: self._pool.submit(getattr(s, method), want) for s in self.sources if hasattr(s, method)}
        out, deadline = {}, time.monotonic() + self.budget
        for name, job in jobs.items():
            try:
                out[name] = [t.to_dict() for t in job.result(timeout=max(0.1, deadline - time.monotonic()))]
            except Exception as e:
                job.cancel()  # still queued: don't run it late; running: its own HTTP timeouts end it
                logger.warning("%s recommendations unavailable for %r: %s: %s", name, want.title, type(e).__name__, e)
        return out
