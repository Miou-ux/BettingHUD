"""tennis-data.co.uk Excel files as a 3rd resolution source.

The same files downloaded for backtesting (data/raw/tennis_data/{year}.xlsx
and data/raw/tennis_data_wta/{year}.xlsx) contain authoritative Winner/Loser
columns published weekly. We reuse them to cross-check Tennis Explorer +
Sackmann results during the weekly reconciliation pass.

For the current year, we re-download the file if older than 24h so the
reconciliation always runs against the latest weekly export.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd
import requests

LOGGER = logging.getLogger(__name__)

CACHE_REFRESH_SECONDS = 24 * 3600  # re-download current-year files every 24h

_BASE_DIRS = {
    "ATP": os.path.join("data", "raw", "tennis_data"),
    "WTA": os.path.join("data", "raw", "tennis_data_wta"),
}
_BASE_URLS = {
    "ATP": "http://www.tennis-data.co.uk/{year}/{year}.xlsx",
    "WTA": "http://www.tennis-data.co.uk/{year}w/{year}.xlsx",
}


def _file_path(tour: str, year: int) -> str:
    return os.path.join(_BASE_DIRS[tour.upper()], f"{year}.xlsx")


def _is_current_year(year: int) -> bool:
    return year == datetime.now().year


def _refresh_if_stale(tour: str, year: int) -> str | None:
    """Re-download current-year files if stale. Returns local path or None."""
    tour = tour.upper()
    path = _file_path(tour, year)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path) and not _is_current_year(year):
        return path  # historical files never change
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < CACHE_REFRESH_SECONDS:
            return path
    url = _BASE_URLS[tour].format(year=year)
    try:
        LOGGER.info("Re-downloading tennis-data %s %d from %s", tour, year, url)
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            LOGGER.warning("tennis-data %s %d HTTP %d", tour, year, r.status_code)
            return path if os.path.exists(path) else None
        with open(path, "wb") as fp:
            fp.write(r.content)
        return path
    except Exception as exc:
        LOGGER.warning("tennis-data fetch failed: %s", exc)
        return path if os.path.exists(path) else None


def load_recent_results(
    target_dates: Iterable[str], *, lookback_days: int = 14
) -> list[dict]:
    """Return list of {tour, match_date, winner_name, loser_name, score} rows
    covering the requested dates (with a small lookback buffer).
    """
    dates_norm: list[datetime] = []
    for d in target_dates:
        try:
            dates_norm.append(datetime.strptime(d, "%Y-%m-%d"))
        except Exception:
            continue
    if not dates_norm:
        return []
    earliest = min(dates_norm) - timedelta(days=lookback_days)
    latest = max(dates_norm) + timedelta(days=1)
    years = sorted({d.year for d in dates_norm} | {earliest.year, latest.year})

    out: list[dict] = []
    for tour in ("ATP", "WTA"):
        for year in years:
            path = _refresh_if_stale(tour, year)
            if not path or not os.path.exists(path):
                continue
            try:
                sheets = pd.read_excel(path, sheet_name=None)
            except Exception as exc:
                LOGGER.warning("read_excel failed for %s: %s", path, exc)
                continue
            frames = [
                f for f in sheets.values() if isinstance(f, pd.DataFrame) and len(f)
            ]
            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True, sort=False)
            cols = {c.lower(): c for c in df.columns}
            if "winner" not in cols or "loser" not in cols or "date" not in cols:
                continue
            dser = pd.to_datetime(df[cols["date"]], errors="coerce", dayfirst=True)
            df = df.assign(_date=dser)
            df = df[df["_date"].between(earliest, latest)]
            if df.empty:
                continue
            score_col = None
            for cand in ("comment", "score"):
                if cand in cols:
                    score_col = cols[cand]
                    break
            for _, row in df.iterrows():
                w = str(row[cols["winner"]] or "").strip()
                l = str(row[cols["loser"]] or "").strip()
                if not w or not l:
                    continue
                # tennis-data publishes a "Comment" column when retired/walkover
                comment = ""
                if score_col:
                    comment = str(row.get(score_col, "") or "").strip()
                out.append(
                    {
                        "tour": tour,
                        "match_date": pd.Timestamp(row["_date"]).strftime("%Y-%m-%d"),
                        "winner_name": w,
                        "loser_name": l,
                        "score": comment,
                    }
                )
    return out
