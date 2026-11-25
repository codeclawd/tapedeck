"""YouTube Music catalog, unauthenticated (no account, no cookies)."""
import logging
import os
import random
import re
import string
import threading
import time

from ytmusicapi import YTMusic

from . import config
from .match import best, norm
from .models import Track

logger = logging.getLogger("tapedeck")
# The album/single track ("ATV") and label-uploaded audio. Music videos (OMV) and fan uploads (UGC) are not audio
# versions: they carry intros, skits and video-only edits.
AUDIO_TYPES = {"MUSIC_VIDEO_TYPE_ATV", "MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC"}


def is_audio(row: dict) -> bool:
    """A watch-playlist row is an audio version. Rows with no videoType (older shapes) are kept."""
    vt = row.get("videoType")
    return vt is None or vt in AUDIO_TYPES


def cover_url(thumbs: list | None, size: int = 544) -> str | None:
    """Largest thumbnail, re-sized and forced to JPEG (`-rj`): iOS 13 can't decode WebP."""
    if not thumbs:
        return None
    url = thumbs[-1].get("url") if isinstance(thumbs[-1], dict) else None
    if not url:
        return None
    if re.search(r"=w\d+-h\d+", url):
        return re.sub(r"=w\d+-h\d+.*$", f"=w{size}-h{size}-l90-rj", url)
    return url


def _artists(r: dict) -> tuple[list[str], str | None]:
    arts = [a for a in r.get("artists") or [] if a.get("name")]
    return [a["name"] for a in arts], next((a.get("id") for a in arts if a.get("id")), None)


def _track(r: dict, album: dict | None = None) -> Track:
    alb = r.get("album") or {}
    if isinstance(alb, str):  # get_album() tracks carry the album name as a plain string
        alb = {"name": alb}
    names, aid = _artists(r)
    return Track(
        title=r["title"],
        artists=names,
        duration=r.get("duration_seconds") or _secs(r.get("length") or r.get("duration")),
        album=alb.get("name") or (album or {}).get("title"),
        video_id=r.get("videoId"),
        album_id=alb.get("id"),
        cover=cover_url(r.get("thumbnails") or r.get("thumbnail") or (album or {}).get("thumbnails")),
        artist_id=aid,
    )


def _item(r: dict) -> Track | None:
    """A home/feed/search card: song, album, playlist or artist. Anything else is skipped."""
    if r.get("videoId"):
        return _track(r)
    names, aid = _artists(r)
    cover = cover_url(r.get("thumbnails"))
    bid = str(r.get("browseId") or "")
    if bid.startswith("MPRE"):
        return Track(title=r["title"], artists=names, album=r["title"], album_id=bid, kind="album",
                     cover=cover, artist_id=aid)
    if bid.startswith("UC"):
        return Track(title=r.get("title") or r.get("artist") or "", kind="artist", artist_id=bid, cover=cover)
    if r.get("playlistId"):
        sub = r.get("description") or ", ".join(names)
        return Track(title=r["title"], artists=[sub] if sub else [], kind="playlist",
                     playlist_id=r["playlistId"], cover=cover)
    return None


def _home_shelves(res: list[dict]) -> list[dict]:
    shelves = []
    for s in res:
        items = [t for t in (_item(r) for r in s.get("contents") or []) if t]
        if items:
            shelves.append({"title": s.get("title") or "", "items": items})
    return shelves


VISITOR_HEADER = "X-Goog-Visitor-Id"


class Visitor:
    """One signed-out YouTube Music visitor that stays the same across restarts. The owner's plays are reported
    under its id, so its home page learns their taste with no Google account. Measured 2026-11-20:
    8 play reports put that artist on the visitor's home (BLACKPINK, Fairuz, Tame Impala; a fresh id showed none),
    and a new session carrying only the saved id kept it."""

    def __init__(self, path, pace):
        self.api = YTMusic(proxies=config.proxies())
        self._pace = pace  # the shared YTM pacing: these calls count against the same budget
        saved = path.read_text().strip() if path.exists() else ""
        if saved:
            self.api.headers[VISITOR_HEADER] = saved
        else:
            new = self.api.headers.get(VISITOR_HEADER) or ""
            if not new:  # YouTube answered with a bot check instead of a page: try again on the next use
                raise ValueError("YouTube Music gave no visitor id")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never readable by others
            with os.fdopen(fd, "w") as f:
                f.write(new)
        self._home: tuple[float, list[dict]] | None = None

    def report_play(self, video_id: str) -> bool:
        """What music.youtube.com sends when a signed-out visitor starts a song (its playbackTracking URL)."""
        self._pace()
        song = self.api.get_song(video_id)
        url = ((song.get("playbackTracking") or {}).get("videostatsPlaybackUrl") or {}).get("baseUrl")
        if not url:
            return False
        cpn = "".join(random.choices(string.ascii_letters + string.digits + "-_", k=16))
        self._pace()
        r = self.api._session.get(url, params={"ver": 2, "c": "WEB_REMIX", "cpn": cpn}, headers=self.api.headers,
                                  proxies=self.api.proxies, timeout=15)
        return r.status_code == 204

    def home(self, limit: int = 6) -> list[dict]:
        if self._home and time.monotonic() - self._home[0] < 600:
            return self._home[1]
        self._pace()
        shelves = _home_shelves(self.api.get_home(limit=limit))
        self._home = (time.monotonic(), shelves)
        return shelves


