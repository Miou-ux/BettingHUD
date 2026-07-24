#!/usr/bin/env python3
"""Juin–Juillet 2026 — Expert Kelly vs Top5 prod (snapshots matin JSONL, sans leakage).

Usage:
  py -3 scripts/experiment_july_expert_kelly.py
  py -3 scripts/experiment_july_expert_kelly.py --from 2026-06-01 --to 2026-07-24
  py -3 scripts/experiment_july_expert_kelly.py --month 2026-07 --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PARIS = ZoneInfo("Europe/Paris")
JSONL_DIR = os.path.join(ROOT, "data", "exports", "daily_top_proba")
DB_DEFAULT = os.path.join(ROOT, "data", "bettinghud.db")

# --- Expert bettor profile (avisé, sélectif) ---
EXPERT_MAX_PICKS = 3
EXPERT_MIN_PROBA = 0.77
EXPERT_MIN_REL = 85
EXPERT_MIN_EV_PCT = 8.0
EXPERT_MAX_EV_PCT = 35.0  # évite value « fantôme » tier2 extrême
EXPERT_MAX_BOOK_GAP_PP = 25.0
EXPERT_MIN_KELLY_FRAC = 0.04  # ≥ 4 % BR théorique


@dataclass(frozen=True)
class ExpertCfg:
    max_picks: int = EXPERT_MAX_PICKS
    min_proba: float = EXPERT_MIN_PROBA
    min_rel: int = EXPERT_MIN_REL
    min_ev_pct: float = EXPERT_MIN_EV_PCT
    max_ev_pct: float = EXPERT_MAX_EV_PCT
    max_book_gap_pp: float = EXPERT_MAX_BOOK_GAP_PP
    min_kelly_frac: float = EXPERT_MIN_KELLY_FRAC
    block_book_gap_flag: bool = True
    block_rank_default: bool = True
    block_te_both: bool = True
    block_dup_prob: bool = True
    sort_mode: str = "score"  # score | proba | ev | kelly
    tier_fill: bool = False  # tier1 EV 15-35 puis complément
    rel_fallback: bool = False  # tente rel≥85 puis rel cfg si vide

    @property
    def label(self) -> str:
        return (
            f"p≥{self.min_proba*100:.0f}% rel≥{self.min_rel} "
            f"EV {self.min_ev_pct:.0f}-{self.max_ev_pct:.0f}% "
            f"gap≤{self.max_book_gap_pp:.0f} K≥{self.min_kelly_frac*100:.0f}% max{self.max_picks}"
        )


EXPERT_STRICT = ExpertCfg()
EXPERT_VOLUME = ExpertCfg(
    min_rel=80,
    max_ev_pct=45.0,
    max_book_gap_pp=30.0,
    min_kelly_frac=0.02,
    max_picks=5,
)
EXPERT_SCOUT = ExpertCfg(
    min_proba=0.75,
    min_rel=80,
    min_ev_pct=6.0,
    max_ev_pct=55.0,
    max_book_gap_pp=35.0,
    min_kelly_frac=0.02,
    max_picks=5,
    block_book_gap_flag=False,
)
EXPERT_BALANCED = ExpertCfg(
    min_rel=82,
    max_ev_pct=40.0,
    max_book_gap_pp=28.0,
    min_kelly_frac=0.03,
    max_picks=4,
)


def _load_morning_pool(jsonl_path: str, cal_day: str) -> tuple[list[dict], str | None]:
    """Retourne (pool picks, captured_ts) — capture matin sans leakage."""
    if not os.path.isfile(jsonl_path):
        return [], None
    morning: tuple[str, list[dict]] | None = None
    fallback: tuple[str, list[dict]] | None = None
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            ts = str(obj.get("captured_ts") or "")
            if not ts.startswith(cal_day):
                continue
            picks = [dict(p) for p in (obj.get("picks") or []) if isinstance(p, dict)]
            if not picks:
                continue
            hour = int(ts[11:13]) if len(ts) > 12 else 99
            if 4 <= hour <= 6:
                if morning is None or ts < morning[0]:
                    morning = (ts, picks)
            elif hour < 12 and fallback is None:
                fallback = (ts, picks)
    if morning:
        return morning[1], morning[0]
    if fallback:
        return fallback[1], fallback[0]
    return [], None


def _flags_blob(row: dict) -> str:
    return str(row.get("data_reliability_flags") or "")


def _expert_eligible(row: dict, *, duplicate_keys: set, cfg: ExpertCfg = EXPERT_STRICT) -> tuple[bool, str | None]:
    from scripts.hybrid_pick_selection import ev_fav_pct
    from scripts.match_rank_quality import (
        excluded_duplicate_model_prob_from_top5,
        passes_public_pick_gates,
    )

    if not passes_public_pick_gates(row, duplicate_keys=duplicate_keys, min_score=cfg.min_rel):
        return False, "gates"
    p = float(row.get("p_model_fav") or 0.0)
    if p < cfg.min_proba:
        return False, "proba"
    ev = ev_fav_pct(row)
    if ev < cfg.min_ev_pct or ev > cfg.max_ev_pct:
        return False, "ev_band"
    gap = row.get("book_gap_pp")
    try:
        gap_f = float(gap) if gap is not None else 0.0
    except (TypeError, ValueError):
        gap_f = 99.0
    if gap_f > cfg.max_book_gap_pp:
        return False, "book_gap"
    flags = _flags_blob(row)
    if cfg.block_book_gap_flag and "book_gap_high" in flags:
        return False, "book_gap_flag"
    if cfg.block_dup_prob and excluded_duplicate_model_prob_from_top5(row, duplicate_keys=duplicate_keys):
        return False, "dup_prob"
    if cfg.block_rank_default and any(
        x in flags
        for x in (
            "p1_default_model_stats",
            "p2_default_model_stats",
            "p1_rank_placeholder",
            "p2_rank_placeholder",
        )
    ):
        return False, "rank_default"
    if cfg.block_te_both and "p1_te_estimate" in flags and "p2_te_estimate" in flags:
        return False, "te_both"
    kelly = float(row.get("theoretical_stake_frac") or 0.0)
    if kelly < cfg.min_kelly_frac:
        return False, "kelly_min"
    return True, None


def _expert_reject_reasons(row: dict, *, duplicate_keys: set, cfg: ExpertCfg = EXPERT_STRICT) -> list[str]:
    """Toutes les raisons de rejet (pour near-miss analysis)."""
    reasons: list[str] = []
    ok, only = _expert_eligible(row, duplicate_keys=duplicate_keys, cfg=cfg)
    if ok:
        return []
    # Re-test each soft criterion individually on top of hard gates
    from scripts.hybrid_pick_selection import ev_fav_pct
    from scripts.match_rank_quality import (
        excluded_duplicate_model_prob_from_top5,
        passes_public_pick_gates,
    )

    flags = _flags_blob(row)
    if cfg.block_rank_default and any(
        x in flags
        for x in (
            "p1_default_model_stats",
            "p2_default_model_stats",
            "p1_rank_placeholder",
            "p2_rank_placeholder",
        )
    ):
        reasons.append("rank_default")
    if cfg.block_dup_prob and excluded_duplicate_model_prob_from_top5(row, duplicate_keys=duplicate_keys):
        reasons.append("dup_prob")
    if cfg.block_te_both and "p1_te_estimate" in flags and "p2_te_estimate" in flags:
        reasons.append("te_both")
    if cfg.block_book_gap_flag and "book_gap_high" in flags:
        reasons.append("book_gap_flag")
    if not passes_public_pick_gates(row, duplicate_keys=duplicate_keys, min_score=cfg.min_rel):
        reasons.append("gates")
    p = float(row.get("p_model_fav") or 0.0)
    if p < cfg.min_proba:
        reasons.append("proba")
    ev = ev_fav_pct(row)
    if ev < cfg.min_ev_pct or ev > cfg.max_ev_pct:
        reasons.append("ev_band")
    gap = row.get("book_gap_pp")
    try:
        gap_f = float(gap) if gap is not None else 0.0
    except (TypeError, ValueError):
        gap_f = 99.0
    if gap_f > cfg.max_book_gap_pp:
        reasons.append("book_gap")
    kelly = float(row.get("theoretical_stake_frac") or 0.0)
    if kelly < cfg.min_kelly_frac:
        reasons.append("kelly_min")
    return reasons or ([only] if only else ["unknown"])


def _expert_score(row: dict) -> float:
    rel = int(row.get("data_reliability_score") or 0) / 100.0
    kelly = float(row.get("theoretical_stake_frac") or 0.0)
    p = float(row.get("p_model_fav") or 0.0)
    ev = float(row.get("ev_fav_pct") or 0.0)
    gap = float(row.get("book_gap_pp") or 25.0)
    gap_pen = max(0.4, 1.0 - gap / 35.0)
    ev_bonus = min(ev, 30.0) / 30.0
    return kelly * rel * gap_pen * (0.4 + p) * (0.7 + 0.3 * ev_bonus)


def _expert_sort_key(row: dict, cfg: ExpertCfg) -> tuple:
    from scripts.hybrid_pick_selection import ev_fav_pct

    name = str(row.get("match_name") or "").lower()
    p = float(row.get("p_model_fav") or 0.0)
    ev = ev_fav_pct(row) / 100.0
    kelly = float(row.get("theoretical_stake_frac") or 0.0)
    mode = str(cfg.sort_mode or "score").lower()
    if mode == "proba":
        return (-p, -ev, name)
    if mode == "ev":
        return (-ev, -p, name)
    if mode == "kelly":
        return (-kelly, -p, name)
    return (-_expert_score(row), name)


def _in_scout_tier1(row: dict) -> bool:
    from scripts.hybrid_pick_selection import ev_fav_pct

    ev = ev_fav_pct(row)
    return 15.0 <= ev <= 35.0


def _rank_eligible(eligible: list[dict], cfg: ExpertCfg) -> list[dict]:
    if not cfg.tier_fill:
        return sorted(eligible, key=lambda r: _expert_sort_key(r, cfg))
    tier1 = [r for r in eligible if _in_scout_tier1(r)]
    tier2 = [r for r in eligible if r not in tier1]
    return sorted(tier1, key=lambda r: _expert_sort_key(r, cfg)) + sorted(
        tier2, key=lambda r: _expert_sort_key(r, cfg)
    )


def select_expert_picks(
    pool: list[dict],
    *,
    duplicate_keys: set | None = None,
    cfg: ExpertCfg = EXPERT_STRICT,
) -> list[dict]:
    from scripts.match_rank_quality import duplicate_model_prob_keys

    dup = duplicate_keys if duplicate_keys is not None else duplicate_model_prob_keys(pool)

    def _pick_at_rel(rel_min: int) -> list[dict]:
        use_cfg = replace(cfg, min_rel=rel_min) if rel_min != cfg.min_rel else cfg
        eligible: list[dict] = []
        for row in pool:
            ok, _ = _expert_eligible(row, duplicate_keys=dup, cfg=use_cfg)
            if ok:
                eligible.append(dict(row))
        ranked = _rank_eligible(eligible, use_cfg)
        out: list[dict] = []
        seen: set[str] = set()
        for row in ranked:
            mk = str(row.get("match_name") or "").strip().lower()
            if not mk or mk in seen:
                continue
            seen.add(mk)
            out.append(row)
            if len(out) >= use_cfg.max_picks:
                break
        return out

    if cfg.rel_fallback and cfg.min_rel < 85:
        at85 = _pick_at_rel(85)
        if at85:
            return at85
    return _pick_at_rel(cfg.min_rel)


def _settlement_map(db_path: str) -> dict[str, dict]:
    from scripts.bets_db import open_db, read_daily_top_proba_picks, sync_daily_top_proba_from_results

    conn = open_db(db_path)
    try:
        sync_daily_top_proba_from_results(conn)
    finally:
        conn.close()
    rows = read_daily_top_proba_picks(db_path=db_path)
    return {str(r.get("pick_key") or ""): r for r in rows if r.get("pick_key")}


def _attach_settlement(
    pick: dict,
    smap: dict[str, dict] | None = None,
    *,
    conn=None,
) -> dict:
    """Settlement live : match_results (backtest) puis fallback daily_top_proba_picks."""
    if conn is not None:
        from scripts.bets_db import attach_pick_settlement_from_results

        out = attach_pick_settlement_from_results(pick, conn)
        if out.get("settled") or str(out.get("status") or "") == "Annulé":
            return out
    out = dict(pick)
    sm = smap or {}
    pk = str(out.get("pick_key") or "")
    src = sm.get(pk) if pk else None
    if src:
        status = str(src.get("status") or "")
        out["status"] = status
        out["settled"] = status in ("Gagné", "Perdu")
        out["won"] = status == "Gagné"
        out["score_final"] = src.get("score_final")
        return out
    if out.get("settled") is True and out.get("won") is not None:
        return out
    out["status"] = out.get("status") or "En cours"
    out["settled"] = False
    out["won"] = False
    return out


def _date_range(date_from: str, date_to: str) -> list[str]:
    start = datetime.strptime(date_from[:10], "%Y-%m-%d").date()
    end = datetime.strptime(date_to[:10], "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    out: list[str] = []
    cur = start
    from datetime import timedelta

    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _july_days(month: str) -> list[str]:
    y, m = month.split("-")
    import calendar

    last = calendar.monthrange(int(y), int(m))[1]
    return [f"{month}-{d:02d}" for d in range(1, last + 1)]


def run_experiment(
    *,
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db_path: str = DB_DEFAULT,
    verbose: bool = False,
) -> dict:
    from scripts.backtest_pack12_global_2026 import BR_START, FLAT, _kelly_sim, _perf_flat, _tag
    from scripts.backtest_prod_top5_2026 import select_prod_top5_day
    from scripts.hybrid_pick_selection import ev_fav_pct
    from scripts.match_rank_quality import duplicate_model_prob_keys
    from scripts.ml_model import TennisMLModel

    if month:
        days = _july_days(month)
        period_label = month
    else:
        d0 = date_from or "2026-06-01"
        d1 = date_to or datetime.now(PARIS).date().isoformat()
        days = _date_range(d0, d1)
        period_label = f"{d0} → {d1}"

    smap = _settlement_map(db_path)
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()

    prod_all: list[dict] = []
    expert_all: list[dict] = []
    daily_rows: list[dict] = []

    for cal_day in days:
        jsonl = os.path.join(JSONL_DIR, f"{cal_day}.jsonl")
        pool, ts = _load_morning_pool(jsonl, cal_day)
        if not pool:
            if verbose:
                print(f"{cal_day}: pas de snapshot matin")
            daily_rows.append({"date": cal_day, "skip": True, "reason": "no_snapshot"})
            continue

        dup = duplicate_model_prob_keys(pool)
        prod = select_prod_top5_day(pool)
        expert = select_expert_picks(pool, duplicate_keys=dup)

        prod_s = [_attach_settlement(p, smap) for p in prod]
        expert_s = [_attach_settlement(p, smap) for p in expert]
        prod_all.extend(prod_s)
        expert_all.extend(expert_s)

        prod_settled = [p for p in prod_s if p.get("settled")]
        exp_settled = [p for p in expert_s if p.get("settled")]
        prod_w = sum(1 for p in prod_settled if p.get("won"))
        exp_w = sum(1 for p in exp_settled if p.get("won"))

        row = {
            "date": cal_day,
            "capture_ts": ts,
            "pool_n": len(pool),
            "prod_n": len(prod_s),
            "expert_n": len(expert_s),
            "prod_names": [p.get("fav_player") for p in prod_s],
            "expert_names": [p.get("fav_player") for p in expert_s],
            "prod_hit": f"{prod_w}/{len(prod_settled)}" if prod_settled else "—",
            "expert_hit": f"{exp_w}/{len(exp_settled)}" if exp_settled else "—",
        }
        daily_rows.append(row)

        if verbose:
            print(f"\n=== {cal_day} (capture {ts}) pool={len(pool)} ===")
            print(f"  PROD ({len(prod_s)}): ", end="")
            for p in prod_s:
                st = "W" if p.get("won") else ("L" if p.get("settled") else "?")
                print(
                    f"{p.get('fav_player')} p={float(p.get('p_model_fav') or 0)*100:.0f}% "
                    f"EV={ev_fav_pct(p):+.0f}% rel={p.get('data_reliability_score')} [{st}]",
                    end=" | ",
                )
            print()
            print(f"  EXPERT ({len(expert_s)}): ", end="")
            if not expert_s:
                print("(skip jour — aucun pick qualité)")
            else:
                for p in expert_s:
                    st = "W" if p.get("won") else ("L" if p.get("settled") else "?")
                    print(
                        f"{p.get('fav_player')} p={float(p.get('p_model_fav') or 0)*100:.0f}% "
                        f"EV={ev_fav_pct(p):+.0f}% K={float(p.get('theoretical_stake_frac') or 0)*100:.1f}% [{st}]",
                        end=" | ",
                    )
                print()

    _, ns_p, w_p, flat_p = _perf_flat(prod_all)
    _, ns_e, w_e, flat_e = _perf_flat(expert_all)
    k_p = _kelly_sim(_tag(prod_all, "top5"), ml)
    k_e = _kelly_sim(_tag(expert_all, "top5"), ml)

    summary: dict = {
        "period": period_label,
        "days_with_snapshot": sum(1 for r in daily_rows if not r.get("skip")),
        "prod": {
            "picks": len(prod_all),
            "settled": ns_p,
            "wins": w_p,
            "hit_pct": (w_p / ns_p * 100.0) if ns_p else 0.0,
            "flat_pnl": flat_p,
            "kelly": k_p,
        },
        "expert": {
            "picks": len(expert_all),
            "settled": ns_e,
            "wins": w_e,
            "hit_pct": (w_e / ns_e * 100.0) if ns_e else 0.0,
            "flat_pnl": flat_e,
            "kelly": k_e,
        },
        "daily": daily_rows,
    }

    # Sous-totaux mensuels
    by_month: dict[str, dict[str, list[dict]]] = defaultdict(lambda: {"prod": [], "expert": []})
    for p in prod_all:
        by_month[str(p.get("calendar_date") or "")[:7]]["prod"].append(p)
    for p in expert_all:
        by_month[str(p.get("calendar_date") or "")[:7]]["expert"].append(p)
    monthly: dict[str, dict] = {}
    for mkey in sorted(by_month):
        mp = by_month[mkey]["prod"]
        me = by_month[mkey]["expert"]
        _, ns_p, w_p, flat_p = _perf_flat(mp)
        _, ns_e, w_e, flat_e = _perf_flat(me)
        monthly[mkey] = {
            "prod": {"settled": ns_p, "wins": w_p, "hit_pct": (w_p / ns_p * 100) if ns_p else 0, "flat": flat_p, "kelly": _kelly_sim(_tag(mp, "top5"), ml)},
            "expert": {"settled": ns_e, "wins": w_e, "hit_pct": (w_e / ns_e * 100) if ns_e else 0, "flat": flat_e, "kelly": _kelly_sim(_tag(me, "top5"), ml)},
        }
    summary["monthly"] = monthly
    return summary


def _print_summary(s: dict) -> None:
    from scripts.backtest_pack12_global_2026 import BR_START

    p, e = s["prod"], s["expert"]
    kp, ke = p["kelly"], e["kelly"]
    print(f"\n{'='*60}")
    print(f"EXPERT KELLY vs TOP5 PROD — {s.get('period') or s.get('month')}")
    print(f"Jours avec snapshot matin : {s['days_with_snapshot']}")
    print(f"{'='*60}\n")

    print("Critères EXPERT (parieur avisé):")
    print(f"  p≥{EXPERT_MIN_PROBA*100:.0f}% · rel≥{EXPERT_MIN_REL} · EV {EXPERT_MIN_EV_PCT:.0f}–{EXPERT_MAX_EV_PCT:.0f}%")
    print(f"  book_gap≤{EXPERT_MAX_BOOK_GAP_PP:.0f}pp · Kelly th≥{EXPERT_MIN_KELLY_FRAC*100:.0f}% · max {EXPERT_MAX_PICKS}/j")
    print("  Exclut : dup proba, defaults rank, book_gap_high, TE×2, EV extrême\n")

    def _line(label: str, arm: dict, k: dict) -> None:
        print(f"--- {label} ---")
        print(
            f"Picks: {arm['picks']} (réglés {arm['settled']}) | Hit: {arm['hit_pct']:.1f}% | "
            f"Flat: {arm['flat_pnl']:+.0f}€"
        )
        print(
            f"Kelly: {float(k.get('net_profit_eur') or 0):+.1f}€ | "
            f"BR {float(k.get('bankroll_final') or BR_START):.1f}€ (start {BR_START:.0f}€) | "
            f"ROI vol {float(k.get('roi_on_staked_pct') or 0):+.1f}% | "
            f"MaxDD {float(k.get('max_drawdown_pct') or 0):.1f}% | "
            f"Sharpe {float(k.get('sharpe_daily') or 0):.2f} | "
            f"PF {float(k.get('profit_factor') or 0):.2f}"
        )
        print()

    _line("TOP5 PROD (hybride)", p, kp)
    _line("EXPERT KELLY", e, ke)

    delta_k = float(ke.get("net_profit_eur") or 0) - float(kp.get("net_profit_eur") or 0)
    delta_flat = e["flat_pnl"] - p["flat_pnl"]
    print(f"Δ Expert − Prod : Kelly {delta_k:+.1f}€ | Flat {delta_flat:+.0f}€")

    monthly = s.get("monthly") or {}
    if monthly:
        print(f"\n--- Par mois ---")
        for mkey in sorted(monthly):
            mp = monthly[mkey]["prod"]
            me = monthly[mkey]["expert"]
            kpm = mp["kelly"]
            kem = me["kelly"]
            print(
                f"  {mkey} | PROD hit={mp['hit_pct']:.0f}% ({mp['wins']}/{mp['settled']}) "
                f"Kelly {float(kpm.get('net_profit_eur') or 0):+.0f}€ | "
                f"EXPERT hit={me['hit_pct']:.0f}% ({me['wins']}/{me['settled']}) "
                f"Kelly {float(kem.get('net_profit_eur') or 0):+.0f}€"
            )

    # Divergence days
    diverge = []
    for d in s["daily"]:
        if d.get("skip"):
            continue
        pn = d.get("prod_names") or []
        en = d.get("expert_names") or []
        if set(pn) != set(en):
            diverge.append(d)
    if diverge:
        print(f"\nJours où Expert ≠ Prod ({len(diverge)}):")
        for d in diverge[:15]:
            print(f"  {d['date']}: prod={d['prod_names']} | expert={d['expert_names'] or ['—']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default="", help="Un mois YYYY-MM (alternative à --from/--to)")
    ap.add_argument("--from", dest="date_from", default="2026-06-01")
    ap.add_argument("--to", dest="date_to", default=datetime.now(PARIS).date().isoformat())
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.month:
        s = run_experiment(month=args.month, db_path=args.db, verbose=args.verbose)
    else:
        s = run_experiment(
            date_from=args.date_from,
            date_to=args.date_to,
            db_path=args.db,
            verbose=args.verbose,
        )
    _print_summary(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
