"""Cache runtime pour le bot Telegram (snapshot + bundle ML).

Évite de recharger joblib et le modèle à chaque /jour ou /top5.
Invalidation automatique quand le snapshot full change (mtime).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from scripts.ml_model import TennisMLModel

LOGGER = logging.getLogger("telegram_runtime_cache")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL_SNAPSHOT_PATH = os.path.join(
    ROOT, "data", "cache", "live_matches_snapshot.full.joblib"
)

_ml: TennisMLModel | None = None
_snapshot_mtime: float = 0.0
_today_matches: list[dict] | None = None
_snapshot_meta: dict[str, Any] | None = None


def _snapshot_file_mtime() -> float:
    try:
        return os.path.getmtime(FULL_SNAPSHOT_PATH)
    except OSError:
        return 0.0


def get_ml_model() -> TennisMLModel:
    global _ml
    if _ml is None:
        LOGGER.info("Chargement bundle ML (cache Telegram)…")
        t0 = time.perf_counter()
        _ml = TennisMLModel()
        if hasattr(_ml, "_load_bundle_if_needed"):
            _ml._load_bundle_if_needed()
        LOGGER.info("Bundle ML pret en %.2f s", time.perf_counter() - t0)
    return _ml


def invalidate_snapshot_cache() -> None:
    global _today_matches, _snapshot_meta, _snapshot_mtime
    _today_matches = None
    _snapshot_meta = None
    _snapshot_mtime = 0.0


def get_today_matches_cached(
    *,
    max_age_sec: float | None = None,
    force_reload: bool = False,
) -> tuple[list[dict], dict[str, Any]]:
    """Matchs du jour (snapshot live filtré), avec cache RAM."""
    global _today_matches, _snapshot_meta, _snapshot_mtime

    mtime = _snapshot_file_mtime()
    if (
        not force_reload
        and _today_matches is not None
        and _snapshot_meta is not None
        and mtime > 0
        and mtime == _snapshot_mtime
    ):
        return list(_today_matches), dict(_snapshot_meta)

    from scripts.daily_top_proba_store import load_today_matches_for_daily_top_proba

    t0 = time.perf_counter()
    matches, meta = load_today_matches_for_daily_top_proba(max_age_sec=max_age_sec)
    _today_matches = list(matches)
    _snapshot_meta = dict(meta or {})
    _snapshot_mtime = mtime
    LOGGER.info(
        "Snapshot Telegram recharge : %d match(s) jour en %.2f s",
        len(_today_matches),
        time.perf_counter() - t0,
    )
    return list(_today_matches), dict(_snapshot_meta)


def warm_telegram_runtime_cache() -> None:
    """Précharge ML + snapshot (appel au démarrage du daemon)."""
    try:
        get_ml_model()
        matches, meta = get_today_matches_cached()
        LOGGER.info(
            "Warm cache Telegram OK — %d match(s), built_at=%s",
            len(matches),
            meta.get("built_at"),
        )
    except Exception as exc:
        LOGGER.warning("Warm cache Telegram partiel : %s", exc)
