"""Song lyrics, timed when any source has timings.

Order: timed YouTube Music, timed LRCLIB, plain YouTube Music, plain LRCLIB. Only title and artist go to LRCLIB
(free, keyless). Timed results are kept a day in memory, plain or missing ones an hour; when a source errored only a
timed result is kept, so the next open retries.
"""
import logging
import re
import threading
import time

import requests

from . import config

LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{2}(?:[.:]\d{1,3})?)\]")
TTL = 24 * 3600
TTL_UNTIMED = 3600  # LRCLIB search results vary between calls; give a plain or missing result another go soon
logger = logging.getLogger(__name__)
_cache: dict[str, tuple[float, dict | None]] = {}
_lock = threading.Lock()


class SourceDown(Exception):
    """A lyrics source failed (timeout, 5xx). Not the same as "it has no lyrics"."""


def parse_lrc(lrc: str) -> list[dict]:
    """`[mm:ss.xx] text`; several stamps on one line repeat it; `[ar: …]`-style tags are skipped."""
    out = []
    for line in lrc.splitlines():
        stamps = LRC_STAMP.findall(line)
        if not stamps:
            continue
        text = LRC_STAMP.sub("", line).strip()
        for m, s in stamps:
            out.append({"t": round(int(m) * 60 + float(s.replace(":", ".")), 3), "text": text})
    return sorted(out, key=lambda ln: ln["t"])


def plain(text: str) -> list[dict]:
    return [{"t": None, "text": ln.strip()} for ln in text.strip("\n").splitlines()]


def _lrclib_search(params: dict) -> list[dict]:
    """/api/search, not /api/get: on 2026-11-03 /api/get answered a one-line "probe" upload for a hit song while
    search listed the real ones. SourceDown on a timeout or any non-200."""
    try:
        r = requests.get("https://lrclib.net/api/search", params=params, timeout=10, proxies=config.proxies(),
                         headers={"User-Agent": "tapedeck (private music server)"})
    except requests.RequestException as e:
        raise SourceDown(f"LRCLIB {type(e).__name__}") from e
    if r.status_code != 200:
        raise SourceDown(f"LRCLIB {r.status_code}")
    return r.json() or []


MIN_LINES = 4  # fewer non-empty lines than this is a stub or a test upload, not a song's lyrics


def _lrclib(title: str, artist: str, duration: int | None) -> tuple[dict | None, bool]:
    """(result, a search failed). A failed second search keeps what the first found."""
    def off(h):  # how far the upload's length is from ours; unknown either side = can't trust its timings
        return abs(h["duration"] - duration) if duration and h.get("duration") else float("inf")

    synced, plains = [], []

    def take(found):
        for h in found:
            if h.get("syncedLyrics"):
                lines = parse_lrc(h["syncedLyrics"])
                if sum(1 for ln in lines if ln["text"]) >= MIN_LINES:
                    synced.append((off(h), lines))
            if h.get("plainLyrics"):
                lines = plain(h["plainLyrics"])
                if sum(1 for ln in lines if ln["text"]) >= MIN_LINES:
                    plains.append((off(h), lines))

    take(_lrclib_search({"track_name": title, "artist_name": artist}))
    near = [c for c in synced if c[0] <= 5]  # timings from a different cut (live, extended) would drift
    down = False
    if not near:  # a looser free-text search often turns up timings the field search missed
        try:
            take(_lrclib_search({"q": f"{artist} {title}".strip()}))
        except SourceDown:
            down = True
        near = [c for c in synced if c[0] <= 5]
    if near:
        return {"synced": True, "source": "LRCLIB", "lines": min(near, key=lambda c: c[0])[1]}, down
    if plains or synced:
        best = min(plains or synced, key=lambda c: c[0])[1]
        return {"synced": False, "source": "LRCLIB", "lines": [{"t": None, "text": ln["text"]} for ln in best]}, down
    return None, down


def find(ytm, video_id: str) -> dict | None:
    with _lock:
        hit = _cache.get(video_id)
    if hit and time.time() - hit[0] < (TTL if hit[1] and hit[1]["synced"] else TTL_UNTIMED):
        return hit[1]
    down = False
    try:
        yt = ytm.lyrics(video_id)
    except Exception:  # ytmusicapi raises anything when YouTube changes; LRCLIB may still answer
        logger.info("YouTube Music lyrics failed for %s", video_id, exc_info=True)
        yt, down = None, True
    result = yt if yt and yt["synced"] else None
    if result is None:
        song = ytm.song(video_id)
        try:
            lr, partly_down = _lrclib(song.title, song.artists[0] if song.artists else "", song.duration)
            down = down or partly_down
        except SourceDown:
            lr, down = None, True
        result = lr if lr and lr["synced"] else (yt or lr)
    # A source that failed might have had better (timed) lyrics, so only a timed result outlives a failure.
    if not down or (result and result["synced"]):
        with _lock:
            _cache[video_id] = (time.time(), result)
            if len(_cache) > 2000:
                for k in sorted(_cache, key=lambda k: _cache[k][0])[:500]:
                    _cache.pop(k, None)
    return result
