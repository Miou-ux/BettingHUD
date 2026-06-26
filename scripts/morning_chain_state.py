"""État partagé de la chaîne matinale (sync tours → build → publications)."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

PARIS_TZ = ZoneInfo("Europe/Paris")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAIN_STATE_PATH = os.path.join(ROOT, "data", "cache", "morning_chain_state.json")


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
