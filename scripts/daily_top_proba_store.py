"""Persistance journalière du top N probas favori (ATP / WTA séparés) pour replay réel."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scripts.bets_db import (
    DB_PATH_DEFAULT,
    _algo_kelly_stake_frac,
    ensure_daily_top_proba_schema,
    sync_daily_top_proba_from_results,
    upsert_daily_top_proba_picks,
)
from scripts.ml_model import TennisMLModel, resolve_match_brier_segment_key
from scripts.match_rank_quality import match_has_rank_points_source
from scripts.tournament_tier import is_major_tournament_match

# Alias rétrocompat (scripts / tests).
is_major_atp_wta_match = is_major_tournament_match

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARIS_TZ = ZoneInfo("Europe/Paris")
DEFAULT_TOP_LIMIT = 15
DEFAULT_TOURS = ("ATP", "WTA")
JSONL_DIR = os.path.join(ROOT, "data", "exports", "daily_top_proba")
DAEMON_STATE_PATH = os.path.join(ROOT, "data", "cache", ".daily_top_proba_daemon.json")
DEFAULT_SNAPSHOT_TTL_SEC = int(os.getenv("BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC", str(24 * 3600)))
DEFAULT_DAEMON_MIN_INTERVAL_SEC = int(
    os.getenv("BETTINGHUD_DAILY_TOP_PROBA_DAEMON_INTERVAL_SEC", "600")
)
DEFAULT_JSONL_MIN_INTERVAL_SEC = int(
    os.getenv("BETTINGHUD_DAILY_TOP_PROBA_JSONL_INTERVAL_SEC", "3600")
)


def filter_matches_for_daily_top_proba(matches: list) -> list[dict]:
    out: list[dict] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        try:
            if float(m.get("odd_p1") or 0.0) <= 1.0 or float(m.get("odd_p2") or 0.0) <= 1.0:
                continue
        except (TypeError, ValueError):
            continue
        if not match_has_rank_points_source(m):
            continue
        out.append(dict(m))
    return out


def _read_daemon_state() -> dict[str, Any]:
    try:
        with open(DAEMON_STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_daemon_state(state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(DAEMON_STATE_PATH), exist_ok=True)
    tmp = DAEMON_STATE_PATH + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, DAEMON_STATE_PATH)


def _sync_results_only(db_path: str) -> int:
    cn = sqlite3.connect(db_path)
    try:
        return int(sync_daily_top_proba_from_results(cn))
    finally:
        cn.close()


def load_today_matches_for_daily_top_proba(
    *,
    max_age_sec: float | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    """Charge le snapshot live courant et filtre les matchs du jour (headless)."""
    from scripts.live_snapshot import load_latest_live_snapshot, snapshot_meta

    ttl = float(max_age_sec if max_age_sec is not None else DEFAULT_SNAPSHOT_TTL_SEC)
    matches, load_meta = load_latest_live_snapshot(max_age_sec=ttl)
    meta = dict(load_meta or snapshot_meta() or {})
    if not matches:
        return [], meta
    filtered = filter_matches_for_daily_top_proba(
        [dict(m) for m in matches if isinstance(m, dict)]
    )
    today = [
        sanitize_stale_demain_time_label(m)
        for m in filtered
        if is_today_paris_match(m)
    ]
    return today, meta


def _match_calendar_date(m: dict) -> datetime.date | None:
    raw = str(m.get("date") or "")[:10].strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def is_today_paris_match(m: dict, *, today: datetime.date | None = None) -> bool:
    today = today or datetime.now(PARIS_TZ).date()
    d = _match_calendar_date(m)
    if d is not None:
        return d == today
    return not str(m.get("time") or "").startswith("Demain")


def sanitize_stale_demain_time_label(m: dict) -> dict:
    """Retire le préfixe scrape « Demain » si ``date`` est déjà le jour courant (Paris)."""
    if not isinstance(m, dict):
        return m
    out = dict(m)
    t = str(out.get("time") or "").strip()
    if t.startswith("Demain") and is_today_paris_match(out):
        out["time"] = t.replace("Demain", "", 1).strip() or t
    return out


def format_match_time_display(m: dict, *, ref_date: datetime.date | None = None) -> str | None:
    """Libellé horaire Telegram / UI : « Aujourd'hui 10:00 » ou « Demain 10:00 » selon ``date``."""
    ref = ref_date or datetime.now(PARIS_TZ).date()
    sm = sanitize_stale_demain_time_label(m)
    t = str(sm.get("time") or "").strip()
    d = _match_calendar_date(sm)
    if d is None:
        return t[:40] or None
    if d == ref:
        clock = t.replace("Demain", "", 1).strip() if t.startswith("Demain") else t
        if clock and not clock.lower().startswith("aujourd"):
            return f"Aujourd'hui {clock}"
        return clock or "Aujourd'hui"
    if d > ref:
        clock = t.replace("Demain", "", 1).strip() if t.startswith("Demain") else t
        if clock.lower().startswith("demain"):
            return clock
        return f"Demain {clock}".strip() if clock else "Demain"
    return t[:40] or None


