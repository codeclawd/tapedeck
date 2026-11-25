"""Genius v2 (2026-11-05): no song twice, song versions not videos, artist mixes, new releases, albums."""
import random

from tapedeck.genius import build
from tapedeck.store import Store

A = {"video_id": "aaaaaaaaaa1", "title": "A song", "artists": ["Alpha"], "artist_id": "UCalpha", "album": "A album"}
B = {"video_id": "bbbbbbbbbb1", "title": "B song", "artists": ["Beta"], "artist_id": "UCbeta", "album": "B album"}
VIDEO = {"video_id": "vvvvvvvvvv1", "title": "Moonlight (Official Video)", "artists": ["Vee"], "artist_id": "UCvee"}


def _feed(tmp_path, fake, *seeds):
    s = Store(tmp_path / "g.db")
    for t in seeds:
        s.add_event(t, "finish")
        s.add_event(t, "like")
    return build(s, fake, set(), random.Random(3))


def _songs(shelf):
    return [t["video_id"] for t in shelf["items"] if t.get("kind", "song") == "song" and t.get("video_id")]


def test_no_song_appears_in_two_shelves(tmp_path, deck_env):
    feed = _feed(tmp_path, deck_env, A, B)
    seen = {}
    for sh in feed["shelves"]:
        if sh["id"] == "on-repeat":  # your own plays, by design
            continue
        for v in _songs(sh):
            assert v not in seen, f"{v} in {seen.get(v)} and {sh['id']}"
            seen[v] = sh["id"]


def test_a_song_two_radios_agree_on_leads_the_mix(tmp_path, deck_env):
    feed = _feed(tmp_path, deck_env, A, B)
    mix = next(s for s in feed["shelves"] if s["id"] == "genius-mix")
    assert mix["items"][0]["video_id"] == "sharedTrk01"


def test_a_music_video_seed_radios_from_its_song_version(tmp_path, deck_env):
    _feed(tmp_path, deck_env, VIDEO)
    assert ("radio", "songOfvvvvv") in deck_env.calls
    assert ("radio", "vvvvvvvvvv1") not in deck_env.calls


def test_artist_mixes_and_new_releases(tmp_path, deck_env):
    feed = _feed(tmp_path, deck_env, A, B)
    ids = [s["id"] for s in feed["shelves"]]
    assert "mix-UCalpha" in ids and "mix-UCbeta" in ids
    mix = next(s for s in feed["shelves"] if s["id"] == "mix-UCalpha")
    assert mix["title"] == "Alpha Mix" and mix["items"]
    new = next(s for s in feed["shelves"] if s["id"] == "new-releases")
    titles = [t["title"] for t in new["items"]]
    assert "New Single" in titles and "Old Single" not in titles
    assert all(t["kind"] == "album" and t["album_id"] for t in new["items"])


def test_albums_for_you_are_albums_the_radios_point_at(tmp_path, deck_env):
    feed = _feed(tmp_path, deck_env, A, B)
    albums = next(s for s in feed["shelves"] if s["id"] == "albums-for-you")
    assert albums["items"] and all(t["kind"] == "album" for t in albums["items"])
    assert "A album" not in [t["title"] for t in albums["items"]]  # not an album you already play


def test_an_expired_feed_is_served_at_once_and_rebuilt_behind_it(deck_env, monkeypatch):
    """A v2 build is ~10-15 s of paced calls; opening the app after the TTL must not wait for it."""
    import threading
    import time

    from starlette.testclient import TestClient

    from tapedeck import service
    from tapedeck.server import create_app

    owner = {"Authorization": "Bearer owner-token-0123456789"}
    gate = threading.Event()
    real = service.build

    def slow_build(*a, **k):
        if service._feeds.get("owner"):  # the rebuild, not the first build
            gate.wait(5)
        return real(*a, **k)
    monkeypatch.setattr(service, "build", slow_build)
    with TestClient(create_app({"owner": "owner-token-0123456789", "agent": "agent-token-0123456789"},
                               prewarm=False)) as c:
        first = c.get("/api/feed", headers=owner).json()["generated"]
        service._feeds["owner"]["generated"] -= service.FEED_TTL + 1  # make it old
        old = service._feeds["owner"]["generated"]
        t0 = time.monotonic()
        served = c.get("/api/feed", headers=owner).json()["generated"]
        assert served == old and time.monotonic() - t0 < 2  # answered from what it had, without waiting
        gate.set()
        for _ in range(250):
            if service._feeds["owner"]["generated"] > first:
                break
            time.sleep(0.02)
        assert service._feeds["owner"]["generated"] > first  # and rebuilt behind it


def test_because_title_reads_as_the_song_not_the_video(tmp_path, deck_env):
    feed = _feed(tmp_path, deck_env, VIDEO)
    titles = [s["title"] for s in feed["shelves"] if s["id"].startswith("because-")]
    assert titles == ["Because you played Moonlight"]


# ---- v3: ranking with churn, artist satisfaction, listen again, explore ----

