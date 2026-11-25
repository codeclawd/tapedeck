from tapedeck.match import best, norm, score
from tapedeck.models import Track


def T(title, artist, dur, vid=None):
    return Track(title=title, artists=[artist], duration=dur, video_id=vid)


def test_norm_strips_noise():
    assert norm("Get Lucky (feat. Pharrell Williams) - Radio Edit") == "get lucky"
    assert norm("Beyoncé") == "beyonce"
    assert norm("Wonderwall - Remastered 2014") == "wonderwall"


def test_same_song_scores_high():
    assert score(T("Loser", "Tame Impala", 223), T("Loser", "Tame Impala", 224)) >= 0.9


def test_remaster_suffix_still_matches():
    assert score(T("Wonderwall - Remastered", "Oasis", 258), T("Wonderwall", "Oasis", 259)) >= 0.8


def test_wrong_artist_rejected():
    assert score(T("Loser", "Tame Impala", 223), T("loser.", "shteppi", 223)) < 0.8


def test_duration_far_off_rejected():
    assert score(T("Loser", "Tame Impala", 223), T("Loser", "Tame Impala", 290)) == 0


def test_best_picks_matching_candidate():
    want = T("Loser", "Tame Impala", 223)
    cands = [T("loser.", "shteppi", 174, "a"), T("Loser", "Tame Impala", 224, "b")]
    assert best(want, cands).video_id == "b"


def test_best_returns_none_below_threshold():
    assert best(T("Loser", "Tame Impala", 223), [T("Other", "Someone", 100, "a")]) is None


def test_non_latin_titles_are_compared_not_erased():
    assert norm("Группа крови") == "группа крови"
    assert norm("طلع البدر علينا") != ""
    assert score(T("Группа крови", "Кино", 285), T("Звезда по имени Солнце", "Нина Хаген", 290)) < 0.8
    assert score(T("طلع البدر علينا", "Maher Zain", 300), T("طلع البدر علينا", "Maher Zain", 301)) >= 0.9


def test_artist_must_match_as_a_whole_name():
    assert score(T("Hey", "Pink", 200), T("Hey", "Pink Floyd", 200)) < 0.8
    assert score(T("Loser", "Tame Impala", 223), Track(title="Loser", artists=[], duration=223)) < 0.8


def test_different_numbers_in_title_reject():
    assert score(T("Interlude 1", "X", 100), T("Interlude 2", "X", 104)) == 0


def test_brackets_only_stripped_on_whole_noise_words():
    assert norm("Stay (Without You)") == "stay without you"
    assert score(T("Time (With You)", "A", 200), T("Time (Without Me)", "A", 200)) < 0.8


def test_indic_and_kana_marks_are_kept():
    assert norm("तू") != norm("तो")
    assert norm("かぜ") != norm("かせ")
    assert norm("Beyoncé") == "beyonce"  # Latin accents still fold


def test_variants_must_agree_on_both_sides():
    assert score(T("Song", "A", 200), T("Song (Live)", "A", 200)) == 0
    assert score(T("Song", "A", 200), T("Song (Acoustic Version)", "A", 200)) == 0
    assert score(T("Song", "A", 200), T("Song (Club Remix)", "A", 200)) == 0
    assert score(T("Song (Live)", "A", 200), T("Song - Live at Wembley", "A", 200)) >= 0.8
    assert score(T("Tala Al Badru (Vocals Only)", "A", 200), T("Tala Al Badru", "A", 200)) == 0
    assert score(T("Song (feat. B)", "A", 200), T("Song", "A", 200)) >= 0.9
    assert norm("Song (Clean Bandit)") == "song clean bandit"


def test_near_titles_reject_but_spelling_variants_match():
    assert score(T("Believe", "Cher", 239), T("Believer", "Cher", 239)) == 0
    assert score(T("Love Me", "The Beatles", 140), T("Love Me Do", "The Beatles", 140)) == 0
    assert score(T("Part I", "A", 200), T("Part II", "A", 200)) == 0
    assert score(T("Tala'al Badru", "A", 200), T("Tala al Badru", "A", 200)) >= 0.9
    assert score(T("Track 01", "A", 200), T("Track 1", "A", 200)) >= 0.9


def test_artist_names_that_normalise_to_nothing():
    assert score(T("Song", "!!!", 200), T("Song", "Other", 200)) < 0.8
    assert score(T("Song", "X", 200), T("Song", "Some Other Band", 200)) < 0.8
    assert score(T("Song", "!!!", 200), T("Song", "!!!", 200)) >= 0.9


