import random

from tapedeck.genius import build
from tapedeck.store import Store

LIKED = {"video_id": "likedSong01", "title": "Liked", "artists": ["Fav"], "artist_id": "UCfav"}
SECRET = {"video_id": "secretSng01", "title": "Secret", "artists": ["Hidden"], "artist_id": "UChid"}


def test_cold_start_is_youtube_music_home(tmp_path, deck_env):
    feed = build(Store(tmp_path / "g.db"), deck_env, set(), random.Random(1))
    assert feed["cold_start"] is True
    assert [s["title"] for s in feed["shelves"]] == ["Quick picks", "Throwbacks"]


def test_history_builds_mix_because_and_related_and_drops_skips(tmp_path, deck_env):
    s = Store(tmp_path / "g.db")
    s.add_event(LIKED, "finish")
    s.add_event(LIKED, "like")
    skipped = {"video_id": "likedSr0003", "title": "Radio Lik 3", "artists": ["Artist0"]}
    s.add_event(skipped, "skip")
    feed = build(s, deck_env, set(), random.Random(1))
    ids = [x["id"] for x in feed["shelves"]]
    assert ids[0] == "genius-mix" and "because-likedSong01" in ids  # v2: artist mixes sit between them
    assert "artists-like-UCfav" in ids  # v3: "Listen again" replaces On Repeat, and needs a few days' gap
    mix = feed["shelves"][0]["items"]
    assert mix and all(t["video_id"] != "likedSr0003" for t in mix)  # skipped recently → never recommended
    assert all(t["video_id"] != "likedSong01" for t in mix)  # the seed itself isn't recommended back


def test_exclude_keeps_private_songs_from_shaping_the_feed(tmp_path, deck_env):
    s = Store(tmp_path / "g.db")
    s.add_event(SECRET, "like")
    s.add_event(LIKED, "like")
    build(s, deck_env, set(), random.Random(1), exclude=lambda t: t["video_id"] == "secretSng01")
    assert not [c for c in deck_env.calls if "secretSng01" in c]  # not even looked up
    assert ("song_version", "likedSong01") in deck_env.calls and any(c[0] == "radio" for c in deck_env.calls)
