#!/usr/bin/env python3
"""Alerte admin Telegram (ops / P0)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass


def admin_chat_ids() -> list[str]:
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


def send_ops_alert(subject: str, body: str = "", *, dry_run: bool = False) -> bool:
    if os.getenv("BETTINGHUD_OPS_ALERT", "1").strip().lower() in ("0", "false", "no"):
        return True
    text = f"🚨 OPS — {subject}"
    if body:
        text = f"{text}\n{body}"[:3900]
    if dry_run:
        print(text, flush=True)
        return True
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = admin_chat_ids()
    if not token or not chats:
        print("[ops-alert] TELEGRAM_BOT_TOKEN ou admin chat absent", flush=True)
        return False
    from scripts.telegram_top5_notify import send_telegram_message

    ok = True
    for chat_id in chats:
        try:
            send_telegram_message(text, token=token, chat_id=chat_id)
        except Exception as exc:
            print(f"[ops-alert] échec chat {chat_id}: {exc}", flush=True)
            ok = False
    return ok


def main() -> int:
    import argparse

    _load_env()
    ap = argparse.ArgumentParser(description="Alerte ops Telegram")
    ap.add_argument("subject", help="Titre court")
    ap.add_argument("body", nargs="*", default=[], help="Détail optionnel")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    ok = send_ops_alert(args.subject, " ".join(args.body), dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
