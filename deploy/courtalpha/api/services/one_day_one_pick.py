"""Replay 1 Day 1 Pick : rang 1 de la sélection Top picks du jour (même règles que /top5)."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from api.services.hybrid_selection_text import hybrid_selection_description

PARIS = ZoneInfo("Europe/Paris")
EV_MIN_PCT = 15.0
EV_MAX_PCT = 100.0
DEFAULT_BANKROLL_EUR = 100.0


def _format_score(raw: object) -> str | None:
    from scripts.score_display import format_tennis_score_display

    return format_tennis_score_display(raw)


def _status_flags(status: object) -> dict[str, bool]:
    st = str(status or "").strip().lower()
    void = "annul" in st
    return {
        "won": "gagn" in st,
        "lost": "perdu" in st,
        "open": "cours" in st or st in {"", "en cours"},
        "void": void,
        "settled": "gagn" in st or "perdu" in st or void,
    }


def _profit_frac_for_pick(row: dict[str, Any]) -> float:
    flags = _status_flags(row.get("status"))
    if flags["void"]:
        return 0.0
    if not flags["settled"]:
        return 0.0
    try:
        stored = float(row.get("theoretical_profit"))
        if stored != 0.0 or flags["won"] or flags["lost"]:
            return stored
    except (TypeError, ValueError):
        pass
    from scripts.bets_db import _algo_profit_for_status

    return float(
        _algo_profit_for_status(
            str(row.get("status") or ""),
            row.get("odd_fav"),
            row.get("theoretical_stake_frac"),
        )
    )


def _stake_frac(row: dict[str, Any]) -> float:
    try:
        stake = float(row.get("theoretical_stake_frac") or 0.0)
        if stake > 0.0:
            return stake
    except (TypeError, ValueError):
        pass
    from scripts.bets_db import _algo_kelly_stake_frac

    return float(
        _algo_kelly_stake_frac(
            row.get("p_model_fav"),
            row.get("odd_fav"),
            row.get("segment_brier"),
        )
    )


def _enrich_picks_with_replay_pnl(
    picks: list[dict[str, Any]],
    *,
    bankroll_start: float,
) -> list[dict[str, Any]]:
    """Ajoute replay_net_profit_eur (simulation BR séquentielle, même logique que la courbe)."""
    bankroll = float(bankroll_start)
    out: list[dict[str, Any]] = []
    for row in picks:
        enriched = dict(row)
        flags = _status_flags(enriched.get("status"))
        profit_eur: float | None = None
        if flags["settled"]:
            if flags["void"]:
                profit_eur = 0.0
            else:
                profit_frac = _profit_frac_for_pick(enriched)
                profit_eur = round(bankroll * profit_frac, 2)
                bankroll += profit_eur
        enriched["replay_net_profit_eur"] = profit_eur
        out.append(enriched)
    return out


def _serialize_pick(row: dict[str, Any], *, day_rank: int) -> dict[str, Any]:
    flags = _status_flags(row.get("status"))
    try:
        p_model = float(row.get("p_model_fav") or 0.0)
    except (TypeError, ValueError):
        p_model = 0.0
    try:
        ev_pct = float(row.get("ev_fav_pct")) if row.get("ev_fav_pct") is not None else None
    except (TypeError, ValueError):
        ev_pct = None
    try:
        odd = float(row.get("odd_fav")) if row.get("odd_fav") is not None else None
    except (TypeError, ValueError):
        odd = None
    stake_frac = _stake_frac(row)
    profit_frac = _profit_frac_for_pick(row) if flags["settled"] else None
    return {
        "calendar_date": str(row.get("calendar_date") or ""),
        "match_date": row.get("match_date"),
        "tour": str(row.get("tour") or "").upper(),
        "rank": int(row.get("rank") or 1),
        "day_rank": day_rank,
        "match_name": row.get("match_name"),
        "fav_player": row.get("fav_player"),
        "underdog_player": row.get("underdog_player"),
        "bet_on": row.get("bet_on") or row.get("fav_player"),
        "opponent": row.get("opponent") or row.get("underdog_player"),
        "tournament": row.get("tournament"),
        "surface": row.get("surface"),
        "p_model_fav": round(p_model, 4),
        "p_model_pct": round(p_model * 100.0, 1),
        "ev_fav_pct": round(ev_pct, 1) if ev_pct is not None else None,
        "odd_fav": round(odd, 3) if odd is not None else None,
        "theoretical_stake_frac": round(stake_frac, 5),
        "theoretical_stake_pct": round(stake_frac * 100.0, 2),
        "theoretical_profit_frac": round(profit_frac, 5) if profit_frac is not None else None,
        "replay_net_profit_eur": row.get("replay_net_profit_eur"),
        "status": row.get("status"),
        "score_final": row.get("score_final"),
        "score_display": _format_score(row.get("score_final")),
        "capture_source": row.get("capture_source"),
        "won": flags["won"],
        "lost": flags["lost"],
        "void": flags["void"],
        "settled": flags["settled"],
        "open": flags["open"],
        "is_today": bool(row.get("is_today")),
        "selection_mode": row.get("selection_mode"),
    }


def _enrich_rows_reliability(
    rows: list[dict[str, Any]],
    *,
    db_path: str,
) -> list[dict[str, Any]]:
    """Score fiabilité manquant (replay historique avant backfill DB)."""
    if not any(r.get("data_reliability_score") is None for r in rows):
        return rows
    from scripts.match_rank_quality import match_data_reliability_score
    from scripts.reliability_pick_match import match_dict_from_top_proba_row
    from scripts.stats_engine import TennisStatsEngine

    engine = TennisStatsEngine(db_path=db_path)
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        if d.get("data_reliability_score") is None:
            match = match_dict_from_top_proba_row(d, engine)
            score, flags = match_data_reliability_score(match)
            d["data_reliability_score"] = score
            d["data_reliability_flags"] = "|".join(flags) if flags else None
        out.append(d)
    return out


def _load_ranked_rows(
    db_path: str,
) -> list[dict[str, Any]]:
    from scripts.bets_db import ensure_daily_top_proba_schema, sync_daily_top_proba_from_results
    from scripts.tournament_tier import is_major_atp_wta_by_name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_daily_top_proba_schema(conn)
        sync_daily_top_proba_from_results(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM daily_top_proba_picks
            ORDER BY calendar_date ASC, tour ASC, rank ASC
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            tour = str(d.get("tour") or "").upper()
            tournament = str(d.get("tournament") or "")
            if not is_major_atp_wta_by_name(tour, tournament):
                continue
            out.append(d)
        return _enrich_rows_reliability(out, db_path=db_path)
    finally:
        conn.close()


def _resolve_today_pick(
    *,
    db_path: str,
    today: str,
    ev_min_pct: float,
    ev_max_pct: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """Retourne (pick brut, source db|live) — Option A via discord_1d1p_core."""
    from scripts.discord_1d1p_core import load_1d1p_today_pick

    pick, _, _, _ = load_1d1p_today_pick(
        db_path=db_path,
        calendar_date=today,
        ev_min_pct=ev_min_pct,
        ev_max_pct=ev_max_pct,
    )
    if pick is None:
        return None, None
    return dict(pick), str(pick.get("source") or "live")


def _merge_today_settlement(db_path: str, row: dict[str, Any]) -> dict[str, Any]:
    """Applique le settlement DB (match_results / daily_top_proba) au pick du jour."""
    try:
        from scripts.bets_db import attach_pick_settlement_from_results, ensure_daily_top_proba_schema, open_db

        conn = open_db(db_path)
        try:
            ensure_daily_top_proba_schema(conn)
            return attach_pick_settlement_from_results(dict(row), conn)
        finally:
            conn.close()
    except Exception:
        return dict(row)


def _resolve_tracking_bankroll(db_path: str, *, mode: str, default: float) -> tuple[float, str | None]:
    try:
        from scripts.bets_db import open_db
        from scripts.portfolio_tracking_store import get_tracking_config

        conn = open_db(db_path)
        try:
            cfg = get_tracking_config(conn, mode)
            if cfg:
                return float(cfg["bankroll_start_eur"]), str(cfg["start_date"])
        finally:
            conn.close()
    except Exception:
        pass
    return float(default), None


def _build_tracked_1d1p_picks(
    *,
    db_path: str,
    today: str,
    today_raw: dict[str, Any] | None,
    exclude_today: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Replay 1D1P depuis le ledger portfolio + pick live du jour si absent."""
    from scripts.bets_db import open_db
    from scripts.portfolio_tracking_store import (
        build_portfolio_curve_and_summary,
        load_portfolio_replay_picks,
        refresh_portfolio_tracking,
        sync_portfolio_from_published,
    )
    from scripts.published_picks_store import MODE_1D1P

    conn = open_db(db_path)
    try:
        refresh_portfolio_tracking(conn)
        sync_portfolio_from_published(conn, mode=MODE_1D1P, calendar_date=today)
        refresh_portfolio_tracking(conn)
        picks_raw = load_portfolio_replay_picks(conn, MODE_1D1P, exclude_date=None)
        if not exclude_today and today_raw is not None:
            has_today = any(str(p.get("calendar_date") or "") == today for p in picks_raw)
            if not has_today:
                today_row = _merge_today_settlement(db_path, dict(today_raw))
                today_row["is_today"] = True
                today_row.setdefault("rank", 1)
                picks_raw.append(today_row)
            else:
                for p in picks_raw:
                    if str(p.get("calendar_date") or "") == today:
                        merged = _merge_today_settlement(db_path, dict(p))
                        p.update(merged)
                        p["is_today"] = True
        curve, summary = build_portfolio_curve_and_summary(conn, MODE_1D1P)
        return picks_raw, curve, summary
    finally:
        conn.close()


