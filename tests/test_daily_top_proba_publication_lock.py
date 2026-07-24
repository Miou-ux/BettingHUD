"""Verrou publication matin — daily_top_proba_picks ne doit pas être écrasé intraday."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import (
    _is_publication_locked,
    _should_skip_intraday_identity_overwrite,
    ensure_daily_top_proba_schema,
    upsert_daily_top_proba_picks,
)


def _row(*, key: str, match_name: str, source: str, first_ts: str | None = None) -> dict:
    cal, tour, rank_s = key.split("|")
    return {
        "pick_key": key,
        "calendar_date": cal,
        "match_date": cal,
        "tour": tour,
        "rank": int(rank_s),
        "match_name": match_name,
        "fav_player": match_name.split(" vs ")[-1],
        "p_model_fav": 0.7,
        "ev_fav_pct": 20.0,
        "capture_source": source,
        "first_captured_ts": first_ts,
        "last_captured_ts": first_ts,
    }


def test_publication_lock_helpers():
    assert _is_publication_locked("2026-07-06T05:29:39+02:00", "2026-07-06")
    assert not _is_publication_locked("2026-07-06T04:10:00+02:00", "2026-07-06")
    assert _should_skip_intraday_identity_overwrite(
        existing_first_ts="2026-07-06T05:29:39+02:00",
        existing_calendar_date="2026-07-06",
        incoming_source="portfolio_results_daemon",
    )
    assert not _should_skip_intraday_identity_overwrite(
        existing_first_ts="2026-07-06T05:29:39+02:00",
        existing_calendar_date="2026-07-06",
        incoming_source="backfill_jsonl_publish_5am",
    )


def test_upsert_blocks_intraday_identity_overwrite():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = sqlite3.connect(db_path)
        ensure_daily_top_proba_schema(conn)
        conn.close()

        morning = _row(
            key="2026-07-06|WTA|01",
            match_name="Krueger A. vs Kostyuk M. (12)",
            source="live_snapshot",
            first_ts="2026-07-06T05:29:39+02:00",
        )
        upsert_daily_top_proba_picks([morning], db_path=db_path)

        intraday = _row(
            key="2026-07-06|WTA|01",
            match_name="Other A. vs Other B.",
            source="portfolio_results_daemon",
        )
        upsert_daily_top_proba_picks([intraday], db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT match_name, capture_source, first_captured_ts FROM daily_top_proba_picks WHERE pick_key = ?",
            ("2026-07-06|WTA|01",),
        ).fetchone()
        conn.close()

        assert row[0] == "Krueger A. vs Kostyuk M. (12)"
        assert row[1] == "live_snapshot"
        assert row[2] == "2026-07-06T05:29:39+02:00"


if __name__ == "__main__":
    test_publication_lock_helpers()
    test_upsert_blocks_intraday_identity_overwrite()
    print("ok")
