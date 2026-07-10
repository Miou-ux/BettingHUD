import sqlite3

c = sqlite3.connect("data/bettinghud.db")
print("wta_max", c.execute("SELECT MAX(tourney_date) FROM wta_matches").fetchone()[0])
print("post_jun8", c.execute("SELECT COUNT(*) FROM wta_matches WHERE tourney_date > '2026-06-08'").fetchone()[0])
q = """
SELECT COUNT(*) FROM wta_matches
WHERE tourney_date > '2026-06-08'
  AND winner_rank IS NOT NULL AND loser_rank IS NOT NULL
  AND CAST(winner_rank AS REAL) > 0 AND CAST(loser_rank AS REAL) > 0
"""
print("post_jun8_with_ranks", c.execute(q).fetchone()[0])
q2 = """
SELECT COUNT(*) FROM wta_matches
WHERE tourney_date > '2026-06-08' AND w_svpt IS NOT NULL AND w_svpt != ''
"""
print("post_jun8_with_serve", c.execute(q2).fetchone()[0])
