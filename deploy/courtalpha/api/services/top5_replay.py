"""Replay Top picks du jour (HYB P75+P80-all) — pool matin JSONL + Kelly fresh."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from api.services.hybrid_selection_text import hybrid_selection_description
from api.services.one_day_one_pick import (
    DEFAULT_BANKROLL_EUR,
    EV_MAX_PCT,
    EV_MIN_PCT,
    _merge_today_settlement,
    _resolve_tracking_bankroll,
    _serialize_pick,
    _status_flags,
    PARIS,
)


def _cap_picks(items: list, limit: int | None) -> list:
    if limit is None or int(limit) <= 0:
        return items
    return items[: int(limit)]


def _select_top5_per_day(
    *,
    db_path: str,
    exclude_date: str | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    from scripts.live_replay_engine import load_ml, select_historical_top5_live

    ml = load_ml()
    return select_historical_top5_live(
        db_path,
        ml,
        exclude_date=exclude_date,
        limit=limit,
    )


def _resolve_today_picks(
    *,
    limit: int | None = None,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
) -> tuple[list[dict[str, Any]], str | None]:
    from scripts.pick_modes import Channel, PickMode, load_picks

    res = load_picks(
        PickMode.TOP5,
        channel=Channel.WEB,
        limit=limit,
        ev_min_pct=ev_min_pct,
        ev_max_pct=ev_max_pct,
    )
    if not res.picks:
        return [], res.calendar_date
    out: list[dict[str, Any]] = []
    for rank, pick in enumerate(_cap_picks(res.picks, limit), start=1):
        row = dict(pick)
        row["rank"] = rank
        row["is_today"] = True
        row["source"] = "live"
        out.append(row)
    return out, res.calendar_date


def _build_daily_summaries(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in picks:
        cal = str(row.get("calendar_date") or "")
        if cal:
            by_day[cal].append(row)

    daily: list[dict[str, Any]] = []
    for cal in sorted(by_day.keys()):
        rows = by_day[cal]
        n_won = n_lost = n_void = n_open = 0
        daily_profit = 0.0
        for row in rows:
            flags = _status_flags(row.get("status"))
            if flags["won"]:
                n_won += 1
            elif flags["lost"]:
                n_lost += 1
            elif flags["void"]:
                n_void += 1
            elif flags["open"]:
                n_open += 1
            rep = row.get("replay_net_profit_eur")
            if rep is not None:
                daily_profit += float(rep)
        n_decided = n_won + n_lost
        hit_pct = (n_won / n_decided * 100.0) if n_decided > 0 else 0.0
        daily.append(
            {
                "date": cal,
                "n_picks": len(rows),
                "n_won": n_won,
                "n_lost": n_lost,
                "n_void": n_void,
                "n_open": n_open,
                "hit_pct": round(hit_pct, 1),
                "daily_profit_eur": round(daily_profit, 2),
            }
        )
    return list(reversed(daily))


def _build_tracked_top5_picks(
    *,
    db_path: str,
    today: str,
    today_picks: list[dict[str, Any]],
    exclude_today: bool,
    limit: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from scripts.bets_db import open_db
    from scripts.portfolio_tracking_store import (
        build_portfolio_curve_and_summary,
        load_portfolio_replay_picks,
        refresh_portfolio_tracking,
        sync_portfolio_from_published,
    )
    from scripts.published_picks_store import MODE_TOP5

    conn = open_db(db_path)
    try:
        refresh_portfolio_tracking(conn)
        sync_portfolio_from_published(conn, mode=MODE_TOP5, calendar_date=today)
        refresh_portfolio_tracking(conn)
        picks_raw = load_portfolio_replay_picks(conn, MODE_TOP5, exclude_date=None)
        if not exclude_today and today_picks:
            picks_raw = [p for p in picks_raw if str(p.get("calendar_date") or "") != today]
            for rank, row in enumerate(_cap_picks(today_picks, limit), start=1):
                merged = _merge_today_settlement(db_path, dict(row))
                merged["rank"] = rank
                merged["is_today"] = True
                picks_raw.append(merged)
            picks_raw.sort(
                key=lambda r: (str(r.get("calendar_date") or ""), int(r.get("rank") or 99))
            )
        else:
            for p in picks_raw:
                if str(p.get("calendar_date") or "") == today:
                    merged = _merge_today_settlement(db_path, dict(p))
                    p.update(merged)
                    p["is_today"] = True
        curve, summary = build_portfolio_curve_and_summary(conn, MODE_TOP5)
        return picks_raw, curve, summary
    finally:
        conn.close()


def build_top5_replay(
    *,
    db_path: str,
    bankroll_start: float = DEFAULT_BANKROLL_EUR,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
    exclude_today: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Replay Top picks du jour : pool matin JSONL + picks publiés + Kelly séquentiel fresh."""
    from scripts.live_replay_engine import kelly_replay_metrics, load_ml
    from scripts.published_picks_store import MODE_TOP5

    today = datetime.now(PARIS).date().isoformat()
    ml = load_ml()

    bankroll_start, tracking_start = _resolve_tracking_bankroll(
        db_path, mode=MODE_TOP5, default=float(bankroll_start)
    )

    today_picks, _ = _resolve_today_picks(
        limit=limit,
        ev_min_pct=float(ev_min_pct),
        ev_max_pct=float(ev_max_pct),
    )

    if tracking_start is not None:
        enriched, curve, summary = _build_tracked_top5_picks(
            db_path=db_path,
            today=today,
            today_picks=today_picks,
            exclude_today=exclude_today,
            limit=limit,
        )
    else:
        picks_raw = _select_top5_per_day(
            db_path=db_path,
            exclude_date=today,
            limit=limit,
        )
        if not exclude_today and today_picks:
            picks_raw = [r for r in picks_raw if str(r.get("calendar_date") or "") != today]
            for row in today_picks:
                live = _merge_today_settlement(db_path, dict(row))
                live["is_today"] = True
                picks_raw.append(live)
            picks_raw.sort(
                key=lambda r: (str(r.get("calendar_date") or ""), int(r.get("rank") or 99))
            )

        enriched, curve, summary = kelly_replay_metrics(
            picks_raw,
            ml,
            bankroll_start=float(bankroll_start),
        )

    day_rank = 0
    picks: list[dict[str, Any]] = []
    for row in enriched:
        day_rank += 1
        cal = str(row.get("calendar_date") or "")
        picks.append(
            _serialize_pick(
                {**row, "is_today": cal == today},
                day_rank=day_rank,
            )
        )

    daily = _build_daily_summaries(picks)

    dates = sorted({str(r.get("calendar_date") or "") for r in enriched if r.get("calendar_date")})
    start_date = dates[0] if dates else None
    end_date = dates[-1] if dates else None
    picks_display = list(reversed(picks))

    picks_today = [p for p in picks if p.get("is_today")]
    if not picks_today and today_picks:
        picks_today = [
            _serialize_pick({**row, "is_today": True}, day_rank=i)
            for i, row in enumerate(today_picks, start=1)
        ]

    return {
        "selection": {
            "mode": "top_picks_hybrid_live_replay",
            "label": "Top picks du jour",
            "description": hybrid_selection_description(rank1=False),
            "ev_min_pct": ev_min_pct,
            "ev_max_pct": ev_max_pct,
            "exclude_today": exclude_today,
            "bankroll_start_eur": bankroll_start,
            "max_picks_per_day": limit,
            "replay_mode": summary.get("replay_mode"),
            "kelly_base_frac": summary.get("kelly_base_frac"),
            "tracking_start_date": tracking_start,
        },
        "today_date": today,
        "picks_today": picks_today,
        "period": {
            "start_date": start_date,
            "end_date": end_date,
            "n_days": len(dates),
        },
        "summary": summary,
        "daily": daily,
        "picks": picks_display,
        "curve": curve,
        "generated_at": datetime.now(PARIS).isoformat(timespec="seconds"),
    }
