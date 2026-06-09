#!/usr/bin/env python3
"""Rapport hebdomadaire trafic web → Telegram admin."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)


def _admin_chat_ids() -> list[str]:
    raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    extra = (os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    ids: list[str] = []
    for part in (raw, extra):
        for bit in part.replace(";", ",").split(","):
            b = bit.strip()
            if b and b not in ids:
                ids.append(b)
    return ids


def format_traffic_report_html(report: dict) -> str:
    s = report.get("summary") or {}
    lines = [
        "<b>📈 CourtAlpha — trafic 7 jours</b>",
        "",
        f"Vues : <b>{s.get('views_7d', 0)}</b> · Uniques : <b>{s.get('unique_7d', 0)}</b>",
        f"Aujourd'hui : {s.get('views_today', 0)} vues · {s.get('unique_today', 0)} uniques",
        "",
        "<b>Top pages</b>",
    ]
    for p in (report.get("top_pages") or [])[:5]:
        lines.append(f"· {p.get('label', p.get('path'))} — {p.get('views')} ({p.get('share_pct')}%)")
    lines.append("")
    lines.append("<b>Top sources</b>")
    for src in (report.get("top_sources") or [])[:6]:
        lines.append(f"· {src.get('label')} — {src.get('views')} ({src.get('share_pct')}%)")
    lines.append("")
    lines.append("<i>Admin → Fréquentation pour le détail.</i>")
    return "\n".join(lines)


def run_traffic_weekly_report(*, days: int = 7, dry_run: bool = False) -> dict:
    from scripts.bets_db import DB_PATH_DEFAULT
    from scripts.telegram_top5_notify import send_telegram_message

    db_path = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    ca_root = os.getenv("COURTALPHA_ROOT", os.path.join(os.path.dirname(ROOT), "CourtAlpha"))
    if ca_root not in sys.path:
        sys.path.insert(0, ca_root)
    from api.services.web_traffic import build_traffic_report

    report = build_traffic_report(db_path=db_path, days=days)
    prev = build_traffic_report(db_path=db_path, days=days * 2)
    cur_views = int((report.get("summary") or {}).get("views_7d") or 0)
    prev_views = int((prev.get("summary") or {}).get("views_period") or 0) - cur_views
    alert = ""
    if prev_views > 0 and cur_views < prev_views * 0.8:
        alert = "\n\n⚠️ <b>Alerte</b> : trafic &lt; 80 % vs semaine précédente"

    text = format_traffic_report_html(report) + alert

    if dry_run:
        print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        return {"ok": True, "dry_run": True, "views_7d": cur_views}

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = _admin_chat_ids()
    if not token or not chats:
        return {"ok": False, "reason": "missing_telegram_config"}

    sent = 0
    for chat_id in chats:
        send_telegram_message(text, token=token, chat_id=chat_id)
        sent += 1
    return {"ok": True, "sent": sent, "views_7d": cur_views}


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    out = run_traffic_weekly_report(days=args.days, dry_run=args.dry_run)
    print(out)
    if not out.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
