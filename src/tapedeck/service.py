"""Deck: everything the API, the web UI, MCP tools and the CLI can do, in one place, with the privacy filter
applied inside every read (see privacy.py). Routes and tools stay thin and can't skip it."""
import logging
import os
import queue
import random
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from concurrent.futures import ThreadPoolExecutor

import requests
from ytmusicapi.exceptions import YTMusicError

from . import config, navidrome, outside, spotify, stream
from .genius import _song_key, build
from .library import Log, download
from .models import Track
from .privacy import Filter, key
from .store import OWNER, Store
from .ytm import YTM, Visitor, _item, _track, cover_url

logger = logging.getLogger("tapedeck")

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
FEED_TTL = 1800
CHECK_VIDEO = "MX7FSAnRPug"  # a stable, ordinary song used to test that YouTube audio is reachable
BLOCKED_TRIGGER_EVERY = 2 * 3600
BOT_CHECK_TRIGGER_EVERY = 15 * 60  # a bot-flagged exit refuses every song, so don't wait two hours to move

PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ytm: YTM | None = None
_outside: "outside.Sources | None" = None
_ytm_lock = threading.Lock()
_visitor: Visitor | None = None
_visitor_lock = threading.Lock()
_report_worker_lock = threading.Lock()  # not _visitor_lock: creating the visitor fetches from the network
_visitor_q: "queue.Queue[str]" = queue.Queue()
_visitor_worker: threading.Thread | None = None
_feeds: dict[str, dict] = {}
_feed_locks: dict[str, threading.Lock] = {}
_feed_locks_guard = threading.Lock()
# The next feed, kept built ahead in the background, so pulling to refresh swaps it in at once instead of waiting
# 7-15 s of paced YouTube Music calls (owner 2026-11-20: the phone gave up with "cancelled"; 2026-11-21: "very
# slow"). The ready one stays usable while its replacement is built; songs judged since are filtered out of it.
_spares: dict[str, list[dict]] = {}  # next first; each built from the impressions of the one before it
SPARE_DEPTH = 2  # two ready, so pulling twice in a row is instant too (owner pulls 2-3 times)
_spare_locks: dict[str, threading.Lock] = {}  # held while one is being built
_spare_again: dict[str, tuple[bool, bool]] = {}  # (promote, fresh) asked for mid-build: run once more after
_spare_timers: dict[str, threading.Timer] = {}
_spare_gen: dict[str, int] = {}  # bumped by a history clear: anything built before it is from forgotten listening
_spares_guard = threading.Condition()  # short holds only: the dicts above; notified when a spare lands or a build ends
SPARE_QUIET = 20  # seconds without a new play or verdict before the feed built ahead is redone


def _impressions(base: dict) -> dict[str, float]:
    """Churn (YouTube 2016: "successive requests do not return identical lists"): songs the last feed showed near
    the front of a row count as an impression; older impressions fade by 0.7 per build. Played songs are
    banned from recommendations anyway, so what's left here is shown-but-not-played."""
    shown = {v: c * 0.7 for v, c in (base.get("shown") or {}).items() if c * 0.7 >= 0.2}
    for s in base.get("shelves") or []:
        for t in (s.get("items") or [])[:8]:
            v = t.get("video_id")
            if v and t.get("kind", "song") == "song":
                shown[v] = shown.get(v, 0.0) + 1.0
    return shown


def _pop_spare(key: str) -> dict | None:
    """The next feed built ahead, taken off the queue. Call holding _spares_guard."""
    ready = _spares.get(key)
    return ready.pop(0) if ready else None


def _feed_lock(key: str) -> threading.Lock:
    with _feed_locks_guard:
        return _feed_locks.setdefault(key, threading.Lock())


def _spare_lock(key: str) -> threading.Lock:
    with _feed_locks_guard:
        return _spare_locks.setdefault(key, threading.Lock())
_streams: dict[str, tuple[str, float]] = {}
_streams_lock = threading.Lock()
_jobs: "queue.Queue[tuple[int | None, list[dict] | None]]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_health = {"youtube": None, "at": None, "detail": "not checked yet", "switch_requested": None}
_prefetch_q: "queue.Queue[str]" = queue.Queue()
_prefetching: set[str] = set()
_prefetch_lock = threading.Lock()
_prefetch_workers: list[threading.Thread] = []
PREFETCH_WORKERS = 2  # each lookup takes YouTube ~2-3 s; two at a time keeps it gentle
FEED_REBUILD_AFTER = 3  # new listening events since the last build before Genius rebuilds itself
_TUNING_EVENTS = ("play", "finish", "like", "unlike", "dislike", "skip")  # plain listening tunes it too


class NotFound(LookupError):
    """Mapped to HTTP 404. Plain KeyError/IndexError from upstream data stay 500s."""


class Conflict(Exception):
    """Mapped to HTTP 409."""


class BadRequest(TypeError, ValueError):
    """A request body of the wrong type. A ValueError too, so it maps to HTTP 400."""


def shared_ytm(gap: float = 0.25) -> YTM:
    global _ytm
    with _ytm_lock:
        if _ytm is None:
            _ytm = YTM(gap=gap)
        return _ytm


def shared_visitor() -> Visitor:
    """The owner's anonymous YouTube Music visitor (id kept in the data dir, mode 600)."""
    global _visitor
    if _visitor is None:
        pace = shared_ytm()._pace  # taken outside _visitor_lock: shared_ytm has its own lock
        with _visitor_lock:
            if _visitor is None:
                _visitor = Visitor(config.data_dir() / "visitor_id", pace)
    return _visitor


def _report_loop():
    while True:
        vid = _visitor_q.get()
        try:
            if not shared_visitor().report_play(vid):
                logger.info("YouTube Music gave no play report URL for %s", vid)
        except (requests.RequestException, YTMusicError, OSError, KeyError, ValueError, TypeError) as e:
            # one line, no traceback: bot checks on a VPN exit fail many reports in a row
            logger.warning("couldn't report a play to YouTube Music (%s: %s)", type(e).__name__, e)
        finally:
            _visitor_q.task_done()


