#!/usr/bin/env python3
"""Alerte ops Telegram (admin) pour rapports QC matinaux."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.qc_common import QcReport

PARIS_TZ = ZoneInfo("Europe/Paris")


def _admin_chat_ids() -> list[str]:
    from scripts.telegram_access import admin_notify_chat_id

    ids: list[str] = []
    primary = admin_notify_chat_id()
    if primary:
        ids.append(primary)
    raw = (os.getenv("TELEGRAM_ADMIN_USER_IDS") or "").strip()
    for x in raw.split(","):
        x = x.strip()
        if x and x not in ids:
            ids.append(x)
    return ids


def format_qc_telegram(reports: list[QcReport], *, publish_ok: bool | None = None) -> str:
    day = datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M")
    n_block = sum(len(r.blocking) for r in reports)
    n_warn = sum(len(r.warnings) for r in reports)
    if publish_ok is True and n_block == 0:
        head = f"Morning QC — {day}\n✅ Publish autorisé"
    elif publish_ok is False or n_block > 0:
        head = f"Morning QC — {day}\n🛑 Publish bloqué"
    else:
        head = f"Morning QC — {day}\n⚠️ Warnings"
    lines = [head, f"Blocking: {n_block} | Warnings: {n_warn}"]
    for rep in reports:
        for issue in rep.blocking[:8]:
            lines.append(f"🛑 [{rep.name}] {issue.code}: {issue.message[:200]}")
        for issue in rep.warnings[:8]:
            lines.append(f"⚠️ [{rep.name}] {issue.code}: {issue.message[:200]}")
    extra = n_block + n_warn - 16
    if extra > 0:
        lines.append(f"… +{extra} issue(s) (voir logs)")
    return "\n".join(lines)


def notify_qc_reports(
    reports: list[QcReport],
    *,
    publish_ok: bool | None = None,
    dry_run: bool = False,
) -> bool:
    """Envoie au(x) admin(s) si blocking, warnings, ou publish_ok=False."""
    n_block = sum(len(r.blocking) for r in reports)
    n_warn = sum(len(r.warnings) for r in reports)
    if publish_ok is True and n_block == 0 and n_warn == 0:
        return True
    if os.getenv("BETTINGHUD_QC_NOTIFY", "1").strip() in ("0", "false", "False", "no"):
        return True

    text = format_qc_telegram(reports, publish_ok=publish_ok)
    if dry_run:
        print(text, flush=True)
        return True

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = _admin_chat_ids()
    if not token or not chats:
        print("[qc-notify] TELEGRAM_BOT_TOKEN ou admin chat absent — skip", flush=True)
        return False

    from scripts.telegram_top5_notify import send_telegram_message

    ok = True
    for chat_id in chats:
        try:
            send_telegram_message(text, token=token, chat_id=chat_id)
        except Exception as exc:
            print(f"[qc-notify] échec chat {chat_id}: {exc}", flush=True)
            ok = False
    return ok


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Test notification QC ops")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from scripts.qc_live_snapshot import run_qc_live_snapshot
    from scripts.qc_post_sync import run_qc_post_sync

    reports = [run_qc_post_sync(), run_qc_live_snapshot()]
    notify_qc_reports(reports, publish_ok=reports[0].ok and reports[1].ok, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
