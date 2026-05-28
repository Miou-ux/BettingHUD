"""
Analyse quantitative A/B : Kelly adaptatif (Brier segment) + plafond 15 % sur liquidité intraday.

Usage (depuis la racine du repo) :
    python scripts/kelly_ab_analysis_2025.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.backtest_staking_sim import load_and_filter_bets_csv, resolve_backtest_csv, simulate_sequential_intraday
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key


def _segment_calibration_key(row: pd.Series, ml: TennisMLModel) -> str:
    """Clé alignée sur les entrées ``segment_brier_scores`` du bundle V47 (ex. ATP_Hard_G)."""
    return resolve_match_brier_segment_key(
        ml,
        tour=row.get("tour"),
        surface=row.get("surface"),
        tournament=row.get("tournament"),
        tourney_level=row.get("tourney_level"),
    )


def _max_neg_streak_days(daily: list[float]) -> int:
    m = cur = 0
    for x in daily:
        if x < -1e-9:
            cur += 1
            m = max(m, cur)
        else:
            cur = 0
    return int(m)


def _daily_returns_from_history(history: list[dict], br0: float) -> list[float]:
    """Rendements journaliers par rapport à la BR en début de journée (échelle comparable Run A/B)."""
    if not history:
        return []
    prev = float(br0)
    out: list[float] = []
    for h in history:
        pnl = float(h.get("day_pnl_eur", 0.0))
        if prev > 0:
            out.append(pnl / prev)
        else:
            out.append(0.0)
        prev = float(h.get("bankroll", prev))
    return out


def _worst_k_day_pnl_pct(history: list[dict], br0: float, k: int) -> float:
    """Pire fenêtre de k jours : somme des PnL / BR au début de la fenêtre (%)."""
    if not history or k <= 0:
        return 0.0
    dpn = [float(h.get("day_pnl_eur", 0.0)) for h in history]
    starts: list[float] = []
    prev = float(br0)
    for h in history:
        starts.append(prev)
        prev = float(h.get("bankroll", prev))
    best = float("inf")
    for i in range(0, len(dpn) - k + 1):
        s = sum(dpn[i : i + k])
        b0w = starts[i]
        if b0w > 0:
            best = min(best, s / b0w * 100.0)
    return 0.0 if best == float("inf") else float(best)


def _fmt_eur_scientific(x: float) -> str:
    if not np.isfinite(x):
        return "-"
    ax = abs(x)
    if ax >= 1e7 or (ax > 0 and ax < 0.01):
        return f"{x:+.3e}"
    return f"{x:+,.2f}"


def main() -> None:
    year = 2025
    br0 = 100.0
    csv_path = resolve_backtest_csv(ROOT, year)
    if not csv_path or not os.path.isfile(csv_path):
        raise SystemExit(f"CSV introuvable pour {year}: {csv_path}")

    ml = TennisMLModel()
    if hasattr(ml, "_load_bundle_if_needed"):
        ml._load_bundle_if_needed()
    seg = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))

    df = load_and_filter_bets_csv(csv_path, year=year)
    if df.empty:
        raise SystemExit("DataFrame vide apres filtre.")
    df = df.copy()
    df["segment_calibration_key"] = df.apply(lambda r: _segment_calibration_key(r, ml), axis=1)

    common = dict(
        bankroll_start=br0,
        kelly_multiplier=1.0,
        max_stake_pct=15.0,
        daily_stake_budget_pct=100.0,
        use_fixed_stake_pct=False,
        use_adaptive_kelly_quarter=True,
        segment_brier_scores=seg,
        global_brier_score=glob_b,
        return_history=True,
        stake_cap_basis="liquid",
        track_stake_cap_hits=True,
    )

    runs = [
        ("Run A (Prudent, base Kelly 0.5)", 0.5),
        ("Run B (Agressif, base Kelly 1.0)", 1.0),
    ]

    rows = []
    full_out = []
    ev_mean = float(df["ev"].mean()) * 100.0 if "ev" in df.columns else float("nan")
    edge_pi = (
        float((df["p_model"].astype(float) - df["p_implied"].astype(float)).mean()) * 100.0
        if "p_implied" in df.columns
        else float("nan")
    )
    for label, base in runs:
        res = simulate_sequential_intraday(
            df,
            adaptive_kelly_base_fraction=float(base),
            **common,
        )
        full_out.append((label, base, res))
        n_days = int(res.get("n_trading_days") or 0)
        mult = float(res["bankroll_final"]) / float(br0) if br0 else 1.0
        if n_days > 0 and mult > 0 and np.isfinite(mult):
            geom_d = (mult ** (1.0 / n_days) - 1.0) * 100.0
            geom_s = f"{geom_d:+.3f}"
        else:
            geom_s = "-"
        rows.append(
            {
                "Scenario": label,
                "Profit total (EUR)": _fmt_eur_scientific(float(res["net_profit_eur"])),
                "Multiple BR final / depart": f"{mult:.3e}" if mult >= 1e6 or mult <= 1e-6 else f"{mult:.4f}",
                "Rend. geom. moy. journalier (%)": geom_s,
                "ROI sur volume (%)": f"{res['roi_on_staked_pct']:+.2f}",
                "Max drawdown (%)": f"{res['max_drawdown_pct']:.2f}",
                "Paris": res["n_bets"],
                "Mises plafonnees (15 %)": res.get("n_stake_cap_hits", 0),
                "Sharpe journalier *": f"{res['sharpe_daily']:.2f}",
                "EV moyenne placement (%) **": f"{ev_mean:.2f}" if np.isfinite(ev_mean) else "-",
                "Ecart p - p_implicite (pp) **": f"{edge_pi:+.2f}" if np.isfinite(edge_pi) else "-",
            }
        )

    _df_out = pd.DataFrame(rows)
    cols = list(_df_out.columns)
    out_md = "| " + " | ".join(cols) + " |\n"
    out_md += "| " + " | ".join("---" for _ in cols) + " |\n"
    for _, r in _df_out.iterrows():
        out_md += "| " + " | ".join(str(r[c]) for c in cols) + " |\n"
    print("## Configuration commune\n")
    print(f"- Fichier : `{os.path.relpath(csv_path, ROOT)}` ({len(df)} lignes)\n")
    print(f"- Brier global bundle : **{glob_b:.4f}**")
    print(f"- Segments Brier charges : **{len(seg)}** cles\n")
    print("- Kelly **adaptatif** : `stake_frac = base * f* * max(0, 1 - Brier_segment / 0.25)`\n")
    print("- **Plafond** : 15 % de la **liquidite disponible** avant chaque mise (`stake_cap_basis='liquid'`) -- ")
    print("tresorerie intraday restante (interpretation bankroll actuelle mobilisable).\n")
    print("- Budget journalier cumulé : 100 % BR du matin (inchangé).\n")
    print("## Tableau comparatif\n")
    print(out_md)
    print("\n* Sharpe = moyenne(PnL jour) / ecart-type(PnL jour) * sqrt(N jours) (implementation simulate_sequential_intraday, non annualise).\n")
    print("** Pas de closing odds dans le CSV : CLV closing non calculable ; proxies = EV au moment du backtest et p_model - p_implicite.\n")

    print("## Analyse de survie / discipline (Run B vs A)\n")
    _, _, rA = full_out[0]
    _, _, rB = full_out[1]
    dA = np.asarray(rA.get("daily_pnls") or [], dtype=float)
    dB = np.asarray(rB.get("daily_pnls") or [], dtype=float)
    hA = rA.get("history") or []
    hB = rB.get("history") or []
    rretA = _daily_returns_from_history(hA, br0)
    rretB = _daily_returns_from_history(hB, br0)
    w1A = min(rretA) * 100.0 if rretA else 0.0
    w1B = min(rretB) * 100.0 if rretB else 0.0
    w5A = _worst_k_day_pnl_pct(hA, br0, 5)
    w5B = _worst_k_day_pnl_pct(hB, br0, 5)
    print(
        f"- **Pire journee (vs BR debut de journee)** : A = **{w1A:.2f} %** | B = **{w1B:.2f} %** "
        f"(les montants EUR bruts divergent avec la taille de la BR ; le pourcentage est plus lisible).\n"
    )
    print(f"- **Pire fenetre 5 jours (PnL cumule / BR debut fenetre)** : A = **{w5A:.2f} %** | B = **{w5B:.2f} %**.\n")
    print(f"- **Longue serie max de jours negatifs** : A = {_max_neg_streak_days(list(dA))} | B = {_max_neg_streak_days(list(dB))}.\n")
    stdA = float(np.std(rretA, ddof=1)) * 100.0 if len(rretA) > 1 else 0.0
    stdB = float(np.std(rretB, ddof=1)) * 100.0 if len(rretB) > 1 else 0.0
    print(f"- **Ecart-type rendement journalier** : A = {stdA:.3f} pts | B = {stdB:.3f} pts.\n")
    ndA = int(rA.get("n_trading_days") or 0)
    ndB = int(rB.get("n_trading_days") or 0)
    mA = float(rA["bankroll_final"]) / br0 if br0 else 1.0
    mB = float(rB["bankroll_final"]) / br0 if br0 else 1.0
    gA = (mA ** (1.0 / ndA) - 1.0) * 100.0 if ndA > 0 and mA > 0 else 0.0
    gB = (mB ** (1.0 / ndB) - 1.0) * 100.0 if ndB > 0 and mB > 0 else 0.0
    add_geom = gB - gA
    add_dd = float(rB["max_drawdown_pct"] - rA["max_drawdown_pct"])
    add_sh = float(rB["sharpe_daily"] - rA["sharpe_daily"])
    print("\n### Synthese risque / rendement\n")
    print(
        f"- **Rendement geometrique moyen journalier** (B - A) : **{add_geom:+.3f} points** (sur le replay 2025 ; "
        "les niveaux absolus refletent une edge retracée sans friction).\n"
        f"- **Max drawdown** (B - A) : **{add_dd:+.2f} points**.\n"
        f"- **Sharpe journalier** (B - A) : **{add_sh:+.2f}**.\n"
        f"- **Paris avec mise coupee par le plafond 15 %** : A = **{rA.get('n_stake_cap_hits', 0)}** | B = **{rB.get('n_stake_cap_hits', 0)}** "
        "(Run B sollicite davantage le plafond si le Kelly brut depasse 15 % de la liquidite).\n"
    )
    roi_st_diff = float(rB["roi_on_staked_pct"] - rA["roi_on_staked_pct"])
    if rB["max_drawdown_pct"] > rA["max_drawdown_pct"] + 3.0 and add_sh < -0.2:
        print(
            "- **Lecture** : le Run B **degrade le ratio moyenne/volatilite** (Sharpe) et augmente le drawdown ; "
            f"le ROI sur volume evolue de **{roi_st_diff:+.2f} pts**. "
            "Pour un operateur sensible au stress intraday, le Run A offre une trajectoire plus lisse malgre un compose journalier inferieur.\n"
        )
    elif add_geom > 0.5 and add_dd < 25.0:
        print(
            "- **Lecture** : le Run B ameliore le compose journalier avec un surcout de drawdown modere au regard du replay ; "
            "la discipline reste a juger sur la **frequence des plafonnements** et les sequences de jours rouges.\n"
        )
    else:
        print(
            "- **Lecture** : arbitrer selon le mandat : **Sharpe**, **drawdown**, **plafonds** et **ROI sur volume** ; "
            "les euros absolus du replay ne sont pas calibres marché (pas de frais, liquidite illimitee).\n"
        )


if __name__ == "__main__":
    main()
