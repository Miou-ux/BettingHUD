"""Tests résolution picks algo sans paris portefeuille."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from scripts.bets_db import ensure_daily_top_proba_schema
from scripts.scraper_results import expand_resolution_target_dates, open_algo_resolution_dates


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_daily_top_proba_schema(conn)
    return conn


def test_open_algo_resolution_dates_from_top_proba():
    conn = _mem_db()
    base = (
        "pick_key, calendar_date, match_date, tour, rank, match_name, fav_player, status"
    )
    conn.execute(
        f"INSERT INTO daily_top_proba_picks ({base}) "
        "VALUES ('k1', '2026-07-22', '2026-07-22', 'ATP', 1, 'A vs B', 'A', 'En cours')"
    )
    conn.execute(
        f"INSERT INTO daily_top_proba_picks ({base}) "
        "VALUES ('k2', '2026-07-21', '2026-07-21', 'ATP', 1, 'C vs D', 'C', 'Gagné')"
    )
    conn.commit()
    assert open_algo_resolution_dates(conn) == {"2026-07-22"}


def test_expand_resolution_target_dates_adds_nearby():
    today = date(2026, 7, 22)
    cutoff = today - timedelta(days=7)
    upper = today + timedelta(days=3)
    out = expand_resolution_target_dates(
        {"2026-07-22"},
        cutoff=cutoff,
        today=today,
        lookup_upper=upper,
    )
    assert "2026-07-22" in out
    assert "2026-07-21" in out
    assert "2026-07-25" in out
