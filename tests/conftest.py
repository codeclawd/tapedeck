import pytest

from tapedeck import service
from tapedeck.models import Track


class FakeYTM:
    """Stands in for YouTube Music: deterministic radios, one artist page, two home shelves."""

    def __init__(self):
        self.calls = []

    def _t(self, vid, title, artist, aid=None):
        return Track(title=title, artists=[artist], video_id=vid, artist_id=aid, cover="https://lh3.googleusercontent.com/x=w60-h60")

    def radio(self, video_id, limit=25):
        """Song radio: 10 of its own tracks (on 4 albums) plus one track every radio shares."""
        self.calls.append(("radio", video_id))
        own = [self._t(f"{video_id[:6]}r{i:04d}", f"Radio {video_id[:3]} {i}", f"Artist{i % 3}") for i in range(10)]
        for i, t in enumerate(own):
            t.album, t.album_id = f"Album {video_id[:3]} {i % 4}", f"MPREb_{video_id[:4]}{i % 4}"
        shared = self._t("sharedTrk01", "Everyone agrees", "Common")
        shared.album, shared.album_id = "Common Album", "MPREb_common"
        return [self._t(video_id, "seed", "Seed")] + own[:3] + [shared] + own[3:]

    def artist(self, channel_id):
        import time as _time
        self.calls.append(("artist", channel_id))
        thumb = [{"url": "https://lh3.googleusercontent.com/a=w60-h60"}]
        return {"name": "Someone", "radioId": f"RDEM{channel_id}",
                "singles": {"results": [{"title": "New Single", "year": str(_time.gmtime().tm_year),
                                         "browseId": f"MPREb_new{channel_id[-3:]}", "thumbnails": thumb},
                                        {"title": "Old Single", "year": "2001", "browseId": f"MPREb_old{channel_id[-3:]}",
                                         "thumbnails": thumb}]},
                "related": {"results": [{"browseId": "UCrelated000000000000001", "title": "Related One", "thumbnails": thumb}]}}

    def artist_radio(self, playlist_id, limit=25):
        self.calls.append(("artist_radio", playlist_id))
        out = []
        for i in range(12):
            t = self._t(f"{playlist_id[-5:]}m{i:05d}"[-11:], f"Mix {playlist_id[-3:]} {i}", f"MixArtist{i % 4}")
            t.album, t.album_id = "Mix Album", f"MPREb_mix{i % 2}"
            out.append(t)
        return out

    def song_version(self, d):
        """A music video maps to its album track; a song is already one."""
        self.calls.append(("song_version", d.get("video_id")))
        if d.get("album"):
            return d
        return {**d, "video_id": "songOf" + d["video_id"][:5], "album": "Real Album", "album_id": "MPREb_real"}

    def home(self, limit=6):
        self.calls.append(("home",))
        return [{"title": "Quick picks", "items": [self._t("homeAAAAAAA", "Home song", "Home Artist")]},
                {"title": "Throwbacks", "items": [self._t("homeBBBBBBB", "Old song", "Old Artist")]}]

    def search(self, q, limit=10):
        self.calls.append(("search", q))
        return [self._t("searchAAAAA", f"{q} hit", "Private Person"), self._t("searchBBBBB", f"{q} other", "Public Person")]

    search_albums = search
    search_artists = search

    def match(self, want):
        """An outside song is on YouTube Music unless its title says otherwise."""
        self.calls.append(("match", want.title))
        if "nowhere" in want.title.lower():
            return None
        t = self._t(("m" + "".join(c for c in want.title if c.isalnum()))[:11].ljust(11, "x"), want.title,
                    want.artists[0] if want.artists else "Someone")
        t.album, t.album_id = "Matched Album", "MPREb_match"
        return t

    def song(self, video_id):
        return self._t(video_id, "A song", "Someone")

    def meta(self, t):
        return {"title": t.title, "artist": t.artist, "album": t.album or "Singles"}


class FakeOutside:
    """Stands in for Spotify/SoundCloud: no network. Tests set `votes` = {source: [track dicts]}."""

    def __init__(self):
        self.votes: dict[str, list[dict]] = {}
        self.seeds: list[dict] = []

        self.artist_votes: dict[str, list[dict]] = {}

    def similar(self, seed):
        self.seeds.append(seed)
        return {s: [dict(t) for t in ts] for s, ts in self.votes.items()}

    def artist_similar(self, seed):
        return {s: [dict(t) for t in ts] for s, ts in self.artist_votes.items()}


class FakeVisitor:
    """The owner's anonymous YouTube Music visitor, without the network."""

    def __init__(self):
        self.reported = []

    def report_play(self, video_id):
        self.reported.append(video_id)
        return True

    def home(self, limit=6):
        return [{"title": "Quick picks", "items": [
            Track(title="Mine", artists=["Home Artist"], video_id="mineAAAAAAA", kind="song")]}]


@pytest.fixture
def outside_fake(deck_env, monkeypatch):
    fake = FakeOutside()
    monkeypatch.setattr(service, "_outside", fake)
    return fake


@pytest.fixture
def deck_env(tmp_path, monkeypatch):
    """Isolated data dir + library and a fake YTM shared by every Deck."""
    monkeypatch.setenv("TAPEDECK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TAPEDECK_LIBRARY", str(tmp_path / "lib"))
    monkeypatch.delenv("NAVIDROME_URL", raising=False)
    fake = FakeYTM()
    monkeypatch.setattr(service, "_ytm", fake)
    monkeypatch.setattr(service, "_outside", FakeOutside())  # never the real services in tests
    monkeypatch.setattr(service, "_visitor", FakeVisitor())
    monkeypatch.setattr(service, "SPARE_QUIET", 0)  # tests don't wait out the quiet period before a rebuild
    _forget_feeds()
    yield fake
    _forget_feeds()
    _wait_for_builds()  # a build left running would set up the next test's fresh database at the same time


def _wait_for_builds():
    import time
    for _ in range(500):
        with service._feed_locks_guard:
            locks = list(service._spare_locks.values())
        if not any(lock.locked() for lock in locks):
            return
        time.sleep(0.01)


def _forget_feeds():
    """Feeds and the ones built ahead are process-wide; no test may pick up another test's."""
    service._feeds.clear()
    with service._spares_guard:
        for timer in service._spare_timers.values():
            timer.cancel()
        service._spare_timers.clear()
        service._spares.clear()
        service._spare_again.clear()
