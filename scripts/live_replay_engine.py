"""Replay live CourtAlpha / backtests — pool matin JSONL + HYB P75+P80 + Kelly frais.

Source unique pour la perf « replay live » documentée (≥ LIVE_CUTOFF).

Priorité par jour historique :
  1. ``daily_published_picks`` (envoi Telegram réel, si archivé)
  2. Pool matin JSONL (~04–06h Paris) + complément CSV live_replay
  3. Sélection HYB P75+P80-all sur ce pool (pas l'archive top-15 intraday)

Kelly : ``simulate_sequential_intraday`` (Brier segment, cap 15 % liquidité).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pandas as pd

from scripts.backtest_pack12_global_2026 import (
    BR_START,
    LIVE_CUTOFF,
    MAX_STAKE_PCT,
    _picks_to_kelly_df,
    _tag,
)
from scripts.backtest_prod_top5_2026 import _norm_pick_row
from scripts.backtest_scout_mega_grid import _daily_pools_unlimited
from scripts.backtest_staking_sim import kelly_full_fraction, simulate_sequential_intraday
from scripts.bets_db import open_db
from scripts.experiment_july_expert_kelly import _attach_settlement, _settlement_map
from scripts.hyb_p75_p80_selection import select_hyb_p75_p80_all
from scripts.kelly_policy import KELLY_BASE_FRAC
from scripts.match_rank_quality import duplicate_model_prob_keys
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class LiveReplayMeta:
    pool_source: str
    selection: str
    kelly_frac: float


def _day_pools_live(
    ml: TennisMLModel,
    *,
    start: str = LIVE_CUTOFF,
    end: str | None = None,
) -> dict[str, tuple[list[dict], set]]:
    pools = _daily_pools_unlimited(2026, ml)
    out: dict[str, tuple[list[dict], set]] = {}
    for day, rows in sorted(pools.items()):
        if day < start:
            continue
        if end and day > end:
            continue
        norm = [_norm_pick_row(dict(r)) for r in rows]
        out[day] = (norm, duplicate_model_prob_keys(norm))
    return out


def _attach_all(picks: list[dict], *, db_path: str, smap: dict[str, dict], conn) -> list[dict]:
    out: list[dict] = []
    for pick in picks:
        row = _attach_settlement(dict(pick), smap, conn=conn)
        st = str(row.get("status") or "")
        row["void"] = st == "Annulé"
        row["settled"] = st in ("Gagné", "Perdu")
        row["won"] = st == "Gagné"
        out.append(row)
    return out


def _cap_picks(items: list, limit: int | None) -> list:
    if limit is None or int(limit) <= 0:
        return items
    return items[: int(limit)]


def _select_hyb_top5_from_pool(pool: list[dict], dup: set, *, limit: int | None = None) -> list[dict]:
    picks = select_hyb_p75_p80_all(pool, duplicate_keys=dup, limit=limit)
    out: list[dict] = []
    for rank, pick in enumerate(picks, start=1):
        row = dict(pick)
        row["rank"] = rank
        row["pool_source"] = "morning_jsonl"
        out.append(row)
    return out


def select_historical_top5_live(
    db_path: str,
    ml: TennisMLModel,
    *,
    exclude_date: str | None,
    limit: int | None = None,
    end_date: str | None = None,
    start_date: str | None = None,
) -> list[dict]:
    """Top picks du jour — historique aligné backtest live replay (HYB illimité par défaut)."""
    from scripts.published_picks_store import (
        MODE_TOP5,
        has_published_for_date,
        load_published_replay_picks,
    )

    conn = open_db(db_path)
    try:
        from scripts.portfolio_tracking_store import (
            get_tracking_config,
            load_portfolio_replay_picks,
        )

        cfg = get_tracking_config(conn, MODE_TOP5)
        if cfg:
            if start_date is None:
                start_date = cfg["start_date"]
            ledger = load_portfolio_replay_picks(
                conn, MODE_TOP5, exclude_date=exclude_date
            )
            if ledger:
                return ledger
    finally:
        conn.close()

    day_pools = _day_pools_live(ml, end=end_date)
    smap = _settlement_map(db_path)
    conn = open_db(db_path)
    picks: list[dict] = []
    try:
        for cal in sorted(day_pools):
            if exclude_date and cal >= exclude_date:
                continue
            if start_date and cal < start_date:
                continue
            if has_published_for_date(conn, cal, MODE_TOP5):
                pub = load_published_replay_picks(db_path, mode=MODE_TOP5, calendar_date=cal)
                day_picks = []
                for pick in _cap_picks(pub, limit):
                    row = dict(pick)
                    row["rank"] = int(row.get("publish_rank") or row.get("rank") or 0)
                    row["pool_source"] = "published"
                    day_picks.append(row)
                picks.extend(_attach_all(day_picks, db_path=db_path, smap=smap, conn=conn))
                continue
            pool, dup = day_pools[cal]
            day_picks = _select_hyb_top5_from_pool(pool, dup, limit=limit)
            picks.extend(_attach_all(day_picks, db_path=db_path, smap=smap, conn=conn))
    finally:
        conn.close()
    return picks


def select_historical_1d1p_live(
    db_path: str,
    ml: TennisMLModel,
    *,
    exclude_date: str | None,
    end_date: str | None = None,
    start_date: str | None = None,
) -> list[dict]:
    """1D1P historique : meilleure proba HYB sur pool matin (ou pick publié)."""
    from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match, matchup_players_key
    from scripts.discord_1d1p_core import select_1d1p_pick
    from scripts.published_picks_store import (
        MODE_1D1P,
        has_published_for_date,
        load_published_replay_picks,
    )
    from scripts.tournament_tier import is_major_atp_wta_by_name

    conn = open_db(db_path)
    try:
        from scripts.portfolio_tracking_store import (
            get_tracking_config,
            load_portfolio_replay_picks,
        )

        cfg = get_tracking_config(conn, MODE_1D1P)
        if cfg:
            if start_date is None:
                start_date = cfg["start_date"]
            ledger = load_portfolio_replay_picks(
                conn, MODE_1D1P, exclude_date=exclude_date
            )
            if ledger:
                return ledger
    finally:
        conn.close()

    day_pools = _day_pools_live(ml, end=end_date)
    smap = _settlement_map(db_path)
    conn = open_db(db_path)
    picks: list[dict] = []
    used_matchups: set[str] = set()
    try:
        for cal in sorted(day_pools):
            if exclude_date and cal >= exclude_date:
                continue
            if start_date and cal < start_date:
                continue

            def _major(row: dict) -> bool:
                return is_major_atp_wta_by_name(
                    str(row.get("tour") or ""),
                    str(row.get("tournament") or ""),
                )

            if has_published_for_date(conn, cal, MODE_1D1P):
                pub = load_published_replay_picks(db_path, mode=MODE_1D1P, calendar_date=cal)
                if not pub:
                    continue
                best = dict(pub[0])
                best["pool_source"] = "published"
            else:
                pool, _dup = day_pools[cal]
                day_rows = dedupe_top_proba_rows_by_match(pool)

                def _row_ok(row: dict) -> bool:
                    if not _major(row):
                        return False
                    mk = matchup_players_key(row)
                    return not mk or mk not in used_matchups

                best_pick = select_1d1p_pick(day_rows, row_ok=_row_ok)
                if best_pick is None:
                    continue
                best = dict(best_pick)
                best["pool_source"] = "morning_jsonl"

            attached = _attach_all([best], db_path=db_path, smap=smap, conn=conn)
            if not attached:
                continue
            pick = attached[0]
            picks.append(pick)
            mk = matchup_players_key(pick)
            if mk:
                used_matchups.add(mk)
    finally:
        conn.close()
    return picks


def _resolve_kelly_frac(kelly_frac: float | None) -> float:
    return float(KELLY_BASE_FRAC if kelly_frac is None else kelly_frac)


def _kelly_eligible(pick: dict) -> bool:
    st = str(pick.get("status") or "")
    return st in ("Gagné", "Perdu")


def kelly_replay_metrics(
    picks: list[dict],
    ml: TennisMLModel,
    *,
    bankroll_start: float = BR_START,
    kelly_frac: float | None = None,
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    """Enrichit les picks (PnL €/pick) + courbe + summary — Kelly séquentiel fresh."""
    kf = _resolve_kelly_frac(kelly_frac)
    settled_picks = [p for p in picks if _kelly_eligible(p)]
    tagged = _tag(settled_picks, "top5")
    df = _picks_to_kelly_df(tagged, ml)
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))

    sim = simulate_sequential_intraday(
        df,
        bankroll_start=float(bankroll_start),
        kelly_multiplier=1.0,
        max_stake_pct=MAX_STAKE_PCT,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=kf,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        stake_cap_basis="liquid",
        return_history=True,
    )

    enriched, curve = _enrich_pick_pnl_walk(
        picks,
        ml,
        bankroll_start=float(bankroll_start),
        kelly_frac=kf,
        sim_history=sim.get("history") or [],
        settled_keys={
            (str(p.get("calendar_date") or "")[:10], str(p.get("match_name") or "").lower())
            for p in settled_picks
        },
    )
    summary = _summary_from_picks(enriched, bankroll_start=float(bankroll_start), sim=sim)
    return enriched, curve, summary


def _enrich_pick_pnl_walk(
    picks: list[dict],
    ml: TennisMLModel,
    *,
    bankroll_start: float,
    kelly_frac: float,
    sim_history: list[dict],
    settled_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Attribue replay_net_profit_eur pick par pick (même ordre / stakes que le simulateur)."""
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    adapt_frac = max(0.0, float(kelly_frac))
    cap_frac = float(MAX_STAKE_PCT) / 100.0

    ordered = sorted(
        picks,
        key=lambda r: (
            str(r.get("calendar_date") or "")[:10],
            int(r.get("rank") or 99),
            -float(r.get("p_model_fav") or 0),
        ),
    )

    br = float(bankroll_start)
    by_day_liquid: dict[str, float] = {}
    by_day_b0: dict[str, float] = {}
    by_day_deploy: dict[str, float] = {}
    out: list[dict] = []
    curve: list[dict] = []
    n_cum = 0

    for row in ordered:
        enriched = dict(row)
        cal = str(row.get("calendar_date") or "")[:10]
        st = str(row.get("status") or "")
        void = "annul" in st.lower()
        settled = st in ("Gagné", "Perdu") or void
        won = st == "Gagné"

        profit_eur: float | None = None
        pk = (cal, str(row.get("match_name") or "").lower())
        in_kelly = settled_keys is None or pk in settled_keys
        if settled and not void and st in ("Gagné", "Perdu") and in_kelly:
            if cal not in by_day_b0:
                by_day_b0[cal] = br
                by_day_liquid[cal] = br
                by_day_deploy[cal] = 0.0
            b0 = by_day_b0[cal]
            liquid = by_day_liquid[cal]
            odd = float(row.get("odd_fav") or row.get("odd") or 0)
            p = float(row.get("p_model_fav") or 0)
            seg_key = resolve_match_brier_segment_key(row)
            brier_seg = float(seg.get(str(seg_key), glob_b))
            kf_raw = kelly_full_fraction(p, odd)
            kelly_adj = max(0.0, 1.0 - (brier_seg / 0.25))
            stake_frac = max(0.0, (adapt_frac * kf_raw) * kelly_adj)
            raw = liquid * stake_frac
            cap_amt = liquid * cap_frac
            if raw > cap_amt + 1e-9:
                raw = cap_amt
            deploy_left = b0 - by_day_deploy[cal]
            stake = min(raw, liquid, deploy_left)
            if stake > 0:
                pnl = stake * (odd - 1.0) if won else -stake
                profit_eur = round(pnl, 2)
                by_day_liquid[cal] = liquid - stake
                by_day_deploy[cal] += stake
                br += pnl
            else:
                # Cote très basse → Kelly = 0 : pari réglé mais 0 € engagé
                profit_eur = 0.0
        elif void:
            profit_eur = 0.0

        enriched["replay_net_profit_eur"] = profit_eur
        enriched["theoretical_stake_frac"] = enriched.get("theoretical_stake_frac")
        out.append(enriched)
        n_cum += 1

    # Courbe journalière depuis sim_history (agrégée) ou fallback par jour
    if sim_history:
        peak = float(bankroll_start)
        max_dd = 0.0
        for pt in sim_history:
            b = float(pt.get("bankroll") or bankroll_start)
            peak = max(peak, b)
            dd = ((peak - b) / peak * 100.0) if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            curve.append(
                {
                    "date": pd.Timestamp(pt["date"]).strftime("%Y-%m-%d"),
                    "bankroll": round(b, 2),
                    "daily_profit_eur": round(float(pt.get("day_pnl_eur") or 0), 2),
                    "daily_stake_eur": round(float(pt.get("day_stake_eur") or 0), 2),
                    "n_picks_cum": int(pt.get("n_bets_cum") or 0),
                    "pnl_cum_eur": round(float(pt.get("pnl_cum_eur") or 0), 2),
                    "drawdown_pct": round(dd, 2),
                    "settled": True,
                }
            )
    return out, curve


