"""Snapshot disque des matchs live (évite un rebuild ML complet à chaque démarrage Streamlit)."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_PATH = os.path.join(ROOT, "data", "cache", "live_matches_snapshot.joblib")
SNAPSHOT_META_PATH = SNAPSHOT_PATH + ".meta.json"
FULL_SNAPSHOT_PATH = os.path.join(ROOT, "data", "cache", "live_matches_snapshot.full.joblib")
FULL_SNAPSHOT_META_PATH = FULL_SNAPSHOT_PATH + ".meta.json"
NEXTDAY_SNAPSHOT_PATH = os.path.join(ROOT, "data", "cache", "live_matches_nextday.full.joblib")
NEXTDAY_SNAPSHOT_META_PATH = NEXTDAY_SNAPSHOT_PATH + ".meta.json"
BUILD_LOCK_PATH = os.path.join(ROOT, "data", "cache", ".live_snapshot_build.lock")
BUILD_PROGRESS_PATH = os.path.join(ROOT, "data", "cache", ".live_snapshot_build_progress.json")

# Cache RAM : évite de relire joblib à chaque rerun Streamlit (Live Tracker).
_SNAPSHOT_RAM: dict[str, Any] = {"file_mtime": 0.0, "signature": None, "matches": None}
# Cache pour load_latest_live_snapshot (bot Telegram, scripts headless).
_LATEST_SNAPSHOT_RAM: dict[str, Any] = {
    "path": "",
    "file_mtime": 0.0,
    "matches": None,
    "meta": None,
}

# Ordre des étapes `_mark()` dans `_build_live_matches_core` (dashboard.py).
LIVE_BUILD_PROGRESS_STEPS: tuple[tuple[str, str], ...] = (
    ("read_csv", "Lecture du CSV prematch"),
    ("filter_doubles", "Exclusion des doubles"),
    ("filter_major", "Filtre tournois ATP/WTA majeurs"),
    ("filter_today_tomorrow", "Filtre calendrier (aujourd'hui / demain)"),
    ("filter_upcoming", "Matchs à venir"),
    ("profile_scrape", "Profils Tennis Explorer"),
    ("identity+stats", "Identité et stats joueurs"),
    ("prepare_player_forms", "Forme, fatigue et signaux avancés"),
    ("rows_build+predict", "Prédictions ML par match"),
    ("finalize", "Finalisation"),
)
_LIVE_BUILD_STEP_INDEX = {k: i for i, (k, _) in enumerate(LIVE_BUILD_PROGRESS_STEPS)}
_LIVE_BUILD_STEP_LABEL = {k: lbl for k, lbl in LIVE_BUILD_PROGRESS_STEPS}
_LIVE_BUILD_TOTAL = len(LIVE_BUILD_PROGRESS_STEPS)


def live_signature_dict(
    csv_path: str,
    csv_mtime: float,
    profile_cache_schema: int,
    model_mtime: float,
    engines_cache_version: int,
) -> dict[str, Any]:
    """Signature snapshot (sans db_mtime : la sync WTA/TML ne doit pas invalider le cache live)."""
    return {
        "csv_path": str(csv_path or ""),
        "csv_mtime": float(csv_mtime or 0.0),
        "profile_cache_schema": int(profile_cache_schema),
        "model_mtime": float(model_mtime or 0.0),
        "engines_cache_version": int(engines_cache_version),
    }


_MODEL_SIGNATURE_KEYS = (
    "profile_cache_schema",
    "model_mtime",
    "engines_cache_version",
)


def signature_matches(current: dict[str, Any], stored: dict[str, Any] | None) -> bool:
    if not stored:
        return False
    for k in (
        "csv_path",
        "csv_mtime",
        *_MODEL_SIGNATURE_KEYS,
    ):
        if current.get(k) != stored.get(k):
            return False
    return True


def model_signature_dict(
    profile_cache_schema: int,
    model_mtime: float,
    engines_cache_version: int,
) -> dict[str, Any]:
    """Signature modèle / moteurs (sans CSV) — permet un patch cotes sans rebuild ML."""
    return {
        "profile_cache_schema": int(profile_cache_schema),
        "model_mtime": float(model_mtime or 0.0),
        "engines_cache_version": int(engines_cache_version),
    }


def signature_matches_model(current: dict[str, Any], stored: dict[str, Any] | None) -> bool:
    if not stored:
        return False
    for k in _MODEL_SIGNATURE_KEYS:
        if current.get(k) != stored.get(k):
            return False
    return True


def _snapshot_file_mtime(path: str = SNAPSHOT_PATH) -> float:
    try:
        return float(os.path.getmtime(path))
    except OSError:
        return 0.0


def invalidate_live_snapshot_ram_cache() -> None:
    _SNAPSHOT_RAM["file_mtime"] = 0.0
    _SNAPSHOT_RAM["signature"] = None
    _SNAPSHOT_RAM["matches"] = None


def _write_snapshot_meta(built_at: float, n_matches: int, signature: dict[str, Any]) -> None:
    _write_snapshot_meta_path(SNAPSHOT_META_PATH, built_at, n_matches, signature)


def _write_full_snapshot_meta(built_at: float, n_matches: int, signature: dict[str, Any]) -> None:
    _write_snapshot_meta_path(FULL_SNAPSHOT_META_PATH, built_at, n_matches, signature)


def _write_nextday_snapshot_meta(
    built_at: float,
    n_matches: int,
    signature: dict[str, Any],
    target_date: str,
) -> None:
    sig = dict(signature)
    sig["target_date"] = str(target_date)
    _write_snapshot_meta_path(NEXTDAY_SNAPSHOT_META_PATH, built_at, n_matches, sig)


def _write_snapshot_meta_path(
    meta_path: str,
    built_at: float,
    n_matches: int,
    signature: dict[str, Any],
) -> None:
    try:
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        tmp = meta_path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "built_at": float(built_at),
                    "n_matches": int(n_matches),
                    "signature": dict(signature),
                },
                f,
                ensure_ascii=False,
            )
        os.replace(tmp, meta_path)
    except OSError:
        pass


def _read_snapshot_meta_file() -> dict[str, Any] | None:
    if not os.path.isfile(SNAPSHOT_META_PATH):
        return None
    try:
        with open(SNAPSHOT_META_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _date_iso(value: object) -> str:
    try:
        return str(datetime.fromisoformat(str(value)[:10]).date())
    except Exception:
        return ""


def _normalize_nextday_match_for_target(match: dict[str, Any], target_date: str) -> dict[str, Any]:
    out = dict(match)
    out["date"] = str(target_date)
    t = str(out.get("time") or "").strip()
    if t.startswith("Demain"):
        out["time"] = t.replace("Demain", "", 1).strip() or t
    out["nextday_preloaded"] = True
    return out


def _save_nextday_snapshot_from_full(
    matches: list,
    signature: dict[str, Any],
    built_at: float,
) -> None:
    """Archive le J+1 d'un snapshot full pour promotion passive le lendemain."""
    try:
        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        next_matches: list[dict[str, Any]] = []
        for m in matches:
            if not isinstance(m, dict):
                continue
            m_date = _date_iso(m.get("date"))
            m_time = str(m.get("time") or "").strip()
            if m_date == tomorrow or m_time.startswith("Demain"):
                next_matches.append(_normalize_nextday_match_for_target(m, tomorrow))
        if not next_matches:
            return
        payload = {
            "signature": dict(signature),
            "built_at": float(built_at),
            "target_date": tomorrow,
            "matches": next_matches,
        }
        joblib.dump(payload, NEXTDAY_SNAPSHOT_PATH)
        _write_nextday_snapshot_meta(built_at, len(next_matches), signature, tomorrow)
    except Exception:
        pass


