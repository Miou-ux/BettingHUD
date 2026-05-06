import sqlite3
from typing import Dict, Any

import pandas as pd


KEY_COLUMNS = [
    "tourney_date",
    "surface",
    "winner_name",
    "loser_name",
    "winner_rank",
    "loser_rank",
    "winner_age",
    "loser_age",
    "winner_rank_points",
    "loser_rank_points",
    "minutes",
]


def run_data_quality_checks(db_path: str = "data/bettinghud.db") -> Dict[str, Any]:
    conn = sqlite3.connect(db_path)
    df = pd.read_sql(
        """
        SELECT *
        FROM matches_recent
        WHERE source='tennismylife'
        """,
        conn,
    )
    conn.close()

    if df.empty:
        return {
            "summary": pd.DataFrame([{"metric": "rows", "value": 0}]),
            "null_rates": pd.DataFrame(columns=["column", "null_rate_pct"]),
            "id_coverage": pd.DataFrame(columns=["metric", "value"]),
            "outliers": pd.DataFrame(columns=["metric", "count"]),
        }

    rows = len(df)
    df["tourney_date"] = pd.to_datetime(df["tourney_date"], errors="coerce")

    null_rates = (
        df[KEY_COLUMNS]
        .isna()
        .mean()
        .mul(100.0)
        .reset_index()
        .rename(columns={"index": "column", 0: "null_rate_pct"})
        .sort_values("null_rate_pct", ascending=False)
        .reset_index(drop=True)
    )

    winner_id_missing = float(df["winner_id"].isna().mean() * 100.0)
    loser_id_missing = float(df["loser_id"].isna().mean() * 100.0)
    both_ids_missing = float(((df["winner_id"].isna()) & (df["loser_id"].isna())).mean() * 100.0)

    id_coverage = pd.DataFrame(
        [
            {"metric": "winner_id_missing_pct", "value": round(winner_id_missing, 3)},
            {"metric": "loser_id_missing_pct", "value": round(loser_id_missing, 3)},
            {"metric": "both_ids_missing_pct", "value": round(both_ids_missing, 3)},
        ]
    )

    minutes = pd.to_numeric(df["minutes"], errors="coerce")
    outliers = pd.DataFrame(
        [
            {"metric": "minutes_gt_240", "count": int((minutes > 240).sum())},
            {"metric": "minutes_gt_300", "count": int((minutes > 300).sum())},
            {"metric": "minutes_missing", "count": int(minutes.isna().sum())},
            {"metric": "rank_missing_any", "count": int((df["winner_rank"].isna() | df["loser_rank"].isna()).sum())},
        ]
    )

    min_date = df["tourney_date"].min()
    max_date = df["tourney_date"].max()
    summary = pd.DataFrame(
        [
            {"metric": "rows", "value": rows},
            {"metric": "date_min", "value": str(min_date.date()) if pd.notna(min_date) else "NA"},
            {"metric": "date_max", "value": str(max_date.date()) if pd.notna(max_date) else "NA"},
            {"metric": "unique_tournaments", "value": int(df["tourney_name"].nunique())},
            {"metric": "unique_surfaces", "value": int(df["surface"].nunique())},
        ]
    )

    return {
        "summary": summary,
        "null_rates": null_rates,
        "id_coverage": id_coverage,
        "outliers": outliers,
    }

