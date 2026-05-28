"""Lock + heartbeat for portfolio results scraping (daemon vs CLI/UI)."""
from __future__ import annotations

import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(ROOT, "data", "cache", ".portfolio_results_scrape.lock")
HEARTBEAT_PATH = os.path.join(ROOT, "data", "cache", ".portfolio_results_daemon.heartbeat")


def scrape_in_progress(max_lock_sec: float = 1200.0) -> bool:
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        age = time.time() - os.path.getmtime(LOCK_PATH)
    except OSError:
        return False
    if age > float(max_lock_sec):
        try:
            os.remove(LOCK_PATH)
        except OSError:
            pass
        return False
    return True


def acquire_scrape_lock() -> bool:
    if scrape_in_progress():
        return False
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return True
    except OSError:
        return False


def release_scrape_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError:
        pass


def touch_daemon_heartbeat() -> None:
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


def daemon_recently_active(max_age_sec: float = 660.0) -> bool:
    """True if the portfolio daemon touched its heartbeat recently."""
    if not os.path.exists(HEARTBEAT_PATH):
        return False
    try:
        age = time.time() - os.path.getmtime(HEARTBEAT_PATH)
    except OSError:
        return False
    return age <= float(max_age_sec)
