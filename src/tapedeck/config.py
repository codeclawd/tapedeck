"""Settings come from environment variables only; nothing here identifies the owner."""
import os
import random
import shutil
import time
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"


def proxy() -> str | None:
    return os.environ.get("TAPEDECK_PROXY") or None


def proxies() -> dict | None:
    p = proxy()
    return {"http": p, "https": p} if p else None


def data_dir() -> Path:
    d = Path(os.environ.get("TAPEDECK_DATA", Path.home() / ".local/share/tapedeck")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def library() -> Path:
    lib = os.environ.get("TAPEDECK_LIBRARY")
    if not lib:
        raise SystemExit("Set TAPEDECK_LIBRARY to the music folder Navidrome reads (e.g. ~/Desktop/music).")
    return Path(lib).expanduser().resolve()


def pause() -> None:
    """Sleep between downloads so YouTube doesn't start answering 403. TAPEDECK_SLEEP='3-8' (seconds)."""
    lo, _, hi = os.environ.get("TAPEDECK_SLEEP", "3-8").partition("-")
    lo_f, hi_f = float(lo), float(hi or lo)
    if hi_f > 0:
        time.sleep(random.uniform(lo_f, hi_f))


def load_env_file(path: Path | None = None) -> None:
    """KEY=VALUE lines from ~/.config/tapedeck/env; real environment variables win."""
    path = path or Path.home() / ".config/tapedeck/env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            key = key.strip().removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip("'\""))


def js_runtimes() -> dict:
    """yt-dlp needs a JS runtime for YouTube. It enables only deno by default; fall back to node 22+."""
    if shutil.which("deno"):
        return {"deno": {}}
    node = shutil.which("node")
    return {"node": {"path": node}} if node else {"deno": {}}


def ytdl_opts(**extra) -> dict:
    return {"quiet": True, "no_warnings": True, "cachedir": False, "proxy": proxy(),
            "js_runtimes": js_runtimes(), **extra}
