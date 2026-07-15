"""Discord embeds for the general CourtAlpha server channel (English)."""
from __future__ import annotations

import os
from typing import Any

from scripts.comms_locale import DISCLAIMER_EN, PUBLIC_SITE_URL, telegram_bot_markdown_link

_COLOR_WELCOME = 0x5865F2

# No-leak backtest 2025 — scripts/_methodo_2025_bankrolls.py (½ Kelly).
_BACKTEST_2025 = {
    "start_eur": 100,
    "1d1p_kelly_final_eur": 1257,
    "1d1p_kelly_growth_pct": 1157,
    "1d1p_days": 296,
    "1d1p_hit_pct": 86.5,
    "1d1p_max_dd_pct": 21.1,
    "brier": 0.14,
}


def _site_url() -> str:
    return (os.getenv("COURTALPHA_PUBLIC_URL") or PUBLIC_SITE_URL).strip().rstrip("/")


def _tg_bot_mention() -> str:
    return telegram_bot_markdown_link()


def _eur(n: int | float) -> str:
    return f"{int(n):,}".replace(",", " ")


def build_general_welcome_embed() -> dict[str, Any]:
    """Pinned welcome for #general — overview of CourtAlpha, not billing."""
    site = _site_url()
    tg = _tg_bot_mention()
    b = _BACKTEST_2025

    return {
        "title": "👋 Welcome to CourtAlpha",
        "description": (
            "**Transparent tennis intelligence** — model probabilities, expected value (EV), "
            "auditable track record.\n\n"
            "No deleted picks. No hype. **Measure before you bet.**"
        ),
        "color": _COLOR_WELCOME,
        "fields": [
            {
                "name": "🧠 How it works",
                "value": (
                    "• **ML model** — win probability from form, surface, rankings, tournament context\n"
                    "• **EV filter** — we only flag bets where the model beats bookmaker odds\n"
                    "• **Open results** — proba, EV, and outcomes published (model internals stay private)"
                ),
                "inline": False,
            },
            {
                "name": "📊 Top 5",
                "value": (
                    "• Up to **5 picks/day** · ATP/WTA majors **250+**\n"
                    "• Hybrid · proba **≥77%** · EV tier1 **15–35%** + tier2 **30–55%**\n"
                    "• Reliability **≥75** · book gap **≤30pp** · sorted by **EV** ↓ · "
                    "posted **~05:00 Paris** on Telegram " + tg
                ),
                "inline": False,
            },
            {
                "name": "📅 Today's Picks",
                "value": (
                    "• Live **value bets** · EV **≥15%** · intraday snapshots\n"
                    "• Majors **and** challengers\n"
                    f"• Telegram **Today** · web **[Today's Pick]({site}/live)**"
                ),
                "inline": False,
            },
            {
                "name": "🎯 1 Day 1 Pick",
                "value": (
                    "• **One pick per day** — rank **#1** of the same hybrid Top 5 selection\n"
                    "• Daily posts in **#1pick1day**\n"
                    f"• Public replay → **[1-day-1-pick]({site}/1-day-1-pick)** (no account needed)"
                ),
                "inline": False,
            },
            {
                "name": "💶 2025 backtest · 100 € start",
                "value": (
                    "_No-leak simulation — model trained before 2025 · majors · EV 15–100%_\n\n"
                    "**1 Day 1 Pick** · Kelly 0.65 (Brier-adjusted, 15% cap)\n"
                    f"**{_eur(b['start_eur'])} € → {_eur(b['1d1p_kelly_final_eur'])} €**  "
                    f"(**+{_eur(b['1d1p_kelly_growth_pct'])} %**)\n\n"
                    f"• **{b['1d1p_days']}** picks · **{b['1d1p_hit_pct']}%** hit rate\n"
                    f"• Max drawdown **{b['1d1p_max_dd_pct']}%** · Brier **{b['brier']}**\n"
                    f"• Same staking formula as the [live replay]({site}/1-day-1-pick)\n\n"
                    "_Past performance is illustrative — not a guarantee._"
                ),
                "inline": False,
            },
            {
                "name": "📱 Telegram bot",
                "value": (
                    f"**{tg}** — morning Top 5, Today's Picks, 1D1P, bankroll\n"
                    "Commands: `/today` · `/top5` · `/1pick1day` · `/br` · `/help`"
                ),
                "inline": False,
            },
            {
                "name": "🌐 Quick links",
                "value": (
                    f"• **[courtalpha.tech]({site})** — methodo, archives, FAQ\n"
                    f"• **Telegram** {tg}\n"
                    "• **#1pick1day** — daily pick + track record board"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": f"{DISCLAIMER_EN} · Past results ≠ future performance."},
        "url": site,
    }
