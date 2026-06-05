"""Picks du jour alignés onglet Live Tracker (headless, PROD / Telegram).

Par défaut (/jour Telegram) :
  - matchs **Aujourd'hui** (Europe/Paris)
  - cotes valides + rang/points sur les deux joueurs
  - matchs à venir ou démarrés récemment (grâce configurable)
  - **EV > 15 %** (seuil défaut, ``TELEGRAM_JOUR_EV_MIN_PCT``)
  - **proba modèle > 60 %** (filtre affichage Telegram, ``TELEGRAM_MIN_PROBA_PCT``)
  - tri **proba modèle** décroissante (Telegram / dashboard)

``/jourchallenger`` :
  - tournois ATP/WTA dont le nom contient « challenger »
  - EV favori **+15 % → +100 %** (défaut, env ``TELEGRAM_JOURCHALLENGER_EV_*``)
  - tri **proba modèle** décroissante

``/jourmajor`` :
  - tournois **main draw** ATP/WTA 250+ (Paris du jour / Top 5)
  - EV **+15 % → +100 %** (défaut, env ``TELEGRAM_JOURMAJOR_EV_*``)
  - tri **proba modèle** décroissante
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from scripts.bets_db import _algo_kelly_stake_frac
from scripts.daily_top_proba_store import (
    _match_favorite_metrics,
    filter_matches_for_daily_top_proba,
    format_match_time_display,
    is_today_paris_match,
    load_today_matches_for_daily_top_proba,
)
from scripts.ml_model import (
    TennisMLModel,
    resolve_match_brier_segment_key,
    resolve_segment_brier_score,
)
from scripts.priority_scoring import enrich_value_metrics
from scripts.tournament_tier import is_challenger_tier_match, is_major_tournament_match
from scripts.value_detector import ValueDetector

PARIS_TZ = ZoneInfo("Europe/Paris")
DEFAULT_EV_THRESHOLD_PCT = max(
    0.0, float(os.getenv("BETTINGHUD_LIVE_EV_THRESHOLD_PCT", "15"))
)
DEFAULT_CHALLENGER_EV_MIN_PCT = max(
    0.0, float(os.getenv("TELEGRAM_JOURCHALLENGER_EV_MIN_PCT", "15"))
)
DEFAULT_CHALLENGER_EV_MAX_PCT = max(
    0.0, float(os.getenv("TELEGRAM_JOURCHALLENGER_EV_MAX_PCT", "100"))
)
DEFAULT_MAJOR_EV_MIN_PCT = max(
    0.0, float(os.getenv("TELEGRAM_JOURMAJOR_EV_MIN_PCT", "15"))
)
DEFAULT_MAJOR_EV_MAX_PCT = max(
    0.0, float(os.getenv("TELEGRAM_JOURMAJOR_EV_MAX_PCT", "100"))
)
LIVE_STARTED_GRACE_MINUTES = max(
    0, int(os.getenv("BETTINGHUD_LIVE_STARTED_GRACE_MINUTES", "90"))
)


def _pick_model_proba_pct(row: dict) -> float:
    try:
        if row.get("p_model_pct") is not None:
            return float(row["p_model_pct"])
    except (TypeError, ValueError):
        pass
    try:
        return float(row.get("p_model_fav") or 0.0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def sort_picks_by_model_proba_desc(picks: list[dict]) -> list[dict]:
    """Tri commun Telegram : proba modèle ↓, puis EV ↓, puis nom match."""
    ordered = sorted(
        picks,
        key=lambda r: (
            -_pick_model_proba_pct(r),
            -float(r.get("ev_pct") or r.get("ev_fav_pct") or 0.0),
            str(r.get("match_name") or "").lower(),
        ),
    )
    for rank, row in enumerate(ordered, start=1):
        row["rank"] = rank
    return ordered


def _match_calendar_date(m: dict) -> datetime.date | None:
    raw = str(m.get("date") or "")[:10].strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_future_or_recent_started_match(m: dict, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(PARIS_TZ)
    time_str = str(m.get("time") or "").strip()
    if not time_str:
        return True
    if time_str.startswith("Demain"):
        return True
    try:
        match_time = datetime.strptime(time_str, "%H:%M").time()
    except ValueError:
        return True
    d = _match_calendar_date(m)
    if d is None:
        return True
    try:
        scheduled = datetime.combine(d, match_time, tzinfo=PARIS_TZ)
    except Exception:
        return True
    if scheduled >= now:
        return True
    if LIVE_STARTED_GRACE_MINUTES <= 0:
        return False
    return (now - scheduled) <= timedelta(minutes=LIVE_STARTED_GRACE_MINUTES)


def is_challenger_match(match: dict) -> bool:
    """Challenger ATP, WTA 125, etc. (nom, URL TE ou points vainqueur < 250)."""
    return is_challenger_tier_match(match)


def filter_challenger_matches(matches: list[dict]) -> list[dict]:
    return [m for m in matches if is_challenger_match(m)]


def is_major_match(match: dict) -> bool:
    """ATP/WTA 250+ (hors Challenger, WTA 125, ITF)."""
    return is_major_tournament_match(match)


def filter_major_tournament_matches(matches: list[dict]) -> list[dict]:
    return [m for m in matches if is_major_match(m)]


def filter_live_tracker_day_matches(
    matches: list[dict],
    *,
    today_only: bool = True,
) -> list[dict]:
    """Même base que Live Tracker : hygiène données + jour + matchs encore pertinents."""
    base = filter_matches_for_daily_top_proba([dict(m) for m in matches if isinstance(m, dict)])
    out: list[dict] = []
    for m in base:
        if today_only and not is_today_paris_match(m):
            continue
        if not _is_future_or_recent_started_match(m):
            continue
        out.append(m)
    return out


def _match_segment_brier(match: dict, ml: TennisMLModel) -> float:
    key = resolve_match_brier_segment_key(
        ml,
        tour=str(match.get("tour") or match.get("category") or ""),
        surface=match.get("surface"),
        tournament=match.get("tournament"),
        tourney_level=match.get("tourney_level") or match.get("category"),
    )
    return float(resolve_segment_brier_score(ml, key))


def collect_live_tracker_all_side_picks(
    matches: list[dict],
    *,
    ml: TennisMLModel | None = None,
) -> list[dict]:
    """Tous les matchs scannés du jour : côté parié si value, sinon favori modèle (sans filtre EV)."""
    if ml is None:
        ml = TennisMLModel()
        if hasattr(ml, "_load_bundle_if_needed"):
            ml._load_bundle_if_needed()

    detector = ValueDetector(min_value_threshold=-1.0)
    rows: list[dict] = []
    for idx, match in enumerate(matches):
        seg_brier = _match_segment_brier(match, ml)
        p1 = str(match.get("player1") or "").strip()
        p2 = str(match.get("player2") or "").strip()
        sides: list[tuple[int, str, str, float, float, dict]] = []
        for side, bet_on, opp, odd_key, true_key in (
            (1, p1, p2, "odd_p1", "true_odd_p1"),
            (2, p2, p1, "odd_p2", "true_odd_p2"),
        ):
            try:
                odd_book = float(match.get(odd_key) or 0)
                true_odd = float(match.get(true_key) or 0)
            except (TypeError, ValueError):
                continue
            if odd_book <= 1.0 or true_odd <= 1.0:
                continue
            val = enrich_value_metrics(
                detector.detect_value(
                    odd_book,
                    true_odd,
                    confidence=match.get("confidence"),
                ),
                segment_brier=seg_brier,
            )
            sides.append((side, bet_on, opp, odd_book, true_odd, val))

        if not sides:
            continue

        value_sides = [s for s in sides if (s[5] or {}).get("is_value")]
        if value_sides:
            chosen = value_sides
        else:
            fav_met = _match_favorite_metrics(match)
            if fav_met:
                fav_side = int(fav_met.get("fav_side") or 1)
                chosen = [s for s in sides if s[0] == fav_side] or [
                    max(sides, key=lambda s: float((s[5] or {}).get("value_pct") or -999.0))
                ]
            else:
                chosen = [max(sides, key=lambda s: float((s[5] or {}).get("value_pct") or -999.0))]
        for side, bet_on, opp, odd_book, true_odd, val in chosen:
            p_model = 1.0 / true_odd
            kelly_frac = _algo_kelly_stake_frac(p_model, odd_book, seg_brier)
            rows.append(
                {
                    "rank": 0,
                    "bet_on": bet_on,
                    "opponent": opp,
                    "fav_player": bet_on,
                    "underdog_player": opp,
                    "match_name": f"{p1} vs {p2}",
                    "tour": str(match.get("tour") or match.get("category") or "").upper(),
                    "tournament": str(match.get("tournament") or "")[:80] or None,
                    "surface": str(match.get("surface") or "")[:40] or None,
                    "match_time": format_match_time_display(match),
                    "odd_fav": odd_book,
                    "odd_book": odd_book,
                    "true_odd": true_odd,
                    "p_model_fav": p_model,
                    "p_model_pct": p_model * 100.0,
                    "ev_fav_pct": float(val.get("value_pct") or 0.0),
                    "ev_pct": float(val.get("value_pct") or 0.0),
                    "theoretical_stake_frac": kelly_frac,
                    "priority_score": float(val.get("priority_score") or 0.0),
                    "is_value": bool(val.get("is_value")),
                    "side": side,
                    "idx": idx,
                }
            )

    return sort_picks_by_model_proba_desc(rows)


def collect_live_tracker_value_picks(
    matches: list[dict],
    *,
    ev_threshold_pct: float | None = None,
    ml: TennisMLModel | None = None,
) -> list[dict]:
    """Value bets du jour (logique Live Tracker, sans filtre EV max ni favori modèle)."""
    ev_pct = DEFAULT_EV_THRESHOLD_PCT if ev_threshold_pct is None else float(ev_threshold_pct)
    detector = ValueDetector(min_value_threshold=ev_pct / 100.0)
    if ml is None:
        try:
            from scripts.telegram_runtime_cache import get_ml_model

            ml = get_ml_model()
        except Exception:
            ml = TennisMLModel()
            if hasattr(ml, "_load_bundle_if_needed"):
                ml._load_bundle_if_needed()

    value_bets: list[dict] = []
    for idx, match in enumerate(matches):
        seg_brier = _match_segment_brier(match, ml)
        p1_val = enrich_value_metrics(
            detector.detect_value(
                match.get("odd_p1"),
                match.get("true_odd_p1"),
                confidence=match.get("confidence"),
            ),
            segment_brier=seg_brier,
        )
        p2_val = enrich_value_metrics(
            detector.detect_value(
                match.get("odd_p2"),
                match.get("true_odd_p2"),
                confidence=match.get("confidence"),
            ),
            segment_brier=seg_brier,
        )
        if p1_val.get("is_value"):
            value_bets.append({"match": match, "player": 1, "val": p1_val, "idx": idx})
        if p2_val.get("is_value"):
            value_bets.append({"match": match, "player": 2, "val": p2_val, "idx": idx})

    picks: list[dict] = []
    for vb in value_bets:
        match = vb["match"]
        side = int(vb["player"])
        val = vb["val"] or {}
        p1 = str(match.get("player1") or "").strip()
        p2 = str(match.get("player2") or "").strip()
        bet_on = p1 if side == 1 else p2
        opponent = p2 if side == 1 else p1
        odd_book = float(match.get("odd_p1") if side == 1 else match.get("odd_p2") or 0)
        true_odd = float(match.get("true_odd_p1") if side == 1 else match.get("true_odd_p2") or 0)
        p_model = (1.0 / true_odd) if true_odd > 1.0 else 0.0
        p_implicit = (1.0 / odd_book) if odd_book > 1.0 else 0.0
        seg_brier = float(val.get("segment_brier") or 0.1741)
        kelly_frac = _algo_kelly_stake_frac(p_model, odd_book, seg_brier)
        picks.append(
            {
                "bet_on": bet_on,
                "opponent": opponent,
                "fav_player": bet_on,
                "underdog_player": opponent,
                "match_name": f"{p1} vs {p2}",
                "tour": str(match.get("tour") or match.get("category") or "").upper(),
                "tournament": str(match.get("tournament") or "")[:80] or None,
                "surface": str(match.get("surface") or "")[:40] or None,
                "match_time": format_match_time_display(match),
                "odd_fav": odd_book,
                "odd_book": odd_book,
                "true_odd": true_odd,
                "p_model_fav": p_model,
                "p_model_pct": p_model * 100.0,
                "p_implicit_pct": p_implicit * 100.0,
                "ev_fav_pct": float(val.get("value_pct") or 0.0),
                "ev_pct": float(val.get("value_pct") or 0.0),
                "theoretical_stake_frac": kelly_frac,
                "priority_score": float(val.get("priority_score") or 0.0),
                "sharpe_ratio": float(val.get("sharpe_ratio") or 0.0),
                "segment_brier": seg_brier,
                "confidence": match.get("confidence"),
                "side": side,
                "idx": int(vb.get("idx") or 0),
            }
        )
    return sort_picks_by_model_proba_desc(picks)


def load_live_tracker_day_picks(
    *,
    ev_threshold_pct: float | None = None,
    max_age_sec: float | None = None,
) -> tuple[list[dict], dict[str, Any], int]:
    """Charge snapshot + retourne (picks, meta, n_matchs_scannés).

    Telegram ``/jour`` : **value bets EV+** uniquement (seuil min en %, défaut 0).
  """
    try:
        from scripts.telegram_runtime_cache import get_ml_model, get_today_matches_cached

        matches, meta = get_today_matches_cached(max_age_sec=max_age_sec)
        ml = get_ml_model()
    except Exception:
        matches, meta = load_today_matches_for_daily_top_proba(max_age_sec=max_age_sec)
        ml = None
    scanned = filter_live_tracker_day_matches(matches, today_only=True)
    ev_min = 0.0 if ev_threshold_pct is None else float(ev_threshold_pct)
    picks = collect_live_tracker_value_picks(scanned, ev_threshold_pct=ev_min, ml=ml)
    picks = [
        p
        for p in picks
        if float(p.get("ev_pct") or p.get("ev_fav_pct") or 0.0) > 0.0
    ]
    return sort_picks_by_model_proba_desc(picks), meta, len(scanned)


def load_live_tracker_challenger_day_picks(
    *,
    ev_threshold_pct: float | None = None,
    ev_max_pct: float | None = None,
    max_age_sec: float | None = None,
) -> tuple[list[dict], dict[str, Any], int]:
    """Challengers du jour : value bets EV bande, tri proba modèle décroissante."""
    ev_min = (
        DEFAULT_CHALLENGER_EV_MIN_PCT
        if ev_threshold_pct is None
        else float(ev_threshold_pct)
    )
    ev_max = (
        DEFAULT_CHALLENGER_EV_MAX_PCT if ev_max_pct is None else float(ev_max_pct)
    )
    try:
        from scripts.telegram_runtime_cache import get_ml_model, get_today_matches_cached

        matches, meta = get_today_matches_cached(max_age_sec=max_age_sec)
        ml = get_ml_model()
    except Exception:
        matches, meta = load_today_matches_for_daily_top_proba(max_age_sec=max_age_sec)
        ml = None
    scanned = filter_live_tracker_day_matches(matches, today_only=True)
    challenger_pool = filter_challenger_matches(scanned)
    picks = collect_live_tracker_value_picks(
        challenger_pool, ev_threshold_pct=ev_min, ml=ml
    )
    picks = [
        p
        for p in picks
        if ev_min <= float(p.get("ev_pct") or p.get("ev_fav_pct") or 0.0) <= ev_max
    ]
    return sort_picks_by_model_proba_desc(picks), meta, len(challenger_pool)


def load_live_tracker_major_day_picks(
    *,
    ev_threshold_pct: float | None = None,
    ev_max_pct: float | None = None,
    max_age_sec: float | None = None,
) -> tuple[list[dict], dict[str, Any], int]:
    """Tournois majeurs du jour : value bets EV 15–100 %, tri proba modèle ↓."""
    ev_min = (
        DEFAULT_MAJOR_EV_MIN_PCT
        if ev_threshold_pct is None
        else float(ev_threshold_pct)
    )
    ev_max = DEFAULT_MAJOR_EV_MAX_PCT if ev_max_pct is None else float(ev_max_pct)
    try:
        from scripts.telegram_runtime_cache import get_ml_model, get_today_matches_cached

        matches, meta = get_today_matches_cached(max_age_sec=max_age_sec)
        ml = get_ml_model()
    except Exception:
        matches, meta = load_today_matches_for_daily_top_proba(max_age_sec=max_age_sec)
        ml = None
    scanned = filter_live_tracker_day_matches(matches, today_only=True)
    major_pool = filter_major_tournament_matches(scanned)
    picks = collect_live_tracker_value_picks(major_pool, ev_threshold_pct=ev_min, ml=ml)
    picks = [
        p
        for p in picks
        if ev_min <= float(p.get("ev_pct") or p.get("ev_fav_pct") or 0.0) <= ev_max
    ]
    return sort_picks_by_model_proba_desc(picks), meta, len(major_pool)
