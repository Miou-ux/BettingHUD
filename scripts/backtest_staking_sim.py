"""
Simulation de bankroll sur un CSV de paris backtest (colonnes type backtest_2026.py).

⚠️ Intégrité : ce module **rejoue** seulement des `p_model` / résultats déjà présents
dans le CSV. Une projection « sans triche » suppose que ces probabilités viennent d’un
backtest **no-leak** (ex. `scripts/backtest_2026.py` : modèle entraîné uniquement sur
données **strictement avant** le cutoff, typiquement le 1er janvier de l’année cible ; le CSV
porte les `p_model` produites par ce modèle figé, pas un ré-entraînement sur l’année test).

Méthodologie « intra-jour séquentielle » :
  - En début de journée, la BR = bankroll issue de la clôture précédente.
  - Les paris du jour sont ordonnés comme dans le fichier (après tri date).
  - Chaque mise est calculée sur le **liquide restant** du jour (cash non encore engagé
    ce jour-là), après les mises précédentes : ainsi le 2e pari ne « voit » plus la part
    déjà verrouillée par le 1er.
  - Kelly : mise brute = k_mult * f* * liquide ; plafond **par pari** = max_pct% de la **BR du matin**
    (pas du liquide restant) ; plafond **journalier cumulé** optionnel (% BR du matin, défaut 100%).
  - ensuite `stake = min(..., liquide)` pour ne pas dépasser le cash disponible.
  - Les gains/pertes de toutes les mises du jour sont appliqués en fin de journée
    (BR suivante = BR début jour + somme des PnL des mises du jour).
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np
import pandas as pd


def kelly_full_fraction(p_model: float, odd: float) -> float:
    """Fraction Kelly pleine f* = (b p - q) / b, avec b = odd-1, q = 1-p."""
    p = float(p_model)
    odd = float(odd)
    if odd <= 1.0 or p <= 0.0 or p >= 1.0:
        return 0.0
    b = max(1e-12, odd - 1.0)
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, float(f))


def _bool_won(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"1", "true", "yes"}


def load_and_filter_bets_csv(
    csv_path: str,
    *,
    year: Optional[int] = None,
    ev_min_pct: Optional[float] = None,
) -> pd.DataFrame:
    """Charge le CSV et applique filtres année (colonne date) et EV **minimum** (pourcent UI → fraction)."""
    df = pd.read_csv(csv_path)
    need = {"p_model", "odd", "won", "date"}
    miss = need - set(df.columns)
    if miss:
        raise ValueError(f"Colonnes manquantes dans {csv_path}: {sorted(miss)}")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df = df.reset_index(drop=True)
    df["_row"] = np.arange(len(df), dtype=int)
    df = df.sort_values(["date", "_row"], kind="mergesort").drop(columns=["_row"])
    if year is not None:
        y = int(year)
        df = df[df["date"].dt.year == y]
    if ev_min_pct is not None:
        if "ev" not in df.columns:
            raise ValueError("Filtre EV minimum demandé mais colonne 'ev' absente du CSV.")
        df = df[df["ev"].astype(float) >= float(ev_min_pct) / 100.0]
    return df.reset_index(drop=True)


def resolve_backtest_csv(repo_root: str, year: int) -> Optional[str]:
    """Retourne un chemin CSV existant pour l'année, ou None."""
    rels = [
        os.path.join("data", f"backtest_{year}_bets.csv"),
        os.path.join("data", f"backtest_{year}_bets_ev5.csv"),
    ]
    for rel in rels:
        p = os.path.join(repo_root, rel.replace("/", os.sep))
        if os.path.isfile(p):
            return p
    # tout fichier data/backtest_YEAR_bets*.csv
    data_dir = os.path.join(repo_root, "data")
    if not os.path.isdir(data_dir):
        return None
    prefix = f"backtest_{year}_bets"
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return None
    for n in names:
        if n.startswith(prefix) and n.lower().endswith(".csv"):
            return os.path.join(data_dir, n)
    return None


