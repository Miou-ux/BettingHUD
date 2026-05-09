"""Sackmann GitHub raw-CSV fallback for resolving recent matches.

Sackmann publishes ATP/WTA match data daily on:
- https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv
- https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv

Each row contains tourney_date (YYYYMMDD), winner_name, loser_name, score.
We use this as a high-trust fallback when Tennis Explorer scraping fails or
when a bet has been pending more than ~24h (Sackmann lags ~24-48h vs live).

The fetcher caches the CSV in `data/cache/sackmann_{tour}_{year}.csv` with a
TTL of 6h to avoid hammering GitHub.
"""
from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Iterable

import requests

LOGGER = logging.getLogger(__name__)

CACHE_DIR = os.path.join("data", "cache")
CACHE_TTL_SECONDS = 6 * 3600  # 6 hours

_BASE_URLS = {
    "ATP": "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv",
    "WTA": "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv",
}


def _cache_path(tour: str, year: int) -> str:
    return os.path.join(CACHE_DIR, f"sackmann_{tour.lower()}_{year}.csv")


def _is_cache_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    age = time.time() - os.path.getmtime(path)
    return age < CACHE_TTL_SECONDS


def fetch_sackmann_csv(tour: str, year: int, *, force: bool = False) -> str | None:
    """Download the Sackmann CSV for {tour, year}. Returns local path or None."""
    tour = tour.upper()
    if tour not in _BASE_URLS:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(tour, year)
    if not force and _is_cache_fresh(path):
        return path
    url = _BASE_URLS[tour].format(year=year)
    try:
        LOGGER.info("Fetching Sackmann %s %d from %s", tour, year, url)
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            LOGGER.warning("Sackmann %s %d returned HTTP %d", tour, year, r.status_code)
            # keep stale cache rather than nothing
            return path if os.path.exists(path) else None
        with open(path, "w", encoding="utf-8", newline="") as fp:
            fp.write(r.text)
        return path
    except Exception as exc:
        LOGGER.warning("Sackmann fetch failed for %s %d: %s", tour, year, exc)
        return path if os.path.exists(path) else None


def load_recent_results(
    target_dates: Iterable[str], *, lookback_days: int = 14
) -> list[dict]:
    """Load rows from the Sackmann CSVs covering the target dates (and a small
    buffer). Returns a list of dicts with keys:
        tour, match_date (YYYY-MM-DD), winner_name, loser_name, score
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
            path = fetch_sackmann_csv(tour, year)
            if not path or not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", newline="") as fp:
                    reader = csv.DictReader(fp)
                    for row in reader:
                        td = row.get("tourney_date", "")
                        if not td or len(td) < 8:
                            continue
                        try:
                            d = datetime.strptime(td[:8], "%Y%m%d")
                        except Exception:
                            continue
                        if d < earliest or d > latest:
                            continue
                        winner = (row.get("winner_name") or "").strip()
                        loser = (row.get("loser_name") or "").strip()
                        if not winner or not loser:
                            continue
                        out.append(
                            {
                                "tour": tour,
                                "match_date": d.strftime("%Y-%m-%d"),
                                "winner_name": winner,
                                "loser_name": loser,
                                "score": (row.get("score") or "").strip(),
                            }
                        )
            except Exception as exc:
                LOGGER.warning("Failed reading %s: %s", path, exc)
                continue
    return out