def _match_tour(m: dict) -> str:
    return str(m.get("tour") or m.get("category") or "").strip().upper()


def _match_favorite_metrics(m: dict) -> dict[str, Any] | None:
    try:
        odd_p1 = float(m.get("odd_p1") or 0.0)
        odd_p2 = float(m.get("odd_p2") or 0.0)
        if odd_p1 <= 1.0 or odd_p2 <= 1.0:
            return None
    except (TypeError, ValueError):
        return None
    fs = m.get("feature_snapshot") or {}
    try:
        p1 = float(fs.get("capped_p1_prob") or 0.5)
    except (TypeError, ValueError):
        p1 = 0.5
    fav_side = 1 if p1 >= 0.5 else 2
    fav_p = max(p1, 1.0 - p1)
    p1_name = str(m.get("player1") or "").strip()
    p2_name = str(m.get("player2") or "").strip()
    fav_player = p1_name if fav_side == 1 else p2_name
    und_player = p2_name if fav_side == 1 else p1_name
    odd_fav = odd_p1 if fav_side == 1 else odd_p2
    odd_und = odd_p2 if fav_side == 1 else odd_p1
    true_fav = m.get("true_odd_p1") if fav_side == 1 else m.get("true_odd_p2")
    true_und = m.get("true_odd_p2") if fav_side == 1 else m.get("true_odd_p1")
    try:
        true_fav_f = float(true_fav) if true_fav else None
    except (TypeError, ValueError):
        true_fav_f = None
    try:
        true_und_f = float(true_und) if true_und else None
    except (TypeError, ValueError):
        true_und_f = None
    ev_fav = fav_p * float(odd_fav) - 1.0
    p_implicit_fav = 1.0 / float(odd_fav)
    gap = m.get("book_gap_pp")
    try:
        gap_f = float(gap) if gap is not None else None
    except (TypeError, ValueError):
        gap_f = None
    return {
        "fav_side": fav_side,
        "fav_player": fav_player,
        "underdog_player": und_player,
        "p1_prob": p1,
        "p_model_fav": fav_p,
        "odd_fav": float(odd_fav),
        "odd_underdog": float(odd_und),
        "true_odd_fav": true_fav_f,
        "true_odd_underdog": true_und_f,
        "ev_fav": ev_fav,
        "ev_fav_pct": ev_fav * 100.0,
        "p_implicit_fav": p_implicit_fav,
        "book_gap_pp": gap_f,
    }


