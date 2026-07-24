#!/usr/bin/env python3
"""Publie le pick 1 Day 1 Pick sur un salon Discord (webhook).

Variables d'environnement :
  DISCORD_1D1P_WEBHOOK_URL   — URL webhook du salon (obligatoire)
  DISCORD_1D1P_ENABLED       — 1 pour activer (défaut : oui si URL présente)
  DISCORD_1D1P_USERNAME      — nom affiché (défaut CourtAlpha)
  DISCORD_1D1P_SITE_URL      — lien track record (défaut courtalpha.tech/1-day-1-pick)

Usage :
  py -3 scripts/discord_1d1p_notify.py --dry-run
  py -3 scripts/discord_1d1p_notify.py
  py -3 scripts/discord_1d1p_notify.py --results
  py -3 scripts/discord_1d1p_notify.py --results --dry-run
  py -3 scripts/discord_1d1p_notify.py --performance-board
  py -3 scripts/discord_1d1p_notify.py --performance-board --dry-run
  py -3 scripts/discord_1d1p_notify.py --welcome
  py -3 scripts/discord_1d1p_notify.py --welcome --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.bets_db import DB_PATH_DEFAULT, ensure_daily_top_proba_schema, open_db  # noqa: E402
from scripts.discord_1d1p_core import load_1d1p_replay, load_1d1p_today_pick  # noqa: E402
from scripts.discord_1d1p_format import (  # noqa: E402
    build_daily_pick_embed,
    build_no_pick_embed,
    build_performance_board_embed,
    build_result_embed,
    build_welcome_embed,
    performance_board_pin_content,
)
from scripts.discord_1d1p_post_log import (  # noqa: E402
    fetch_daily_pick_keys_pending_result,
    get_performance_board,
    has_daily_post_for_date,
    has_result_post_for_pick,
    log_post,
    upsert_performance_board,
)
from scripts.discord_client import (  # noqa: E402
    discord_1d1p_channel_id,
    discord_posting_enabled,
    patch_webhook_message,
    post_webhook,
    try_pin_channel_message,
)


def _username() -> str:
    return (os.getenv("DISCORD_1D1P_USERNAME") or "CourtAlpha").strip() or "CourtAlpha"


def _pick_key(pick: dict) -> str:
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


def run_daily_pick(*, dry_run: bool = False, force: bool = False) -> dict:
    if not dry_run and not discord_posting_enabled():
        return {"ok": False, "reason": "disabled", "message": "Discord 1D1P désactivé ou webhook absent."}

    pick, cal_day, pool_n, _snapshot_age_min = load_1d1p_today_pick(db_path=DB_PATH_DEFAULT)

    conn = open_db(DB_PATH_DEFAULT)
    try:
        ensure_daily_top_proba_schema(conn)
        if not force and has_daily_post_for_date(conn, cal_day):
            return {
                "ok": False,
                "reason": "already_posted",
                "calendar_date": cal_day,
            }

        if pick is None:
            embed = build_no_pick_embed(calendar_date=cal_day, pool_size=pool_n)
            preview = embed.get("title", "no_pick")
            if dry_run:
                print(json.dumps({"embed": embed}, ensure_ascii=True, indent=2))
                return {"ok": True, "dry_run": True, "no_pick": True, "calendar_date": cal_day}

            res = post_webhook(embeds=[embed], username=_username())
            log_post(
                conn,
                post_type="no_pick",
                calendar_date=cal_day,
                discord_message_id=str(res.get("id") or ""),
                message_preview=preview,
            )
            return {"ok": True, "no_pick": True, "calendar_date": cal_day}

        embed = build_daily_pick_embed(pick, calendar_date=cal_day)
        preview = str(pick.get("match_name") or embed.get("title") or "")
        from scripts.od1p_pick_key import od1p_post_pick_key

        post_key = od1p_post_pick_key(cal_day)
        if dry_run:
            print(
                json.dumps(
                    {"embed": embed, "pick_key": post_key},
                    ensure_ascii=True,
                    indent=2,
                )
            )
            return {"ok": True, "dry_run": True, "calendar_date": cal_day, "pick_key": post_key}

        res = post_webhook(embeds=[embed], username=_username())
        log_post(
            conn,
            post_type="daily_pick",
            calendar_date=cal_day,
            pick_key=post_key,
            discord_message_id=str(res.get("id") or ""),
            message_preview=preview[:200],
        )
        return {
            "ok": True,
            "calendar_date": cal_day,
            "pick_key": post_key,
            "match_name": pick.get("match_name"),
        }
    finally:
        conn.close()


def run_result_notify(*, dry_run: bool = False, force: bool = False) -> dict:
    if not dry_run and not discord_posting_enabled():
        return {"ok": False, "reason": "disabled"}

    conn = open_db(DB_PATH_DEFAULT)
    try:
        ensure_daily_top_proba_schema(conn)
        pending = fetch_daily_pick_keys_pending_result(conn)
        if not pending:
            return {"ok": True, "n_posted": 0}

        posted = 0
        for pick in pending:
            pk = _pick_key(pick)
            if not force and has_result_post_for_pick(conn, pk):
                continue
            embed = build_result_embed(pick)
            if dry_run:
                print(json.dumps({"pick_key": pk, "embed": embed}, ensure_ascii=True, indent=2))
                posted += 1
                continue
            res = post_webhook(embeds=[embed], username=_username())
            log_post(
                conn,
                post_type="result",
                calendar_date=str(pick.get("calendar_date") or "")[:10],
                pick_key=pk,
                discord_message_id=str(res.get("id") or ""),
                message_preview=str(pick.get("status") or ""),
            )
            posted += 1
        out = {"ok": True, "n_posted": posted}
        if posted and not dry_run:
            try:
                board = run_performance_board_update(dry_run=False)
                out["performance_board"] = board
            except Exception as exc:
                out["performance_board_error"] = str(exc)
        return out
    finally:
        conn.close()


def run_performance_board_update(*, dry_run: bool = False) -> dict:
    """Create or edit the pinned-style live track record message (one message, daily edits)."""
    if not dry_run and not discord_posting_enabled():
        return {"ok": False, "reason": "disabled"}

    replay = load_1d1p_replay(db_path=DB_PATH_DEFAULT)
    cal_day = str(replay.get("today_date") or "")[:10]
    embed = build_performance_board_embed(replay, calendar_date=cal_day)
    pin_content = performance_board_pin_content()
    preview = str(embed.get("title") or "performance_board")

    if dry_run:
        print(
            json.dumps(
                {"content": pin_content, "embed": embed, "calendar_date": cal_day},
                ensure_ascii=True,
                indent=2,
            )
        )
        return {"ok": True, "dry_run": True, "calendar_date": cal_day}

    conn = open_db(DB_PATH_DEFAULT)
    try:
        ensure_daily_top_proba_schema(conn)
        board = get_performance_board(conn)
        mid = str((board or {}).get("discord_message_id") or "").strip()

        if mid:
            patch_webhook_message(
                mid,
                content=pin_content,
                embeds=[embed],
                username=_username(),
            )
            upsert_performance_board(
                conn,
                discord_message_id=mid,
                calendar_date=cal_day,
                message_preview=preview[:200],
            )
            return {"ok": True, "action": "updated", "message_id": mid, "calendar_date": cal_day}

        res = post_webhook(
            content=pin_content, embeds=[embed], username=_username()
        )
        new_mid = str(res.get("id") or "").strip()
        if not new_mid:
            return {"ok": False, "reason": "no_message_id", "calendar_date": cal_day}

        upsert_performance_board(
            conn,
            discord_message_id=new_mid,
            calendar_date=cal_day,
            message_preview=preview[:200],
        )

        channel_id = discord_1d1p_channel_id() or str(res.get("channel_id") or "").strip()
        pinned = try_pin_channel_message(channel_id, new_mid) if channel_id else False
        return {
            "ok": True,
            "action": "created",
            "message_id": new_mid,
            "pinned": pinned,
            "calendar_date": cal_day,
        }
    finally:
        conn.close()


def run_welcome_post(*, dry_run: bool = False) -> dict:
    embed = build_welcome_embed()
    if dry_run:
        return {"ok": True, "dry_run": True, "embed": embed}
    if not discord_posting_enabled():
        return {"ok": False, "error": "discord_posting_disabled"}
    res = post_webhook(embeds=[embed], username=_username())
    mid = str(res.get("id") or "").strip()
    return {"ok": True, "action": "welcome_posted", "message_id": mid, "channel_id": res.get("channel_id")}


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Discord 1 Day 1 Pick")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Ignore anti-doublon journal")
    ap.add_argument("--results", action="store_true", help="Publie les résultats réglés")
    ap.add_argument(
        "--performance-board",
        action="store_true",
        help="Crée ou met à jour le message track record (pin / edit quotidien)",
    )
    ap.add_argument(
        "--welcome",
        action="store_true",
        help="Poste le message de bienvenue (à épingler manuellement)",
    )
    args = ap.parse_args(argv)

    if args.welcome:
        out = run_welcome_post(dry_run=args.dry_run)
    elif args.performance_board:
        out = run_performance_board_update(dry_run=args.dry_run)
    elif args.results:
        out = run_result_notify(dry_run=args.dry_run, force=args.force)
    else:
        out = run_daily_pick(dry_run=args.dry_run, force=args.force)
        if out.get("ok") and not args.dry_run:
            try:
                board = run_performance_board_update(dry_run=False)
                out["performance_board"] = board
            except Exception as exc:
                out["performance_board_error"] = str(exc)

    print(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