def test_comma_band_name_needs_the_lead_artist():
    assert score(T("September", "Earth, Wind & Fire", 215), T("September", "Wind", 215)) < 0.8
    assert score(T("September", "Earth, Wind & Fire", 215), T("September", "Earth, Wind & Fire", 215)) >= 0.9


def A(title, artist, dur, album):
    return Track(title=title, artists=[artist], duration=dur, album=album)


def test_album_version_tags_count():  # D1
    assert score(A("Song", "A", 200, "Greatest Hits"), A("Song", "A", 200, "Live at Wembley")) == 0
    assert score(A("Song", "A", 200, None), A("Song", "A", 200, "Forgive Me (Vocals Only)")) == 0
    assert score(T("Song (Vocals Only)", "A", 200), A("Song", "A", 200, "Song (Vocals Only)")) >= 0.9
    assert score(A("Song", "A", 200, None), A("Song", "A", 200, "Song")) >= 0.9


def test_instrument_and_vocal_markers_are_versions():  # D2
    for v in ["(Vocals with Duff)", "(With Duff)", "(With Music)", "(Official Vocals)", "- Daf Version - Official Audio",
              "(8D Audio)", "(Spatial Audio)", "(Piano Edit)"]:
        assert score(T("Song", "A", 200), T(f"Song {v}", "A", 200)) == 0, v
        assert score(T(f"Song {v}", "A", 200), T("Song", "A", 200)) == 0, v
    assert score(T("Song (Vocals Only)", "A", 200), T("Song [Vocals Only]", "A", 200)) >= 0.9


def test_artist_must_really_match():  # D3, D8
    for a, b in [("Ed Sheeran", "Ed Sheeran Tribute"), ("Drake", "Drake Bell"), ("Michael Jackson", "Michael Bolton"),
                 ("John Legend", "John Lennon"), ("Yusuf Islam", "Yusuf Hassan")]:
        assert score(T("Song", a, 200), T("Song", b, 200)) < 0.8, (a, b)
    assert score(Track("Song", ["Mustafa Ceceli, Maher Zain"], 200), Track("Song", ["Maher Zain", "Mustafa Ceceli"], 200)) >= 0.9
    assert score(T("Song", "Maher Zain", 203), T("Song", "Maher Zain - Topic", 200)) >= 0.8
    assert score(T("Song", "The Beatles", 205), T("Song", "Beatles", 200)) >= 0.8


def test_dash_segments_and_parts():  # D4, D5, D12
    assert score(T("Song - Part 1 - Remastered 2011", "A", 200), T("Song - Part 2 - Remastered 2011", "A", 200)) == 0
    assert score(T("Song", "A", 200), T("Song - Part 2 - Radio Edit", "A", 200)) == 0
    assert score(T("Hasbunallah Wa Nimal Wakeel I", "A", 200), T("Hasbunallah Wa Nimal Wakeel II", "A", 200)) == 0
    assert score(T("Mawlaya Salli Wa Sallim Pt. A", "A", 200), T("Mawlaya Salli Wa Sallim Pt. B", "A", 200)) == 0
    assert score(T("Track 1 2", "A", 200), T("Track 12", "A", 200)) == 0


def test_soundtrack_and_packaging_tags_ignored():  # D7
    for v in ['- From "Frozen"', '(From "Movie")', "- From The Original Motion Picture Soundtrack", "(Deluxe Edition)",
              "(25th Anniversary Edition)", "(Original Version)", "(Studio Version)", "(Clean)", "(Visualizer)"]:
        assert score(T(f"Let It Go {v}", "A", 200), T("Let It Go", "A", 200)) >= 0.9, v
    assert norm("Song (Clean Bandit)") == "song clean bandit"


def test_turkish_and_arabic_orthography():  # D9, D10
    assert norm("Işık") == norm("ISIK") and norm("Sığındım") == norm("Sigindim")
    assert norm("نبی") == norm("نبي") and norm("كريم") == norm("کریم") and norm("رحمة") == norm("رحمه")
    assert norm("محمد ﷺ") == norm("محمد") and norm("محـمد") == norm("محمد")
    assert norm("Rock & Roll All Nite") == norm("Rock and Roll All Nite")


def test_soundtrack_album_is_not_a_version():
    want = A("Lose Yourself", "Eminem", 322, None)
    cand = A("Lose Yourself", "Eminem", 321, "8 Mile (Music From And Inspired By The Motion Picture)")
    assert score(want, cand) >= 0.9
    assert score(T("Song", "A", 200), T("Song (Music From The Motion Picture)", "A", 200)) >= 0.9
    assert score(T("Song", "A", 200), T("Song (With Instruments)", "A", 200)) == 0
