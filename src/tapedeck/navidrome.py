"""Optional: ask Navidrome to rescan. Its file watcher normally picks new files up within ~5 s."""
import hashlib
import os
import secrets

import requests


def configured() -> bool:
    return all(os.environ.get(k) for k in ("NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD"))


def call(endpoint: str, **params) -> dict:
    salt = secrets.token_hex(6)
    token = hashlib.md5((os.environ["NAVIDROME_PASSWORD"] + salt).encode()).hexdigest()
    q = {"u": os.environ["NAVIDROME_USER"], "t": token, "s": salt, "v": "1.16.1", "c": "tapedeck", "f": "json", **params}
    r = requests.get(f"{os.environ['NAVIDROME_URL'].rstrip('/')}/rest/{endpoint}", params=q, timeout=20)
    r.raise_for_status()
    body = r.json()["subsonic-response"]
    if body.get("status") != "ok":
        raise RuntimeError(body.get("error", {}).get("message", "Navidrome error"))
    return body


def scan() -> str:
    """startScan is admin-only in Navidrome; returns a short status line."""
    status = call("startScan").get("scanStatus", {})
    return f"scan started (songs: {status.get('count', '?')})"
