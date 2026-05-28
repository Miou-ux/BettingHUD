"""Diagnostic Iatcenko P. vs Monnet C. (French Open WTA)."""
from __future__ import annotations

import os
import sqlite3
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.ml_model import TennisMLModel
from scripts.player_identity import canonical_name, to_lastname_initial
from scripts.stats_engine import TennisStatsEngine
from scripts.value_detector import ValueDetector

# Pont inactivité TE (copie dashboard)
_TE_INACTIVITY_BRIDGE_DAYS = 60


def _blend_inactivity_days_with_te(tml_days, profile, ref_dt_iso):
    try:
        td = float(tml_days)
    except (TypeError, ValueError):
        td = 7.0
    td = max(0.0, td)
    if td < _TE_INACTIVITY_BRIDGE_DAYS:
        return td, False
    if not profile or not ref_dt_iso:
        return td, False
    raw = profile.get("te_last_match_date_iso")
    if not raw:
        return td, False
    try:
        ref = pd.Timestamp(str(ref_dt_iso)[:10]).normalize()
        last = pd.Timestamp(str(raw)[:10]).normalize()
    except Exception:
        return td, False
    if last > ref:
        return td, False
    te_d = int(max(0, (ref - last).days))
    new_d = float(min(td, te_d))
    return new_d, new_d + 1e-6 < td

# Copie minimale dashboard (évite import streamlit)
def _name_key(name: str) -> str:
    from scripts.player_identity import to_lastname_initial

    s = to_lastname_initial(str(name or "")).strip().lower()
    return s.replace(".", "").replace("  ", " ").strip()


def _days_from_wta(name: str, ref_iso: str) -> int:
    import sqlite3

    pk = _name_key(name)
    cn = sqlite3.connect(os.path.join(ROOT, "data", "bettinghud.db"))
    df = pd.read_sql_query(
        "SELECT tourney_date, winner_name, loser_name FROM wta_matches "
        "WHERE CAST(substr(tourney_date,1,4) AS INTEGER) >= 2023",
        cn,
    )
    cn.close()
    df["tourney_date"] = pd.to_datetime(df["tourney_date"], errors="coerce")
    df["w_key"] = df["winner_name"].map(lambda n: _name_key(to_lastname_initial(str(n or ""))))
    df["l_key"] = df["loser_name"].map(lambda n: _name_key(to_lastname_initial(str(n or ""))))
    rows = df[(df["w_key"] == pk) | (df["l_key"] == pk)]
    if rows.empty:
        return -1
    ref = pd.Timestamp(ref_iso).normalize()
    return int(max(0, (ref - rows["tourney_date"].max()).days))

CSV = os.path.join(ROOT, "data", "scraped", "prematch_odds_20260517_200546.csv")


