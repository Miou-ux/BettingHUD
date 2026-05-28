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
from __future__ import annotations

import argparse
import os
import sqlite3

import pandas as pd


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
    rd_raw = rdate if rdate is not None else pd.NA
    out = pd.DataFrame(
        {
            "ranking_date": rd_raw,
            "ranking": rnk,
            "player_id": pd.to_numeric(pid, errors="coerce"),
            "points": pd.to_numeric(points, errors="coerce"),
        }
    )
    out = out.dropna(subset=["player_id", "points"])

    def _to_iso_date(v):
        if pd.isna(v):
            return None
        try:
            n = int(float(v))
            if 19000101 <= n <= 21001231:
                y, mo, d = n // 10000, (n // 100) % 100, n % 100
                return f"{y:04d}-{mo:02d}-{d:02d}"
        except (TypeError, ValueError):
            pass
        try:
            ts = pd.to_datetime(v, errors="coerce")
            if pd.isna(ts):
                return None
            return str(ts.date())
        except Exception:
            return None

    out["ranking_date"] = out["ranking_date"].map(_to_iso_date)
    return out


def _ingest_sqlite(df: pd.DataFrame, db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        df.to_sql("rankings_wta_current", conn, if_exists="replace", index=False)
    finally:
        conn.close()
    return len(df)


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
    if wta.empty:
        print("WTA rankings : fichier vide — skip.")
        return

    try:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{args.db_path}")
        wta.to_sql("rankings_wta_current", engine, if_exists="replace", index=False)
    except ImportError:
        n = _ingest_sqlite(wta, args.db_path)
        print(f"rankings_wta_current : {n} lignes (sqlite3, sqlalchemy absent)")
        return

    print(f"rankings_wta_current : {len(wta)} lignes")


if __name__ == "__main__":
    main()
