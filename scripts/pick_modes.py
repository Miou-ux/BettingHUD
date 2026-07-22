"""Sélection unifiée des picks — web, Telegram, Discord.

Modes :
  - ``1pick1day`` : meilleur pick de la sélection hybride du jour (même logique que Top 5, rank 1)
  - ``top5``      : sélection hybride P≥77 %, rel≥85 (repli ≥80 si vide), EV tier1/tier2, gap≤30 pp, tri proba, max 6
  - ``today``     : Live Tracker / Today's Pick — value bets EV ≥15 %, majeurs + mineurs
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

PARIS_TZ = ZoneInfo("Europe/Paris")

DEFAULT_EV_MIN_PCT = 15.0
DEFAULT_EV_MAX_PCT = 100.0


class PickMode(str, Enum):
    ONE_PICK_ONE_DAY = "1pick1day"
    TOP5 = "top5"
    TODAY = "today"


class Channel(str, Enum):
    WEB = "web"
    TELEGRAM = "telegram"
    DISCORD = "discord"


@dataclass
class PickLoadResult:
    picks: list[dict]
    meta: dict[str, Any]
    calendar_date: str
    pool_n: int
    snapshot_age_min: float | None
    mode: PickMode
    pick_today: dict[str, Any] | None = None


def _snapshot_age_min(meta: dict | None) -> float | None:
    built = (meta or {}).get("built_at") or (meta or {}).get("mtime")
    if not built:
        return None
    try:
        return max(0.0, (datetime.now(PARIS_TZ).timestamp() - float(built)) / 60.0)
    except (TypeError, ValueError):
        return None


def _count_today_pool(matches: list[dict]) -> int:
    from scripts.daily_top_proba_store import is_today_paris_match

    cal = datetime.now(PARIS_TZ).date()
    return sum(1 for m in matches if is_today_paris_match(m, today=cal))


def _load_matches_cached():
    try:
        from scripts.telegram_runtime_cache import get_ml_model, get_today_matches_cached

        matches, meta = get_today_matches_cached()
        return matches, meta, get_ml_model()
    except Exception:
        from scripts.daily_top_proba_store import load_today_matches_for_daily_top_proba

        matches, meta = load_today_matches_for_daily_top_proba()
        return matches, meta, None


def load_picks(
    mode: PickMode | str,
    *,
    channel: Channel | str = Channel.WEB,
    limit: int | None = None,
    ev_min_pct: float = DEFAULT_EV_MIN_PCT,
    ev_max_pct: float = DEFAULT_EV_MAX_PCT,
    db_path: str | None = None,
) -> PickLoadResult:
    """Charge les picks pour un mode (logique alignée web ; TG = filtres affichage)."""
    if isinstance(mode, PickMode):
        mode_enum = mode
    else:
        mode_enum = PickMode(str(mode).strip().lower())
    if isinstance(channel, Channel):
        channel_enum = channel
    else:
        channel_enum = Channel(str(channel).strip().lower())
    cal_day = datetime.now(PARIS_TZ).date().isoformat()

    if channel_enum == Channel.TELEGRAM:
        try:
            from scripts.telegram_runtime_cache import get_cached_pick_load_result

            cached = get_cached_pick_load_result(
                mode_enum,
                channel_enum,
                limit=limit,
                ev_min_pct=ev_min_pct,
                ev_max_pct=ev_max_pct,
            )
            if cached is not None:
                return cached
        except Exception:
            pass

    def _store_telegram_cache(result: PickLoadResult) -> PickLoadResult:
        if channel_enum == Channel.TELEGRAM:
            try:
                from scripts.telegram_runtime_cache import store_cached_pick_load_result

                store_cached_pick_load_result(
                    result,
                    mode_enum,
                    channel_enum,
                    limit=limit,
                    ev_min_pct=ev_min_pct,
                    ev_max_pct=ev_max_pct,
                )
            except Exception:
                pass
        return result

    if mode_enum == PickMode.ONE_PICK_ONE_DAY:
        from scripts.bets_db import DB_PATH_DEFAULT
        from scripts.discord_1d1p_core import load_1d1p_today_pick

        path = db_path or DB_PATH_DEFAULT
        pick, cal_day, pool_n, snapshot_age_min = load_1d1p_today_pick(
            db_path=path,
            calendar_date=cal_day,
            ev_min_pct=ev_min_pct,
            ev_max_pct=ev_max_pct,
        )
        picks = [pick] if pick else []
        return PickLoadResult(
            picks=picks,
            meta={},
            calendar_date=cal_day,
            pool_n=pool_n,
            snapshot_age_min=snapshot_age_min,
            mode=mode_enum,
            pick_today=pick,
        )

    if mode_enum == PickMode.TOP5:
        from scripts.hybrid_pick_selection import HYBRID_DEFAULT_LIMIT
        from scripts.daily_top_proba_store import collect_daily_ev_band_picks
        from scripts.telegram_top5_notify import filter_telegram_display_picks

        matches, meta, ml = _load_matches_cached()
        picks = collect_daily_ev_band_picks(
            matches,
            limit=None,
            ev_min_frac=ev_min_pct / 100.0,
            ev_max_frac=ev_max_pct / 100.0,
            major_only=True,
            calendar_date=cal_day,
            ml=ml,
        )
        if channel_enum == Channel.TELEGRAM:
            picks = filter_telegram_display_picks(picks)
        cap = int(limit) if limit is not None and limit > 0 else HYBRID_DEFAULT_LIMIT
        picks = picks[:cap]
        return _store_telegram_cache(
            PickLoadResult(
                picks=picks,
                meta=meta or {},
                calendar_date=cal_day,
                pool_n=_count_today_pool(matches),
                snapshot_age_min=_snapshot_age_min(meta),
                mode=mode_enum,
            )
        )

    if mode_enum == PickMode.TODAY:
        if channel_enum == Channel.TELEGRAM:
            from scripts.live_tracker_picks import load_live_tracker_day_picks_telegram
        else:
            from scripts.live_tracker_picks import load_live_tracker_day_picks as load_live_tracker_day_picks_telegram
        from scripts.telegram_top5_notify import filter_telegram_display_picks

        picks, meta, scanned = load_live_tracker_day_picks_telegram(
            ev_threshold_pct=ev_min_pct
        )
        if channel_enum == Channel.TELEGRAM:
            picks = filter_telegram_display_picks(picks, apply_proba_filter=False)
        if limit is not None and limit > 0:
            picks = picks[: int(limit)]
        return _store_telegram_cache(
            PickLoadResult(
                picks=picks,
                meta=meta or {},
                calendar_date=cal_day,
                pool_n=scanned,
                snapshot_age_min=_snapshot_age_min(meta),
                mode=mode_enum,
            )
        )

    raise ValueError(f"Unknown pick mode: {mode}")
