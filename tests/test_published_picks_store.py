"""Tests archive picks publiés."""
from __future__ import annotations

import os
import sqlite3
import tempfile

from scripts.bets_db import ensure_daily_top_proba_schema
from scripts.published_picks_store import (
    MODE_TOP5,
    ensure_published_picks_schema,
    save_published_picks,
    select_historical_top5_picks,
)


def test_published_overrides_hybrid_reselect():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        ensure_daily_top_proba_schema(conn)
        ensure_published_picks_schema(conn)
        cal = "2026-07-22"

        def seed(key: str, fav: str, rel: int, rank: int) -> None:
            conn.execute(
                """
                INSERT INTO daily_top_proba_picks (
                    pick_key, calendar_date, match_date, tour, rank, match_name,
                    player1, player2, fav_player, p_model_fav, ev_fav_pct, odd_fav,
                    data_reliability_score, status, tournament
                ) VALUES (?, ?, ?, 'ATP', ?, ?, 'A', 'B', ?, 0.85, 25.0, 1.5, ?, 'En cours', 'Estoril')
                """,
                (key, cal, cal, rank, f"A vs {fav}", fav, rel),
            )

        seed(f"{cal}|ATP|01", "Droguet T.", 80, 1)
        seed(f"{cal}|ATP|02", "Hanfmann Y.", 80, 2)
        seed(f"{cal}|ATP|03", "Vacherot V.", 100, 3)
        conn.commit()
        rows = [
            dict(r) for r in conn.execute("SELECT * FROM daily_top_proba_picks").fetchall()
        ]
        save_published_picks(
            conn,
            mode=MODE_TOP5,
            calendar_date=cal,
            picks=[
                {"pick_key": f"{cal}|ATP|01", "fav_player": "Droguet T.", "tour": "ATP"},
                {"pick_key": f"{cal}|ATP|02", "fav_player": "Hanfmann Y.", "tour": "ATP"},
            ],
            source="test",
        )
        conn.close()

        picked = select_historical_top5_picks(path, rows, exclude_date="2099-01-01", limit=6)
        assert [p.get("fav_player") for p in picked] == ["Droguet T.", "Hanfmann Y."]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
