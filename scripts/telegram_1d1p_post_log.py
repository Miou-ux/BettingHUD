"""Journal SQLite des posts Telegram 1 Day 1 Pick (évite doublons)."""
from __future__ import annotations

import sqlite3
from typing import Any


def ensure_telegram_1d1p_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_1d1p_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_type TEXT NOT NULL,
            calendar_date TEXT,
            pick_key TEXT,
            telegram_message_id TEXT,
            message_preview TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_1d1p_daily
        ON telegram_1d1p_posts(post_type, calendar_date)
        WHERE post_type IN ('daily_pick', 'no_pick') AND calendar_date IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_1d1p_result
        ON telegram_1d1p_posts(post_type, pick_key)
        WHERE post_type = 'result' AND pick_key IS NOT NULL
        """
    )
    conn.commit()


def has_daily_post_for_date(conn: sqlite3.Connection, calendar_date: str) -> bool:
    ensure_telegram_1d1p_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM telegram_1d1p_posts
        WHERE post_type IN ('daily_pick', 'no_pick') AND calendar_date = ?
        LIMIT 1
        """,
        (calendar_date,),
    ).fetchone()
    return row is not None


def has_result_post_for_pick(conn: sqlite3.Connection, pick_key: str) -> bool:
    ensure_telegram_1d1p_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM telegram_1d1p_posts
        WHERE post_type = 'result' AND pick_key = ?
        LIMIT 1
        """,
        (pick_key,),
    ).fetchone()
    return row is not None


def log_post(
    conn: sqlite3.Connection,
    *,
    post_type: str,
    calendar_date: str | None = None,
    pick_key: str | None = None,
    telegram_message_id: str | None = None,
    message_preview: str | None = None,
) -> None:
    ensure_telegram_1d1p_schema(conn)
    conn.execute(
        """
        INSERT INTO telegram_1d1p_posts
        (post_type, calendar_date, pick_key, telegram_message_id, message_preview)
        VALUES (?, ?, ?, ?, ?)
        """,
        (post_type, calendar_date, pick_key, telegram_message_id, message_preview),
    )
    conn.commit()


def fetch_daily_pick_keys_pending_result(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Picks publiés sur Telegram sans résultat (résolution via match publié)."""
    from scripts.od1p_pick_key import fetch_od1p_pending_results

    ensure_telegram_1d1p_schema(conn)
    pending = fetch_od1p_pending_results(conn, posts_table="telegram_1d1p_posts")
    out: list[dict[str, Any]] = []
    for post_pick_key, pick in pending:
        pick = dict(pick)
        pick["_od1p_post_pick_key"] = post_pick_key
        out.append(pick)
    return out
