from scripts.date_display_eu import format_date_eu, format_datetime_cet


def test_format_date_eu_iso():
    assert format_date_eu("2029-07-20 00:00:00.000000") == "20/07/2029"
    assert format_date_eu("20260815") == "15/08/2026"
    assert format_date_eu("2026-08-14") == "14/08/2026"


def test_format_datetime_cet():
    s = format_datetime_cet("2026-08-16T03:20:08")
    assert s.endswith("(CET)")
    assert "16/08/2026" in s