def _select_one_pick_per_day(
    *,
    db_path: str,
    exclude_date: str | None,
    ev_min_pct: float,
    ev_max_pct: float,
) -> list[dict[str, Any]]:
    _ = ev_min_pct, ev_max_pct
    from scripts.live_replay_engine import load_ml, select_historical_1d1p_live

    ml = load_ml()
    return select_historical_1d1p_live(db_path, ml, exclude_date=exclude_date)


def _build_curve(
    picks: list[dict[str, Any]],
    *,
    bankroll_start: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bankroll = float(bankroll_start)
    peak = bankroll
    max_dd_pct = 0.0
    total_staked_eur = 0.0
    curve: list[dict[str, Any]] = []
    n_won = n_lost = n_void = n_open = n_settled = 0

    for i, row in enumerate(picks, start=1):
        flags = _status_flags(row.get("status"))
        stake_frac = _stake_frac(row)
        profit_eur = 0.0
        profit_frac = None

        if flags["settled"]:
            n_settled += 1
            profit_frac = _profit_frac_for_pick(row)
            if flags["void"]:
                n_void += 1
            else:
                stake_eur = bankroll * stake_frac
                profit_eur = bankroll * profit_frac
                total_staked_eur += stake_eur
                bankroll += profit_eur
                if flags["won"]:
                    n_won += 1
                elif flags["lost"]:
                    n_lost += 1
        elif flags["open"]:
            n_open += 1

        peak = max(peak, bankroll)
        dd_pct = ((peak - bankroll) / peak * 100.0) if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_pct)

        cal = str(row.get("calendar_date") or "")
        curve.append(
            {
                "date": cal,
                "bankroll": round(bankroll, 2),
                "daily_profit_eur": round(profit_eur, 2),
                "daily_stake_eur": round(bankroll * stake_frac, 2)
                if flags["settled"] and not flags["void"]
                else 0.0,
                "n_picks_cum": i,
                "pnl_cum_eur": round(bankroll - bankroll_start, 2),
                "drawdown_pct": round(dd_pct, 2),
                "settled": flags["settled"],
            }
        )

    net_profit = bankroll - bankroll_start
    growth_pct = (net_profit / bankroll_start * 100.0) if bankroll_start > 0 else 0.0
    roi_staked = (net_profit / total_staked_eur * 100.0) if total_staked_eur > 0 else 0.0
    n_decided = n_won + n_lost
    hit_pct = (n_won / n_decided * 100.0) if n_decided > 0 else 0.0

    summary = {
        "n_picks": len(picks),
        "n_settled": n_settled,
        "n_open": n_open,
        "n_won": n_won,
        "n_lost": n_lost,
        "n_void": n_void,
        "hit_pct": round(hit_pct, 1),
        "bankroll_start_eur": round(bankroll_start, 2),
        "bankroll_final_eur": round(bankroll, 2),
        "net_profit_eur": round(net_profit, 2),
        "growth_pct": round(growth_pct, 1),
        "total_staked_eur": round(total_staked_eur, 2),
        "roi_on_staked_pct": round(roi_staked, 1),
        "max_drawdown_pct": round(max_dd_pct, 1),
    }
    return curve, summary


