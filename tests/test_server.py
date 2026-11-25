import json

import pytest
from starlette.testclient import TestClient

from tapedeck.library import Log
from tapedeck.server import create_app

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"


@pytest.fixture
def client(deck_env, tmp_path):
    app = create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)
    with TestClient(app) as c:
        yield c


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_health_is_open_everything_else_needs_a_token(client):
    assert client.get("/api/health").json()["name"] == "tapedeck"
    assert client.get("/api/feed").status_code == 401
    assert client.get("/api/feed", headers=h("nope-nope-nope-nope")).status_code == 401
    assert client.get("/api/whoami", headers=h(OWNER)).json()["scope"] == "owner"
    assert client.get(f"/api/whoami?t={AGENT}").json()["scope"] == "agent"  # media players can't send headers
    assert client.post("/mcp", json={}).status_code == 401


def test_agent_cannot_touch_privacy_history_events_or_undo(client):
    assert client.get("/api/private", headers=h(AGENT)).status_code == 403
    assert client.post("/api/private", headers=h(AGENT), json={"kind": "artist", "key": "x"}).status_code == 403
    assert client.get("/api/history", headers=h(AGENT)).status_code == 403
    ev = {"track": {"video_id": "aaaaaaaaaaa", "title": "t"}, "event": "like"}
    assert client.post("/api/events", headers=h(AGENT), json=ev).status_code == 403
    assert client.post("/api/jobs/1/undo", headers=h(AGENT)).status_code == 403
    assert client.post("/api/events", headers=h(OWNER), json=ev).status_code == 200


def test_private_artist_vanishes_from_agent_library_but_not_owner(client, tmp_path):
    log = Log()
    job = log.new_job(label="x")
    for vid, artist in (("aaaaaaaaaaa", "Private Person"), ("bbbbbbbbbbb", "Public Person")):
        log.add(job, vid, None, tmp_path / f"{vid}.m4a", {"title": f"song {vid}", "artist": artist, "album": "A"})
    rule = client.post("/api/private", headers=h(OWNER), json={"kind": "artist", "key": "Private Person"}).json()
    agent_lib = client.get("/api/library", headers=h(AGENT)).json()
    assert [r["video_id"] for r in agent_lib] == ["bbbbbbbbbbb"]
    assert "path" not in agent_lib[0]
    owner_lib = client.get("/api/library", headers=h(OWNER)).json()
    assert {r["video_id"] for r in owner_lib} == {"aaaaaaaaaaa", "bbbbbbbbbbb"} and "path" in owner_lib[0]
    # catalog search still returns the private artist's song (an empty search would leak), with no owned mark
    hits = client.get("/api/search?q=x", headers=h(AGENT)).json()
    assert [t["video_id"] for t in hits] == ["searchAAAAA", "searchBBBBB"]
    assert client.delete(f"/api/private/{rule['id']}", headers=h(OWNER)).json() == {"ok": True}
    assert len(client.get("/api/library", headers=h(AGENT)).json()) == 2


def test_cover_proxy_only_fetches_google_image_hosts(client):
    r = client.get("/api/cover?u=http://169.254.169.254/latest", headers=h(OWNER))
    assert r.status_code == 400


def test_bad_ids_are_400_not_500(client):
    assert client.get("/api/stream/../../etc", headers=h(OWNER)).status_code in (400, 404)
    assert client.get("/api/radio/short", headers=h(OWNER)).status_code == 400
    assert client.post("/api/downloads", headers=h(OWNER), json={"items": [{"video_id": "bad"}]}).status_code == 400


def _mcp(client, method, params=None, tok=AGENT, rid=1):
    r = client.post("/mcp", headers={**h(tok), "Accept": "application/json, text/event-stream",
                                     "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"},
                    json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_lists_tools_and_runs_them_in_agent_scope(client, tmp_path):
    init = _mcp(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "0"}})
    assert init["result"]["serverInfo"]["name"] == "tapedeck"
    names = {t["name"] for t in _mcp(client, "tools/list", rid=2)["result"]["tools"]}
    assert {"search_music", "genius_feed", "library", "download", "taste"} <= names
    assert not names & {"private", "rules", "history", "undo", "event"}
    log = Log()
    job = log.new_job(label="x")
    log.add(job, "aaaaaaaaaaa", None, tmp_path / "a.m4a", {"title": "s", "artist": "Private Person"})
    client.post("/api/private", headers=h(OWNER), json={"kind": "artist", "key": "Private Person"})
    # even with the OWNER token, /mcp is agent scope
    res = _mcp(client, "tools/call", {"name": "library", "arguments": {}}, tok=OWNER, rid=3)["result"]
    payload = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    rows = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert rows == []
