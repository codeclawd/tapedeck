from tapedeck.ytm import _track


def test_album_track_with_plain_string_album():
    # get_album() returns each track's "album" as a string, search() as {"name", "id"}
    t = _track({"title": "Loser", "artists": [{"name": "Tame Impala"}], "videoId": "v", "album": "Deadbeat",
                "duration_seconds": 224}, {"title": "Deadbeat"})
    assert t.album == "Deadbeat" and t.album_id is None
    s = _track({"title": "Loser", "artists": None, "videoId": "v", "album": {"name": "Deadbeat", "id": "MPRE"}})
    assert s.album == "Deadbeat" and s.album_id == "MPRE" and s.artists == []
