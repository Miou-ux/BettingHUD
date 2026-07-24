"""Persistance des picks effectivement publiés (Top5 / 1D1P) pour replay CourtAlpha."""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

MODE_TOP5 = "top5"
MODE_1D1P = "1d1p"
NO_PICK_KEY = "__none__"


def ensure_published_picks_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_published_picks (
            calendar_date TEXT NOT NULL,
            mode TEXT NOT NULL,
            publish_rank INTEGER NOT NULL,
            pick_key TEXT,
            fav_player TEXT,
            match_name TEXT,
            player1 TEXT,
            player2 TEXT,
            tour TEXT,
            tournament TEXT,
            match_date TEXT,
            surface TEXT,
            p_model_fav REAL,
            ev_fav_pct REAL,
            odd_fav REAL,
            data_reliability_score INTEGER,
            hybrid_rel_fallback INTEGER DEFAULT 0,
            theoretical_stake_frac REAL,
            published_ts TEXT NOT NULL,
            publish_source TEXT,
            payload_json TEXT,
            PRIMARY KEY (calendar_date, mode, publish_rank)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_published_cal_mode "
        "ON daily_published_picks(calendar_date, mode)"
    )
    conn.commit()


def _resolve_pick_key(
    conn: sqlite3.Connection,
    pick: dict[str, Any],
    calendar_date: str,
) -> str | None:
    pk = str(pick.get("pick_key") or "").strip()
    if pk:
        return pk
    fav = str(pick.get("fav_player") or "").strip()
    tour = str(pick.get("tour") or "").strip().upper()
    if not fav:
        return None
    row = conn.execute(
        """
        SELECT pick_key FROM daily_top_proba_picks
        WHERE calendar_date = ? AND fav_player = ?
          AND (? = '' OR tour = ?)
        ORDER BY last_captured_ts DESC
        LIMIT 1
        """,
        (calendar_date, fav, tour, tour),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _pick_snapshot_fields(pick: dict[str, Any]) -> dict[str, Any]:
    def _f(key: str):
        return pick.get(key)

    rel_fb = pick.get("hybrid_rel_fallback")
    try:
        rel_fb_i = int(bool(rel_fb))
    except (TypeError, ValueError):
        rel_fb_i = 0
    return {
        "pick_key": _f("pick_key"),
        "fav_player": _f("fav_player"),
        "match_name": _f("match_name"),
        "player1": _f("player1"),
        "player2": _f("player2"),
        "tour": _f("tour"),
        "tournament": _f("tournament"),
        "match_date": _f("match_date") or _f("calendar_date"),
        "surface": _f("surface"),
        "p_model_fav": _f("p_model_fav"),
        "ev_fav_pct": _f("ev_fav_pct"),
        "odd_fav": _f("odd_fav"),
        "data_reliability_score": _f("data_reliability_score"),
        "hybrid_rel_fallback": rel_fb_i,
        "theoretical_stake_frac": _f("theoretical_stake_frac"),
    }


def save_published_picks(
    conn: sqlite3.Connection,
    *,
    mode: str,
    calendar_date: str,
    picks: list[dict[str, Any]],
    source: str = "morning",
) -> int:
    """Remplace la publication du jour pour ``mode`` (top5 / 1d1p)."""
    from scripts.bets_db import ensure_daily_top_proba_schema

    ensure_published_picks_schema(conn)
    ensure_daily_top_proba_schema(conn)
    cal = str(calendar_date)[:10]
    conn.execute(
        "DELETE FROM daily_published_picks WHERE calendar_date = ? AND mode = ?",
        (cal, mode),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    n = 0
    for rank, pick in enumerate(picks, start=1):
        if not isinstance(pick, dict):
            continue
        snap = _pick_snapshot_fields(pick)
        pk = _resolve_pick_key(conn, pick, cal) or snap.get("pick_key")
        snap["pick_key"] = pk
        conn.execute(
            """
            INSERT INTO daily_published_picks (
                calendar_date, mode, publish_rank, pick_key, fav_player, match_name,
                player1, player2, tour, tournament, match_date, surface,
                p_model_fav, ev_fav_pct, odd_fav, data_reliability_score,
                hybrid_rel_fallback, theoretical_stake_frac,
                published_ts, publish_source, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cal,
                mode,
                rank,
                pk,
                snap.get("fav_player"),
                snap.get("match_name"),
                snap.get("player1"),
                snap.get("player2"),
                snap.get("tour"),
                snap.get("tournament"),
                snap.get("match_date"),
                snap.get("surface"),
                snap.get("p_model_fav"),
                snap.get("ev_fav_pct"),
                snap.get("odd_fav"),
                snap.get("data_reliability_score"),
                snap.get("hybrid_rel_fallback"),
                snap.get("theoretical_stake_frac"),
                now,
                source,
                json.dumps(pick, ensure_ascii=False, default=str),
            ),
        )
        n += 1
    conn.commit()
    try:
        from scripts.portfolio_tracking_store import on_published_picks_saved

        on_published_picks_saved(conn, mode=mode, calendar_date=cal)
    except Exception:
        pass
    return n


def mark_published_no_picks(
    conn: sqlite3.Connection,
    *,
    mode: str,
    calendar_date: str,
    source: str = "backfill",
) -> None:
    """Archive un jour sans publication (évite re-sélection fallback au replay)."""
    ensure_published_picks_schema(conn)
    cal = str(calendar_date)[:10]
    conn.execute(
        "DELETE FROM daily_published_picks WHERE calendar_date = ? AND mode = ?",
        (cal, mode),
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO daily_published_picks (
            calendar_date, mode, publish_rank, pick_key, fav_player,
            published_ts, publish_source, payload_json
        ) VALUES (?, ?, 0, ?, NULL, ?, ?, ?)
        """,
        (cal, mode, NO_PICK_KEY, now, source, '{"no_pick": true}'),
    )
    conn.commit()


def has_published_for_date(
    conn: sqlite3.Connection,
    calendar_date: str,
    mode: str,
) -> bool:
    ensure_published_picks_schema(conn)
    row = conn.execute(
        """
        SELECT 1 FROM daily_published_picks
        WHERE calendar_date = ? AND mode = ?
        LIMIT 1
        """,
        (str(calendar_date)[:10], mode),
    ).fetchone()
    return row is not None


def load_published_replay_picks(
    db_path: str,
    *,
    mode: str,
    calendar_date: str,
) -> list[dict[str, Any]]:
    """Picks publiés enrichis avec statut/résultat depuis daily_top_proba_picks."""
    from scripts.bets_db import ensure_daily_top_proba_schema, sync_daily_top_proba_from_results

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_published_picks_schema(conn)
        ensure_daily_top_proba_schema(conn)
        sync_daily_top_proba_from_results(conn)
        cal = str(calendar_date)[:10]
        pub_rows = conn.execute(
            """
            SELECT * FROM daily_published_picks
            WHERE calendar_date = ? AND mode = ?
            ORDER BY publish_rank ASC
            """,
            (cal, mode),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for pub in pub_rows:
            d = dict(pub)
            if str(d.get("pick_key") or "") == NO_PICK_KEY or int(d.get("publish_rank") or 0) <= 0:
                continue
            pk = d.get("pick_key")
            merged: dict[str, Any] | None = None
            if pk:
                full = conn.execute(
                    "SELECT * FROM daily_top_proba_picks WHERE pick_key = ?",
                    (pk,),
                ).fetchone()
                if full:
                    merged = dict(full)
            if merged is None:
                merged = {
                    k: d.get(k)
                    for k in (
                        "calendar_date",
                        "match_date",
                        "tour",
                        "fav_player",
                        "match_name",
                        "player1",
                        "player2",
                        "tournament",
                        "surface",
                        "p_model_fav",
                        "ev_fav_pct",
                        "odd_fav",
                        "data_reliability_score",
                        "theoretical_stake_frac",
                    )
                }
                merged["calendar_date"] = cal
                merged.setdefault("status", "En cours")
            merged["publish_rank"] = int(d.get("publish_rank") or 0)
            merged["rank"] = merged["publish_rank"]
            merged["published"] = True
            merged["publish_source"] = d.get("publish_source")
            if d.get("hybrid_rel_fallback"):
                merged["hybrid_rel_fallback"] = bool(d.get("hybrid_rel_fallback"))
            out.append(merged)
        return out
    finally:
        conn.close()


def select_historical_top5_picks(
    db_path: str,
    rows: list[dict[str, Any]],
    *,
    exclude_date: str | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    from scripts.backtest_prod_top5_2026 import _norm_pick_row, select_prod_top5_day
    from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cal = str(row.get("calendar_date") or "")
        if not cal:
            continue
        if exclude_date and cal >= exclude_date:
            continue
        by_day[cal].append(_norm_pick_row(dict(row)))

    picks: list[dict[str, Any]] = []
    for cal in sorted(by_day.keys()):
        pub = load_published_replay_picks(db_path, mode=MODE_TOP5, calendar_date=cal)
        if pub:
            capped = pub if limit is None or int(limit) <= 0 else pub[: int(limit)]
            for pick in capped:
                r = dict(pick)
                r["rank"] = int(r.get("publish_rank") or r.get("rank") or 0)
                picks.append(r)
            continue
        day_rows = dedupe_top_proba_rows_by_match(by_day[cal])
        for rank, pick in enumerate(select_prod_top5_day(day_rows, limit=limit), start=1):
            r = dict(pick)
            r["rank"] = rank
            picks.append(r)
    return picks


def select_historical_1d1p_picks(
    db_path: str,
    rows: list[dict[str, Any]],
    *,
    exclude_date: str | None,
    ev_min_pct: float,
    ev_max_pct: float,
) -> list[dict[str, Any]]:
    from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match, matchup_players_key
    from scripts.discord_1d1p_core import select_1d1p_pick
    from scripts.tournament_tier import is_major_atp_wta_by_name

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cal = str(row.get("calendar_date") or "")
        if not cal:
            continue
        if exclude_date and cal >= exclude_date:
            continue
        by_day.setdefault(cal, []).append(row)

    picks: list[dict[str, Any]] = []
    used_matchups: set[str] = set()
    for cal in sorted(by_day.keys()):
        pub = load_published_replay_picks(db_path, mode=MODE_1D1P, calendar_date=cal)
        if pub:
            best = pub[0]
            picks.append(dict(best))
            mk = matchup_players_key(best)
            if mk:
                used_matchups.add(mk)
            continue

        day_rows = dedupe_top_proba_rows_by_match(by_day[cal])

        def _row_ok(row: dict[str, Any]) -> bool:
            if not is_major_atp_wta_by_name(str(row.get("tour") or ""), str(row.get("tournament") or "")):
                return False
            key = matchup_players_key(row)
            return not key or key not in used_matchups

        best = select_1d1p_pick(day_rows, ev_min_pct=ev_min_pct, ev_max_pct=ev_max_pct, row_ok=_row_ok)
        if best is not None:
            picks.append(dict(best))
            mk = matchup_players_key(best)
            if mk:
                used_matchups.add(mk)
    return picks