def simulate_sequential_intraday(
    df: pd.DataFrame,
    bankroll_start: float,
    kelly_multiplier: float,
    max_stake_pct: float,
    *,
    daily_stake_budget_pct: float = 100.0,
    return_history: bool = False,
) -> dict:
    """
    Kelly * kelly_multiplier sur le **liquide** restant ; plafond `max_stake_pct` = % de **BR du matin** par pari.

    `daily_stake_budget_pct` (déf. 100) : limite **la somme** des mises du jour (`Σ stake`) au plus
    ce pourcentage de la BR du matin. À 100 %, comportement historique (= jusqu'à toute la BR allouée
    dans la journée). Une valeur plus basse (ex. 30–50 %) évite une récomposition « tout le capital
    peut partir le même jour » qui amplifie l'irrésalisme avec une forte edge retracée.

    `df` : colonnes p_model, odd, won, date (datetime ou parseable).
    """
    if df.empty:
        out = {
            "bankroll_final": float(bankroll_start),
            "net_profit_eur": 0.0,
            "growth_pct": 0.0,
            "n_bets": 0,
            "n_wins": 0,
            "total_staked_eur": 0.0,
            "roi_on_staked_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "peak_bankroll": float(bankroll_start),
            "win_rate_pct": 0.0,
            "avg_stake_eur": 0.0,
            "max_daily_stake_eur": 0.0,
            "best_day_eur": 0.0,
            "worst_day_eur": 0.0,
            "avg_daily_pnl_eur": 0.0,
            "sharpe_daily": 0.0,
            "profit_factor": 0.0,
        }
        if return_history:
            out["history"] = []
            out["daily_pnls"] = []
        return out

    # Ordre intraday déterministe : même date → conserver l'ordre du DataFrame entré.
    # (sort_values(date) sans clé secondaire peut permuter les lignes d'un même jour selon pandas.)
    work = df.copy()
    work["_ord"] = np.arange(len(work), dtype=np.int64)
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    work = work.sort_values(["date", "_ord"], kind="mergesort").drop(columns=["_ord"])
    work = work.reset_index(drop=True)

    br = float(bankroll_start)
    total_staked = 0.0
    n_bets = 0
    n_wins = 0
    gross_win = 0.0
    gross_loss_abs = 0.0
    max_daily_stake = 0.0
    daily_pnls: list[float] = []
    history: list[dict] = []
    equity_for_dd: list[float] = [float(bankroll_start)]

    k_mult = float(kelly_multiplier)
    cap_frac = float(max_stake_pct) / 100.0

    for _day, day_df in work.groupby("date", sort=True):
        day_df = day_df.reset_index(drop=True)
        b0 = br
        liquid = b0
        day_deploy_cap = (
            float(b0) * float(min(100.0, max(0.0, daily_stake_budget_pct)) / 100.0)
        )
        day_deploy_used = 0.0
        day_stake_total = 0.0
        day_pnl = 0.0

        for _, row in day_df.iterrows():
            odd = float(row["odd"])
            p = float(row["p_model"])
            won = _bool_won(row["won"])
            kf = kelly_full_fraction(p, odd)
            raw = liquid * k_mult * kf
            # Plafond : % de la BR **du matin** par pari (pas % du liquide restant).
            cap_lim = b0 * cap_frac
            remaining_day_budget = max(0.0, day_deploy_cap - day_deploy_used)
            stake = max(0.0, min(raw, cap_lim, liquid, remaining_day_budget))
            if stake <= 0.0:
                continue
            liquid -= stake
            day_deploy_used += stake
            n_bets += 1
            total_staked += stake
            day_stake_total += stake
            pnl = stake * (odd - 1.0) if won else -stake
            day_pnl += pnl
            if pnl > 0:
                gross_win += pnl
            else:
                gross_loss_abs += abs(pnl)
            if won:
                n_wins += 1

        max_daily_stake = max(max_daily_stake, day_stake_total)
        daily_pnls.append(day_pnl)
        br = b0 + day_pnl
        equity_for_dd.append(float(br))
        if return_history:
            history.append(
                {
                    "date": pd.Timestamp(_day),
                    "bankroll": float(br),
                    "n_bets_cum": int(n_bets),
                    "pnl_cum_eur": float(br - bankroll_start),
                    "day_pnl_eur": float(day_pnl),
                    "day_stake_eur": float(day_stake_total),
                }
            )

    eq_arr = np.asarray(equity_for_dd, dtype=float)
    peaks = np.maximum.accumulate(eq_arr)
    dd_series = (peaks - eq_arr) / np.where(peaks > 0, peaks, 1.0)
    max_dd = float(dd_series.max()) if len(dd_series) else 0.0

    net = br - float(bankroll_start)
    roi_staked = (net / total_staked * 100.0) if total_staked > 0 else 0.0
    win_rate = (n_wins / n_bets * 100.0) if n_bets else 0.0
    avg_stake = (total_staked / n_bets) if n_bets else 0.0

    dpn = np.asarray(daily_pnls, dtype=float) if daily_pnls else np.array([])
    best_day = float(dpn.max()) if len(dpn) else 0.0
    worst_day = float(dpn.min()) if len(dpn) else 0.0
    avg_daily = float(dpn.mean()) if len(dpn) else 0.0
    if len(dpn) > 1 and float(dpn.std(ddof=1)) > 0:
        sharpe_d = float((dpn.mean() / dpn.std(ddof=1)) * np.sqrt(len(dpn)))
    else:
        sharpe_d = 0.0
    pf = (gross_win / gross_loss_abs) if gross_loss_abs > 1e-12 else (float("inf") if gross_win > 0 else 0.0)

    out: dict = {
        "bankroll_final": float(br),
        "net_profit_eur": float(net),
        "growth_pct": (net / bankroll_start) * 100.0 if bankroll_start else 0.0,
        "n_bets": int(n_bets),
        "n_wins": int(n_wins),
        "total_staked_eur": float(total_staked),
        "roi_on_staked_pct": float(roi_staked),
        "max_drawdown_pct": float(max_dd * 100.0),
        "peak_bankroll": float(peaks.max()) if len(peaks) else float(br),
        "win_rate_pct": float(win_rate),
        "avg_stake_eur": float(avg_stake),
        "max_daily_stake_eur": float(max_daily_stake),
        "best_day_eur": best_day,
        "worst_day_eur": worst_day,
        "avg_daily_pnl_eur": avg_daily,
        "sharpe_daily": sharpe_d,
        "profit_factor": float(pf) if pf != float("inf") else None,
        "n_trading_days": len(dpn),
    }
    if return_history:
        out["history"] = history
        out["daily_pnls"] = daily_pnls
    return out