def report_play(video_id: str) -> None:
    """Queue a play for the anonymous visitor; never blocks the event request."""
    global _visitor_worker
    with _report_worker_lock:
        if _visitor_worker is None or not _visitor_worker.is_alive():
            _visitor_worker = threading.Thread(target=_report_loop, name="tapedeck-visitor", daemon=True)
            _visitor_worker.start()
    _visitor_q.put(video_id)


def shared_outside() -> outside.Sources:
    global _outside
    with _ytm_lock:
        if _outside is None:
            _outside = outside.Sources()
        return _outside


SOURCE_WEIGHT = outside.SOURCE_WEIGHT
AGREE_BONUS = 0.5  # per extra service that nominated the same song


def resolve_outside(ytm, store, t: dict) -> dict | None:
    """A Spotify/SoundCloud song -> its YouTube Music audio track (search is songs-only, so never a video).
    Answers, misses included, are kept for a week: each song costs at most two searches a week."""
    k = f"spotify:{t['spotify_id']}" if t.get("spotify_id") else f"song:{_song_key(t)}"
    try:
        known, hit = store.xmatch_get(k)
        if known:
            return hit
        found = ytm.match(Track.from_dict(t))
        hit = found.to_dict() if found else None
        store.xmatch_put(k, hit)
        return hit
    except Exception:  # not cached: a network blip or a locked database shouldn't break autoplay or the feed
        logger.exception("couldn't look up %r on YouTube Music", t.get("title"))
        return None


def check_video_id(vid) -> str:
    if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid):
        raise ValueError(f"not a YouTube Music video id: {vid!r}")
    return vid


def clamp(n, lo: int, hi: int, default: int) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


# ---- YouTube reachability (a server behind a VPN: some exits can break streaming) ------------
def note_stream(ok: bool, detail: str = "") -> None:
    _health.update(youtube=ok, at=time.time(), detail=detail or ("ok" if ok else "refused"))
    if not ok:
        _request_vpn_switch()


def note_bot_check(video_id: str) -> None:
    """YouTube asked for a sign-in: the exit is flagged, so ask for another one now (at most every 15 minutes)."""
    logger.warning("YouTube bot check on %s; asking for another VPN exit", video_id)
    _health.update(youtube=False, at=time.time(), detail="YouTube bot check")
    _request_vpn_switch(every=BOT_CHECK_TRIGGER_EVERY)


