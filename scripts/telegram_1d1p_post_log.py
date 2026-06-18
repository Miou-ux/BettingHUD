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
    """Picks publiés sur Telegram mais sans message résultat."""
    ensure_telegram_1d1p_schema(conn)
    rows = conn.execute(
        """
        SELECT d.calendar_date, d.pick_key
        FROM telegram_1d1p_posts d
        WHERE d.post_type = 'daily_pick'
          AND d.pick_key IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM telegram_1d1p_posts r
            WHERE r.post_type = 'result' AND r.pick_key = d.pick_key
          )
        ORDER BY d.calendar_date ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for _cal, pick_key in rows:
        row = conn.execute(
            """
            SELECT *
            FROM daily_top_proba_picks
            WHERE pick_key = ?
            LIMIT 1
            """,
            (pick_key,),
        ).fetchone()
        if not row:
            continue
        pick = dict(row)
        if str(pick.get("status") or "En cours") == "En cours":
            continue
        out.append(pick)
    return out