def collect_daily_top_proba_rows(
    matches: list[dict],
    *,
    calendar_date: str | None = None,
    top_limit: int = DEFAULT_TOP_LIMIT,
    tours: tuple[str, ...] = DEFAULT_TOURS,
    today_only: bool = True,
    ml: TennisMLModel | None = None,
) -> list[dict]:
    """Top ``top_limit`` matchs/jour/circuit triés par proba favori modèle."""
    cal_day = calendar_date or datetime.now(PARIS_TZ).date().isoformat()
    cal_date_obj = datetime.strptime(cal_day, "%Y-%m-%d").date()
    if ml is None:
        ml = TennisMLModel()
        if hasattr(ml, "_load_bundle_if_needed"):
            ml._load_bundle_if_needed()

    pool: list[dict] = []
    for m in matches:
        if today_only and not is_today_paris_match(m, today=cal_date_obj):
            continue
        if not is_major_atp_wta_match(m):
            continue
        tour = _match_tour(m)
        if tour not in {t.upper() for t in tours}:
            continue
        met = _match_favorite_metrics(m)
        if met is None:
            continue
        p1_name, p2_name = str(m.get("player1") or "").strip(), str(m.get("player2") or "").strip()
        match_name = f"{p1_name} vs {p2_name}"
        seg_key = resolve_match_brier_segment_key(
            ml,
            tour=tour,
            surface=m.get("surface"),
            tournament=m.get("tournament"),
            tourney_level=m.get("tourney_level") or m.get("category"),
        )
        seg_brier = float(getattr(ml, "segment_brier_scores", {}).get(seg_key, getattr(ml, "global_test_brier", 0.1741)))
        pool.append(
            {
                **met,
                "calendar_date": cal_day,
                "match_date": (_match_calendar_date(m) or cal_date_obj).isoformat(),
                "tour": tour,
                "match_id": str(m.get("prematch_id") or m.get("match_id") or "").strip() or None,
                "match_name": match_name,
                "player1": p1_name,
                "player2": p2_name,
                "tournament": str(m.get("tournament") or "")[:80] or None,
                "surface": str(m.get("surface") or "")[:40] or None,
                "match_time": format_match_time_display(m, ref_date=cal_date_obj),
                "tourney_level": str(m.get("tourney_level") or m.get("category") or "")[:20] or None,
                "confidence": m.get("confidence"),
                "segment_key": seg_key,
                "segment_brier": seg_brier,
                "snapshot_tier": str(m.get("_snapshot_tier") or m.get("snapshot_tier") or "") or None,
                "theoretical_stake_frac": _algo_kelly_stake_frac(
                    met["p_model_fav"], met["odd_fav"], seg_brier
                ),
            }
        )

    rows: list[dict] = []
    for tour in tours:
        tour_u = tour.upper()
        ranked = sorted(
            [r for r in pool if r["tour"] == tour_u],
            key=lambda r: (-float(r["p_model_fav"]), str(r["match_name"]).lower()),
        )
        for rank, row in enumerate(ranked[: max(0, int(top_limit))], start=1):
            pick = dict(row)
            pick["rank"] = rank
            pick["top_limit"] = int(top_limit)
            pick["pick_key"] = f"{cal_day}|{tour_u}|{rank:02d}"
            rows.append(pick)
    return rows


