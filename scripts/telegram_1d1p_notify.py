#!/usr/bin/env python3
"""Publication automatique 1 Day 1 Pick sur Telegram (broadcast + résultats).

Variables :
  TELEGRAM_1D1P_ENABLED   — 1 pour activer (défaut : oui si TELEGRAM_TOP5_AFTER_MORNING=1)
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID + chats approuvés (telegram_access)

Usage :
  py -3 scripts/telegram_1d1p_notify.py
  py -3 scripts/telegram_1d1p_notify.py --results
  py -3 scripts/telegram_1d1p_notify.py --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.bets_db import DB_PATH_DEFAULT, ensure_daily_top_proba_schema, open_db  # noqa: E402
from scripts.telegram_1d1p_post_log import (  # noqa: E402
    fetch_daily_pick_keys_pending_result,
    has_daily_post_for_date,
    has_result_post_for_pick,
    log_post,
)


def _pick_key(pick: dict | None) -> str | None:
    if not pick:
        return None
    from scripts.od1p_pick_key import od1p_post_pick_key

    post_key = str(pick.get("_od1p_post_pick_key") or "").strip()
    if post_key:
        return post_key
    cal = str(pick.get("calendar_date") or "")[:10]
    if cal:
        return od1p_post_pick_key(cal)
    return str(
        pick.get("pick_key")
        or f"{pick.get('calendar_date')}|{pick.get('tour')}|{pick.get('rank', 1):02d}"
    )


def telegram_1d1p_enabled() -> bool:
    raw = (os.getenv("TELEGRAM_1D1P_ENABLED") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return os.getenv("TELEGRAM_TOP5_AFTER_MORNING", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _broadcast_chat_ids() -> list[str]:
    from scripts.telegram_access import load_broadcast_chat_ids

    return load_broadcast_chat_ids()


def run_daily_pick(
    *,
    dry_run: bool = False,
    force: bool = False,
    source: str = "morning-sync",
) -> dict:
    if not dry_run and not telegram_1d1p_enabled():
        return {"ok": False, "reason": "disabled"}

    from scripts.pick_modes import PickMode, load_picks
    from scripts.telegram_top5_notify import (
        format_1d1p_interactive_header,
        format_1d1p_telegram_message,
        send_interactive_pick_messages,
        send_telegram_message,
        tg,
        _interactive_footer,
    )

    res = load_picks(PickMode.ONE_PICK_ONE_DAY)
    cal_day = res.calendar_date
    pick = res.pick_today
    from scripts.od1p_pick_key import od1p_post_pick_key

    post_key = od1p_post_pick_key(cal_day)

    conn = open_db(DB_PATH_DEFAULT)
    try:
        ensure_daily_top_proba_schema(conn)
        if not force and has_daily_post_for_date(conn, cal_day):
            return {
                "ok": False,
                "reason": "already_posted",
                "calendar_date": cal_day,
            }

        if dry_run:
            text = format_1d1p_telegram_message(
                pick,
                calendar_date=cal_day,
                pool_size=res.pool_n,
                snapshot_age_min=res.snapshot_age_min,
            )
            print(text)
            return {
                "ok": True,
                "dry_run": True,
                "calendar_date": cal_day,
                "pick_key": post_key,
                "n_picks": 1 if pick else 0,
            }

        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        chats = _broadcast_chat_ids()
        if not token or not chats:
            return {"ok": False, "reason": "missing_token_or_chats"}

        header = format_1d1p_interactive_header(
            calendar_date=cal_day,
            pool_size=res.pool_n,
            source=source,
            n_picks=1 if pick else 0,
            snapshot_age_min=res.snapshot_age_min,
        )
        sent = 0
        if not pick:
            body = (
                f"{header}\n\n"
                f"😴 <i>{tg('No value pick today.', 'Pas de pick value aujourd\'hui.')}</i>"
            )
            for chat_id in chats:
                send_telegram_message(body, token=token, chat_id=chat_id)
                sent += 1
            preview = "no_pick"
            log_post(
                conn,
                post_type="no_pick",
                calendar_date=cal_day,
                message_preview=preview,
            )
        else:
            pick_row = dict(pick)
            pick_row.setdefault("rank", 1)
            preview = str(pick.get("match_name") or pick.get("fav_player") or "")[:200]
            for chat_id in chats:
                send_interactive_pick_messages(
                    [pick_row],
                    header_text=header,
                    footer_text=_interactive_footer(),
                    token=token,
                    chat_id=chat_id,
                    list_kind="1d1p",
                    telegram_user_id=str(chat_id),
                )
                sent += 1
            log_post(
                conn,
                post_type="daily_pick",
                calendar_date=cal_day,
                pick_key=post_key,
                message_preview=preview,
            )
            from scripts.published_picks_store import MODE_1D1P, save_published_picks

            save_published_picks(
                conn,
                mode=MODE_1D1P,
                calendar_date=cal_day,
                picks=[pick_row],
                source=source,
            )

        return {
            "ok": True,
            "calendar_date": cal_day,
            "pick_key": post_key,
            "sent": sent,
            "n_picks": 1 if pick else 0,
        }
    finally:
        conn.close()


def run_result_notify(*, dry_run: bool = False, force: bool = False) -> dict:
    if not dry_run and not telegram_1d1p_enabled():
        return {"ok": False, "reason": "disabled"}

    from scripts.telegram_top5_notify import (
        format_1d1p_result_telegram_message,
        send_telegram_message,
    )

    conn = open_db(DB_PATH_DEFAULT)
    try:
        ensure_daily_top_proba_schema(conn)
        pending = fetch_daily_pick_keys_pending_result(conn)
        if not pending:
            return {"ok": True, "n_posted": 0}

        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        chats = _broadcast_chat_ids()
        if not dry_run and (not token or not chats):
            return {"ok": False, "reason": "missing_token_or_chats"}

        posted = 0
        for pick in pending:
            pk = _pick_key(pick)
            if not pk:
                continue
            if not force and has_result_post_for_pick(conn, pk):
                continue
            text = format_1d1p_result_telegram_message(pick)
            if dry_run:
                print(text)
                posted += 1
                continue
            for chat_id in chats:
                send_telegram_message(text, token=token, chat_id=chat_id)
            log_post(
                conn,
                post_type="result",
                calendar_date=str(pick.get("calendar_date") or "")[:10],
                pick_key=pk,
                message_preview=str(pick.get("status") or ""),
            )
            posted += 1
        return {"ok": True, "n_posted": posted}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Telegram 1 Day 1 Pick — publish")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--results", action="store_true")
    args = ap.parse_args(argv)

    if args.results:
        out = run_result_notify(dry_run=args.dry_run, force=args.force)
    else:
        out = run_daily_pick(dry_run=args.dry_run, force=args.force)
    print(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
