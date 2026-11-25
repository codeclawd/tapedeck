"""Download a YouTube Music track, tag it, and file it where Navidrome looks."""
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import requests
import yt_dlp
from mutagen.mp4 import MP4, MP4Cover

from . import config

FORMAT = "140/bestaudio[ext=m4a]"  # AAC m4a: plays natively on iOS, no re-encode


def safe(name: str | None) -> str:
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", name or "").strip().strip(".").strip()
    s = s.encode()[:180].decode(errors="ignore").strip()  # APFS caps names at 255 bytes; leave room for "NN " + ".m4a"
    return s or "Unknown"


def target_path(root: Path, album_artist: str, album: str, track: int | None, title: str) -> Path:
    stem = f"{track:02d} {safe(title)}" if track else safe(title)
    return Path(root) / safe(album_artist) / safe(album) / f"{stem}.m4a"


def tag(path: Path, meta: dict, cover: bytes | None = None) -> None:
    m = MP4(path)
    fields = {"\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album", "aART": "album_artist",
              "\xa9day": "year", "\xa9cmt": "comment"}
    for atom, key in fields.items():
        if meta.get(key):
            m[atom] = [str(meta[key])]
    if meta.get("track"):
        m["trkn"] = [(int(meta["track"]), int(meta.get("total") or 0))]
    if cover and cover[:3] == b"\xff\xd8\xff":
        m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    elif cover and cover[:4] == b"\x89PNG":
        m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_PNG)]  # anything else (e.g. WebP) is skipped
    m.save()


def fetch_audio(video_id: str, workdir: Path) -> tuple[Path, dict]:
    opts = config.ytdl_opts(format=FORMAT, outtmpl=str(workdir / "%(id)s.%(ext)s"), noprogress=True, retries=10)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://music.youtube.com/watch?v={video_id}", download=True)
    return Path(info["requested_downloads"][0]["filepath"]), info


def fetch_cover(url: str | None) -> bytes | None:
    if not url:
        return None
    url = re.sub(r"=w\d+-h\d+", "=w1200-h1200", url)  # YTM art is resizable; ask for a larger square
    try:
        r = requests.get(url, headers={"User-Agent": config.UA}, proxies=config.proxies(), timeout=20)
        return r.content if r.ok else None
    except requests.RequestException:
        return None


