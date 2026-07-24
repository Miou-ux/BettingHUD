#!/usr/bin/env python3
"""Build a live-like 2026 backtest CSV (no-leak, reliability, favorite-frame).

Differs from ``backtest_2026.py`` default export:
  - One row per match with bookmaker odds (not only value-bet sides).
  - Randomized P1/P2 fixture order (deterministic per match) — removes winner-as-P1 bias.
  - Favorite metrics pre-computed (aligned with ``collect_top5_proba_picks``).
  - ``data_reliability_score`` on every row (player1/player2 oriented).

Training: identical to prod / ``backtest_2026.py`` (data strictly before 2026-01-01).

Usage:
  python scripts/build_backtest_2026_live_replay.py
  python scripts/build_backtest_2026_live_replay.py --out data/backtest_2026_live_replay.csv
  python scripts/build_backtest_2026_live_replay.py --also-write-legacy
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# backtest_2026 re-wraps stdout on import (Windows UTF-8); restore for SSH/non-TTY runs.
_STDOUT = sys.stdout
from scripts.backtest_2026 import (  # noqa: E402
    _surname_initial,
    build_dataset_with_identity,
    load_bookmaker_odds_year,
    train_no_leak,
)
from scripts.bookmaker_odds_loader import (  # noqa: E402
    preflight_odds_coverage,
    resolve_bookmaker_odds_for_year,
)
from scripts.enrich_backtest_csv_reliability import (  # noqa: E402
    HistoricalRankLookup,
    _norm,
    _prediction_contradicts_rank_points,
)
from scripts.match_rank_quality import match_data_reliability_score  # noqa: E402
from scripts.ml_model import TennisMLModel, resolve_segment_brier_score  # noqa: E402

if getattr(sys.stdout, "closed", False):
    sys.stdout = _STDOUT

REPLAY_SOURCE = "live_replay_v1"
DEFAULT_OUT = os.path.join(ROOT, "data", "backtest_2026_live_replay.csv")
DEFAULT_CUTOFF = "2026-01-01"
DEFAULT_SEED = 42


def _stable_swap(*, seed: int, date_iso: str, winner: str, loser: str) -> bool:
    """True → loser is player1 (randomized fixture, reproducible)."""
    key = f"{seed}|{date_iso}|{winner}|{loser}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 2) == 1


def _batch_predict_p1(ml: TennisMLModel, Xfeat: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """Segment-blended P(p1 wins) for each row (same logic as backtest_2026.run_backtest)."""
    rc = list(TennisMLModel.ROUTING_COLS_BO5)
    routing_bt = Xfeat.loc[:, rc] if all(c in Xfeat.columns for c in rc) else None
    global_proba = (
        ml.predict_proba_calibrated_routed(Xfeat, routing=routing_bt)
        if hasattr(ml, "predict_proba_calibrated_routed")
        else ml.model.predict_proba(Xfeat)[:, 1]
    )
    seg_proba_cache: dict[str, np.ndarray] = {}
    for seg_key, seg_model in ml.model_segments.items():
        seg_proba_cache[seg_key] = seg_model.predict_proba(Xfeat)[:, 1]

    surf_code_arr = Xfeat["surface_encoded"].values
    lvl_code_arr = Xfeat["tournament_level_encoded"].values
    tour_arr = Xfeat["tour_encoded"].values
    surf_label_map = {0.0: "Hard", 1.0: "Clay", 2.0: "Grass"}
    lvl_label_map = {3.0: "G", 2.0: "M", 1.0: "A"}
    blend_w = float(getattr(ml, "segment_blend_weight", 0.7))
    seg_sizes = getattr(ml, "segment_train_sizes", {})

    p1_probs = np.empty(len(Xfeat))
    seg_used: list[str] = []
    for i in range(len(Xfeat)):
        sl = surf_label_map.get(float(surf_code_arr[i]))
        ll = lvl_label_map.get(float(lvl_code_arr[i]))
        seg_key = f"{sl}_{ll}" if sl and ll else None
        if float(tour_arr[i]) == 1.0 and sl and ll:
            wta_key = f"WTA_{sl}_{ll}"
            if wta_key in seg_proba_cache:
                seg_key = wta_key
        if seg_key in seg_proba_cache:
            n_seg = int(seg_sizes.get(seg_key, 1500))
            volume_factor = max(0.30, min(1.0, n_seg / 1500.0))
            bw = blend_w * volume_factor
            p1_probs[i] = bw * seg_proba_cache[seg_key][i] + (1.0 - bw) * global_proba[i]
            seg_used.append(seg_key)
        else:
            p1_probs[i] = global_proba[i]
            seg_used.append("Global")
    return p1_probs, seg_used


def _lookup_oriented_stats(
    lookup: HistoricalRankLookup,
    *,
    tour: str,
    player1: str,
    player2: str,
    winner: str,
    loser: str,
    date_iso: str,
) -> tuple[dict, dict, str | None, str | None]:
    """Map historical winner/loser ranks onto player1/player2."""
    hist = lookup.lookup(tour=tour, winner=winner, loser=loser, date_iso=date_iso)
    if not hist:
        empty = {"rank": 0, "pts": 0, "stats_source": None, "stats_reference_date": date_iso}
        return empty, empty, None, None

    w_norm = _norm(winner)
    l_norm = _norm(loser)
    p1_norm = _norm(player1)
    p2_norm = _norm(player2)

    def _stats_for(name_norm: str) -> dict:
        if name_norm == w_norm:
            return {
                "rank": hist["winner_rank"],
                "pts": hist["winner_rank_points"],
                "stats_source": hist["stats_source"],
                "stats_reference_date": hist["date"],
            }
        if name_norm == l_norm:
            return {
                "rank": hist["loser_rank"],
                "pts": hist["loser_rank_points"],
                "stats_source": hist["stats_source"],
                "stats_reference_date": hist["date"],
            }
        return {"rank": 0, "pts": 0, "stats_source": None, "stats_reference_date": date_iso}

    p1_stats = _stats_for(p1_norm)
    p2_stats = _stats_for(p2_norm)
    tour_u = str(tour or "ATP").upper()
    p1_id = f"{tour_u}::hist::{p1_norm}" if p1_stats.get("stats_source") else None
    p2_id = f"{tour_u}::hist::{p2_norm}" if p2_stats.get("stats_source") else None
    return p1_stats, p2_stats, p1_id, p2_id


def _reliability_for_row(
    lookup: HistoricalRankLookup,
    *,
    tour: str,
    date_iso: str,
    player1: str,
    player2: str,
    winner: str,
    loser: str,
    p1_prob: float,
    p_model_fav: float,
    odd_fav: float,
) -> tuple[int, str]:
    p1_stats, p2_stats, p1_id, p2_id = _lookup_oriented_stats(
        lookup,
        tour=tour,
        player1=player1,
        player2=player2,
        winner=winner,
        loser=loser,
        date_iso=date_iso,
    )
    p_implicit_fav = 1.0 / float(odd_fav) if odd_fav > 1.0 else 0.5
    gap_pp = abs(float(p_model_fav) - p_implicit_fav) * 100.0
    match = {
        "date": date_iso,
        "player1": player1,
        "player2": player2,
        "p1_player_id": p1_id,
        "p2_player_id": p2_id,
        "p1_stats": p1_stats,
        "p2_stats": p2_stats,
        "book_gap_pp": gap_pp,
        "snapshot_tier": "full",
        "unreliable": _prediction_contradicts_rank_points(
            float(p1_prob),
            int(p1_stats.get("rank") or 0),
            int(p2_stats.get("rank") or 0),
        ),
    }
    score, flags = match_data_reliability_score(match)
    return score, "|".join(flags) if flags else ""


def build_live_replay_dataframe(
    *,
    year: int = 2026,
    cutoff: str = DEFAULT_CUTOFF,
    orient_seed: int = DEFAULT_SEED,
    data_lag_days: int = 0,
    db_path: str | None = None,
    auto_download: bool = True,
    use_legacy_csv: bool = True,
    min_odds_keys: int = 500,
) -> pd.DataFrame:
    ml = TennisMLModel()
    assert "xgb_model_tml_v47.pkl" in str(ml.model_path).replace("\\", "/")
    ml.elo_decay_tau_days = 365.0
    ml.surface_blend_n0 = 30.0
    ml.segment_blend_weight = 0.7

    dataset, df1, df2, identity = build_dataset_with_identity(ml, data_lag_days=int(data_lag_days))
    train_no_leak(ml, dataset, pd.Timestamp(cutoff))

    year_start = pd.Timestamp(f"{year}-01-01")
    year_end = pd.Timestamp(f"{year + 1}-01-01")
    test_mask = (identity["tourney_date"] >= year_start) & (identity["tourney_date"] < year_end)
    df1_test = df1.loc[test_mask].copy().reset_index(drop=True)
    df2_test = df2.loc[test_mask].copy().reset_index(drop=True)
    id_test = identity.loc[test_mask].copy().reset_index(drop=True)

    feat_complete = df1_test[ml.features].notna().all(axis=1)
    df1_test = df1_test.loc[feat_complete].reset_index(drop=True)
    df2_test = df2_test.loc[feat_complete].reset_index(drop=True)
    id_test = id_test.loc[feat_complete].reset_index(drop=True)

    print(f"Matchs {year} (features complètes): {len(df1_test)}")

    odds_atp, odds_wta = resolve_bookmaker_odds_for_year(
        year,
        load_bookmaker_odds_year,
        root=ROOT,
        auto_download=auto_download,
        use_legacy_csv=use_legacy_csv,
    )
    preflight_odds_coverage(year, odds_atp, odds_wta, min_keys=min_odds_keys)

    p1_winner_probs, seg_w = _batch_predict_p1(ml, df1_test[ml.features])
    p1_loser_probs, seg_l = _batch_predict_p1(ml, df2_test[ml.features])

    db = db_path or os.path.join(ROOT, "data", "bettinghud.db")
    lookup = HistoricalRankLookup(db)

    rows: list[dict] = []
    n_no_odds = 0
    n_swapped = 0

    for i in range(len(id_test)):
        row = id_test.iloc[i]
        winner = str(row["winner_name"])
        loser = str(row["loser_name"])
        tour = str(row["tour"]).upper()
        tourney_start_iso = pd.Timestamp(row["tourney_date"]).strftime("%Y-%m-%d")
        odds_book = odds_atp if tour == "ATP" else odds_wta

        triplet = None
        odds_date_iso: str | None = None
        deltas = list(range(0, 15)) + [-1, -2, -3]
        wkey = _surname_initial(winner)
        lkey = _surname_initial(loser)
        for delta in deltas:
            d = (pd.Timestamp(row["tourney_date"]) + pd.Timedelta(days=delta)).strftime("%Y-%m-%d")
            triplet = odds_book.get((d, wkey, lkey))
            if triplet:
                odds_date_iso = d
                break
            triplet_swap = odds_book.get((d, lkey, wkey))
            if triplet_swap:
                triplet = (triplet_swap[1], triplet_swap[0], triplet_swap[2])
                odds_date_iso = d
                break
        if triplet is None:
            n_no_odds += 1
            continue

        odd_winner, odd_loser, odds_source = triplet
        bet_calendar_date = odds_date_iso or tourney_start_iso

        swap = _stable_swap(
            seed=int(orient_seed),
            date_iso=bet_calendar_date,
            winner=winner,
            loser=loser,
        )
        if swap:
            n_swapped += 1
            player1, player2 = loser, winner
            odd_p1, odd_p2 = float(odd_loser), float(odd_winner)
            p1_prob = float(p1_loser_probs[i])
            seg_key = seg_l[i]
        else:
            player1, player2 = winner, loser
            odd_p1, odd_p2 = float(odd_winner), float(odd_loser)
            p1_prob = float(p1_winner_probs[i])
            seg_key = seg_w[i]

        p2_prob = 1.0 - p1_prob
        fav_side = 1 if p1_prob >= p2_prob else 2
        p_model_fav = max(p1_prob, p2_prob)
        odd_fav = odd_p1 if fav_side == 1 else odd_p2
        fav_player = player1 if fav_side == 1 else player2
        underdog_player = player2 if fav_side == 1 else player1
        ev_fav = p_model_fav * float(odd_fav) - 1.0
        p_implicit_fav = 1.0 / float(odd_fav)
        book_gap_pp = abs(p_model_fav - p_implicit_fav) * 100.0

        winner_norm = _norm(winner)
        fav_won = _norm(fav_player) == winner_norm
        seg_brier = float(resolve_segment_brier_score(ml, seg_key if seg_key != "Global" else ""))

        rel_score, rel_flags = _reliability_for_row(
            lookup,
            tour=tour,
            date_iso=bet_calendar_date,
            player1=player1,
            player2=player2,
            winner=winner,
            loser=loser,
            p1_prob=p1_prob,
            p_model_fav=p_model_fav,
            odd_fav=float(odd_fav),
        )
        confidence = abs(p_model_fav - 0.5) * 2.0

        rows.append(
            {
                "replay_source": REPLAY_SOURCE,
                "orient_swapped": swap,
                "tour": tour,
                "date": bet_calendar_date,
                "calendar_date": bet_calendar_date,
                "tournament": row.get("tourney_name", ""),
                "surface": row.get("surface", ""),
                "tourney_level": row.get("tourney_level", ""),
                "player1": player1,
                "player2": player2,
                "winner_name": winner,
                "loser_name": loser,
                "match_name": f"{player1} vs {player2}",
                "p1_prob": p1_prob,
                "global_p1_prob": p1_prob,
                "fav_side": fav_side,
                "fav_player": fav_player,
                "underdog_player": underdog_player,
                "p_model_fav": p_model_fav,
                "odd_fav": float(odd_fav),
                "odd_p1": odd_p1,
                "odd_p2": odd_p2,
                "ev_fav_pct": ev_fav * 100.0,
                "ev": ev_fav,
                "p_model": p_model_fav,
                "odd": float(odd_fav),
                "p_implied": p_implicit_fav,
                "odds_source": odds_source,
                "book_gap_pp": book_gap_pp,
                "confidence": confidence,
                "fav_won": fav_won,
                "won": fav_won,
                "settled": True,
                "segment_key": seg_key,
                "segment_brier": seg_brier,
                "data_reliability_score": rel_score,
                "data_reliability_flags": rel_flags,
            }
        )

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(
            ["calendar_date", "p_model_fav"],
            ascending=[True, False],
            kind="mergesort",
        ).reset_index(drop=True)

    print(
        f"Export: {len(out)} matchs | sans cotes: {n_no_odds} | "
        f"P1=loser (swap): {n_swapped}/{len(out)} ({100*n_swapped/max(1,len(out)):.1f}%)"
    )
    if not out.empty:
        ge80 = int((out["data_reliability_score"] >= 80).sum())
        print(
            f"Fiabilité: moy={out['data_reliability_score'].mean():.1f} | "
            f"≥80: {ge80}/{len(out)} ({100*ge80/len(out):.1f}%)"
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Build live-like 2026 backtest CSV")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--cutoff", default="", help="Train cutoff (default: YYYY-01-01 for --year)")
    ap.add_argument("--orient-seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--data-lag-days", type=int, default=0)
    ap.add_argument("--out", default="", help="Output CSV (default: data/backtest_YEAR_live_replay.csv)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument("--no-download", action="store_true", help="Ne pas télécharger les xlsx manquants")
    ap.add_argument("--no-legacy-fallback", action="store_true", help="Pas de fallback backtest_YEAR_bets.csv")
    ap.add_argument("--min-odds-keys", type=int, default=500, help="Preflight min clés cotes ATP+WTA")
    ap.add_argument(
        "--also-write-legacy",
        action="store_true",
        help="Also write data/backtest_YEAR_bets.csv (subset: ev>=15%% pool rows)",
    )
    args = ap.parse_args()
    year = int(args.year)
    cutoff = str(args.cutoff).strip() or f"{year}-01-01"
    out_path = str(args.out).strip() or os.path.join(ROOT, "data", f"backtest_{year}_live_replay.csv")

    print("=" * 78, flush=True)
    print(f"BUILD backtest {year} live-replay CSV", flush=True)
    print(f"  cutoff train: {cutoff}", flush=True)
    print(f"  orient seed:    {args.orient_seed}", flush=True)
    print(f"  output:         {out_path}", flush=True)
    print("=" * 78, flush=True)

    df = build_live_replay_dataframe(
        year=year,
        cutoff=cutoff,
        orient_seed=int(args.orient_seed),
        data_lag_days=int(args.data_lag_days),
        db_path=str(args.db),
        auto_download=not args.no_download,
        use_legacy_csv=not args.no_legacy_fallback,
        min_odds_keys=int(args.min_odds_keys),
    )
    if df.empty:
        print("[!] Aucune ligne exportée — vérifier cotes / preflight.", flush=True)
        return 1

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nSaved: {out_path} ({len(df)} rows)")

    if args.also_write_legacy:
        legacy = df[
            (df["p_model_fav"].astype(float) > 0.60)
            & (df["ev"].astype(float) >= 0.15)
            & (df["ev"].astype(float) <= 1.0)
        ].copy()
        legacy_path = os.path.join(ROOT, "data", f"backtest_{int(args.year)}_bets.csv")
        legacy.to_csv(legacy_path, index=False)
        print(f"Legacy subset: {legacy_path} ({len(legacy)} rows, ev>=15% & proba>60%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
