import json
import sqlite3
from datetime import datetime
from urllib.request import urlopen

import pandas as pd


def ensure_table(conn):
    # Source unique TML: on repart d'une table propre pour éviter les schémas historiques.
    conn.execute("DROP TABLE IF EXISTS matches_recent")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches_recent (
            tourney_id TEXT,
            tourney_name TEXT,
            surface TEXT,
            draw_size TEXT,
            tourney_level TEXT,
            indoor TEXT,
            tourney_date TEXT,
            match_num INTEGER,
            winner_id INTEGER,
            winner_seed REAL,
            winner_entry TEXT,
            winner_name TEXT,
            winner_hand TEXT,
            winner_ht REAL,
            winner_ioc TEXT,
            winner_age REAL,
            winner_rank REAL,
            winner_rank_points REAL,
            loser_id INTEGER,
            loser_seed REAL,
            loser_entry TEXT,
            loser_name TEXT,
            loser_hand TEXT,
            loser_ht REAL,
            loser_ioc TEXT,
            loser_age REAL,
            loser_rank REAL,
            loser_rank_points REAL,
            score TEXT,
            best_of INTEGER,
            round TEXT,
            minutes REAL,
            w_ace REAL,
            w_df REAL,
            w_svpt REAL,
            w_1stIn REAL,
            w_1stWon REAL,
            w_2ndWon REAL,
            w_SvGms REAL,
            w_bpSaved REAL,
            w_bpFaced REAL,
            l_ace REAL,
            l_df REAL,
            l_svpt REAL,
            l_1stIn REAL,
            l_1stWon REAL,
            l_2ndWon REAL,
            l_SvGms REAL,
            l_bpSaved REAL,
            l_bpFaced REAL,
            source TEXT,
            source_updated_at TEXT,
            UNIQUE(tourney_id, match_num, winner_id, loser_id)
        )
        """
    )
    conn.commit()


def fetch_year_csv(url: str) -> pd.DataFrame:
    df = pd.read_csv(url)
    df["source"] = "tennismylife"
    df["source_updated_at"] = datetime.utcnow().isoformat()
    return df


def fetch_available_files():
    payload = json.loads(urlopen("https://stats.tennismylife.org/api/data-files", timeout=30).read().decode("utf-8"))
    files = payload.get("files", [])
    # keep yearly files only: 2026.csv, 2025.csv, ...
    out = []
    for f in files:
        name = f.get("name", "")
        if name.endswith(".csv") and name[:-4].isdigit():
            out.append(f)
    return out


def sync_years(min_year=2010, max_year=None, db_path="data/bettinghud.db"):
    conn = sqlite3.connect(db_path)
    ensure_table(conn)

    if max_year is None:
        max_year = datetime.utcnow().year

    files = fetch_available_files()
    files = [f for f in files if min_year <= int(f["name"][:-4]) <= max_year]
    files = sorted(files, key=lambda x: int(x["name"][:-4]))

    total = 0
    for f in files:
        y = int(f["name"][:-4])
        try:
            df = fetch_year_csv(f["url"])
            # Harmoniser colonnes si variation mineure
            table_cols = [r[1] for r in conn.execute("PRAGMA table_info(matches_recent)").fetchall()]
            for c in table_cols:
                if c not in df.columns:
                    df[c] = pd.NA
            df = df[table_cols]
            # replace yearly slice (idempotent reruns)
            conn.execute(
                "DELETE FROM matches_recent WHERE source='tennismylife' AND substr(tourney_date,1,4)=?",
                (str(y),),
            )
            conn.commit()
            df.to_sql("matches_recent", conn, if_exists="append", index=False)
            n = len(df)
            total += n
            print(f"{y}: inserted {n}")
        except Exception as e:
            print(f"{y}: ERR {e}")

    conn.commit()
    conn.close()
    print(f"TOTAL_INSERTED {total}")


if __name__ == "__main__":
    sync_years(min_year=2010)
