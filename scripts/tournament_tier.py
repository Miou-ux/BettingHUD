"""Classification tournoi TE : main draw (250+) vs Challenger / WTA 125 / ITF."""
from __future__ import annotations

_MINOR_TOURNAMENT_NAME_TOKENS = (
    "challenger",
    "itf",
    "utr",
    "utr pro tennis",
    "universal tennis",
    "futures",
    "future",
    "m15",
    "m25",
    "m35",
    "m50",
    "m60",
    "m80",
    "m100",
    "w15",
    "w25",
    "w35",
    "w50",
    "w60",
    "w80",
    "w100",
)

# ATP 250 / WTA 250 et au-dessus (Paris du jour, Top 5 Telegram).
MIN_MAIN_DRAW_WINNER_POINTS = 250


def _name_excludes_minor_tournament(tournament_name: str) -> bool:
    t = str(tournament_name or "").lower()
    return not any(tok in t for tok in _MINOR_TOURNAMENT_NAME_TOKENS)


def _url_excludes_minor_tournament(tournament_url: str) -> bool:
    u = str(tournament_url or "").lower()
    if not u:
        return True
    if "-challenger" in u or "/challenger/" in u:
        return False
    if "itf" in u or "/futures" in u:
        return False
    return True


def tourney_winner_points_from_match(match: dict) -> int | None:
    for key in ("tourney_winner_points", "tournament_winner_points"):
        raw = match.get(key)
        if raw is None or raw == "":
            continue
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            continue
    return None


def is_main_draw_tournament_match(match: dict) -> bool:
    """True si tournoi ATP/WTA 250+ (ou sans info tier, nom sans token mineur)."""
    c = str(match.get("category") or match.get("tour") or "").strip().upper()
    if c not in {"ATP", "WTA"}:
        return False
    if not _url_excludes_minor_tournament(str(match.get("tournament_url") or "")):
        return False
    if not _name_excludes_minor_tournament(str(match.get("tournament") or "")):
        return False
    wp = tourney_winner_points_from_match(match)
    if wp is not None and wp < MIN_MAIN_DRAW_WINNER_POINTS:
        return False
    return True


def is_challenger_tier_match(match: dict) -> bool:
    """Challenger ATP, WTA 125, ou nom/url assimilé mineur."""
    c = str(match.get("category") or match.get("tour") or "").strip().upper()
    if c == "CHALLENGER":
        t = str(match.get("tournament") or "").lower()
        return "itf" not in t and "utr" not in t
    if c not in {"ATP", "WTA"}:
        return False
    t = str(match.get("tournament") or "").lower()
    if "challenger" in t:
        return True
    u = str(match.get("tournament_url") or "").lower()
    if "-challenger" in u:
        return True
    wp = tourney_winner_points_from_match(match)
    if wp is not None and wp < MIN_MAIN_DRAW_WINNER_POINTS:
        return True
    return False


def is_major_tournament_match(match: dict) -> bool:
    """Alias Paris du jour / Top 5 : main draw uniquement."""
    return is_main_draw_tournament_match(match)


def is_major_atp_wta_by_name(category, tournament_name) -> bool:
    """Filtre nom seul (CSV ancien sans points vainqueur)."""
    c = str(category or "").strip().upper()
    if c not in {"ATP", "WTA"}:
        return False
    return _name_excludes_minor_tournament(str(tournament_name or ""))
