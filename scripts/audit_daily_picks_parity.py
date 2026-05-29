#!/usr/bin/env python3
"""Audit parité des picks du jour : Paris du jour vs Telegram vs daemon DB.

Usage:
  py -3 scripts/audit_daily_picks_parity.py
  py -3 scripts/audit_daily_picks_parity.py --date 2026-05-28
  py -3 scripts/audit_daily_picks_parity.py --export data/reports/audit_picks_parity.csv
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")
os.environ.setdefault("BETTINGHUD_LIVE_DATA_DAEMON", "0")

PARIS_TZ = ZoneInfo("Europe/Paris")
EV_MIN = 0.15
EV_MAX = 1.0
TOP_N = 5


def _install_streamlit_mock() -> None:
    def _passthrough_cache(*_cache_args, **_cache_kwargs):
        def _decorator(fn):
            cache: dict = {}

            def wrapper(*args, **kwargs):
                key = (args, tuple(sorted(kwargs.items())))
                if key not in cache:
                    cache[key] = fn(*args, **kwargs)
                return cache[key]

            wrapper.clear = cache.clear  # type: ignore[attr-defined]
            return wrapper

        if len(_cache_args) == 1 and callable(_cache_args[0]) and not _cache_kwargs:
            return _decorator(_cache_args[0])
        return _decorator

    class _MockSessionState(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key) from None

        def __setattr__(self, key, value):
            self[key] = value

    st = MagicMock()
    st.session_state = _MockSessionState()
    st.cache_resource = _passthrough_cache
    st.cache_data = _passthrough_cache
    sys.modules["streamlit"] = st
    sar = MagicMock()
    sar.st_autorefresh = lambda *a, **k: None
    sys.modules["streamlit_autorefresh"] = sar


def _pick_key(match_name: str, fav_player: str) -> tuple[str, str]:
    return (
        str(match_name or "").strip().lower(),
        str(fav_player or "").split("(")[0].strip().lower(),
    )


def _rows_from_cards(cards: list[dict]) -> list[dict]:
    out: list[dict] = []
    for card in cards:
        m = card.get("match") or {}
        met = card.get("metrics") or {}
        p1 = str(m.get("player1") or "")
        p2 = str(m.get("player2") or "")
        fav = str(met.get("fav") or "")
        out.append(
            {
                "source": "paris_du_jour",
                "rank": len(out) + 1,
                "match_name": f"{p1} vs {p2}",
                "fav_player": fav,
                "p_model_pct": round(float(met.get("fav_p") or 0) * 100, 1),
                "ev_pct": round(float(met.get("ev_fav_pct") or 0), 1),
                "odd": float(met.get("odd_fav") or 0),
            }
        )
    return out


def _rows_from_store_picks(picks: list[dict], *, source: str) -> list[dict]:
    out: list[dict] = []
    for i, p in enumerate(picks, start=1):
        out.append(
            {
                "source": source,
                "rank": i,
                "match_name": str(p.get("match_name") or ""),
                "fav_player": str(p.get("fav_player") or ""),
                "p_model_pct": round(float(p.get("p_model_fav") or 0) * 100, 1),
                "ev_pct": round(float(p.get("ev_fav_pct") or 0), 1),
                "odd": float(p.get("odd_fav") or 0),
            }
        )
    return out


def _rows_from_db(db_path: str, cal_day: str, *, top_n: int = TOP_N) -> list[dict]:
    if not os.path.isfile(db_path):
        return []
    cn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in cn.execute("PRAGMA table_info(daily_top_proba_picks)")}
        if not cols:
            return []
        q = """
            SELECT calendar_date, tour, rank, match_name, fav_player,
                   p_model_fav, ev_fav_pct, odd_fav
            FROM daily_top_proba_picks
            WHERE calendar_date = ?
            ORDER BY tour, rank
        """
        rows = cn.execute(q, (cal_day,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        cn.close()
    out: list[dict] = []
    for cal, tour, rank, mn, fav, p, ev, odd in rows:
        if int(rank) > int(top_n):
            continue
        out.append(
            {
                "source": f"db_{str(tour).upper()}_top{top_n}",
                "rank": int(rank),
                "match_name": str(mn or ""),
                "fav_player": str(fav or ""),
                "p_model_pct": round(float(p or 0) * 100, 1),
                "ev_pct": round(float(ev or 0), 1),
                "odd": float(odd or 0),
            }
        )
    return out


def _global_top5_from_db_rows(db_rows: list[dict]) -> list[dict]:
    """Top 5 global depuis les lignes DB (tous circuits confondus)."""
    ranked = sorted(
        db_rows,
        key=lambda r: (-float(r.get("p_model_pct") or 0), str(r.get("match_name") or "").lower()),
    )
    out: list[dict] = []
    for i, row in enumerate(ranked[:TOP_N], start=1):
        r = dict(row)
        r["source"] = "db_global_top5"
        r["rank"] = i
        out.append(r)
    return out


def _compare_lists(a: list[dict], b: list[dict]) -> dict[str, Any]:
    keys_a = [_pick_key(r["match_name"], r["fav_player"]) for r in a]
    keys_b = [_pick_key(r["match_name"], r["fav_player"]) for r in b]
    only_a = [a[i] for i, k in enumerate(keys_a) if k not in keys_b]
    only_b = [b[i] for i, k in enumerate(keys_b) if k not in keys_a]
    order_diff = keys_a != keys_b and not only_a and not only_b
    return {
        "match": keys_a == keys_b,
        "order": keys_a == keys_b,
        "only_a": only_a,
        "only_b": only_b,
        "order_diff": order_diff,
    }


def _funnel_store(matches: list[dict], *, today_only: bool, ev_min: float, ev_max: float) -> dict:
    from scripts.daily_top_proba_store import _match_favorite_metrics, is_today_paris_match

    stats = {"total": len(matches), "today": 0, "with_metrics": 0, "ev_in_band": 0}
    for m in matches:
        if today_only and not is_today_paris_match(m):
            continue
        stats["today"] += 1
        met = _match_favorite_metrics(m)
        if met is None:
            continue
        stats["with_metrics"] += 1
        ev = float(met.get("ev_fav") or 0)
        if ev_min <= ev <= ev_max:
            stats["ev_in_band"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (defaut: aujourd'hui Paris)")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    ap.add_argument("--export", default=None)
    args = ap.parse_args()

    cal_day = args.date or datetime.now(PARIS_TZ).date().isoformat()

    from scripts.daily_top_proba_store import (
        collect_top5_proba_picks,
        load_today_matches_for_daily_top_proba,
    )

    store_matches, meta = load_today_matches_for_daily_top_proba()
    if args.date:
        store_matches = [m for m in store_matches if str(m.get("date") or "")[:10] == cal_day]

    telegram_picks = collect_top5_proba_picks(
        store_matches,
        limit=TOP_N,
        ev_min_frac=EV_MIN,
        ev_max_frac=EV_MAX,
        today_only=True,
        calendar_date=cal_day,
    )
    telegram_rows = _rows_from_store_picks(telegram_picks, source="telegram_top5")

    daemon_rows = _rows_from_db(args.db, cal_day, top_n=TOP_N)
    db_global = _global_top5_from_db_rows(daemon_rows)

    _install_streamlit_mock()
    from app.dashboard import (  # noqa: E402
        _collect_top_favorite_action_cards,
        _compute_favorite_ev_funnel_stats,
        _load_today_tracked_matches_for_inplay,
    )

    paris_matches = _load_today_tracked_matches_for_inplay()
    if args.date:
        paris_matches = [m for m in paris_matches if str(m.get("date") or "")[:10] == cal_day]
    paris_cards = _collect_top_favorite_action_cards(paris_matches, limit=TOP_N)
    paris_rows = _rows_from_cards(paris_cards)

    funnel_telegram = _funnel_store(store_matches, today_only=True, ev_min=EV_MIN, ev_max=EV_MAX)
    funnel_paris = _compute_favorite_ev_funnel_stats(
        paris_matches,
        today_only=True,
        ev_min_frac=EV_MIN,
        ev_max_frac=EV_MAX,
    )

    cmp_pt = _compare_lists(paris_rows, telegram_rows)
    cmp_db = _compare_lists(paris_rows, db_global) if db_global else {"match": True, "order": True, "only_a": [], "only_b": [], "order_diff": False}

    print("=" * 88)
    print(f"AUDIT PARITE PICKS — {cal_day} · Top {TOP_N} proba · EV +15 % -> +100 %")
    print("=" * 88)
    n_snap = int((meta or {}).get("n_matches") or 0)
    print(f"Snapshot : {n_snap} matchs disque · pool Telegram (jour+rang) : {funnel_telegram['today']}")
    print(f"Pool Paris du jour (jour+rang+ATP/WTA majeur) : {funnel_paris['today']}")
    print()
    print("--- Entonnoir EV (Paris du jour) ---")
    print(
        f"  {funnel_paris['total']} pool -> {funnel_paris['today']} jour -> "
        f"{funnel_paris['with_metrics']} cotes/probas -> "
        f"{funnel_paris['ev_in_band']} EV 15-100 % -> Top {TOP_N} : {len(paris_rows)}"
    )
    print("--- Entonnoir EV (Telegram / store) ---")
    print(
        f"  {funnel_telegram['total']} pool -> {funnel_telegram['today']} jour -> "
        f"{funnel_telegram['with_metrics']} cotes/probas -> "
        f"{funnel_telegram['ev_in_band']} EV 15-100 % -> Top {TOP_N} : {len(telegram_rows)}"
    )
    print()

    def _print_table(label: str, rows: list[dict]) -> None:
        print(f"--- {label} ({len(rows)}) ---")
        if not rows:
            print("  (vide)")
            return
        for r in rows:
            print(
                f"  #{r['rank']} {r['fav_player']} @ {r['odd']:.2f} · "
                f"p={r['p_model_pct']:.1f}% ev={r['ev_pct']:+.1f}% · {r['match_name']}"
            )

    _print_table("Paris du jour", paris_rows)
    print()
    _print_table("Telegram / collect_top5_proba_picks", telegram_rows)
    print()
    _print_table("DB daily_top_proba (top 5 / circuit)", daemon_rows)
    if db_global:
        print()
        _print_table("DB recompose global top 5", db_global)

    print()
    print("--- Comparaisons ---")
    status_pt = "OK" if cmp_pt["match"] and cmp_pt["order"] else "ECART"
    print(f"  Paris vs Telegram : {status_pt}")
    if not cmp_pt["match"]:
        for r in cmp_pt["only_a"]:
            print(f"    - Paris seulement : {r['fav_player']} ({r['match_name']})")
        for r in cmp_pt["only_b"]:
            print(f"    - Telegram seulement : {r['fav_player']} ({r['match_name']})")
    elif cmp_pt.get("order_diff"):
        print("    Memes matchs, ordre different.")

    if db_global:
        status_db = "OK" if cmp_db["match"] and cmp_db["order"] else "ECART (attendu si DB par circuit)"
        print(f"  Paris vs DB global top5 : {status_db}")

    if funnel_paris["today"] != funnel_telegram["today"]:
        print(
            f"\n  Note : pools differents ({funnel_paris['today']} vs {funnel_telegram['today']}) — "
            "Paris filtre les tournois ATP/WTA mineurs (Challenger/ITF)."
        )

    if args.export:
        import pandas as pd

        all_rows = paris_rows + telegram_rows + daemon_rows + db_global
        os.makedirs(os.path.dirname(args.export) or ".", exist_ok=True)
        pd.DataFrame(all_rows).to_csv(args.export, index=False, encoding="utf-8-sig")
        print(f"\nExport : {os.path.relpath(args.export, ROOT)}")

    print("=" * 88)
    return 0 if cmp_pt["match"] and cmp_pt["order"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
