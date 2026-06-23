"""Locale for outbound Telegram & Discord messages (not the web app UI).

Policy (since 2026-06): public bot/channel posts are **English**.
Internal docs may stay French. Web app i18n (FR/EN) is unchanged.

Env: ``COMMS_LOCALE`` — ``en`` (default) or ``fr`` (legacy preview only).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PARIS_TZ = ZoneInfo("Europe/Paris")

_WD_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MO_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WD_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MO_FR = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)

DISCLAIMER_EN = "Information only — not financial advice. Bet responsibly."
DISCLAIMER_FR = "Information — pas un conseil de pari. Jouez responsablement."

# Public brand for Telegram / Discord (display name in messages; @username unchanged via BotFather).
BRAND_NAME = (os.getenv("COMMS_BRAND_NAME") or "CourtAlpha").strip() or "CourtAlpha"
PUBLIC_SITE_URL = (os.getenv("COURTALPHA_PUBLIC_URL") or "https://courtalpha.tech").rstrip("/")
TELEGRAM_BOT_USERNAMES: tuple[str, ...] = tuple(
    u.strip().lstrip("@")
    for u in (os.getenv("TELEGRAM_BOT_USERNAMES") or "CourtAlphabot").split(",")
    if u.strip()
)


def telegram_command_aliases(*bases: str) -> frozenset[str]:
    """Command tokens plus @username variants for group chats."""
    out: set[str] = set()
    for b in bases:
        out.add(b)
        if "@" not in b:
            for username in TELEGRAM_BOT_USERNAMES:
                out.add(f"{b}@{username}")
    return frozenset(out)


def comms_locale() -> str:
    return (os.getenv("COMMS_LOCALE") or "en").strip().lower()


def comms_is_english() -> bool:
    return not comms_locale().startswith("fr")


def comms_disclaimer() -> str:
    return DISCLAIMER_EN if comms_is_english() else DISCLAIMER_FR


def tg(en: str, fr: str) -> str:
    """Outbound Telegram copy — English by default (COMMS_LOCALE=en)."""
    return en if comms_is_english() else fr


def format_calendar_date_label(calendar_date: str, *, tz: ZoneInfo = PARIS_TZ) -> str:
    """Human date for TG/Discord headers (Europe/Paris today/tomorrow)."""
    try:
        d = date.fromisoformat(str(calendar_date)[:10])
    except ValueError:
        return str(calendar_date)
    today = datetime.now(tz).date()
    if comms_is_english():
        base = f"{_WD_EN[d.weekday()]} {d.day} {_MO_EN[d.month - 1]} {d.year}"
        if d == today:
            return f"Today · {base}"
        if d == today + timedelta(days=1):
            return f"Tomorrow · {base}"
        return base
    base = f"{_WD_FR[d.weekday()]} {d.day} {_MO_FR[d.month - 1]} {d.year}"
    if d == today:
        return f"Aujourd'hui · {base}"
    if d == today + timedelta(days=1):
        return f"Demain · {base}"
    return base


def telegram_bot_username() -> str:
    return TELEGRAM_BOT_USERNAMES[0] if TELEGRAM_BOT_USERNAMES else "CourtAlphabot"


def telegram_bot_markdown_link() -> str:
    user = telegram_bot_username()
    return f"[@{user}](https://t.me/{user})"


def telegram_pick_criteria_line(*, min_proba_pct: float, min_ev_pct: float, ev_max_pct: float | None = 100.0) -> str:
    from scripts.match_rank_quality import MIN_DATA_RELIABILITY_SCORE

    rel = f" · fiabilité data ≥{MIN_DATA_RELIABILITY_SCORE}"
    if comms_is_english():
        ev_part = (
            f"EV <code>&gt;{min_ev_pct:.0f}%</code> → <code>+{ev_max_pct:.0f}%</code>"
            if ev_max_pct is not None
            else f"EV <code>&gt;{min_ev_pct:.0f}%</code>"
        )
        return (
            f"📊 Model proba <code>&gt;{min_proba_pct:.0f}%</code> · {ev_part}"
            f" · data reliability ≥{MIN_DATA_RELIABILITY_SCORE} · sorted by proba ↓"
        )
    ev_part = (
        f"EV <code>&gt;{min_ev_pct:.0f}%</code> → <code>+{ev_max_pct:.0f}%</code>"
        if ev_max_pct is not None
        else f"EV <code>&gt;{min_ev_pct:.0f}%</code>"
    )
    return (
        f"📊 Proba <code>&gt;{min_proba_pct:.0f}%</code> · {ev_part}{rel} · tri proba modèle ↓"
    )
