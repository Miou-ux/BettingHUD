#!/usr/bin/env python3
"""Repair remaining CourtAlphaX tweets (posts 8-9) with delay."""
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.courtalphax_repair_tweets import (  # noqa: E402
    _fix_bet_on,
    _rebuild_daily_pick_tweet,
    _rebuild_weekly_recap_tweet,
)
from scripts.x_client import delete_tweet, post_tweet, x_posting_enabled  # noqa: E402

POST_IDS = (8, 9)


def main() -> int:
    if not x_posting_enabled():
        raise SystemExit("COURTALPHAX_X_ENABLED=1 required")
    conn = sqlite3.connect("data/bettinghud.db")
    for pid in POST_IDS:
        row = conn.execute(
            "SELECT id, post_type, calendar_date, bet_id, tweet_id FROM courtalphax_x_posts WHERE id = ?",
            (pid,),
        ).fetchone()
        if not row:
            print(f"skip #{pid}: not found")
            continue
        _, post_type, cal_date, bet_id, tweet_id = row
        if post_type == "daily_pick" and bet_id:
            _fix_bet_on(conn, int(bet_id))
            text = _rebuild_daily_pick_tweet(conn, pid, int(bet_id))
        elif post_type == "weekly_recap":
            text = _rebuild_weekly_recap_tweet(conn, str(cal_date))
        else:
            print(f"skip #{pid}: unsupported {post_type}")
            continue
        if not text:
            print(f"fail #{pid}: rebuild failed")
            continue
        print(f"repair #{pid} ({post_type})...")
        if tweet_id:
            try:
                delete_tweet(str(tweet_id), dry_run=False)
                print(f"  deleted {tweet_id}")
            except Exception as exc:
                print(f"  delete warn: {exc}")
        time.sleep(5)
        posted = post_tweet(text, dry_run=False)
        new_id = posted.get("tweet_id")
        conn.execute(
            "UPDATE courtalphax_x_posts SET tweet_id = ?, tweet_text = ? WHERE id = ?",
            (str(new_id or "") or None, text, pid),
        )
        conn.commit()
        print(f"  posted {new_id}")
        time.sleep(10)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
