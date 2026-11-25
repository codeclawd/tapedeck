"""Spotify catalog without an account or API key: public embed pages carry the track list."""
import json
import re

import requests

from . import config
from .models import Track

LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?|spotify:)(playlist|album|track)[/:]([A-Za-z0-9]{22})")
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
EMBED_CAP = 100  # playlist embeds stop at 100 tracks (probe 2026-11-02)


def parse_link(text: str) -> tuple[str, str] | None:
    m = LINK.search(text)
    return (m.group(1), m.group(2)) if m else None


def _id(uri: str | None) -> str | None:
    return uri.rsplit(":", 1)[-1] if uri else None


def _secs(ms) -> int | None:
    return round(ms / 1000) if ms else None


def parse_embed(html: str) -> tuple[str, list[Track], bool]:
    """Return (name, tracks, truncated). Raises ValueError if Spotify changed the page shape."""
    try:
        return _parse(html)
    except (AttributeError, KeyError, TypeError) as e:
        raise ValueError(f"Spotify embed page has an unexpected shape ({type(e).__name__}: {e})") from e


def _parse(html: str) -> tuple[str, list[Track], bool]:
    m = NEXT_DATA.search(html)
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as e:
        raise ValueError("Spotify embed page has no track data; the page format may have changed") from e
    name = entity.get("name") or entity.get("title") or ""
    if entity.get("type") == "track":
        t = Track(title=name, artists=[a["name"] for a in entity.get("artists") or [] if isinstance(a, dict) and a.get("name")],
                  duration=_secs(entity.get("duration")), spotify_id=_id(entity.get("uri")))
        return name, [t], False
    album = name if entity.get("type") == "album" else None
    tracks = [
        Track(title=i["title"], artists=[i["subtitle"]] if i.get("subtitle") else [],
              duration=_secs(i.get("duration")), album=album, spotify_id=_id(i.get("uri")))
        for i in entity.get("trackList") or []
        if isinstance(i, dict) and i.get("title")
    ]
    return name, tracks, entity.get("type") == "playlist" and len(entity.get("trackList") or []) >= EMBED_CAP


def fetch(link: str) -> tuple[str, list[Track], bool]:
    parsed = parse_link(link)
    if not parsed:
        raise ValueError(f"Not a Spotify playlist/album/track link: {link}")
    kind, sid = parsed
    r = requests.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers={"User-Agent": config.UA},
                     proxies=config.proxies(), timeout=20)
    r.raise_for_status()
    return parse_embed(r.text)
