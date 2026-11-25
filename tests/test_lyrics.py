"""Lyrics for client apps (owner request 2026-11-03).

Timed lyrics win from any source; then plain. Only title and artist leave the server (to LRCLIB).
A source that errors is skipped and nothing is cached, so the next open retries.
"""
import pytest
from starlette.testclient import TestClient

from tapedeck import lyrics
from tapedeck.models import Track
from tapedeck.server import create_app

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"
SONG = Track(title="Paper Lanterns", artists=["The Example Band"], duration=214, video_id="ExmpleVid01")
LRC = ("[ar: The Example Band]\n[00:18.50] Lanterns drift over the river\n[00:22.80][01:30.00] Paper boats along the shore\n[00:27.00]\n"
       "[00:31.00] Light the way back home\n")


def test_parse_lrc_handles_tags_repeats_and_gaps():
    lines = lyrics.parse_lrc(LRC)
    assert [(ln["t"], ln["text"]) for ln in lines] == [
        (18.5, "Lanterns drift over the river"), (22.8, "Paper boats along the shore"), (27.0, ""), (31.0, "Light the way back home"),
        (90.0, "Paper boats along the shore")]


class FakeYTM:
    def __init__(self, plain=None, timed=None, error=None):
        self.plain, self.timed, self.error, self.calls = plain, timed, error, 0

    def song(self, vid):
        return SONG

    def lyrics(self, vid):
        self.calls += 1
        if self.error:
            raise self.error
        if self.timed:
            return {"synced": True, "source": "YouTube Music", "lines": self.timed}
        if self.plain:
            return {"synced": False, "source": "YouTube Music", "lines": lyrics.plain(self.plain)}
        return None


@pytest.fixture(autouse=True)
def fresh_cache():
    lyrics._cache.clear()
    yield
    lyrics._cache.clear()


def lrclib_returning(monkeypatch, *, synced=None, plain=None, status=200, seen=None, results=None):
    def fake(params):
        if seen is not None:
            seen.append(params)
        if status != 200:
            raise lyrics.SourceDown(f"LRCLIB {status}")
        if results is not None:
            return results
        return [{"duration": 214, "syncedLyrics": synced, "plainLyrics": plain}] if (synced or plain) else []
    monkeypatch.setattr(lyrics, "_lrclib_search", fake)


def test_synced_lrclib_beats_plain_youtube(monkeypatch):
    seen = []
    lrclib_returning(monkeypatch, synced=LRC, seen=seen)
    got = lyrics.find(FakeYTM(plain="Lanterns drift over the river"), "ExmpleVid01")
    assert got["synced"] is True and got["source"] == "LRCLIB"
    assert got["lines"][0] == {"t": 18.5, "text": "Lanterns drift over the river"}
    assert seen[0] == {"track_name": SONG.title, "artist_name": "The Example Band"}  # nothing else leaves


def test_plain_youtube_when_lrclib_has_nothing(monkeypatch):
    lrclib_returning(monkeypatch, results=[])
    got = lyrics.find(FakeYTM(plain="line one\nline two"), "ExmpleVid01")
    assert got == {"synced": False, "source": "YouTube Music",
                   "lines": [{"t": None, "text": "line one"}, {"t": None, "text": "line two"}]}


def test_junk_uploads_lose_to_real_lyrics_near_the_songs_length(monkeypatch):
    # live LRCLIB 2026-11-03: /api/get for this song returned a one-line "[00:00.00]probe" upload
    real_far = "\n".join(f"[00:{i:02d}.00] far {i}" for i in range(10))
    real_near = "\n".join(f"[00:{i:02d}.00] near {i}" for i in range(10))
    lrclib_returning(monkeypatch, results=[
        {"duration": 214, "syncedLyrics": "[00:00.00]probe"},
        {"duration": 179, "syncedLyrics": real_far},
        {"duration": 212, "syncedLyrics": real_near},
    ])
    got = lyrics.find(FakeYTM(), "ExmpleVid01")
    assert got["lines"][0]["text"] == "near 0"