def collect_top5_proba_picks(
    matches: list[dict],
    *,
    limit: int | None = 5,
    ev_min_frac: float = 0.15,
    ev_max_frac: float = 1.0,
    today_only: bool = True,
    calendar_date: str | None = None,
    ml: TennisMLModel | None = None,
) -> list[dict]:
    """Top N favoris modèle du jour (EV favori dans la bande), tri proba ↓ — aligné onglet Paris du jour."""
    cal_day = calendar_date or datetime.now(PARIS_TZ).date().isoformat()
    cal_date_obj = datetime.strptime(cal_day, "%Y-%m-%d").date()
    if ml is None:
        ml = TennisMLModel()
        if hasattr(ml, "_load_bundle_if_needed"):
            ml._load_bundle_if_needed()

    pool: list[dict] = []
    for m in matches:
        if today_only and not is_today_paris_match(m, today=cal_date_obj):
            continue
        if not is_major_atp_wta_match(m):
            continue
        met = _match_favorite_metrics(m)
        if met is None:
            continue
        ev_f = float(met["ev_fav"])
        if ev_f < float(ev_min_frac) or ev_f > float(ev_max_frac):
            continue
        tour = _match_tour(m)
        p1_name, p2_name = str(m.get("player1") or "").strip(), str(m.get("player2") or "").strip()
        seg_key = resolve_match_brier_segment_key(
            ml,
            tour=tour,
            surface=m.get("surface"),
            tournament=m.get("tournament"),
            tourney_level=m.get("tourney_level") or m.get("category"),
        )
        seg_brier = float(
            getattr(ml, "segment_brier_scores", {}).get(
                seg_key, getattr(ml, "global_test_brier", 0.1741)
            )
        )
        pool.append(
            {
                **met,
                "bet_on": met["fav_player"],
                "opponent": met["underdog_player"],
                "p_model_pct": float(met["p_model_fav"]) * 100.0,
                "rank": 0,
                "calendar_date": cal_day,
                "tour": tour,
                "match_name": f"{p1_name} vs {p2_name}",
                "player1": p1_name,
                "player2": p2_name,
                "tournament": str(m.get("tournament") or "")[:80] or None,
                "surface": str(m.get("surface") or "")[:40] or None,
                "match_time": format_match_time_display(m, ref_date=cal_date_obj),
                "segment_key": seg_key,
                "segment_brier": seg_brier,
                "theoretical_stake_frac": _algo_kelly_stake_frac(
                    met["p_model_fav"], met["odd_fav"], seg_brier
                ),
            }
        )

    ranked = sorted(pool, key=lambda r: (-float(r["p_model_fav"]), str(r["match_name"]).lower()))
    out: list[dict] = []
    cap = len(ranked) if limit is None else max(0, int(limit))
    for rank, row in enumerate(ranked[:cap], start=1):
        pick = dict(row)
        pick["rank"] = rank
        out.append(pick)
    return out


def collect_daily_ev_band_picks(
    matches: list[dict],
    *,
    limit: int | None = None,
    ev_min_frac: float = 0.15,
    ev_max_frac: float = 1.0,
    today_only: bool = True,
    calendar_date: str | None = None,
    ml: TennisMLModel | None = None,
) -> list[dict]:
    """Tous les picks du jour dans la bande EV favori (tri proba ↓)."""
    return collect_top5_proba_picks(
        matches,
        limit=limit,
        ev_min_frac=ev_min_frac,
        ev_max_frac=ev_max_frac,
        today_only=today_only,
        calendar_date=calendar_date,
        ml=ml,
    )


