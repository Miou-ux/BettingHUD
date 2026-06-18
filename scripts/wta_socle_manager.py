#!/usr/bin/env python3
"""Gestion du socle WTA Sackmann immuable (preprod).

Le socle est extrait du tarball de référence et ne doit jamais être modifié.
Les expériences delta écrivent uniquement dans ``data/preprod/wta_work/``.

Usage :
  python scripts/wta_socle_manager.py verify
  python scripts/wta_socle_manager.py init --from-archive data/backups/wta_sackmann_offsite/wta_sackmann_20260617_130955.tar.gz
  python scripts/wta_socle_manager.py refresh-work
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOCLE_DIR = ROOT / "data" / "archives" / "wta_sackmann_socle"
WORK_DIR = ROOT / "data" / "preprod" / "wta_work" / "tennis_wta"
SOCLE_META = SOCLE_DIR / ".socle_meta.json"
DEFAULT_ARCHIVE = ROOT / "data" / "backups" / "wta_sackmann_offsite" / "wta_sackmann_20260617_130955.tar.gz"
DEFAULT_MANIFEST = ROOT / "data" / "backups" / "wta_sackmann_offsite" / "wta_sackmann_20260617_130955.manifest.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_backup(archive: Path, manifest: Path) -> dict:
    if not archive.is_file():
        raise FileNotFoundError(f"Archive introuvable: {archive}")
    if not manifest.is_file():
        raise FileNotFoundError(f"Manifest introuvable: {manifest}")
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    got = _sha256(archive)
    return {"archive": str(archive), "sha256": got, "manifest": meta, "file_count": meta.get("file_count")}


def init_socle(archive: Path, manifest: Path, *, force: bool = False) -> Path:
    info = verify_backup(archive, manifest)
    if SOCLE_META.is_file() and not force:
        existing = json.loads(SOCLE_META.read_text(encoding="utf-8"))
        if existing.get("archive_sha256") == info["sha256"]:
            print(f"Socle déjà initialisé: {SOCLE_DIR}")
            return SOCLE_DIR

    if SOCLE_DIR.exists() and force:
        shutil.rmtree(SOCLE_DIR)
    SOCLE_DIR.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(SOCLE_DIR)

    meta = {
        "immutable": True,
        "source_archive": str(archive.resolve()),
        "archive_sha256": info["sha256"],
        "manifest_path": str(manifest.resolve()),
        "file_count": info["file_count"],
        "instruction": "NE PAS MODIFIER — copier vers wta_work via refresh-work",
    }
    SOCLE_META.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    readme = SOCLE_DIR / "README_DO_NOT_EDIT.txt"
    readme.write_text(
        "SOCLE WTA SACKMANN IMMUABLE\n"
        f"Archive: {archive.name}\n"
        f"SHA256: {info['sha256']}\n"
        "Modifier uniquement data/preprod/wta_work/tennis_wta/\n",
        encoding="utf-8",
    )
    print(f"Socle extrait -> {SOCLE_DIR} ({info['file_count']} fichiers)")
    return SOCLE_DIR


def refresh_work(*, force: bool = False) -> Path:
    if not SOCLE_DIR.is_dir() or not SOCLE_META.is_file():
        raise RuntimeError("Socle absent — lancer: python scripts/wta_socle_manager.py init")

    if WORK_DIR.exists():
        if force:
            shutil.rmtree(WORK_DIR)
        else:
            raise RuntimeError(f"Work dir existe déjà: {WORK_DIR} (utiliser --force)")

    WORK_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOCLE_DIR, WORK_DIR, ignore=shutil.ignore_patterns("README_DO_NOT_EDIT.txt", ".socle_meta.json"))
    print(f"Copie socle -> work: {WORK_DIR}")
    return WORK_DIR


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Socle WTA Sackmann immuable (preprod).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_v = sub.add_parser("verify", help="Vérifier tarball + manifest")
    p_v.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    p_v.add_argument("--manifest", default=str(DEFAULT_MANIFEST))

    p_i = sub.add_parser("init", help="Extraire le socle depuis le tarball")
    p_i.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    p_i.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    p_i.add_argument("--force", action="store_true")

    p_r = sub.add_parser("refresh-work", help="Copier socle -> preprod work dir")
    p_r.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "verify":
            info = verify_backup(Path(args.archive), Path(args.manifest))
            print(json.dumps(info, indent=2, ensure_ascii=False))
            return 0
        if args.cmd == "init":
            init_socle(Path(args.archive), Path(args.manifest), force=args.force)
            return 0
        if args.cmd == "refresh-work":
            refresh_work(force=args.force)
            return 0
    except Exception as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
