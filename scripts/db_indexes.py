"""Index SQLite pour requêtes live (defending_ratio, signaux avancés)."""
from __future__ import annotations

import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "bettinghud.db")

_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_wta_matches_year
    ON wta_matches (CAST(substr(tourney_date, 1, 4) AS INTEGER));
CREATE INDEX IF NOT EXISTS idx_wta_matches_tourney_name
    ON wta_matches (tourney_name);
CREATE INDEX IF NOT EXISTS idx_matches_recent_tml_year
    ON matches_recent (CAST(substr(tourney_date, 1, 4) AS INTEGER))
    WHERE source = 'tennismylife';
CREATE INDEX IF NOT EXISTS idx_matches_recent_tourney_name
    ON matches_recent (tourney_name)
    WHERE source = 'tennismylife';
"""


def ensure_bettinghud_query_indexes(db_path: str | None = None) -> bool:
    path = db_path or DEFAULT_DB
    if not os.path.isfile(path):
        return False
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_INDEX_DDL)
        conn.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    ok = ensure_bettinghud_query_indexes()
    print("Indexes OK" if ok else "Indexes skipped or failed")
