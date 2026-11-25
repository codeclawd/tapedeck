"""Owner 2026-11-20: "make YouTube Music know it's the same person", without a Google account. Plays are reported
under one anonymous visitor id that survives restarts, and its home page feeds the owner's Genius."""
from tapedeck import service, ytm
from tapedeck.service import Deck


def _song(vid, artist="Public Person"):
    return {"video_id": vid, "title": "A song", "artists": [artist]}


def test_owner_plays_are_reported_but_never_private_ones(deck_env):
    owner = Deck("owner")
    owner.add_rule("artist", "Private Person")
    owner.event(_song("publicAAAAA"), "play")
    owner.event(_song("privateAAAA", "Private Person"), "play")
    owner.event(_song("publicBBBBB"), "skip")  # only plays are reported
    service._visitor_q.join()
    assert service._visitor.reported == ["publicAAAAA"]


def test_other_peoples_plays_are_not_reported(deck_env):
    Deck("owner", account="zz-sara").event(_song("saraAAAAAAA"), "play")
    service._visitor_q.join()
    assert service._visitor.reported == []


def test_owner_feed_uses_the_visitor_home_and_agents_do_not(deck_env):
    mine = [s for s in Deck("owner").feed()["shelves"] if s["id"].startswith("ytm-")]
    assert mine and mine[0]["subtitle"] == "From YouTube Music, tuned to your plays"
    assert mine[0]["items"][0]["video_id"] == "mineAAAAAAA"
    agents = [s for s in Deck("agent").feed()["shelves"] if s["id"].startswith("ytm-")]
    assert agents and all(s["subtitle"] == "From YouTube Music, no account" for s in agents)
    assert "mineAAAAAAA" not in {t.get("video_id") for s in agents for t in s["items"]}


def test_visitor_id_is_saved_once_and_reused(tmp_path, monkeypatch):
    made = iter(["first-id", "second-id"])

    class FakeApi:
        def __init__(self, proxies=None):
            self.headers = {ytm.VISITOR_HEADER: next(made)}
    monkeypatch.setattr(ytm, "YTMusic", FakeApi)
    path = tmp_path / "visitor_id"
    a = ytm.Visitor(path, pace=lambda: None)
    assert a.api.headers[ytm.VISITOR_HEADER] == "first-id" and path.read_text() == "first-id"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    b = ytm.Visitor(path, pace=lambda: None)  # a restart: a new session, the same visitor
    assert b.api.headers[ytm.VISITOR_HEADER] == "first-id"


def test_a_play_with_no_artist_names_is_not_reported(deck_env, monkeypatch):
    """Review 2026-11-20: if the catalog lookup fails, privacy rules have no names to match, so fail closed."""
    def no_song(vid):
        raise LookupError("gone")
    monkeypatch.setattr(deck_env, "song", no_song)
    Deck("owner").event({"video_id": "noNamesAAAA"}, "play")
    service._visitor_q.join()
    assert service._visitor.reported == []


def test_a_blank_visitor_id_is_never_saved(tmp_path, monkeypatch):
    class BotCheckApi:
        def __init__(self, proxies=None):
            self.headers = {ytm.VISITOR_HEADER: ""}
    monkeypatch.setattr(ytm, "YTMusic", BotCheckApi)
    path = tmp_path / "visitor_id"
    try:
        ytm.Visitor(path, pace=lambda: None)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    assert not path.exists()
