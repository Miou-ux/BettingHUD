"""
Importe les matchs WTA (Jeff Sackmann / Tennis Abstract) dans SQLite table `wta_matches`.
Prérequis : CSV sous ``data/raw/tennis_wta`` (``wta_matches_*.csv``) — clone
https://github.com/JeffSackmann/tennis_wta ou ``python scripts/fetch_wta_sackmann_raw.py``.

Usage :
  python scripts/ingest_sackmann_wta.py
  python scripts/ingest_sackmann_wta.py --db-path data/bettinghud.db --raw-dir data/raw/tennis_wta
"""
import argparse
import glob
import os

import pandas as pd
from sqlalchemy import create_engine


def _load_match_frames(raw_dir: str):
    files = sorted(
        glob.glob(os.path.join(raw_dir, "wta_matches_*.csv"))
    )
    files = [
        f
        for f in files
        if "doubles" not in f.lower() and "qual" not in os.path.basename(f).lower()
    ]
    if not files:
        raise FileNotFoundError(
            f"Aucun wta_matches_*.csv trouvé dans {raw_dir}. "
            "Télécharge le dépôt tennis_wta (Jeff Sackmann)."
        )
    dfs = []
    for p in files:
        try:
            dfs.append(pd.read_csv(p, low_memory=False))
        except Exception as e:
            print(f"Skip {p}: {e}")
    if not dfs:
        raise RuntimeError("Impossible de lire les CSV WTA.")
    out = pd.concat(dfs, ignore_index=True)
    for col in out.columns:
        if out[col].dtype == "object":
            out[col] = out[col].astype(str)
    out["tourney_date"] = pd.to_datetime(out["tourney_date"], format="%Y%m%d", errors="coerce")
    out = out.dropna(subset=["tourney_date"]).sort_values("tourney_date").reset_index(drop=True)
    return out


def main():
    parser = argparse.ArgumentParser(description="Ingest WTA Sackmann matches into wta_matches table.")
    parser.add_argument("--db-path", default=os.path.join("data", "bettinghud.db"))
    parser.add_argument("--raw-dir", default=os.path.join("data", "raw", "tennis_wta"))
    args = parser.parse_args()

    if not os.path.isdir(args.raw_dir):
        raise SystemExit(
            f"Dossier introuvable: {args.raw_dir}. Crée-le et y place les CSV wta_matches_*.csv."
        )

    df = _load_match_frames(args.raw_dir)
    engine = create_engine(f"sqlite:///{args.db_path}")
    df.to_sql("wta_matches", engine, if_exists="replace", index=False)
    print(f"Ingest WTA OK - {len(df)} lignes -> wta_matches ({args.db_path})")


if __name__ == "__main__":
    main()
