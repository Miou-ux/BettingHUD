"""Lignes pick depuis CSV backtest (fiabilité + confiance)."""
from __future__ import annotations

import pandas as pd

from scripts.enrich_backtest_csv_reliability import augment_backtest_dataframe


def augment_csv_pick_fields(df: pd.DataFrame, *, db_path: str | None = None) -> pd.DataFrame:
    """Ajoute/lit book_gap, data_reliability_score, confidence sur un pool backtest."""
    return augment_backtest_dataframe(df, db_path=db_path, recompute_missing=True)


def dataframe_to_pick_rows(df: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    for rec in df.to_dict(orient="records"):
        row = dict(rec)
        rel = row.get("data_reliability_score")
        if rel is not None and pd.notna(rel):
            try:
                row["data_reliability_score"] = int(float(rel))
            except (TypeError, ValueError):
                row["data_reliability_score"] = None
        else:
            row["data_reliability_score"] = None
        conf = row.get("confidence")
        if conf is not None and pd.notna(conf):
            try:
                row["confidence"] = float(conf)
            except (TypeError, ValueError):
                row["confidence"] = abs(float(row.get("p_model_fav") or row.get("p_model") or 0.5) - 0.5) * 2.0
        else:
            pm = float(row.get("p_model_fav") or row.get("p_model") or 0.5)
            row["confidence"] = abs(pm - 0.5) * 2.0
        flags = row.get("data_reliability_flags")
        if flags is not None and pd.isna(flags):
            row["data_reliability_flags"] = None
        rows.append(row)
    return rows