def main() -> None:
    row = pd.read_csv(CSV)
    row = row[
        row["player1"].astype(str).str.contains("Iatcenko", case=False, na=False)
        & row["player2"].astype(str).str.contains("Monnet", case=False, na=False)
    ].iloc[0]
    print("=== PREMATCH ROW ===")
    for k, v in row.items():
        print(f"  {k}: {v}")

    se = TennisStatsEngine()
    ml = TennisMLModel()
    ml._load_bundle_if_needed()

    p1n, p2n = str(row["player1"]).strip(), str(row["player2"]).strip()
    tour = "WTA"
    mdate = str(row.get("date", "2026-05-18"))[:10]

    for label, name in [("P1 Iatcenko", p1n), ("P2 Monnet", p2n)]:
        print(f"\n=== {label} ===")
        meta = se.get_player_id_meta(name, source_url=row.get("p1_url" if "Iatcenko" in name else "p2_url"), tour_hint=tour)
        pid = meta.get("player_id")
        print(f"  player_id: {pid!r}  meta: {meta}")
        st = se.get_player_stats(pid, name, tour_hint=tour)
        keys = (
            "rank",
            "pts",
            "age",
            "ht",
            "hand",
            "stats_source",
            "stats_reference_date",
            "stats_source_detail",
        )
        print("  stats:", {k: st.get(k) for k in keys})
        if pid:
            print("  form:", se.get_recent_form(pid, tour_hint=tour))
            print("  fatigue:", se.get_recent_fatigue(pid, tour_hint=tour))
            print("  match_quality:", se.get_recent_match_quality(pid, tour_hint=tour))
        pat = f"%{name.split()[0]}%"
        li = f"%{to_lastname_initial(name)}%"
        cn = sqlite3.connect(ml.db_path)
        r = pd.read_sql_query(
            """
            SELECT tourney_date, tourney_name, winner_name, loser_name,
                   winner_rank, loser_rank, winner_rank_points, loser_rank_points
            FROM wta_matches
            WHERE winner_name LIKE ? OR loser_name LIKE ?
               OR winner_name LIKE ? OR loser_name LIKE ?
            ORDER BY tourney_date DESC LIMIT 8
            """,
            cn,
            params=(pat, pat, li, li),
        )
        cn.close()
        print("  wta_matches (fuzzy last name):")
        print(r.to_string(index=False) if not r.empty else "    (aucune ligne)")

    p1_id = se.get_player_id_meta(p1n, source_url=row.get("p1_url"), tour_hint=tour).get("player_id")
    p2_id = se.get_player_id_meta(p2n, source_url=row.get("p2_url"), tour_hint=tour).get("player_id")
    p1_stats = se.get_player_stats(p1_id, p1n, tour_hint=tour)
    p2_stats = se.get_player_stats(p2_id, p2n, tour_hint=tour)

    print("\n=== NAME KEYS & INACTIVITY (ref 2026-05-18) ===")
    print(f"  p1_key={_name_key(p1n)!r}  p2_key={_name_key(p2n)!r}")
    print(f"  Iatcenko days since last WTA row: {_days_from_wta(p1n, mdate)}")
    print(f"  Monnet days since last WTA row: {_days_from_wta(p2n, mdate)}")

    for pid, label in [(p1_id, "P1"), (p2_id, "P2")]:
        in_bundle = pid in (ml.player_elo or {}) if pid else False
        nk = _name_key(p1n if label == "P1" else p2n)
        in_name = nk in (ml.player_elo or {})
        in_name_elo = nk in (getattr(ml, "player_name_elo", {}) or {})
        print(
            f"  {label} id={pid!r} in player_elo={in_bundle} "
            f"name_key={nk!r} in player_elo={in_name} in player_name_elo={in_name_elo}"
        )

    p1_days_sig = _days_from_wta(p1n, mdate)
    p2_days_sig = _days_from_wta(p2n, mdate)
    print(f"  (dashboard adv_signals devrait donner p1_days={p1_days_sig}, p2_days={p2_days_sig})")

    det = ValueDetector(0.05)

    print("\n=== PREDICT (défaut: 7 j — comme si signaux absents) ===")
    preds_bad = ml.predict_match(
        p1_name=p1n,
        p2_name=p2n,
        p1_rank=p1_stats.get("rank", 100),
        p2_rank=p2_stats.get("rank", 100),
        p1_pts=p1_stats.get("pts", 1000),
        p2_pts=p2_stats.get("pts", 1000),
        p1_age=p1_stats.get("age", 25),
        p2_age=p2_stats.get("age", 25),
        p1_ht=p1_stats.get("ht", 185),
        p2_ht=p2_stats.get("ht", 185),
        p1_hand=p1_stats.get("hand", "U"),
        p2_hand=p2_stats.get("hand", "U"),
        surface="Clay",
        tour=tour,
        tournament_level="Grand Slam",
        tournament_name=str(row.get("tournament", "French Open")),
        match_date=mdate,
        p1_id=p1_id,
        p2_id=p2_id,
    )
    print(
        f"  50/50 bug: P1={1/preds_bad['p1_true_odd']*100:.1f}% "
        f"EV_Iatcenko={det.detect_value(float(row['odd_p1']), preds_bad['p1_true_odd'])['value_pct']:+.1f}%"
    )

    print("\n=== PREDICT (avec jours inactivité réels) ===")
    preds = ml.predict_match(
        p1_name=p1n,
        p2_name=p2n,
        p1_rank=p1_stats.get("rank", 100),
        p2_rank=p2_stats.get("rank", 100),
        p1_pts=p1_stats.get("pts", 1000),
        p2_pts=p2_stats.get("pts", 1000),
        p1_age=p1_stats.get("age", 25),
        p2_age=p2_stats.get("age", 25),
        p1_ht=p1_stats.get("ht", 185),
        p2_ht=p2_stats.get("ht", 185),
        p1_hand=p1_stats.get("hand", "U"),
        p2_hand=p2_stats.get("hand", "U"),
        surface="Clay",
        tour=tour,
        tournament_level="Grand Slam",
        tournament_name=str(row.get("tournament", "French Open")),
        match_date=mdate,
        p1_days_since_last_match=float(p1_days_sig) if p1_days_sig >= 0 else 200.0,
        p2_days_since_last_match=float(p2_days_sig) if p2_days_sig >= 0 else 7.0,
        p1_id=p1_id,
        p2_id=p2_id,
    )
    p1t, p2t = preds["p1_true_odd"], preds["p2_true_odd"]
    print(f"  p1_prob: {1/p1t*100:.1f}%  p2_prob: {1/p2t*100:.1f}%")
    print(f"  true_odds: P1={p1t:.2f}  P2={p2t:.2f}")
    print(f"  confidence: {preds.get('confidence')}")
    print(f"  calibration: {preds.get('calibration_used')}")
    print(f"  segment: {preds.get('segment_calibration_key')}")

    fs = preds.get("feature_snapshot") or {}
    print("\n  feature_snapshot (clés utiles):")
    for k in sorted(fs.keys()):
        kl = k.lower()
        if any(x in kl for x in ("days", "inact", "rank", "elo", "diff", "points", "style")):
            print(f"    {k}: {fs[k]}")

    import json

    prof_path = os.path.join(ROOT, "data", "cache", "player_iatcenko.json")
    if os.path.isfile(prof_path):
        with open(prof_path, encoding="utf-8") as f:
            prof = json.load(f)
        print("\n=== PROFIL TE Iatcenko (cache) ===")
        print(f"  te_last_match_date_iso: {prof.get('te_last_match_date_iso')}")
        print(f"  rank TE: {prof.get('rank')}  (WTA base rank: {p1_stats.get('rank')})")
        eff, used = _blend_inactivity_days_with_te(p1_days_sig, prof, mdate)
        print(f"  Pont inactivité: WTA {p1_days_sig}j -> effectif {eff:.0f}j  blend_used={used}")
        pr_te = ml.predict_match(
            p1_name=p1n,
            p2_name=p2n,
            p1_rank=p1_stats.get("rank", 100),
            p2_rank=p2_stats.get("rank", 100),
            p1_pts=p1_stats.get("pts", 1000),
            p2_pts=p2_stats.get("pts", 1000),
            p1_age=p1_stats.get("age", 25),
            p2_age=p2_stats.get("age", 25),
            p1_ht=185,
            p2_ht=185,
            p1_hand="U",
            p2_hand="L",
            surface="Clay",
            tour=tour,
            tournament_level="Grand Slam",
            p1_days_since_last_match=eff,
            p2_days_since_last_match=float(p2_days_sig),
            p1_id=p1_id,
            p2_id=p2_id,
        )
        p1t_te = pr_te["p1_true_odd"]
        print(
            f"  Proba avec pont TE: P1 {100/p1t_te:.1f}% "
            f"(cote juste {p1t_te:.2f}) — proche du scénario « actif récemment »"
        )

    print("\n=== VALUE vs BOOK ===")
    for side, o, t in [
        ("P1 Iatcenko", float(row["odd_p1"]), p1t),
        ("P2 Monnet", float(row["odd_p2"]), p2t),
    ]:
        v = det.detect_value(o, t)
        print(
            f"  {side}: book={o:.2f} true={t:.2f} "
            f"EV={v['value_pct']:+.1f}% sharpe={v['sharpe_ratio']:.3f} value={v['is_value']}"
        )
    print("\n  Book implied: P1={:.1f}% P2={:.1f}%".format(
        100 / float(row["odd_p1"]), 100 / float(row["odd_p2"])
    ))


if __name__ == "__main__":
    main()
