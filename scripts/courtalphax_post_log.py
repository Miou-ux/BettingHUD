"""Journal SQLite des posts CourtAlphaX (évite doublons)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

DB_PATH_DEFAULT = "data/bettinghud.db"


def ensure_courtalphax_x_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS courtalphax_x_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_type TEXT NOT NULL,
            calendar_date TEXT,
            bet_id INTEGER,
            tweet_id TEXT,
            tweet_text TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cax_daily_pick
        ON courtalphax_x_posts(post_type, calendar_date)
        WHERE post_type = 'daily_pick' AND calendar_date IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cax_result_bet
        ON courtalphax_x_posts(post_type, bet_id)
        WHERE post_type = 'result' AND bet_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cax_no_pick
        ON courtalphax_x_posts(post_type, calendar_date)
        WHERE post_type = 'no_pick' AND calendar_date IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_cax_weekly_recap
        ON courtalphax_x_posts(post_type, calendar_date)
        WHERE post_type = 'weekly_recap' AND calendar_date IS NOT NULL
        """
    )
    conn.commit()


def has_daily_post_for_date(conn: sqlite3.Connection, calendar_date: str) -> bool:
    """Pick du jour ou message « pas de value » déjà publié."""
    ensure_courtalphax_x_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM courtalphax_x_posts
        WHERE post_type IN ('daily_pick', 'no_pick') AND calendar_date = ?
        LIMIT 1
        """,
        (calendar_date,),
    ).fetchone()
    return row is not None


def has_weekly_recap_for_week(conn: sqlite3.Connection, week_key: str) -> bool:
    ensure_courtalphax_x_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM courtalphax_x_posts
        WHERE post_type = 'weekly_recap' AND calendar_date = ?
        LIMIT 1
        """,
        (week_key,),
    ).fetchone()
    return row is not None


def fetch_weekly_no_pick_dates(
    conn: sqlite3.Connection,
    *,
    week_start: str,
    week_end: str,
) -> list[str]:
    ensure_courtalphax_x_schema(conn)
    cur = conn.execute(
        """
        SELECT calendar_date FROM courtalphax_x_posts
        WHERE post_type = 'no_pick'
          AND calendar_date >= ? AND calendar_date <= ?
        ORDER BY calendar_date ASC
        """,
        (week_start, week_end),
    )
    return [str(r[0]) for r in cur.fetchall() if r[0]]


def fetch_weekly_courtalphax_bets(
    conn: sqlite3.Connection,
    *,
    telegram_user_id: str,
    tracker_source: str,
    week_start: str,
    week_end: str,
) -> list[dict[str, Any]]:
    ensure_courtalphax_x_schema(conn)
    cur = conn.execute(
        """
        SELECT b.id, b.bet_on, b.odds, b.stake, b.status, b.profit,
               COALESCE(NULLIF(TRIM(b.match_date), ''), b.date) AS sched,
               b.tournament, b.tour
        FROM user_bets b
        WHERE b.telegram_user_id = ?
          AND COALESCE(TRIM(b.tracker_source), '') = ?
          AND COALESCE(NULLIF(TRIM(b.match_date), ''), b.date) >= ?
          AND COALESCE(NULLIF(TRIM(b.match_date), ''), b.date) <= ?
        ORDER BY sched ASC, b.id ASC
        """,
        (
            str(telegram_user_id).strip(),
            str(tracker_source).strip(),
            week_start,
            week_end,
        ),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def has_daily_pick_for_date(conn: sqlite3.Connection, calendar_date: str) -> bool:
    ensure_courtalphax_x_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM courtalphax_x_posts
        WHERE post_type = 'daily_pick' AND calendar_date = ?
        LIMIT 1
        """,
        (calendar_date,),
    ).fetchone()
    return row is not None


def has_result_post_for_bet(conn: sqlite3.Connection, bet_id: int) -> bool:
    ensure_courtalphax_x_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM courtalphax_x_posts
        WHERE post_type = 'result' AND bet_id = ?
        LIMIT 1
        """,
        (int(bet_id),),
    ).fetchone()
    return row is not None


def log_post(
    conn: sqlite3.Connection,
    *,
    post_type: str,
    calendar_date: str | None = None,
    bet_id: int | None = None,
    tweet_id: str | None = None,
    tweet_text: str | None = None,
) -> int:
    ensure_courtalphax_x_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO courtalphax_x_posts
            (post_type, calendar_date, bet_id, tweet_id, tweet_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(post_type),
            calendar_date,
            int(bet_id) if bet_id is not None else None,
            str(tweet_id or "") or None,
            tweet_text,
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def try_claim_result_post(conn: sqlite3.Connection, bet_id: int) -> bool:
    """Réserve un pari pour un tweet résultat (1 seul envoi même si cron en double)."""
    ensure_courtalphax_x_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO courtalphax_x_posts (post_type, bet_id, created_at)
        SELECT 'result', ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM courtalphax_x_posts
            WHERE post_type = 'result' AND bet_id = ?
        )
        """,
        (int(bet_id), datetime.utcnow().isoformat(timespec="seconds"), int(bet_id)),
    )
    conn.commit()
    return int(cur.rowcount or 0) == 1


def finalize_result_post(
    conn: sqlite3.Connection,
    *,
    bet_id: int,
    tweet_id: str | None,
    tweet_text: str,
) -> None:
    conn.execute(
        """
        UPDATE courtalphax_x_posts
        SET tweet_id = ?, tweet_text = ?
        WHERE post_type = 'result' AND bet_id = ?
        """,
        (str(tweet_id or "") or None, tweet_text, int(bet_id)),
    )
    conn.commit()


def release_result_post_claim(conn: sqlite3.Connection, bet_id: int) -> None:
    """Annule la réservation si l'envoi X a échoué (permet un retry cron)."""
    conn.execute(
        """
        DELETE FROM courtalphax_x_posts
        WHERE post_type = 'result' AND bet_id = ?
          AND (tweet_id IS NULL OR TRIM(tweet_id) = '')
        """,
        (int(bet_id),),
    )
    conn.commit()


def count_daily_picks(conn: sqlite3.Connection) -> int:
    ensure_courtalphax_x_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) FROM courtalphax_x_posts WHERE post_type = 'daily_pick'"
    ).fetchone()
    return int(row[0] or 0)


def fetch_bets_pending_result_post(
    conn: sqlite3.Connection,
    *,
    telegram_user_id: str,
    tracker_source: str,
) -> list[dict[str, Any]]:
    """Paris réglés sans tweet résultat publié."""
    ensure_courtalphax_x_schema(conn)
    cur = conn.execute(
        """
        SELECT b.id, b.match_name, b.bet_on, b.odds, b.stake, b.status, b.profit,
               COALESCE(NULLIF(TRIM(b.match_date), ''), b.date) AS sched,
               b.tournament, b.tour
        FROM user_bets b
        WHERE b.telegram_user_id = ?
          AND COALESCE(TRIM(b.tracker_source), '') = ?
          AND COALESCE(TRIM(b.status), '') IN ('Gagné', 'Perdu')
          AND NOT EXISTS (
              SELECT 1 FROM courtalphax_x_posts p
              WHERE p.post_type = 'result' AND p.bet_id = b.id
          )
        ORDER BY b.id ASC
        """,
        (str(telegram_user_id).strip(), str(tracker_source).strip()),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]
