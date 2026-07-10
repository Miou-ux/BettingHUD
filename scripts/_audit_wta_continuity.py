"""Prod WTA continuity audit snapshot."""
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(".")
c = sqlite3.connect(ROOT / "data/bettinghud.db")
print("=== SQLite wta_matches ===")
print("count", c.execute("SELECT COUNT(*) FROM wta_matches").fetchone()[0])
print("max_date", c.execute("SELECT MAX(tourney_date) FROM wta_matches").fetchone()[0])
print("post_jun8", c.execute("SELECT COUNT(*) FROM wta_matches WHERE tourney_date > '2026-06-08'").fetchone()[0])
print("=== rankings_wta_current ===")
print("max_rank_date", c.execute("SELECT MAX(ranking_date) FROM rankings_wta_current").fetchone()[0])
print("count", c.execute("SELECT COUNT(*) FROM rankings_wta_current").fetchone()[0])

w = ROOT / "data/raw/tennis_wta"
for fn in ["wta_matches_2026.csv", "wta_matches_qual_itf_2026.csv"]:
    p = w / fn
    if p.exists():
        df = pd.read_csv(p, usecols=["tourney_date"])
        print(fn, "rows", len(df), "max", int(df["tourney_date"].max()))

p = w / "wta_rankings_current.csv"
if p.exists():
    df = pd.read_csv(p)
    print("wta_rankings_current.csv rows", len(df), "max_rank_date", df["ranking_date"].max())

# serve stats coverage on post-cutoff main tour
df26 = pd.read_csv(w / "wta_matches_2026.csv")
post = df26[df26["tourney_date"] > 20260608]
if len(post):
    sv = post["w_svpt"].notna().mean() * 100
    rk = post["winner_rank"].notna().mean() * 100
    print("post_jun8_main_rows", len(post), "serve_pct", round(sv, 1), "winner_rank_pct", round(rk, 1))
