from tapedeck.privacy import Filter

RULES = [
    {"kind": "song", "key": "secretSong1", "ref": None},
    {"kind": "artist", "key": "privateperson", "ref": "UCprivate"},
    {"kind": "album", "key": "MPREsecret", "ref": "hiddenalbum"},
]
SONG = {"video_id": "secretSong1", "title": "x", "artists": ["Someone"]}
BY_ARTIST = {"video_id": "aaaaaaaaaaa", "title": "y", "artists": ["Private Person", "Guest"]}
BY_ALBUM_ID = {"video_id": "bbbbbbbbbbb", "title": "z", "artists": ["A"], "album_id": "MPREsecret"}
BY_ALBUM_NAME = {"video_id": "ccccccccccc", "title": "w", "artists": ["A"], "album": "Hidden Album!"}
ARTIST_CARD = {"kind": "artist", "title": "Someone Else", "artist_id": "UCprivate"}
LIB_ROW = {"video_id": "ddddddddddd", "title": "v", "artist": "Guest, Private Person", "path": "/m/a.m4a"}
PUBLIC = {"video_id": "eeeeeeeeeee", "title": "ok", "artists": ["Public"], "path": "/m/b.m4a", "created": "t"}
PRIVATE = [SONG, BY_ARTIST, BY_ALBUM_ID, BY_ALBUM_NAME, ARTIST_CARD, LIB_ROW]


def test_owner_sees_everything_including_paths():
    f = Filter("owner", RULES)
    assert f.tracks([*PRIVATE, PUBLIC]) == [*PRIVATE, PUBLIC]


def test_agent_never_sees_private_items_in_personal_lists():
    f = Filter("agent", RULES)
    for item in PRIVATE:
        assert f.hidden(item), item
    out = f.tracks([*PRIVATE, PUBLIC])
    assert [t["video_id"] for t in out] == ["eeeeeeeeeee"]
    assert "path" not in out[0] and "created" not in out[0]


def test_catalog_keeps_private_items_but_without_a_tell():
    f = Filter("agent", RULES)
    out = f.catalog([SONG, PUBLIC], owned={"secretSong1", "eeeeeeeeeee"}, liked={"secretSong1"})
    assert [t["video_id"] for t in out] == ["secretSong1", "eeeeeeeeeee"]  # an empty search would itself leak
    assert out[0]["owned"] is False and out[0]["liked"] is False  # looks like any song you don't have
    assert out[1]["owned"] is True
    assert set(out[0]) == {"video_id", "title", "artists", "owned", "liked"}  # same keys as any other song


def test_feed_drops_shelves_seeded_by_private_songs_and_owner_only_shelves():
    shelves = [
        {"id": "because-secret", "title": "Because you played x", "seed": SONG, "items": [PUBLIC]},
        {"id": "on-repeat", "title": "On Repeat", "owner_only": True, "items": [PUBLIC]},
        {"id": "mix", "title": "Genius Mix", "items": [BY_ARTIST, PUBLIC]},
        {"id": "empty-after", "title": "All private", "items": [SONG]},
    ]
    agent = Filter("agent", RULES).feed(shelves)
    assert [s["id"] for s in agent] == ["mix"]
    assert [t["video_id"] for t in agent[0]["items"]] == ["eeeeeeeeeee"]
    assert "seed" not in agent[0]
    owner = Filter("owner", RULES).feed(shelves)
    assert [s["id"] for s in owner] == ["because-secret", "on-repeat", "mix", "empty-after"]
