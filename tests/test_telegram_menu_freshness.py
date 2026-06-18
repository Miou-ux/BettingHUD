"""Tests menu Telegram et fraîcheur snapshot."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.telegram_top5_notify import (
    filter_telegram_display_picks,
    format_snapshot_freshness_line,
    resolve_menu_button_text,
)


def test_resolve_menu_button_text():
    assert resolve_menu_button_text("🎯 1 Day 1 Pick") == "/1pick1day"
    assert resolve_menu_button_text("📊 Top 5") == "/top5"
    assert resolve_menu_button_text("📅 Today") == "/today"
    assert resolve_menu_button_text("💰 Bankroll") == "/br"
    assert resolve_menu_button_text("/top5") is None


def test_filter_telegram_ev_inclusive_at_15():
    pick = {"p_model_fav": 0.7, "ev_fav_pct": 15.0}
    out = filter_telegram_display_picks([pick], apply_proba_filter=False)
    assert len(out) == 1
    pick14 = {"p_model_fav": 0.7, "ev_fav_pct": 14.99}
    assert filter_telegram_display_picks([pick14], apply_proba_filter=False) == []


def test_format_snapshot_freshness_stale_warning():
    fresh = format_snapshot_freshness_line(30.0)
    assert "30" in fresh
    assert "🕐" in fresh
    stale = format_snapshot_freshness_line(150.0)
    assert "🔴" in stale
    assert "stale" in stale.lower()
