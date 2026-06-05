#!/usr/bin/env python3
"""Test envoi SMTP (charge .env). Usage: ./venv/bin/python scripts/test_smtp_send.py [to_email]"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

from scripts.web_email import send_email, smtp_configured


def main() -> int:
    to = (sys.argv[1] if len(sys.argv) > 1 else "miouppy86@gmail.com").strip()
    print("smtp_configured:", smtp_configured())
    try:
        send_email(to=to, subject="BettingHUD — test SMTP", body_text="Test envoi depuis le serveur PROD.")
        print("OK — e-mail envoyé à", to)
        return 0
    except Exception as exc:
        print("ERREUR:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
