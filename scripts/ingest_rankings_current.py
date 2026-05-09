"""
Importe le classement WTA courant Sackmann (`wta_rankings_current.csv`) dans SQLite.

Le pipeline ATP ne dépend plus des classements Sackmann : la source ATP officielle
est désormais TennisMyLife (`scripts/sync_tml_recent.py`). Ce script ne traite donc
que la WTA.

Prérequis :
  data/raw/tennis_wta/wta_rankings_current.csv

Usage :
  python scripts/ingest_rankings_current.py
"""
import argparse
import os

import pandas as pd
from sqlalchemy import create_engine


def _normalize_rankings_df(df: pd.DataFrame) -> pd.DataFrame:
    colmap = {c.lower().strip(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in colmap:
                return df[colmap[n]]
        return None

    points = pick("points", "ranking_points", "pts")
    pid = pick("player_id", "player")
    rnk = pick("ranking", "rank")
    rdate = pick("ranking_date", "week_date", "date")
    if points is None or pid is None:
        raise ValueError("CSV classements WTA : colonnes player_id / points introuvables.")
    if rnk is None:
        rnk = pd.Series(range(1, len(df) + 1), dtype="float")
    out = pd.DataFrame(
        {
            "ranking_date": rdate if rdate is not None else pd.NA,
            "ranking": rnk,
            "player_id": pd.to_numeric(pid, errors="coerce"),
            "points": pd.to_numeric(points, errors="coerce"),
        }
    )
    out = out.dropna(subset=["player_id", "points"])
    return out


def main():
    parser = argparse.ArgumentParser(description="Ingest Sackmann WTA current rankings into SQLite.")
    parser.add_argument("--db-path", default=os.path.join("data", "bettinghud.db"))
    parser.add_argument(
        "--wta-csv",
        default=os.path.join("data", "raw", "tennis_wta", "wta_rankings_current.csv"),
    )
    args = parser.parse_args()

    if not os.path.isfile(args.wta_csv):
        print(f"WTA rankings : fichier {args.wta_csv} absent — skip.")
        return

    df = pd.read_csv(args.wta_csv, low_memory=False)
    wta = _normalize_rankings_df(df)

    engine = create_engine(f"sqlite:///{args.db_path}")
    if not wta.empty:
        wta.to_sql("rankings_wta_current", engine, if_exists="replace", index=False)
        print(f"rankings_wta_current : {len(wta)} lignes")
    else:
        print("WTA rankings : fichier vide — skip.")


if __name__ == "__main__":
    main()
