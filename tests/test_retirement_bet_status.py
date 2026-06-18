"""Tests _resolve_bet_status_from_match_result retirement / walkover rules."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import (
    _resolve_bet_status_from_match_result,
    correct_retirement_voids_user_bets,
    ensure_match_results_cache,
    ensure_user_bets_schema,
)
from scripts.scraper_results import canonical_player, score_suggests_retirement, match_result_effective_retired
from scripts.scraper_results import _infer_te_retirement


def test_walkover_is_void():
    status, fav_won = _resolve_bet_status_from_match_result(
        "Player A", "Player B", walkover=True, retired=False
    )
    assert status == "Annulé"
    assert fav_won is None


def test_fav_retired_is_void():
    status, fav_won = _resolve_bet_status_from_match_result(
        "Victoria Mboko", "Opponent X", walkover=False, retired=True
    )
    assert status == "Annulé"
    assert fav_won is None


def test_opponent_retired_is_win():
    status, fav_won = _resolve_bet_status_from_match_result(
        "Victoria Mboko", "Victoria Mboko", walkover=False, retired=True
    )
    assert status == "Gagné"
    assert fav_won == 1


def test_normal_loss():
    status, fav_won = _resolve_bet_status_from_match_result(
        "Player A", "Player B", walkover=False, retired=False
    )
    assert status == "Perdu"
    assert fav_won == 0


def test_normal_win():
    status, fav_won = _resolve_bet_status_from_match_result(
        "Player A", "Player A", walkover=False, retired=False
    )
    assert status == "Gagné"
    assert fav_won == 1


def test_score_suggests_retirement_mboko_style():
    assert score_suggests_retirement("6 3 | 2 4")
    assert not score_suggests_retirement("6 4 | 6 2")
    assert not score_suggests_retirement("6 6 | 4 4")
    assert not match_result_effective_retired(
        retired=True, walkover=False, score="6 6 | 3 4"
    )


def test_infer_tied_sets_requires_incomplete_last_set():
    assert not _infer_te_retirement(
        p1_res="1",
        p2_res="1",
        full_score="6 6 | 3 4",
        sets_p1=1,
        sets_p2=1,
    )


def test_correct_retirement_voids_user_bets():
    conn = sqlite3.connect(":memory:")
    ensure_user_bets_schema(conn)
    ensure_match_results_cache(conn)
    now = datetime.utcnow().isoformat(timespec="seconds")
    p1 = canonical_player("Mboko V. (3)")
    p2 = canonical_player("Pliskova K.")
    conn.execute(
        """
        INSERT INTO match_results (
            match_date, p1_canonical, p2_canonical, winner_canonical,
            score, retired, walkover, source, scraped_at
        ) VALUES (?, ?, ?, ?, ?, 1, 0, 'tennisexplorer', ?)
        """,
        ("2026-06-10", p1, p2, p2, "6 3 | 2 4", now),
    )
    conn.execute(
        """
        INSERT INTO user_bets (
            date, match_date, match_name, bet_on, odds, stake, status, profit, score_final
        ) VALUES (?, ?, ?, ?, ?, ?, 'Perdu', ?, ?)
        """,
        (
            "2026-06-10",
            "2026-06-10",
            "Mboko V. (3) vs Pliskova K.",
            "Mboko V. (3)",
            1.85,
            5.46,
            -5.46,
            "6 3 | 2 4",
        ),
    )
    conn.commit()
    n = correct_retirement_voids_user_bets(conn)
    assert n == 1
    row = conn.execute(
        "SELECT status, profit FROM user_bets ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] == "Annulé"
    assert row[1] == 0.0
    conn.close()


def test_correct_retirement_voids_user_bets_no_score_heuristic():
    """TE multi-set scores must not void without explicit retired=1."""
    conn = sqlite3.connect(":memory:")
    ensure_user_bets_schema(conn)
    ensure_match_results_cache(conn)
    now = datetime.utcnow().isoformat(timespec="seconds")
    p1 = canonical_player("Collignon R.")
    p2 = canonical_player("Shelton B. (5)")
    conn.execute(
        """
        INSERT INTO match_results (
            match_date, p1_canonical, p2_canonical, winner_canonical,
            score, retired, walkover, source, scraped_at
        ) VALUES (?, ?, ?, ?, ?, 0, 0, 'tennisexplorer', ?)
        """,
        ("2026-05-28", p1, p2, p1, "6 7 6 | 4 5 4", now),
    )
    conn.execute(
        """
        INSERT INTO user_bets (
            date, match_date, match_name, bet_on, odds, stake, status, profit, score_final
        ) VALUES (?, ?, ?, ?, ?, ?, 'Perdu', ?, ?)
        """,
        (
            "2026-05-28",
            "2026-05-28",
            "Collignon R. vs Shelton B. (5)",
            "Shelton B. (5)",
            2.1,
            5.0,
            -5.0,
            "6 7 6 | 4 5 4",
        ),
    )
    conn.commit()
    assert correct_retirement_voids_user_bets(conn) == 0
    row = conn.execute("SELECT status, profit FROM user_bets").fetchone()
    assert row[0] == "Perdu"
    assert row[1] == -5.0
    conn.close()
