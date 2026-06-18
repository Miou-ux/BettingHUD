#!/usr/bin/env python3
"""Enrichit le delta WTA avec stats de service (Flashscore).

Sources (probe 2026-06-17) — voir aussi ``scripts/_wta_serve_stats_sources.md`` :
- **Flashscore** (retenu) : feeds ``f_2_{offset}_3_en_1`` + ``df_st_1_{match_id}`` via
  ``global.flashscore.ninja`` + header ``x-fsign: SW9D1eZo``. Couvre aces/svpt/1stIn/…
- Tennis Explorer : découverte d'URL OK, mais pages WTA match-detail sans table stats serve.
- tennisdata.app / SofaScore / tennisabstract : pas d'automatisation fiable gratuite.

Ne modifie que ``--work-dir``. Rate limit configurable (~1,5–2 s entre matchs).

Usage :
  python scripts/enrich_wta_delta_te_stats.py --work-dir data/preprod/wta_work/tennis_wta
  python scripts/enrich_wta_delta_te_stats.py --max-matches 30
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.wta_flashscore_serve_stats import enrich_batch  # noqa: E402
from scripts.wta_sackmann_common import DEFAULT_CUTOFF, dedup_key, parse_yyyymmdd  # noqa: E402


def _needs_enrich(row: pd.Series) -> bool:
    v = row.get("w_svpt")
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == ""


def _load_delta_rows(work_dir: Path, cutoff: int) -> tuple[list[Path], pd.DataFrame]:
    paths = [p for p in sorted(work_dir.glob("wta_matches*.csv")) if "doubles" not in p.name.lower()]
    frames = []
    for p in paths:
        df = pd.read_csv(p, low_memory=False)
        df["_path"] = str(p)
        frames.append(df)
    if not frames:
        return paths, pd.DataFrame()
    all_df = pd.concat(frames, ignore_index=True)
    all_df["_td"] = all_df["tourney_date"].apply(parse_yyyymmdd)
    delta = all_df[(all_df["_td"] >= cutoff) & all_df.apply(_needs_enrich, axis=1)].copy()
    return paths, delta


def enrich_work_dir(
    work_dir: Path,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    max_matches: int | None = None,
    delay_s: float = 1.75,
) -> dict:
    _, delta = _load_delta_rows(work_dir, cutoff)
    if delta.empty:
        return {"enriched": 0, "skipped": 0, "failed": 0, "message": "aucune ligne delta sans stats"}

    if max_matches:
        delta = delta.head(max_matches)

    rows = []
    for _, row in delta.iterrows():
        rows.append(
            {
                "winner_name": row["winner_name"],
                "loser_name": row["loser_name"],
                "tourney_date": int(row["_td"]),
                "dedup_key": dedup_key(row),
            }
        )

    updates, summary = enrich_batch(rows, delay_s=delay_s)
    if not updates:
        return {"enriched": 0, "skipped": len(delta), "failed": summary["failed"]}

    for p in sorted(work_dir.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        df = pd.read_csv(p, low_memory=False)
        changed = 0
        for i, row in df.iterrows():
            k = dedup_key(row)
            if k not in updates:
                continue
            for col, val in updates[k].items():
                if val is not None:
                    df.at[i, col] = val
            changed += 1
        if changed:
            df.to_csv(p, index=False)

    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Enrichir delta WTA via Flashscore serve stats.")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "preprod" / "wta_work" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--max-matches", type=int, default=None)
    ap.add_argument("--delay-s", type=float, default=1.75)
    args = ap.parse_args(argv)
    stats = enrich_work_dir(
        Path(args.work_dir),
        cutoff=args.cutoff_date,
        max_matches=args.max_matches,
        delay_s=args.delay_s,
    )
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
