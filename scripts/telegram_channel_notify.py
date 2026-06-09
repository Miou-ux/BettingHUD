#!/usr/bin/env python3
"""Posts public Telegram channel messages for CourtAlpha 1 Day 1 Pick acquisition.

Documentation: docs/TELEGRAM_CHANNEL_ACQUISITION.md

Env:
  TELEGRAM_BOT_TOKEN       — same bot as BettingHUD (must be channel admin)
  TELEGRAM_CHANNEL_ID      — public channel id (e.g. -100xxxxxxxxxx or @channelname)
  COURTALPHA_PUBLIC_URL    — default https://courtalpha.tech
  COURTALPHA_ROOT          — path to CourtAlpha repo for one_day_one_pick service
  TELEGRAM_CHANNEL_ENABLED — 1 to send (default 1 on prod cron)

Usage:
  py -3 scripts/telegram_channel_notify.py --dry-run
  py -3 scripts/telegram_channel_notify.py --dry-run --weekly
  py -3 scripts/telegram_channel_notify.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

PARIS = ZoneInfo("Europe/Paris")
META_DAILY_PREFIX = "tg_channel_daily_"
META_RESULT_PREFIX = "tg_channel_result_"
META_WEEKLY_PREFIX = "tg_channel_weekly_"


def _safe_print(text: str) -> None:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(enc, errors="replace").decode(enc))


def _courtalpha_root() -> str:
    raw = (os.getenv("COURTALPHA_ROOT") or "").strip()
    if raw and os.path.isdir(raw):
        return raw
    sibling = os.path.join(os.path.dirname(ROOT), "CourtAlpha")
    if os.path.isdir(sibling):
        return sibling
    prod = "/opt/courtalpha"
    if os.path.isdir(prod):
        return prod
    return sibling


def _load_replay(db_path: str) -> dict:
    ca = _courtalpha_root()
    if ca not in sys.path:
        sys.path.insert(0, ca)
    from api.services.one_day_one_pick import build_one_day_one_pick_replay

    return build_one_day_one_pick_replay(db_path=db_path)


def _channel_id() -> str | None:
    raw = (os.getenv("TELEGRAM_CHANNEL_ID") or "").strip()
    return raw or None


def _channel_enabled() -> bool:
    return (os.getenv("TELEGRAM_CHANNEL_ENABLED") or "1").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _public_base_url() -> str:
    return (os.getenv("COURTALPHA_PUBLIC_URL") or "https://courtalpha.tech").rstrip("/")


def _utm_link(campaign: str) -> str:
    return (
        f"{_public_base_url()}/1-day-1-pick"
        f"?utm_source=telegram&utm_medium=channel&utm_campaign={campaign}"
    )


def _archive_link(year_month: str) -> str:
    return (
        f"{_public_base_url()}/1-day-1-pick/archive/{year_month}"
        f"?utm_source=telegram&utm_medium=channel&utm_campaign=weekly"
    )


def _format_date_fr(iso_date: str) -> str:
    try:
        d = date.fromisoformat(iso_date)
        months = (
            "janvier",
            "février",
            "mars",
            "avril",
            "mai",
            "juin",
            "juillet",
            "août",
            "septembre",
            "octobre",
            "novembre",
            "décembre",
        )
        return f"{d.day} {months[d.month - 1]}"
    except ValueError:
        return iso_date


def _pick_match_line(pick: dict) -> str:
    fav = str(pick.get("fav_player") or pick.get("bet_on") or "—")
    dog = str(pick.get("underdog_player") or pick.get("opponent") or "—")
    tour = str(pick.get("tour") or "").upper()
    proba = pick.get("p_model_pct")
    ev = pick.get("ev_fav_pct")
    odd = pick.get("odd_fav")
    parts = [f"{fav} vs {dog}"]
    if tour:
        parts.append(tour)
    if odd is not None:
        parts.append(f"@{float(odd):.2f}")
    if proba is not None:
        parts.append(f"Proba {float(proba):.1f}%")
    if ev is not None:
        parts.append(f"EV +{float(ev):.1f}%")
    return " · ".join(parts)


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    from scripts.bets_db import ensure_bets_meta, get_meta

    ensure_bets_meta(conn)
    return get_meta(conn, key)


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    from scripts.bets_db import ensure_bets_meta, set_meta

    ensure_bets_meta(conn)
    set_meta(conn, key, value)


def _already_sent(conn: sqlite3.Connection, prefix: str, token: str) -> bool:
    return _meta_get(conn, f"{prefix}{token}") == "1"


def _mark_sent(conn: sqlite3.Connection, prefix: str, token: str) -> None:
    _meta_set(conn, f"{prefix}{token}", "1")
    conn.commit()


def format_daily_pick_message(pick: dict, *, today: str) -> str:
    line = _pick_match_line(pick)
    tournoi = str(pick.get("tournament") or "").strip()
    header = f"🎾 <b>1 Day 1 Pick</b> — {_format_date_fr(today)}"
    body = f"{line}"
    if tournoi:
        body += f"\n🏆 {tournoi}"
    link = _utm_link("daily")
    footer = (
        f'\n<a href="{link}">Historique public vérifiable</a>\n'
        f"<i>Info — pas un conseil de pari. Jouez responsablement.</i>"
    )
    return header + "\n" + body + footer


def format_no_pick_message(*, today: str) -> str:
    link = _utm_link("daily")
    return (
        f"🎾 <b>1 Day 1 Pick</b> — {_format_date_fr(today)}\n"
        "Aucun majeur rank=1 dans la bande EV 15–100 % aujourd'hui.\n"
        f'<a href="{link}">Voir le track record</a>'
    )


def format_result_message(pick: dict) -> str:
    cal = str(pick.get("calendar_date") or "")
    won = bool(pick.get("won"))
    lost = bool(pick.get("lost"))
    emoji = "✅" if won else "❌" if lost else "⏳"
    label = "Gagné" if won else "Perdu" if lost else str(pick.get("status") or "—")
    score = pick.get("score_display") or pick.get("score_final") or "—"
    line = _pick_match_line(pick)
    link = _utm_link("result")
    return (
        f"{emoji} <b>Résultat</b> — {_format_date_fr(cal)}\n"
        f"{line}\n"
        f"Score : {score} · <b>{label}</b>\n"
        f'<a href="{link}">Track record complet</a>'
    )


def format_weekly_recap(*, picks: list[dict], week_label: str, year_month: str) -> str:
    settled = [p for p in picks if p.get("won") or p.get("lost")]
    wins = sum(1 for p in settled if p.get("won"))
    hit = (100.0 * wins / len(settled)) if settled else 0.0
    pricing = f"{_public_base_url()}/pricing?utm_source=telegram&utm_medium=channel&utm_campaign=weekly"
    archive = _archive_link(year_month)
    return (
        f"📊 <b>Récap hebdo</b> — {week_label}\n"
        f"Picks : {len(picks)} · Terminés : {len(settled)} · Hit : <b>{hit:.1f}%</b>\n"
        f'<a href="{archive}">Archive du mois</a>\n'
        f'<a href="{pricing}">Outils Premium</a> (Live, Top 5…)\n'
        f"<i>Track record public — pas de picks effacés.</i>"
    )


def run_channel_notify(*, dry_run: bool = False, weekly: bool = False) -> dict:
    from scripts.bets_db import DB_PATH_DEFAULT
    from scripts.telegram_top5_notify import send_telegram_message

    if not _channel_enabled():
        return {"ok": True, "skipped": "channel_disabled"}

    channel = _channel_id() or ("@dry_run_channel" if dry_run else None)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() or ("dry-run" if dry_run else "")
    if not _channel_id() and not dry_run:
        return {"ok": False, "reason": "missing_telegram_channel_id"}
    if not (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip() and not dry_run:
        return {"ok": False, "reason": "missing_telegram_bot_token"}

    env_name = (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower()
    if env_name != "prod" and not dry_run:
        return {"ok": True, "skipped": "not_prod"}

    db_path = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    replay = _load_replay(db_path)
    today = str(replay.get("today_date") or datetime.now(PARIS).date().isoformat())
    pick_today = replay.get("pick_today")
    all_picks = list(replay.get("picks") or [])

    conn = sqlite3.connect(db_path)
    sent: list[str] = []
    try:
        if weekly:
            now = datetime.now(PARIS)
            if now.weekday() != 6 and not dry_run:  # Sunday
                return {"ok": True, "skipped": "not_sunday"}
            iso_week = now.strftime("%G-W%V")
            if _already_sent(conn, META_WEEKLY_PREFIX, iso_week) and not dry_run:
                return {"ok": True, "skipped": "weekly_already_sent", "week": iso_week}
            week_start = (now.date() - timedelta(days=6)).isoformat()
            week_picks = [p for p in all_picks if str(p.get("calendar_date") or "") >= week_start]
            ym = now.strftime("%Y-%m")
            msg = format_weekly_recap(
                picks=week_picks,
                week_label=f"sem. {iso_week}",
                year_month=ym,
            )
            if dry_run:
                _safe_print(msg)
            else:
                send_telegram_message(msg, token=token, chat_id=channel)
                _mark_sent(conn, META_WEEKLY_PREFIX, iso_week)
            sent.append("weekly")
            return {"ok": True, "sent": sent, "week": iso_week}

        # Result for yesterday
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        y_pick = next((p for p in all_picks if str(p.get("calendar_date")) == yesterday), None)
        if y_pick and (y_pick.get("won") or y_pick.get("lost")):
            if not _already_sent(conn, META_RESULT_PREFIX, yesterday) or dry_run:
                msg = format_result_message(y_pick)
                if dry_run:
                    _safe_print("--- RESULT ---")
                    _safe_print(msg)
                else:
                    send_telegram_message(msg, token=token, chat_id=channel)
                    _mark_sent(conn, META_RESULT_PREFIX, yesterday)
                sent.append(f"result:{yesterday}")

        # Today's pick
        if not _already_sent(conn, META_DAILY_PREFIX, today) or dry_run:
            if pick_today:
                msg = format_daily_pick_message(pick_today, today=today)
            else:
                msg = format_no_pick_message(today=today)
            if dry_run:
                _safe_print("--- DAILY ---")
                _safe_print(msg)
            else:
                send_telegram_message(msg, token=token, chat_id=channel)
                if not dry_run:
                    _mark_sent(conn, META_DAILY_PREFIX, today)
            sent.append(f"daily:{today}")
    finally:
        conn.close()

    return {"ok": True, "sent": sent}


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="CourtAlpha public Telegram channel notify")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--weekly", action="store_true", help="Récap hebdomadaire (dimanche)")
    args = ap.parse_args()
    out = run_channel_notify(dry_run=args.dry_run, weekly=args.weekly)
    print(out)
    if not out.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