def save_live_snapshot(matches: list, signature: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    built_at = time.time()
    payload = {
        "signature": dict(signature),
        "built_at": built_at,
        "matches": matches,
    }
    joblib.dump(payload, SNAPSHOT_PATH)
    _write_snapshot_meta(built_at, len(matches), signature)
    # Conserve le dernier snapshot enrichi complet. Il sert de base stable quand un
    # nouveau CSV arrive : on patche les cotes dessus au lieu de remplacer par une preview.
    if matches and not any(str(m.get("snapshot_tier") or "full") == "preview" for m in matches):
        joblib.dump(payload, FULL_SNAPSHOT_PATH)
        _write_full_snapshot_meta(built_at, len(matches), signature)
        _save_nextday_snapshot_from_full(matches, signature, built_at)
    try:
        fm = _snapshot_file_mtime()
    except Exception:
        fm = built_at
    _SNAPSHOT_RAM["file_mtime"] = fm
    _SNAPSHOT_RAM["built_at"] = float(built_at)
    _SNAPSHOT_RAM["signature"] = dict(signature)
    _SNAPSHOT_RAM["matches"] = list(matches)


def load_live_snapshot(
    signature: dict[str, Any],
    *,
    max_age_sec: float,
) -> list | None:
    if not os.path.isfile(SNAPSHOT_PATH):
        return None
    try:
        file_mtime = _snapshot_file_mtime()
        if max_age_sec > 0 and (time.time() - file_mtime) > float(max_age_sec):
            return None
        if (
            _SNAPSHOT_RAM.get("file_mtime") == file_mtime
            and signature_matches(signature, _SNAPSHOT_RAM.get("signature"))
            and isinstance(_SNAPSHOT_RAM.get("matches"), list)
        ):
            return list(_SNAPSHOT_RAM["matches"])
        payload = joblib.load(SNAPSHOT_PATH)
        if not signature_matches(signature, payload.get("signature")):
            return None
        matches = payload.get("matches")
        if not isinstance(matches, list):
            return None
        out = list(matches)
        _SNAPSHOT_RAM["file_mtime"] = file_mtime
        _SNAPSHOT_RAM["built_at"] = float(payload.get("built_at") or 0.0)
        _SNAPSHOT_RAM["signature"] = dict(payload.get("signature") or {})
        _SNAPSHOT_RAM["matches"] = out
        return out
    except Exception:
        return None


def load_live_snapshot_by_model(
    model_signature: dict[str, Any],
    *,
    max_age_sec: float,
) -> list | None:
    """Charge le snapshot si le modèle correspond (ignore csv_path / csv_mtime)."""
    # Priorité au dernier full archivé : ne pas laisser une preview récente masquer
    # une version enrichie utilisable pour patcher les cotes.
    for path in (FULL_SNAPSHOT_PATH, SNAPSHOT_PATH):
        if not os.path.isfile(path):
            continue
        try:
            file_mtime = _snapshot_file_mtime(path)
            if max_age_sec > 0 and (time.time() - file_mtime) > float(max_age_sec):
                continue
            payload = joblib.load(path)
            if not signature_matches_model(model_signature, payload.get("signature")):
                continue
            matches = payload.get("matches")
            if not isinstance(matches, list):
                continue
            return list(matches)
        except Exception:
            continue
    return None


def load_nextday_snapshot_for_date(
    model_signature: dict[str, Any],
    target_date_iso: str,
    *,
    max_age_sec: float,
) -> list | None:
    """Charge le J+1 préchargé la veille pour `target_date_iso`."""
    if not os.path.isfile(NEXTDAY_SNAPSHOT_PATH):
        return None
    try:
        file_mtime = _snapshot_file_mtime(NEXTDAY_SNAPSHOT_PATH)
        if max_age_sec > 0 and (time.time() - file_mtime) > float(max_age_sec):
            return None
        payload = joblib.load(NEXTDAY_SNAPSHOT_PATH)
        if str(payload.get("target_date") or "") != str(target_date_iso):
            return None
        if not signature_matches_model(model_signature, payload.get("signature")):
            return None
        matches = payload.get("matches")
        if not isinstance(matches, list):
            return None
        return [
            _normalize_nextday_match_for_target(m, target_date_iso)
            for m in matches
            if isinstance(m, dict)
        ]
    except Exception:
        return None


def load_latest_live_snapshot(
    *,
    max_age_sec: float,
) -> tuple[list | None, dict[str, Any] | None]:
    """Charge le meilleur snapshot dispo (full puis preview) sans vérifier la signature CSV."""
    for path in (FULL_SNAPSHOT_PATH, SNAPSHOT_PATH):
        if not os.path.isfile(path):
            continue
        try:
            file_mtime = _snapshot_file_mtime(path)
            if max_age_sec > 0 and (time.time() - file_mtime) > float(max_age_sec):
                continue
            if (
                _LATEST_SNAPSHOT_RAM.get("path") == path
                and _LATEST_SNAPSHOT_RAM.get("file_mtime") == file_mtime
                and isinstance(_LATEST_SNAPSHOT_RAM.get("matches"), list)
                and isinstance(_LATEST_SNAPSHOT_RAM.get("meta"), dict)
            ):
                return list(_LATEST_SNAPSHOT_RAM["matches"]), dict(
                    _LATEST_SNAPSHOT_RAM["meta"]
                )
            payload = joblib.load(path)
            matches = payload.get("matches")
            if not isinstance(matches, list):
                continue
            built_at = float(payload.get("built_at") or file_mtime)
            meta = {
                "built_at": built_at,
                "n_matches": len(matches),
                "signature": payload.get("signature") or {},
                "snapshot_path": path,
            }
            _LATEST_SNAPSHOT_RAM["path"] = path
            _LATEST_SNAPSHOT_RAM["file_mtime"] = file_mtime
            _LATEST_SNAPSHOT_RAM["matches"] = list(matches)
            _LATEST_SNAPSHOT_RAM["meta"] = dict(meta)
            return list(matches), meta
        except Exception:
            continue
    return None, None


def snapshot_meta() -> dict[str, Any] | None:
    meta = _read_snapshot_meta_file()
    if meta is not None:
        return {
            "built_at": float(meta.get("built_at") or 0.0),
            "n_matches": int(meta.get("n_matches") or 0),
            "signature": meta.get("signature") or {},
        }
    if not os.path.isfile(SNAPSHOT_PATH):
        return None
    try:
        payload = joblib.load(SNAPSHOT_PATH)
        built_at = float(payload.get("built_at") or 0.0)
        n_matches = len(payload.get("matches") or [])
        signature = payload.get("signature") or {}
        _write_snapshot_meta(built_at, n_matches, signature)
        return {
            "built_at": built_at,
            "n_matches": n_matches,
            "signature": signature,
        }
    except Exception:
        return None


def _write_build_progress(payload: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(BUILD_PROGRESS_PATH), exist_ok=True)
        tmp = BUILD_PROGRESS_PATH + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, BUILD_PROGRESS_PATH)
    except OSError:
        pass


