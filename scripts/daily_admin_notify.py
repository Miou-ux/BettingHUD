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
    from scripts.ops_alert_human import format_pipeline_digest_human

    return [format_pipeline_digest_human()]


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
    try:
        from scripts.portfolio_projection_track import build_projection_report, format_projection_telegram

        proj = build_projection_report()
        if proj.get("ok"):
            parts.extend(["", format_projection_telegram(proj)])
    except Exception as exc:
        parts.extend(["", f"<b>📊 Projection HYB</b>", f"— indisponible ({exc})"])
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
            from scripts.ops_alert_human import format_calibration_drift_alert
            from scripts.ops_telegram_alert import send_ops_alert

            subject, body = format_calibration_drift_alert(list(cal.get("alerts") or []))
            send_ops_alert(subject, body)
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