def test_a_second_looser_search_runs_when_the_first_has_no_timings(monkeypatch):
    # live 2026-11-03: the same LRCLIB search returned no synced rows, then synced rows a minute later
    seen = []
    good = "\n".join(f"[00:{i:02d}.00] line {i}" for i in range(10))

    def fake(params):
        seen.append(params)
        return [] if "track_name" in params else [{"duration": 213, "syncedLyrics": good}]
    monkeypatch.setattr(lyrics, "_lrclib_search", fake)
    got = lyrics.find(FakeYTM(plain="a\nb\nc\nd"), "ExmpleVid01")
    assert got["synced"] is True
    assert seen[1] == {"q": "The Example Band Paper Lanterns"}


def test_plain_results_are_kept_an_hour_not_a_day(monkeypatch):
    lrclib_returning(monkeypatch, results=[])
    y = FakeYTM(plain="a\nb\nc\nd")
    lyrics.find(y, "ExmpleVid01")
    when, _ = lyrics._cache["ExmpleVid01"]
    monkeypatch.setattr(lyrics.time, "time", lambda: when + 2 * 3600)
    lyrics.find(y, "ExmpleVid01")
    assert y.calls == 2


def test_timed_youtube_is_used_without_asking_lrclib(monkeypatch):
    seen = []
    lrclib_returning(monkeypatch, synced=LRC, seen=seen)
    got = lyrics.find(FakeYTM(timed=[{"t": 1.0, "text": "a"}]), "ExmpleVid01")
    assert got["source"] == "YouTube Music" and got["synced"] is True and seen == []


def test_none_is_cached_but_a_down_source_is_not(monkeypatch):
    lrclib_returning(monkeypatch, status=503)
    y = FakeYTM()
    assert lyrics.find(y, "ExmpleVid01") is None
    assert lyrics.find(y, "ExmpleVid01") is None
    assert y.calls == 2  # LRCLIB was down, so the miss wasn't remembered
    lrclib_returning(monkeypatch, results=[])
    lyrics.find(y, "ExmpleVid01")
    lyrics.find(y, "ExmpleVid01")
    assert y.calls == 3  # a real "no lyrics" is remembered


def test_route_returns_lyrics_or_404(deck_env, monkeypatch):
    calls = {"n": 0}

    def fake_find(ytm, vid):
        calls["n"] += 1
        return {"synced": True, "source": "LRCLIB", "lines": [{"t": 1.0, "text": "hi"}]} if vid == "ExmpleVid01" else None
    monkeypatch.setattr(lyrics, "find", fake_find)
    with TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)) as c:
        r = c.get("/api/lyrics/ExmpleVid01", headers={"Authorization": f"Bearer {OWNER}"})
        assert r.status_code == 200 and r.json()["lines"][0]["text"] == "hi"
        assert c.get("/api/lyrics/aaaaaaaaaaa", headers={"Authorization": f"Bearer {OWNER}"}).status_code == 404
        assert c.get("/api/lyrics/bad", headers={"Authorization": f"Bearer {OWNER}"}).status_code == 400
        assert c.get("/api/lyrics/ExmpleVid01").status_code == 401


# ---- review fixes (2026-11-03) -----------------------------------------------------------------
def test_a_plain_result_is_not_cached_when_the_other_source_was_down(monkeypatch):
    lrclib_returning(monkeypatch, status=503)
    y = FakeYTM(plain="a\nb\nc\nd")
    assert lyrics.find(y, "ExmpleVid01")["source"] == "YouTube Music"
    lyrics.find(y, "ExmpleVid01")
    assert y.calls == 2  # LRCLIB might have had timings; ask again


def test_a_failed_second_search_keeps_what_the_first_found(monkeypatch):
    words = "\n".join(f"word {i}" for i in range(6))

    def fake(params):
        if "q" in params:
            raise lyrics.SourceDown("LRCLIB 503")
        return [{"duration": 214, "plainLyrics": words}]
    monkeypatch.setattr(lyrics, "_lrclib_search", fake)
    got = lyrics.find(FakeYTM(), "ExmpleVid01")
    assert got and got["synced"] is False and got["lines"][0]["text"] == "word 0"
    assert "ExmpleVid01" not in lyrics._cache  # partly down: not remembered
