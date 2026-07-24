"""
Simulation 2026 sans leak : top 10 probas / jour, EV >= 15 %, filtres backtest dashboard,
gestion BR = Kelly 0,85 × facteur Brier segment (÷0,25), plafond 15 % liquidité intraday.

Étapes :
  1. CSV no-leak via ``backtest_2026.py`` (ou fichier existant).
  2. Filtres ``load_and_filter_bets_csv`` (circuit, niveaux G/M/A, exclusions, EV min).
  3. Top N par jour sur ``p_model`` (proba du côté parié).
  4. ``simulate_sequential_intraday`` (même logique que live / kelly_ab_analysis).

Usage :
    python scripts/simulate_top10_proba_2026.py
    python scripts/simulate_top10_proba_2026.py --skip-backtest --csv data/backtest_2026_bets.csv
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import (  # noqa: E402
    load_and_filter_bets_csv,
    resolve_backtest_csv,
    simulate_sequential_intraday,
)
from scripts.kelly_policy import KELLY_BASE_FRAC as KELLY_BASE  # noqa: E402
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key  # noqa: E402

# Filtres alignés onglet Backtest Kelly (dashboard.py, défauts)
DEFAULT_TOURNEY_LEVELS = ["G", "M", "A"]
DEFAULT_EXTRA_EXCLUDE = (
    "olympics,davis cup,billie jean king cup,united cup,atp finals,wta finals,laver cup"
)
DEFAULT_TOP_N = 10
DEFAULT_EV_MIN_PCT = 15.0
DEFAULT_EV_MAX_PCT = 100.0
DEFAULT_BR0 = 100.0
MAX_STAKE_PCT = 15.0


def _segment_calibration_key(row: pd.Series, ml: TennisMLModel) -> str:
    return resolve_match_brier_segment_key(
        ml,
        tour=row.get("tour"),
        surface=row.get("surface"),
        tournament=row.get("tournament"),
        tourney_level=row.get("tourney_level"),
    )


def select_top_proba_per_day(df: pd.DataFrame, *, top_n: int = 10) -> pd.DataFrame:
    """Garde au plus ``top_n`` paris par jour calendaire (meilleure ``p_model`` en premier)."""
    if df.empty or top_n <= 0:
        return df.iloc[0:0].copy()
    work = df.copy()
    work["_ord"] = np.arange(len(work), dtype=np.int64)
    work = work.sort_values(
        ["date", "p_model", "_ord"],
        ascending=[True, False, True],
        kind="mergesort",
    )
    work["_day_rank"] = work.groupby("date", sort=False).cumcount() + 1
    out = work.loc[work["_day_rank"] <= int(top_n)].drop(columns=["_ord", "_day_rank"])
    return out.reset_index(drop=True)


def flat_stake_metrics(df: pd.DataFrame, *, br0: float = 100.0) -> dict:
    """Référence 1 unité fixe par pari (ordre jour = DataFrame)."""
    if df.empty:
        return {"n_bets": 0, "roi_pct": 0.0, "hit_pct": 0.0, "net_units": 0.0, "bankroll_final": br0}
    rets = np.where(df["won"].astype(bool), df["odd"].astype(float) - 1.0, -1.0)
    n = len(rets)
    net = float(rets.sum())
    wins = int((rets > 0).sum())
    bank = br0 + net
    cum = br0 + np.cumsum(rets)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / np.where(peak > 0, peak, 1.0)
    return {
        "n_bets": n,
        "roi_pct": net / n * 100.0,
        "hit_pct": wins / n * 100.0,
        "net_units": net,
        "bankroll_final": float(bank),
        "max_drawdown_pct": float(abs(dd.min()) * 100.0) if len(dd) else 0.0,
        "brier": float(((df["p_model"].astype(float) - df["won"].astype(float)) ** 2).mean()),
    }


def _ensure_backtest_csv(year: int, csv_path: str, *, ev_min: float) -> str:
    if os.path.isfile(csv_path):
        return csv_path
    script = os.path.join(ROOT, "scripts", "backtest_2026.py")
    cmd = [
        sys.executable,
        script,
        "--year",
        str(year),
        "--ev-min",
        str(ev_min),
        "--ev-max",
        "1.0",
        "--out",
        csv_path,
    ]
    print(f"[backtest] Génération no-leak : {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ROOT, check=True)
    return csv_path


def run_year_projection(
    year: int,
    *,
    csv_path: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    ev_min_pct: float = DEFAULT_EV_MIN_PCT,
    ev_max_pct: float | None = DEFAULT_EV_MAX_PCT,
    br_start: float = DEFAULT_BR0,
    levels: list[str] | None = None,
    extra_tokens: list[str] | None = None,
    ml: TennisMLModel | None = None,
) -> dict:
    """Exécute la projection pour une année ; retourne métriques agrégées."""
    levels = levels or list(DEFAULT_TOURNEY_LEVELS)
    extra_tokens = extra_tokens or [t.strip() for t in DEFAULT_EXTRA_EXCLUDE.split(",") if t.strip()]
    path = csv_path or resolve_backtest_csv(ROOT, year) or os.path.join(
        ROOT, "data", f"backtest_{year}_bets.csv"
    )
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV introuvable pour {year}: {path}")

    n_raw = len(pd.read_csv(path))
    df_f = load_and_filter_bets_csv(
        path,
        year=year,
        ev_min_pct=float(ev_min_pct),
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=levels,
        extra_tournament_tokens=extra_tokens,
    )
    if ev_max_pct is not None:
        df_f = df_f[df_f["ev"].astype(float) <= float(ev_max_pct) / 100.0].reset_index(drop=True)
    n_filtered = len(df_f)
    df_sel = select_top_proba_per_day(df_f, top_n=top_n)
    n_sel = len(df_sel)
    n_days = int(df_sel["date"].nunique()) if not df_sel.empty else 0

    if ml is None:
        ml = TennisMLModel()
        if hasattr(ml, "_load_bundle_if_needed"):
            ml._load_bundle_if_needed()
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))

    if not df_sel.empty:
        df_sel = df_sel.copy()
        df_sel["segment_calibration_key"] = df_sel.apply(
            lambda r: _segment_calibration_key(r, ml), axis=1
        )

    flat = flat_stake_metrics(df_sel, br0=float(br_start))
    kelly = simulate_sequential_intraday(
        df_sel,
        bankroll_start=float(br_start),
        kelly_multiplier=1.0,
        max_stake_pct=MAX_STAKE_PCT,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=KELLY_BASE,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        return_history=True,
        stake_cap_basis="liquid",
        track_stake_cap_hits=True,
    )

    atp = wta = None
    if not df_sel.empty and "tour" in df_sel.columns:
        if (df_sel["tour"] == "ATP").any():
            atp = flat_stake_metrics(df_sel[df_sel["tour"] == "ATP"], br0=0.0)
        if (df_sel["tour"] == "WTA").any():
            wta = flat_stake_metrics(df_sel[df_sel["tour"] == "WTA"], br0=0.0)

    return {
        "year": year,
        "csv_path": path,
        "n_raw": n_raw,
        "n_filtered": n_filtered,
        "n_bets": n_sel,
        "n_days": n_days,
        "flat": flat,
        "kelly": kelly,
        "atp": atp,
        "wta": wta,
    }


def compare_years(
    years: list[int],
    *,
    ev_min_pct: float = DEFAULT_EV_MIN_PCT,
    ev_max_pct: float | None = 100.0,
    top_n: int = DEFAULT_TOP_N,
    br_start: float = DEFAULT_BR0,
) -> pd.DataFrame:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    rows = []
    for y in years:
        try:
            r = run_year_projection(
                y,
                top_n=top_n,
                ev_min_pct=ev_min_pct,
                ev_max_pct=ev_max_pct,
                br_start=br_start,
                ml=ml,
            )
        except FileNotFoundError as e:
            print(f"[WARN] {e}")
            continue
        f, k = r["flat"], r["kelly"]
        rows.append(
            {
                "Année": y,
                "Paris": r["n_bets"],
                "Jours": r["n_days"],
                "Hit %": f"{f['hit_pct']:.1f}",
                "ROI 1u %": f"{f['roi_pct']:+.2f}",
                "Profit 1u": f"{f['net_units']:+.1f}",
                "BR 1u fin": f"{f['bankroll_final']:.1f}",
                "DD 1u %": f"{f['max_drawdown_pct']:.1f}",
                "Brier": f"{f['brier']:.3f}",
                "ROI Kelly %": f"{k['roi_on_staked_pct']:+.2f}",
                "BR Kelly fin": f"{k['bankroll_final']:.0f}",
                "× BR": f"{k['bankroll_final']/br_start:.2f}",
                "DD Kelly %": f"{k['max_drawdown_pct']:.1f}",
                "Sharpe j.": f"{k['sharpe_daily']:.2f}",
                "PF": f"{k.get('profit_factor') or 0:.2f}",
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument(
        "--compare-years",
        type=str,
        default=None,
        help="Comparer plusieurs années (ex. 2024,2025,2026).",
    )
    ap.add_argument("--csv", type=str, default=None)
    ap.add_argument("--skip-backtest", action="store_true")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    ap.add_argument("--ev-min-pct", type=float, default=DEFAULT_EV_MIN_PCT)
    ap.add_argument(
        "--ev-max-pct",
        type=float,
        default=DEFAULT_EV_MAX_PCT,
        help="EV maximum inclus (défaut 100 %%). Passer une valeur négative pour désactiver le plafond.",
    )
    ap.add_argument("--br-start", type=float, default=DEFAULT_BR0)
    ap.add_argument(
        "--levels",
        type=str,
        default=",".join(DEFAULT_TOURNEY_LEVELS),
        help="Niveaux tournoi (G,M,A).",
    )
    ap.add_argument(
        "--exclude-tokens",
        type=str,
        default=DEFAULT_EXTRA_EXCLUDE,
        help="Sous-chaînes tournoi à exclure (colonne tournament).",
    )
    args = ap.parse_args()

    if args.compare_years:
        years = [int(y.strip()) for y in args.compare_years.split(",") if y.strip()]
        ev_max = float(args.ev_max_pct) if float(args.ev_max_pct) >= 0 else None
        print("=" * 72)
        print(
            f"COMPARAISON {years} — Top {args.top_n} probas/jour · "
            f"EV {args.ev_min_pct:.0f}–{ev_max:.0f} % · sélection p_model · no-leak"
        )
        print("=" * 72)
        tbl = compare_years(
            years,
            ev_min_pct=float(args.ev_min_pct),
            ev_max_pct=ev_max,
            top_n=int(args.top_n),
            br_start=float(args.br_start),
        )
        if tbl.empty:
            raise SystemExit("Aucune année simulée.")
        print("\n" + tbl.to_string(index=False))
        out_name = (
            "compare_top10_proba_years.csv"
            if int(args.top_n) == DEFAULT_TOP_N
            else f"compare_top{int(args.top_n)}_proba_years.csv"
        )
        out = os.path.join(ROOT, "data", "reports", out_name)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tbl.to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\nExport : {os.path.relpath(out, ROOT)}")
        print("=" * 72)
        return

    year = int(args.year)
    csv_path = args.csv or resolve_backtest_csv(ROOT, year) or os.path.join(
        ROOT, "data", f"backtest_{year}_bets.csv"
    )
    if not args.skip_backtest and not os.path.isfile(csv_path):
        _ensure_backtest_csv(year, csv_path, ev_min=float(args.ev_min_pct) / 100.0)
    if not os.path.isfile(csv_path):
        raise SystemExit(f"CSV introuvable : {csv_path}")

    levels = [x.strip().upper() for x in str(args.levels).split(",") if x.strip()]
    extra = [t.strip() for t in str(args.exclude_tokens).split(",") if t.strip()]

    df_raw = pd.read_csv(csv_path)
    n_raw = len(df_raw)

    df_f = load_and_filter_bets_csv(
        csv_path,
        year=year,
        ev_min_pct=float(args.ev_min_pct),
        allowed_tours=["ATP", "WTA"],
        allowed_tourney_levels=levels,
        extra_tournament_tokens=extra,
    )
    if args.ev_max_pct is not None and float(args.ev_max_pct) >= 0:
        if "ev" not in df_f.columns:
            raise SystemExit("Colonne 'ev' absente du CSV — impossible d'appliquer --ev-max-pct.")
        ev_max_frac = float(args.ev_max_pct) / 100.0
        df_f = df_f[df_f["ev"].astype(float) <= ev_max_frac].reset_index(drop=True)
    n_after_filters = len(df_f)

    df_sel = select_top_proba_per_day(df_f, top_n=int(args.top_n))
    n_sel = len(df_sel)
    n_days = int(df_sel["date"].nunique()) if not df_sel.empty else 0
    avg_per_day = n_sel / n_days if n_days else 0.0

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))

    if not df_sel.empty:
        df_sel = df_sel.copy()
        df_sel["segment_calibration_key"] = df_sel.apply(
            lambda r: _segment_calibration_key(r, ml), axis=1
        )

    flat = flat_stake_metrics(df_sel, br0=float(args.br_start))
    kelly = simulate_sequential_intraday(
        df_sel,
        bankroll_start=float(args.br_start),
        kelly_multiplier=1.0,
        max_stake_pct=MAX_STAKE_PCT,
        daily_stake_budget_pct=100.0,
        use_adaptive_kelly_quarter=True,
        adaptive_kelly_base_fraction=KELLY_BASE,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        return_history=True,
        stake_cap_basis="liquid",
        track_stake_cap_hits=True,
    )

    print("=" * 72)
    ev_band = f"EV {args.ev_min_pct:.0f}"
    if args.ev_max_pct is not None:
        ev_band += f"–{args.ev_max_pct:.0f}"
    ev_band += " %"
    print(f"SIMULATION {year} — Top {args.top_n} probas/jour · {ev_band} · no-leak")
    print("=" * 72)
    print(f"\nCSV source : {os.path.relpath(csv_path, ROOT)}")
    print(f"  Lignes brutes (génération backtest)     : {n_raw}")
    print(f"  Après filtres backtest (EV, G/M/A, …) : {n_after_filters}")
    print(f"  Après top {args.top_n}/jour (p_model)       : {n_sel}  ({n_days} jours, ~{avg_per_day:.1f} paris/j)")
    print("\n--- Filtres appliqués ---")
    print(f"  EV minimum          : {args.ev_min_pct:.0f} %")
    if args.ev_max_pct is not None:
        print(f"  EV maximum          : {args.ev_max_pct:.0f} %")
    print(f"  Circuits            : ATP + WTA")
    print(f"  Niveaux tournoi     : {', '.join(levels)}")
    print(f"  Exclusions tournoi  : {', '.join(extra[:5])}{'…' if len(extra) > 5 else ''}")
    print(f"  Sélection journalière : top {args.top_n} par ``p_model`` (proba côté parié)")

    print("\n--- Gestion bankroll (Kelly adaptatif) ---")
    print(f"  BR départ           : {args.br_start:.2f} €")
    print(f"  Kelly base          : {KELLY_BASE} (prod fractional Kelly)")
    print(f"  Ajustement Brier    : max(0, 1 - Brier_seg / 0.25)")
    print(f"  Plafond mise        : {MAX_STAKE_PCT:.0f} % liquidité intraday")
    print(f"  Brier global bundle : {glob_b:.4f} ({len(seg)} segments)")

    print("\n--- Résultats : mise fixe 1 u (référence) ---")
    print(f"  Paris               : {flat['n_bets']}")
    print(f"  Hit rate            : {flat['hit_pct']:.2f} %")
    print(f"  ROI (sur volume)    : {flat['roi_pct']:+.2f} %")
    print(f"  Profit net          : {flat['net_units']:+.2f} u")
    print(f"  BR finale (1u)      : {flat['bankroll_final']:.2f} u")
    print(f"  Max drawdown        : {flat['max_drawdown_pct']:.2f} %")
    print(f"  Brier (paris)       : {flat['brier']:.4f}")

    print("\n--- Résultats : Kelly 1/2 + Brier + cap 15 % ---")
    print(f"  Paris               : {kelly['n_bets']}")
    print(f"  Hit rate            : {kelly['win_rate_pct']:.2f} %")
    print(f"  Volume misé         : {kelly['total_staked_eur']:.2f} €")
    print(f"  ROI sur volume      : {kelly['roi_on_staked_pct']:+.2f} %")
    print(f"  Profit net          : {kelly['net_profit_eur']:+.2f} €")
    print(f"  BR finale           : {kelly['bankroll_final']:.2f} €  (×{kelly['bankroll_final']/args.br_start:.3f})")
    print(f"  Croissance BR       : {kelly['growth_pct']:+.2f} %")
    print(f"  Max drawdown        : {kelly['max_drawdown_pct']:.2f} %")
    print(f"  Sharpe journalier   : {kelly['sharpe_daily']:.2f}")
    print(f"  Profit factor       : {kelly.get('profit_factor')}")
    print(f"  Mises plafonnées 15%: {kelly.get('n_stake_cap_hits', 0)}")
    print(f"  Meilleur / pire jour: {kelly['best_day_eur']:+.2f} / {kelly['worst_day_eur']:+.2f} €")

    if not df_sel.empty and "tour" in df_sel.columns:
        print("\n--- Par circuit (1 u, après sélection) ---")
        for tour in sorted(df_sel["tour"].dropna().unique()):
            sub = df_sel[df_sel["tour"] == tour]
            m = flat_stake_metrics(sub, br0=0.0)
            print(f"  {tour}: n={m['n_bets']}  hit={m['hit_pct']:.1f}%  ROI={m['roi_pct']:+.2f}%")

    if not df_sel.empty:
        df_sel = df_sel.copy()
        df_sel["month"] = pd.to_datetime(df_sel["date"]).dt.strftime("%Y-%m")
        print("\n--- Par mois (1 u) ---")
        for m in sorted(df_sel["month"].unique()):
            sub = df_sel[df_sel["month"] == m]
            rets = np.where(sub["won"].astype(bool), sub["odd"].astype(float) - 1.0, -1.0)
            n = len(rets)
            hit = float((rets > 0).sum() / n * 100) if n else 0.0
            roi = float(rets.sum() / n * 100) if n else 0.0
            print(f"  {m}: n={n:>3d}  hit={hit:5.1f}%  ROI={roi:+6.2f}%")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
