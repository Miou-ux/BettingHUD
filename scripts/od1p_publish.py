"""Publication coordonnée 1 Day 1 Pick — Telegram + Discord.

Appelé par :
  - morning_live_pipeline.py (05:00, --morning-publish)
  - portfolio_results_daemon.py (~10 min)

Documentation : docs/ONE_DAY_ONE_PICK.md
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass


def publish_1d1p_morning(*, source: str = "morning-sync", force: bool = False) -> dict:
    """Pick du jour + track record Discord (05:00 Paris, snapshot frais)."""
    _load_dotenv()
    out: dict = {"source": source}

    try:
        from scripts.telegram_1d1p_notify import run_daily_pick as tg_daily

        out["telegram"] = tg_daily(dry_run=False, force=force, source=source)
    except Exception as exc:
        out["telegram"] = {"ok": False, "error": str(exc)}

    try:
        from scripts.discord_1d1p_notify import run_daily_pick as dc_daily

        out["discord"] = dc_daily(dry_run=False, force=force)
    except Exception as exc:
        out["discord"] = {"ok": False, "error": str(exc)}

    try:
        from scripts.discord_1d1p_notify import run_performance_board_update

        out["discord_board"] = run_performance_board_update(dry_run=False)
    except Exception as exc:
        out["discord_board"] = {"ok": False, "error": str(exc)}

    return out


def publish_1d1p_results(*, force: bool = False) -> dict:
    """Résultats réglés sur Telegram + Discord."""
    _load_dotenv()
    out: dict = {}

    try:
        from scripts.telegram_1d1p_notify import run_result_notify as tg_results

        out["telegram"] = tg_results(dry_run=False, force=force)
    except Exception as exc:
        out["telegram"] = {"ok": False, "error": str(exc)}

    try:
        from scripts.discord_1d1p_notify import (
            run_performance_board_update,
            run_result_notify as dc_results,
        )

        out["discord"] = dc_results(dry_run=False, force=force)
        if int((out["discord"] or {}).get("n_posted") or 0) > 0:
            out["discord_board"] = run_performance_board_update(dry_run=False)
    except Exception as exc:
        out["discord"] = {"ok": False, "error": str(exc)}

    return out
