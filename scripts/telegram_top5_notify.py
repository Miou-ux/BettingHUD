#!/usr/bin/env python3
"""Envoie des notifications Telegram CourtAlpha (Top 5 et /jour Live Tracker).

Documentation : docs/TELEGRAM_TOP5.md

Variables d'environnement :
  TELEGRAM_BOT_TOKEN   — token du bot (@BotFather)
  TELEGRAM_CHAT_ID     — id du chat / canal / groupe

Optionnel :
  TELEGRAM_TOP5_LIMIT          (defaut 5)
  TELEGRAM_TOP5_EV_MIN_PCT     (defaut 15)
  TELEGRAM_TOP5_EV_MAX_PCT     (defaut 100)
  TELEGRAM_DAILY_PICKS_LIMIT   (defaut 0 = tous les picks EV+ /jour)
  TELEGRAM_JOUR_EV_MIN_PCT     (defaut 15 = EV >= 15 %)
  TELEGRAM_MIN_PROBA_PCT       (defaut 60 = proba modèle strictement > 60 %)
  TELEGRAM_MIN_EV_PCT          (defaut 15 = EV >= 15 %, filtre affichage)
  TELEGRAM_JOURCHALLENGER_EV_MIN_PCT  (defaut 15)
  TELEGRAM_JOURCHALLENGER_EV_MAX_PCT  (defaut 100)
  TELEGRAM_TOP5_AFTER_MORNING  (pipeline matin)
  BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC

Usage :
  py -3 scripts/telegram_top5_notify.py
  py -3 scripts/telegram_top5_notify.py --dry-run
  py -3 scripts/telegram_top5_notify.py --dry-run --daily   # aperçu /jour
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

import requests

from scripts.comms_locale import BRAND_NAME, PUBLIC_SITE_URL, comms_disclaimer, tg
from scripts.daily_top_proba_store import (
    collect_daily_ev_band_picks,
    collect_paris_du_jour_picks,
    load_today_matches_for_daily_top_proba,
)
from scripts.live_tracker_picks import (
    DEFAULT_CHALLENGER_EV_MAX_PCT,
    DEFAULT_CHALLENGER_EV_MIN_PCT,
    DEFAULT_MAJOR_EV_MAX_PCT,
    DEFAULT_MAJOR_EV_MIN_PCT,
    load_live_tracker_challenger_day_picks,
    load_live_tracker_day_picks,
    load_live_tracker_major_day_picks,
)

PARIS_TZ = ZoneInfo("Europe/Paris")
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"

_WD_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
_MO_FR = (
    "jan.",
    "fév.",
    "mar.",
    "avr.",
    "mai",
    "juin",
    "juil.",
    "aoû.",
    "sep.",
    "oct.",
    "nov.",
    "déc.",
)
_TELEGRAM_MAX_CHARS = 3900
_TELEGRAM_HARD_LIMIT = 4096
_TOUR_EMOJI = {"ATP": "🟢", "WTA": "🔵"}


def _telegram_min_proba_pct() -> float:
    raw = os.getenv("TELEGRAM_MIN_PROBA_PCT", "60").strip()
    return float(raw) if raw else 60.0


def _telegram_min_ev_pct() -> float:
    raw = os.getenv("TELEGRAM_MIN_EV_PCT", "15").strip()
    return float(raw) if raw else 15.0


def _pick_proba_pct(pick: dict) -> float:
    try:
        if pick.get("p_model_pct") is not None:
            return float(pick["p_model_pct"])
    except (TypeError, ValueError):
        pass
    try:
        return float(pick.get("p_model_fav") or 0.0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def _pick_ev_pct(pick: dict) -> float:
    try:
        if pick.get("ev_fav_pct") is not None:
            return float(pick["ev_fav_pct"])
        if pick.get("ev_pct") is not None:
            return float(pick["ev_pct"])
    except (TypeError, ValueError):
        pass
    try:
        ev_f = pick.get("ev_fav")
        if ev_f is not None:
            return float(ev_f) * 100.0
    except (TypeError, ValueError):
        pass
    return 0.0


def filter_telegram_display_picks(
    picks: list[dict],
    *,
    min_proba_pct: float | None = None,
    min_ev_pct: float | None = None,
    apply_proba_filter: bool = True,
) -> list[dict]:
    """Filtre affichage Telegram : EV >= seuil (aligné web/Discord) ; proba optionnelle."""
    from scripts.match_rank_quality import passes_data_reliability_filter

    mp = _telegram_min_proba_pct() if min_proba_pct is None else float(min_proba_pct)
    me = _telegram_min_ev_pct() if min_ev_pct is None else float(min_ev_pct)
    kept: list[dict] = []
    for p in picks:
        if not passes_data_reliability_filter(p):
            continue
        if apply_proba_filter and _pick_proba_pct(p) <= mp:
            continue
        if _pick_ev_pct(p) < me:
            continue
        kept.append(p)
    for rank, row in enumerate(kept, start=1):
        row["rank"] = rank
    return kept


def _telegram_pick_criteria_line(*, ev_max_pct: float | None = 100.0) -> str:
    from scripts.comms_locale import telegram_pick_criteria_line

    return telegram_pick_criteria_line(
        min_proba_pct=_telegram_min_proba_pct(),
        min_ev_pct=_telegram_min_ev_pct(),
        ev_max_pct=ev_max_pct,
    )


def _escape_html(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_date_label(calendar_date: str) -> str:
    from scripts.comms_locale import format_calendar_date_label

    try:
        return format_calendar_date_label(str(calendar_date)[:10])
    except ValueError:
        return _escape_html(calendar_date)


def _rank_icon(rank: int) -> str:
    icons = {1: "🥇", 2: "🥈", 3: "🥉", 4: "4️⃣", 5: "5️⃣"}
    r = int(rank or 0)
    if r in icons:
        return icons[r]
    if 6 <= r <= 10:
        return f"{r}️⃣"
    return f"{r}."


def _format_pick_block(row: dict, *, rank: int | None = None) -> list[str]:
    r = int(rank if rank is not None else row.get("rank") or 0)
    bet_on = _escape_html(row.get("bet_on") or row.get("fav_player") or "?")
    opp = _escape_html(row.get("opponent") or row.get("underdog_player") or "?")
    tour = _escape_html(row.get("tour") or "")
    tourn = _escape_html(row.get("tournament") or "")
    when = _escape_html(row.get("match_time") or "")
    p_pct = float(row.get("p_model_pct") or (row.get("p_model_fav") or 0) * 100.0)
    ev_pct = float(row.get("ev_pct") or row.get("ev_fav_pct") or 0)
    odd = float(row.get("odd_book") or row.get("odd_fav") or 0)
    kelly_pct = float(row.get("theoretical_stake_frac") or 0) * 100.0

    meta_parts: list[str] = []
    if tour:
        meta_parts.append(f"🏟 {tour}")
    if tourn:
        meta_parts.append(tourn)
    if when:
        meta_parts.append(f"🕒 {when}")

    lines = ["", f"{_rank_icon(r)} <b>{bet_on}</b> vs {opp}"]
    if meta_parts:
        lines.append(f"   {' · '.join(meta_parts)}")
    lines.append(
        f"   📊 Proba <b>{p_pct:.1f}%</b> · EV <b>{ev_pct:+.1f}%</b> · Cote <b>@{odd:.2f}</b>"
    )
    lines.append(f"   💰 Kelly reco ~<b>{kelly_pct:.1f}%</b> BR")
    lines.append("")
    return lines


def _format_pick_block_text(row: dict) -> str:
    """Un pick en texte HTML (sans lignes vides en tête/tail inutiles)."""
    return "\n".join(_format_pick_block(row)).strip()


def _join_pick_blocks(picks: list[dict]) -> str:
    """Plusieurs picks dans un même message : double saut de ligne entre chaque."""
    parts = [_format_pick_block_text(row) for row in picks if row]
    return "\n\n".join(p for p in parts if p)


def _split_text_block(block: str, *, max_chars: int) -> list[str]:
    """Découpe un bloc trop long (ex. corps /jour avec dizaines de picks)."""
    block = block.strip()
    if not block:
        return []
    if len(block) <= max_chars:
        return [block]
    lines = block.split("\n")
    pieces: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        line = line.rstrip()
        add = len(line) + (1 if buf else 0)
        if buf and buf_len + add > max_chars:
            pieces.append("\n".join(buf).strip())
            buf = [line] if line else []
            buf_len = len(line)
        else:
            if buf:
                buf_len += 1
            if line:
                buf.append(line)
                buf_len += len(line)
    if buf:
        pieces.append("\n".join(buf).strip())
    if not pieces:
        return [block[:max_chars]]
    return pieces


def _chunk_telegram_messages(parts: list[str], *, max_chars: int = _TELEGRAM_MAX_CHARS) -> list[str]:
    if not parts:
        return [""]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for part in parts:
        for block in _split_text_block(part, max_chars=max_chars):
            extra = len(block) + (2 if current else 0)
            if current and current_len + extra > max_chars:
                chunks.append("\n".join(current).strip())
                current = [block]
                current_len = len(block)
            else:
                if current:
                    current_len += 2
                current.append(block)
                current_len += len(block)
    if current:
        chunks.append("\n".join(current).strip())
    return chunks or [""]


def format_telegram_error_message(title: str, exc: BaseException) -> str:
    """Message d'erreur HTML sûr (évite 400 si l'exception contient des chevrons)."""
    detail = _escape_html(str(exc))[:900]
    return f"⚠️ <b>{_escape_html(title)}</b>\n<code>{detail}</code>"


def _snapshot_age_min(meta: dict) -> float | None:
    from scripts.daily_top_proba_store import snapshot_age_min_from_meta

    return snapshot_age_min_from_meta(meta)


def format_snapshot_freshness_line(snapshot_age_min: float | None) -> str:
    """Ligne visible sur l'âge des cotes (alerte si snapshot ancien)."""
    if snapshot_age_min is None:
        return "🕐 <i>Odds age unknown — always verify live odds at your bookmaker.</i>"
    age = float(snapshot_age_min)
    if age <= 45:
        return (
            f"🕐 Odds data · <b>{age:.0f} min ago</b> — verify live odds before betting"
        )
    if age <= 120:
        return (
            f"⚠️ Odds data · <b>{age:.0f} min ago</b> — may be outdated; "
            "use menu <b>Top 5</b> or <b>Today</b> to refresh"
        )
    return (
        f"🔴 Odds data · <b>{age:.0f} min ago</b> — stale; "
        "refresh via menu <b>Top 5</b> / <b>Today</b> before betting"
    )


