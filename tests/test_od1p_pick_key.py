"""Tests od1p_pick_key — résolution 1D1P vs Top5."""
from __future__ import annotations

import sqlite3

from scripts.od1p_pick_key import (
    fetch_od1p_pending_results,
    od1p_post_pick_key,
    resolve_od1p_pick_for_result,
)


def _mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE daily_top_proba_picks (
            pick_key TEXT PRIMARY KEY,
            calendar_date TEXT,
            fav_player TEXT,
            underdog_player TEXT,
            player1 TEXT,
            player2 TEXT,
            match_name TEXT,
            tour TEXT,
            status TEXT
        );
        CREATE TABLE discord_1d1p_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_type TEXT,
            calendar_date TEXT,
            pick_key TEXT,
            message_preview TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        """
        INSERT INTO daily_top_proba_picks VALUES
        ('2026-07-21|WTA|01', '2026-07-21', 'Tararudee L.', 'Kovackova J.',
         'Tararudee L.', 'Kovackova J.', 'Tararudee L. vs Kovackova J.', 'WTA', 'Gagné'),
        ('2026-07-21|WTA|02', '2026-07-21', 'Parry D. (7)', 'Hontama M.',
         'Parry D. (7)', 'Hontama M.', 'Parry D. (7) vs Hontama M.', 'WTA', 'Perdu')
        """
    )
    conn.execute(
        """
        INSERT INTO discord_1d1p_posts (post_type, calendar_date, pick_key, message_preview)
        VALUES ('daily_pick', '2026-07-21', '2026-07-21|WTA|01', 'Parry D. (7) vs Hontama M.')
        """
    )
    conn.commit()
    return conn


def test_od1p_post_pick_key():
    assert od1p_post_pick_key("2026-07-21") == "2026-07-21|1D1P"


def test_resolve_by_preview_not_top5_key():
    conn = _mem_db()
    pick = resolve_od1p_pick_for_result(
        conn,
        calendar_date="2026-07-21",
        message_preview="Parry D. (7) vs Hontama M.",
    )
    assert pick is not None
    assert pick["fav_player"] == "Parry D. (7)"
    assert pick["status"] == "Perdu"


def test_pending_results_use_preview():
    conn = _mem_db()
    pending = fetch_od1p_pending_results(conn, posts_table="discord_1d1p_posts")
    assert len(pending) == 1
    post_key, pick = pending[0]
    assert post_key == "2026-07-21|WTA|01"
    assert pick["fav_player"] == "Parry D. (7)"
