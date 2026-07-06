#!/usr/bin/env python3
"""Compare fiabilité stockée (CSV/replay) vs rescore v3 sur backtest Top 5 / 1D1P.

Le backtest prod lit les scores déjà persistés ; ce script recomputte v3 par jour
(rangs historiques + duplicate par tournoi) et mesure l'impact sur le pool et les picks.

Limites : pas de feature_snapshot TE complet sur CSV → hist_te / ref_date_stale v3
sous-estimés vs live. La détection duplicate_model_prob par tournoi est active.

Usage:
  py -3 scripts/compare_reliability_v3_backtest.py --year 2026
  py -3 scripts/compare_reliability_v3_backtest.py --year 2025 2026 --channel both
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_pack12_global_2026 import LIVE_CUTOFF, _live_rows  # noqa: E402
from scripts.backtest_prod_1d1p_2026 import select_prod_1d1p_day  # noqa: E402
from scripts.backtest_prod_top5_2026 import (  # noqa: E402
    PROD_LIMIT,
    _csv_rows_for_year,
    _norm_pick_row,
    _print_block,
    report_block,
    select_prod_top5_day,
)
from scripts.enrich_backtest_csv_reliability import (  # noqa: E402
    HistoricalRankLookup,
    _prediction_contradicts_rank_points,
)
from scripts.hybrid_pick_selection import hybrid_pool_ok  # noqa: E402
from scripts.match_rank_quality import (  # noqa: E402
    compute_match_reliability,
    duplicate_model_prob_keys,
    match_in_duplicate_model_prob_cluster,
    passes_data_reliability_filter,
)
from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.simulate_top10_proba_2026 import KELLY_BASE  # noqa: E402


def _p1_prob(row: dict) -> float:
    for key in ("p1_prob", "global_p1_prob"):
        raw = row.get(key)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    fav_side = int(row.get("fav_side") or 1)
    p_fav = float(row.get("p_model_fav") or row.get("p_model") or 0.5)
    return p_fav if fav_side == 1 else 1.0 - p_fav


def pick_row_to_match(row: dict, lookup: HistoricalRankLookup) -> dict:
    p1 = str(row.get("player1") or "").strip()
    p2 = str(row.get("player2") or "").strip()
    if not p1 or not p2:
        parts = str(row.get("match_name") or "").split(" vs ", 1)
        if len(parts) == 2:
            p1, p2 = parts[0].strip(), parts[1].strip()
    tour = str(row.get("tour") or "ATP").upper()
    date_iso = str(row.get("calendar_date") or row.get("date") or "")[:10]
    p1_prob = _p1_prob(row)
    hist = lookup.lookup(tour=tour, winner=p1, loser=p2, date_iso=date_iso)

    if hist:
        p1_stats = {
            "rank": hist["winner_rank"],
            "pts": hist["winner_rank_points"],
            "stats_source": hist["stats_source"],
            "stats_reference_date": hist["date"],
        }
        p2_stats = {
            "rank": hist["loser_rank"],
            "pts": hist["loser_rank_points"],
            "stats_source": hist["stats_source"],
            "stats_reference_date": hist["date"],
        }
        p1_id = f"{tour}::hist::{p1.lower()}"
        p2_id = f"{tour}::hist::{p2.lower()}"
    else:
        p1_stats = {"rank": 0, "pts": 0, "stats_source": None, "stats_reference_date": date_iso}
        p2_stats = {"rank": 0, "pts": 0, "stats_source": None, "stats_reference_date": date_iso}
        p1_id = None
        p2_id = None

    gap = row.get("book_gap_pp")
    try:
        gap_pp = float(gap) if gap is not None else None
    except (TypeError, ValueError):
        gap_pp = None

    match = {
        "date": date_iso,
        "tournament": row.get("tournament"),
        "tour": tour,
        "player1": p1,
        "player2": p2,
        "p1_player_id": p1_id,
        "p2_player_id": p2_id,
        "p1_stats": p1_stats,
        "p2_stats": p2_stats,
        "book_gap_pp": gap_pp,
        "snapshot_tier": "full",
        "feature_snapshot": {"capped_p1_prob": p1_prob},
    }
    match["unreliable"] = _prediction_contradicts_rank_points(
        p1_prob,
        int(p1_stats.get("rank") or 0),
        int(p2_stats.get("rank") or 0),
    )
    return match


def rescore_day_v3(day_rows: list[dict], lookup: HistoricalRankLookup) -> list[dict]:
    matches = [pick_row_to_match(r, lookup) for r in day_rows]
    dup_keys = duplicate_model_prob_keys(matches)
    out: list[dict] = []
    for row, match in zip(day_rows, matches):
        score, flags = compute_match_reliability(match, duplicate_keys=dup_keys)
        nr = dict(row)
        nr["data_reliability_score"] = score
        nr["data_reliability_flags"] = "|".join(flags) if flags else None
        nr["data_reliability_version"] = 3
        nr["duplicate_model_prob"] = match_in_duplicate_model_prob_cluster(match, dup_keys)
        nr["player1"] = match.get("player1")
        nr["player2"] = match.get("player2")
        nr["p1_player_id"] = match.get("p1_player_id")
        nr["p2_player_id"] = match.get("p2_player_id")
        nr["unreliable"] = bool(match.get("unreliable"))
        out.append(nr)
    return out


def rescore_all_v3(rows: list[dict], lookup: HistoricalRankLookup) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))
    out: list[dict] = []
    for day in sorted(by_day):
        out.extend(rescore_day_v3(by_day[day], lookup))
    return out


def _pick_key(p: dict) -> tuple[str, str]:
    return (
        str(p.get("calendar_date") or "")[:10],
        str(p.get("match_name") or "").lower(),
    )


def _overlap(a: list[dict], b: list[dict]) -> tuple[int, int, int]:
    ka = {_pick_key(p) for p in a}
    kb = {_pick_key(p) for p in b}
    return len(ka & kb), len(ka - kb), len(kb - ka)


def top5_picks_for_rows(rows: list[dict]) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))
    out: list[dict] = []
    for day in sorted(by_day):
        out.extend(select_prod_top5_day(by_day[day], limit=PROD_LIMIT))
    return out


def oned1p_picks_for_rows(rows: list[dict]) -> list[dict]:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(_norm_pick_row(r))
    out: list[dict] = []
    for day in sorted(by_day):
        p = select_prod_1d1p_day(by_day[day])
        if p is not None:
            out.append(p)
    return out


def _score_delta_stats(rows_stored: list[dict], rows_v3: list[dict]) -> dict:
    by_key = {_pick_key(r): r for r in rows_v3}
    deltas: list[int] = []
    cross_up = cross_down = changed = 0
    for s in rows_stored:
        v = by_key.get(_pick_key(s))
        if v is None:
            continue
        old = s.get("data_reliability_score")
        new = v.get("data_reliability_score")
        if old is None or new is None:
            continue
        try:
            o, n = int(old), int(new)
        except (TypeError, ValueError):
            continue
        if o != n:
            changed += 1
        deltas.append(n - o)
        if o < 80 <= n:
            cross_up += 1
        if o >= 80 > n:
            cross_down += 1
    avg = sum(deltas) / len(deltas) if deltas else 0.0
    return {
        "changed": changed,
        "total": len(deltas),
        "avg_delta": avg,
        "cross_up": cross_up,
        "cross_down": cross_down,
    }


def _pool_rel80(rows: list[dict]) -> int:
    return sum(1 for r in rows if passes_data_reliability_filter(r))


def pool_hybrid_count(rows: list[dict]) -> int:
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[str(r.get("calendar_date") or "")[:10]].append(r)
    total = 0
    for day_rows in by_day.values():
        pseudo = []
        for r in day_rows:
            pseudo.append({
                "feature_snapshot": {"capped_p1_prob": _p1_prob(r)},
                "tournament": r.get("tournament"),
                "player1": r.get("player1") or str(r.get("match_name") or "").split(" vs ")[0],
                "player2": r.get("player2") or (str(r.get("match_name") or "").split(" vs ")[1] if " vs " in str(r.get("match_name") or "") else ""),
                "p1_player_id": r.get("p1_player_id"),
                "p2_player_id": r.get("p2_player_id"),
            })
        dup = duplicate_model_prob_keys(pseudo)
        total += sum(1 for r in day_rows if hybrid_pool_ok(r, duplicate_keys=dup))
    return total


def _days_changed(stored_picks: list[dict], v3_picks: list[dict]) -> int:
    by_day_s: dict[str, set[tuple[str, str]]] = defaultdict(set)
    by_day_v: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for p in stored_picks:
        by_day_s[str(p.get("calendar_date") or "")[:10]].add(_pick_key(p))
    for p in v3_picks:
        by_day_v[str(p.get("calendar_date") or "")[:10]].add(_pick_key(p))
    days = set(by_day_s) | set(by_day_v)
    return sum(1 for d in days if by_day_s.get(d, set()) != by_day_v.get(d, set()))


def run_year(
    year: int,
    ml: TennisMLModel,
    *,
    kelly_frac: float,
    channel: str,
) -> None:
    db_path = os.path.join(ROOT, "data", "bettinghud.db")
    lookup = HistoricalRankLookup(db_path)
    csv_rows = _csv_rows_for_year(year)
    live_rows = _live_rows() if year == 2026 else []
    stored_rows = [_norm_pick_row(r) for r in csv_rows + live_rows]
    v3_rows = rescore_all_v3(stored_rows, lookup)

    stats = _score_delta_stats(stored_rows, v3_rows)
    pool_rel_stored = _pool_rel80(stored_rows)
    pool_rel_v3 = _pool_rel80(v3_rows)
    pool_hyb_stored = pool_hybrid_count(stored_rows)
    pool_hyb_v3 = pool_hybrid_count(v3_rows)

    print(f"\n{'=' * 72}")
    print(f"YEAR {year} — fiabilité stockée vs rescore v3")
    if year == 2026:
        print(f"  Pool: CSV < {LIVE_CUTOFF} ({len(csv_rows)}) + live ({len(live_rows)})")
    else:
        print(f"  Pool CSV: {len(csv_rows)} lignes")
    print(f"  Scores modifiés: {stats['changed']}/{stats['total']} (Δ moy {stats['avg_delta']:+.1f})")
    print(f"  Franchissement seuil 80: +{stats['cross_up']} entrent | -{stats['cross_down']} sortent")
    print(f"  Pool rel≥80: stocké {pool_rel_stored} → v3 {pool_rel_v3} (Δ {pool_rel_v3 - pool_rel_stored:+d})")
    print(f"  Pool hybride (P80+EV tiers+rel): stocké {pool_hyb_stored} → v3 {pool_hyb_v3} (Δ {pool_hyb_v3 - pool_hyb_stored:+d})")
    print(f"{'=' * 72}")

    if channel in ("top5", "both"):
        stored_picks = top5_picks_for_rows(stored_rows)
        v3_picks = top5_picks_for_rows(v3_rows)
        ov, only_s, only_v = _overlap(stored_picks, v3_picks)
        days_chg = _days_changed(stored_picks, v3_picks)
        print(f"\nTop 5 — overlap {ov} | only stocké {only_s} | only v3 {only_v} | jours modifiés {days_chg}")
        for label, picks in (("stocké (baseline)", stored_picks), ("rescore v3", v3_picks)):
            r = report_block(label, picks, ml, kelly_frac=kelly_frac)
            _print_block(r)

    if channel in ("1d1p", "both"):
        stored_picks = oned1p_picks_for_rows(stored_rows)
        v3_picks = oned1p_picks_for_rows(v3_rows)
        ov, only_s, only_v = _overlap(stored_picks, v3_picks)
        days_chg = _days_changed(stored_picks, v3_picks)
        print(f"\n1D1P — overlap {ov} | only stocké {only_s} | only v3 {only_v} | jours modifiés {days_chg}")
        for label, picks in (("stocké (baseline)", stored_picks), ("rescore v3", v3_picks)):
            r = report_block(label, picks, ml, kelly_frac=kelly_frac)
            _print_block(r)


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B fiabilité stockée vs v3 sur backtest prod")
    ap.add_argument("--year", type=int, nargs="+", default=[2025, 2026])
    ap.add_argument("--channel", choices=("top5", "1d1p", "both"), default="both")
    ap.add_argument("--kelly-frac", type=float, default=KELLY_BASE)
    args = ap.parse_args()

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    print("=== Comparaison fiabilité stockée vs rescore v3 ===")
    print("Règles Top5/1D1P inchangées (hybride P80, EV tiers, rel≥80).")
    print("CSV sans feature_snapshot TE → impact hist_te/ref_date sous-estimé vs live.")

    for year in sorted(set(int(y) for y in args.year)):
        run_year(year, ml, kelly_frac=float(args.kelly_frac), channel=args.channel)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