_MENU_BUTTON_TO_COMMAND: dict[str, str] = {
    "🎯 1 day 1 pick": "/1pick1day",
    "📊 top 5": "/top5",
    "📅 today": "/today",
    "💰 bankroll": "/br",
    "❓ help": "/help",
    "📖 strategy": "/strategy",
}


def resolve_menu_button_text(text: str) -> str | None:
    """Mappe un libellé du clavier reply vers une commande slash."""
    key = (text or "").strip().lower()
    return _MENU_BUTTON_TO_COMMAND.get(key)


def main_menu_reply_keyboard() -> dict:
    """Clavier reply persistant — évite de mémoriser les commandes."""
    return {
        "keyboard": [
            [
                {"text": "🎯 1 Day 1 Pick"},
                {"text": "📊 Top 5"},
            ],
            [
                {"text": "📅 Today"},
                {"text": "💰 Bankroll"},
            ],
            [
                {"text": "❓ Help"},
                {"text": "📖 Strategy"},
            ],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def register_bot_commands(token: str) -> bool:
    """Enregistre le menu « / » natif Telegram (BotFather-style)."""
    tok = (token or "").strip()
    if not tok:
        return False
    commands = [
        {"command": "start", "description": "Welcome & keyboard menu"},
        {"command": "1pick1day", "description": "One pick per day (majors)"},
        {"command": "top5", "description": "Top 5 model proba"},
        {"command": "today", "description": "Today's value picks"},
        {"command": "br", "description": "Bankroll summary"},
        {"command": "help", "description": "Full command list"},
        {"command": "strategy", "description": "Selection & Kelly staking"},
    ]
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{tok}/setMyCommands",
            json={"commands": commands},
            timeout=15,
        )
        return resp.ok
    except requests.RequestException:
        return False


def pending_access_inline_keyboard() -> dict:
    """Bouton web pour les utilisateurs en attente d'approbation."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📊 1 Day 1 Pick (free track record)",
                    "url": f"{PUBLIC_SITE_URL}/1-day-1-pick",
                }
            ],
        ]
    }


def _count_today_pool(matches: list) -> int:
    from scripts.daily_top_proba_store import _match_favorite_metrics, is_today_paris_match

    pool_n = 0
    for m in matches:
        if not is_today_paris_match(m):
            continue
        if _match_favorite_metrics(m):
            pool_n += 1
    return pool_n


def format_bot_invite_start_message() -> str:
    """First contact — prompt /start."""
    return "\n".join(
        [
            f"👋 <b>Welcome to {BRAND_NAME} Bot</b>",
            "",
            "To <b>request access</b>, send:",
            "",
            "👉 <code>/start</code>",
            "",
            "💡 On Telegram, the blue <b>Start</b> button also sends <code>/start</code>.",
            "",
            "Once an admin approves you, you'll get a short setup guide.",
        ]
    )


def format_bot_onboarding_after_approval() -> str:
    """Post-approval onboarding (English)."""
    from scripts.comms_locale import DISCLAIMER_EN

    return "\n".join(
        [
            "🚀 <b>Getting started</b>",
            "",
            "<b>1. Bankroll</b>",
            "  <code>/brset 80</code> — set starting capital (€)",
            "  <code>/br</code> — available BR · <code>/brstats</code> — detailed stats",
            "",
            "<b>2. Daily picks</b>",
            "  <code>/top5</code> — Top 5 model proba",
            "  <code>/today</code> — Today's Pick (value bets EV ≥15%)",
            "  <code>/1pick1day</code> — one pick per day (same as the website)",
            "",
            "<b>3. Place a bet</b>",
            "  Under each pick: <b>💰 Bet</b> button",
            "  → enter your <b>real odds</b> → Kelly stake → <b>Confirm</b>",
            "  (<code>/cancel</code> to abort a flow in progress)",
            "",
            "<b>4. Learn more</b>",
            "  <code>/strategy</code> — selection & staking (½ Kelly × Brier)",
            "  <code>/help</code> — full command list",
            "",
            "🌅 Every morning (~05:00 Paris): auto <code>/top5</code>.",
            "",
            f"ℹ️ <i>{DISCLAIMER_EN}</i>",
        ]
    )


def format_bot_welcome_message() -> str:
    return "\n".join(
        [
            f"👋 <b>Welcome to {BRAND_NAME} Bot</b>",
            "",
            "I send the <b>Top 5</b> each morning (model proba &gt;60%, EV 15–100%).",
            "",
            "📌 <b>Quick menu</b> — use the buttons below or type commands",
            "  🎯 1 Day 1 Pick · 📊 Top 5 · 📅 Today",
            "  💰 Bankroll · ❓ Help · 📖 Strategy",
            "",
            "Slash commands still work: <code>/1pick1day</code> <code>/top5</code> <code>/today</code>",
            "",
            f"🌐 {PUBLIC_SITE_URL.replace('https://', '')}",
        ]
    )


def format_bot_strategy_message() -> str:
    """Strategy summary (/strategy)."""
    from scripts.comms_locale import DISCLAIMER_EN

    return "\n".join(
        [
            f"📖 <b>{BRAND_NAME} — Strategy</b>",
            "",
            "<b>1. Principle</b>",
            "ML tennis model (ATP/WTA): calibrated win probability from rankings, "
            "form, surface, tournament context.",
            "We only flag a bet when the model shows <b>edge</b> vs book odds (positive EV).",
            "",
            "<b>2. Pick selection</b>",
            "• <b>Today</b> matches (Europe/Paris), valid odds",
            "• <b>Model favorite</b> = highest model probability",
            "• <b>/top5</b>: model favorite, proba &gt;60%, EV 15–100%, majors, top 5 by proba",
            "• <b>/today</b>: value bets EV ≥15% (majors + minors)",
            "• <b>/1pick1day</b>: best EV-eligible pick per circuit (proba ↓), ATP vs WTA, majors",
            "",
            "<b>3. Staking (Kelly)</b>",
            "• <b>½ Kelly</b> (conservative vs full Kelly)",
            "• <b>Brier</b> adjustment by segment (surface / tour)",
            "• <b>15% cap</b> of available bankroll per bet",
            "",
            "<b>4. Bet from Telegram</b>",
            "Under each <b>/today</b> or <b>/top5</b> pick: <b>Bet</b> → your odds → Kelly → Confirm.",
            "",
            "<b>5. In practice</b>",
            "1️⃣ Check /top5, /today or /1pick1day",
            "2️⃣ Verify <b>real odds</b> at your bookmaker",
            "3️⃣ Stake at most the Kelly suggestion",
            "",
            f"ℹ️ <i>{DISCLAIMER_EN} Past results do not guarantee future performance.</i>",
        ]
    )


def format_bot_help_message() -> str:
    return "\n".join(
        [
            f"ℹ️ <b>{BRAND_NAME} Bot — Help</b>",
            "",
            "<b>/1pick1day</b> · /1d1p",
            "  One pick per day · best EV-eligible per circuit · EV 15–100% · majors.",
            "",
            "<b>/top5</b> · /top",
            "  Top 5 model proba · <b>proba &gt;60%</b> · <b>EV 15–100%</b> · ATP/WTA 250+.",
            "  <b>Bet</b> button under each match.",
            "",
            "<b>/today</b>",
            "  Today's Pick · value bets <b>EV ≥15%</b> · majors + minors.",
            "  Same as web · <b>Bet</b> button under each match.",
            "",
            "<i>/jour · /picks → alias /today</i>",
            "",
            "<b>/cancel</b> · /annuler",
            "  Cancel an in-progress odds entry.",
            "",
            "<b>/br</b>",
            "  Bankroll: available, committed, P/L.",
            "<b>/brstats</b> · /bradv",
            "  Advanced stats: ROI, win rate, form, open bets.",
            "<b>/brset 80</b>",
            "  Starting capital (€).",
            "<b>/brajust +10</b>",
            "  Manual bankroll adjustment.",
            "",
            "<b>/strategy</b> · /strategie",
            "  Pick selection + Kelly / Brier / 15% cap.",
            "",
            "<b>/start</b>",
            "  Welcome message.",
            "",
            "🌅 Auto /top5 ~05:00 Paris.",
        ]
    )


def format_top5_telegram_message(
    picks: list[dict],
    *,
    calendar_date: str,
    pool_size: int = 0,
    snapshot_age_min: float | None = None,
    source: str = "morning",
) -> str:
    lines = [
        f"🎾 <b>{BRAND_NAME}</b> · Top 5 Proba",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        _telegram_pick_criteria_line(),
    ]
    if source == "morning":
        lines.append("🌅 Morning auto-send")
    elif source == "morning-sync":
        lines.append("🔄 Morning resync (fresh odds · web-aligned)")
    elif source == "manual":
        lines.append("📲 On-demand")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        lines.extend(
            [
                "",
                "😴 <i>No matches above proba / EV thresholds today.</i>",
            ]
        )
        if pool_size > 0:
            lines.append(
                tg(
                    f"🔍 {pool_size} match(es) scanned outside thresholds or without odds.",
                    f"🔍 {pool_size} match(s) analysé(s) hors bande ou sans cotes.",
                )
            )
        lines.append("")
        lines.append(
            f"ℹ️ <i>{comms_disclaimer()} Check odds before betting.</i>"
        )
        return "\n".join(lines)

    if picks:
        lines.append(_join_pick_blocks(picks))

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            f"ℹ️ <i>{comms_disclaimer()} Check odds before betting.</i>",
        ]
    )
    return "\n".join(lines).strip()


def _interactive_footer() -> str:
    return "\n".join(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "👆 <b>Bet</b>: button under each match → your odds → Kelly stake → portfolio",
            f"ℹ️ <i>{comms_disclaimer()}</i>",
        ]
    )


def format_top5_interactive_header(
    *,
    calendar_date: str,
    pool_size: int = 0,
    snapshot_age_min: float | None = None,
    source: str = "manual",
    n_picks: int = 0,
) -> str:
    lines = [
        f"🎾 <b>{BRAND_NAME}</b> · Top 5 Proba",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        _telegram_pick_criteria_line(),
    ]
    if source == "manual":
        lines.append("📲 On-demand · <b>Bet</b> mode")
    elif source == "morning":
        lines.append("🌅 Morning auto-send")
    elif source == "morning-sync":
        lines.append("🔄 Morning resync (fresh odds · web-aligned)")
    lines.append(
        tg(
            f"✅ <b>{n_picks}</b> pick(s) · {pool_size} match(es) scanned",
            f"✅ <b>{n_picks}</b> pick(s) · {pool_size} match(s) analysé(s)",
        )
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👇 One message per match — <b>Bet</b> button below")
    return "\n".join(lines).strip()


def format_daily_interactive_header(
    *,
    calendar_date: str,
    pool_size: int = 0,
    snapshot_age_min: float | None = None,
    source: str = "manual",
    n_picks: int = 0,
) -> str:
    lines = [
        f"📋 <b>{BRAND_NAME}</b> · Today's Pick",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        f"{_telegram_pick_criteria_line(ev_max_pct=None)} · <b>Bet</b> mode",
    ]
    if source == "manual":
        lines.append("📲 On-demand")
    lines.append(f"✅ <b>{n_picks}</b> pick(s) · {pool_size} match(es) scanned")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👇 One message per match — <b>Bet</b> button below")
    return "\n".join(lines).strip()


def format_daily_picks_telegram_messages(
    picks: list[dict],
    *,
    calendar_date: str,
    pool_size: int = 0,
    snapshot_age_min: float | None = None,
    source: str = "manual",
) -> list[str]:
    header_lines = [
        f"📋 <b>{BRAND_NAME}</b> · Today's Pick",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        _telegram_pick_criteria_line(ev_max_pct=None),
    ]
    if source == "manual":
        header_lines.append("📲 On-demand")
    elif source == "morning":
        header_lines.append("🌅 Morning auto-send")
    header_lines.append(f"✅ <b>{len(picks)}</b> pick(s) · {pool_size} match(es) scanned")
    header_lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        empty = header_lines + [
            "",
            "😴 <i>No matches scanned for today.</i>",
        ]
        if pool_size > 0:
            empty.append(
                f"🔍 {pool_size} match(es) scanned — no proba / EV picks today."
            )
        empty.extend(
            [
                "",
                f"ℹ️ <i>{comms_disclaimer()} Check odds before betting.</i>",
            ]
        )
        return ["\n".join(empty).strip()]

    body_picks = _join_pick_blocks(picks)
    pick_parts = [body_picks] if body_picks else []
    footer = "\n".join(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            f"ℹ️ <i>{comms_disclaimer()}</i>",
        ]
    )
    header = "\n".join(header_lines).strip()
    return _chunk_telegram_messages([header, *pick_parts, footer])


def format_challenger_daily_picks_telegram_messages(
    picks: list[dict],
    *,
    calendar_date: str,
    pool_size: int = 0,
    snapshot_age_min: float | None = None,
    source: str = "manual",
    ev_min_pct: float = 15.0,
    ev_max_pct: float = 100.0,
) -> list[str]:
    header_lines = [
        f"🏆 <b>{BRAND_NAME}</b> · Challengers (Today)",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        _telegram_pick_criteria_line(ev_max_pct=ev_max_pct),
    ]
    if source == "manual":
        header_lines.append("📲 Demande manuelle")
    header_lines.append(
        f"✅ <b>{len(picks)}</b> pick(s) · {pool_size} Challenger(s) scanné(s)"
    )
    header_lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        empty = header_lines + [
            "",
            "😴 <i>Aucun Challenger au-dessus des seuils proba / EV aujourd'hui.</i>",
        ]
        if pool_size > 0:
            empty.append(
                f"🔍 {pool_size} match(s) Challenger scanné(s) hors bande EV."
            )
        empty.extend(
            [
                "",
                "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
            ]
        )
        return ["\n".join(empty).strip()]

    body_picks = _join_pick_blocks(picks)
    pick_parts = [body_picks] if body_picks else []
    footer = "\n".join(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
        ]
    )
    header = "\n".join(header_lines).strip()
    return _chunk_telegram_messages([header, *pick_parts, footer])


def format_major_daily_picks_telegram_messages(
    picks: list[dict],
    *,
    calendar_date: str,
    pool_size: int = 0,
    snapshot_age_min: float | None = None,
    source: str = "manual",
    ev_min_pct: float = 15.0,
    ev_max_pct: float = 100.0,
) -> list[str]:
    header_lines = [
        f"🎾 <b>{BRAND_NAME}</b> · Majors 250+ (Today)",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        _telegram_pick_criteria_line(ev_max_pct=ev_max_pct),
    ]
    if source == "manual":
        header_lines.append("📲 Demande manuelle")
    header_lines.append(
        f"✅ <b>{len(picks)}</b> pick(s) · {pool_size} major(s) scanné(s)"
    )
    header_lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        empty = header_lines + [
            "",
            "😴 <i>Aucun pick major au-dessus des seuils proba / EV aujourd'hui.</i>",
        ]
        if pool_size > 0:
            empty.append(
                f"🔍 {pool_size} match(s) 250+ scanné(s) hors bande EV."
            )
        empty.extend(
            [
                "",
                "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
            ]
        )
        return ["\n".join(empty).strip()]

    body_picks = _join_pick_blocks(picks)
    pick_parts = [body_picks] if body_picks else []
    footer = "\n".join(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
        ]
    )
    header = "\n".join(header_lines).strip()
    return _chunk_telegram_messages([header, *pick_parts, footer])


def send_telegram_message(
    text: str,
    *,
    token: str,
    chat_id: str,
    disable_web_page_preview: bool = True,
    parse_mode: str | None = "HTML",
    reply_markup: dict | None = None,
) -> dict:
    url = f"{TELEGRAM_API_BASE.format(token=token.strip())}/sendMessage"
    body = str(text or "").strip() or "⚠️ Message vide."
    if len(body) > _TELEGRAM_HARD_LIMIT:
        body = body[: _TELEGRAM_HARD_LIMIT - 12] + "\n… <i>(tronqué)</i>"
    payload: dict = {
        "chat_id": chat_id.strip(),
        "text": body,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code >= 400:
        detail = resp.text[:500]
        if parse_mode == "HTML":
            try:
                return send_telegram_message(
                    _escape_html(body),
                    token=token,
                    chat_id=chat_id,
                    disable_web_page_preview=disable_web_page_preview,
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
            except requests.RequestException:
                pass
        raise RuntimeError(f"Telegram sendMessage HTTP {resp.status_code}: {detail}") from None
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def answer_telegram_callback_query(
    callback_query_id: str,
    *,
    token: str,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    url = f"{TELEGRAM_API_BASE.format(token=token.strip())}/answerCallbackQuery"
    payload: dict = {"callback_query_id": str(callback_query_id)}
    if text:
        payload["text"] = str(text)[:200]
    if show_alert:
        payload["show_alert"] = True
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except requests.RequestException:
        pass


def send_interactive_load_ack(*, token: str, chat_id: str) -> None:
    """Retour immédiat Telegram pendant le chargement des picks (mode Parier)."""
    send_telegram_message(
        tg("⏳ Loading picks…", "⏳ Chargement des picks…"),
        token=token,
        chat_id=str(chat_id).strip(),
    )


def send_interactive_pick_messages(
    picks: list[dict],
    *,
    header_text: str,
    footer_text: str,
    token: str,
    chat_id: str,
    list_kind: str,
    telegram_user_id: str | None = None,
) -> int:
    """Envoie en-tête + un message par pick avec bouton « Parier »."""
    from scripts.telegram_bet_flow import (
        _normalize_pick_row,
        existing_stakes_eur_for_picks,
        format_pick_telegram_card,
        format_user_br_caption,
        get_pick_by_token,
        inline_keyboard_parier,
        register_picks,
    )

    sent = 0
    hdr = str(header_text or "").strip()
    if hdr:
        if telegram_user_id:
            hdr = f"{hdr}\n\n{format_user_br_caption(str(telegram_user_id))}"
        send_telegram_message(hdr, token=token, chat_id=chat_id)
        sent += 1

    if not telegram_user_id:
        raise ValueError("telegram_user_id requis pour le mode Parier interactif")

    ordered = sorted(picks, key=lambda r: int(r.get("rank") or 0))
    tokens = register_picks(
        ordered,
        list_kind=list_kind,
        chat_id=chat_id,
        telegram_user_id=str(telegram_user_id),
    )
    stakes_by_key = existing_stakes_eur_for_picks(
        ordered,
        telegram_user_id=str(telegram_user_id),
    )

    for row, tok in zip(ordered, tokens):
        pick = get_pick_by_token(tok, telegram_user_id=str(telegram_user_id)) or _normalize_pick_row(
            row, list_kind=list_kind
        )
        key = (pick["match_name"], pick["bet_on"])
        already = float(stakes_by_key.get(key) or 0.0)
        body = format_pick_telegram_card(pick, already_stake=already)
        send_telegram_message(
            body,
            token=token,
            chat_id=chat_id,
            reply_markup=inline_keyboard_parier(tok),
        )
        sent += 1

    if str(footer_text or "").strip():
        send_telegram_message(footer_text.strip(), token=token, chat_id=chat_id)
        sent += 1
    return sent


def send_telegram_chat_action(
    *,
    token: str,
    chat_id: str,
    action: str = "typing",
) -> None:
    url = f"{TELEGRAM_API_BASE.format(token=token.strip())}/sendChatAction"
    payload = {"chat_id": chat_id.strip(), "action": action}
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except requests.RequestException:
        pass


def send_telegram_messages(
    texts: list[str],
    *,
    token: str,
    chat_id: str,
) -> int:
    sent = 0
    for i, text in enumerate(texts):
        if not str(text or "").strip():
            continue
        body = text.strip()
        if len(texts) > 1:
            body = f"📄 <i>Partie {i + 1}/{len(texts)}</i>\n\n{body}"
        send_telegram_message(body, token=token, chat_id=chat_id)
        sent += 1
    return sent


def _resolve_pick_limit(raw: str | int | None, *, default: int | None) -> int | None:
    if raw is None:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return None if n <= 0 else n


def _load_live_tracker_jour_context(
    *,
    limit: int | None,
    ev_threshold_pct: float | None = None,
) -> tuple[list[dict], dict, str, int, float | None]:
    """Today's Pick (``/today``) : aligné web Live Tracker — value bets EV ≥ seuil."""
    from scripts.pick_modes import Channel, PickMode, load_picks

    ev_min = 15.0 if ev_threshold_pct is None else float(ev_threshold_pct)
    res = load_picks(
        PickMode.TODAY,
        channel=Channel.TELEGRAM,
        limit=limit,
        ev_min_pct=ev_min,
    )
    return res.picks, res.meta, res.calendar_date, res.pool_n, res.snapshot_age_min


def _load_paris_du_jour_context(
    *,
    limit: int | None = None,
    ev_min_pct: float = 15.0,
    ev_max_pct: float = 100.0,
) -> tuple[list[dict], dict, str, int, float | None]:
    """Alias historique API ``/picks/jour`` → Today's Pick (Live Tracker web)."""
    _ = ev_max_pct
    return _load_live_tracker_jour_context(limit=limit, ev_threshold_pct=ev_min_pct)


def _load_challenger_jour_context(
    *,
    limit: int | None,
    ev_threshold_pct: float | None = None,
    ev_max_pct: float | None = None,
) -> tuple[list[dict], dict, str, int, float | None, float, float]:
    picks, meta, scanned = load_live_tracker_challenger_day_picks(
        ev_threshold_pct=ev_threshold_pct,
        ev_max_pct=ev_max_pct,
    )
    picks = filter_telegram_display_picks(picks)
    cal_day = datetime.now(PARIS_TZ).date().isoformat()
    ev_min = (
        DEFAULT_CHALLENGER_EV_MIN_PCT
        if ev_threshold_pct is None
        else float(ev_threshold_pct)
    )
    ev_max = (
        DEFAULT_CHALLENGER_EV_MAX_PCT if ev_max_pct is None else float(ev_max_pct)
    )
    if limit is not None and limit > 0:
        picks = picks[: int(limit)]
        for i, row in enumerate(picks, start=1):
            row["rank"] = i
    return picks, meta, cal_day, scanned, _snapshot_age_min(meta), ev_min, ev_max


def _load_major_jour_context(
    *,
    limit: int | None,
    ev_threshold_pct: float | None = None,
    ev_max_pct: float | None = None,
) -> tuple[list[dict], dict, str, int, float | None, float, float]:
    picks, meta, scanned = load_live_tracker_major_day_picks(
        ev_threshold_pct=ev_threshold_pct,
        ev_max_pct=ev_max_pct,
    )
    picks = filter_telegram_display_picks(picks)
    cal_day = datetime.now(PARIS_TZ).date().isoformat()
    ev_min = (
        DEFAULT_MAJOR_EV_MIN_PCT
        if ev_threshold_pct is None
        else float(ev_threshold_pct)
    )
    ev_max = DEFAULT_MAJOR_EV_MAX_PCT if ev_max_pct is None else float(ev_max_pct)
    if limit is not None and limit > 0:
        picks = picks[: int(limit)]
        for i, row in enumerate(picks, start=1):
            row["rank"] = i
    return picks, meta, cal_day, scanned, _snapshot_age_min(meta), ev_min, ev_max


def _load_top5_context(
    *,
    limit: int,
    ev_min_pct: float,
    ev_max_pct: float,
) -> tuple[list[dict], dict, str, int, float | None]:
    from scripts.pick_modes import Channel, PickMode, load_picks

    res = load_picks(
        PickMode.TOP5,
        channel=Channel.TELEGRAM,
        limit=limit,
        ev_min_pct=ev_min_pct,
        ev_max_pct=ev_max_pct,
    )
    return res.picks, res.meta, res.calendar_date, res.pool_n, res.snapshot_age_min


def _is_prod_env() -> bool:
    return (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower() == "prod"


def _require_prod_for_send(*, force: bool, dry_run: bool) -> None:
    if dry_run or force:
        return
    if not _is_prod_env():
        raise SystemExit(
            "Envoi Telegram desactive en PREPROD (BETTINGHUD_ENV != prod). "
            "Utiliser --dry-run pour previsualiser, ou deployer sur PROD. "
            "Override explicite : --force (deconseille en local)."
        )


def run_notify(
    *,
    dry_run: bool = False,
    force: bool = False,
    limit: int = 5,
    ev_min_pct: float = 15.0,
    ev_max_pct: float = 100.0,
    chat_id: str | None = None,
    source: str = "morning",
    interactive: bool = False,
    telegram_user_id: str | None = None,
) -> dict:
    _require_prod_for_send(force=force, dry_run=dry_run)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if interactive and not dry_run:
        if not token or not target_chat:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis (.env ou variables d'environnement)."
            )
        send_interactive_load_ack(token=token, chat_id=target_chat)
    picks, _meta, cal_day, pool_n, age_min = _load_top5_context(
        limit=int(limit),
        ev_min_pct=ev_min_pct,
        ev_max_pct=ev_max_pct,
    )

    text = format_top5_telegram_message(
        picks,
        calendar_date=cal_day,
        pool_size=pool_n,
        snapshot_age_min=age_min,
        source=source,
    )

    result = {
        "calendar_date": cal_day,
        "n_matches_today": pool_n,
        "n_picks": len(picks),
        "dry_run": dry_run,
        "message_preview": text[:500],
    }

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(text)
        return result

    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN requis (.env ou variables d'environnement)."
        )

    if chat_id:
        target_chats = [str(chat_id).strip()]
    elif source == "morning":
        from scripts.telegram_access import load_broadcast_chat_ids

        target_chats = load_broadcast_chat_ids()
    else:
        primary = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        target_chats = [primary] if primary else []

    if not target_chats:
        raise SystemExit(
            "Aucun chat Telegram cible (TELEGRAM_CHAT_ID ou utilisateurs validés)."
        )

    if interactive:
        header = format_top5_interactive_header(
            calendar_date=cal_day,
            pool_size=pool_n,
            snapshot_age_min=age_min,
            source=source,
            n_picks=len(picks),
        )
        sent = 0
        for target_chat in target_chats:
            user_id = telegram_user_id or str(target_chat)
            if not picks:
                no_picks_msg = tg("No matches in the EV band today.", "Aucun match dans la bande EV aujourd'hui.")
                send_telegram_message(
                    f"{header}\n\n😴 <i>{no_picks_msg}</i>",
                    token=token,
                    chat_id=target_chat,
                )
                sent += 1
            else:
                sent += send_interactive_pick_messages(
                    picks,
                    header_text=header,
                    footer_text=_interactive_footer(),
                    token=token,
                    chat_id=target_chat,
                    list_kind="top5",
                    telegram_user_id=user_id,
                )
        result["sent"] = sent
        result["chat_ids"] = target_chats
        result["chat_id"] = target_chats[0] if len(target_chats) == 1 else None
    else:
        sent = 0
        for target_chat in target_chats:
            send_telegram_message(text, token=token, chat_id=target_chat)
            sent += 1
        result["sent"] = sent
        result["chat_ids"] = target_chats
        result["chat_id"] = target_chats[0] if len(target_chats) == 1 else None
    return result


def format_1d1p_telegram_message(
    pick: dict | None,
    *,
    calendar_date: str,
    pool_size: int = 0,
    snapshot_age_min: float | None = None,
) -> str:
    lines = [
        f"🎯 <b>{BRAND_NAME}</b> · 1 Day 1 Pick",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        tg(
            "Majors 250+ · best EV-eligible pick per circuit · EV 15–100%",
            "Majeurs 250+ · meilleur candidat EV par circuit · EV 15–100 %",
        ),
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if not pick:
        lines.extend(
            [
                "",
                tg("😴 <i>No value pick today.</i>", "😴 <i>Pas de pick value aujourd'hui.</i>"),
                tg(
                    f"<i>{pool_size} major match(es) scanned in EV band.</i>",
                    f"<i>{pool_size} match(s) majeur(s) dans la bande EV scanné(s).</i>",
                ),
            ]
        )
        return "\n".join(lines)
    bet_on = str(pick.get("fav_player") or pick.get("bet_on") or "?")
    opp = str(pick.get("underdog_player") or pick.get("opponent") or "?")
    p_pct = _pick_proba_pct(pick)
    ev_pct = _pick_ev_pct(pick)
    odd = float(pick.get("odd_fav") or pick.get("odd_book") or 0.0)
    stake_pct = float(pick.get("theoretical_stake_frac") or 0.0) * 100.0
    tour = str(pick.get("tour") or "").upper()
    tourn = str(pick.get("tournament") or "")
    lines.extend(
        [
            "",
            f"<b>{bet_on}</b> vs {opp}",
            tg(
                f"Model proba {p_pct:.1f}% · EV {ev_pct:+.1f}% · @{odd:.2f}",
                f"Proba {p_pct:.1f} % · EV {ev_pct:+.1f} % · @{odd:.2f}",
            ),
            tg(
                f"Theoretical Kelly {stake_pct:.1f}% BR · {tour} · {tourn}",
                f"Mise Kelly th. {stake_pct:.1f} % BR · {tour} · {tourn}",
            ),
            "",
            f"🔗 {PUBLIC_SITE_URL.replace('https://', '')}/1-day-1-pick",
            "",
            f"ℹ️ <i>{comms_disclaimer()}</i>",
        ]
    )
    return "\n".join(lines)


def format_1d1p_result_telegram_message(pick: dict) -> str:
    from scripts.score_display import format_tennis_score_display

    status = str(pick.get("status") or "En cours")
    cal = str(pick.get("calendar_date") or "")[:10]
    bet_on = str(pick.get("fav_player") or pick.get("bet_on") or "?")
    opp = str(pick.get("underdog_player") or pick.get("opponent") or "?")
    score = format_tennis_score_display(pick.get("score_final")) or str(
        pick.get("score_final") or "—"
    )
    try:
        profit_frac = float(pick.get("theoretical_profit") or 0.0)
    except (TypeError, ValueError):
        profit_frac = 0.0
    profit_pct = profit_frac * 100.0

    if status == "Gagné":
        headline = "✅ Won"
    elif status == "Perdu":
        headline = "❌ Lost"
    elif status == "Annulé":
        headline = "⏸️ Void (retirement / walkover)"
    else:
        headline = status

    pnl = (
        f"{profit_pct:+.2f}% BR (theo.)"
        if status != "Annulé"
        else "Stake refunded (0% BR)"
    )

    return "\n".join(
        [
            f"🎯 <b>{BRAND_NAME}</b> · 1D1P result",
            "",
            f"📅 {_format_date_label(cal)}",
            f"<b>{bet_on}</b> vs {opp}",
            headline,
            f"Score: <code>{_escape_html(score)}</code>",
            f"Theo. P/L: <b>{pnl}</b>",
            "",
            f"🔗 {PUBLIC_SITE_URL.replace('https://', '')}/1-day-1-pick",
            f"ℹ️ <i>{comms_disclaimer()}</i>",
        ]
    )


def format_1d1p_interactive_header(
    *,
    calendar_date: str,
    pool_size: int = 0,
    source: str = "manual",
    n_picks: int = 0,
    snapshot_age_min: float | None = None,
) -> str:
    lines = [
        f"🎯 <b>{BRAND_NAME}</b> · 1 Day 1 Pick",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        format_snapshot_freshness_line(snapshot_age_min),
        tg(
            "Majors 250+ · best EV-eligible pick per circuit · EV 15–100% · <b>Bet</b> mode",
            "Majeurs 250+ · meilleur candidat EV par circuit · EV 15–100 % · mode Parier",
        ),
    ]
    if source == "manual":
        lines.append(tg("📲 On-demand", "📲 Demande manuelle"))
    lines.append(
        tg(
            f"✅ <b>{n_picks}</b> pick(s) · {pool_size} major match(es) scanned",
            f"✅ <b>{n_picks}</b> pick(s) · {pool_size} major(s) scanné(s)",
        )
    )
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if n_picks:
        lines.append(tg("👇 Match below — <b>Bet</b> button", "👇 Match ci-dessous — bouton Parier"))
    return "\n".join(lines).strip()


def run_1d1p_notify(
    *,
    dry_run: bool = False,
    force: bool = False,
    chat_id: str | None = None,
    source: str = "manual",
    interactive: bool = False,
    telegram_user_id: str | None = None,
) -> dict:
    _require_prod_for_send(force=force, dry_run=dry_run)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if interactive and not dry_run:
        if not token or not target_chat:
            raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required.")
        if not telegram_user_id:
            raise ValueError("telegram_user_id required for interactive Bet mode")
        send_interactive_load_ack(token=token, chat_id=target_chat)
    from scripts.pick_modes import PickMode, load_picks

    res = load_picks(PickMode.ONE_PICK_ONE_DAY)
    pick = res.pick_today
    text = format_1d1p_telegram_message(
        pick,
        calendar_date=res.calendar_date,
        pool_size=res.pool_n,
        snapshot_age_min=res.snapshot_age_min,
    )
    result = {
        "calendar_date": res.calendar_date,
        "n_picks": 1 if pick else 0,
        "dry_run": dry_run,
        "message_preview": text[:500],
        "source": source,
    }
    if dry_run:
        print(text)
        return result
    if not token or not target_chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required.")

    if interactive:
        if not telegram_user_id:
            raise ValueError("telegram_user_id required for interactive Bet mode")
        header = format_1d1p_interactive_header(
            calendar_date=res.calendar_date,
            pool_size=res.pool_n,
            source=source,
            n_picks=1 if pick else 0,
            snapshot_age_min=res.snapshot_age_min,
        )
        if not pick:
            no_val = tg("No value pick today.", "Pas de pick value aujourd'hui.")
            send_telegram_message(
                f"{header}\n\n😴 <i>{no_val}</i>",
                token=token,
                chat_id=target_chat,
            )
            result["sent"] = 1
        else:
            pick_row = dict(pick)
            pick_row.setdefault("rank", 1)
            result["sent"] = send_interactive_pick_messages(
                [pick_row],
                header_text=header,
                footer_text=_interactive_footer(),
                token=token,
                chat_id=target_chat,
                list_kind="1d1p",
                telegram_user_id=telegram_user_id,
            )
    else:
        send_telegram_message(text, token=token, chat_id=target_chat)
        result["sent"] = 1
    result["chat_id"] = target_chat
    return result


def run_daily_picks_notify(
    *,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    chat_id: str | None = None,
    source: str = "manual",
    ev_threshold_pct: float | None = None,
    interactive: bool = False,
    telegram_user_id: str | None = None,
) -> dict:
    _require_prod_for_send(force=force, dry_run=dry_run)
    if limit is None:
        limit = _resolve_pick_limit(
            os.getenv("TELEGRAM_DAILY_PICKS_LIMIT", "0"),
            default=None,
        )
    if ev_threshold_pct is None:
        raw = os.getenv("TELEGRAM_JOUR_EV_MIN_PCT", "").strip()
        ev_threshold_pct = float(raw) if raw else 15.0
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if interactive and not dry_run:
        if not token or not target_chat:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis (.env ou variables d'environnement)."
            )
        send_interactive_load_ack(token=token, chat_id=target_chat)
    picks, _meta, cal_day, pool_n, age_min = _load_live_tracker_jour_context(
        limit=limit,
        ev_threshold_pct=ev_threshold_pct,
    )
    texts = format_daily_picks_telegram_messages(
        picks,
        calendar_date=cal_day,
        pool_size=pool_n,
        snapshot_age_min=age_min,
        source=source,
    )

    result = {
        "calendar_date": cal_day,
        "n_picks": len(picks),
        "n_messages": len(texts),
        "dry_run": dry_run,
        "message_preview": texts[0][:500] if texts else "",
    }

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        for i, text in enumerate(texts, start=1):
            if len(texts) > 1:
                print(f"--- Partie {i}/{len(texts)} ---")
            print(text)
            if i < len(texts):
                print()
        return result

    if not token or not target_chat:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis (.env ou variables d'environnement)."
        )

    if interactive:
        header = format_daily_interactive_header(
            calendar_date=cal_day,
            pool_size=pool_n,
            snapshot_age_min=age_min,
            source=source,
            n_picks=len(picks),
        )
        if not picks:
            no_pe = tg("No proba / EV picks today.", "Aucun pick proba / EV aujourd'hui.")
            send_telegram_message(
                f"{header}\n\n😴 <i>{no_pe}</i>",
                token=token,
                chat_id=target_chat,
            )
            result["sent"] = 1
        else:
            result["sent"] = send_interactive_pick_messages(
                picks,
                header_text=header,
                footer_text=_interactive_footer(),
                token=token,
                chat_id=target_chat,
                list_kind="jour",
                telegram_user_id=telegram_user_id,
            )
    else:
        result["sent"] = send_telegram_messages(texts, token=token, chat_id=target_chat)
    result["chat_id"] = target_chat
    return result


def run_challenger_daily_picks_notify(
    *,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    chat_id: str | None = None,
    source: str = "manual",
    ev_threshold_pct: float | None = None,
    ev_max_pct: float | None = None,
) -> dict:
    _require_prod_for_send(force=force, dry_run=dry_run)
    if limit is None:
        limit = _resolve_pick_limit(
            os.getenv("TELEGRAM_CHALLENGER_PICKS_LIMIT", "0"),
            default=None,
        )
    picks, _meta, cal_day, pool_n, age_min, ev_min, ev_max = _load_challenger_jour_context(
        limit=limit,
        ev_threshold_pct=ev_threshold_pct,
        ev_max_pct=ev_max_pct,
    )
    texts = format_challenger_daily_picks_telegram_messages(
        picks,
        calendar_date=cal_day,
        pool_size=pool_n,
        snapshot_age_min=age_min,
        source=source,
        ev_min_pct=ev_min,
        ev_max_pct=ev_max,
    )

    result = {
        "calendar_date": cal_day,
        "n_picks": len(picks),
        "n_messages": len(texts),
        "dry_run": dry_run,
        "message_preview": texts[0][:500] if texts else "",
    }

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        for i, text in enumerate(texts, start=1):
            if len(texts) > 1:
                print(f"--- Partie {i}/{len(texts)} ---")
            print(text)
            if i < len(texts):
                print()
        return result

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not target_chat:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis (.env ou variables d'environnement)."
        )

    result["sent"] = send_telegram_messages(texts, token=token, chat_id=target_chat)
    result["chat_id"] = target_chat
    return result


def run_major_daily_picks_notify(
    *,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    chat_id: str | None = None,
    source: str = "manual",
    ev_threshold_pct: float | None = None,
    ev_max_pct: float | None = None,
) -> dict:
    _require_prod_for_send(force=force, dry_run=dry_run)
    if limit is None:
        limit = _resolve_pick_limit(
            os.getenv("TELEGRAM_MAJOR_PICKS_LIMIT", "0"),
            default=None,
        )
    picks, _meta, cal_day, pool_n, age_min, ev_min, ev_max = _load_major_jour_context(
        limit=limit,
        ev_threshold_pct=ev_threshold_pct,
        ev_max_pct=ev_max_pct,
    )
    texts = format_major_daily_picks_telegram_messages(
        picks,
        calendar_date=cal_day,
        pool_size=pool_n,
        snapshot_age_min=age_min,
        source=source,
        ev_min_pct=ev_min,
        ev_max_pct=ev_max,
    )

    result = {
        "calendar_date": cal_day,
        "n_picks": len(picks),
        "n_messages": len(texts),
        "dry_run": dry_run,
        "message_preview": texts[0][:500] if texts else "",
    }

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        for i, text in enumerate(texts, start=1):
            if len(texts) > 1:
                print(f"--- Partie {i}/{len(texts)} ---")
            print(text)
            if i < len(texts):
                print()
        return result

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not target_chat:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis (.env ou variables d'environnement)."
        )

    result["sent"] = send_telegram_messages(texts, token=token, chat_id=target_chat)
    result["chat_id"] = target_chat
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Notification Telegram Top 5 proba")
    ap.add_argument("--dry-run", action="store_true", help="Afficher le message sans envoyer")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Autoriser l'envoi hors PROD (debug uniquement, deconseille).",
    )
    ap.add_argument(
        "--daily",
        action="store_true",
        help="Envoyer tous les picks du jour (pas seulement le top 5).",
    )
    ap.add_argument(
        "--challenger",
        action="store_true",
        help="Apercu /jourchallenger (Challengers, EV 15-100 %%, tri proba).",
    )
    ap.add_argument(
        "--major",
        action="store_true",
        help="Apercu /jourmajor (Majors 250+, EV 15-100 %%, tri proba).",
    )
    ap.add_argument(
        "--strategy",
        action="store_true",
        help="Afficher le message /strategie (apercu, sans envoi).",
    )
    ap.add_argument("--limit", type=int, default=int(os.getenv("TELEGRAM_TOP5_LIMIT", "5")))
    ap.add_argument(
        "--ev-min-pct",
        type=float,
        default=float(os.getenv("TELEGRAM_TOP5_EV_MIN_PCT", "15")),
    )
    ap.add_argument(
        "--ev-max-pct",
        type=float,
        default=float(os.getenv("TELEGRAM_TOP5_EV_MAX_PCT", "100")),
    )
    ap.add_argument("--chat-id", default="", help="Override chat_id (debug)")
    ap.add_argument(
        "--source",
        choices=("morning", "manual", "cli"),
        default="cli",
        help="Contexte du message (morning/manual/cli)",
    )
    args = ap.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    source = str(args.source)
    if source == "cli":
        source = "morning"

    if args.strategy:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
        print(format_bot_strategy_message())
        return 0

    if args.challenger:
        chal_limit = _resolve_pick_limit(
            os.getenv("TELEGRAM_CHALLENGER_PICKS_LIMIT", "0"),
            default=None,
        )
        out = run_challenger_daily_picks_notify(
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            limit=chal_limit,
            chat_id=str(args.chat_id).strip() or None,
            source="manual",
        )
        if not args.dry_run:
            print(
                f"Telegram OK — {out['n_picks']} Challenger pick(s), "
                f"{out.get('sent', 0)} message(s) pour {out['calendar_date']}."
            )
        return 0

    if args.major:
        major_limit = _resolve_pick_limit(
            os.getenv("TELEGRAM_MAJOR_PICKS_LIMIT", "0"),
            default=None,
        )
        out = run_major_daily_picks_notify(
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            limit=major_limit,
            chat_id=str(args.chat_id).strip() or None,
            source="manual",
        )
        if not args.dry_run:
            print(
                f"Telegram OK — {out['n_picks']} Major pick(s), "
                f"{out.get('sent', 0)} message(s) pour {out['calendar_date']}."
            )
        return 0

    if args.daily:
        daily_limit = _resolve_pick_limit(
            os.getenv("TELEGRAM_DAILY_PICKS_LIMIT", "0"),
            default=None,
        )
        out = run_daily_picks_notify(
            dry_run=bool(args.dry_run),
            force=bool(args.force),
            limit=daily_limit,
            chat_id=str(args.chat_id).strip() or None,
            source="manual" if source == "manual" else source,
        )
        if not args.dry_run:
            print(
                f"Telegram OK — {out['n_picks']} pick(s), "
                f"{out.get('sent', 0)} message(s) pour {out['calendar_date']}."
            )
        return 0

    out = run_notify(
        dry_run=bool(args.dry_run),
        force=bool(args.force),
        limit=int(args.limit),
        ev_min_pct=float(args.ev_min_pct),
        ev_max_pct=float(args.ev_max_pct),
        chat_id=str(args.chat_id).strip() or None,
        source=source,
    )
    if not args.dry_run:
        print(f"Telegram OK — {out['n_picks']} pick(s) pour {out['calendar_date']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