class Log:
    """SQLite record of every file written, so `undo` removes exactly what a job added."""

    FILE_COLS: ClassVar[dict[str, str]] = {"title": "TEXT", "artist": "TEXT", "album": "TEXT", "album_id": "TEXT", "cover": "TEXT"}
    JOB_COLS: ClassVar[dict[str, str]] = {"status": "TEXT", "label": "TEXT", "actor": "TEXT", "total": "INTEGER", "done": "INTEGER",
                "failed": "INTEGER", "error": "TEXT", "finished": "TEXT"}

    def __init__(self, path: Path | None = None):
        from .store import connect
        self.db = connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS files (job INTEGER, video_id TEXT, spotify_id TEXT,
                           path TEXT UNIQUE, created TEXT)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT)")
        for table, cols in (("files", self.FILE_COLS), ("jobs", self.JOB_COLS)):  # phase-1 DBs lack these
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols.items():
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        # phase-1 jobs: give them a status and real counts so the UI can show and undo them
        self.db.execute("""UPDATE jobs SET status='done',
                           done=(SELECT COUNT(*) FROM files WHERE files.job=jobs.id),
                           total=(SELECT COUNT(*) FROM files WHERE files.job=jobs.id), failed=0
                           WHERE status IS NULL""")
        # songs an agent asked for itself: never hidden from agents (hiding them would reveal a privacy rule)
        self.db.execute("CREATE TABLE IF NOT EXISTS asked (job INTEGER, video_id TEXT, UNIQUE(job, video_id))")
        self.db.commit()

    def ask(self, job: int, video_ids: list[str]) -> None:
        self.db.executemany("INSERT OR IGNORE INTO asked VALUES (?,?)", [(job, v) for v in video_ids if v])
        self.db.commit()

    def asked(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT video_id FROM asked")}

    def job(self, job: int) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        return dict(row) if row else None

    def job_rows(self, job: int) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT video_id, title, artist, album, album_id FROM files WHERE job=?", (job,))]

    def new_job(self, label: str | None = None, actor: str = "owner", status: str = "running") -> int:
        """Monotonic ids (AUTOINCREMENT never reuses), so `undo N` always means one job."""
        cur = self.db.execute("INSERT INTO jobs (created, label, actor, status, total, done, failed) VALUES (?,?,?,?,0,0,0)",
                              (datetime.now(UTC).isoformat(), label, actor, status))
        self.db.commit()
        return cur.lastrowid

    def update_job(self, job: int, **fields) -> None:
        bad = set(fields) - set(self.JOB_COLS)
        if bad:
            raise ValueError(f"unknown job fields {bad}")
        if fields.get("status") in ("done", "failed"):
            fields["finished"] = datetime.now(UTC).isoformat()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job))
        self.db.commit()

    def jobs(self, n: int | None = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (-1 if n is None else n,))]

    def owned(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT video_id FROM files WHERE video_id IS NOT NULL")}

    def files(self, n: int | None = 200) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM files ORDER BY created DESC LIMIT ?",
                                                 (-1 if n is None else n,))]
        for r in rows:  # phase-1 rows have only a path: Artist/Album/NN Title.m4a
            p = Path(r["path"])
            r["title"] = r.get("title") or re.sub(r"^\d{2} ", "", p.stem)
            r["artist"] = r.get("artist") or p.parent.parent.name
            r["album"] = r.get("album") or p.parent.name
        return rows

    def have(self, video_id: str) -> Path | None:
        row = self.db.execute("SELECT path FROM files WHERE video_id=?", (video_id,)).fetchone()
        return Path(row[0]) if row and Path(row[0]).exists() else None

    def add(self, job: int, video_id: str, spotify_id: str | None, path: Path, meta: dict | None = None) -> None:
        m = meta or {}
        self.db.execute("""INSERT OR REPLACE INTO files (job, video_id, spotify_id, path, created, title, artist, album,
                           album_id, cover) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (job, video_id, spotify_id, str(path), datetime.now(UTC).isoformat(), m.get("title"),
                         m.get("artist"), m.get("album"), m.get("album_id"), m.get("cover")))
        self.db.commit()

    def job_files(self, job: int) -> list[Path]:
        return [Path(r[0]) for r in self.db.execute("SELECT path FROM files WHERE job=?", (job,))]

    def forget_job(self, job: int) -> None:
        self.db.execute("DELETE FROM files WHERE job=?", (job,))
        self.db.execute("UPDATE jobs SET status='undone' WHERE id=?", (job,))
        self.db.commit()

    def recent(self, n: int = 10) -> list[tuple]:
        return self.db.execute("SELECT job, path, created FROM files ORDER BY created DESC LIMIT ?", (n,)).fetchall()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]


def download(meta: dict, video_id: str, root: Path) -> tuple[Path, bool]:
    """meta: title, artist, album, album_artist, track, total, year, cover_url, comment.
    Returns (path, created). created=False means the library already had that file; it is left untouched."""
    with tempfile.TemporaryDirectory(prefix="tapedeck-") as tmp:
        src, info = fetch_audio(video_id, Path(tmp))
        meta = {**meta}
        meta["album"] = meta.get("album") or info.get("album") or "Singles"
        meta["artist"] = meta.get("artist") or info.get("artist") or info.get("uploader")
        meta["album_artist"] = meta.get("album_artist") or meta["artist"]
        meta["year"] = meta.get("year") or info.get("release_year")
        tag(src, meta, fetch_cover(meta.get("cover_url") or info.get("thumbnail")))
        dest = target_path(root, meta["album_artist"], meta["album"], meta.get("track"), meta["title"])
        if dest.exists():
            return dest, False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dest)
        return dest, True
