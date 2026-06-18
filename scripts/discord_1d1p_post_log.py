"""Journal SQLite des posts Discord 1 Day 1 Pick (évite doublons)."""
from __future__ import annotations

import sqlite3
from typing import Any

from scripts.bets_db import DB_PATH_DEFAULT


def ensure_discord_1d1p_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_1d1p_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_type TEXT NOT NULL,
            calendar_date TEXT,
            pick_key TEXT,
            discord_message_id TEXT,
            message_preview TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_1d1p_daily
        ON discord_1d1p_posts(post_type, calendar_date)
        WHERE post_type IN ('daily_pick', 'no_pick') AND calendar_date IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_1d1p_result
        ON discord_1d1p_posts(post_type, pick_key)
        WHERE post_type = 'result' AND pick_key IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_discord_1d1p_perf_board
        ON discord_1d1p_posts(post_type)
        WHERE post_type = 'performance_board'
        """
    )
    conn.commit()


def get_performance_board(conn: sqlite3.Connection) -> dict[str, Any] | None:
    ensure_discord_1d1p_schema(conn)
    row = conn.execute(
        """
        SELECT id, discord_message_id, calendar_date, message_preview
        FROM discord_1d1p_posts
        WHERE post_type = 'performance_board'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "discord_message_id": str(row[1] or ""),
        "calendar_date": str(row[2] or ""),
        "message_preview": str(row[3] or ""),
    }


def upsert_performance_board(
    conn: sqlite3.Connection,
    *,
    discord_message_id: str,
    calendar_date: str,
    message_preview: str = "",
) -> None:
    ensure_discord_1d1p_schema(conn)
    row = get_performance_board(conn)
    if row:
        conn.execute(
            """
            UPDATE discord_1d1p_posts
            SET discord_message_id = ?, calendar_date = ?, message_preview = ?,
                created_at = datetime('now')
            WHERE id = ?
            """,
            (discord_message_id, calendar_date, message_preview, int(row["id"])),
        )
    else:
        conn.execute(
            """
            INSERT INTO discord_1d1p_posts
            (post_type, calendar_date, discord_message_id, message_preview)
            VALUES ('performance_board', ?, ?, ?)
            """,
            (calendar_date, discord_message_id, message_preview),
        )
    conn.commit()


def has_daily_post_for_date(conn: sqlite3.Connection, calendar_date: str) -> bool:
    ensure_discord_1d1p_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM discord_1d1p_posts
        WHERE post_type IN ('daily_pick', 'no_pick') AND calendar_date = ?
        LIMIT 1
        """,
        (calendar_date,),
    ).fetchone()
    return row is not None


def has_result_post_for_pick(conn: sqlite3.Connection, pick_key: str) -> bool:
    ensure_discord_1d1p_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM discord_1d1p_posts
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
    discord_message_id: str | None = None,
    message_preview: str | None = None,
) -> None:
    ensure_discord_1d1p_schema(conn)
    conn.execute(
        """
        INSERT INTO discord_1d1p_posts
        (post_type, calendar_date, pick_key, discord_message_id, message_preview)
        VALUES (?, ?, ?, ?, ?)
        """,
        (post_type, calendar_date, pick_key, discord_message_id, message_preview),
    )
    conn.commit()


def fetch_daily_pick_keys_pending_result(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Picks du jour publiés sur Discord mais sans tweet résultat."""
    ensure_discord_1d1p_schema(conn)
    rows = conn.execute(
        """
        SELECT d.calendar_date, d.pick_key, d.message_preview
        FROM discord_1d1p_posts d
        WHERE d.post_type = 'daily_pick'
          AND d.pick_key IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM discord_1d1p_posts r
            WHERE r.post_type = 'result' AND r.pick_key = d.pick_key
          )
        ORDER BY d.calendar_date ASC
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for cal, pick_key, preview in rows:
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
        status = str(pick.get("status") or "En cours")
        if status == "En cours":
            continue
        out.append(pick)
    return out