def _request_vpn_switch(every: float = BLOCKED_TRIGGER_EVERY) -> None:
    """Run TAPEDECK_ON_STREAM_BLOCKED (e.g. kick the VPN auto-switch job), at most once per `every` seconds."""
    cmd = os.environ.get("TAPEDECK_ON_STREAM_BLOCKED")
    last = _health["switch_requested"]
    if not cmd or (last and time.time() - last < every):
        return
    _health["switch_requested"] = time.time()
    try:
        subprocess.Popen(shlex.split(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        logger.warning("YouTube refused audio; ran TAPEDECK_ON_STREAM_BLOCKED")
    except OSError:
        logger.exception("TAPEDECK_ON_STREAM_BLOCKED could not start")


def check_stream(video_id: str = CHECK_VIDEO) -> tuple[bool, str]:
    """Resolve a fresh audio URL and fetch its first bytes twice, the way playback does."""
    import requests
    import yt_dlp
    try:
        url = resolve_stream(video_id, fresh=True)
        codes = []
        for _ in range(2):
            r = requests.get(url, headers={"Range": "bytes=0-1023", "User-Agent": config.UA}, timeout=20,
                             proxies=config.proxies())
            codes.append(r.status_code)
        ok, detail = all(c == 206 for c in codes), f"audio fetch {codes}"
    except (requests.RequestException, yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as e:
        ok, detail = False, f"{type(e).__name__}: {str(e)[:160]}"
    _health.update(youtube=ok, at=time.time(), detail=detail)
    return ok, detail


def _cached_stream(video_id: str) -> bool:
    with _streams_lock:
        hit = _streams.get(video_id)
    return bool(hit and time.time() < hit[1])


def prefetch(video_ids) -> int:
    """Look audio URLs up in the background so the first play starts at once (~3 s cold vs ~0.2 s warm).
    Fire and forget: invalid ids, cached ids and ids already queued are skipped. Returns how many were queued."""
    queued = 0
    for vid in list(video_ids or [])[:10]:
        if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid) or _cached_stream(vid):
            continue
        with _prefetch_lock:
            if vid in _prefetching:
                continue
            _prefetching.add(vid)
        _prefetch_q.put(vid)
        queued += 1
    if queued:
        _ensure_prefetchers()
    return queued


def _prefetch_work() -> None:
    while True:
        vid = _prefetch_q.get()
        try:
            if not _cached_stream(vid):
                resolve_stream(vid)
        except stream.Blocked:
            note_bot_check(vid)  # a flagged exit shows up here first, before anyone presses play
        except Exception:
            logger.info("prefetch of %s failed; it will be looked up on play", vid, exc_info=True)
        finally:
            with _prefetch_lock:
                _prefetching.discard(vid)
            _prefetch_q.task_done()


def _ensure_prefetchers() -> None:
    with _prefetch_lock:
        _prefetch_workers[:] = [t for t in _prefetch_workers if t.is_alive()]
        while len(_prefetch_workers) < PREFETCH_WORKERS:
            t = threading.Thread(target=_prefetch_work, name="tapedeck-prefetch", daemon=True)
            t.start()
            _prefetch_workers.append(t)


def _prefetch_feed(shelves: list[dict]) -> None:
    """The songs you're most likely to tap: the first 6 of the first shelf, the first 2 of each other."""
    ids = []
    for i, s in enumerate(shelves):
        songs = [t["video_id"] for t in s.get("items") or [] if t.get("video_id")]
        ids += songs[: 6 if i == 0 else 2]
    for start in range(0, len(ids), 10):
        prefetch(ids[start:start + 10])


def resolve_stream(video_id: str, fresh: bool = False) -> str:
    with _streams_lock:
        hit = _streams.get(video_id)
    if hit and not fresh and time.time() < hit[1]:
        return hit[0]
    url = stream.resolve(video_id)
    exp = parse_qs(urlparse(url).query).get("expire", [None])[0]
    with _streams_lock:
        _streams[video_id] = (url, (float(exp) if exp else time.time() + 3 * 3600) - 600)
        if len(_streams) > 500:
            for k in sorted(_streams, key=lambda k: _streams[k][1])[:100]:
                _streams.pop(k, None)
    return url


# ---- downloads (shared with the CLI) ----------------------------------------------------------
def expand(items: list[dict], ytm: YTM, say=lambda msg: None) -> list[Track]:
    """{video_id} | {album_id} | {playlist_id} | {spotify: link} → tracks to download."""
    out: list[Track] = []
    for it in items:
        if it.get("video_id"):
            out.append(ytm.song(check_video_id(it["video_id"])))
        elif it.get("album_id"):
            out += ytm.album_tracks(it["album_id"])
        elif it.get("playlist_id"):
            out += ytm.playlist_tracks(it["playlist_id"])[1]
        elif it.get("spotify"):
            name, wanted, truncated = spotify.fetch(it["spotify"])
            say(f"Spotify: {name}, {len(wanted)} track(s)" + (" (first 100 only)" if truncated else ""))
            for w in wanted:
                hit = ytm.match(w)
                say(f"  {'✓ ' + hit.video_id if hit else '✗ no confident match'}  {w.title} — {w.artist}")
                if hit:
                    out.append(hit)
        else:
            raise ValueError(f"don't know how to download {it!r}")
    return out


def _scan(say) -> None:
    if not navidrome.configured():
        return
    try:
        say(f"navidrome: {navidrome.scan()}")
    except Exception:
        logger.exception("navidrome scan request failed")
        say("navidrome: scan request failed; its file watcher may still pick files up")


def run_job(tracks: list[Track], log: Log, ytm: YTM, job: int | None = None, say=lambda msg: None,
            actor: str = "owner", show_paths: bool = True, fetch=download) -> tuple[int | None, int, list]:
    """Download, tag and file each track. A job id is created lazily (only when a file is written) unless one
    is passed in. Files the library already had are never claimed, so undo can't delete them, but they count as
    done: a request that's already satisfied must look the same as a fresh download (no ownership tell)."""
    root = config.library()
    done, failed = 0, []
    if job and actor == "agent":
        log.ask(job, [t.video_id for t in tracks])

    def where(path, t):
        return str(path) if show_paths else f"{t.title} — {t.artist}"

    for i, t in enumerate(tracks):
        if (have := log.have(t.video_id)):
            done += 1
            say(f"= already have {where(have, t)}")
            if job:
                log.update_job(job, done=done)
            continue
        if i:
            config.pause()
        try:
            meta = ytm.meta(t)
            path, created = fetch(meta, t.video_id, root)
        except Exception as e:
            logger.exception("download failed for %s", t.video_id)
            failed.append((t, e))
            if "403" in str(e):
                note_stream(False, f"download refused: {str(e)[:120]}")
            say(f"✗ {t.title} — {t.artist}: {e}")
            if job:
                log.update_job(job, failed=len(failed), error=str(e)[:300])
            continue
        done += 1
        if not created:
            say(f"= already in library {where(path, t)}")
            if job:
                log.update_job(job, done=done)
            continue
        if job is None:
            job = log.new_job(label=f"{t.title} — {t.artist}", actor=actor)
            if actor == "agent":
                log.ask(job, [x.video_id for x in tracks])
        log.add(job, t.video_id, t.spotify_id, path, {"title": t.title, "artist": t.artist, "album": meta.get("album"),
                                                       "album_id": t.album_id, "cover": t.cover})
        log.update_job(job, done=done)
        say(f"✓ {where(path, t)}")
    if done:
        _scan(say)
    return job, done, failed


def undo_job(job: int, log: Log, say=lambda msg: None) -> int:
    j = log.job(job)
    if j and j.get("status") in ("queued", "running"):
        raise Conflict(f"job {job} is still {j['status']}; undo it when it has finished")
    files = log.job_files(job)
    if not files:
        raise NotFound(f"no files recorded for job {job}")
    removed = 0
    for f in files:
        if not f.exists():
            say(f"already gone {f}")
            continue
        f.unlink()
        removed += 1
        for d in (f.parent, f.parent.parent):  # drop album/artist folders left empty
            if d.exists() and not any(d.iterdir()):
                d.rmdir()
        say(f"removed {f}")
    log.forget_job(job)
    _scan(say)
    return removed


def reconcile_jobs() -> None:
    """At server start: jobs left queued/running by a previous process will never finish; say so."""
    log = Log()
    for j in log.jobs(None):
        if j.get("status") in ("queued", "running"):
            log.update_job(j["id"], status="failed", error="interrupted by a restart; start it again")


def _work() -> None:
    while True:
        job, items = _jobs.get()
        if job is None:  # sentinel (tests)
            _jobs.task_done()
            return
        log = Log()
        try:
            log.update_job(job, status="running")
            actor = (log.job(job) or {}).get("actor") or "owner"
            tracks = expand(items, shared_ytm())
            log.update_job(job, total=len(tracks))
            _, done, failed = run_job(tracks, log, shared_ytm(), job=job, actor=actor)
            log.update_job(job, status="failed" if failed and not done else "done", done=done, failed=len(failed))
        except (Exception, SystemExit) as e:  # SystemExit: config.library() when the library isn't set
            logger.exception("download job %s failed", job)
            log.update_job(job, status="failed", error=(str(e) or type(e).__name__)[:300])
        finally:
            _jobs.task_done()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="tapedeck-downloads", daemon=True)
            _worker.start()


# ---- the facade -----------------------------------------------------------------------------
class Deck:
    def __init__(self, scope: str = "owner", ytm: YTM | None = None, account: str = OWNER):
        """scope: "owner" (a person, full access to their own data) or "agent" (filtered, permission-gated).
        account: whose data. Agents always see the owner account's data, filtered."""
        self.scope = scope
        self.account = account if scope == "owner" else OWNER
        self.admin = scope == "owner" and self.account == OWNER
        # Feed cache key: the existing "owner"/"agent" keys stay the same for the owner account.
        self.key = scope if self.account == OWNER else f"{scope}:{self.account}"
        self.store = Store(account=self.account)
        self.log = Log()
        self.rules_list = self.store.rules()
        self.filter = Filter(scope, self.rules_list, exempt=self.log.asked() if scope == "agent" else frozenset())
        self.perms = self.store.permissions()
        self.ytm = ytm or shared_ytm()

    def _owner_only(self, what: str) -> None:
        if self.scope != "owner":
            raise PermissionError(f"{what} is owner-only")

    def _admin_only(self, what: str) -> None:
        if not self.admin:
            raise PermissionError(f"{what} belong to the owner account")

    def _allowed(self, perm: str, what: str) -> None:
        if self.scope != "owner" and not self.perms.get(perm):
            raise PermissionError(f"the owner has turned off agent access to {what}")

    def _catalog(self, tracks: list[Track] | list[dict]) -> list[dict]:
        items = [t.to_dict() if isinstance(t, Track) else t for t in tracks]
        return self.filter.catalog(items, self.log.owned(), self.store.liked())

    # catalog: public, same for everyone, never filtered (see privacy.py)
    def search(self, q: str, kind: str = "songs", limit: int = 20) -> list[dict] | dict[str, list[dict]]:
        if not isinstance(q, str) or not q.strip():
            raise ValueError("empty search")
        if kind == "all":  # one upstream call for the app's merged list
            found = {k: self._catalog(v[:clamp(limit, 1, 50, 20)]) for k, v in self.ytm.search_all(q).items()}
            if self.filter.owner:
                prefetch([t["video_id"] for t in found["songs"] if t.get("video_id")][:3])
            return found
        fn = {"songs": self.ytm.search, "albums": self.ytm.search_albums, "artists": self.ytm.search_artists}.get(kind)
        if not fn:
            raise ValueError("kind must be songs, albums, artists or all")
        results = self._catalog(fn(q, clamp(limit, 1, 50, 20)))
        if self.filter.owner:
            prefetch([t["video_id"] for t in results if t.get("video_id")][:3])
        return results

    def lyrics(self, video_id: str) -> dict:
        from . import lyrics
        found = lyrics.find(self.ytm, check_video_id(video_id))
        if not found:
            raise NotFound("no lyrics for this song")
        return found

    def album(self, album_id: str) -> dict:
        a = self.ytm.album(album_id)
        return {"album_id": album_id, "title": a.get("title"), "year": a.get("year"),
                "artists": [x["name"] for x in a.get("artists") or []], "cover": cover_url(a.get("thumbnails")),
                "tracks": self._catalog(self.ytm.album_tracks(album_id))}

    def artist(self, artist_id: str) -> dict:
        a = self.ytm.artist(artist_id)

        def cards(key):
            return [t.to_dict() for t in (_item(r) for r in (a.get(key) or {}).get("results") or []) if t]

        songs = [_track(r) for r in (a.get("songs") or {}).get("results") or [] if r.get("videoId")]
        related = [{"title": r.get("title") or "", "kind": "artist", "artist_id": r.get("browseId"),
                    "cover": cover_url(r.get("thumbnails")), "artists": []}
                   for r in (a.get("related") or {}).get("results") or [] if r.get("browseId")]
        return {"artist_id": artist_id, "name": a.get("name"), "cover": cover_url(a.get("thumbnails")),
                "songs": self._catalog(songs), "albums": cards("albums"), "singles": cards("singles"),
                "related": related}

    def playlist(self, playlist_id: str) -> dict:
        p, tracks = self.ytm.playlist_tracks(playlist_id)
        return {"playlist_id": playlist_id, "title": p.get("title"), "cover": cover_url(p.get("thumbnails")),
                "tracks": self._catalog(tracks)}

    def radio(self, video_id: str) -> list[dict]:
        return self._catalog(self.ytm.radio(check_video_id(video_id), 50))

    def track(self, video_id: str) -> dict:
        return self._catalog([self.ytm.song(check_video_id(video_id))])[0]

    # personal: filtered for agents, and only if the owner allows it
    def feed(self, refresh: bool = False, _wait: bool = False) -> dict:
        """Past its TTL, the feed you have is served at once and rebuilt in the background (a Genius build is
        ~10-15 s of paced YouTube Music calls). Only a first build, a privacy-rule change or Refresh waits."""
        self._allowed("feed", "your feed")
        refresh = refresh and self.filter.owner  # agents can't force expensive rebuilds
        sig = self._sig()
        base = _feeds.get(self.key)
        must = not base or base.get("sig") != sig
        expired = bool(base) and time.time() - base["generated"] > FEED_TTL
        if refresh or must or (expired and _wait):
            with _feed_lock(self.key):  # only builders wait here; readers of a cached feed never do
                base = _feeds.get(self.key)
                must = not base or base.get("sig") != sig
                expired = bool(base) and time.time() - base["generated"] > FEED_TTL
                if refresh or must or expired:
                    shown = _impressions(base) if base and not must else {}
                    spare = self._take_spare(sig) if (refresh or expired) and not must else None
                    new = spare or self._build(shown)
                    if base and not must and len(new["shelves"]) < len(base["shelves"]) // 2:
                        # YouTube Music failing mid-build (rate limit, outage) yields a near-empty feed: keep the
                        # good one and try again in 5 minutes instead of caching the thin one for 30.
                        logger.warning("Genius rebuild came back thin (%d shelves vs %d); keeping the old feed",
                                       len(new["shelves"]), len(base["shelves"]))
                        base["generated"] = time.time() - FEED_TTL + 300
                        return self._serve(base)
                    base = new
                    base["sig"] = sig  # a rule added anywhere (CLI too) invalidates the agent feed
                    if not spare:  # a spare keeps the count it was built from
                        base["events"] = self._tuning_count()
                    base["shown"] = shown
                    _feeds[self.key] = base
                    if self.filter.owner:
                        _prefetch_feed(base["shelves"])
                    if refresh or spare:
                        self._start_spare()
                expired = False
        if expired and not _feed_lock(self.key).locked():
            scope, account = self.scope, self.account

            def rebuild():
                try:
                    Deck(scope, account=account).feed(_wait=True)
                except Exception:
                    logger.exception("background Genius rebuild failed")
            threading.Thread(target=rebuild, name="tapedeck-feed-refresh", daemon=True).start()
        elif self.filter.owner and not refresh:
            self._keep_spare_ready()
        return self._serve(base)

    def _sig(self) -> str:
        return repr(sorted((r["kind"], r["key"]) for r in self.rules_list)) if not self.filter.owner else ""

    def _build(self, shown: dict) -> dict:
        exclude = None if self.filter.owner else self.filter.hidden
        return build(self.store, self.ytm, self.log.owned(), random.Random(), exclude=exclude,
                     shown=shown, outside=shared_outside().similar,
                     outside_artist=shared_outside().artist_similar,
                     resolve=lambda t: resolve_outside(self.ytm, self.store, t),
                     personal_home=(lambda: shared_visitor().home()) if self.admin else None)

    def _take_spare(self, sig: str) -> dict | None:
        """The feed built ahead, if it still fits: same privacy rules, under the TTL, and not from before a history
        clear. Songs disliked or skipped since it was built are taken out of it, so a verdict shows on this
        refresh without a wait. Waits only when none is ready and one is being built right now."""
        building = _spare_lock(self.key)
        with _spares_guard:
            spare = _pop_spare(self.key)
            # Part built already: waiting for it beats a build from scratch. Woken when it lands, not when the
            # builder's queue of rebuilds is done; the timeout re-checks so a missed wake-up can't hang a refresh.
            while spare is None and building.locked():
                _spares_guard.wait(1)
                spare = _pop_spare(self.key)
            gen = _spare_gen.get(self.key, 0)
        if not spare or spare.get("sig") != sig or time.time() - spare["generated"] > FEED_TTL:
            return None
        if spare.get("gen") != gen:  # built from listening history that was cleared since
            return None
        gone = self.store.disliked() | self.store.recent_skips()
        for s in spare["shelves"]:
            s["items"] = [t for t in s.get("items") or [] if t.get("video_id") not in gone]
        spare["shelves"] = [s for s in spare["shelves"] if s["items"]]
        return spare

    def _keep_spare_ready(self) -> None:
        """On an ordinary read: top up what is built ahead, or redo it if it will expire soon."""
        with _spares_guard:
            ready = list(_spares.get(self.key) or [])
        if _spare_lock(self.key).locked():
            return
        if ready and time.time() - ready[0]["generated"] > FEED_TTL - 300:
            self._start_spare(fresh=True)
        elif len(ready) < SPARE_DEPTH:
            self._start_spare()

    def _start_spare(self, promote: bool = False, fresh: bool = False) -> None:
        """Build the next feed in the background, from the impressions of the one just served. One build at a
        time: asked again while one runs, it builds once more after (the running one used older listening)."""
        scope, account, key = self.scope, self.account, self.key
        building = _spare_lock(key)
        if not building.acquire(blocking=False):  # taken here, not in the thread, so two reads can't both start
            with _spares_guard:
                p, f = _spare_again.get(key, (False, False))
                _spare_again[key] = (p or promote, f or fresh)
            return

        def run():
            try:
                Deck(scope, account=account)._build_spare(key, promote, fresh)
            except Exception:
                logger.exception("building the next Genius feed failed")
            finally:
                release()

        def release():
            building.release()
            with _spares_guard:
                _spares_guard.notify_all()  # a refresh waiting for this build stops waiting
                again = _spare_again.pop(key, None)  # asked for after the builder's last look (or it failed)
            if again is not None:
                Deck(scope, account=account)._start_spare(*again)
        try:
            threading.Thread(target=run, name="tapedeck-feed-spare", daemon=True).start()
        except RuntimeError:  # can't start a thread: never leave the lock held, or every refresh would hang
            logger.exception("could not start building the next Genius feed")
            building.release()

    def _spare_soon(self) -> None:
        """After new listening: once Genius would retune (FEED_REBUILD_AFTER new plays or verdicts), redo the feed
        built ahead after SPARE_QUIET seconds without another one, so a burst of skips costs one build."""
        base = _feeds.get(self.key)
        if not base:
            return
        with _spares_guard:
            ready = list(_spares.get(self.key) or [])
        count = self._tuning_count()
        if ready and count - ready[0].get("events", 0) < FEED_REBUILD_AFTER:
            return
        promote = count - base.get("events", 0) >= FEED_REBUILD_AFTER  # the next open shows it re-ranked too
        timer = threading.Timer(SPARE_QUIET, self._start_spare, kwargs={"promote": promote, "fresh": True})
        timer.daemon = True
        with _spares_guard:
            old = _spare_timers.pop(self.key, None)
            _spare_timers[self.key] = timer
        if old:
            old.cancel()
        timer.start()

    def _build_spare(self, key: str, promote: bool = False, fresh: bool = False) -> None:
        """Runs holding _spare_lock(key). With `fresh`, replace what is built ahead (from older listening), and
        with `promote` first build a new current feed too; then top up to SPARE_DEPTH. Each result is swapped in
        only when done, so whatever is ready stays usable the whole time."""
        while True:
            ok = self._build_ahead(key, promote, fresh)
            while ok:
                with _spares_guard:
                    if len(_spares.get(key) or []) >= SPARE_DEPTH:
                        break
                ok = self._build_ahead(key, False, False)
            with _spares_guard:
                if key not in _spare_again:
                    return
                promote, fresh = _spare_again.pop(key)

    def _build_ahead(self, key: str, promote: bool, fresh: bool) -> bool:
        base = _feeds.get(key)
        if not base:
            return False
        events, sig = self._tuning_count(), self._sig()
        with _spares_guard:
            gen = _spare_gen.get(key, 0)
        current = None
        if promote:
            current = self._build(_impressions(base))
            if len(current["shelves"]) < len(base["shelves"]) // 2:
                return False  # YouTube Music failed mid-build; keep what is there
            current.update(sig=sig, events=events, shown=_impressions(base))
        with _spares_guard:
            ready = list(_spares.get(key) or [])
        after = current or (ready[-1] if ready and not fresh else base)  # what this one is shown after
        shown = _impressions(after)
        spare = self._build(shown)
        if len(spare["shelves"]) < len(base["shelves"]) // 2:
            return False  # YouTube Music failed mid-build; the next refresh builds on the spot instead
        spare.update(sig=sig, events=events, gen=gen, shown=shown)
        now = _feeds.get(key)
        with _spares_guard:
            cleared = _spare_gen.get(key, 0) != gen
        if now is None or now.get("sig") != sig or cleared:
            return False  # history cleared or rules changed while building: built from the old state
        # Never wait for the feed lock here: a refresh holding it may be waiting for this build (_take_spare).
        feed_lock = _feed_lock(key)
        if current and now is base and feed_lock.acquire(blocking=False):
            try:
                with _spares_guard:
                    cleared = _spare_gen.get(key, 0) != gen
                if _feeds.get(key) is base and not cleared:
                    _feeds[key] = current
                    if self.filter.owner:
                        _prefetch_feed(current["shelves"])
            finally:
                feed_lock.release()
        with _spares_guard:
            if _spare_gen.get(key, 0) != gen:
                return False
            _spares[key] = [spare] if fresh else [*(_spares.get(key) or []), spare][-SPARE_DEPTH:]
            _spares_guard.notify_all()
        return True

    def _serve(self, base: dict) -> dict:
        shelves = self.filter.feed(base["shelves"])
        for s in shelves:
            s["items"] = self._catalog(s["items"])
        return {"generated": base["generated"], "cold_start": base["cold_start"], "shelves": shelves}

    def _library_rows(self) -> list[dict]:
        rows = [{"video_id": r["video_id"], "title": r["title"],
                 "artists": [a.strip() for a in (r["artist"] or "").split(",") if a.strip()],
                 "album": r["album"], "album_id": r.get("album_id"), "cover": r.get("cover"), "job": r["job"],
                 "path": r["path"], "created": r["created"]} for r in self.log.files(None)]
        return self.filter.tracks(rows)  # filter first, then limit: limits must not reveal hidden rows

    def library(self, n: int = 200) -> list[dict]:
        self._allowed("library", "your library")
        rows = self._library_rows()[:clamp(n, 1, 5000, 200)]
        if not self.filter.owner:
            for r in rows:
                r.pop("job", None)  # job ids line up with /api/jobs timestamps
        return rows

    # ---- streamed songs in Navidrome playlists (owner and people, never agents) --------------
    def _playlist_id(self, pid) -> str:
        self._owner_only("playlists")
        if not isinstance(pid, str) or not PLAYLIST_ID.fullmatch(pid):
            raise ValueError("bad playlist id")
        return pid

    def playlist_items(self, pid: str) -> list[dict]:
        items = self.store.playlist_items(self._playlist_id(pid))
        marked = self._catalog([i["track"] for i in items])  # owned/liked marks, like any song list
        return [{**i, "track": t} for i, t in zip(items, marked, strict=True)]

    def add_playlist_item(self, pid: str, track, slot) -> dict:
        pid = self._playlist_id(pid)
        if not isinstance(track, dict) or not isinstance(track.get("title"), str):
            raise ValueError("track needs a title and video_id")  # noqa: TRY004  (400 to the client)
        check_video_id(track.get("video_id"))
        if not isinstance(slot, (int, float)) or slot < 0:
            raise ValueError("slot must be a number >= 0")
        clean = Track.from_dict(track).to_dict()
        clean["kind"] = "song"
        return {"id": self.store.add_playlist_item(pid, clean, slot)}

    def remove_playlist_item(self, pid: str, item_id: int) -> bool:
        return self.store.remove_playlist_item(self._playlist_id(pid), item_id)

    def move_playlist_item(self, pid: str, item_id: int, slot) -> bool:
        pid = self._playlist_id(pid)
        if not isinstance(slot, (int, float)) or slot < 0:
            raise ValueError("slot must be a number >= 0")
        return self.store.move_playlist_item(pid, item_id, slot)

    def drop_playlist(self, pid: str) -> int:
        return self.store.drop_playlist(self._playlist_id(pid))

    def likes(self) -> list[dict]:
        self._allowed("likes", "your likes")
        scores = self.store.track_scores()
        liked = [scores[v]["track"] for v in self.store.liked() if v in scores]
        return self.filter.tracks(self._catalog(liked))

    def taste(self, n: int = 10) -> list[dict]:
        """Top artists from your own listening. Agents get names only, and private songs never count."""
        self._allowed("feed", "your top artists")
        n = clamp(n, 1, 50, 10)
        exclude = None if self.filter.owner else self.filter.hidden
        top = self.store.top_artists(self.log.owned(), n * 2, exclude=exclude)
        visible = [a for a in top if not self.filter.hidden({"kind": "artist", "title": a["name"],
                                                               "artist_id": a["artist_id"]})][:n]
        if self.filter.owner:
            return visible
        return [{"name": a["name"], "artist_id": a["artist_id"]} for a in visible]

    def history(self, n: int = 50) -> list[dict]:
        self._owner_only("listening history")
        return self.store.history(clamp(n, 1, 500, 50))

    def clear_history(self, entry_id: int | None = None) -> int:
        """Delete one history entry, or all listening history (likes stay). Genius forgets those plays too."""
        self._owner_only("listening history")
        removed = self.store.delete_history(entry_id)
        if entry_id is not None and not removed:
            raise NotFound("no such history entry")
        for cache_key in (self.key, "agent" if self.admin else None):  # rebuild from what's left
            if cache_key:
                with _spares_guard:
                    _spare_gen[cache_key] = _spare_gen.get(cache_key, 0) + 1
                    _spares.pop(cache_key, None)
                _feeds.pop(cache_key, None)
        return removed

    def autoplay(self, video_id: str, exclude=(), n: int = 10) -> list[dict]:
        """What to play after `video_id` when the queue runs out. YouTube Music's own Up Next for that song is the
        candidate pool (Spotify closed its recommendations API to new apps in Nov 2024). It is re-ranked with your
        Genius taste: songs you played recently, skipped or disliked, and anything already queued are dropped;
        artists you play rise; at most 2 songs per artist; and a quarter comes from the radio of one of your
        favourites so a long session doesn't drift away from you."""
        self._allowed("feed", "your recommendations")
        check_video_id(video_id)
        n = clamp(n, 1, 25, 10)
        exclude = {v for v in (exclude or []) if isinstance(v, str)}
        owned = self.log.owned()
        scores = self.store.track_scores(owned)
        if not self.filter.owner:
            scores = {v: e for v, e in scores.items() if not self.filter.hidden(e["track"])}
        personal = self.store.recent_skips() | self.store.disliked() | self.store.recently_played(50)
        if not self.filter.owner:  # a hidden song must not even leave a gap: only ban what the agent may see
            personal = {v for v in personal if v in scores}
        banned = personal | exclude | {video_id}
        affinity: dict[str, float] = {}
        for e in scores.values():
            for a in e["track"].get("artists") or []:
                affinity[key(a)] = affinity.get(key(a), 0.0) + e["score"]

        # The same songs as other uploads (a Spotify pick resolves to the album track, not the video you disliked).
        # Disliked songs aren't in `scores`, so read titles from history. Agents: only songs they may see, or the
        # presence of a ban would tell them what's in your history.
        recognisable = banned if self.filter.owner else banned & set(scores)
        banned_keys = {_song_key(t) for t in self.store.titles(recognisable)}

        def boost(t: dict) -> float:
            return 0.15 * max(min(affinity.get(key((t.get("artists") or [""])[0]), 0.0), 5.0), -5.0)

        def ranked(votes: dict[str, list[dict]]) -> list[dict]:
            """Every service's list votes by position; the same song from several services is one candidate
            (matched by title and lead artist) and gets a bonus per extra service. The YouTube Music copy is kept
            when there is one; otherwise the song is matched to its audio track only if it's actually picked."""
            cands: dict[str, dict] = {}
            for source, tracks in votes.items():
                w = SOURCE_WEIGHT.get(source, 0.5)
                for pos, d in enumerate(tracks):
                    k = _song_key(d)
                    if k.startswith("|") or k.endswith("|") or k in banned_keys or d.get("video_id") in banned:
                        continue
                    c = cands.setdefault(k, {"track": None, "outside": None, "votes": {}})
                    if d.get("video_id"):
                        c["track"] = c["track"] or d
                    else:
                        c["outside"] = c["outside"] or d
                    c["votes"][source] = max(c["votes"].get(source, 0.0), w * (1 - pos / (len(tracks) + 1)))
            for c in cands.values():
                c["score"] = (sum(c["votes"].values()) + AGREE_BONUS * (len(c["votes"]) - 1)
                              + boost(c["track"] or c["outside"]))
            return sorted(cands.values(), key=lambda c: -c["score"])

        top = sorted((e for e in scores.values() if e["score"] > 0 and e["track"].get("video_id") not in (video_id,)),
                     key=lambda e: -e["score"])
        try:
            seed = self.ytm.song(video_id).to_dict()
        except LookupError:
            seed = {}
        if seed.get("title"):  # its audio version (another id) and other services' copies mustn't come back
            banned_keys.add(_song_key(seed))
        with ThreadPoolExecutor(max_workers=3) as ex:  # the services answer in parallel
            yt = ex.submit(self.ytm.radio, video_id, 50)
            out = ex.submit(shared_outside().similar, seed) if seed else None
            fav = ex.submit(self.ytm.radio, top[0]["track"]["video_id"], 25) if top else None
            votes = {"youtube": [t.to_dict() for t in yt.result()]}
            try:
                votes.update(out.result() if out else {})
            except Exception:
                logger.exception("other services' recommendations failed; autoplay uses YouTube Music alone")
            anchor: list[dict] = []
            if fav:
                try:
                    anchor = ranked({"youtube": [t.to_dict() for t in fav.result()]})
                except Exception:
                    logger.exception("anchor radio failed; autoplay continues from the current song only")
        main = ranked(votes)
        logger.info("autoplay %s: %s", video_id, {s: len(v) for s, v in votes.items()})

        picks, per_artist, seen, resolves = [], {}, set(), 0
        streams = [iter(main), iter(anchor)]
        while len(picks) < n:
            src = streams[1] if anchor and len(picks) % 4 == 3 else streams[0]
            c = next(src, None) or next(streams[0], None) or next(streams[1], None)
            if c is None:
                break
            t = c["track"]
            if t is None:
                if resolves >= 2 * n:  # bound the first-time searches; cached matches cost nothing
                    continue
                resolves += 1
                t = resolve_outside(self.ytm, self.store, c["outside"])
                if not t or t.get("video_id") in banned or _song_key(t) in banned_keys:
                    continue
            lead = key((t.get("artists") or [""])[0])
            if t["video_id"] in seen or per_artist.get(lead, 0) >= 2:
                continue
            seen.add(t["video_id"])
            per_artist[lead] = per_artist.get(lead, 0) + 1
            picks.append(dict(t))
        if self.filter.owner:
            prefetch([t["video_id"] for t in picks[:2]])
        return self._catalog(picks)

    def event(self, track: dict, event: str) -> None:
        self._owner_only("recording plays")  # agents can't fake plays to steer your feed
        if not isinstance(track, dict):
            raise BadRequest("track must be an object")
        check_video_id(track.get("video_id"))
        if not track.get("title") or not track.get("artists"):  # privacy rules match on names: fill them in
            try:
                full = self.ytm.song(track["video_id"]).to_dict()
                track = {**full, **{k: v for k, v in track.items() if v}}
            except LookupError:
                logger.warning("no catalog details for %s; storing the event as sent", track["video_id"])
        self.store.add_event(track, event)
        # Teach the owner's anonymous YouTube Music visitor, never with a song the owner hides from agents.
        # Without artist names a privacy rule can't match, so such a play is not reported (fails closed).
        if (event == "play" and self.admin and track.get("artists")
                and not Filter("agent", self.store.rules()).hidden(track)):
            report_play(track["video_id"])
        if event in _TUNING_EVENTS:
            self._spare_soon()

    def _tuning_count(self) -> int:
        return self.store.count_events(_TUNING_EVENTS)

    def prefetch(self, video_ids) -> int:
        if not isinstance(video_ids, list):
            raise BadRequest("video_ids must be a list")
        return prefetch(video_ids)

    def permissions(self) -> dict:
        return dict(self.perms)  # agents may read their own permissions, so they don't fail blindly

    def set_permissions(self, changes: dict) -> dict:
        self._owner_only("agent permissions")
        if not self.admin:
            raise PermissionError("only the owner account changes agent permissions")
        if not isinstance(changes, dict):
            raise BadRequest("permissions must be an object")
        self.perms = self.store.set_permissions(changes)
        return dict(self.perms)

    def rules(self) -> list[dict]:
        self._owner_only("privacy rules")
        self._admin_only("privacy rules")  # they hide things from agents, who only ever see the owner account
        return self.store.rules()

    def add_rule(self, kind: str, key: str, ref: str | None = None, label: str | None = None) -> dict:
        self._owner_only("privacy rules")
        self._admin_only("privacy rules")  # they hide things from agents, who only ever see the owner account
        return self.store.add_rule(kind, key, ref, label)

    def remove_rule(self, rule_id: int) -> bool:
        self._owner_only("privacy rules")
        self._admin_only("privacy rules")  # they hide things from agents, who only ever see the owner account
        return self.store.remove_rule(rule_id)

    def _agent_sees_job(self, j: dict) -> bool:
        if j.get("actor") == "agent":
            return True  # it started it, it knows what's in it; hiding it would reveal a rule
        if j.get("status") != "done" or not j.get("done"):
            return False  # labels of owner jobs with no files yet could name a private song or album
        rows = self.log.job_rows(j["id"])
        return bool(rows) and not any(self.filter.hidden(r) for r in rows)

    def jobs(self, n: int = 20) -> list[dict]:
        self._allowed("jobs", "download activity")
        n = clamp(n, 1, 200, 20)
        if self.filter.owner:
            if not self.admin:  # the library is shared, but who asked for what is each person's own
                return [j for j in self.log.jobs(None) if j.get("actor") == self.account][:n]
            return self.log.jobs(n)
        out = []
        for j in self.log.jobs(None):
            if self._agent_sees_job(j):
                out.append({k: v for k, v in j.items() if k != "error"})
                if len(out) >= n:
                    break
        return out

    def enqueue(self, items, label: str | None = None) -> dict:
        self._allowed("download", "downloads")
        if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
            raise ValueError("items must be a non-empty list of objects")
        if label is not None and not isinstance(label, str):
            raise ValueError("label must be a string")
        for it in items:
            if it.get("video_id"):
                check_video_id(it["video_id"])
            elif not any(isinstance(it.get(k), str) and it.get(k) for k in ("album_id", "playlist_id", "spotify")):
                raise ValueError(f"don't know how to download {it!r}")
        job = self.log.new_job(label=label or _label(items), actor=self.actor, status="queued")
        if self.scope == "agent":
            self.log.ask(job, [it["video_id"] for it in items if it.get("video_id")])
        _jobs.put((job, items))
        _ensure_worker()
        return self.log.job(job)

    @property
    def actor(self) -> str:
        """Who a download job belongs to: "agent", "owner", or the person's account name."""
        return "agent" if self.scope == "agent" else self.account

    def undo(self, job: int) -> int:
        if self.scope == "owner" and not self.admin:
            j = self.log.job(job)
            if not j or j.get("actor") != self.account:
                raise PermissionError("you can only undo downloads you started")
        if self.scope != "owner":
            mode = self.perms.get("undo", "off")
            j = self.log.job(job)
            if mode == "off" or (mode == "own" and (not j or j.get("actor") != "agent")):
                raise PermissionError("the owner only lets agents undo downloads they started" if mode == "own"
                                      else "the owner has turned off undo for agents")
        return undo_job(job, self.log)

    def stream_source(self, video_id: str) -> tuple[str, str | Path]:
        """('file', path) for songs you own, else ('url', googlevideo url). Agents always get the proxied stream,
        so the response never reveals whether (or when) a song was downloaded."""
        check_video_id(video_id)
        if self.filter.owner and (p := self.log.have(video_id)):
            return "file", p
        return "url", resolve_stream(video_id)

    def status(self) -> dict:
        import yt_dlp

        from . import __version__
        s = {"version": __version__, "yt_dlp": yt_dlp.version.__version__, "scope": self.scope,
             "queue": _jobs.qsize(), "navidrome": navidrome.configured(),
             "youtube": {k: _health[k] for k in ("youtube", "at", "detail")},
             "feed_age": int(time.time() - _feeds[self.key]["generated"]) if self.key in _feeds else None,
             "account": self.account,
             "permissions": self.perms}
        if self.filter.owner:
            s.update(downloads=self.log.count(), private_rules=len(self.rules_list),
                     events=self.store.count_events(),
                     library=str(config.library()), vpn_switch_requested=_health["switch_requested"])
        else:
            s["downloads"] = len(self._library_rows()) if self.perms.get("library") else None
        return s


def _label(items: list[dict]) -> str:
    it = items[0]
    more = f" (+{len(items) - 1})" if len(items) > 1 else ""
    if isinstance(it.get("title"), str) and it["title"]:
        return f"{it['title']} — {it.get('artist') or ''}".strip(" —") + more
    for k, name in (("album_id", "album"), ("playlist_id", "playlist"), ("spotify", "Spotify link"), ("video_id", "song")):
        if it.get(k):
            return f"{name} {it[k]}" + more
    return "download"
