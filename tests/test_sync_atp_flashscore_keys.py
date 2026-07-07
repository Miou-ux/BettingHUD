from scripts.sync_atp_flashscore_results import _parse_tourney_date_int


def test_parse_tourney_date_int_valid():
    assert _parse_tourney_date_int("2026-07-07") == 20260707
    assert _parse_tourney_date_int(20260707) == 20260707


def test_parse_tourney_date_int_invalid():
    assert _parse_tourney_date_int("tourney_x") is None
    assert _parse_tourney_date_int("") is None
    assert _parse_tourney_date_int(None) is None
