"""Chargement du pick 1 Day 1 Pick (aligné CourtAlpha / one_day_one_pick)."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

PARIS_TZ = ZoneInfo("Europe/Paris")
EV_MIN_PCT = 15.0
EV_MAX_PCT = 100.0


def _passes_ev_band(row: dict[str, Any], *, ev_min_pct: float, ev_max_pct: float) -> bool:
    try:
        ev = float(row.get("ev_fav_pct"))
    except (TypeError, ValueError):
        return False
    return ev_min_pct <= ev <= ev_max_pct


def _best_circuit_candidate_between_circuits(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Meilleur candidat ATP vs WTA : proba modèle max, tie-break ATP."""
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda r: (
            float(r.get("p_model_fav") or 0.0),
            str(r.get("tour") or "").upper() == "ATP",
        ),
    )


# Alias rétrocompat (scripts diag / tests).
_best_rank1_between_circuits = _best_circuit_candidate_between_circuits


def _first_ev_eligible_per_circuit(
    rows: list[dict[str, Any]],
    *,
    ev_min_pct: float,
    ev_max_pct: float,
    row_ok: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Par circuit (ATP/WTA) : premier match classé proba ↓ avec EV dans la bande."""
    from scripts.daily_top_proba_store import DEFAULT_TOURS

    candidates: list[dict[str, Any]] = []
    for tour in DEFAULT_TOURS:
        tour_u = tour.upper()
        ranked = sorted(
            [r for r in rows if str(r.get("tour") or "").upper() == tour_u],
            key=lambda r: (int(r.get("rank") or 999), -float(r.get("p_model_fav") or 0.0)),
        )
        for row in ranked:
            if row_ok is not None and not row_ok(row):
                continue
            if _passes_ev_band(row, ev_min_pct=ev_min_pct, ev_max_pct=ev_max_pct):
                candidates.append(row)
                break
    return candidates


def load_1d1p_today_pick(
    *,
    db_path: str,
    calendar_date: str | None = None,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
) -> tuple[dict[str, Any] | None, str, int, float | None]:
    """Retourne (pick, calendar_date, pool_size_majeur_EV, snapshot_age_min)."""
    from scripts.bets_db import ensure_daily_top_proba_schema, sync_daily_top_proba_from_results
    from scripts.daily_top_proba_store import (
        DEFAULT_TOP_LIMIT,
        collect_daily_top_proba_rows,
        dedupe_top_proba_rows_by_match,
        load_today_matches_for_daily_top_proba,
        snapshot_age_min_from_meta,
    )
    from scripts.tournament_tier import is_major_atp_wta_by_name

    cal_day = calendar_date or datetime.now(PARIS_TZ).date().isoformat()

    def _major_row(row: dict[str, Any]) -> bool:
        return is_major_atp_wta_by_name(
            str(row.get("tour") or ""),
            str(row.get("tournament") or ""),
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_daily_top_proba_schema(conn)
        sync_daily_top_proba_from_results(conn)
        db_rows = conn.execute(
            """
            SELECT *
            FROM daily_top_proba_picks
            WHERE calendar_date = ?
            ORDER BY tour, rank
            """,
            (cal_day,),
        ).fetchall()
        circuit_db = _first_ev_eligible_per_circuit(
            dedupe_top_proba_rows_by_match([dict(r) for r in db_rows]),
            ev_min_pct=ev_min_pct,
            ev_max_pct=ev_max_pct,
            row_ok=_major_row,
        )
        pick = _best_circuit_candidate_between_circuits(circuit_db)
        if pick is not None:
            pick = dict(pick)
            pick["source"] = "db"
    finally:
        conn.close()

    matches, snap_meta = load_today_matches_for_daily_top_proba()
    snapshot_age_min = snapshot_age_min_from_meta(snap_meta)
    ranked_rows = collect_daily_top_proba_rows(
        matches,
        calendar_date=cal_day,
        top_limit=DEFAULT_TOP_LIMIT,
        today_only=True,
    )
    pool_n = len(
        [
            r
            for r in ranked_rows
            if _major_row(r)
            and _passes_ev_band(r, ev_min_pct=ev_min_pct, ev_max_pct=ev_max_pct)
        ]
    )

    if pick is None:
        circuit_live = _first_ev_eligible_per_circuit(
            ranked_rows,
            ev_min_pct=ev_min_pct,
            ev_max_pct=ev_max_pct,
            row_ok=_major_row,
        )
        pick = _best_circuit_candidate_between_circuits(circuit_live)
        if pick is not None:
            pick = dict(pick)
            pick["source"] = "live"

    return pick, cal_day, pool_n, snapshot_age_min


def _courtalpha_root() -> str:
    raw = (os.getenv("COURTALPHA_ROOT") or "").strip()
    if raw and os.path.isdir(raw):
        return raw
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sibling = os.path.join(os.path.dirname(root), "CourtAlpha")
    if os.path.isdir(sibling):
        return sibling
    prod = "/opt/courtalpha"
    if os.path.isdir(prod):
        return prod
    return sibling


def load_1d1p_replay(*, db_path: str) -> dict[str, Any]:
    ca = _courtalpha_root()
    if ca not in sys.path:
        sys.path.insert(0, ca)
    from api.services.one_day_one_pick import build_one_day_one_pick_replay

    return build_one_day_one_pick_replay(db_path=db_path)
