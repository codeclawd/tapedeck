import json

import pytest

from tapedeck.spotify import parse_embed, parse_link


def page(entity):
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


@pytest.mark.parametrize(
    "link,expected",
    [
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
        ("https://open.spotify.com/intl-de/album/4m2880jivSbbyEGAKfITCa", ("album", "4m2880jivSbbyEGAKfITCa")),
        ("spotify:track:7bxaFZ1O3cHkgLKMsdC3xR", ("track", "7bxaFZ1O3cHkgLKMsdC3xR")),
    ],
)
def test_parse_link(link, expected):
    assert parse_link(link) == expected


def test_parse_link_rejects_other_urls():
    assert parse_link("https://music.youtube.com/watch?v=MX7FSAnRPug") is None


def test_parse_playlist_embed():
    html = page({
        "type": "playlist", "name": "Mix",
        "trackList": [
            {"title": "Loser", "subtitle": "Tame Impala", "uri": "spotify:track:7bxaFZ1O3cHkgLKMsdC3xR", "duration": 223069},
            {"title": "Housewife", "subtitle": "Tate McRae", "uri": "spotify:track:2sFepQNyl1WjpROruP8bJ0", "duration": 208810},
        ],
    })
    name, tracks, truncated = parse_embed(html)
    assert name == "Mix" and not truncated
    assert tracks[0].title == "Loser" and tracks[0].artists == ["Tame Impala"]
    assert tracks[0].duration == 223 and tracks[0].spotify_id == "7bxaFZ1O3cHkgLKMsdC3xR"


def test_parse_album_embed_keeps_album_name():
    html = page({"type": "album", "name": "Random Access Memories", "trackList": [
        {"title": "Get Lucky", "subtitle": "Daft Punk, Pharrell Williams", "uri": "spotify:track:x" * 1, "duration": 369000}]})
    _, tracks, _ = parse_embed(html)
    assert tracks[0].album == "Random Access Memories"


def test_parse_track_embed():
    html = page({"type": "track", "name": "Loser", "artists": [{"name": "Tame Impala"}],
                 "uri": "spotify:track:7bxaFZ1O3cHkgLKMsdC3xR", "duration": 223069})
    name, tracks, _ = parse_embed(html)
    assert name == "Loser" and len(tracks) == 1 and tracks[0].artists == ["Tame Impala"]


def test_hundred_tracks_flags_truncation():
    items = [{"title": f"t{i}", "subtitle": "a", "uri": f"spotify:track:{i}", "duration": 1000} for i in range(100)]
    _, _, truncated = parse_embed(page({"type": "playlist", "name": "Big", "trackList": items}))
    assert truncated


def test_changed_page_shape_fails_loudly():
    with pytest.raises(ValueError):
        parse_embed("<html>nothing here</html>")


def test_partial_data_does_not_crash():
    _, tracks, _ = parse_embed(page({"type": "track", "name": "X", "artists": None, "uri": "spotify:track:a", "duration": 1000}))
    assert tracks[0].artists == []
    _, tracks, _ = parse_embed(page({"type": "playlist", "name": "P", "trackList": None}))
    assert tracks == []
    _, tracks, _ = parse_embed(page({"type": "playlist", "name": "P", "trackList": [{"subtitle": "a"}, {"title": "ok"}]}))
    assert [t.title for t in tracks] == ["ok"]


@pytest.mark.parametrize("entity", [None, [], {"type": "track", "name": "x", "artists": [None]},
                                    {"type": "playlist", "name": "p", "trackList": [None]},
                                    {"type": "playlist", "name": "p", "trackList": [{"title": "t", "duration": "abc"}]}])
def test_malformed_pages_raise_valueerror(entity):
    try:
        parse_embed(page(entity))
    except ValueError:
        pass