def _append_jsonl_capture(
    rows: list[dict],
    *,
    snapshot_built_at: float | None,
    capture_source: str,
) -> str | None:
    if not rows:
        return None
    cal_day = str(rows[0]["calendar_date"])
    os.makedirs(JSONL_DIR, exist_ok=True)
    path = os.path.join(JSONL_DIR, f"{cal_day}.jsonl")
    payload = {
        "captured_ts": datetime.now(PARIS_TZ).isoformat(timespec="seconds"),
        "calendar_date": cal_day,
        "snapshot_built_at": snapshot_built_at,
        "capture_source": capture_source,
        "n_picks": len(rows),
        "picks": rows,
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path


def persist_daily_top_proba_picks(
    matches: list[dict],
    *,
    snapshot_built_at: float | None = None,
    capture_source: str = "live_snapshot",
    top_limit: int = DEFAULT_TOP_LIMIT,
    calendar_date: str | None = None,
    append_jsonl: bool = True,
    db_path: str = DB_PATH_DEFAULT,
) -> int:
    """Upsert top N ATP/WTA + archive JSONL append-only (historique replay)."""
    rows = collect_daily_top_proba_rows(
        matches,
        calendar_date=calendar_date,
        top_limit=top_limit,
    )
    if not rows:
        return 0
    for r in rows:
        r["snapshot_built_at"] = snapshot_built_at
        r["capture_source"] = capture_source
    n = upsert_daily_top_proba_picks(rows, db_path=db_path)
    if append_jsonl:
        _append_jsonl_capture(rows, snapshot_built_at=snapshot_built_at, capture_source=capture_source)

    cn = sqlite3.connect(db_path)
    try:
        sync_daily_top_proba_from_results(cn)
    finally:
        cn.close()
    return int(n)


def sync_daily_top_proba_from_snapshot(
    matches: list[dict],
    *,
    snapshot_meta: dict | None = None,
    capture_source: str = "live_snapshot",
    top_limit: int = DEFAULT_TOP_LIMIT,
    db_path: str = DB_PATH_DEFAULT,
    append_jsonl: bool = True,
) -> int:
    meta = snapshot_meta or {}
    built_at = float(meta.get("built_at") or 0.0) or None
    return persist_daily_top_proba_picks(
        matches,
        snapshot_built_at=built_at,
        capture_source=capture_source,
        top_limit=top_limit,
        db_path=db_path,
        append_jsonl=append_jsonl,
    )


def run_daily_top_proba_daemon_pass(
    *,
    db_path: str = DB_PATH_DEFAULT,
    capture_source: str = "portfolio_results_daemon",
    force: bool = False,
    min_interval_sec: int | None = None,
    jsonl_interval_sec: int | None = None,
) -> dict[str, Any]:
    """Passe daemon headless : capture top probas + sync résultats (anti-spam JSONL)."""
    min_interval = max(60, int(min_interval_sec or DEFAULT_DAEMON_MIN_INTERVAL_SEC))
    jsonl_interval = max(min_interval, int(jsonl_interval_sec or DEFAULT_JSONL_MIN_INTERVAL_SEC))
    state = _read_daemon_state()
    now = time.time()
    last_pass = float(state.get("last_pass_ts") or 0.0)
    last_built = float(state.get("last_snapshot_built_at") or 0.0)
    last_jsonl = float(state.get("last_jsonl_ts") or 0.0)

    today_matches, meta = load_today_matches_for_daily_top_proba()
    built_at = float(meta.get("built_at") or 0.0)
    n_results = _sync_results_only(db_path)

    if not today_matches:
        _write_daemon_state(
            {
                **state,
                "last_pass_ts": now,
                "last_results_sync": n_results,
                "last_skip_reason": "no_today_matches",
            }
        )
        return {
            "skipped_capture": True,
            "reason": "no_today_matches",
            "n_picks": 0,
            "n_results": n_results,
        }

    same_snapshot = built_at > 0 and built_at <= last_built + 1e-6
    if not force and same_snapshot and (now - last_pass) < min_interval:
        return {
            "skipped_capture": True,
            "reason": "recent_same_snapshot",
            "n_picks": 0,
            "n_results": n_results,
            "snapshot_built_at": built_at,
        }

    append_jsonl = force or (not same_snapshot) or ((now - last_jsonl) >= jsonl_interval)
    n_picks = persist_daily_top_proba_picks(
        today_matches,
        snapshot_built_at=built_at or None,
        capture_source=capture_source,
        append_jsonl=append_jsonl,
        db_path=db_path,
    )
    new_state = {
        **state,
        "last_pass_ts": now,
        "last_snapshot_built_at": built_at or last_built,
        "last_n_picks": n_picks,
        "last_results_sync": n_results,
        "last_capture_source": capture_source,
    }
    if append_jsonl:
        new_state["last_jsonl_ts"] = now
    _write_daemon_state(new_state)
    return {
        "skipped_capture": False,
        "n_picks": n_picks,
        "n_results": n_results,
        "append_jsonl": append_jsonl,
        "snapshot_built_at": built_at,
        "capture_source": capture_source,
    }


BACKFILL_CAPTURE_PREFIX = "backfill_"


def _is_backfill_capture_source(source: str | None) -> bool:
    return str(source or "").startswith(BACKFILL_CAPTURE_PREFIX)


def _load_existing_capture_sources(db_path: str) -> dict[str, str | None]:
    cn = sqlite3.connect(db_path)
    try:
        return {
            str(k): (str(v) if v is not None else None)
            for k, v in cn.execute(
                "SELECT pick_key, capture_source FROM daily_top_proba_picks"
            ).fetchall()
        }
    finally:
        cn.close()


def filter_rows_respecting_existing_sources(
    rows: list[dict],
    existing_sources: dict[str, str | None],
    *,
    force: bool = False,
) -> list[dict]:
    if force:
        return list(rows)
    kept: list[dict] = []
    for row in rows:
        key = str(row.get("pick_key") or "")
        if not key:
            continue
        existing = existing_sources.get(key)
        if existing and not _is_backfill_capture_source(existing):
            continue
        kept.append(row)
    return kept


def import_daily_top_proba_from_jsonl_dir(
    jsonl_dir: str = JSONL_DIR,
    *,
    db_path: str = DB_PATH_DEFAULT,
    force: bool = False,
) -> dict[str, Any]:
    """Importe les captures JSONL append-only (dernière ligne par jour prioritaire)."""
    if not os.path.isdir(jsonl_dir):
        return {"files": 0, "days": 0, "n_picks": 0}

    by_day: dict[str, tuple[str, list[dict]]] = {}
    n_files = 0
    for name in sorted(os.listdir(jsonl_dir)):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(jsonl_dir, name)
        n_files += 1
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cal_day = str(payload.get("calendar_date") or name.replace(".jsonl", ""))
                captured_ts = str(payload.get("captured_ts") or "")
                picks = payload.get("picks") or []
                if not isinstance(picks, list) or not picks:
                    continue
                rows = []
                for pick in picks:
                    if not isinstance(pick, dict):
                        continue
                    row = dict(pick)
                    src = str(payload.get("capture_source") or row.get("capture_source") or "backfill_jsonl")
                    row["capture_source"] = src
                    row["first_captured_ts"] = captured_ts or row.get("first_captured_ts")
                    row["last_captured_ts"] = captured_ts or row.get("last_captured_ts")
                    row["snapshot_built_at"] = payload.get("snapshot_built_at", row.get("snapshot_built_at"))
                    rows.append(row)
                prev = by_day.get(cal_day)
                if prev is None or captured_ts >= prev[0]:
                    by_day[cal_day] = (captured_ts, rows)

    existing = _load_existing_capture_sources(db_path)
    all_rows: list[dict] = []
    for _cal, (_ts, rows) in sorted(by_day.items()):
        all_rows.extend(filter_rows_respecting_existing_sources(rows, existing, force=force))

    n = upsert_daily_top_proba_picks(all_rows, db_path=db_path) if all_rows else 0
    return {"files": n_files, "days": len(by_day), "n_picks": int(n)}


def _merge_algo_opportunity_match(
    opp_rows: list[dict],
    *,
    calendar_date: str,
) -> dict | None:
    if not opp_rows:
        return None
    ordered = sorted(opp_rows, key=lambda r: str(r.get("detected_ts") or ""), reverse=True)
    latest = ordered[0]
    by_side: dict[int, dict] = {}
    earliest_ts = str(ordered[-1].get("detected_ts") or "")
    for row in ordered:
        try:
            side = int(row.get("side") or 0)
        except (TypeError, ValueError):
            continue
        if side in (1, 2) and side not in by_side:
            by_side[side] = row

    p1_prob = None
    p2_prob = None
    odd_p1 = None
    odd_p2 = None
    true_odd_p1 = None
    true_odd_p2 = None
    if 1 in by_side:
        r = by_side[1]
        p1_prob = _float_or_none(r.get("p_model"))
        odd_p1 = _float_or_none(r.get("odd_book"))
        true_odd_p1 = _float_or_none(r.get("true_odd"))
    if 2 in by_side:
        r = by_side[2]
        p2_prob = _float_or_none(r.get("p_model"))
        odd_p2 = _float_or_none(r.get("odd_book"))
        true_odd_p2 = _float_or_none(r.get("true_odd"))
    if p1_prob is None and p2_prob is not None:
        p1_prob = 1.0 - p2_prob
    elif p2_prob is None and p1_prob is not None:
        p2_prob = 1.0 - p1_prob
    if p1_prob is None or p2_prob is None:
        return None

    fav_side = 1 if p1_prob >= 0.5 else 2
    fav_p = max(p1_prob, p2_prob)
    p1_name = str(latest.get("player1") or "").strip()
    p2_name = str(latest.get("player2") or "").strip()
    fav_player = p1_name if fav_side == 1 else p2_name
    und_player = p2_name if fav_side == 1 else p1_name
    odd_fav = odd_p1 if fav_side == 1 else odd_p2
    odd_und = odd_p2 if fav_side == 1 else odd_p1
    true_fav = true_odd_p1 if fav_side == 1 else true_odd_p2
    true_und = true_odd_p2 if fav_side == 1 else true_odd_p1
    if odd_fav is None or odd_fav <= 1.0:
        return None
    if odd_und is None or odd_und <= 1.0:
        odd_und = None

    seg_brier = _float_or_none(latest.get("segment_brier")) or 0.1741
    ev_fav = fav_p * float(odd_fav) - 1.0
    return {
        "calendar_date": calendar_date,
        "match_date": str(latest.get("match_date") or calendar_date)[:10],
        "tour": str(latest.get("tour") or "").upper(),
        "match_id": str(latest.get("match_id") or "").strip() or None,
        "match_name": str(latest.get("match_name") or "").strip(),
        "player1": p1_name or None,
        "player2": p2_name or None,
        "fav_side": fav_side,
        "fav_player": fav_player,
        "underdog_player": und_player,
        "p1_prob": p1_prob,
        "p_model_fav": fav_p,
        "odd_fav": float(odd_fav),
        "odd_underdog": float(odd_und) if odd_und else None,
        "true_odd_fav": true_fav,
        "true_odd_underdog": true_und,
        "ev_fav": ev_fav,
        "ev_fav_pct": ev_fav * 100.0,
        "p_implicit_fav": 1.0 / float(odd_fav),
        "book_gap_pp": (fav_p - (1.0 / float(odd_fav))) * 100.0,
        "tournament": latest.get("tournament"),
        "surface": latest.get("surface"),
        "match_time": None,
        "tourney_level": None,
        "confidence": _float_or_none(latest.get("confidence")),
        "segment_key": latest.get("segment_key"),
        "segment_brier": seg_brier,
        "theoretical_stake_frac": _algo_kelly_stake_frac(fav_p, odd_fav, seg_brier),
        "snapshot_built_at": None,
        "snapshot_tier": latest.get("snapshot_tier"),
        "capture_source": "backfill_algo_opportunities",
        "first_captured_ts": earliest_ts or None,
        "last_captured_ts": str(latest.get("detected_ts") or "") or None,
    }


def _float_or_none(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def reconstruct_daily_top_proba_from_algo_opportunities(
    *,
    db_path: str = DB_PATH_DEFAULT,
    start_date: str | None = None,
    end_date: str | None = None,
    top_limit: int = DEFAULT_TOP_LIMIT,
    skip_dates: set[str] | None = None,
    force: bool = False,
) -> list[dict]:
    """Reconstruit un top N/jour/circuit depuis `algo_opportunities` (proxy historique)."""
    cn = sqlite3.connect(db_path)
    try:
        ensure_daily_top_proba_schema(cn)
        q = """
            SELECT detected_date, detected_ts, match_date, match_id, match_name,
                   player1, player2, side, tour, surface, tournament,
                   odd_book, true_odd, p_model, confidence, segment_key,
                   segment_brier, snapshot_tier, status
            FROM algo_opportunities
            WHERE detected_date IS NOT NULL AND match_name IS NOT NULL
        """
        params: list[str] = []
        if start_date:
            q += " AND detected_date >= ?"
            params.append(start_date)
        if end_date:
            q += " AND detected_date <= ?"
            params.append(end_date)
        q += " ORDER BY detected_date, detected_ts"
        raw = cn.execute(q, params).fetchall()
        cols = [
            "detected_date",
            "detected_ts",
            "match_date",
            "match_id",
            "match_name",
            "player1",
            "player2",
            "side",
            "tour",
            "surface",
            "tournament",
            "odd_book",
            "true_odd",
            "p_model",
            "confidence",
            "segment_key",
            "segment_brier",
            "snapshot_tier",
            "status",
        ]
    finally:
        cn.close()

    from collections import defaultdict

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in raw:
        rec = dict(zip(cols, row))
        cal = str(rec.get("detected_date") or "")[:10]
        tour = str(rec.get("tour") or "").upper()
        name = str(rec.get("match_name") or "").strip()
        if not cal or not tour or not name:
            continue
        if skip_dates and cal in skip_dates:
            continue
        groups[(cal, name, tour)].append(rec)

    pool: list[dict] = []
    for (cal_day, _name, _tour), opp_rows in groups.items():
        merged = _merge_algo_opportunity_match(opp_rows, calendar_date=cal_day)
        if merged and merged.get("tour") in DEFAULT_TOURS:
            pool.append(merged)

    rows: list[dict] = []
    by_day: dict[str, list[dict]] = defaultdict(list)
    for item in pool:
        by_day[str(item["calendar_date"])].append(item)

    for cal_day, items in sorted(by_day.items()):
        for tour in DEFAULT_TOURS:
            ranked = sorted(
                [r for r in items if r["tour"] == tour],
                key=lambda r: (-float(r["p_model_fav"]), str(r["match_name"]).lower()),
            )
            for rank, row in enumerate(ranked[: max(0, int(top_limit))], start=1):
                pick = dict(row)
                pick["rank"] = rank
                pick["top_limit"] = int(top_limit)
                pick["pick_key"] = f"{cal_day}|{tour}|{rank:02d}"
                rows.append(pick)

    existing = _load_existing_capture_sources(db_path)
    return filter_rows_respecting_existing_sources(rows, existing, force=force)


def run_backfill_daily_top_proba(
    *,
    db_path: str = DB_PATH_DEFAULT,
    start_date: str | None = None,
    end_date: str | None = None,
    jsonl_dir: str = JSONL_DIR,
    force: bool = False,
    skip_algo_if_date_has_real: bool = True,
) -> dict[str, Any]:
    """Backfill JSONL + proxy algo_opportunities, puis sync résultats."""
    jsonl_stats = import_daily_top_proba_from_jsonl_dir(jsonl_dir, db_path=db_path, force=force)

    skip_dates: set[str] | None = None
    if skip_algo_if_date_has_real and not force:
        cn = sqlite3.connect(db_path)
        try:
            skip_dates = {
                str(d)
                for d, src in cn.execute(
                    """
                    SELECT calendar_date, capture_source
                    FROM daily_top_proba_picks
                    GROUP BY calendar_date
                    HAVING SUM(CASE WHEN capture_source LIKE 'backfill_%' THEN 0 ELSE 1 END) > 0
                    """
                ).fetchall()
            }
        finally:
            cn.close()

    algo_rows = reconstruct_daily_top_proba_from_algo_opportunities(
        db_path=db_path,
        start_date=start_date,
        end_date=end_date,
        skip_dates=skip_dates,
        force=force,
    )
    n_algo = upsert_daily_top_proba_picks(algo_rows, db_path=db_path) if algo_rows else 0
    n_results = _sync_results_only(db_path)

    cn = sqlite3.connect(db_path)
    try:
        coverage = cn.execute(
            """
            SELECT calendar_date, tour, count(*), min(capture_source), max(capture_source)
            FROM daily_top_proba_picks
            GROUP BY calendar_date, tour
            ORDER BY calendar_date, tour
            """
        ).fetchall()
    finally:
        cn.close()

    return {
        "jsonl": jsonl_stats,
        "algo_rows_built": len(algo_rows),
        "algo_rows_upserted": int(n_algo),
        "n_results_synced": n_results,
        "coverage": coverage,
    }