def clear_live_build_progress() -> None:
    try:
        if os.path.isfile(BUILD_PROGRESS_PATH):
            os.remove(BUILD_PROGRESS_PATH)
    except OSError:
        pass


def init_live_build_progress() -> None:
    _write_build_progress(
        {
            "started_at": time.time(),
            "step_key": "",
            "step_index": -1,
            "total_steps": _LIVE_BUILD_TOTAL,
            "label": "Démarrage…",
            "pct": 0.0,
            "done": False,
            "n_matches": None,
        }
    )


def advance_live_build_progress(step_key: str, *, n_matches: int | None = None) -> None:
    idx = _LIVE_BUILD_STEP_INDEX.get(str(step_key))
    if idx is None:
        return
    label = _LIVE_BUILD_STEP_LABEL.get(str(step_key), str(step_key))
    pct = float(idx + 1) / float(_LIVE_BUILD_TOTAL)
    started = time.time()
    try:
        if os.path.isfile(BUILD_PROGRESS_PATH):
            with open(BUILD_PROGRESS_PATH, encoding="utf-8") as f:
                prev = json.load(f)
            started = float(prev.get("started_at") or started)
    except Exception:
        pass
    _write_build_progress(
        {
            "started_at": started,
            "step_key": str(step_key),
            "step_index": int(idx),
            "total_steps": _LIVE_BUILD_TOTAL,
            "label": label,
            "pct": min(1.0, pct),
            "done": False,
            "n_matches": n_matches,
        }
    )


