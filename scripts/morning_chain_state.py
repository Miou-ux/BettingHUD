"""État partagé de la chaîne matinale (sync tours → build → publications)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

PARIS_TZ = ZoneInfo("Europe/Paris")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAIN_STATE_PATH = os.path.join(ROOT, "data", "cache", "morning_chain_state.json")
TOURS_SYNC_LOCK_PATH = os.path.join(ROOT, "data", "cache", "tours_sync.lock")


def _paris_calendar_date() -> str:
    return datetime.now(PARIS_TZ).date().isoformat()


def _read() -> dict[str, Any]:
    try:
        with open(CHAIN_STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CHAIN_STATE_PATH), exist_ok=True)
    tmp = CHAIN_STATE_PATH + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CHAIN_STATE_PATH)


def record_step(
    step: str,
    *,
    ok: bool,
    rc: int = 0,
    detail: dict[str, Any] | None = None,
) -> None:
    data = _read()
    data[step] = {
        "calendar_date": _paris_calendar_date(),
        "finished_at": datetime.now(PARIS_TZ).isoformat(timespec="seconds"),
        "ok": bool(ok),
        "rc": int(rc),
        "detail": detail or {},
    }
    _write(data)


def step_ok_today(step: str) -> bool:
    rec = _read().get(step) or {}
    return bool(rec.get("ok")) and str(rec.get("calendar_date") or "") == _paris_calendar_date()


def get_step(step: str) -> dict[str, Any]:
    return dict(_read().get(step) or {})


def tours_sync_in_progress() -> bool:
    """True si le lock sync tours est présent (job 00:30 encore actif)."""
    try:
        if not os.path.isfile(TOURS_SYNC_LOCK_PATH):
            return False
        age_sec = time.time() - os.path.getmtime(TOURS_SYNC_LOCK_PATH)
        return age_sec < 6 * 3600
    except OSError:
        return False


def wait_for_step_ok(
    step: str,
    *,
    max_wait_sec: int,
    poll_sec: int = 30,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Attend qu'une étape soit OK aujourd'hui (garde-fou entre crons)."""
    deadline = time.time() + max(0, max_wait_sec)
    while time.time() < deadline:
        if step_ok_today(step):
            return True
        if log:
            remaining = int(deadline - time.time())
            log(f"Attente {step}… ({remaining}s restantes)")
        time.sleep(max(5, poll_sec))
    return step_ok_today(step)