class YTM:
    """One shared instance in the server: a lock keeps requests paced, and small TTL caches keep the UI fast."""

    def __init__(self, gap: float = 1.0):
        self.api = YTMusic(proxies=config.proxies())
        self.gap = gap
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache: dict[tuple, tuple[float, object]] = {}

    def _pace(self):  # at most one request per `gap` seconds, across threads
        with self._lock:
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _cached(self, key: tuple, ttl: float, fn):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        self._pace()
        val = fn()
        self._cache[key] = (time.monotonic(), val)
        if len(self._cache) > 500:  # crude bound; oldest first
            for k, _ in sorted(self._cache.items(), key=lambda kv: kv[1][0])[:100]:
                self._cache.pop(k, None)
        return val

    def search(self, query: str, limit: int = 10) -> list[Track]:
        res = self._cached(("search", query, limit), 600, lambda: self.api.search(query, filter="songs", limit=limit))
        return [_track(r) for r in res if r.get("videoId")][:limit]

    def search_all(self, query: str) -> dict[str, list[Track]]:
        """Songs, albums and artists from ONE unfiltered search (about 5-8 songs, 3 albums, 6 artists). Three filtered
        searches cost three paced upstream calls (~1.3 s); this is one (~0.5 s)."""
        res = self._cached(("all", query), 600, lambda: self.api.search(query))
        out: dict[str, list[Track]] = {"songs": [], "albums": [], "artists": []}
        seen: set[str] = set()
        for r in res:
            kind = {"song": "songs", "album": "albums", "artist": "artists"}.get(r.get("resultType"))
            if kind == "artists" and not r.get("browseId") and r.get("artists"):  # the top-result card
                r = {**r, "browseId": r["artists"][0].get("id"), "artist": r["artists"][0].get("name")}
            t = _item(r) if kind else None
            ident = t and (t.video_id or t.album_id or t.artist_id)
            if not t or not ident or ident in seen or (kind == "songs" and not t.video_id):
                continue
            seen.add(ident)
            if kind == "artists":
                t.title = r.get("artist") or t.title
            out[kind].append(t)
        return out

    def search_albums(self, query: str, limit: int = 10) -> list[Track]:
        res = self._cached(("albums", query, limit), 600, lambda: self.api.search(query, filter="albums", limit=limit))
        return [t for t in (_item(r) for r in res[:limit]) if t]

    def search_artists(self, query: str, limit: int = 10) -> list[Track]:
        res = self._cached(("artists", query, limit), 600, lambda: self.api.search(query, filter="artists", limit=limit))
        out = []
        for r in res[:limit]:
            if r.get("browseId"):
                out.append(Track(title=r.get("artist") or r.get("title") or "", kind="artist",
                                 artist_id=r["browseId"], cover=cover_url(r.get("thumbnails"))))
        return out

    def album(self, browse_id: str) -> dict:
        return self._cached(("album", browse_id), 3600, lambda: self.api.get_album(browse_id))

    def album_tracks(self, browse_id: str) -> list[Track]:
        a = self.album(browse_id)
        tracks = [_track(t, a) for t in a["tracks"] if t.get("videoId")]
        for t in tracks:
            t.album_id = browse_id
            t.album = t.album or a.get("title")
        return tracks

    def playlist_tracks(self, playlist_id: str, limit: int = 100) -> tuple[dict, list[Track]]:
        p = self._cached(("playlist", playlist_id), 1800, lambda: self.api.get_playlist(playlist_id, limit=limit))
        return p, [_track(t) for t in p.get("tracks") or [] if t.get("videoId")]

    def artist(self, channel_id: str) -> dict:
        return self._cached(("artist", channel_id), 3600, lambda: self.api.get_artist(channel_id))

    def radio(self, video_id: str, limit: int = 25) -> list[Track]:
        """Song radio, audio versions only. YouTube Music builds a music video's radio from music videos (measured:
        an OMV seed gave 50/50 OMV, its album track 50/50 audio), so a video seed is swapped for its album track
        first, and any video left over is dropped."""
        def fetch(vid):
            return self._cached(("radio", vid, limit), 1800,
                                lambda: self.api.get_watch_playlist(videoId=vid, radio=True, limit=limit))
        rows = fetch(video_id).get("tracks") or []
        seed = rows[0] if rows and rows[0].get("videoId") == video_id else None
        if seed and not is_audio(seed):
            try:
                song = self.song_version(_track(seed).to_dict())
            except Exception:
                logger.exception("no audio version for %s; its radio keeps only audio rows", video_id)
                song = None
            if song and song.get("video_id") and song["video_id"] != video_id:
                rows = fetch(song["video_id"]).get("tracks") or rows
        return [_track(t) for t in rows if t.get("videoId") and is_audio(t)]

    def artist_radio(self, playlist_id: str, limit: int = 25) -> list[Track]:
        """An artist's radio (`radioId`, RDEM...): led by that artist, mixed with the artists around them."""
        res = self._cached(("artist_radio", playlist_id, limit), 1800,
                           lambda: self.api.get_watch_playlist(playlistId=playlist_id, limit=limit))
        return [_track(t) for t in res.get("tracks") or [] if t.get("videoId") and is_audio(t)]

    def song_version(self, track: dict) -> dict | None:
        """The album track for a music video (anonymous YTM gives no counterpart): search songs by clean title and
        artist. None when no confident match. Cached for a day; the answer doesn't change."""
        from .genius import clean_title
        title = clean_title(track.get("title") or "")
        want = Track(title=title, artists=track.get("artists") or [])

        def find():
            hit = self.match(want) if title else None
            return hit.to_dict() if hit else None
        return self._cached(("song_version", track.get("video_id")), 86400, find)

    def home(self, limit: int = 6) -> list[dict]:
        return _home_shelves(self._cached(("home", limit), 1800, lambda: self.api.get_home(limit=limit)))

    def lyrics(self, video_id: str) -> dict | None:
        """YouTube Music's own lyrics (often Musixmatch): timed when the mobile client has timings, else plain."""
        def fetch():
            bid = self.api.get_watch_playlist(videoId=video_id, limit=1).get("lyrics")
            return self.api.get_lyrics(bid, timestamps=True) if bid else None
        res = self._cached(("lyrics", video_id), 3600, fetch)
        if not res or not res.get("lyrics"):
            return None
        source = (res.get("source") or "YouTube Music").removeprefix("Source: ")
        if res.get("hasTimestamps"):
            lines = [{"t": round(ln.start_time / 1000, 3), "text": ln.text} for ln in res["lyrics"]]
            return {"synced": True, "source": source, "lines": lines}
        return {"synced": False, "source": source,
                "lines": [{"t": None, "text": ln.strip()} for ln in res["lyrics"].strip("\n").splitlines()]}

    def song(self, video_id: str) -> Track:
        try:
            t = self._cached(("song", video_id), 3600,
                             lambda: self.api.get_watch_playlist(videoId=video_id, limit=1))["tracks"][0]
        except Exception as e:
            raise LookupError(f"YouTube Music has no track with id {video_id} ({type(e).__name__})") from e
        tr = _track(t)
        tr.video_id = video_id
        return tr

    def match(self, want: Track) -> Track | None:
        """YTM search results vary between calls, so try a second, cleaner query before giving up."""
        lead = want.artists[0].split(",")[0].strip() if want.artists else ""
        for query in (f"{want.artist} {want.title}", f"{lead} {norm(want.title)}"):
            hit = best(want, self.search(query, limit=5))
            if hit:
                hit.spotify_id = want.spotify_id
                return hit
        return None

    def meta(self, t: Track) -> dict:
        """Tags for a download: album/year/track number/cover from the YTM album when there is one."""
        m = {"title": t.title, "artist": t.artist, "album": t.album, "cover_url": t.cover,
             "comment": " ".join(x for x in (f"ytm:{t.video_id}", t.spotify_id and f"spotify:{t.spotify_id}") if x)}
        if not t.album_id:
            return m
        a = self.album(t.album_id)
        tracks = a.get("tracks") or []
        pos = next((x.get("trackNumber") for x in tracks if x.get("videoId") == t.video_id), None)
        pos = pos or next((x.get("trackNumber") for x in tracks if norm(t.title) and norm(x["title"]) == norm(t.title)), None)
        m.update(album=a.get("title") or t.album, year=a.get("year"), track=pos, total=len(tracks) or None,
                 album_artist=", ".join(x["name"] for x in a.get("artists") or []) or t.artist,
                 cover_url=(a.get("thumbnails") or [{}])[-1].get("url") or t.cover)
        return m


def _secs(length) -> int | None:
    if not length or not isinstance(length, str):
        return None
    total = 0
    try:
        for part in length.split(":"):
            total = total * 60 + int(part)
    except ValueError:
        return None
    return total
