#!/usr/bin/env python3
"""Nettoyage disque PROD — sauvegardes, caches, journaux systemd.

Usage (sur le serveur) :
  python scripts/disk_cleanup_prod.py              # dry-run
  python scripts/disk_cleanup_prod.py --apply    # exécution réelle

Cibles sûres :
  - backups SQLite serveur (/opt/bettinghud/backups/prod) — garde N derniers
  - archives WTA Sackmann (data/backups/wta_sackmann) — garde N dernières
  - cache pip utilisateur (~/.cache/pip)
  - journaux systemd (vacuum-size)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_BACKUP_DIR = ROOT / "backups" / "prod"
WTA_BACKUP_DIR = ROOT / "data" / "backups" / "wta_sackmann"
DEFAULT_DB_KEEP = int(os.getenv("BETTINGHUD_SERVER_BACKUP_KEEP_DAYS", "14"))
DEFAULT_WTA_KEEP = 4
DEFAULT_JOURNAL_MAX = "500M"


def _log(msg: str) -> None:
    print(msg, flush=True)


def prune_db_backups(*, keep: int, apply: bool) -> tuple[int, int]:
    if not DB_BACKUP_DIR.is_dir():
        return 0, 0
    files = sorted(DB_BACKUP_DIR.glob("bettinghud_prod_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    to_remove = files[keep:]
    nbytes = sum(p.stat().st_size for p in to_remove)
    for p in to_remove:
        _log(f"{'remove' if apply else 'would remove'} {p.name}")
        if apply:
            p.unlink(missing_ok=True)
    return len(to_remove), nbytes


def prune_wta_archives(*, keep: int, apply: bool) -> tuple[int, int]:
    if not WTA_BACKUP_DIR.is_dir():
        return 0, 0
    archives = sorted(WTA_BACKUP_DIR.glob("wta_sackmann_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    to_remove = archives[keep:]
    nbytes = sum(p.stat().st_size for p in to_remove)
    for p in to_remove:
        stem = p.name[: -len(".tar.gz")]
        _log(f"{'remove' if apply else 'would remove'} {p.name}")
        if apply:
            p.unlink(missing_ok=True)
            manifest = WTA_BACKUP_DIR / f"{stem}.manifest.json"
            manifest.unlink(missing_ok=True)
    return len(to_remove), nbytes


def purge_pip_cache(*, apply: bool) -> int:
    pip = shutil.which("pip") or str(ROOT / "venv" / "bin" / "pip")
    if not Path(pip).exists():
        _log("pip introuvable — skip cache")
        return 0
    cmd = [pip, "cache", "purge"]
    _log(f"{'run' if apply else 'would run'}: {' '.join(cmd)}")
    if apply:
        subprocess.run(cmd, check=False)
    cache = Path.home() / ".cache" / "pip"
    return sum(f.stat().st_size for f in cache.rglob("*") if f.is_file()) if cache.is_dir() else 0


def vacuum_journal(*, max_size: str, apply: bool) -> None:
    cmd = ["journalctl", f"--vacuum-size={max_size}"]
    _log(f"{'run' if apply else 'would run'}: {' '.join(cmd)}")
    if apply:
        subprocess.run(cmd, check=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Nettoyage disque PROD BettingHUD")
    ap.add_argument("--apply", action="store_true", help="Exécuter (sinon dry-run)")
    ap.add_argument("--db-keep", type=int, default=DEFAULT_DB_KEEP)
    ap.add_argument("--wta-keep", type=int, default=DEFAULT_WTA_KEEP)
    ap.add_argument("--journal-max", default=DEFAULT_JOURNAL_MAX)
    ap.add_argument("--skip-pip", action="store_true")
    ap.add_argument("--skip-journal", action="store_true")
    args = ap.parse_args(argv)

    mode = "APPLY" if args.apply else "DRY-RUN"
    _log(f"=== disk_cleanup_prod ({mode}) ===")

    n_db, b_db = prune_db_backups(keep=args.db_keep, apply=args.apply)
    n_wta, b_wta = prune_wta_archives(keep=args.wta_keep, apply=args.apply)
    _log(f"DB backups: {n_db} fichier(s), ~{b_db / (1024**2):.0f} Mo")
    _log(f"WTA archives: {n_wta} fichier(s), ~{b_wta / (1024**2):.0f} Mo")

    if not args.skip_pip:
        est = purge_pip_cache(apply=args.apply)
        if est:
            _log(f"pip cache estimé: ~{est / (1024**2):.0f} Mo")

    if not args.skip_journal:
        vacuum_journal(max_size=args.journal_max, apply=args.apply)

    if args.apply:
        try:
            out = subprocess.check_output(["df", "-h", "/"], text=True)
            _log("--- df -h / ---")
            _log(out.strip())
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
