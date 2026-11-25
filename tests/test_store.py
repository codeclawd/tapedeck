import sqlite3
import time

import pytest

from tapedeck.library import Log
from tapedeck.store import Store

T = {"video_id": "aaaaaaaaaaa", "title": "Song A", "artists": ["Artist A"], "artist_id": "UCa"}
U = {"video_id": "bbbbbbbbbbb", "title": "Song B", "artists": ["Artist B"]}


def test_scores_reward_finishes_and_likes_and_punish_skips(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_event(T, "finish")
    s.add_event(T, "like")
    s.add_event(U, "play")
    s.add_event(U, "skip")
    sc = s.track_scores()
    assert sc["aaaaaaaaaaa"]["score"] == pytest.approx(3.0, abs=0.01)
    assert sc["bbbbbbbbbbb"]["score"] == pytest.approx(-0.7, abs=0.01)
    assert s.recent_skips() == {"bbbbbbbbbbb"}
    assert [a["name"] for a in s.top_artists()] == ["Artist A"]


def test_old_plays_decay_with_a_two_week_half_life(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_event(T, "finish", ts=time.time() - 14 * 86400)
    assert s.track_scores()["aaaaaaaaaaa"]["score"] == pytest.approx(0.5, abs=0.01)


def test_unlike_and_dislike(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_event(T, "like")
    s.add_event(T, "unlike")
    assert s.liked() == set()
    s.add_event(U, "finish")
    s.add_event(U, "dislike")
    assert "bbbbbbbbbbb" not in s.track_scores()


def test_bad_events_are_rejected(tmp_path):
    s = Store(tmp_path / "t.db")
    with pytest.raises(ValueError):
        s.add_event(T, "teleport")
    with pytest.raises(ValueError):
        s.add_event({"title": "no id"}, "play")


def test_rules_normalise_artist_names_and_dedupe(tmp_path):
    s = Store(tmp_path / "t.db")
    r1 = s.add_rule("artist", "Beyoncé", label="Beyoncé")
    r2 = s.add_rule("artist", "BEYONCE")
    assert r1["id"] == r2["id"] and len(s.rules()) == 1
    assert s.remove_rule(r1["id"]) and not s.rules()
    with pytest.raises(ValueError):
        s.add_rule("planet", "x")


def test_log_upgrades_a_phase_one_database_in_place(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE files (job INTEGER, video_id TEXT, spotify_id TEXT, path TEXT UNIQUE, created TEXT)")
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT)")
    con.execute("INSERT INTO jobs (created) VALUES ('2026-11-02')")
    con.execute("INSERT INTO files VALUES (1, 'aaaaaaaaaaa', NULL, '/lib/Tame Impala/Deadbeat/03 Loser.m4a', '2026-11-02')")
    con.commit()
    con.close()
    log = Log(db)
    row = log.files()[0]
    assert (row["title"], row["artist"], row["album"]) == ("Loser", "Tame Impala", "Deadbeat")
    assert log.jobs()[0]["id"] == 1
    j = log.new_job(label="x", actor="agent", status="queued")
    assert j == 2 and log.jobs()[0]["actor"] == "agent"
    log.update_job(j, status="done", done=1)
    assert log.jobs()[0]["finished"]
