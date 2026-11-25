"""tapedeck: ad-free, account-free music from YouTube Music and Spotify's catalog, filed into Navidrome."""
import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path

from . import config, service, spotify, stream
from .library import Log
from .models import Track
from .store import Store
from .ytm import YTM

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
YTM_WATCH = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})")


def _last_path():
    return config.data_dir() / "last.json"


def save_last(tracks: list[Track]) -> None:
    _last_path().write_text(json.dumps([t.to_dict() for t in tracks]))


def load_last() -> list[Track]:
    p = _last_path()
    return [Track.from_dict(d) for d in json.loads(p.read_text())] if p.exists() else []


def scope() -> str:
    """Agents running the CLI (Claude Code sets CLAUDECODE, others AI_AGENT) get the agent view.
    ssh doesn't forward these, so remote agents should use MCP; TAPEDECK_SCOPE=agent forces it."""
    if os.environ.get("TAPEDECK_SCOPE") == "agent" or os.environ.get("CLAUDECODE") or os.environ.get("AI_AGENT"):
        return "agent"
    return "owner"


def fmt_dur(s: int | None) -> str:
    return f"{s // 60}:{s % 60:02d}" if s else "  -  "


def show(tracks: list[Track]) -> None:
    for i, t in enumerate(tracks, 1):
        if t.kind == "album":
            print(f"{i:>3}. [album] {t.title} — {t.artist}")
        else:
            print(f"{i:>3}. {t.title} — {t.artist}  ({t.album or 'single'}, {fmt_dur(t.duration)})")


def from_spotify(link: str, ytm: YTM) -> list[Track]:
    name, wanted, truncated = spotify.fetch(link)
    print(f"Spotify: {name} — {len(wanted)} track(s)", file=sys.stderr)
    if truncated:
        print("  note: Spotify's public page lists only the first 100 tracks of a playlist", file=sys.stderr)
    found = []
    for w in wanted:
        hit = ytm.match(w)
        mark = f"✓ {hit.video_id}" if hit else "✗ no confident match"
        print(f"  {mark}  {w.title} — {w.artist}", file=sys.stderr)
        if hit:
            found.append(hit)
    print(f"  matched {len(found)}/{len(wanted)}", file=sys.stderr)
    return found


def resolve(refs: list[str], ytm: YTM) -> list[Track]:
    """Numbers (from the last search), YTM video ids/URLs, Spotify links, album results → playable tracks."""
    last, out = None, []
    for ref in refs:
        if ref.isascii() and ref.isdigit():
            last = last if last is not None else load_last()
            if not 1 <= int(ref) <= len(last):
                raise SystemExit(f"No result #{ref}; run `tapedeck search` first.")
            t = last[int(ref) - 1]
            out += ytm.album_tracks(t.album_id) if t.kind == "album" else [t]
        elif spotify.parse_link(ref):
            out += from_spotify(ref, ytm)
        elif VIDEO_ID.match(ref) or YTM_WATCH.search(ref):
            vid = ref if VIDEO_ID.match(ref) else YTM_WATCH.search(ref).group(1)
            out.append(ytm.song(vid))
        else:
            raise SystemExit(f"Don't know what {ref!r} is: use a result number, a YouTube Music id/link or a Spotify link.")
    return out


def cmd_search(a) -> None:
    ytm = YTM()
    if a.spotify:
        tracks = from_spotify(a.spotify, ytm)
    elif a.albums:
        tracks = ytm.search_albums(" ".join(a.query), a.n)
    else:
        tracks = ytm.search(" ".join(a.query), a.n)
    save_last(tracks)
    show(tracks)


def cmd_play(a) -> None:
    ytm = YTM()
    for t in resolve(a.ref, ytm):
        url = stream.resolve(t.video_id)
        if a.url:
            print(url)
            continue
        print(f"▶ {t.title} — {t.artist}", file=sys.stderr)
        if stream.play(url, f"{t.title} — {t.artist}") != 0:
            break


