#!/usr/bin/env python3
"""Backup SQLite sur le serveur PROD (local + rétention)."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import DB_PATH_DEFAULT

PARIS = ZoneInfo("Europe/Paris")
BACKUP_DIR = os.path.join(ROOT, "backups", "prod")
KEEP_DAYS = int(os.getenv("BETTINGHUD_SERVER_BACKUP_KEEP_DAYS", "30"))


def _log(msg: str) -> None:
    print(f"[{datetime.now(PARIS).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def backup_db() -> str:
    src = DB_PATH_DEFAULT if os.path.isabs(DB_PATH_DEFAULT) else os.path.join(ROOT, DB_PATH_DEFAULT)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"DB introuvable: {src}")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(PARIS).strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, f"bettinghud_prod_{stamp}.db")
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    size_mb = os.path.getsize(dst) / (1024 * 1024)
    if size_mb < 1.0:
        raise RuntimeError(f"backup suspect ({size_mb:.2f} Mo): {dst}")
    _log(f"OK {dst} ({size_mb:.1f} Mo)")
    return dst


def prune_old() -> int:
    if KEEP_DAYS <= 0:
        return 0
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = 0
    for name in os.listdir(BACKUP_DIR):
        if not name.startswith("bettinghud_prod_") or not name.endswith(".db"):
            continue
        path = os.path.join(BACKUP_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
                _log(f"retention: supprimé {name}")
        except OSError:
            pass
    return removed


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass
    try:
        backup_db()
        prune_old()
        return 0
    except Exception as exc:
        _log(f"ERREUR: {exc}")
        # Sous cron_run_with_alert le wrapper envoie déjà l'échec.
        if os.getenv("BETTINGHUD_IN_CRON_ALERT", "").strip() not in ("1", "true", "yes"):
            try:
                from scripts.ops_telegram_alert import send_ops_alert

                send_ops_alert("Backup DB serveur — ÉCHEC", str(exc)[:500], dedup_key="backup_db_server")
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
