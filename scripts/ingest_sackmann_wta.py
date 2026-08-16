"""
Importe les matchs WTA (Jeff Sackmann / Tennis Abstract) dans SQLite table `wta_matches`.
Prérequis : CSV sous ``data/raw/tennis_wta`` (``wta_matches_*.csv`` + ``wta_matches_qual_itf_*.csv``) — clone
https://github.com/JeffSackmann/tennis_wta ou ``python scripts/fetch_wta_sackmann_raw.py``.

Par défaut, seules les saisons **>= 2010** sont ingérées (même coupure que l'ATP TML / ``ml_model.prepare_data``).

Usage :
  python scripts/ingest_sackmann_wta.py
  python scripts/ingest_sackmann_wta.py --min-year 2010
"""
import argparse
import glob
import os
import re
import sys

import pandas as pd
from sqlalchemy import create_engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_WTA_CSV_YEAR_RE = re.compile(r"(\d{4})\.csv$", re.IGNORECASE)
DEFAULT_WTA_MIN_YEAR = int(os.getenv("BETTINGHUD_WTA_SACKMANN_MIN_YEAR", "2010"))


def _csv_file_year(path: str) -> int | None:
    m = _WTA_CSV_YEAR_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _list_wta_match_csv_files(raw_dir: str, min_year: int) -> list[str]:
    files = sorted(glob.glob(os.path.join(raw_dir, "wta_matches_*.csv")))
    # Inclut wta_matches_YYYY.csv et wta_matches_qual_itf_YYYY.csv ; exclut uniquement les doubles.
    out = []
    for f in files:
        if "doubles" in os.path.basename(f).lower():
            continue
        y = _csv_file_year(f)
        if y is not None and y < int(min_year):
            continue
        out.append(f)
    return out


def _load_match_frames(raw_dir: str, min_year: int):
    files = _list_wta_match_csv_files(raw_dir, min_year)
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
    from scripts.wta_sackmann_common import max_sane_wta_year

    out["tourney_date"] = pd.to_datetime(out["tourney_date"], format="%Y%m%d", errors="coerce")
    out = out.dropna(subset=["tourney_date"])
    y0 = int(min_year)
    y_max = max_sane_wta_year()
    out = out.loc[(out["tourney_date"].dt.year >= y0) & (out["tourney_date"].dt.year <= y_max)]
    return out.sort_values("tourney_date").reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description="Ingest WTA Sackmann matches into wta_matches table.")
    parser.add_argument("--db-path", default=os.path.join("data", "bettinghud.db"))
    parser.add_argument("--raw-dir", default=os.path.join("data", "raw", "tennis_wta"))
    parser.add_argument(
        "--min-year",
        type=int,
        default=DEFAULT_WTA_MIN_YEAR,
        help="Première année calendaire incluse (déf. 2010, aligné ATP).",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.raw_dir):
        raise SystemExit(
            f"Dossier introuvable: {args.raw_dir}. Crée-le et y place les CSV wta_matches_*.csv."
        )

    files = _list_wta_match_csv_files(args.raw_dir, args.min_year)
    df = _load_match_frames(args.raw_dir, args.min_year)
    engine = create_engine(f"sqlite:///{args.db_path}")
    df.to_sql("wta_matches", engine, if_exists="replace", index=False)
    n_qual = sum(1 for f in files if "qual_itf" in os.path.basename(f).lower())
    n_main = len(files) - n_qual
    print(
        f"Ingest WTA OK - {len(df)} lignes -> wta_matches ({args.db_path}) "
        f"[{n_main} fichiers main + {n_qual} qual_itf, min_year={args.min_year}]"
    )


if __name__ == "__main__":
    main()
