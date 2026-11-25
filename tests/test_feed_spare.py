"""Owner 2026-11-20: pulling to refresh Music sat for 7-15 s while Genius rebuilt, then the phone showed
"Couldn't load your Genius feed: cancelled". 2026-11-21, after the first fix: "it reloads, but it's very slow".
A refresh now swaps in a feed kept built ahead, even after new plays or verdicts, and never waits for the build
of its replacement."""
import threading
import time

from starlette.testclient import TestClient

from tapedeck import service
from tapedeck.server import create_app

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"
H = {"Authorization": f"Bearer {OWNER}"}


def _idle():
    for _ in range(500):
        if not service._spare_lock("owner").locked():
            return
        time.sleep(0.01)
    raise AssertionError("the feed built ahead never finished")


def _ready_spare():
    _idle()
    with service._spares_guard:
        spare = (service._spares.get("owner") or [None])[0]
    assert spare, "no feed was built ahead"
    return spare


def _ids(feed):
    return {t.get("video_id") for s in feed["shelves"] for t in s["items"]}


def _app():
    return TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False))


def test_opening_the_feed_builds_the_next_one_ahead(deck_env):
    with _app() as c:
        c.get("/api/feed", headers=H)
        spare = _ready_spare()
        got = c.get("/api/feed?refresh=1", headers=H).json()
        assert got["generated"] == spare["generated"]  # the first pull is instant too
        assert service._feeds["owner"] is spare
        _ready_spare()  # and the one after it is built ahead again


def test_new_plays_dont_make_a_refresh_wait(deck_env, monkeypatch):
    with _app() as c:
        c.get("/api/feed", headers=H)
        spare = _ready_spare()
        monkeypatch.setattr(service, "SPARE_QUIET", 60)  # the replacement isn't due yet
        t = {"video_id": "playedSong1", "title": "Played", "artists": ["Someone"]}
        for _ in range(service.FEED_REBUILD_AFTER):
            c.post("/api/events", headers=H, json={"track": t, "event": "play"})
        got = c.get("/api/feed?refresh=1", headers=H).json()
        assert got["generated"] == spare["generated"]
        _idle()


def test_a_song_disliked_since_is_taken_out_of_the_feed_built_ahead(deck_env):
    with _app() as c:
        c.get("/api/feed", headers=H)
        spare = _ready_spare()
        hated = next(t for s in spare["shelves"] for t in s["items"] if t.get("video_id"))
        c.post("/api/events", headers=H, json={"track": hated, "event": "dislike"})
        got = c.get("/api/feed?refresh=1", headers=H).json()
        assert got["generated"] == spare["generated"]  # no rebuild for one verdict
        assert hated["video_id"] not in _ids(got)
        _idle()


def test_a_refresh_doesnt_wait_while_the_next_one_is_being_built(deck_env, monkeypatch):
    with _app() as c:
        c.get("/api/feed", headers=H)
        spare = _ready_spare()
        gate = threading.Event()
        real = service.build

        def slow(*a, **kw):
            gate.wait(5)
            return real(*a, **kw)
        monkeypatch.setattr(service, "build", slow)
        service.Deck("owner")._start_spare()  # its replacement is being built right now
        t0 = time.monotonic()
        got = c.get("/api/feed?refresh=1", headers=H).json()
        waited = time.monotonic() - t0
        gate.set()
        assert got["generated"] == spare["generated"] and waited < 1
        _idle()


def test_a_refresh_waiting_on_a_build_that_promotes_doesnt_deadlock(deck_env, monkeypatch):
    with _app() as c:
        c.get("/api/feed", headers=H)
        _idle()
        with service._spares_guard:
            service._spares.clear()  # nothing ready: the refresh will wait for the build
        gate = threading.Event()
        real = service.build

        def slow(*a, **kw):
            gate.wait(5)
            return real(*a, **kw)
        monkeypatch.setattr(service, "build", slow)
        service.Deck("owner")._start_spare(promote=True)
        done = {}
        pull = threading.Thread(target=lambda: done.update(r=service.Deck("owner").feed(refresh=True)))
        pull.start()
        time.sleep(0.2)  # the refresh now holds the feed lock and waits for the build
        gate.set()
        pull.join(10)
        assert not pull.is_alive() and done["r"]["shelves"]
        _idle()


