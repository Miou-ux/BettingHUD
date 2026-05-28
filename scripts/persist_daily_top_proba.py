#!/usr/bin/env python3
"""Persiste le top 15 ATP/WTA du jour depuis le snapshot live (CLI / cron)."""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

from scripts.daily_top_proba_store import run_daily_top_proba_daemon_pass


def main() -> None:
    result = run_daily_top_proba_daemon_pass(
        capture_source="cli_persist_daily_top_proba",
        force=True,
    )
    print(
        "Top probas journaliers :",
        json.dumps(result, ensure_ascii=False),
    )


if __name__ == "__main__":
    main()
