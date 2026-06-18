"""Discord embeds for 1 Day 1 Pick (English outbound)."""
from __future__ import annotations

import os
from typing import Any

from scripts.comms_locale import DISCLAIMER_EN, format_calendar_date_label, telegram_bot_markdown_link

_COLOR_PICK = 0x5865F2
_COLOR_WIN = 0x57F287
_COLOR_LOSS = 0xED4245
_COLOR_VOID = 0x99AAB5
_COLOR_NOPICK = 0xFEE75C
_TOUR_EMOJI = {"ATP": "🟢", "WTA": "🔵"}


def _site_url() -> str:
    return (os.getenv("DISCORD_1D1P_SITE_URL") or "https://courtalpha.tech/1-day-1-pick").strip()


def _proba_pct(pick: dict[str, Any]) -> float:
    try:
        if pick.get("p_model_pct") is not None:
            return float(pick["p_model_pct"])
    except (TypeError, ValueError):
        pass
    return float(pick.get("p_model_fav") or 0.0) * 100.0


def _ev_pct(pick: dict[str, Any]) -> float:
    try:
        if pick.get("ev_fav_pct") is not None:
            return float(pick["ev_fav_pct"])
    except (TypeError, ValueError):
        pass
    try:
        return float(pick.get("ev_fav") or 0.0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def _pick_names(pick: dict[str, Any]) -> tuple[str, str]:
    bet_on = str(pick.get("fav_player") or pick.get("bet_on") or "").strip()
    opp = str(pick.get("underdog_player") or pick.get("opponent") or "").strip()
    if not bet_on or not opp:
        mn = str(pick.get("match_name") or "")
        if " vs " in mn:
            p1, p2 = mn.split(" vs ", 1)
            bet_on = bet_on or p1.strip()
            opp = opp or p2.strip()
    return bet_on or "?", opp or "?"


def _stake_pct(pick: dict[str, Any]) -> float:
    try:
        frac = float(pick.get("theoretical_stake_frac") or 0.0)
        if frac > 0:
            return frac * 100.0
    except (TypeError, ValueError):
        pass
    return 0.0


def build_daily_pick_embed(pick: dict[str, Any], *, calendar_date: str) -> dict[str, Any]:
    bet_on, opp = _pick_names(pick)
    tour = str(pick.get("tour") or "").upper()
    tour_tag = _TOUR_EMOJI.get(tour, "🎾")
    odd = float(pick.get("odd_fav") or 0.0)
    p_pct = _proba_pct(pick)
    ev_pct = _ev_pct(pick)
    stake_pct = _stake_pct(pick)
    tourn = str(pick.get("tournament") or "").strip()
    surface = str(pick.get("surface") or "").strip()
    when = str(pick.get("match_time") or "").strip()
    meta_bits = [x for x in (tourn, surface, when) if x]

    return {
        "title": f"{tour_tag} 1 Day 1 Pick — {format_calendar_date_label(calendar_date)}",
        "description": f"**{bet_on}** vs {opp}",
        "color": _COLOR_PICK,
        "fields": [
            {"name": "Model proba", "value": f"{p_pct:.1f}%", "inline": True},
            {"name": "EV", "value": f"{ev_pct:+.1f}%", "inline": True},
            {"name": "Odds", "value": f"@{odd:.2f}", "inline": True},
            {"name": "Kelly stake (theo.)", "value": f"{stake_pct:.1f}% BR", "inline": True},
            {"name": "Tour", "value": tour or "—", "inline": True},
            {
                "name": "Context",
                "value": " · ".join(meta_bits) if meta_bits else "—",
                "inline": False,
            },
        ],
        "footer": {"text": f"{DISCLAIMER_EN} · courtalpha.tech"},
        "url": _site_url(),
    }


def build_no_pick_embed(*, calendar_date: str, pool_size: int) -> dict[str, Any]:
    return {
        "title": f"🎾 1 Day 1 Pick — {format_calendar_date_label(calendar_date)}",
        "description": (
            "No value pick today.\n\n"
            "Rules: best EV-eligible pick per circuit (proba ↓) · max model proba · EV 15–100% · main draw 250+."
        ),
        "color": _COLOR_NOPICK,
        "fields": [
            {
                "name": "Pool scanned",
                "value": f"{pool_size} eligible major match(es) in EV band",
                "inline": False,
            }
        ],
        "footer": {"text": "courtalpha.tech/1-day-1-pick"},
        "url": _site_url(),
    }


def build_result_embed(pick: dict[str, Any]) -> dict[str, Any]:
    from scripts.score_display import format_tennis_score_display

    status = str(pick.get("status") or "En cours")
    cal = str(pick.get("calendar_date") or "")[:10]
    bet_on, opp = _pick_names(pick)
    score = format_tennis_score_display(pick.get("score_final")) or str(pick.get("score_final") or "—")

    try:
        profit_frac = float(pick.get("theoretical_profit") or 0.0)
    except (TypeError, ValueError):
        profit_frac = 0.0
    profit_pct = profit_frac * 100.0

    if status == "Gagné":
        color = _COLOR_WIN
        headline = "✅ Won"
    elif status == "Perdu":
        color = _COLOR_LOSS
        headline = "❌ Lost"
    elif status == "Annulé":
        color = _COLOR_VOID
        headline = "⏸️ Void (retirement / walkover)"
    else:
        color = _COLOR_PICK
        headline = status

    pnl_line = f"{profit_pct:+.2f}% BR (theo.)" if status != "Annulé" else "Stake refunded (0% BR)"

    return {
        "title": f"1D1P result — {format_calendar_date_label(cal)}",
        "description": f"**{bet_on}** vs {opp}\n{headline}",
        "color": color,
        "fields": [
            {"name": "Score", "value": score, "inline": True},
            {"name": "Theo. P/L", "value": pnl_line, "inline": True},
        ],
        "footer": {"text": "Track record · courtalpha.tech/1-day-1-pick"},
        "url": _site_url(),
    }


_COLOR_BOARD = 0x00D4AA
_PIN_CONTENT = (
    "📌 **Live track record** — updated daily after the morning pick and when results settle."
)


def performance_board_pin_content() -> str:
    return _PIN_CONTENT


def _pick_settled(pick: dict[str, Any]) -> bool:
    if pick.get("won") or pick.get("lost"):
        return True
    return str(pick.get("status") or "") in ("Gagné", "Perdu", "Annulé")


def _result_icon(pick: dict[str, Any]) -> str:
    if pick.get("won") or str(pick.get("status") or "") == "Gagné":
        return "✅"
    if pick.get("lost") or str(pick.get("status") or "") == "Perdu":
        return "❌"
    return "⏸️"


def build_performance_board_embed(replay: dict[str, Any], *, calendar_date: str) -> dict[str, Any]:
    summary = replay.get("summary") or {}
    pick_today = replay.get("pick_today")
    picks = list(replay.get("picks") or [])

    n = int(summary.get("n_picks") or 0)
    n_won = int(summary.get("n_won") or 0)
    n_lost = int(summary.get("n_lost") or 0)
    n_void = int(summary.get("n_void") or 0)
    hit = float(summary.get("hit_pct") or 0.0)
    growth = float(summary.get("growth_pct") or 0.0)
    br = float(summary.get("bankroll_final_eur") or 100.0)
    net = float(summary.get("net_profit_eur") or 0.0)
    roi = float(summary.get("roi_on_staked_pct") or 0.0)
    max_dd = float(summary.get("max_drawdown_pct") or 0.0)

    period_start = str(summary.get("period_start") or summary.get("start_date") or "").strip()
    period_end = str(
        summary.get("period_end") or summary.get("end_date") or calendar_date
    ).strip()
    period = f"{period_start} → {period_end}" if period_start else f"Through {period_end}"

    if pick_today:
        fav, opp = _pick_names(pick_today)
        today_line = f"**{fav}** vs {opp} · EV {_ev_pct(pick_today):+.1f}%"
    else:
        today_line = "No value pick today (EV band 15–100%)."

    fields: list[dict[str, Any]] = [
        {"name": "Record", "value": f"{n_won}W · {n_lost}L · {n_void}V", "inline": True},
        {"name": "Hit rate", "value": f"{hit:.1f}%", "inline": True},
        {"name": "Picks", "value": str(n), "inline": True},
        {
            "name": "Bankroll (100€ start)",
            "value": f"{br:.0f}€ ({growth:+.1f}%)",
            "inline": True,
        },
        {"name": "Net P/L", "value": f"{net:+.1f}€", "inline": True},
        {"name": "ROI on staked", "value": f"{roi:+.1f}%", "inline": True},
    ]
    if max_dd > 0:
        fields.append(
            {"name": "Max drawdown", "value": f"{max_dd:.1f}%", "inline": True}
        )
    fields.append(
        {
            "name": f"Today ({format_calendar_date_label(calendar_date)})",
            "value": today_line,
            "inline": False,
        }
    )

    settled = [p for p in picks if _pick_settled(p)]
    # picks replay : date la plus récente en premier
    recent = settled[:5]
    if recent:
        lines = []
        for p in recent:
            cal = str(p.get("calendar_date") or "")[:10]
            fav = str(p.get("fav_player") or p.get("bet_on") or "?")
            lines.append(f"{_result_icon(p)} {cal} · {fav}")
        fields.append({"name": "Recent picks", "value": "\n".join(lines), "inline": False})

    return {
        "title": "📊 1 Day 1 Pick — Track Record",
        "description": f"Public replay · Kelly theo. stakes · {period}",
        "color": _COLOR_BOARD,
        "fields": fields,
        "footer": {"text": f"Updated {calendar_date} · {DISCLAIMER_EN}"},
        "url": _site_url(),
    }


_COLOR_WELCOME = 0x5865F2


def build_welcome_embed() -> dict[str, Any]:
    """Pinned welcome / onboarding message for the 1D1P Discord channel."""
    site = _site_url()
    tg = telegram_bot_markdown_link()
    return {
        "title": "👋 Welcome to CourtAlpha",
        "description": (
            "Your **daily, transparent tennis signal** — one pick per calendar day, "
            "fully auditable on the web. Here's how this channel works."
        ),
        "color": _COLOR_WELCOME,
        "fields": [
            {
                "name": "🎯 What happens here",
                "value": (
                    "Each morning (**~05:00 Paris**) we post **one pick** from major ATP/WTA "
                    "tournaments (250+ main draw).\n"
                    "When the match settles → **result embed** (Win / Loss / Void).\n"
                    "The **📊 Track Record** message updates automatically — "
                    "**pin it** to keep stats at hand."
                ),
                "inline": False,
            },
            {
                "name": "🔬 How picks are chosen",
                "value": (
                    "🟢 **ATP** · 🔵 **WTA** — best EV-eligible pick per circuit (model proba ↓)\n"
                    "Then **highest model probability** between circuits\n"
                    "EV band **15–100%** on the favourite · majors **250+** only\n"
                    "Theoretical stake: **½ Kelly** (Brier-adjusted), cap **15%** bankroll"
                ),
                "inline": False,
            },
            {
                "name": "⏰ Daily rhythm",
                "value": (
                    "🌅 **Morning** — today's pick (or *no value* if nothing qualifies)\n"
                    "🏁 **After the match** — outcome + score\n"
                    "📈 **Track record** — simulated **100 €** bankroll, hit rate, last picks"
                ),
                "inline": False,
            },
            {
                "name": "📱 Telegram bot",
                "value": (
                    f"Same picks on **{tg}** — free to join\n"
                    "• `/1pick1day` — today's pick · `/top5` · `/today` (live value)\n"
                    "• `/br` — bankroll · `/help` — full command list\n"
                    "Morning posts **~05:00 Paris**"
                ),
                "inline": False,
            },
            {
                "name": "🌐 Explore on the web",
                "value": (
                    f"**[Full track record]({site})** — audit every past pick "
                    "(odds, proba, EV, score) · no account needed\n"
                    "Also: **Methodo** (strategy & backtests) · **monthly archives** · **FAQ**"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": DISCLAIMER_EN},
        "url": site,
    }
