#!/usr/bin/env python3
"""Corrige tweets CourtAlphaX avec « ? vs ? » (delete + repost, X ne permet pas l'édition).

Usage PROD :
  cd /opt/bettinghud && ./venv/bin/python3 scripts/courtalphax_repair_tweets.py --dry-run
  cd /opt/bettinghud && ./venv/bin/python3 scripts/courtalphax_repair_tweets.py
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.bets_db import compute_telegram_user_bankroll_eur  # noqa: E402
from scripts.courtalphax_config import (  # noqa: E402
    COURTALPHAX_START_BR_EUR,
    COURTALPHAX_TELEGRAM_USER_ID,
    COURTALPHAX_TRACKER_SOURCE,
)
from scripts.courtalphax_format import (  # noqa: E402
    format_daily_pick_tweet,
    format_result_tweet,
    format_weekly_recap_tweet,
)
from scripts.courtalphax_post_log import (  # noqa: E402
    ensure_courtalphax_x_schema,
    fetch_weekly_courtalphax_bets,
    fetch_weekly_no_pick_dates,
)
from scripts.x_client import delete_tweet, post_tweet, truncate_tweet, x_posting_enabled  # noqa: E402

_BAD_NAME = re.compile(r"\?\s*vs\s*\?|— \? @| \? @")


def _lookup_fav_player(conn: sqlite3.Connection, match_name: str, calendar_date: str) -> str | None:
    row = conn.execute(
        """
        SELECT fav_player FROM daily_top_proba_picks
        WHERE calendar_date = ? AND match_name = ?
        LIMIT 1
        """,
        (calendar_date, match_name),
    ).fetchone()
    if row and row[0]:
        return str(row[0]).strip()
    base = match_name.split(" vs ", 1)[0].strip() if " vs " in match_name else match_name
    row = conn.execute(
        """
        SELECT fav_player FROM daily_top_proba_picks
        WHERE calendar_date = ? AND match_name LIKE ?
        LIMIT 1
        """,
        (calendar_date, f"{base}%"),
    ).fetchone()
    return str(row[0]).strip() if row and row[0] else None


def _fix_bet_on(conn: sqlite3.Connection, bet_id: int) -> str | None:
    row = conn.execute(
        "SELECT bet_on, match_name, match_date, date FROM user_bets WHERE id = ?",
        (int(bet_id),),
    ).fetchone()
    if not row:
        return None
    bet_on, match_name, match_date, date = row
    if str(bet_on or "").strip():
        return str(bet_on).strip()
    cal = str(match_date or date or "")[:10]
    fav = _lookup_fav_player(conn, str(match_name or ""), cal)
    if not fav:
        return None
    conn.execute("UPDATE user_bets SET bet_on = ? WHERE id = ?", (fav, int(bet_id)))
    conn.commit()
    return fav


def _rebuild_daily_pick_tweet(conn: sqlite3.Connection, post_id: int, bet_id: int) -> str | None:
    bet = conn.execute(
        """
        SELECT b.bet_on, b.match_name, b.odds, b.stake, b.tour, b.surface, b.tournament,
               b.match_date, b.date, b.notes, b.p_model, b.ev_at_bet,
               p.fav_player, p.underdog_player, p.p_model_fav, p.ev_fav, p.ev_fav_pct,
               p.odd_fav, p.match_time
        FROM user_bets b
        LEFT JOIN daily_top_proba_picks p
          ON p.calendar_date = COALESCE(NULLIF(TRIM(b.match_date), ''), b.date)
         AND p.match_name = b.match_name
        WHERE b.id = ?
        """,
        (int(bet_id),),
    ).fetchone()
    if not bet:
        return None
    (
        bet_on, match_name, odds, stake, tour, surface, tournament,
        match_date, date, notes, p_model, ev_at_bet,
        fav_player, underdog_player, p_model_fav, ev_fav, ev_fav_pct,
        odd_fav, match_time,
    ) = bet
    cal = str(match_date or date or "")[:10]
    pick = {
        "bet_on": bet_on or fav_player,
        "opponent": underdog_player,
        "fav_player": fav_player,
        "underdog_player": underdog_player,
        "odd_fav": odd_fav or odds,
        "p_model_fav": p_model_fav or p_model,
        "ev_fav": ev_fav or ev_at_bet,
        "ev_fav_pct": ev_fav_pct,
        "tour": tour,
        "surface": surface,
        "tournament": tournament,
        "match_time": match_time,
        "match_name": match_name,
    }
    m = re.search(r"#(\d+)", str(notes or ""))
    pick_number = int(m.group(1)) if m else 1
    snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)
    avail = float(snap["available_eur"])
    stake_f = float(stake or 0)
    stake_pct = 100.0 * stake_f / avail if avail > 0 else 0.0
    return truncate_tweet(
        format_daily_pick_tweet(
            pick,
            pick_number=pick_number,
            stake_eur=stake_f,
            stake_pct=stake_pct,
            bankroll_eur=avail,
            calendar_date=cal,
        )
    )


def _rebuild_result_tweet(conn: sqlite3.Connection, bet_id: int) -> str | None:
    _fix_bet_on(conn, bet_id)
    bet = conn.execute(
        """
        SELECT id, bet_on, match_name, odds, stake, status, profit,
               COALESCE(NULLIF(TRIM(match_date), ''), date) AS sched,
               tournament, tour
        FROM user_bets WHERE id = ?
        """,
        (int(bet_id),),
    ).fetchone()
    if not bet:
        return None
    cols = [
        "id", "bet_on", "match_name", "odds", "stake", "status", "profit",
        "sched", "tournament", "tour",
    ]
    bet_d = dict(zip(cols, bet))
    from scripts.bets_db import compute_telegram_user_br_advanced_stats

    stats = compute_telegram_user_br_advanced_stats(conn, COURTALPHAX_TELEGRAM_USER_ID)
    snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)
    roi = stats.get("roi_pct")
    return truncate_tweet(
        format_result_tweet(
            bet_d,
            bankroll_eur=float(snap["available_eur"]),
            start_br_eur=COURTALPHAX_START_BR_EUR,
            wins=int(stats.get("wins") or 0),
            losses=int(stats.get("losses") or 0),
            roi_pct=float(roi) if roi is not None else None,
        )
    )


def _rebuild_weekly_recap_tweet(conn: sqlite3.Connection, week_key: str) -> str | None:
    if "_2026-" not in week_key:
        return None
    week_start, week_end = week_key.split("_", 1)
    for bet in fetch_weekly_courtalphax_bets(
        conn,
        telegram_user_id=COURTALPHAX_TELEGRAM_USER_ID,
        tracker_source=COURTALPHAX_TRACKER_SOURCE,
        week_start=week_start,
        week_end=week_end,
    ):
        _fix_bet_on(conn, int(bet["id"]))
    bets = fetch_weekly_courtalphax_bets(
        conn,
        telegram_user_id=COURTALPHAX_TELEGRAM_USER_ID,
        tracker_source=COURTALPHAX_TRACKER_SOURCE,
        week_start=week_start,
        week_end=week_end,
    )
    no_pick_dates = fetch_weekly_no_pick_dates(conn, week_start=week_start, week_end=week_end)
    snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)
    return truncate_tweet(
        format_weekly_recap_tweet(
            week_start=week_start,
            week_end=week_end,
            bets=bets,
            no_pick_dates=no_pick_dates,
            bankroll_eur=float(snap["available_eur"]),
            start_br_eur=COURTALPHAX_START_BR_EUR,
        )
    )


def repair_bad_tweets(*, dry_run: bool = True) -> dict:
    conn = sqlite3.connect("data/bettinghud.db")
    ensure_courtalphax_x_schema(conn)
    rows = conn.execute(
        """
        SELECT id, post_type, calendar_date, bet_id, tweet_id, tweet_text
        FROM courtalphax_x_posts
        WHERE tweet_text IS NOT NULL
        ORDER BY id ASC
        """
    ).fetchall()
    repaired: list[dict] = []
    skipped: list[dict] = []

    for post_id, post_type, cal_date, bet_id, tweet_id, tweet_text in rows:
        if not _BAD_NAME.search(str(tweet_text or "")):
            continue
        new_text: str | None = None
        if post_type == "daily_pick" and bet_id:
            new_text = _rebuild_daily_pick_tweet(conn, post_id, int(bet_id))
        elif post_type == "result" and bet_id:
            new_text = _rebuild_result_tweet(conn, int(bet_id))
        elif post_type == "weekly_recap" and cal_date:
            new_text = _rebuild_weekly_recap_tweet(conn, str(cal_date))

        if not new_text or _BAD_NAME.search(new_text):
            skipped.append({"post_id": post_id, "post_type": post_type, "reason": "rebuild_failed"})
            continue

        entry = {
            "post_id": post_id,
            "post_type": post_type,
            "old_tweet_id": tweet_id,
            "new_text_preview": new_text[:120],
        }
        if dry_run:
            print(f"[dry-run] post #{post_id} ({post_type})")
            print(new_text)
            print("---")
            repaired.append(entry)
            continue

        if not x_posting_enabled():
            raise SystemExit("COURTALPHAX_X_ENABLED=1 requis (ou --dry-run).")

        if tweet_id:
            delete_tweet(str(tweet_id), dry_run=False)
        posted = post_tweet(new_text, dry_run=False)
        new_id = posted.get("tweet_id")
        conn.execute(
            """
            UPDATE courtalphax_x_posts
            SET tweet_id = ?, tweet_text = ?
            WHERE id = ?
            """,
            (str(new_id or "") or None, new_text, int(post_id)),
        )
        conn.commit()
        entry["new_tweet_id"] = new_id
        repaired.append(entry)

    conn.close()
    return {"repaired": repaired, "skipped": skipped, "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(description="Réparer tweets CourtAlphaX « ? vs ? »")
    ap.add_argument("--dry-run", action="store_true", help="Aperçu sans delete/repost")
    args = ap.parse_args()
    res = repair_bad_tweets(dry_run=args.dry_run)
    print(
        f"OK — {len(res['repaired'])} réparé(s), "
        f"{len(res['skipped'])} ignoré(s)"
        + (" (dry-run)" if args.dry_run else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
