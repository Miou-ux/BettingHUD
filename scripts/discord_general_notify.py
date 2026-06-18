#!/usr/bin/env python3
"""Post onboarding / welcome messages to the Discord **general** channel.

Env (prod) :
  DISCORD_GENERAL_WEBHOOK_URL  — webhook for #general (required)
  DISCORD_WEBHOOK_URL          — legacy fallback for #general only
  DISCORD_GENERAL_USERNAME     — display name (default CourtAlpha)

Do NOT use DISCORD_1D1P_WEBHOOK_URL here — that channel has its own bot.

Usage :
  py -3 scripts/discord_general_notify.py --welcome --dry-run
  py -3 scripts/discord_general_notify.py --welcome
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.discord_client import discord_general_webhook_url, post_webhook  # noqa: E402
from scripts.discord_general_format import build_general_welcome_embed  # noqa: E402


def _username() -> str:
    return (os.getenv("DISCORD_GENERAL_USERNAME") or os.getenv("DISCORD_1D1P_USERNAME") or "CourtAlpha").strip() or "CourtAlpha"


def run_welcome_post(*, dry_run: bool = False) -> dict:
    embed = build_general_welcome_embed()
    if dry_run:
        return {"ok": True, "dry_run": True, "embed": embed}
    url = discord_general_webhook_url()
    if not url:
        return {
            "ok": False,
            "error": "missing_webhook",
            "message": "Set DISCORD_GENERAL_WEBHOOK_URL in .env (webhook for #general).",
        }
    res = post_webhook(embeds=[embed], username=_username(), webhook_url=url)
    return {
        "ok": True,
        "action": "general_welcome_posted",
        "message_id": str(res.get("id") or ""),
        "channel_id": res.get("channel_id"),
    }


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Discord general channel posts")
    ap.add_argument("--welcome", action="store_true", help="Post pinned welcome overview")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.welcome:
        ap.error("--welcome is required")

    out = run_welcome_post(dry_run=args.dry_run)
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
