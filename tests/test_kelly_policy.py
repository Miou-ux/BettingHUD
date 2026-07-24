from scripts.kelly_policy import KELLY_BASE_FRAC, KELLY_MAX_STAKE_FRAC, kelly_base_label


def test_kelly_base_prod():
    assert KELLY_BASE_FRAC == 0.85
    assert KELLY_MAX_STAKE_FRAC == 0.15


def test_kelly_base_label():
    assert kelly_base_label(0.85) == "0.85"
    assert kelly_base_label(0.65) == "0.65"
    assert kelly_base_label(0.5) == "1/2"
