"""Interactive picks must be sent in rank order (no parallel shuffle)."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.telegram_top5_notify import send_interactive_pick_messages


def test_send_interactive_picks_preserves_rank_order():
    sent_bodies: list[str] = []

    def fake_send(body, *, token, chat_id, reply_markup=None):
        sent_bodies.append(str(body))

    picks = [
        {"rank": 3, "match_name": "C vs D", "fav_player": "C", "bet_on": "C"},
        {"rank": 1, "match_name": "A vs B", "fav_player": "A", "bet_on": "A"},
        {"rank": 2, "match_name": "E vs F", "fav_player": "E", "bet_on": "E"},
    ]
    with patch("scripts.telegram_top5_notify.send_telegram_message", fake_send):
        with patch(
            "scripts.telegram_bet_flow.register_picks",
            return_value=["t1", "t2", "t3"],
        ):
            with patch(
                "scripts.telegram_bet_flow.existing_stakes_eur_for_picks",
                return_value={},
            ):
                with patch(
                    "scripts.telegram_bet_flow.format_pick_telegram_card",
                    side_effect=lambda p, **_: p["bet_on"],
                ):
                    n = send_interactive_pick_messages(
                        picks,
                        header_text="hdr",
                        footer_text="",
                        token="tok",
                        chat_id="1",
                        list_kind="top5",
                        telegram_user_id="1",
                    )
    assert n == 4  # header + 3 picks
    assert sent_bodies[1:] == ["A", "E", "C"]
