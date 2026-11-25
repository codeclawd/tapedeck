"""Guards for the 2026-11-03 adversarial review (findings F1-F15). Each test asserts the CORRECT behaviour."""
import os
import time

import pytest
from starlette.testclient import TestClient

from tapedeck import service
from tapedeck.library import Log
from tapedeck.models import Track
from tapedeck.server import create_app
from tapedeck.service import Deck, check_video_id
from tapedeck.store import Store

OWNER, AGENT = "owner-token-0123456789", "agent-token-0123456789"


def H(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def client(deck_env, monkeypatch):
    monkeypatch.setattr(service, "_ensure_worker", lambda: None)  # jobs stay queued; tests drive run_job directly
    with TestClient(create_app({"owner": OWNER, "agent": AGENT}, prewarm=False)) as c:
        yield c


def _file(tmp_path, name, mtime=None):
    f = tmp_path / name
    f.write_bytes(b"A" * 100)
    if mtime:
        os.utime(f, (mtime, mtime))
    return f


def hide(client, **rule):
    assert client.post("/api/private", headers=H(OWNER), json=rule).status_code == 201


def test_f1_collab_by_hidden_artist_is_hidden_in_library(client, tmp_path):
    log = Log()
    log.add(log.new_job(label="x"), "aaaaaaaaaaa", None, _file(tmp_path, "a.m4a"),
            {"title": "Collab", "artist": "Guest, Private Person", "album": "A"})
    hide(client, kind="artist", key="Private Person")
    assert client.get("/api/library", headers=H(AGENT)).json() == []


def test_f2_taste_ignores_private_songs(client):
    s = Store()
    t = {"video_id": "secretSong1", "title": "Secret", "artists": ["Hidden Soloist"]}
    for e in ("play", "finish", "finish"):
        s.add_event(t, e)
    hide(client, kind="song", key="secretSong1")
    assert client.get("/api/taste", headers=H(AGENT)).json() == []
    assert client.get("/api/taste", headers=H(OWNER)).json()[0]["name"] == "Hidden Soloist"


def test_f3_owner_jobs_for_private_things_never_show_to_agents(client):
    hide(client, kind="song", key="secretSong1")
    hide(client, kind="album", key="MPREb_secretalbum", ref="Secret Album")
    for label, status in (("Secret Song — Public Artist", "queued"), ("Secret Album", "queued"),
                          ("album MPREb_secretalbum", "failed")):
        Log().new_job(label=label, actor="owner", status=status)
    assert client.get("/api/jobs", headers=H(AGENT)).json() == []


def test_f4_agent_cannot_probe_privacy_with_its_own_downloads(client, tmp_path):
    hide(client, kind="artist", key="Private Person")
    hide(client, kind="song", key="secretSong1")
    log = Log()
    log.add(log.new_job(label="o"), "secretSong1", None, _file(tmp_path, "own.m4a"), {"title": "S", "artist": "Z", "album": "A"})
    # (a) an agent download of a hidden artist's song stays visible to that agent, file and job
    probe = client.post("/api/downloads", headers=H(AGENT), json={"items": [{"video_id": "aaaaaaaaaaa"}]}).json()
    service.run_job([Track(title="t", artists=["Private Person"], video_id="aaaaaaaaaaa")], log, service.shared_ytm(),
                    job=probe["id"], actor="agent", fetch=lambda m, v, r: (_file(tmp_path, "p.m4a"), True))
    assert probe["id"] in [j["id"] for j in client.get("/api/jobs", headers=H(AGENT)).json()]
    assert "aaaaaaaaaaa" in [r["video_id"] for r in client.get("/api/library", headers=H(AGENT)).json()]
    # (b) asking for a private song the owner already has reads exactly like a fresh download
    job2 = client.post("/api/downloads", headers=H(AGENT), json={"items": [{"video_id": "secretSong1"}]}).json()["id"]
    _, done, _ = service.run_job([Track(title="S", artists=["Z"], video_id="secretSong1")], log, service.shared_ytm(),
                                 job=job2, actor="agent")
    assert done == 1
    # and the owner's other private songs stay hidden
    assert "secretSong1" in [r["video_id"] for r in client.get("/api/library", headers=H(AGENT)).json()]


def test_f5_counts_and_limits_dont_reveal_hidden_items(client, tmp_path):
    log = Log()
    job = log.new_job(label="x")
    for i, a in enumerate(("Public", "Private Person", "Private Person")):
        log.add(job, f"vid{i}xxxxxxxx"[:11], None, _file(tmp_path, f"{i}.m4a"), {"title": "t", "artist": a, "album": "A"})
        time.sleep(0.01)
    hide(client, kind="artist", key="Private Person")
    assert client.get("/api/status", headers=H(AGENT)).json()["downloads"] == 1
    for n in (1, 2, 3):
        assert len(client.get(f"/api/library?limit={n}", headers=H(AGENT)).json()) == 1


def test_f6_agents_never_get_the_owned_file_or_its_mtime(client, tmp_path, monkeypatch):
    log = Log()
    log.add(log.new_job(label="o"), "secretSong1", None, _file(tmp_path, "own.m4a", 1_700_000_000),
            {"title": "S", "artist": "Z", "album": "A"})
    monkeypatch.setattr(service, "resolve_stream", lambda vid, fresh=False: "http://upstream.invalid/x")
    assert Deck("agent").stream_source("secretSong1")[0] == "url"
    owner = client.get("/api/stream/secretSong1", headers=H(OWNER))
    assert owner.status_code == 200 and "last-modified" not in owner.headers and "etag" not in owner.headers


def test_f7_bare_like_is_filled_in_so_artist_rules_apply(client):
    client.post("/api/events", headers=H(OWNER), json={"track": {"video_id": "likedBareXX"}, "event": "like"})
    hide(client, kind="artist", key="Someone")  # FakeYTM.song() says the id is by "Someone"
    assert client.get("/api/likes", headers=H(AGENT)).json() == []


def test_f9_worker_survives_systemexit_and_restart_reconciles(deck_env, monkeypatch, tmp_path):
    monkeypatch.delenv("TAPEDECK_LIBRARY")
    log = Log()
    job = log.new_job(label="x", status="queued")
    service._jobs.put((job, [{"video_id": "aaaaaaaaaaa"}]))
    service._jobs.put((None, None))  # sentinel: stop after one
    service._work()
    assert Log().jobs()[0]["status"] == "failed"
    stale = log.new_job(label="y", status="running")
    service.reconcile_jobs()
    assert {j["id"]: j["status"] for j in Log().jobs()}[stale] == "failed"


def test_f9_undo_refuses_a_running_job(client, tmp_path):
    log = Log()
    job = log.new_job(label="x", status="running")
    log.add(job, "aaaaaaaaaaa", None, _file(tmp_path, "a.m4a"), {"title": "t", "artist": "a"})
    assert client.post(f"/api/jobs/{job}/undo", headers=H(OWNER)).status_code == 409


def test_f10_phase_one_jobs_get_a_status_and_counts(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (job INTEGER, video_id TEXT, spotify_id TEXT, path TEXT UNIQUE, created TEXT)")
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT)")
    con.execute("INSERT INTO jobs (created) VALUES ('2026-11-02')")
    con.execute("INSERT INTO files VALUES (1, 'aaaaaaaaaaa', NULL, '/l/A/B/01 C.m4a', '2026-11-02')")
    con.commit()
    con.close()
    j = Log(db).jobs()[0]
    assert (j["status"], j["done"], j["total"]) == ("done", 1, 1)


def test_f11_video_id_must_match_exactly():
    with pytest.raises(ValueError):
        check_video_id("abcdefghijk\n")


def test_f12_bad_input_is_400_and_upstream_is_502(client, monkeypatch):
    assert client.post("/api/downloads", headers=H(OWNER), json={"items": "abc"}).status_code == 400
    assert client.post("/api/downloads", headers=H(OWNER), json={"items": [1]}).status_code == 400
    assert client.post("/api/private", headers=H(OWNER), json={"kind": "artist", "key": 5}).status_code == 400
    import yt_dlp

    def boom(vid, fresh=False):
        raise yt_dlp.utils.DownloadError("blocked")
    monkeypatch.setattr(service, "resolve_stream", boom)
    assert client.get("/api/stream/aaaaaaaaaaa", headers=H(OWNER)).status_code == 502


def test_f13_latest_event_wins(tmp_path):
    s = Store(tmp_path / "t.db")
    t = {"video_id": "aaaaaaaaaaa", "title": "x", "artists": ["a"]}
    s.add_event(t, "skip", ts=time.time() - 100)
    s.add_event(t, "finish", ts=time.time() - 50)
    assert "aaaaaaaaaaa" not in s.recent_skips()
    s.add_event(t, "dislike", ts=time.time() - 40)
    s.add_event(t, "like", ts=time.time() - 30)
    assert "aaaaaaaaaaa" not in s.disliked() and "aaaaaaaaaaa" in s.liked()


def test_f15_odd_names_and_negative_limits(client, tmp_path):
    hide(client, kind="artist", key="!!!")
    log = Log()
    for i in range(3):
        log.add(log.new_job(label="x"), f"vid{i}xxxxxxxx"[:11], None, _file(tmp_path, f"{i}.m4a"), {"title": "t", "artist": "Pub"})
    assert len(client.get("/api/library?limit=-1", headers=H(OWNER)).json()) == 1


def test_permissions_gate_agents_and_only_the_owner_changes_them(client, tmp_path):
    assert client.put("/api/permissions", headers=H(AGENT), json={"library": True}).status_code == 403
    assert client.put("/api/permissions", headers=H(OWNER), json={"library": "yes"}).status_code == 400
    assert client.put("/api/permissions", headers=H(OWNER), json={"library": False}).json()["library"] is False
    assert client.get("/api/library", headers=H(AGENT)).status_code == 403
    assert client.get("/api/library", headers=H(OWNER)).status_code == 200
    assert client.get("/api/whoami", headers=H(AGENT)).json()["permissions"]["library"] is False
    # undo "own": an agent may undo only jobs an agent started
    log = Log()
    owner_job = log.new_job(label="o", status="done")
    log.add(owner_job, "aaaaaaaaaaa", None, _file(tmp_path, "o.m4a"), {"title": "t", "artist": "a"})
    agent_job = log.new_job(label="a", actor="agent", status="done")
    log.add(agent_job, "bbbbbbbbbbb", None, _file(tmp_path, "a.m4a"), {"title": "t", "artist": "a"})
    assert client.post(f"/api/jobs/{owner_job}/undo", headers=H(AGENT)).status_code == 403
    assert client.post(f"/api/jobs/{agent_job}/undo", headers=H(AGENT)).json() == {"removed": 1}
    client.put("/api/permissions", headers=H(OWNER), json={"download": False})
    assert client.post("/api/downloads", headers=H(AGENT), json={"items": [{"video_id": "ccccccccccc"}]}).status_code == 403


def test_cli_never_prints_paths_to_an_agent(deck_env, tmp_path, monkeypatch, capsys):
    from tapedeck import cli
    log = Log()
    log.add(log.new_job(label="o"), "secretSong1", None, _file(tmp_path, "own.m4a"), {"title": "S", "artist": "Z"})
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setattr(cli, "YTM", lambda: deck_env)
    cli.main(["get", "secretSong1"])
    out = capsys.readouterr()
    assert str(tmp_path) not in out.out + out.err


def test_stream_refusal_marks_youtube_unhealthy_and_requests_one_vpn_switch(client, monkeypatch, tmp_path):
    marker = tmp_path / "switched"
    monkeypatch.setenv("TAPEDECK_ON_STREAM_BLOCKED", f"touch {marker}")
    monkeypatch.setitem(service._health, "switch_requested", None)
    monkeypatch.setattr(service, "resolve_stream", lambda vid, fresh=False: "https://upstream.invalid/x")

    class Refused:
        status_code, headers = 403, {}

        def close(self):
            pass
    monkeypatch.setattr("tapedeck.server.requests.get", lambda *a, **k: Refused())
    for _ in range(3):
        assert client.get("/api/stream/aaaaaaaaaaa", headers=H(OWNER)).status_code == 502
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.02)
    assert marker.exists()
    st = client.get("/api/status", headers=H(OWNER)).json()
    assert st["youtube"]["youtube"] is False and st["vpn_switch_requested"]
