"""Resolve a YouTube Music track to its raw audio file URL. No ad pipeline is involved."""
import logging
import re
import shutil
import subprocess
import threading
import time

import requests
import yt_dlp

from . import config
from .library import FORMAT

BLOCKED_MESSAGE = ("YouTube is blocking the server's connection right now. It's switching to another one; "
                   "try again in a minute.")


class Blocked(Exception):
    """YouTube asks this server to "confirm you're not a bot": its network exit is flagged. Other player clients
    don't help (measured 2026-11-03: android_vr, tv_simply, web_safari, mweb, web_embedded and tv all lack the m4a
    without a sign-in), so the cure is another VPN exit."""

    def __init__(self):
        super().__init__(BLOCKED_MESSAGE)


class Gone(LookupError):
    """The video was taken down or made private."""


def classify(e: Exception) -> Exception:
    """yt-dlp's error as Blocked, Gone, or itself."""
    msg = str(e)
    if "not a bot" in msg or "Sign in to confirm" in msg:
        return Blocked()
    if any(w in msg for w in ("Video unavailable", "This video is unavailable", "has been removed", "Private video")):
        return Gone("This song isn't on YouTube anymore.")
    return e


logger = logging.getLogger("tapedeck")

# ---- fast path: one innertube player call ------------------------------------------------------
# Measured 2026-11-04: yt-dlp takes 1.8-2.3 s for a never-played song (watch page ~1 s, /next ~0.7 s,
# player ~0.6 s, HLS info ~0.2 s). Only the player answer carries the audio URL. Asking for it directly, with the
# same JS-free visionOS client yt-dlp itself picks here (yt-dlp 2026.10.26 `_base.py` INNERTUBE_CLIENTS['visionos']),
# takes 0.12-0.23 s and the m4a (itag 140) URL plays (206). Anything unexpected falls back to yt-dlp.
_VISION = {"clientName": "VISIONOS", "clientVersion": "1.02", "deviceMake": "Apple", "deviceModel": "RealityDevice17,1",
           "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/26.0 Safari/605.1.15",
           "osName": "visionOS", "osVersion": "26.5.23O471", "hl": "en"}
PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
VISITOR_TTL = 3600
_fast = {"session": None, "visitor": None, "at": 0.0}
_fast_lock = threading.Lock()


def _session() -> requests.Session:
    with _fast_lock:
        if _fast["session"] is None:
            _fast["session"] = requests.Session()  # keep-alive: no new TLS handshake per song
        return _fast["session"]


def _visitor(force: bool = False) -> str | None:
    """YouTube Music's anonymous visitor id, read from its home page once an hour (what the watch page gave yt-dlp)."""
    with _fast_lock:
        if not force and _fast["visitor"] and time.time() - _fast["at"] < VISITOR_TTL:
            return _fast["visitor"]
    html = _session().get("https://music.youtube.com/", headers={"User-Agent": config.UA}, timeout=10,
                          proxies=config.proxies()).text
    m = re.search(r'"VISITOR_DATA":"([^"]+)"', html)
    with _fast_lock:
        _fast["visitor"], _fast["at"] = (m.group(1) if m else None), time.time()
        return _fast["visitor"]


def fast_resolve(video_id: str) -> tuple[str, float | None] | None:
    """(m4a URL, length in seconds) from one player call, or None to fall back to yt-dlp. Raises Blocked on a bot check."""
    vd = _visitor()
    if not vd:
        return None
    body = {"context": {"client": {**_VISION, "visitorData": vd}}, "videoId": video_id,
            "contentCheckOk": True, "racyCheckOk": True}
    headers = {"User-Agent": _VISION["userAgent"], "X-YouTube-Client-Name": "101", "X-YouTube-Client-Version": "1.02",
               "X-Goog-Visitor-Id": vd, "Origin": "https://www.youtube.com"}
    j = _session().post(PLAYER_URL, json=body, headers=headers, timeout=10, proxies=config.proxies()).json()
    status = j.get("playabilityStatus") or {}
    if status.get("status") != "OK":
        reason = " ".join(str(status.get(k) or "") for k in ("reason", "messages"))
        if "not a bot" in reason or "Sign in to confirm" in reason:
            raise Blocked()
        if status.get("status") in ("ERROR", "UNPLAYABLE") and "unavailable" in reason.lower():
            raise Gone("This song isn't on YouTube anymore.")
        return None
    for f in (j.get("streamingData") or {}).get("adaptiveFormats") or []:
        if f.get("itag") == 140 and f.get("url"):
            ms = f.get("approxDurationMs")
            return f["url"], (int(ms) / 1000 if ms and str(ms).isdigit() else None)
    return None  # signed (cipher) URLs or no m4a: yt-dlp knows how to handle those


def resolve(video_id: str) -> str:
    """URL is bound to this machine's IP and expires after ~6 h; resolve where you play or proxy."""
    try:
        hit = fast_resolve(video_id)
        if hit:
            return hit[0]
    except (Blocked, Gone):
        raise
    except (requests.RequestException, ValueError, KeyError, TypeError):
        logger.info("fast resolve of %s failed; using yt-dlp", video_id, exc_info=True)
    try:
        with yt_dlp.YoutubeDL(config.ytdl_opts(format=FORMAT)) as y:
            return y.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=False)["url"]
    except yt_dlp.utils.DownloadError as e:
        err = classify(e)
        if err is e:
            raise
        raise err from e


def play(url: str, title: str = "") -> int:
    if shutil.which("mpv"):
        cmd = ["mpv", "--no-video", "--really-quiet", f"--force-media-title={title}", url]
    elif shutil.which("ffplay"):
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", url]
    else:
        raise SystemExit("Install mpv or ffmpeg (ffplay) to play locally, or use --url.")
    return subprocess.call(cmd)
