"""Projection BR 2026 — Challengers (ATP + WTA 125), mise fixe 5 €.

Critères (alignés /jourchallenger + filtre proba demandé) :
  - tournoi challenger tier (`is_challenger_tier_match`)
  - proba modèle du côté parié >= 65 %
  - EV >= 15 %
  - mise fixe 5 € par pari qualifié

Sources de cotes :
  1. Historique TE agrégé : ``data/scraped/prematch_odds_2026*.csv`` (couverture ~mai–juin 2026).
  2. tennis-data.co.uk : ne contient pas les Challengers / WTA 125 → 0 pari sur cette source.

Résultats settle via ``match_results`` (cache scraper TE).

Usage:
    python scripts/backtest_challenger_br_2026.py
    python scripts/backtest_challenger_br_2026.py --max-matches 500   # échantillon rapide
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import read_cached_results  # noqa: E402
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key, resolve_segment_brier_score  # noqa: E402
from scripts.player_identity import canonical_name, to_lastname_initial  # noqa: E402
from scripts.stats_engine import TennisStatsEngine  # noqa: E402
from scripts.tournament_tier import is_challenger_tier_match  # noqa: E402
from scripts.value_detector import ValueDetector  # noqa: E402

STAKE_EUR = 5.0
SURFACE_GUESS = {"clay": "Clay", "grass": "Grass", "hard": "Hard", "carpet": "Carpet"}


def _guess_surface(tournament: str) -> str:
    t = str(tournament or "").lower()
    for tok, surf in SURFACE_GUESS.items():
        if tok in t:
            return surf
    return "Hard"


def _tour_from_category(category: str) -> str:
    c = str(category or "").strip().upper()
    return "WTA" if c == "WTA" else "ATP"


def _match_key(date_s: str, tournament: str, p1: str, p2: str) -> tuple:
    a = canonical_name(to_lastname_initial(p1))
    b = canonical_name(to_lastname_initial(p2))
    return (str(date_s), str(tournament or "").strip().lower(), tuple(sorted([a, b])))


def load_scraped_challenger_matches(year: int = 2026) -> pd.DataFrame:
    pattern = os.path.join(ROOT, "data", "scraped", f"prematch_odds_{year}*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        return pd.DataFrame()
    chunks = []
    usecols = ["date", "tournament", "category", "player1", "player2", "odd_p1", "odd_p2", "scraped_at"]
    for path in files:
        try:
            df = pd.read_csv(path, usecols=lambda c: c in usecols)
        except Exception:
            df = pd.read_csv(path)
        df["src_file"] = os.path.basename(path)
        chunks.append(df)
    raw = pd.concat(chunks, ignore_index=True)
    raw["odd_p1"] = pd.to_numeric(raw["odd_p1"], errors="coerce")
    raw["odd_p2"] = pd.to_numeric(raw["odd_p2"], errors="coerce")
    raw = raw[(raw["odd_p1"] > 1.0) & (raw["odd_p2"] > 1.0)]
    raw["is_challenger"] = raw.apply(
        lambda r: is_challenger_tier_match(
            {"category": r.get("category"), "tournament": r.get("tournament")}
        ),
        axis=1,
    )
    raw = raw[raw["is_challenger"]].copy()
    if raw.empty:
        return raw
    raw["_key"] = raw.apply(
        lambda r: _match_key(r["date"], r["tournament"], r["player1"], r["player2"]),
        axis=1,
    )
    # Médiane des cotes par match (plusieurs snapshots/jour)
    agg = (
        raw.groupby("_key", as_index=False)
        .agg(
            date=("date", "first"),
            tournament=("tournament", "first"),
            category=("category", "first"),
            player1=("player1", "first"),
            player2=("player2", "first"),
            odd_p1=("odd_p1", "median"),
            odd_p2=("odd_p2", "median"),
            n_snaps=("_key", "count"),
        )
    )
    agg["surface"] = agg["tournament"].map(_guess_surface)
    agg["tour"] = agg["category"].map(_tour_from_category)
    return agg.reset_index(drop=True)


def _lookup_winner(
    cache: dict,
    date_s: str,
    p1: str,
    p2: str,
) -> bool | None:
    """True si p1 (ordre CSV) a gagné, False si p2, None si inconnu."""
    d = str(date_s)[:10]
    a = canonical_name(to_lastname_initial(p1))
    b = canonical_name(to_lastname_initial(p2))
    day = cache.get(d) or {}
    row = day.get(f"{a}||{b}") or day.get(f"{b}||{a}")
    if not row:
        return None
    w = str(row.get("winner_canonical") or "")
    if not w:
        return None
    if w == a:
        return True
    if w == b:
        return False
    return None


def _player_stats_cached(
    engine: TennisStatsEngine,
    cache: dict,
    name: str,
    tour: str,
) -> dict:
    key = (tour, canonical_name(to_lastname_initial(name)))
    if key in cache:
        return cache[key]
    st = engine.get_player_stats(None, player_name=name, tour_hint=tour)
    cache[key] = st
    return st


def score_matches(
    matches: pd.DataFrame,
    *,
    proba_min: float,
    ev_min: float,
    max_matches: int | None,
    stake_eur: float = STAKE_EUR,
) -> pd.DataFrame:
    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    engine = TennisStatsEngine(db_path=os.path.join(ROOT, "data", "bettinghud.db"))
    detector = ValueDetector(min_value_threshold=ev_min)
    stats_cache: dict = {}

    conn = sqlite3.connect(os.path.join(ROOT, "data", "bettinghud.db"))
    dates = sorted({str(d)[:10] for d in matches["date"].dropna().unique()})
    results_cache = read_cached_results(conn, dates)
    conn.close()

    work = matches if max_matches is None else matches.head(int(max_matches))
    bets = []
    n_no_result = 0
    n_scored = 0

    for i, row in work.iterrows():
        n_scored += 1
        if n_scored % 250 == 0:
            print(f"  … {n_scored}/{len(work)} matchs scorés", flush=True)

        p1 = str(row["player1"])
        p2 = str(row["player2"])
        tour = str(row["tour"])
        surface = str(row.get("surface") or "Hard")
        date_s = str(row["date"])[:10]

        won_p1 = _lookup_winner(results_cache, date_s, p1, p2)
        if won_p1 is None:
            n_no_result += 1
            continue

        p1s = _player_stats_cached(engine, stats_cache, p1, tour)
        p2s = _player_stats_cached(engine, stats_cache, p2, tour)
        try:
            preds = ml.predict_match(
                surface=surface,
                p1_name=p1,
                p2_name=p2,
                p1_rank=p1s.get("rank"),
                p2_rank=p2s.get("rank"),
                p1_age=p1s.get("age"),
                p2_age=p2s.get("age"),
                p1_ht=p1s.get("ht"),
                p2_ht=p2s.get("ht"),
                p1_pts=p1s.get("pts"),
                p2_pts=p2s.get("pts"),
                p1_id=p1s.get("player_id"),
                p2_id=p2s.get("player_id"),
                tour=tour,
                tournament_name=row.get("tournament"),
                tournament_level="C",
                match_date=date_s,
            )
        except Exception:
            continue

        p1_prob = float(preds.get("p1_win_prob") or 0.0)
        p2_prob = 1.0 - p1_prob
        odd_p1 = float(row["odd_p1"])
        odd_p2 = float(row["odd_p2"])
        true_odd_p1 = float(preds.get("p1_true_odd") or (1.0 / p1_prob if p1_prob > 0 else 0))
        true_odd_p2 = float(preds.get("p2_true_odd") or (1.0 / p2_prob if p2_prob > 0 else 0))

        seg_key = resolve_match_brier_segment_key(
            ml, tour=tour, surface=surface, tournament=row.get("tournament"), tourney_level="C"
        )
        seg_brier = float(resolve_segment_brier_score(ml, seg_key))

        for side, bet_on, opp, odd, p_model, true_odd, won in (
            (1, p1, p2, odd_p1, p1_prob, true_odd_p1, won_p1),
            (2, p2, p1, odd_p2, p2_prob, true_odd_p2, not won_p1),
        ):
            if p_model < proba_min or true_odd <= 1.0:
                continue
            val = detector.detect_value(odd, true_odd)
            ev = float(val.get("value_pct") or 0.0) / 100.0
            if ev < ev_min:
                continue
            ret = (odd - 1.0) if won else -1.0
            bets.append(
                {
                    "date": date_s,
                    "tournament": row.get("tournament"),
                    "tour": tour,
                    "surface": surface,
                    "bet_on": bet_on,
                    "opponent": opp,
                    "side": side,
                    "p_model": p_model,
                    "odd": odd,
                    "ev": ev,
                    "won": bool(won),
                    "ret": ret,
                    "profit_eur": ret * STAKE_EUR,
                }
            )

    print(f"  Matchs sans résultat en cache: {n_no_result}")
    return pd.DataFrame(bets)


def report_br(label: str, bets: pd.DataFrame, stake: float = STAKE_EUR):
    print(f"\n{'=' * 72}")
    print(label)
    print(f"{'=' * 72}")
    if bets.empty:
        print("Aucun pari qualifié avec résultat connu.")
        return
    n = len(bets)
    wins = int(bets["won"].sum())
    staked = n * stake
    profit = float(bets["profit_eur"].sum())
    roi = profit / staked if staked else 0.0
    print(f"  Paris:           {n}")
    print(f"  Gagnés:          {wins} ({100 * wins / n:.1f} %)")
    print(f"  Mises totales:   {staked:.2f} €")
    print(f"  Profit net:      {profit:+.2f} €")
    print(f"  ROI:             {roi * 100:+.2f} %")
    print(f"  Cote moyenne:    {bets['odd'].mean():.2f}")
    print(f"  Proba moyenne:   {bets['p_model'].mean() * 100:.1f} %")
    print(f"  EV moyenne:      {bets['ev'].mean() * 100:+.1f} %")
    if "tour" in bets.columns:
        for t in sorted(bets["tour"].unique()):
            sub = bets[bets["tour"] == t]
            p = float(sub["profit_eur"].sum())
            print(f"    {t}: n={len(sub)}  profit={p:+.2f} €")
    bd = bets.copy()
    bd["month"] = pd.to_datetime(bd["date"]).dt.strftime("%Y-%m")
    print("  Par mois:")
    for m in sorted(bd["month"].unique()):
        sub = bd[bd["month"] == m]
        p = float(sub["profit_eur"].sum())
        print(f"    {m}: n={len(sub)}  profit={p:+.2f} €  ROI={100 * p / (len(sub) * stake):+.1f} %")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--proba-min", type=float, default=0.65)
    parser.add_argument("--ev-min", type=float, default=0.15)
    parser.add_argument("--stake", type=float, default=STAKE_EUR)
    parser.add_argument("--max-matches", type=int, default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    stake = float(args.stake)

    print("Chargement snapshots TE Challengers…")
    matches = load_scraped_challenger_matches(year=int(args.year))
    if matches.empty:
        print("Aucun snapshot TE challenger pour", args.year)
        return
    print(f"  Matchs uniques (médiane cotes): {len(matches)}")
    print(f"  Periode: {matches['date'].min()} -> {matches['date'].max()}")

    print("\nScoring modèle v47 + filtres…")
    bets = score_matches(
        matches,
        proba_min=float(args.proba_min),
        ev_min=float(args.ev_min),
        max_matches=args.max_matches,
        stake_eur=stake,
    )
    if not bets.empty:
        bets["profit_eur"] = bets["ret"] * stake

    out = args.out or os.path.join(ROOT, "data", f"backtest_challenger_{args.year}_flat5.csv")
    if not bets.empty:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        bets.to_csv(out, index=False)
        print(f"\nExport: {out}")

    report_br(
        f"CHALLENGERS {args.year} (cotes TE) - proba >= {args.proba_min*100:.0f} %, "
        f"EV >= {args.ev_min*100:.0f} %, mise {stake:.0f} EUR",
        bets,
        stake=stake,
    )

    print("\n--- Limites ---")
    print("  - tennis-data.co.uk n'inclut pas ATP Challenger / WTA 125 (pas de cotes historiques).")
    print("  - Projection basee sur cotes TE scrapees (fenetre ci-dessus).")
    print("  - Matchs sans resultat dans match_results exclus du PnL.")


if __name__ == "__main__":
    main()
