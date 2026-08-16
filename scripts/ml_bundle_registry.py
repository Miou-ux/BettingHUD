"""Registre du bundle ML actif — rollback v47 sans toucher au code métier.

Fichiers :
  models/baselines/xgb_model_tml_v47_prod_baseline.pkl  — copie figée (rollback)
  models/.ml_bundle_active                              — chemin relatif du bundle actif
  models/candidates/                                    — entraînements v48+ (jamais écrasent v47)

Priorité de résolution :
  1. BETTINGHUD_ML_BUNDLE (chemin relatif ou absolu)
  2. models/.ml_bundle_active
  3. models/xgb_model_tml_v47.pkl (défaut code)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BUNDLE_REL = Path("models") / "xgb_model_tml_v47.pkl"
BASELINE_REL = Path("models") / "baselines" / "xgb_model_tml_v47_prod_baseline.pkl"
ACTIVE_POINTER_REL = Path("models") / ".ml_bundle_active"
MANIFEST_REL = Path("models") / "baselines" / "ml_bundle_manifest.json"
CANDIDATES_DIR = Path("models") / "candidates"


def _repo_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _abs_from_rel(rel: str | Path) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return (ROOT / p).resolve()


def resolve_active_bundle_rel() -> str:
    env = (os.getenv("BETTINGHUD_ML_BUNDLE") or "").strip()
    if env:
        p = _abs_from_rel(env)
        if p.is_file():
            return _repo_rel(p)
    pointer = ROOT / ACTIVE_POINTER_REL
    if pointer.is_file():
        rel = pointer.read_text(encoding="utf-8").strip()
        if rel and _abs_from_rel(rel).is_file():
            return rel.replace("\\", "/")
    return str(DEFAULT_BUNDLE_REL).replace("\\", "/")


def resolve_active_bundle_abspath() -> str:
    return str(_abs_from_rel(resolve_active_bundle_rel()))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict[str, Any]:
    mp = ROOT / MANIFEST_REL
    if not mp.is_file():
        return {}
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(data: dict[str, Any]) -> None:
    mp = ROOT / MANIFEST_REL
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def freeze_v47_baseline(*, source: Path | None = None) -> dict[str, Any]:
    """Copie le bundle v47 courant vers baselines/ (idempotent si même SHA)."""
    src = source or (ROOT / DEFAULT_BUNDLE_REL)
    src = src.resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Bundle source introuvable: {src}")

    dst = (ROOT / BASELINE_REL).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)

    entry = {
        "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_rel": _repo_rel(src),
        "baseline_rel": _repo_rel(dst),
        "sha256": _sha256(dst),
        "size_bytes": dst.stat().st_size,
    }
    manifest = _load_manifest()
    manifest["v47_prod_baseline"] = entry
    _save_manifest(manifest)
    return entry


def set_active_bundle(rel_path: str | Path) -> str:
    rel = _repo_rel(_abs_from_rel(rel_path))
    ap = _abs_from_rel(rel)
    if not ap.is_file():
        raise FileNotFoundError(f"Bundle actif introuvable: {ap}")
    pointer = ROOT / ACTIVE_POINTER_REL
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(rel + "\n", encoding="utf-8")
    manifest = _load_manifest()
    manifest["active"] = {
        "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bundle_rel": rel,
        "sha256": _sha256(ap),
    }
    _save_manifest(manifest)
    return rel


def rollback_to_v47_baseline() -> str:
    baseline = (ROOT / BASELINE_REL).resolve()
    if not baseline.is_file():
        raise FileNotFoundError(
            f"Baseline absente: {baseline}. Lancez: python scripts/ml_bundle_cli.py freeze"
        )
    # Restaurer aussi le fichier défaut v47 (compat code / scp habituel)
    default = (ROOT / DEFAULT_BUNDLE_REL).resolve()
    default.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(baseline, default)
    return set_active_bundle(default)


def candidate_output_path(name: str = "xgb_model_tml_v48_candidate.pkl") -> Path:
    d = ROOT / CANDIDATES_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def promote_candidate(candidate_rel: str | Path, *, target_name: str = "xgb_model_tml_v48.pkl") -> str:
    """Promouvoir un candidat validé — n'écrase pas la baseline figée."""
    cand = _abs_from_rel(candidate_rel)
    if not cand.is_file():
        raise FileNotFoundError(cand)
    promoted = ROOT / "models" / target_name
    shutil.copy2(cand, promoted)
    return set_active_bundle(promoted)


def status_report() -> dict[str, Any]:
    baseline = ROOT / BASELINE_REL
    default = ROOT / DEFAULT_BUNDLE_REL
    active_rel = resolve_active_bundle_rel()
    active_abs = _abs_from_rel(active_rel)
    out: dict[str, Any] = {
        "active_rel": active_rel,
        "active_exists": active_abs.is_file(),
        "env_override": (os.getenv("BETTINGHUD_ML_BUNDLE") or "").strip() or None,
        "pointer_file": str(ACTIVE_POINTER_REL).replace("\\", "/"),
        "default_rel": str(DEFAULT_BUNDLE_REL).replace("\\", "/"),
        "baseline_rel": str(BASELINE_REL).replace("\\", "/"),
        "baseline_exists": baseline.is_file(),
        "default_exists": default.is_file(),
        "manifest": _load_manifest(),
    }
    if active_abs.is_file():
        out["active_sha256"] = _sha256(active_abs)
        out["active_size_bytes"] = active_abs.stat().st_size
    if baseline.is_file():
        out["baseline_sha256"] = _sha256(baseline)
    tour = _tour_routing_status_safe()
    if tour:
        out["tour_routing"] = tour
    return out


def _tour_routing_status_safe() -> dict[str, Any] | None:
    try:
        from scripts.ml_tour_router import routing_status

        return routing_status()
    except Exception:
        return None
