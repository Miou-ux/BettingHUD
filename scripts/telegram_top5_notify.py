#!/usr/bin/env python3
"""Envoie des notifications Telegram BettingHUD (Top 5 et /jour Live Tracker).

Documentation : docs/TELEGRAM_TOP5.md

Variables d'environnement :
  TELEGRAM_BOT_TOKEN   — token du bot (@BotFather)
  TELEGRAM_CHAT_ID     — id du chat / canal / groupe

Optionnel :
  TELEGRAM_TOP5_LIMIT          (defaut 5)
  TELEGRAM_TOP5_EV_MIN_PCT     (defaut 15)
  TELEGRAM_TOP5_EV_MAX_PCT     (defaut 100)
  TELEGRAM_DAILY_PICKS_LIMIT   (defaut 0 = tous les picks EV+ /jour)
  TELEGRAM_JOUR_EV_MIN_PCT     (defaut 0 = tout EV strictement positif)
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
from datetime import date, datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

import requests

from scripts.daily_top_proba_store import (
    collect_daily_ev_band_picks,
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


def _escape_html(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _format_date_label(calendar_date: str) -> str:
    try:
        d = date.fromisoformat(str(calendar_date)[:10])
        return f"{_WD_FR[d.weekday()]} {d.day} {_MO_FR[d.month - 1]} {d.year}"
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
    return lines


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
    built = meta.get("built_at") or meta.get("mtime")
    if not built:
        return None
    try:
        return max(0.0, (datetime.now(PARIS_TZ).timestamp() - float(built)) / 60.0)
    except (TypeError, ValueError):
        return None


def _count_today_pool(matches: list) -> int:
    from scripts.daily_top_proba_store import _match_favorite_metrics, is_today_paris_match

    pool_n = 0
    for m in matches:
        if not is_today_paris_match(m):
            continue
        if _match_favorite_metrics(m):
            pool_n += 1
    return pool_n


def format_bot_welcome_message() -> str:
    return "\n".join(
        [
            "👋 <b>Bienvenue sur BettingHUD Bot</b>",
            "",
            "Je t'envoie chaque matin le <b>Top 5 proba</b> (EV +15 % → +100 %).",
            "",
            "📌 <b>Commandes</b>",
            "  /jour — Picks du jour (EV+ uniquement)",
            "  /jourchallenger — Challengers EV +15 % → +100 %, tri proba",
            "  /jourmajor — Majors 250+ EV +15 % → +100 %, tri proba",
            "  /top5 — Top 5 proba (résumé)",
            "  /strategie — Comment on sélectionne et mise",
            "  /help — Aide",
            "",
            "🌐 Dashboard : serveur PROD BettingHUD",
        ]
    )


def format_bot_strategy_message() -> str:
    """Résumé synthétique stratégie BettingHUD + mise (commande /strategie)."""
    return "\n".join(
        [
            "📖 <b>BettingHUD — Stratégie</b>",
            "",
            "<b>1. Principe</b>",
            "Modèle ML tennis (ATP/WTA) : probabilité de victoire calibrée "
            "à partir du classement, de la forme, du contexte (surface, tournoi…).",
            "On ne retient un pari que si le modèle voit un <b>edge</b> vs la cote book "
            "(EV = espérance de gain &gt; 0).",
            "",
            "<b>2. Sélection des picks</b>",
            "• Matchs du <b>jour</b> (Europe/Paris), cotes valides",
            "• <b>Favori modèle</b> = joueur avec la plus forte proba",
            "• Filtre principal : <b>EV favori +15 % → +100 %</b>",
            "• Tri : proba modèle décroissante → <b>Top 5</b>",
            "",
            "<b>/top5</b> = ce Top 5 (Paris du jour)",
            "<b>/jour</b> = value bets du jour (EV &gt; 0, tri proba modèle ↓)",
            "<b>/jourchallenger</b> = Challengers du jour, EV +15 % → +100 %, tri proba",
            "<b>/jourmajor</b> = Tournois 250+ du jour, EV +15 % → +100 %, tri proba",
            "",
            "<b>3. Stratégie de mise (Kelly)</b>",
            "• <b>½ Kelly</b> (mise prudente vs Kelly plein)",
            "• Ajustement <b>Brier</b> : réduction si le segment "
            "(surface / circuit) est moins bien calibré historiquement",
            "• <b>Plafond 15 %</b> de la bankroll disponible par pari",
            "• Mise reco = fraction finale × BR dispo (dashboard / messages)",
            "",
            "<b>4. En pratique</b>",
            "1️⃣ Consulter /top5 ou le dashboard",
            "2️⃣ Vérifier la <b>cote réelle</b> chez le bookmaker",
            "3️⃣ Miser au plus la reco Kelly (ou moins si tu veux)",
            "",
            "ℹ️ <i>Information — pas un conseil de pari. "
            "Les performances passées ne garantissent pas les résultats futurs.</i>",
        ]
    )


def format_bot_help_message() -> str:
    return "\n".join(
        [
            "ℹ️ <b>Aide BettingHUD Bot</b>",
            "",
            "<b>/jour</b> · /picks · /picksdujour",
            "  Matchs <b>Aujourd'hui</b> · value bets EV+ (tri proba modèle ↓).",
            "",
            "<b>/jourchallenger</b>",
            "  Tournois <b>Challenger</b> ATP/WTA du jour.",
            "  EV favori <b>+15 % → +100 %</b> · tri <b>proba modèle</b> ↓",
            "",
            "<b>/jourmajor</b> · /majors",
            "  Tournois <b>main draw 250+</b> (ATP/WTA) du jour.",
            "  EV <b>+15 % → +100 %</b> · tri <b>proba modèle</b> ↓",
            "",
            "<b>/top5</b> · /top",
            "  Top 5 proba du jour (EV favori +15 % → +100 %).",
            "",
            "<b>/strategie</b> · /strategy",
            "  Résumé : sélection des picks + Kelly / Brier / plafond 15 %.",
            "",
            "<b>/start</b>",
            "  Message de bienvenue.",
            "",
            "🌅 Envoi automatique /top5 après le pipeline matin (~02:00 Paris).",
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
        "🎾 <b>BettingHUD</b> · Top 5 Proba",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        "⚡ EV <code>+15%</code> → <code>+100%</code> · tri proba modèle",
    ]
    if source == "morning":
        lines.append("🌅 Envoi matinal automatique")
    elif source == "manual":
        lines.append("📲 Demande manuelle")
    if snapshot_age_min is not None:
        lines.append(f"🕐 Snapshot ~{snapshot_age_min:.0f} min")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        lines.extend(
            [
                "",
                "😴 <i>Aucun match dans la bande EV aujourd'hui.</i>",
            ]
        )
        if pool_size > 0:
            lines.append(
                f"🔍 {pool_size} match(s) analysé(s) hors bande ou sans cotes."
            )
        lines.append("")
        lines.append(
            "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>"
        )
        return "\n".join(lines)

    for row in picks:
        lines.extend(_format_pick_block(row))

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
        ]
    )
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
        "📋 <b>BettingHUD</b> · Live Tracker (Aujourd'hui)",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        "🎯 Live Tracker · value bets <b>EV+</b> · tri <b>proba modèle</b> ↓",
    ]
    if source == "manual":
        header_lines.append("📲 Demande manuelle")
    elif source == "morning":
        header_lines.append("🌅 Envoi matinal automatique")
    if snapshot_age_min is not None:
        header_lines.append(f"🕐 Snapshot ~{snapshot_age_min:.0f} min")
    header_lines.append(f"✅ <b>{len(picks)}</b> match(s) · {pool_size} scanné(s) au total")
    header_lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        empty = header_lines + [
            "",
            "😴 <i>Aucun match scanné pour aujourd'hui.</i>",
        ]
        if pool_size > 0:
            empty.append(
                f"🔍 {pool_size} match(s) scanné(s) — aucun pari EV+ aujourd'hui."
            )
        empty.extend(
            [
                "",
                "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
            ]
        )
        return ["\n".join(empty).strip()]

    pick_parts = [
        "\n".join(_format_pick_block(row)).strip()
        for row in picks
        if _format_pick_block(row)
    ]
    footer = "\n".join(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
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
        "🏆 <b>BettingHUD</b> · Challengers (Aujourd'hui)",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        f"⚡ EV <code>+{ev_min_pct:.0f}%</code> → <code>+{ev_max_pct:.0f}%</code> · tri proba modèle ↓",
    ]
    if source == "manual":
        header_lines.append("📲 Demande manuelle")
    if snapshot_age_min is not None:
        header_lines.append(f"🕐 Snapshot ~{snapshot_age_min:.0f} min")
    header_lines.append(
        f"✅ <b>{len(picks)}</b> pick(s) · {pool_size} Challenger(s) scanné(s)"
    )
    header_lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        empty = header_lines + [
            "",
            "😴 <i>Aucun Challenger EV+ dans la bande aujourd'hui.</i>",
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

    pick_parts = [
        "\n".join(_format_pick_block(row)).strip()
        for row in picks
        if _format_pick_block(row)
    ]
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
        "🎾 <b>BettingHUD</b> · Majors 250+ (Aujourd'hui)",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        f"⚡ EV <code>+{ev_min_pct:.0f}%</code> → <code>+{ev_max_pct:.0f}%</code> · tri proba modèle ↓",
    ]
    if source == "manual":
        header_lines.append("📲 Demande manuelle")
    if snapshot_age_min is not None:
        header_lines.append(f"🕐 Snapshot ~{snapshot_age_min:.0f} min")
    header_lines.append(
        f"✅ <b>{len(picks)}</b> pick(s) · {pool_size} major(s) scanné(s)"
    )
    header_lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not picks:
        empty = header_lines + [
            "",
            "😴 <i>Aucun pick major EV+ dans la bande aujourd'hui.</i>",
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

    pick_parts = [
        "\n".join(_format_pick_block(row)).strip()
        for row in picks
        if _format_pick_block(row)
    ]
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
                )
            except requests.RequestException:
                pass
        raise RuntimeError(f"Telegram sendMessage HTTP {resp.status_code}: {detail}") from None
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


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
    picks, meta, scanned = load_live_tracker_day_picks(ev_threshold_pct=ev_threshold_pct)
    cal_day = datetime.now(PARIS_TZ).date().isoformat()
    if limit is not None and limit > 0:
        picks = picks[: int(limit)]
        for i, row in enumerate(picks, start=1):
            row["rank"] = i
    return picks, meta, cal_day, scanned, _snapshot_age_min(meta)


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
    matches, meta = load_today_matches_for_daily_top_proba()
    cal_day = datetime.now(PARIS_TZ).date().isoformat()
    picks = collect_daily_ev_band_picks(
        matches,
        limit=int(limit),
        ev_min_frac=ev_min_pct / 100.0,
        ev_max_frac=ev_max_pct / 100.0,
        calendar_date=cal_day,
    )
    return picks, meta, cal_day, _count_today_pool(matches), _snapshot_age_min(meta)


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
) -> dict:
    _require_prod_for_send(force=force, dry_run=dry_run)
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

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not target_chat:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis (.env ou variables d'environnement)."
        )

    send_telegram_message(text, token=token, chat_id=target_chat)
    result["sent"] = True
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
) -> dict:
    _require_prod_for_send(force=force, dry_run=dry_run)
    if limit is None:
        limit = _resolve_pick_limit(
            os.getenv("TELEGRAM_DAILY_PICKS_LIMIT", "0"),
            default=None,
        )
    if ev_threshold_pct is None:
        raw = os.getenv("TELEGRAM_JOUR_EV_MIN_PCT", "").strip()
        ev_threshold_pct = float(raw) if raw else 0.0
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

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not token or not target_chat:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID requis (.env ou variables d'environnement)."
        )

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
