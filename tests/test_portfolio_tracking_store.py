"""Tests portfolio_tracking_store."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from scripts.portfolio_tracking_store import (
    MODE_1D1P,
    build_portfolio_curve_and_summary,
    init_portfolio_tracking,
    load_portfolio_replay_picks,
    recompute_portfolio_ledger,
    sync_portfolio_from_published,
)
from scripts.published_picks_store import ensure_published_picks_schema, save_published_picks


class PortfolioTrackingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.conn = sqlite3.connect(self.db_path)
        from scripts.bets_db import ensure_daily_top_proba_schema

        ensure_daily_top_proba_schema(self.conn)
        ensure_published_picks_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO daily_top_proba_picks (
                pick_key, calendar_date, match_date, tour, rank, fav_player, match_name,
                player1, player2, p_model_fav, ev_fav_pct, odd_fav, status,
                segment_brier, theoretical_stake_frac
            ) VALUES (?, ?, ?, 'ATP', 1, 'A', 'A vs B', 'A', 'B', 0.8, 20.0, 1.5, 'Gagné', 0.18, 0.05)
            """,
            ("2026-07-24|ATP|01", "2026-07-24", "2026-07-24"),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.db_path)

    def test_ledger_roundtrip(self) -> None:
        init_portfolio_tracking(
            self.conn, mode=MODE_1D1P, start_date="2026-07-24", bankroll_start_eur=100.0
        )
        save_published_picks(
            self.conn,
            mode=MODE_1D1P,
            calendar_date="2026-07-24",
            picks=[
                {
                    "pick_key": "2026-07-24|ATP|01",
                    "fav_player": "A",
                    "match_name": "A vs B",
                    "player1": "A",
                    "player2": "B",
                    "p_model_fav": 0.8,
                    "ev_fav_pct": 20.0,
                    "odd_fav": 1.5,
                }
            ],
            source="test",
        )
        sync_portfolio_from_published(
            self.conn, mode=MODE_1D1P, calendar_date="2026-07-24"
        )

        class _FakeMl:
            segment_brier_scores = {}
            global_test_brier = 0.18

        recompute_portfolio_ledger(self.conn, MODE_1D1P, ml=_FakeMl())

        from scripts.portfolio_tracking_store import reconcile_portfolio_ledger

        rep = reconcile_portfolio_ledger(self.conn, MODE_1D1P, ml=_FakeMl())
        self.assertTrue(rep["ok"], rep)
        rows = load_portfolio_replay_picks(self.conn, MODE_1D1P)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "Gagné")
        self.assertIsNotNone(rows[0].get("replay_net_profit_eur"))
        curve, summary = build_portfolio_curve_and_summary(self.conn, MODE_1D1P)
        self.assertEqual(len(curve), 1)
        self.assertGreater(summary["bankroll_final_eur"], 100.0)


if __name__ == "__main__":
    unittest.main()