def cmd_get(a) -> None:
    ytm, log = YTM(), Log()
    tracks = resolve(a.ref, ytm)
    say = lambda m: print(m, file=sys.stderr if m.startswith(("✗", "navidrome")) else sys.stdout)
    job, done, failed = service.run_job(tracks, log, ytm, say=say, actor=scope(), show_paths=scope() == "owner")
    if job:
        log.update_job(job, status="failed" if failed and not done else "done", total=len(tracks), failed=len(failed))
    print(f"job {job or '-'}: {done} downloaded, {len(failed)} failed", file=sys.stderr)
    if failed:
        sys.exit(1)


def cmd_status(a) -> None:
    deck = service.Deck(scope())
    rows = deck.library(a.n)
    print(f"{deck.log.count() if deck.filter.owner else len(rows)} file(s) downloaded")
    for r in rows:
        where = r["path"] if deck.filter.owner else f"{r['title']} — {', '.join(r['artists'])}"
        print(f"  job {r['job']}  {(r.get('created') or '')[:19]}  {where}")


def cmd_undo(a) -> None:
    if scope() != "owner":
        raise SystemExit("undo is owner-only")
    try:
        service.undo_job(a.job, Log(), say=print)
    except LookupError as e:
        raise SystemExit(str(e)) from e


def cmd_check_stream(a) -> None:
    """Exit 0 if YouTube audio plays through this machine's network right now (used by the VPN auto-switch)."""
    ok, detail = service.check_stream()
    print(f"{'ok' if ok else 'BLOCKED'}: {detail}")
    sys.exit(0 if ok else 1)


def cmd_serve(a) -> None:
    from .server import serve
    print(f"tapedeck {a.host}:{a.port}", file=sys.stderr)
    serve(a.host, a.port)


def _env_path() -> Path:
    return Path.home() / ".config/tapedeck/env"


def cmd_token(a) -> None:
    """--init writes two random tokens into the env file (never printed). --show prints one, for wiring an app."""
    path = _env_path()
    if a.init:
        if scope() != "owner":
            raise SystemExit("token setup is owner-only")
        text = path.read_text() if path.exists() else ""
        added = []
        for name in ("TAPEDECK_OWNER_TOKEN", "TAPEDECK_AGENT_TOKEN"):
            if not re.search(rf"^{name}=\S", text, re.MULTILINE):
                text = text.rstrip("\n") + f"\n{name}={secrets.token_urlsafe(24)}\n"
                added.append(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.lstrip("\n"))
        path.chmod(0o600)
        print(f"{', '.join(added) or 'tokens already set'} in {path}")
    elif a.show:
        if a.show == "owner" and scope() != "owner":
            raise SystemExit("the owner token is owner-only")
        val = os.environ.get(f"TAPEDECK_{a.show.upper()}_TOKEN")
        if not val:
            raise SystemExit("no token yet: run `tapedeck token --init`")
        print(val)


def cmd_hide(a) -> None:
    deck = service.Deck(scope())
    ref = " ".join(a.ref)
    if a.kind == "artist":
        rule = deck.add_rule("artist", ref, label=ref)
    elif a.kind == "song":
        t = resolve([ref], deck.ytm)[0] if not service.VIDEO_ID.match(ref) else deck.ytm.song(ref)
        rule = deck.add_rule("song", t.video_id, label=f"{t.title} — {t.artist}")
    else:
        t = load_last()[int(ref) - 1] if ref.isdigit() else None
        album_id = t.album_id if t else ref
        title = (t.album or t.title) if t else deck.ytm.album(album_id).get("title")
        from .privacy import key
        rule = deck.add_rule("album", album_id, ref=key(title), label=title)
    print(f"hidden from agents: #{rule['id']} {rule['kind']} {rule['label'] or rule['key']}")


def cmd_unhide(a) -> None:
    if not service.Deck(scope()).remove_rule(a.id):
        raise SystemExit(f"no rule #{a.id}")
    print(f"rule #{a.id} removed")


def cmd_private(a) -> None:
    for r in service.Deck(scope()).rules():
        print(f"  #{r['id']}  {r['kind']:<6} {r['label'] or r['key']}")


def _read_password(a) -> str:
    """--password-stdin reads one line (for scripts); otherwise ask twice without echo."""
    if a.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    import getpass
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("passwords don't match")
    return pw


