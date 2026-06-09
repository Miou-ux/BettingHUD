import sqlite3
from scripts.bets_db import ensure_daily_top_proba_schema, sync_daily_top_proba_from_results

c = sqlite3.connect("data/bettinghud.db")
ensure_daily_top_proba_schema(c)
sync_daily_top_proba_from_results(c)
r = c.execute(
    "SELECT MIN(calendar_date), MAX(calendar_date), COUNT(DISTINCT calendar_date) "
    "FROM daily_top_proba_picks WHERE rank=1"
).fetchone()
print("dates rank1:", r)
for row in c.execute(
    """SELECT calendar_date, tour, fav_player, status, theoretical_profit, ev_fav_pct, p_model_fav
    FROM daily_top_proba_picks WHERE rank=1 ORDER BY calendar_date DESC LIMIT 10"""
):
    print(row)
