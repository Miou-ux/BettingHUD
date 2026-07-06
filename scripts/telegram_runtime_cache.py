"""Cache runtime pour le bot Telegram (snapshot + bundle ML + picks pré-calculés).

Évite de recharger joblib et le modèle à chaque /jour ou /top5.
Invalidation automatique quand le snapshot full change (mtime).
"""
from __future__ import annotations

import copy
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
_precomputed_mtime: float = 0.0
_precomputed_picks: dict[tuple, Any] = {}


def _snapshot_file_mtime() -> float:
    try:
        return os.path.getmtime(FULL_SNAPSHOT_PATH)
    except OSError:
        return 0.0


def _pick_cache_key(
    mode: Any,
    channel: Any,
    limit: int | None,
    ev_min_pct: float,
    ev_max_pct: float,
) -> tuple:
    mode_v = mode.value if hasattr(mode, "value") else str(mode)
    channel_v = channel.value if hasattr(channel, "value") else str(channel)
    return (
        mode_v,
        channel_v,
        int(limit) if limit is not None and int(limit) > 0 else None,
        round(float(ev_min_pct), 4),
        round(float(ev_max_pct), 4),
    )


def get_cached_pick_load_result(
    mode: Any,
    channel: Any,
    *,
    limit: int | None,
    ev_min_pct: float,
    ev_max_pct: float,
) -> Any | None:
    """Retourne une copie des picks pré-calculés si le snapshot n'a pas changé."""
    if os.getenv("TELEGRAM_PICKS_CACHE", "1").strip().lower() in ("0", "false", "no"):
        return None
    mtime = _snapshot_file_mtime()
    if mtime <= 0 or mtime != _precomputed_mtime:
        return None
    key = _pick_cache_key(mode, channel, limit, ev_min_pct, ev_max_pct)
    hit = _precomputed_picks.get(key)
    if hit is None:
        return None
    return copy.deepcopy(hit)


def store_cached_pick_load_result(
    result: Any,
    mode: Any,
    channel: Any,
    *,
    limit: int | None,
    ev_min_pct: float,
    ev_max_pct: float,
) -> None:
    global _precomputed_mtime
    mtime = _snapshot_file_mtime()
    if mtime <= 0:
        return
    if mtime != _precomputed_mtime:
        _precomputed_picks.clear()
        _precomputed_mtime = mtime
    key = _pick_cache_key(mode, channel, limit, ev_min_pct, ev_max_pct)
    _precomputed_picks[key] = copy.deepcopy(result)


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
    global _today_matches, _snapshot_meta, _snapshot_mtime, _precomputed_picks, _precomputed_mtime
    _today_matches = None
    _snapshot_meta = None
    _snapshot_mtime = 0.0
    _precomputed_picks.clear()
    _precomputed_mtime = 0.0


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


def _warm_precomputed_picks() -> None:
    from scripts.pick_modes import Channel, PickMode, load_picks

    limit_raw = (os.getenv("TELEGRAM_DAILY_PICKS_LIMIT") or "0").strip()
    limit = int(limit_raw) if limit_raw.isdigit() and int(limit_raw) > 0 else None
    ev_today = float((os.getenv("TELEGRAM_JOUR_EV_MIN_PCT") or "15").strip() or 15)
    t0 = time.perf_counter()
    load_picks(PickMode.TOP5, channel=Channel.TELEGRAM)
    load_picks(
        PickMode.TODAY,
        channel=Channel.TELEGRAM,
        limit=limit,
        ev_min_pct=ev_today,
    )
    load_picks(PickMode.ONE_PICK_ONE_DAY, channel=Channel.TELEGRAM)
    LOGGER.info("Picks Telegram precomputes en %.2f s", time.perf_counter() - t0)


def warm_telegram_runtime_cache() -> None:
    """Précharge ML + snapshot + picks /top5 et /today (bot quasi instantané)."""
    try:
        get_ml_model()
        matches, meta = get_today_matches_cached()
        _warm_precomputed_picks()
        LOGGER.info(
            "Warm cache Telegram OK — %d match(s), built_at=%s, picks=%d",
            len(matches),
            meta.get("built_at"),
            len(_precomputed_picks),
        )
    except Exception as exc:
        LOGGER.warning("Warm cache Telegram partiel : %s", exc)