def cmd_account(a) -> None:
    """People with their own feed. Each signs in with a username and password, like Sonarr/Radarr."""
    if scope() != "owner":
        raise SystemExit("accounts are owner-only")
    store = Store()
    if a.action == "add":
        if not a.name:
            raise SystemExit("usage: tapedeck account add NAME")
        pw = _read_password(a)
        store.add_account(a.name)
        try:
            store.set_login(a.name, a.username or a.name, pw)
        except (LookupError, ValueError):
            store.remove_account(a.name)  # no half-made account without a way to sign in
            raise
        print(f"{a.name} added. Sign in with username {store.login_for(a.name)!r} and that password.")
    elif a.action == "password":
        if not a.name:
            raise SystemExit("usage: tapedeck account password NAME [--username U]   (NAME can be owner)")
        user = a.username or store.login_for(a.name) or a.name
        store.set_login(a.name, user, _read_password(a))
        print(f"{a.name} signs in as {user!r}; their other devices were signed out")
    elif a.action == "remove":
        if not a.name or not store.remove_account(a.name):
            raise SystemExit(f"no account {a.name!r}")
        print(f"{a.name} removed; their login and devices no longer work")
    else:
        print(f"  owner  (login: {store.login_for('owner') or 'none yet: tapedeck account password owner'})")
        for r in store.accounts():
            print(f"  {r['name']}  (login: {r['username'] or 'none'})")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tapedeck", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search YouTube Music (or list a Spotify link's tracks)")
    s.add_argument("query", nargs="*")
    s.add_argument("--albums", action="store_true", help="search albums instead of songs")
    s.add_argument("--spotify", metavar="LINK", help="Spotify playlist/album/track link to match")
    s.add_argument("-n", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    pl = sub.add_parser("play", help="play ad-free (result numbers, YTM ids/links, Spotify links)")
    pl.add_argument("ref", nargs="+")
    pl.add_argument("--url", action="store_true", help="print the audio URL instead of playing")
    pl.set_defaults(fn=cmd_play)

    g = sub.add_parser("get", aliases=["import"], help="download into the Navidrome library")
    g.add_argument("ref", nargs="+")
    g.add_argument("--scan", action="store_true", help="ask Navidrome to rescan afterwards")
    g.set_defaults(fn=cmd_get)

    st = sub.add_parser("status", help="what has been downloaded")
    st.add_argument("-n", type=int, default=10)
    st.set_defaults(fn=cmd_status)

    u = sub.add_parser("undo", help="delete every file a download job added")
    u.add_argument("job", type=int)
    u.set_defaults(fn=cmd_undo)

    sv = sub.add_parser("serve", help="run the HTTP API, web UI and MCP endpoint")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=int(os.environ.get("TAPEDECK_PORT", "4540")))
    sv.set_defaults(fn=cmd_serve)

    cs = sub.add_parser("check-stream", help="exit 0 if YouTube audio is reachable from this network")
    cs.set_defaults(fn=cmd_check_stream)

    tk = sub.add_parser("token", help="create (--init) or print (--show owner|agent) the API tokens")
    tk.add_argument("--init", action="store_true")
    tk.add_argument("--show", choices=["owner", "agent"])
    tk.set_defaults(fn=cmd_token)

    h = sub.add_parser("hide", help="hide a song, artist or album from agents (owner only)")
    h.add_argument("kind", choices=["song", "artist", "album"])
    h.add_argument("ref", nargs="+", help="artist name, or a result number / YTM id")
    h.set_defaults(fn=cmd_hide)

    uh = sub.add_parser("unhide", help="remove a privacy rule (owner only)")
    uh.add_argument("id", type=int)
    uh.set_defaults(fn=cmd_unhide)

    pv = sub.add_parser("private", help="list privacy rules (owner only)")
    pv.set_defaults(fn=cmd_private)

    ac = sub.add_parser("account", help="people with their own feed: add NAME | password NAME | list | remove NAME")
    ac.add_argument("action", choices=["add", "password", "list", "remove"])
    ac.add_argument("name", nargs="?")
    ac.add_argument("--username", help="sign-in name (default: the account name)")
    ac.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    ac.set_defaults(fn=cmd_account)

    config.load_env_file()
    a = p.parse_args(argv)
    if a.cmd == "search" and not (a.query or a.spotify):
        p.error("search needs a query or --spotify LINK")
    try:
        a.fn(a)
    except (LookupError, PermissionError, ValueError) as e:
        raise SystemExit(str(e)) from e
