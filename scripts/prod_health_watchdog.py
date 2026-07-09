#!/usr/bin/env python3
"""Watchdog PROD : health dashboard, heartbeat daemon, alerte + restart optionnel."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PARIS = ZoneInfo("Europe/Paris")
HEALTH_URL = os.getenv("BETTINGHUD_HEALTH_URL", "http://127.0.0.1:8501/_stcore/health")
DAEMON_MAX_AGE = float(os.getenv("BETTINGHUD_WATCHDOG_DAEMON_MAX_AGE_SEC", "900"))
COOLDOWN_PATH = os.path.join(ROOT, "data", "cache", ".health_watchdog_last_alert.ts")
COOLDOWN_SEC = int(os.getenv("BETTINGHUD_WATCHDOG_ALERT_COOLDOWN_SEC", "1800"))
AUTO_RESTART = os.getenv("BETTINGHUD_WATCHDOG_AUTO_RESTART", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)


def _log(msg: str) -> None:
    print(f"[{datetime.now(PARIS).strftime('%H:%M:%S')}] {msg}", flush=True)


def _http_ok(url: str, timeout: float = 8.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(resp.status) < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log(f"health KO {url}: {exc}")
        return False


def _systemd_active(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            cwd=ROOT,
            timeout=15,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


def _restart(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["sudo", "systemctl", "restart", unit],
            cwd=ROOT,
            timeout=120,
            check=False,
        )
        return r.returncode == 0
    except Exception as exc:
        _log(f"restart {unit} failed: {exc}")
        return False


def _cooldown_active() -> bool:
    if not os.path.isfile(COOLDOWN_PATH):
        return False
    try:
        age = time.time() - os.path.getmtime(COOLDOWN_PATH)
        return age < COOLDOWN_SEC
    except OSError:
        return False


def _touch_cooldown() -> None:
    try:
        os.makedirs(os.path.dirname(COOLDOWN_PATH), exist_ok=True)
        with open(COOLDOWN_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    issues: list[str] = []

    if not _http_ok(HEALTH_URL):
        issues.append(f"dashboard health KO ({HEALTH_URL})")
    if not _systemd_active("bettinghud-dashboard"):
        issues.append("systemd bettinghud-dashboard inactive")
    if not _systemd_active("bettinghud-daemon"):
        issues.append("systemd bettinghud-daemon inactive")
    if not _systemd_active("bettinghud-telegram-bot"):
        issues.append("systemd bettinghud-telegram-bot inactive")

    from scripts.portfolio_sync_lock import daemon_recently_active

    if not daemon_recently_active(max_age_sec=DAEMON_MAX_AGE):
        issues.append(f"portfolio daemon heartbeat > {int(DAEMON_MAX_AGE)}s")

    if not issues:
        _log("OK")
        return 0

    body = "\n".join(f"• {x}" for x in issues)
    _log(f"ALERTE: {body}")

    if _cooldown_active():
        _log("cooldown alerte actif — skip TG")
        return 1

    restarted: list[str] = []
    if AUTO_RESTART:
        for unit in ("bettinghud-dashboard", "bettinghud-daemon", "bettinghud-telegram-bot"):
            if not _systemd_active(unit):
                if _restart(unit):
                    restarted.append(unit)

    from scripts.ops_telegram_alert import send_ops_alert

    extra = ""
    if restarted:
        extra = "\nRestart tenté: " + ", ".join(restarted)
    send_ops_alert("Watchdog PROD — anomalie", body + extra)
    _touch_cooldown()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
