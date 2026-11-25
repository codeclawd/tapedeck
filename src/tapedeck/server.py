"""tapedeck's HTTP side: JSON API for client apps and the web UI, an audio/cover proxy, and MCP for agents.

People sign in with a username and password (`POST /api/login`, like Sonarr/Radarr) and get a session token.
Two bearer tokens from the env file also work: TAPEDECK_OWNER_TOKEN (you, scripts) and TAPEDECK_AGENT_TOKEN
(agents). /mcp always runs in agent scope, whichever token is used. Media players can't set headers, so
`?t=<token>` works too; access logging stays off so tokens never land in a log.
"""
import contextlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

import requests
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, QueryParams
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from . import __version__, config, service, spotify, stream
from .service import Conflict, Deck, NotFound
from .store import OWNER, Store

logger = logging.getLogger("tapedeck")

WEB = Path(__file__).parent / "web"
COVER_HOSTS = re.compile(r"^https://(lh3\.googleusercontent\.com|yt3\.googleusercontent\.com|yt3\.ggpht\.com|"
                         r"i\.ytimg\.com|i\d?\.ytimg\.com|image-cdn-[a-z0-9-]+\.spotifycdn\.com|i\.scdn\.co)/")
_covers: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_covers_lock = threading.Lock()
COVER_MAX_BYTES = 5 * 1024 * 1024


def tokens() -> dict[str, str]:
    t = {"owner": os.environ.get("TAPEDECK_OWNER_TOKEN", ""), "agent": os.environ.get("TAPEDECK_AGENT_TOKEN", "")}
    if len(t["owner"]) < 16 or len(t["agent"]) < 16 or t["owner"] == t["agent"]:
        raise SystemExit("Set TAPEDECK_OWNER_TOKEN and TAPEDECK_AGENT_TOKEN (different, 16+ chars): run `tapedeck token --init`.")
    return t


def who(headers: Headers, query: QueryParams, toks: dict[str, str]) -> tuple[str, str] | None:
    """(scope, account) for a request's token: the env owner/agent tokens, or a person's account token."""
    auth = headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else query.get("t", "")
    if not given:
        return None
    for scope in ("owner", "agent"):
        if hmac.compare_digest(given.encode(), toks[scope].encode()):
            return scope, OWNER
    name = Store().account_for_token(given)
    return ("owner", name) if name else None


OPEN_PATHS = ("/api/health", "/api/login")