def test_a_song_shown_before_and_not_played_sinks(tmp_path, deck_env):
    first = _feed(tmp_path, deck_env, A, B)
    top = next(s for s in first["shelves"] if s["id"] == "genius-mix")["items"][0]["video_id"]
    s = Store(tmp_path / "g.db")
    again = build(s, deck_env, set(), random.Random(3), shown={top: 3.0})
    picks = [t["video_id"] for t in next(x for x in again["shelves"] if x["id"] == "genius-mix")["items"]]
    assert picks[0] != top and (top not in picks or picks.index(top) > 0)


def test_listen_again_brings_back_favourites_you_havent_played_lately(tmp_path, deck_env):
    import time
    s = Store(tmp_path / "g.db")
    old = time.time() - 10 * 86400
    s.add_event(A, "finish", ts=old)
    s.add_event(A, "like", ts=old)
    s.add_event(B, "finish")
    feed = build(s, deck_env, set(), random.Random(3))
    again = next(x for x in feed["shelves"] if x["id"] == "listen-again")
    assert [t["video_id"] for t in again["items"]] == ["aaaaaaaaaa1"]  # B was just played: not "again" yet
    assert again["owner_only"] is True


def test_an_artist_you_keep_skipping_ranks_lower(tmp_path, deck_env):
    s = Store(tmp_path / "g.db")
    for t in (A, B):
        s.add_event(t, "finish")
        s.add_event(t, "like")
    for i in range(6):  # Artist1 shows up in both radios; you skip them every time
        s.add_event({"video_id": f"skipArt{i:04d}", "title": f"S{i}", "artists": ["Artist1"]}, "skip")
    feed = build(s, deck_env, set(), random.Random(3))
    picks = next(x for x in feed["shelves"] if x["id"] == "genius-mix")["items"]
    leads = [t["artists"][0] for t in picks]
    assert "Artist1" not in leads[:4]


def test_the_album_version_of_a_video_you_played_isnt_new(tmp_path, deck_env, monkeypatch):
    played = {"video_id": "vidOfSong01", "title": "Radio aaa 1 (Official Video)", "artists": ["Artist1"]}
    s = Store(tmp_path / "g.db")
    for t in (A, B):
        s.add_event(t, "finish")
    s.add_event(played, "finish")
    feed = build(s, deck_env, set(), random.Random(3))
    titles = [t["title"] for sh in feed["shelves"] if sh["id"] != "listen-again" for t in sh["items"]]
    assert "Radio aaa 1" not in titles


def test_a_private_song_version_is_never_used_as_a_seed(tmp_path, deck_env):
    feed = _feed  # noqa
    s = Store(tmp_path / "g.db")
    s.add_event(VIDEO, "finish")
    s.add_event(VIDEO, "like")
    build(s, deck_env, set(), random.Random(3), exclude=lambda t: t.get("video_id") == "songOfvvvvv")
    assert ("radio", "songOfvvvvv") not in deck_env.calls  # falls back to the (public) video itself


def test_quick_picks_keep_a_share_for_artists_you_never_played(tmp_path, deck_env):
    feed = _feed(tmp_path, deck_env, A, B)
    picks = next(x for x in feed["shelves"] if x["id"] == "genius-mix")["items"]
    assert any(t["artists"][0] not in ("Alpha", "Beta") for t in picks)


# ---- fixes from the v3 review (2026-11-05) ----

def test_late_night_counts_plays_on_both_sides_of_midnight():
    from tapedeck.genius import _bucket
    assert _bucket(2)[1] == _bucket(23)[1] and 23 in _bucket(2)[1] and 1 in _bucket(23)[1]


def test_a_band_name_with_a_comma_is_one_artist():
    from tapedeck.genius import _near
    from tapedeck.match import norm
    assert _near({"kind": "song", "artists": ["Earth, Wind & Fire"]}, {norm("Earth, Wind & Fire")})
    assert not _near({"kind": "song", "artists": ["Ed and Bob"]}, {norm("Ed")})
    assert _near({"kind": "playlist", "artists": ["SZA, Summer Walker and more"]}, {norm("SZA")})


def test_liked_but_never_played_isnt_listen_again(tmp_path, deck_env):
    import time
    s = Store(tmp_path / "g.db")
    s.add_event(A, "finish", ts=time.time() - 10 * 86400)
    s.add_event(B, "like", ts=time.time() - 10 * 86400)
    feed = build(s, deck_env, set(), random.Random(3))
    again = next(x for x in feed["shelves"] if x["id"] == "listen-again")
    assert [t["video_id"] for t in again["items"]] == ["aaaaaaaaaa1"]


def test_a_thin_rebuild_keeps_the_previous_feed(deck_env, monkeypatch):
    from tapedeck import service
    d = service.Deck("owner")
    for t in (A, B):
        d.store.add_event(t, "finish")
    good = d.feed(refresh=True)
    monkeypatch.setattr(service, "build", lambda *a, **k: {"generated": 1.0, "cold_start": False, "shelves": []})
    again = d.feed(refresh=True)
    assert [s["id"] for s in again["shelves"]] == [s["id"] for s in good["shelves"]]
