"""Owner 2026-11-20: use YouTube Music's, Spotify's and SoundCloud's own recommenders with one tapedeck profile
(no Google account), and only ever the audio versions of songs, never the music videos."""
import random
import time

import pytest
from starlette.testclient import TestClient

from tapedeck import genius, outside, service
from tapedeck.models import Track
from tapedeck.server import create_app
from tapedeck.store import Store
from tapedeck.ytm import YTM

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"
H = {"Authorization": f"Bearer {OWNER}"}


@pytest.fixture
def client(deck_env, outside_fake, monkeypatch):
    monkeypatch.setattr(service, "prefetch", lambda ids: 0)
    with TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)) as c:
        yield c


# ---- audio versions only -------------------------------------------------------------------------------------
class _Api:
    """ytmusicapi's get_watch_playlist: a video seed's radio is videos, its album track's radio is audio."""

    def __init__(self):
        self.asked = []

    def get_watch_playlist(self, videoId=None, radio=False, limit=25, playlistId=None):
        self.asked.append(videoId)
        if videoId == "videoSeed01":
            rows = [("videoSeed01", "Song (Official Video)", "MUSIC_VIDEO_TYPE_OMV")] + \
                   [(f"omvRow{i:05d}", f"Video {i}", "MUSIC_VIDEO_TYPE_OMV") for i in range(5)]
        else:
            rows = [(videoId, "Song", "MUSIC_VIDEO_TYPE_ATV"), ("ugcRow00001", "Fan upload", "MUSIC_VIDEO_TYPE_UGC")] + \
                   [(f"atvRow{i:05d}", f"Audio {i}", "MUSIC_VIDEO_TYPE_ATV") for i in range(5)]
        return {"tracks": [{"videoId": v, "title": t, "videoType": vt, "artists": [{"name": "A", "id": "UCa"}],
                            "album": {"name": "Alb", "id": "MPREb_x"} if vt == "MUSIC_VIDEO_TYPE_ATV" else None}
                           for v, t, vt in rows]}


def _ytm(api):
    y = YTM.__new__(YTM)
    y.api, y.gap, y._last, y._cache = api, 0.0, 0.0, {}
    import threading
    y._lock = threading.Lock()
    y.song_version = lambda d: {**d, "video_id": "albumTrack1", "album": "Alb"}
    return y


def test_a_music_video_seed_gets_the_radio_of_its_audio_version_and_no_videos():
    api = _Api()
    out = _ytm(api).radio("videoSeed01", 50)
    assert api.asked == ["videoSeed01", "albumTrack1"]
    assert out and all(t.video_id.startswith(("albumTrack1", "atvRow")) for t in out)


def test_an_audio_seed_keeps_its_radio_and_drops_fan_uploads():
    api = _Api()
    out = _ytm(api).radio("audioSeed01", 50)
    assert api.asked == ["audioSeed01"]
    assert "ugcRow00001" not in {t.video_id for t in out} and len(out) == 6


# ---- autoplay: every service votes ---------------------------------------------------------------------------
def test_a_song_two_services_agree_on_beats_youtubes_own_first_pick(client, outside_fake):
    # FakeYTM's radio for seedSong001 ends with "Radio see 9" by Artist0; Spotify puts it first.
    outside_fake.votes = {"spotify": [{"title": "Radio see 9", "artists": ["Artist0"], "spotify_id": "sp9"}]}
    r = client.get("/api/autoplay/seedSong001?limit=6", headers=H)
    assert r.status_code == 200
    assert r.json()[0]["title"] == "Radio see 9"
    assert outside_fake.seeds and outside_fake.seeds[0]["video_id"] == "seedSong001"


def test_a_song_only_spotify_suggests_is_played_as_its_youtube_audio_track(client, outside_fake, deck_env):
    outside_fake.votes = {"spotify": [{"title": "Brand New", "artists": ["Newcomer"], "spotify_id": "spNEW"},
                                      {"title": "Nowhere Song", "artists": ["Ghost"], "spotify_id": "spGONE"}]}
    first = client.get("/api/autoplay/seedSong001?limit=10", headers=H).json()
    titles = [t["title"] for t in first]
    assert "Brand New" in titles and "Nowhere Song" not in titles
    new = next(t for t in first if t["title"] == "Brand New")
    assert len(new["video_id"]) == 11 and new["album"] == "Matched Album"
    asked = [c for c in deck_env.calls if c[0] == "match"]
    client.get("/api/autoplay/seedSong001?limit=10", headers=H)
    assert [c for c in deck_env.calls if c[0] == "match"] == asked  # hits and misses both cached


def test_outside_failure_leaves_youtube_autoplay_working(client, monkeypatch):
    def boom(seed):
        raise RuntimeError("spotify changed its page")
    monkeypatch.setattr(service._outside, "similar", boom)
    r = client.get("/api/autoplay/seedSong001?limit=5", headers=H)
    assert r.status_code == 200 and len(r.json()) == 5


def test_the_playing_song_never_comes_back_from_another_service(client, outside_fake):
    outside_fake.votes = {"spotify": [{"title": "A song", "artists": ["Someone"], "spotify_id": "spSELF"}]}
    titles = [t["title"] for t in client.get("/api/autoplay/seedSong001?limit=10", headers=H).json()]
    assert "A song" not in titles  # FakeYTM.song() names the seed "A song" by Someone


