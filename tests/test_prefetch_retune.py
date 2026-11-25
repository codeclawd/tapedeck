"""Owner's phone test 2026-11-03: playback lagged ~3 s on first play, and Genius should tune itself."""
import time

import pytest
from starlette.testclient import TestClient

from tapedeck import service
from tapedeck.server import create_app

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def resolved(deck_env, monkeypatch):
    seen = []

    def fake_resolve(vid, fresh=False):
        seen.append(vid)
        with service._streams_lock:
            service._streams[vid] = (f"https://upstream.invalid/{vid}", time.time() + 3600)
        return f"https://upstream.invalid/{vid}"
    service._prefetch_q.join()  # earlier tests may have queued ids (e.g. a feed build); let them drain first
    with service._prefetch_lock:
        service._prefetching.clear()
    monkeypatch.setattr(service, "resolve_stream", fake_resolve)
    service._streams.clear()
    yield seen
    service._prefetch_q.join()
    service._streams.clear()


def _settle():
    service._prefetch_q.join()


def test_prefetch_looks_up_each_valid_uncached_id_once(resolved):
    assert service.prefetch(["aaaaaaaaaaa", "bbbbbbbbbbb", "aaaaaaaaaaa", "bad id", 5]) == 2
    _settle()
    assert sorted(resolved) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert service.prefetch(["aaaaaaaaaaa"]) == 0  # cached now: nothing to do


def test_prefetch_route_and_search_warm_the_cache(resolved):
    with TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)) as c:
        r = c.post("/api/prefetch", headers=H(OWNER), json={"video_ids": ["ccccccccccc"]})
        assert r.status_code == 202 and r.json() == {"queued": 1}
        assert c.post("/api/prefetch", headers=H(OWNER), json={"video_ids": "x"}).status_code == 400
        c.get("/api/search?q=hello", headers=H(OWNER))  # FakeYTM returns searchAAAAA, searchBBBBB
        _settle()
        assert {"ccccccccccc", "searchAAAAA", "searchBBBBB"} <= set(resolved)


def test_genius_rebuilds_itself_after_three_new_listens(resolved):
    with TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)) as c:
        first = c.get("/api/feed", headers=H(OWNER)).json()["generated"]
        t = {"video_id": "likedSong01", "title": "Liked", "artists": ["Fav"], "artist_id": "UCfav"}
        for ev in ("play", "play"):
            c.post("/api/events", headers=H(OWNER), json={"track": t, "event": ev})
        assert service._feeds["owner"]["generated"] == first  # 2 new events: not yet
        c.post("/api/events", headers=H(OWNER), json={"track": t, "event": "play"})  # owner just listens: plays count
        for _ in range(100):
            if service._feeds["owner"]["generated"] != first:
                break
            time.sleep(0.02)
        feed = c.get("/api/feed", headers=H(OWNER)).json()
        assert feed["generated"] != first and feed["cold_start"] is False  # re-ranked from the new plays


def test_whole_file_requests_ask_youtube_for_a_range_and_answer_200(deck_env, monkeypatch):
    """Owner 2026-11-03: 'Download to iPhone' crawled at playback speed. YouTube throttles requests without a
    Range header (measured: 2.7 MB in 300 s) but serves `bytes=0-` at full speed (4.2 MB in 4 s)."""
    monkeypatch.setattr(service, "resolve_stream", lambda vid, fresh=False: "https://upstream.invalid/x")
    sent = []

    class Up:
        def __init__(self):
            self.status_code = 206
            self.headers = {"Content-Length": "5", "Content-Range": "bytes 0-4/5"}

        def iter_content(self, n):
            yield b"abcde"

        def close(self):
            pass

    def fake_get(url, headers=None, **kw):
        sent.append(dict(headers or {}))
        return Up()
    monkeypatch.setattr("tapedeck.server.requests.get", fake_get)
    with TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)) as c:
        r = c.get("/api/stream/aaaaaaaaaaa", headers=H(OWNER))
        assert sent[-1].get("Range") == "bytes=0-"
        assert r.status_code == 200 and r.content == b"abcde" and "content-range" not in r.headers
        r = c.get("/api/stream/aaaaaaaaaaa", headers={**H(OWNER), "Range": "bytes=2-"})
        assert sent[-1]["Range"] == "bytes=2-" and r.status_code == 206
