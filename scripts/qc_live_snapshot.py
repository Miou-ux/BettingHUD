#!/usr/bin/env python3
"""QC post-build snapshot : surface tournoi + cohérence inactivité vs wins_last7d."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.qc_common import QcReport

PARIS_TZ = ZoneInfo("Europe/Paris")


def _inactivity_threshold_days() -> int:
    try:
        return max(7, int(os.getenv("BETTINGHUD_QC_INACTIVITY_DAYS_THRESHOLD", "14")))
    except ValueError:
        return 14


def _check_match_surface(m: dict, report: QcReport) -> None:
    from scripts.surface_speed import resolve_tournament_surface

    tname = str(m.get("tournament") or "").strip()
    if not tname:
        return
    expected = resolve_tournament_surface(
        tname,
        tournament_url=m.get("tournament_url"),
    )
    actual = str(m.get("surface") or "").strip().title()
    if actual not in ("Hard", "Clay", "Grass", "Carpet"):
        report.add_blocking(
            "surface_missing",
            f"surface absente/invalide pour {tname!r}",
            tournament=tname,
            match=f"{m.get('player1')} vs {m.get('player2')}",
        )
        return
    if actual != expected:
        report.add_blocking(
            "surface_mismatch",
            f"{tname}: snapshot={actual} attendu={expected}",
            tournament=tname,
            actual=actual,
            expected=expected,
            match=f"{m.get('player1')} vs {m.get('player2')}",
        )


def _check_match_inactivity(m: dict, report: QcReport) -> None:
    from scripts.stats_engine import reconcile_days_with_recent_wins

    threshold = _inactivity_threshold_days()
    fs = m.get("feature_snapshot") or {}
    for side, name_key, mq_key in (
        (1, "player1", "p1_match_quality"),
        (2, "player2", "p2_match_quality"),
    ):
        days_raw = fs.get(f"p{side}_days_since_last_match")
        mq = m.get(mq_key) or {}
        try:
            w7 = int(mq.get("wins_last7d") or 0)
        except (TypeError, ValueError):
            w7 = 0
        days = reconcile_days_with_recent_wins(days_raw, w7)
        if days is None or w7 < 1:
            continue
        if days > threshold:
            pname = str(m.get(name_key) or f"P{side}")
            report.add_blocking(
                "inactivity_wins_conflict",
                f"{pname}: days_since={days} mais wins_last7d={w7}",
                player=pname,
                days=days,
                wins_last7d=w7,
                tournament=m.get("tournament"),
                match=f"{m.get('player1')} vs {m.get('player2')}",
            )


def _check_book_gap_warnings(m: dict, report: QcReport) -> None:
    from scripts.match_rank_quality import BOOK_GAP_HIGH_PP, book_gap_pp_from_match

    gap = m.get("book_gap_pp")
    if gap is None:
        gap = book_gap_pp_from_match(m)
    try:
        gap_f = float(gap) if gap is not None else None
    except (TypeError, ValueError):
        gap_f = None
    if gap_f is not None and gap_f > BOOK_GAP_HIGH_PP:
        report.add_warning(
            "book_gap_high",
            f"écart modèle/book {gap_f:.1f} pp sur {m.get('player1')} vs {m.get('player2')}",
            book_gap_pp=round(gap_f, 1),
            tournament=m.get("tournament"),
        )


def _check_model_odds_consistency(m: dict, report: QcReport) -> None:
    from scripts.match_rank_quality import (
        capped_p1_prob_from_match,
        match_model_odds_inconsistent,
    )

    if capped_p1_prob_from_match(m) is None:
        return
    if match_model_odds_inconsistent(m):
        report.add_warning(
            "model_odds_inconsistent",
            f"true_odd désynchronisé de capped_p1_prob sur {m.get('player1')} vs {m.get('player2')}",
            tournament=m.get("tournament"),
            match=f"{m.get('player1')} vs {m.get('player2')}",
        )


def run_qc_live_snapshot(matches: list[dict] | None = None) -> QcReport:
    from scripts.daily_top_proba_store import is_today_paris_match

    report = QcReport("live_snapshot")
    if matches is None:
        from scripts.live_snapshot import FULL_SNAPSHOT_PATH, load_latest_live_snapshot

        if not os.path.isfile(FULL_SNAPSHOT_PATH):
            report.add_blocking(
                "snapshot_missing",
                f"fichier snapshot absent ({FULL_SNAPSHOT_PATH})",
            )
            return report

        matches, meta = load_latest_live_snapshot(max_age_sec=24 * 3600)
        if not matches:
            report.add_blocking("snapshot_empty", "snapshot live vide")
            return report
        built = (meta or {}).get("built_at")
        if not built:
            report.add_warning("snapshot_meta", "meta built_at absente")
        else:
            try:
                max_age_min = float(os.getenv("BETTINGHUD_QC_SNAPSHOT_MAX_AGE_MIN", "120"))
            except ValueError:
                max_age_min = 120.0
            age_min = (datetime.now(PARIS_TZ).timestamp() - float(built)) / 60.0
            if age_min > max_age_min:
                report.add_warning(
                    "snapshot_stale",
                    f"snapshot âgé de {age_min:.0f} min (seuil {max_age_min:.0f})",
                    age_min=round(age_min, 1),
                )

    today = datetime.now(PARIS_TZ).date()
    checked = 0
    for m in matches:
        if not isinstance(m, dict):
            continue
        if not is_today_paris_match(m, today=today):
            continue
        checked += 1
        _check_match_surface(m, report)
        _check_match_inactivity(m, report)
        _check_book_gap_warnings(m, report)
        _check_model_odds_consistency(m, report)

    if checked == 0:
        report.add_warning("no_today_matches", "aucun match du jour dans le snapshot")
    return report


def main() -> int:
    report = run_qc_live_snapshot()
    for line in report.summary_lines():
        print(line, flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
