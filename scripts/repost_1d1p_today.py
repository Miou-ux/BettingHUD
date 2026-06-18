#!/usr/bin/env python3
"""Delete today's 1D1P posts on Discord (+ TG channel if message_id known) and repost."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")


def _delete_discord_webhook_message(webhook_url: str, message_id: str) -> bool:
    mid = str(message_id or "").strip()
    if not mid:
        return False
    base = webhook_url.rstrip("/")
    url = f"{base}/messages/{mid}"
    resp = requests.delete(url, timeout=30)
    if resp.status_code in (200, 204, 404):
        return True
    resp.raise_for_status()
    return True


def _delete_telegram_message(token: str, chat_id: str, message_id: str) -> bool:
    mid = str(message_id or "").strip()
    if not mid:
        return False
    url = f"https://api.telegram.org/bot{token}/deleteMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "message_id": int(mid)}, timeout=30)
    data = resp.json()
    return bool(data.get("ok"))


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Delete + repost (default dry-run)")
    args = ap.parse_args()

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from scripts.bets_db import DB_PATH_DEFAULT, ensure_bets_meta, get_meta, set_meta
    from scripts.discord_1d1p_notify import run_daily_pick
    from scripts.discord_1d1p_post_log import ensure_discord_1d1p_schema

    paris = ZoneInfo("Europe/Paris")
    today = datetime.now(paris).date().isoformat()
    db_path = DB_PATH_DEFAULT

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_discord_1d1p_schema(conn)
    ensure_bets_meta(conn)

    discord_rows = conn.execute(
        """
        SELECT id, post_type, discord_message_id, message_preview
        FROM discord_1d1p_posts
        WHERE calendar_date = ? AND post_type IN ('daily_pick', 'no_pick')
        """,
        (today,),
    ).fetchall()

    tg_msg_key = f"tg_channel_daily_msg_{today}"  # META_DAILY_MSG_PREFIX + date
    tg_sent_key = f"tg_channel_daily_{today}"
    tg_msg_id = get_meta(conn, tg_msg_key)

    webhook = (os.getenv("DISCORD_1D1P_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    tg_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    tg_channel = (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()

    print(f"today={today} apply={args.apply}")
    print(f"discord posts to delete: {len(discord_rows)}")
    for r in discord_rows:
        print(f"  id={r['id']} type={r['post_type']} msg={r['discord_message_id']} preview={r['message_preview']}")
    print(f"tg channel msg_id meta: {tg_msg_id or '(none)'}")

    if not args.apply:
        print("\n(dry-run — pass --apply to delete and repost)")
        conn.close()
        return 0

    for r in discord_rows:
        mid = str(r["discord_message_id"] or "")
        if webhook and mid:
            try:
                _delete_discord_webhook_message(webhook, mid)
                print(f"discord deleted message {mid}")
            except Exception as exc:
                print(f"discord delete warn {mid}: {exc}")
        conn.execute("DELETE FROM discord_1d1p_posts WHERE id = ?", (int(r["id"]),))

    if tg_token and tg_channel and tg_msg_id:
        try:
            if _delete_telegram_message(tg_token, tg_channel, tg_msg_id):
                print(f"telegram channel deleted message {tg_msg_id}")
            else:
                print(f"telegram delete failed for {tg_msg_id}")
        except Exception as exc:
            print(f"telegram delete warn: {exc}")

    set_meta(conn, tg_sent_key, "")
    set_meta(conn, tg_msg_key, "")
    conn.commit()
    conn.close()

    discord_out = run_daily_pick(dry_run=False, force=True)
    print("discord repost:", discord_out)

    from scripts.telegram_channel_notify import run_channel_notify

    tg_out = run_channel_notify(dry_run=False, force=True)
    print("telegram channel repost:", tg_out)

    # Private bot chat (TELEGRAM_CHAT_ID) — interactive /1pick1day
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if chat_id:
        from scripts.telegram_top5_notify import run_1d1p_notify

        bot_msg_key = f"tg_bot_1d1p_msg_{today}"
        conn2 = sqlite3.connect(db_path)
        try:
            ensure_bets_meta(conn2)
            old_bot_msg = get_meta(conn2, bot_msg_key)
            if tg_token and old_bot_msg:
                try:
                    if _delete_telegram_message(tg_token, chat_id, old_bot_msg):
                        print(f"telegram bot deleted message {old_bot_msg}")
                except Exception as exc:
                    print(f"telegram bot delete warn: {exc}")
            set_meta(conn2, bot_msg_key, "")
            conn2.commit()
        finally:
            conn2.close()

        bot_out = run_1d1p_notify(
            chat_id=chat_id,
            source="repost",
            interactive=True,
            telegram_user_id=chat_id,
        )
        print("telegram bot /1pick1day:", bot_out)
    else:
        print("telegram bot: TELEGRAM_CHAT_ID absent — skip")

    return 0 if discord_out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
