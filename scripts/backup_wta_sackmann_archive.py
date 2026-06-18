#!/usr/bin/env python3
"""Archive timestampée de l'archive WTA Sackmann (data/raw/tennis_wta).

Crée un tarball .tar.gz + manifest.json (SHA256, row counts, max tourney_date).

Usage :
  python scripts/backup_wta_sackmann_archive.py
  python scripts/backup_wta_sackmann_archive.py --raw-dir data/raw/tennis_wta --retain 4
  python scripts/backup_wta_sackmann_archive.py --remote bettinghud --retain 8

Sur prod : archives sous /opt/bettinghud/data/backups/wta_sackmann/
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOGGER = logging.getLogger(__name__)

_WTA_DATE_RE = re.compile(r"^\d{8}$")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _csv_stats(path: Path) -> dict:
    rows = 0
    max_date: str | None = None
    try:
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows += 1
                td = (row.get("tourney_date") or "").strip()
                if _WTA_DATE_RE.match(td):
                    if max_date is None or td > max_date:
                        max_date = td
    except Exception as exc:
        return {"rows": None, "max_tourney_date": None, "error": str(exc)}
    return {"rows": rows, "max_tourney_date": max_date}


def _collect_files(raw_dir: Path) -> list[Path]:
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"raw-dir introuvable: {raw_dir}")
    patterns = (
        "wta_matches_*.csv",
        "wta_players.csv",
        "wta_rankings_current.csv",
    )
    found: list[Path] = []
    for pat in patterns:
        found.extend(sorted(raw_dir.glob(pat)))
    # Exclure doubles si présents
    found = [p for p in found if "doubles" not in p.name.lower()]
    if not found:
        raise FileNotFoundError(f"Aucun CSV WTA dans {raw_dir}")
    return found


def build_manifest(raw_dir: Path, files: list[Path], archive_name: str) -> dict:
    entries = []
    for p in files:
        rel = p.relative_to(raw_dir).as_posix()
        st = p.stat()
        entry = {
            "path": rel,
            "bytes": st.st_size,
            "sha256": _sha256_file(p),
            "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        }
        if p.suffix.lower() == ".csv" and p.name.startswith("wta_matches"):
            entry.update(_csv_stats(p))
        entries.append(entry)
    return {
        "archive": archive_name,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(raw_dir.resolve()),
        "file_count": len(entries),
        "files": entries,
    }


def create_archive(
    raw_dir: Path,
    backup_dir: Path,
    *,
    retain: int,
) -> tuple[Path, Path]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_name = f"wta_sackmann_{stamp}.tar.gz"
    archive_path = backup_dir / archive_name
    manifest_path = backup_dir / f"wta_sackmann_{stamp}.manifest.json"

    files = _collect_files(raw_dir)
    manifest = build_manifest(raw_dir, files, archive_name)

    with tarfile.open(archive_path, "w:gz") as tar:
        for p in files:
            tar.add(p, arcname=p.relative_to(raw_dir).as_posix())

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    LOGGER.info("Archive OK: %s (%.2f Mo, %d fichiers)", archive_path, size_mb, len(files))

    if retain > 0:
        archives = sorted(backup_dir.glob("wta_sackmann_*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in archives[retain:]:
            LOGGER.info("Retention: suppression %s", old.name)
            old.unlink(missing_ok=True)
            stem = old.name[: -len(".tar.gz")] if old.name.endswith(".tar.gz") else old.stem
            (backup_dir / f"{stem}.manifest.json").unlink(missing_ok=True)

    return archive_path, manifest_path


def run_remote_backup(
    ssh_host: str,
    *,
    remote_root: str,
    raw_dir: str,
    backup_dir: str,
    retain: int,
    remote_python: str,
) -> int:
    script_local = ROOT / "scripts" / "backup_wta_sackmann_archive.py"
    script_remote = "/tmp/bettinghud_backup_wta_sackmann.py"
    if not script_local.is_file():
        print(f"Script local introuvable: {script_local}", file=sys.stderr)
        return 1

    scp = shutil.which("scp")
    ssh = shutil.which("ssh")
    if not scp or not ssh:
        print("scp/ssh requis pour --remote", file=sys.stderr)
        return 1

    subprocess.check_call([scp, str(script_local), f"{ssh_host}:{script_remote}"])
    cmd = [
        ssh,
        ssh_host,
        remote_python,
        script_remote,
        "--raw-dir",
        raw_dir,
        "--backup-dir",
        backup_dir,
        "--retain",
        str(retain),
    ]
    rc = subprocess.call(cmd)
    subprocess.call([ssh, ssh_host, "rm", "-f", script_remote])
    return int(rc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backup archive WTA Sackmann (tar.gz + manifest).")
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "raw" / "tennis_wta"))
    ap.add_argument("--backup-dir", default=str(ROOT / "data" / "backups" / "wta_sackmann"))
    ap.add_argument("--retain", type=int, default=4, help="Conserver les N dernières archives (0 = illimité)")
    ap.add_argument("--remote", default=None, help="Hôte SSH (ex. bettinghud) — exécute le backup sur le serveur")
    ap.add_argument("--remote-root", default="/opt/bettinghud")
    ap.add_argument("--remote-python", default="/opt/bettinghud/venv/bin/python")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    if args.remote:
        raw = f"{args.remote_root}/data/raw/tennis_wta"
        bdir = f"{args.remote_root}/data/backups/wta_sackmann"
        rc = run_remote_backup(
            args.remote,
            remote_root=args.remote_root,
            raw_dir=raw,
            backup_dir=bdir,
            retain=args.retain,
            remote_python=args.remote_python,
        )
        if rc == 0:
            print(f"Backup distant OK -> {args.remote}:{bdir}")
        return rc

    raw_dir = Path(args.raw_dir)
    backup_dir = Path(args.backup_dir)
    try:
        archive_path, manifest_path = create_archive(raw_dir, backup_dir, retain=args.retain)
    except Exception as exc:
        print(f"ERREUR backup: {exc}", file=sys.stderr)
        return 1

    print(f"OK — {archive_path}")
    print(f"     manifest: {manifest_path}")
    print(f"     Copie hors-site suggérée : scp {archive_path} <stockage_externe>/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
