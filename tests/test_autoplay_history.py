"""Owner 2026-11-03: 'play one song and just autoplay to the next, smart' and 'view or clean the history'."""

import pytest
from starlette.testclient import TestClient

from tapedeck import service
from tapedeck.server import create_app
from tapedeck.store import Store

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def client(deck_env, monkeypatch):
    monkeypatch.setattr(service, "prefetch", lambda ids: 0)
    with TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)) as c:
        yield c


def ev(vid, event, artist="Artist0", ts=None):
    Store().add_event({"video_id": vid, "title": vid, "artists": [artist]}, event, ts=ts)


def test_autoplay_skips_recent_skipped_queued_and_caps_each_artist(client):
    seed = "seedSong001"
    ev("seedSong0r0001", "play")  # FakeYTM radio ids are seed[:6] + 'r' + 4 digits -> 'seedSor0001' style
    radio_ids = [t.video_id for t in service._ytm.radio(seed)][1:]
    played, skipped, queued = radio_ids[0], radio_ids[1], radio_ids[2]
    ev(played, "finish")
    ev(skipped, "skip")
    r = client.get(f"/api/autoplay/{seed}?exclude={queued}&limit=10", headers=H(OWNER))
    assert r.status_code == 200
    ids = [t["video_id"] for t in r.json()]
    assert ids and seed not in ids and not {played, skipped, queued} & set(ids)
    leads = [t["artists"][0] for t in r.json()]
    assert all(leads.count(a) <= 2 for a in set(leads))


def test_autoplay_prefers_artists_you_play(client):
    for _ in range(3):
        ev("favSong0001", "finish", artist="Artist2")
    first = client.get("/api/autoplay/seedSong001?limit=6", headers=H(OWNER)).json()
    assert first[0]["artists"] == ["Artist2"]


def test_history_clear_keeps_likes_and_single_delete_works(client):
    ev("aaaaaaaaaaa", "play")
    ev("bbbbbbbbbbb", "finish")
    ev("ccccccccccc", "like")
    hist = client.get("/api/history", headers=H(OWNER)).json()
    assert {h["event"] for h in hist} == {"play", "finish", "like"} and all("id" in h for h in hist)
    one = next(h for h in hist if h["video_id"] == "aaaaaaaaaaa")
    assert client.delete(f"/api/history/{one['id']}", headers=H(OWNER)).json() == {"removed": 1}
    assert client.delete(f"/api/history/{one['id']}", headers=H(OWNER)).status_code == 404
    assert client.delete("/api/history", headers=H(OWNER)).json() == {"removed": 1}
    left = client.get("/api/history", headers=H(OWNER)).json()
    assert [h["event"] for h in left] == ["like"]  # opinions survive a history clear
    assert "ccccccccccc" in Store().liked()


def test_agents_cannot_read_or_clear_history(client):
    assert client.get("/api/history", headers=H(AGENT)).status_code == 403
    assert client.delete("/api/history", headers=H(AGENT)).status_code == 403