def build_one_day_one_pick_replay(
    *,
    db_path: str,
    bankroll_start: float = DEFAULT_BANKROLL_EUR,
    ev_min_pct: float = EV_MIN_PCT,
    ev_max_pct: float = EV_MAX_PCT,
    exclude_today: bool = False,
) -> dict[str, Any]:
    """Replay 1D1P : pool matin JSONL + picks publiés + Kelly séquentiel fresh."""
    from scripts.live_replay_engine import kelly_replay_metrics, load_ml
    from scripts.published_picks_store import MODE_1D1P

    today = datetime.now(PARIS).date().isoformat()
    ml = load_ml()

    bankroll_start, tracking_start = _resolve_tracking_bankroll(
        db_path, mode=MODE_1D1P, default=float(bankroll_start)
    )

    today_raw, today_source = _resolve_today_pick(
        db_path=db_path,
        today=today,
        ev_min_pct=float(ev_min_pct),
        ev_max_pct=float(ev_max_pct),
    )

    use_ledger = tracking_start is not None
    if use_ledger:
        enriched, curve, summary = _build_tracked_1d1p_picks(
            db_path=db_path,
            today=today,
            today_raw=today_raw,
            exclude_today=exclude_today,
        )
    else:
        picks_raw = _select_one_pick_per_day(
            db_path=db_path,
            exclude_date=today,
            ev_min_pct=float(ev_min_pct),
            ev_max_pct=float(ev_max_pct),
        )
        if not exclude_today and today_raw is not None:
            today_row = _merge_today_settlement(db_path, dict(today_raw))
            today_row["is_today"] = True
            if today_source:
                today_row["source"] = today_source
            picks_raw.append(today_row)
            picks_raw.sort(key=lambda r: str(r.get("calendar_date") or ""))

        enriched, curve, summary = kelly_replay_metrics(
            picks_raw,
            ml,
            bankroll_start=float(bankroll_start),
        )

    picks = [
        _serialize_pick({**row, "is_today": str(row.get("calendar_date") or "") == today}, day_rank=i)
        for i, row in enumerate(enriched, start=1)
    ]

    start_date = str(enriched[0].get("calendar_date") or "") if enriched else None
    end_date = str(enriched[-1].get("calendar_date") or "") if enriched else None
    picks_display = list(reversed(picks))

    pick_today: dict[str, Any] | None = None
    if today_raw is not None:
        pick_today = next((dict(p) for p in picks if p.get("is_today")), None)
        if pick_today is None:
            row = _merge_today_settlement(db_path, dict(today_raw))
            pick_today = _serialize_pick({**row, "is_today": True}, day_rank=len(picks) or 1)
        if pick_today is not None and today_source:
            pick_today["source"] = today_source

    return {
        "selection": {
            "mode": "one_day_one_pick_live_replay",
            "description": hybrid_selection_description(rank1=True),
            "ev_min_pct": ev_min_pct,
            "ev_max_pct": ev_max_pct,
            "exclude_today": exclude_today,
            "bankroll_start_eur": bankroll_start,
            "replay_mode": summary.get("replay_mode"),
            "kelly_base_frac": summary.get("kelly_base_frac"),
            "tracking_start_date": tracking_start,
        },
        "today_date": today,
        "pick_today": pick_today,
        "period": {
            "start_date": start_date,
            "end_date": end_date,
            "n_days": len(picks),
        },
        "summary": summary,
        "picks": picks_display,
        "curve": curve,
        "generated_at": datetime.now(PARIS).isoformat(timespec="seconds"),
    }