def complete_live_build_progress(n_matches: int) -> None:
    started = time.time()
    try:
        if os.path.isfile(BUILD_PROGRESS_PATH):
            with open(BUILD_PROGRESS_PATH, encoding="utf-8") as f:
                prev = json.load(f)
            started = float(prev.get("started_at") or started)
    except Exception:
        pass
    _write_build_progress(
        {
            "started_at": started,
            "step_key": "finalize",
            "step_index": _LIVE_BUILD_TOTAL - 1,
            "total_steps": _LIVE_BUILD_TOTAL,
            "label": "Terminé",
            "pct": 1.0,
            "done": True,
            "n_matches": int(n_matches),
        }
    )


def read_live_build_progress() -> dict[str, Any] | None:
    if not os.path.isfile(BUILD_PROGRESS_PATH):
        return None
    try:
        with open(BUILD_PROGRESS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def snapshot_build_in_progress(max_lock_sec: float = 1800.0) -> bool:
    if not os.path.exists(BUILD_LOCK_PATH):
        return False
    try:
        age = time.time() - os.path.getmtime(BUILD_LOCK_PATH)
    except OSError:
        return False
    if age > float(max_lock_sec):
        try:
            os.remove(BUILD_LOCK_PATH)
        except OSError:
            pass
        return False
    return True


def acquire_snapshot_build_lock() -> bool:
    if snapshot_build_in_progress():
        return False
    try:
        os.makedirs(os.path.dirname(BUILD_LOCK_PATH), exist_ok=True)
        with open(BUILD_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return True
    except OSError:
        return False


def release_snapshot_build_lock() -> None:
    try:
        if os.path.exists(BUILD_LOCK_PATH):
            os.remove(BUILD_LOCK_PATH)
    except OSError:
        pass
