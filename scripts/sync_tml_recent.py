import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from urllib.request import urlopen

import pandas as pd

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
from surface_speed import lookup_surface_speed  # noqa: E402


def ensure_surface_speed_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches_recent)").fetchall()}
    if "surface_speed" not in cols:
        conn.execute("ALTER TABLE matches_recent ADD COLUMN surface_speed REAL")
        conn.commit()


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
            surface_speed REAL,
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
    t_sync_all = time.perf_counter()
    conn = sqlite3.connect(db_path)
    ensure_table(conn)
    ensure_surface_speed_column(conn)

    if max_year is None:
        max_year = datetime.utcnow().year

    files = fetch_available_files()
    files = [f for f in files if min_year <= int(f["name"][:-4]) <= max_year]
    files = sorted(files, key=lambda x: int(x["name"][:-4]))
    print(
        f"[sync_tml] {len(files)} fichier(s) année(s) {min_year}-{max_year} — début",
        flush=True,
    )

    total = 0
    for f in files:
        y = int(f["name"][:-4])
        t_y = time.perf_counter()
        try:
            df = fetch_year_csv(f["url"])
            # Harmoniser colonnes si variation mineure
            table_cols = [r[1] for r in conn.execute("PRAGMA table_info(matches_recent)").fetchall()]
            for c in table_cols:
                if c not in df.columns:
                    df[c] = pd.NA
            df["surface_speed"] = df.apply(
                lambda r: float(lookup_surface_speed(r.get("tourney_name"), r.get("surface"))),
                axis=1,
            )
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
            dt_y = time.perf_counter() - t_y
            print(f"{y}: inserted {n} — {dt_y:.1f}s", flush=True)
        except Exception as e:
            print(f"{y}: ERR {e}", flush=True)

    conn.commit()
    conn.close()
    dt_sync = time.perf_counter() - t_sync_all
    print(f"TOTAL_INSERTED {total}", flush=True)
    print(f"[sync_tml] durée totale synchronisation {dt_sync:.1f}s ({dt_sync/60:.1f} min)", flush=True)


if __name__ == "__main__":
    sync_years(min_year=2010)
