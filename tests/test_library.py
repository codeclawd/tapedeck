import shutil
import subprocess

import pytest
from mutagen.mp4 import MP4

from tapedeck.library import safe, tag, target_path


def test_safe_strips_path_characters():
    assert safe("AC/DC: Live?") == "AC_DC_ Live_"
    assert safe("..hidden. ") == "hidden"
    assert safe("") == "Unknown"


def test_target_path_layout(tmp_path):
    p = target_path(tmp_path, "Tame Impala", "Deadbeat", 3, "Loser")
    assert p == tmp_path / "Tame Impala" / "Deadbeat" / "03 Loser.m4a"
    assert target_path(tmp_path, "A", "B", None, "C") == tmp_path / "A" / "B" / "C.m4a"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_tag_round_trip(tmp_path):
    f = tmp_path / "x.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-t", "1", "-c:a", "aac", str(f)], check=True)
    tag(f, {"title": "Loser", "artist": "Tame Impala", "album": "Deadbeat", "album_artist": "Tame Impala",
            "track": 3, "total": 12, "year": "2025", "comment": "ytm:MX7FSAnRPug"}, cover=b"\xff\xd8\xff\xe0fakejpeg")
    m = MP4(f)
    assert m["\xa9nam"] == ["Loser"] and m["aART"] == ["Tame Impala"] and m["trkn"] == [(3, 12)]
    assert m["\xa9day"] == ["2025"] and m["\xa9cmt"] == ["ytm:MX7FSAnRPug"] and len(m["covr"]) == 1


def test_existing_library_file_is_never_claimed(tmp_path, monkeypatch):
    """A file already in the library must not be logged as ours, or `undo` would delete the owner's copy."""
    from tapedeck import library

    existing = target_path(tmp_path, "A", "B", 1, "Song")
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"owner's file")

    def fake_fetch(video_id, workdir):
        f = workdir / "x.m4a"
        f.write_bytes(b"new")
        return f, {}

    monkeypatch.setattr(library, "fetch_audio", fake_fetch)
    monkeypatch.setattr(library, "tag", lambda *a, **k: None)
    monkeypatch.setattr(library, "fetch_cover", lambda url: None)
    path, created = library.download({"title": "Song", "artist": "A", "album": "B", "track": 1}, "vid", tmp_path)
    assert path == existing and created is False
    assert existing.read_bytes() == b"owner's file"


def test_job_ids_never_reused_after_undo(tmp_path):
    from tapedeck.library import Log

    log = Log(tmp_path / "t.db")
    first = log.new_job()
    log.add(first, "v1", None, tmp_path / "a.m4a")
    log.forget_job(first)
    assert log.new_job() > first


def test_safe_caps_bytes_not_characters():
    assert len(safe("曲" * 100).encode()) <= 180


def test_webp_cover_is_not_embedded(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return
    f = tmp_path / "x.m4a"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1", "-c:a", "aac", str(f)], check=True)
    tag(f, {"title": "t"}, cover=b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    assert "covr" not in MP4(f)