def _summary_from_picks(
    picks: list[dict],
    *,
    bankroll_start: float,
    sim: dict[str, Any],
) -> dict[str, Any]:
    n_won = n_lost = n_void = n_open = n_settled = 0
    for row in picks:
        st = str(row.get("status") or "").lower()
        if "gagn" in st:
            n_won += 1
            n_settled += 1
        elif "perdu" in st:
            n_lost += 1
            n_settled += 1
        elif "annul" in st:
            n_void += 1
            n_settled += 1
        else:
            n_open += 1

    n_decided = n_won + n_lost
    hit_pct = (n_won / n_decided * 100.0) if n_decided > 0 else 0.0
    br_final = float(sim.get("bankroll_final") or bankroll_start)
    net = br_final - bankroll_start
    growth = (net / bankroll_start * 100.0) if bankroll_start > 0 else 0.0
    total_staked = float(sim.get("total_staked_eur") or 0)
    roi = (net / total_staked * 100.0) if total_staked > 0 else 0.0

    return {
        "n_picks": len(picks),
        "n_settled": n_settled,
        "n_open": n_open,
        "n_won": n_won,
        "n_lost": n_lost,
        "n_void": n_void,
        "hit_pct": round(hit_pct, 1),
        "bankroll_start_eur": round(bankroll_start, 2),
        "bankroll_final_eur": round(br_final, 2),
        "net_profit_eur": round(net, 2),
        "growth_pct": round(growth, 1),
        "total_staked_eur": round(total_staked, 2),
        "roi_on_staked_pct": round(roi, 1),
        "max_drawdown_pct": round(float(sim.get("max_drawdown_pct") or 0), 1),
        "kelly_base_frac": _resolve_kelly_frac(None),
        "replay_mode": "live_morning_pool",
    }


def run_top5_live_replay_backtest(
    db_path: str,
    *,
    bankroll_start: float = BR_START,
    kelly_frac: float | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Backtest live replay Top5 — même chiffres que ``_hyb_live_kelly_compare``."""
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    picks = select_historical_top5_live(
        db_path,
        ml,
        exclude_date="2099-01-01",
        limit=None,
        end_date=end_date,
    )
    _, _, summary = kelly_replay_metrics(
        picks, ml, bankroll_start=bankroll_start, kelly_frac=kelly_frac
    )
    return {"picks": picks, "summary": summary}


def load_ml() -> TennisMLModel:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    return ml