class Auth:
    """Every /api/* (except /api/health) and /mcp request needs a token; /mcp is always agent scope."""

    def __init__(self, app, toks: dict[str, str]):
        self.app, self.toks = app, toks

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope["path"]
            if (path.startswith("/api/") and path not in OPEN_PATHS) or path.startswith("/mcp"):
                w = who(Headers(scope=scope), QueryParams(scope.get("query_string", b"")), self.toks)
                if not w:
                    await JSONResponse({"error": "missing or wrong token"}, 401,
                                       headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                    return
                state = scope.setdefault("state", {})
                state["scope"] = "agent" if path.startswith("/mcp") else w[0]
                state["account"] = OWNER if path.startswith("/mcp") else w[1]
        await self.app(scope, receive, send)


def deck(request: Request) -> Deck:
    return Deck(request.state.scope, account=request.state.account)


async def body(request: Request) -> dict:
    try:
        data = await request.json()
    except json.JSONDecodeError as e:
        raise ValueError("body must be JSON") from e
    if isinstance(data, dict):
        return data
    raise ValueError("body must be a JSON object")


def ok(data, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status)


# ---- catalog -------------------------------------------------------------------------------
def r_search(request):
    q = request.query_params
    return ok(deck(request).search(q.get("q", ""), q.get("kind", "songs"), min(int(q.get("limit", 20)), 50)))


def r_lyrics(request):
    return ok(deck(request).lyrics(request.path_params["id"]))


def r_album(request):
    return ok(deck(request).album(request.path_params["id"]))


def r_artist(request):
    return ok(deck(request).artist(request.path_params["id"]))


def r_playlist(request):
    return ok(deck(request).playlist(request.path_params["id"]))


def r_radio(request):
    return ok(deck(request).radio(request.path_params["id"]))


def r_track(request):
    return ok(deck(request).track(request.path_params["id"]))


def r_spotify(request):
    name, tracks, truncated = spotify.fetch(request.query_params.get("link", ""))
    return ok({"name": name, "truncated": truncated, "tracks": [t.to_dict() for t in tracks]})


# ---- personal ------------------------------------------------------------------------------
def r_feed(request):
    return ok(deck(request).feed(refresh=request.query_params.get("refresh") == "1"))


def r_library(request):
    return ok(deck(request).library(min(int(request.query_params.get("limit", 200)), 1000)))


async def r_playlist_items(request):
    pid = request.path_params["pid"]
    if request.method == "GET":
        return ok(await _thread(lambda: deck(request).playlist_items(pid)))
    data = await body(request)
    return ok(await _thread(lambda: deck(request).add_playlist_item(pid, data.get("track"), data.get("slot", 1e9))), 201)


async def r_playlist_item(request):
    """DELETE removes a streamed entry; PUT {slot} moves it (the app sends final positions after an edit)."""
    pid, item = request.path_params["pid"], int(request.path_params["item"])
    if request.method == "PUT":
        data = await body(request)
        found = await _thread(lambda: deck(request).move_playlist_item(pid, item, data.get("slot")))
    else:
        found = await _thread(lambda: deck(request).remove_playlist_item(pid, item))
    if not found:
        raise NotFound("no such playlist entry")
    return ok({"ok": True})


def r_playlist_forget(request):
    return ok({"removed": deck(request).drop_playlist(request.path_params["pid"])})


def r_likes(request):
    return ok(deck(request).likes())


def r_taste(request):
    return ok(deck(request).taste())


def r_history(request):
    if request.method == "DELETE":
        return ok({"removed": deck(request).clear_history()})
    return ok(deck(request).history(min(int(request.query_params.get("limit", 50)), 500)))


def r_history_entry(request):
    return ok({"removed": deck(request).clear_history(int(request.path_params["id"]))})


def r_autoplay(request):
    q = request.query_params
    exclude = [v for v in (q.get("exclude") or "").split(",") if v]
    return ok(deck(request).autoplay(request.path_params["id"], exclude[:200], q.get("limit", 10)))


async def r_event(request):
    data = await body(request)
    await _thread(lambda: deck(request).event(data.get("track") or {}, data.get("event", "")))
    return ok({"ok": True})


async def r_rules(request):
    if request.method == "GET":
        return ok(await _thread(lambda: deck(request).rules()))
    data = await body(request)
    return ok(await _thread(lambda: deck(request).add_rule(data.get("kind", ""), data.get("key", ""), data.get("ref"),
                                                          data.get("label"))), 201)


async def r_permissions(request):
    if request.method == "GET":
        return ok(await _thread(lambda: deck(request).permissions()))
    data = await body(request)
    return ok(await _thread(lambda: deck(request).set_permissions(data)))


async def r_prefetch(request):
    data = await body(request)
    return ok({"queued": await _thread(lambda: deck(request).prefetch(data.get("video_ids")))}, 202)


def r_check_stream(request):
    deck(request)._owner_only("the stream check")
    ok_, detail = service.check_stream()
    if not ok_:
        service.note_stream(False, detail)
    return ok({"youtube": ok_, "detail": detail})


def r_rule_delete(request):
    if not deck(request).remove_rule(int(request.path_params["id"])):
        raise NotFound("no such rule")
    return ok({"ok": True})


def r_jobs(request):
    return ok(deck(request).jobs(min(int(request.query_params.get("limit", 30)), 200)))


async def r_download(request):
    data = await body(request)
    return ok(await _thread(lambda: deck(request).enqueue(data.get("items"), data.get("label"))), 202)


def r_undo(request):
    return ok({"removed": deck(request).undo(int(request.path_params["id"]))})


def r_status(request):
    return ok(deck(request).status())


def r_whoami(request):
    return ok({"scope": request.state.scope, "account": request.state.account,
               "username": Store().login_for(request.state.account) if request.state.scope == "owner" else None,
               "permissions": deck(request).permissions()})


# Wrong passwords: 8 per client address per 10 minutes, then 429 until the window passes.
LOGIN_WINDOW, LOGIN_MAX_FAILS = 600.0, 8
_login_fails: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _recent_fails(ip: str, now: float) -> list[float]:
    fails = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_fails[ip] = fails
    return fails


async def r_login(request):
    """Username + password -> a session token for this device. 401 on a wrong pair, 429 when rate-limited."""
    data = await body(request)
    ip = request.client.host if request.client else "?"
    username, password = data.get("username"), data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return JSONResponse({"error": "username and password are required"}, 400)
    # Every attempt takes a slot BEFORE the password is checked, so parallel guesses can't all slip under the limit;
    # a correct password gives its slot back.
    with _login_lock:
        fails = _recent_fails(ip, time.time())
        if len(fails) >= LOGIN_MAX_FAILS:
            return JSONResponse({"error": "too many wrong passwords; try again in a few minutes"}, 429)
        slot = time.time()
        fails.append(slot)
    store = Store()
    name = await run_in_threadpool(store.check_login, username, password)  # scrypt: off the event loop
    if not name:
        return JSONResponse({"error": "wrong username or password"}, 401)
    with _login_lock, contextlib.suppress(ValueError):
        _login_fails.get(ip, []).remove(slot)
    return ok({"token": store.new_session(name), "account": name, "username": username.strip().lower()})


def r_logout(request):
    """Ends the session the request was made with (env tokens can't be ended this way)."""
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("t", "")
    return ok({"ended": Store().end_session(given)})


def r_health(request):
    return ok({"ok": True, "name": "tapedeck", "version": __version__})


# ---- media proxies ---------------------------------------------------------------------------
def r_stream(request):
    vid = request.path_params["id"]
    kind, src = deck(request).stream_source(vid)
    if kind == "file":
        r = FileResponse(src, media_type="audio/mp4", headers={"Cache-Control": "private, max-age=3600"},
                         stat_result=os.stat(src))  # stat now, so the headers below exist to be removed
        for h in ("last-modified", "etag"):  # the file's mtime is its download time; nothing needs it
            if h in r.headers:
                del r.headers[h]
        return r
    # YouTube throttles a request with no Range header to playback speed (2.7 MB in 300 s, measured on a home server)
    # but serves `bytes=0-` at full speed (4.2 MB in 4 s). So always ask for a range; a whole-file request from
    # the client still gets a plain 200 back.
    whole = not request.headers.get("range")
    headers = {"User-Agent": config.UA, "Range": request.headers.get("range") or "bytes=0-"}
    up = None
    for attempt in (0, 1):
        up = requests.get(src, headers=headers, stream=True, timeout=20, proxies=config.proxies())
        if up.status_code in (403, 410) and attempt == 0:  # expired or bound elsewhere: resolve once more
            up.close()
            src = service.resolve_stream(vid, fresh=True)
            continue
        break
    if up.status_code >= 400:
        up.close()
        service.note_stream(False, f"audio fetch answered {up.status_code}")
        return ok({"error": f"YouTube refused the audio ({up.status_code}); the server's network may be blocked"}, 502)
    service.note_stream(True)
    out = {k: up.headers[k] for k in ("Content-Length", "Content-Range") if k in up.headers}
    out["Accept-Ranges"] = "bytes"
    status = up.status_code
    if whole and status == 206:  # the client asked for the whole file: answer as such
        out.pop("Content-Range", None)
        status = 200

    def chunks():
        try:
            yield from up.iter_content(64 * 1024)
        finally:
            up.close()

    return StreamingResponse(chunks(), status_code=status, media_type="audio/mp4", headers=out)


def _fetch_cover(u: str):
    return requests.get(u, headers={"User-Agent": config.UA}, timeout=15, proxies=config.proxies(),
                        allow_redirects=False, stream=True)


def r_cover(request):
    """Fetches artwork on the server so the phone never talks to Google, and always hands out JPEG/PNG."""
    u = request.query_params.get("u", "")
    if not COVER_HOSTS.match(u):
        raise ValueError("cover host not allowed")
    size = max(60, min(int(request.query_params.get("s", 544)), 1200))
    u = re.sub(r"=w\d+-h\d+.*$", f"=w{size}-h{size}-l90-rj", u)
    u = u.replace("/vi_webp/", "/vi/").replace(".webp", ".jpg")
    if re.match(r"^https://i\d?\.ytimg\.com/vi/[^/?#]+/[^/?#.]+$", u):
        u += ".jpg"  # feed thumbnails sometimes come as ".../hq720" with no extension; YouTube 404s those
    with _covers_lock:
        hit = _covers.get(u)
        if hit:
            _covers.move_to_end(u)
    if hit:
        data, ctype = hit
    else:
        r = _fetch_cover(u)
        ctype = r.headers.get("content-type", "").split(";")[0]
        if r.status_code != 200 or ctype not in ("image/jpeg", "image/png"):
            r.close()
            return ok({"error": f"cover unavailable ({r.status_code} {ctype})"}, 502)
        data = b""
        for chunk in r.iter_content(64 * 1024):
            data += chunk
            if len(data) > COVER_MAX_BYTES:
                r.close()
                return ok({"error": "cover too large"}, 502)
        with _covers_lock:
            _covers[u] = (data, ctype)
            while len(_covers) > 400:
                _covers.popitem(last=False)
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=86400"})


# ---- errors ----------------------------------------------------------------------------------
async def _thread(fn):
    import anyio
    return await anyio.to_thread.run_sync(fn)


def _err(status):
    async def handler(request, exc):
        return JSONResponse({"error": str(exc) or type(exc).__name__}, status)
    return handler


def _upstream(request, exc):
    """Never a class name: people read this in an alert (owner saw "upstream: DownloadError" and read "download")."""
    logger.warning("upstream failure on %s: %s", request.url.path, str(exc)[:300])
    return JSONResponse({"error": "YouTube didn't answer for this song. Try again in a moment."}, 502)


def _blocked(request, exc):
    service.note_bot_check(request.path_params.get("id", "?"))
    return JSONResponse({"error": str(exc), "blocked": True}, 503, headers={"Retry-After": "60"})


def _internal(request, exc):
    logger.exception("internal error", exc_info=exc)
    return JSONResponse({"error": "internal error"}, 500)


def create_app(toks: dict[str, str] | None = None, prewarm: bool = True) -> Auth:
    from .agent import build_mcp

    toks = toks or tokens()
    mcp_app = build_mcp().streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0",
                                              stateless_http=True, json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        service.reconcile_jobs()
        if prewarm:  # build the owner's feed and get YouTube's visitor id in the background so the first play is fast
            threading.Thread(target=lambda: _quiet(lambda: Deck("owner").feed()), daemon=True).start()
            threading.Thread(target=lambda: _quiet(stream._visitor), daemon=True).start()
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    get = ["GET"]
    routes = [
        Route("/api/health", r_health, methods=get),
        Route("/api/login", r_login, methods=["POST"]),
        Route("/api/logout", r_logout, methods=["POST"]),
        Route("/api/whoami", r_whoami, methods=get),
        Route("/api/status", r_status, methods=get),
        Route("/api/search", r_search, methods=get),
        Route("/api/album/{id}", r_album, methods=get),
        Route("/api/lyrics/{id}", r_lyrics, methods=get),
        Route("/api/artist/{id}", r_artist, methods=get),
        Route("/api/playlist/{id}", r_playlist, methods=get),
        Route("/api/radio/{id}", r_radio, methods=get),
        Route("/api/track/{id}", r_track, methods=get),
        Route("/api/spotify", r_spotify, methods=get),
        Route("/api/feed", r_feed, methods=get),
        Route("/api/library", r_library, methods=get),
        Route("/api/likes", r_likes, methods=get),
        Route("/api/playlists/{pid}/items", r_playlist_items, methods=["GET", "POST"]),
        Route("/api/playlists/{pid}/items/{item:int}", r_playlist_item, methods=["PUT", "DELETE"]),
        Route("/api/playlists/{pid}", r_playlist_forget, methods=["DELETE"]),
        Route("/api/taste", r_taste, methods=get),
        Route("/api/history", r_history, methods=["GET", "DELETE"]),
        Route("/api/history/{id:int}", r_history_entry, methods=["DELETE"]),
        Route("/api/autoplay/{id}", r_autoplay, methods=get),
        Route("/api/events", r_event, methods=["POST"]),
        Route("/api/private", r_rules, methods=["GET", "POST"]),
        Route("/api/private/{id:int}", r_rule_delete, methods=["DELETE"]),
        Route("/api/permissions", r_permissions, methods=["GET", "PUT"]),
        Route("/api/check-stream", r_check_stream, methods=["POST"]),
        Route("/api/prefetch", r_prefetch, methods=["POST"]),
        Route("/api/jobs", r_jobs, methods=get),
        Route("/api/downloads", r_download, methods=["POST"]),
        Route("/api/jobs/{id:int}/undo", r_undo, methods=["POST"]),
        Route("/api/stream/{id}", r_stream, methods=get),
        Route("/api/cover", r_cover, methods=get),
        *mcp_app.routes,
    ]
    import yt_dlp
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={
        PermissionError: _err(403), LookupError: _err(404), NotFound: _err(404), Conflict: _err(409),
        ValueError: _err(400), KeyError: _internal, IndexError: _internal, stream.Blocked: _blocked,
        requests.RequestException: _upstream, yt_dlp.utils.DownloadError: _upstream,
        yt_dlp.utils.ExtractorError: _upstream})
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return Auth(app, toks)


def _quiet(fn):
    try:
        fn()
    except Exception:
        logger.exception("feed prewarm failed")


def serve(host: str = "0.0.0.0", port: int = 4540) -> None:
    import uvicorn
    config.library()  # fail at start, not inside the download worker, if TAPEDECK_LIBRARY is unset
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=host, port=port, access_log=False, log_level="warning")