def test_a_song_you_skipped_stays_out_even_when_spotify_suggests_it(client, outside_fake):
    Store().add_event({"video_id": "skipped0001", "title": "Skip Me", "artists": ["Artist0"]}, "skip")
    outside_fake.votes = {"spotify": [{"title": "Skip Me", "artists": ["Artist0"], "spotify_id": "spSKIP"}]}
    titles = [t["title"] for t in client.get("/api/autoplay/seedSong001?limit=10", headers=H).json()]
    assert "Skip Me" not in titles


def test_a_disliked_music_video_doesnt_come_back_as_its_album_track_via_spotify(client, outside_fake):
    # Disliked songs leave the play scores entirely, so the ban by title must come from history.
    Store().add_event({"video_id": "dislikedOMV", "title": "Hate It (Official Video)", "artists": ["Artist1"]},
                      "dislike")
    outside_fake.votes = {"spotify": [{"title": "Hate It", "artists": ["Artist1"], "spotify_id": "spHATE"}]}
    titles = [t["title"] for t in client.get("/api/autoplay/seedSong001?limit=10", headers=H).json()]
    assert "Hate It" not in titles


# ---- the match cache ------------------------------------------------------------------------------------------
def test_match_cache_keeps_hits_and_misses_for_a_week(deck_env):
    s = Store()
    assert s.xmatch_get("spotify:x") == (False, None)
    s.xmatch_put("spotify:x", {"video_id": "abcdefghijk"}, now=1000.0)
    s.xmatch_put("spotify:y", None, now=1000.0)
    assert s.xmatch_get("spotify:x", now=2000.0) == (True, {"video_id": "abcdefghijk"})
    assert s.xmatch_get("spotify:y", now=2000.0) == (True, None)
    assert s.xmatch_get("spotify:x", now=1000.0 + 8 * 86400) == (False, None)


# ---- the home feed --------------------------------------------------------------------------------------------
def test_feed_seeds_ask_other_services_and_their_agreement_ranks_quick_picks(deck_env, outside_fake):
    store = Store()
    for _ in range(3):
        store.add_event({"video_id": "favSong0001", "title": "Fav", "artists": ["Artist2"], "album": "A"}, "finish")
    outside_fake.votes = {"spotify": [{"title": "Radio fav 9", "artists": ["Artist0"]},
                                      {"title": "Only On Spotify", "artists": ["Artist2"]}]}
    feed = genius.build(store, deck_env, set(), random.Random(1), outside=outside_fake.similar,
                        resolve=lambda t: service.resolve_outside(deck_env, store, t))
    picks = next(s for s in feed["shelves"] if s["id"] == "genius-mix")["items"]
    titles = [t["title"] for t in picks]
    assert "Only On Spotify" in titles
    assert titles.index("Radio fav 9") < titles.index("Radio fav 1")  # agreement lifts a late YouTube pick


def test_spotify_artist_radio_joins_the_artist_mix(deck_env, outside_fake):
    store = Store()
    for _ in range(3):
        store.add_event({"video_id": "favSong0001", "title": "Fav", "artists": ["Artist2"], "album": "A",
                         "artist_id": "UCfav0000000000000000001"}, "finish")
    outside_fake.artist_votes = {"spotify": [{"title": "Spotify Pairs This", "artists": ["Pal"]}]}
    feed = genius.build(store, deck_env, set(), random.Random(1), outside=outside_fake.similar,
                        outside_artist=outside_fake.artist_similar,
                        resolve=lambda t: service.resolve_outside(deck_env, store, t))
    shown = [t["title"] for s in feed["shelves"] for t in s.get("items") or []]
    assert "Spotify Pairs This" in shown  # only the artist path nominated it (no song-radio votes set)
    assert any(s["id"].startswith("mix-") for s in feed["shelves"])


# ---- parsers --------------------------------------------------------------------------------------------------
def test_spotify_search_rows_parse():
    data = {"data": {"searchV2": {"tracksV2": {"items": [{"item": {"data": {
        "uri": "spotify:track:7AzlLxHn24DxjgQX73F9fU", "name": "No Idea",
        "artists": {"items": [{"profile": {"name": "Don Toliver"}}]},
        "albumOfTrack": {"name": "Heaven Or Hell"}, "duration": {"totalMilliseconds": 154424}}}},
        {"item": {"data": {"uri": "spotify:episode:x", "name": "Podcast"}}}]}}}}
    [t] = outside.parse_search(data)
    assert (t.title, t.artists, t.spotify_id, t.duration) == ("No Idea", ["Don Toliver"], "7AzlLxHn24DxjgQX73F9fU", 154)


def test_soundcloud_fan_upload_title_gives_the_artist():
    t = outside.parse_sc({"title": "Travis Scott - goosebumps", "user": {"username": "fan123"}, "duration": 243000})
    assert (t.title, t.artists, t.duration) == ("goosebumps", ["Travis Scott"], 243)
    t = outside.parse_sc({"title": "After Party", "publisher_metadata": {"artist": "Don Toliver"}})
    assert t.artists == ["Don Toliver"]


def test_a_slow_or_broken_service_just_doesnt_vote():
    class Slow:
        name = "slow"

        def similar(self, seed):
            time.sleep(2)
            return [Track(title="late")]

    class Broken:
        name = "broken"

        def similar(self, seed):
            raise ValueError("page changed")

    class Good:
        name = "good"

        def similar(self, seed):
            return [Track(title="ok", artists=["B"])]

    t0 = time.monotonic()
    votes = outside.Sources([Slow(), Broken(), Good()], budget=0.3).similar({"title": "x", "artists": ["A"]})
    assert votes == {"good": [Track(title="ok", artists=["B"]).to_dict()]} and time.monotonic() - t0 < 1.5
