#!/usr/bin/env python3
"""Digest admin quotidien — calibration ML live (+ résumé pipeline).

Envoi canal admin Telegram (06:30 cron PROD).
Usage :
  python scripts/daily_admin_notify.py --dry-run
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PARIS_TZ = ZoneInfo("Europe/Paris")


def _pipeline_summary_lines() -> list[str]:
    from scripts.bets_db import DB_PATH_DEFAULT, get_data_freshness_snapshot
    from scripts.morning_chain_state import get_step, step_ok_today

    fresh = get_data_freshness_snapshot(os.path.join(ROOT, DB_PATH_DEFAULT))
    lines = ["<b>Pipeline</b>"]
    lines.append(f"Sync tours : {fresh.get('last_tours_sync_iso') or '—'}")
    tours_ok = step_ok_today("tours_sync")
    qc = get_step("qc_post_sync")
    qc_ok = step_ok_today("qc_post_sync")
    lines.append(f"Chaîne tours_sync today : {'OK' if tours_ok else 'KO/—'}")
    if qc:
        detail = qc.get("detail") or {}
        blocking = detail.get("blocking") if isinstance(detail, dict) else None
        n_block = len(blocking) if isinstance(blocking, list) else "?"
        lines.append(
            f"QC post-sync today : {'OK' if qc_ok else 'KO'} "
            f"(blocking={n_block}, rc={qc.get('rc')})"
        )
    else:
        lines.append("QC post-sync today : —")
    lines.append(f"Train ML : {fresh.get('last_ml_train_iso') or '—'}")
    lm = fresh.get("last_wta_match") or {}
    if lm.get("tourney_date"):
        lines.append(f"Dernier WTA : {str(lm.get('tourney_date'))[:10]}")
    return lines


def format_daily_digest(*, calibration_report: dict) -> str:
    from scripts.ml_calibration_probe import format_calibration_telegram

    day = datetime.now(PARIS_TZ).strftime("%d/%m/%Y")
    parts = [
        f"<b>📋 Digest admin — {day}</b>",
        "",
        format_calibration_telegram(calibration_report),
        "",
    ]
    parts.extend(_pipeline_summary_lines())
    return "\n".join(parts)


def run_daily_admin_notify(*, dry_run: bool = False) -> dict:
    from scripts.ml_calibration_probe import collect_calibration_probe, save_probe_cache

    cal = collect_calibration_probe()
    save_probe_cache(cal)
    text = format_daily_digest(calibration_report=cal)

    if dry_run:
        print(
            text.replace("<b>", "**")
            .replace("</b>", "**")
            .replace("<i>", "_")
            .replace("</i>", "_")
            .replace("&amp;", "&")
        )
        return {
            "ok": True,
            "dry_run": True,
            "calibration_status": cal.get("status"),
            "alerts": len(cal.get("alerts") or []),
        }

    from scripts.ml_weekly_telegram_notify import _admin_chat_ids
    from scripts.telegram_top5_notify import send_telegram_message

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = _admin_chat_ids()
    if not token or not chats:
        return {"ok": False, "reason": "missing_telegram_config"}

    sent = 0
    for chat_id in chats:
        send_telegram_message(text, token=token, chat_id=chat_id)
        sent += 1

    if cal.get("status") == "alert":
        try:
            from scripts.ops_telegram_alert import send_ops_alert

            send_ops_alert(
                "Calibration ML — dérive",
                "\n".join((cal.get("alerts") or [])[:5]),
            )
        except Exception:
            pass

    return {
        "ok": True,
        "sent": sent,
        "calibration_status": cal.get("status"),
        "alerts": len(cal.get("alerts") or []),
    }


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    import argparse

    ap = argparse.ArgumentParser(description="Digest admin quotidien (calibration ML)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = run_daily_admin_notify(dry_run=args.dry_run)
    print(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