def test_a_spare_built_while_history_was_cleared_is_dropped(deck_env, monkeypatch):
    with _app() as c:
        c.get("/api/feed", headers=H)
        _idle()
        with service._spares_guard:
            service._spares.clear()
        real = service.build

        def clearing(*a, **kw):
            service._feeds.pop("owner", None)  # clear_history lands mid-build
            return real(*a, **kw)
        monkeypatch.setattr(service, "build", clearing)
        service.Deck("owner")._build_spare("owner")
        assert not service._spares.get("owner")


def test_a_waiting_refresh_takes_the_spare_as_soon_as_it_lands(deck_env, monkeypatch):
    """Review 2026-11-21: it waited for the builder's whole queue (a rebuild asked for mid-build), not one build."""
    with _app() as c:
        c.get("/api/feed", headers=H)
        _idle()
        with service._spares_guard:
            service._spares.clear()
        second = threading.Event()
        real, calls = service.build, []

        def slow_after_first(*a, **kw):
            calls.append(1)
            if len(calls) > 1:
                second.wait(5)  # the queued rebuild hangs; the first spare is already stored
            return real(*a, **kw)
        monkeypatch.setattr(service, "build", slow_after_first)
        gate = threading.Event()
        monkeypatch.setattr(service.Deck, "_build_ahead", _gated(service.Deck._build_ahead, gate))
        d = service.Deck("owner")
        d._start_spare()
        d._start_spare()  # asked again mid-build: the builder will run once more
        gate.set()
        t0 = time.monotonic()
        got = d.feed(refresh=True)
        waited = time.monotonic() - t0
        second.set()
        assert got["shelves"] and waited < 2
        _idle()


def _gated(fn, gate):
    def run(self, key, promote):
        gate.wait(5)
        return fn(self, key, promote)
    return run


def test_a_rebuild_asked_for_during_a_failed_build_still_happens(deck_env, monkeypatch):
    with _app() as c:
        c.get("/api/feed", headers=H)
        _idle()
        with service._spares_guard:
            service._spares.clear()
        gate, real, calls = threading.Event(), service.build, []

        def fail_first(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(5)
                raise ValueError("YouTube Music hiccup")
            return real(*a, **kw)
        monkeypatch.setattr(service, "build", fail_first)
        d = service.Deck("owner")
        d._start_spare()
        d._start_spare()  # asked for while the first one is about to fail
        gate.set()
        time.sleep(0.05)
        _ready_spare()


def test_a_spare_from_before_a_history_clear_is_never_served(deck_env, monkeypatch):
    """Review 2026-11-21: the feed was rebuilt after the clear before the old build finished, so it looked current."""
    with _app() as c:
        c.get("/api/feed", headers=H)
        _idle()
        with service._spares_guard:
            service._spares.clear()
        gate, real, calls = threading.Event(), service.build, []

        def paused_first(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                gate.wait(5)  # mid-build, from the listening that is about to be cleared
            return real(*a, **kw)
        monkeypatch.setattr(service, "build", paused_first)
        service.Deck("owner")._start_spare()
        time.sleep(0.05)
        service.Deck("owner").clear_history()
        c.get("/api/feed", headers=H)  # the feed is rebuilt from what is left before the old build finishes
        gate.set()
        _idle()
        with service._spares_guard:
            assert not service._spares.get("owner")


def test_two_pulls_in_a_row_are_both_instant(deck_env, monkeypatch):
    """Owner 2026-11-21 pulls two or three times; with one feed built ahead the second pull waited ~8 s."""
    with _app() as c:
        c.get("/api/feed", headers=H)
        _idle()
        with service._spares_guard:
            ready = list(service._spares["owner"])
        assert len(ready) == service.SPARE_DEPTH
        gate, real = threading.Event(), service.build

        def slow(*a, **kw):
            gate.wait(5)
            return real(*a, **kw)
        monkeypatch.setattr(service, "build", slow)
        t0 = time.monotonic()
        first = c.get("/api/feed?refresh=1", headers=H).json()
        second = c.get("/api/feed?refresh=1", headers=H).json()
        waited = time.monotonic() - t0
        gate.set()
        assert waited < 1
        assert [first["generated"], second["generated"]] == [f["generated"] for f in ready]
        _idle()


def test_each_feed_built_ahead_follows_the_one_before_it(deck_env):
    with _app() as c:
        c.get("/api/feed", headers=H)
        _idle()
        with service._spares_guard:
            one, two = service._spares["owner"]
        assert set(service._impressions(one)) <= set(two["shown"])  # what the first shows counts as seen by the second
