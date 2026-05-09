"""Crée des index SQLite pour accélérer les jointures stats / H2H (sans changer les données).

Les index composites `(winner_id, tourney_date)` et `(loser_id, tourney_date)` permettent
de récupérer le dernier match d'un joueur sans scanner la table entière.

Tables couvertes :
  - matches_recent (TennisMyLife / ATP)
  - wta_matches    (Tennis Abstract / WTA)
  - rankings_wta_current (lookup direct par player_id)

Tables ATP Sackmann historiques (`matches`, `players`, `rankings_atp_current`) ne sont
plus indexées : l'ATP est désormais entièrement servie par `matches_recent`.
"""
import argparse
import sqlite3


INDEX_STMTS = [
    "CREATE INDEX IF NOT EXISTS idx_wta_matches_winner_id ON wta_matches(winner_id)",
    "CREATE INDEX IF NOT EXISTS idx_wta_matches_loser_id ON wta_matches(loser_id)",
    "CREATE INDEX IF NOT EXISTS idx_wta_winner_date ON wta_matches(winner_id, tourney_date)",
    "CREATE INDEX IF NOT EXISTS idx_wta_loser_date ON wta_matches(loser_id, tourney_date)",
    "CREATE INDEX IF NOT EXISTS idx_mr_winner_id ON matches_recent(winner_id)",
    "CREATE INDEX IF NOT EXISTS idx_mr_loser_id ON matches_recent(loser_id)",
    "CREATE INDEX IF NOT EXISTS idx_mr_winner_date ON matches_recent(winner_id, tourney_date)",
    "CREATE INDEX IF NOT EXISTS idx_mr_loser_date ON matches_recent(loser_id, tourney_date)",
    "CREATE INDEX IF NOT EXISTS idx_rankings_wta_pid ON rankings_wta_current(player_id)",
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db-path", default="data/bettinghud.db")
    args = p.parse_args()

    conn = sqlite3.connect(args.db_path, timeout=120.0)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    for sql in INDEX_STMTS:
        try:
            cur.execute(sql)
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                continue
            raise
    conn.commit()
    try:
        cur.execute("ANALYZE")
        conn.commit()
    except Exception:
        pass
    conn.close()
    print("Index SQLite appliqués (tables manquantes ignorées) + ANALYZE.")


if __name__ == "__main__":
    main()
