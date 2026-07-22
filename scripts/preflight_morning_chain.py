#!/usr/bin/env python3
"""Preflight chaîne matinale prod — imports, snapshot, picks, QC."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_ENV", "prod")
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

FAILURES: list[str] = []
WARNINGS: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK  {msg}")


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARN {msg}")


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    from datetime import datetime
    from zoneinfo import ZoneInfo

    PARIS = ZoneInfo("Europe/Paris")
    today = datetime.now(PARIS).date().isoformat()
    print(f"=== Preflight morning chain — {today} ===\n")

    # 1) Critical imports (build predict path)
    print("[1] Imports build predict")
    try:
        from scripts.stats_engine import reconcile_days_with_recent_wins

        assert reconcile_days_with_recent_wins(14, 2) == 7
        ok("stats_engine.reconcile_days_with_recent_wins")
    except Exception as exc:
        fail(f"stats_engine.reconcile_days_with_recent_wins: {exc}")

    try:
        from scripts.match_rank_quality import (
            capped_p1_prob_from_match,
            passes_public_pick_gates,
            reconcile_match_true_odds_from_caps,
        )

        ok("match_rank_quality gates")
    except Exception as exc:
        fail(f"match_rank_quality: {exc}")

    # 2) Snapshot health
    print("\n[2] Snapshot")
    from scripts.live_snapshot import load_latest_live_snapshot

    allm, meta = load_latest_live_snapshot(max_age_sec=86400)
    n = len(allm or [])
    if n < 1:
        fail("snapshot empty")
    else:
        ok(f"{n} matches in snapshot")
    has_cap = sum(
        1 for m in (allm or []) if capped_p1_prob_from_match(m) is not None
    )
    if has_cap != n:
        fail(f"capped_p1_prob missing on {n - has_cap}/{n} matches")
    else:
        ok(f"capped_p1_prob on all {n} matches")

    age_h = (
        (datetime.now(PARIS).timestamp() - float(meta.get("built_at") or 0)) / 3600
        if meta.get("built_at")
        else 999
    )
    if age_h > 24:
        warn(f"snapshot age {age_h:.1f}h (>24h before tomorrow build)")
    else:
        ok(f"snapshot age {age_h:.1f}h")

    print("\n[2b] ML predict smoke")
    try:
        from scripts.ml_model import TennisMLModel
        from scripts.stats_engine import TennisStatsEngine

        ml = TennisMLModel()
        ml._load_bundle_if_needed()
        se = TennisStatsEngine("data/bettinghud.db")
        m = (allm or [None])[0]
        if not m:
            fail("no match for predict smoke")
        else:
            p1 = str(m.get("player1") or "")
            p2 = str(m.get("player2") or "")
            tour = str(m.get("tour") or "ATP")
            st1 = se.get_player_stats(m.get("p1_player_id") or p1, p1, tour_hint=tour)
            st2 = se.get_player_stats(m.get("p2_player_id") or p2, p2, tour_hint=tour)
            d1 = reconcile_days_with_recent_wins(7, 0)
            preds = ml.predict_match(
                surface=str(m.get("surface") or "Hard"),
                p1_name=p1,
                p2_name=p2,
                p1_rank=st1["rank"],
                p2_rank=st2["rank"],
                p1_age=st1["age"],
                p2_age=st2["age"],
                p1_ht=st1["ht"],
                p2_ht=st2["ht"],
                p1_pts=st1["pts"],
                p2_pts=st2["pts"],
                p1_id=m.get("p1_player_id"),
                p2_id=m.get("p2_player_id"),
                tour=tour,
                p1_days_since_last_match=d1,
                p2_days_since_last_match=d1,
            )
            cap = (preds.get("feature_snapshot") or {}).get("capped_p1_prob")
            if cap is None:
                fail("predict smoke: no capped_p1_prob")
            else:
                ok(f"predict smoke cap={float(cap)*100:.1f}% ({p1} vs {p2})")
    except Exception as exc:
        fail(f"predict smoke: {exc}")

    # 3) QC live snapshot
    print("\n[3] QC live snapshot")
    try:
        from scripts.qc_live_snapshot import run_qc_live_snapshot

        qc = run_qc_live_snapshot(matches=allm)
        issues = getattr(qc, "issues", None) or (qc.get("issues") if isinstance(qc, dict) else [])
        if issues:
            for iss in issues[:5]:
                warn(f"QC: {iss}")
        else:
            ok("QC no issues")
    except Exception as exc:
        fail(f"qc_live_snapshot: {exc}")

    # 4) Pick pipeline
    print("\n[4] Pick pipeline")
    from scripts.daily_top_proba_store import (
        collect_hybrid_proba_picks,
        load_today_matches_for_daily_top_proba,
    )
    from scripts.discord_1d1p_core import load_1d1p_today_pick
    from scripts.pick_modes import PickMode, load_picks

    matches, _ = load_today_matches_for_daily_top_proba()
    if not matches:
        warn("0 matches after gates today (can be normal if slate weak)")
    else:
        ok(f"{len(matches)} matches after public gates")

    from scripts.hybrid_pick_selection import HYBRID_DEFAULT_LIMIT

    hyb = collect_hybrid_proba_picks(matches, limit=HYBRID_DEFAULT_LIMIT, calendar_date=today)
    pick, _, pool, _ = load_1d1p_today_pick(db_path="data/bettinghud.db", calendar_date=today)
    t5 = load_picks(PickMode.TOP5)
    d1 = load_picks(PickMode.ONE_PICK_ONE_DAY)

    ok(f"hybrid={len(hyb)} top5={len(t5.picks)} 1d1p={d1.pick_today.get('fav_player') if d1.pick_today else None}")

    # 5) TG/Discord env
    print("\n[5] Publication env")
    env = (os.getenv("BETTINGHUD_ENV") or "").lower()
    if env != "prod":
        warn(f"BETTINGHUD_ENV={env}")
    else:
        ok("BETTINGHUD_ENV=prod")

    if os.getenv("TELEGRAM_TOP5_AFTER_MORNING", "").strip().lower() in ("1", "true", "yes"):
        ok("TELEGRAM_TOP5_AFTER_MORNING enabled")
    else:
        warn("TELEGRAM_TOP5_AFTER_MORNING off")

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if token:
        ok("TELEGRAM_BOT_TOKEN set")
    else:
        fail("TELEGRAM_BOT_TOKEN missing")

    from scripts.telegram_1d1p_notify import telegram_1d1p_enabled

    if telegram_1d1p_enabled():
        ok("1D1P telegram enabled")
    else:
        warn("1D1P telegram disabled")

    dc = (os.getenv("DISCORD_1D1P_WEBHOOK_URL") or "").strip()
    if dc:
        ok("DISCORD_1D1P_WEBHOOK_URL set")
    else:
        warn("Discord 1D1P webhook missing")

    # 6) Dry-run notifications (no send)
    print("\n[6] Dry-run notifications")
    try:
        from scripts.telegram_top5_notify import run_notify

        tg = run_notify(dry_run=True, source="preflight")
        ok(f"TG top5 dry-run n_picks={tg.get('n_picks', '?')}")
    except Exception as exc:
        fail(f"telegram_top5 dry-run: {exc}")

    try:
        from scripts.telegram_1d1p_notify import run_daily_pick

        o = run_daily_pick(dry_run=True, source="preflight")
        if o.get("reason") == "already_posted":
            ok("1D1P dry-run: already_posted today (expected after repost)")
        elif o.get("ok"):
            ok(f"1D1P dry-run n_picks={o.get('n_picks', 0)}")
        else:
            warn(f"1D1P dry-run: {o}")
    except Exception as exc:
        fail(f"telegram_1d1p dry-run: {exc}")

    # Summary
    print("\n=== SUMMARY ===")
    print(f"FAILURES: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
    print(f"WARNINGS: {len(WARNINGS)}")
    for w in WARNINGS:
        print(f"  - {w}")
    if FAILURES:
        sys.exit(1)
    print("\nChain OK for next morning build (modulo slate/picks du jour).")


if __name__ == "__main__":
    main()
