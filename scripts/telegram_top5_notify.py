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
  TELEGRAM_JOUR_EV_MIN_PCT     (defaut 15 = EV strictement > 15 %)
  TELEGRAM_MIN_PROBA_PCT       (defaut 60 = proba modèle strictement > 60 %)
  TELEGRAM_MIN_EV_PCT          (defaut 15 = EV strictement > 15 %, filtre affichage)
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
) -> list[dict]:
    """Filtre commun Telegram : proba modèle et EV strictement au-dessus des seuils."""
    mp = _telegram_min_proba_pct() if min_proba_pct is None else float(min_proba_pct)
    me = _telegram_min_ev_pct() if min_ev_pct is None else float(min_ev_pct)
    kept = [
        p
        for p in picks
        if _pick_proba_pct(p) > mp and _pick_ev_pct(p) > me
    ]
    for rank, row in enumerate(kept, start=1):
        row["rank"] = rank
    return kept


def _telegram_pick_criteria_line(*, ev_max_pct: float | None = 100.0) -> str:
    mp = _telegram_min_proba_pct()
    me = _telegram_min_ev_pct()
    ev_part = (
        f"EV <code>&gt;{me:.0f}%</code> → <code>+{ev_max_pct:.0f}%</code>"
        if ev_max_pct is not None
        else f"EV <code>&gt;{me:.0f}%</code>"
    )
    return f"📊 Proba <code>&gt;{mp:.0f}%</code> · {ev_part} · tri proba modèle ↓"


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
        from scripts.daily_top_proba_store import PARIS_TZ

        today = datetime.now(PARIS_TZ).date()
        base = f"{_WD_FR[d.weekday()]} {d.day} {_MO_FR[d.month - 1]} {d.year}"
        if d == today:
            return f"Aujourd'hui · {base}"
        if d == today + timedelta(days=1):
            return f"Demain · {base}"
        return base
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


def format_bot_invite_start_message() -> str:
    """Premier contact / bot ajouté : inviter à lancer /start."""
    return "\n".join(
        [
            "👋 <b>Bienvenue sur BettingHUD Bot</b>",
            "",
            "Pour <b>demander l'accès</b>, envoie maintenant :",
            "",
            "👉 <code>/start</code>",
            "",
            "💡 Sur Telegram, le bouton bleu <b>« Démarrer »</b> en bas de l'écran "
            "envoie aussi <code>/start</code>.",
            "",
            "Dès que l'admin valide, tu recevras un guide pour utiliser le bot.",
        ]
    )


def format_bot_onboarding_after_approval() -> str:
    """Guide pratique envoyé après approbation admin."""
    return "\n".join(
        [
            "🚀 <b>Par où commencer</b>",
            "",
            "<b>1. Ta bankroll</b>",
            "  <code>/brset 80</code> — fixe ton capital de départ (€)",
            "  <code>/br</code> — voir BR dispo · <code>/brstats</code> — stats détaillées",
            "",
            "<b>2. Les picks du jour</b>",
            "  <code>/top5</code> — Top 5 proba (Paris du jour)",
            "  <code>/jour</code> — value bets (proba &gt;60 %, EV &gt;15 %)",
            "  <code>/jourchallenger</code> · <code>/jourmajor</code> — autres pools",
            "",
            "<b>3. Parier un match</b>",
            "  Sous chaque pick : bouton <b>💰 Parier</b>",
            "  → saisis ta <b>cote réelle</b> → mise Kelly → <b>Confirmer</b>",
            "  (<code>/annuler</code> pour annuler une saisie en cours)",
            "",
            "<b>4. Aller plus loin</b>",
            "  <code>/strategie</code> — comment on sélectionne et mise (Kelly ½ × Brier)",
            "  <code>/help</code> — aide complète de toutes les commandes",
            "",
            "🌅 Chaque matin (~04:00 Paris, après rebuild 02:00) : envoi auto du <code>/top5</code>.",
            "",
            "ℹ️ <i>Information — pas un conseil de pari.</i>",
        ]
    )


def format_bot_welcome_message() -> str:
    return "\n".join(
        [
            "👋 <b>Bienvenue sur BettingHUD Bot</b>",
            "",
            "Je t'envoie chaque matin le <b>Top 5 proba</b> (proba &gt;60 %, EV &gt;15 % → +100 %).",
            "",
            "📌 <b>Commandes</b>",
            "  /top5 — Top 5 proba (Paris du jour, bouton Parier)",
            "  /jour — Picks (proba &gt;60 %, EV &gt;15 %, bouton Parier)",
            "  /jourchallenger · /jourmajor — mêmes filtres proba / EV",
            "  /strategie — Comment on sélectionne et mise",
            "  /br — Bankroll · /brstats — stats avancées",
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
            "• Filtres Telegram : <b>proba modèle &gt; 60 %</b> et <b>EV &gt; 15 %</b>",
            "• Bande EV max +100 % (Top 5 / majors / challengers)",
            "• Tri : proba modèle décroissante → <b>Top 5</b>",
            "",
            "<b>/top5</b> = Top 5 proba (Paris du jour)",
            "<b>/jour</b> = value bets du jour (mêmes seuils proba / EV)",
            "<b>/jourchallenger</b> · <b>/jourmajor</b> = pools dédiés, mêmes filtres",
            "",
            "<b>3. Stratégie de mise (Kelly)</b>",
            "• <b>½ Kelly</b> (mise prudente vs Kelly plein)",
            "• Ajustement <b>Brier</b> : réduction si le segment "
            "(surface / circuit) est moins bien calibré historiquement",
            "• <b>Plafond 15 %</b> de la bankroll disponible par pari",
            "• Mise reco = fraction finale × BR dispo (dashboard / messages)",
            "",
            "<b>4. Parier depuis Telegram</b>",
            "Sous chaque pick <b>/jour</b> ou <b>/top5</b> : bouton <b>Parier</b> → "
            "ta cote → mise Kelly → <b>Confirmer</b> → portefeuille.",
            "",
            "<b>5. En pratique</b>",
            "1️⃣ Consulter /top5 ou /jour",
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
            "<b>/top5</b> · /top",
            "  Top 5 proba · <b>proba &gt;60 %</b> · <b>EV &gt;15 %</b> → +100 %.",
            "  Bouton <b>Parier</b> sous chaque match.",
            "",
            "<b>/jour</b> · /picks · /picksdujour",
            "  Matchs <b>Aujourd'hui</b> · <b>proba &gt;60 %</b> · <b>EV &gt;15 %</b>.",
            "  Bouton <b>Parier</b> sous chaque match.",
            "",
            "<b>/jourchallenger</b>",
            "  Challengers ATP/WTA · <b>proba &gt;60 %</b> · <b>EV &gt;15 %</b> → +100 %.",
            "",
            "<b>/jourmajor</b> · /majors",
            "  Main draw 250+ · <b>proba &gt;60 %</b> · <b>EV &gt;15 %</b> → +100 %.",
            "",
            "<b>/annuler</b>",
            "  Annule une saisie de cote en cours.",
            "",
            "<b>/br</b>",
            "  Bankroll : dispo, engagée, P/L (app + Telegram).",
            "<b>/brstats</b> · /bradv",
            "  Stats avancées : ROI, win rate, forme, 7 j, par source, paris en cours.",
            "<b>/brset 80</b>",
            "  Capital de départ (€).",
            "<b>/brajust +10</b>",
            "  Ajustement manuel de la BR.",
            "",
            "<b>/strategie</b> · /strategy",
            "  Résumé : sélection des picks + Kelly / Brier / plafond 15 %.",
            "",
            "<b>/start</b>",
            "  Message de bienvenue.",
            "",
            "🌅 Envoi automatique /top5 (~04:00 Paris, snapshot prêt depuis 02:00).",
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
        _telegram_pick_criteria_line(),
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
                "😴 <i>Aucun match au-dessus des seuils proba / EV aujourd'hui.</i>",
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

    if picks:
        lines.append(_join_pick_blocks(picks))

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
        ]
    )
    return "\n".join(lines).strip()


def _interactive_footer() -> str:
    return "\n".join(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            "👆 <b>Parier</b> : bouton sous chaque match → ta cote → mise Kelly → portefeuille",
            "ℹ️ <i>Info — pas un conseil de pari. Vérifier les cotes avant mise.</i>",
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
        "🎾 <b>BettingHUD</b> · Top 5 Proba",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        _telegram_pick_criteria_line(),
    ]
    if source == "manual":
        lines.append("📲 Demande manuelle · mode <b>Parier</b>")
    elif source == "morning":
        lines.append("🌅 Envoi matinal automatique")
    if snapshot_age_min is not None:
        lines.append(f"🕐 Snapshot ~{snapshot_age_min:.0f} min")
    lines.append(f"✅ <b>{n_picks}</b> pick(s) · {pool_size} match(s) analysé(s)")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👇 Un message par match — bouton <b>Parier</b> ci-dessous")
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
        "📋 <b>BettingHUD</b> · Live Tracker (Aujourd'hui)",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        f"{_telegram_pick_criteria_line(ev_max_pct=None)} · mode <b>Parier</b>",
    ]
    if source == "manual":
        lines.append("📲 Demande manuelle")
    if snapshot_age_min is not None:
        lines.append(f"🕐 Snapshot ~{snapshot_age_min:.0f} min")
    lines.append(f"✅ <b>{n_picks}</b> match(s) · {pool_size} scanné(s) au total")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👇 Un message par match — bouton <b>Parier</b> ci-dessous")
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
        _telegram_pick_criteria_line(ev_max_pct=None),
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
                f"🔍 {pool_size} match(s) scanné(s) — aucun pick proba / EV aujourd'hui."
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
        _telegram_pick_criteria_line(ev_max_pct=ev_max_pct),
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
        "🎾 <b>BettingHUD</b> · Majors 250+ (Aujourd'hui)",
        "",
        f"📅 {_format_date_label(calendar_date)} · Europe/Paris",
        _telegram_pick_criteria_line(ev_max_pct=ev_max_pct),
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
        existing_stake_eur,
        format_pick_telegram_card,
        format_user_br_caption,
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

    tokens = register_picks(
        picks,
        list_kind=list_kind,
        chat_id=chat_id,
        telegram_user_id=str(telegram_user_id),
    )
    max_workers = max(1, int(os.getenv("TELEGRAM_SEND_PARALLEL", "4")))

    def _send_one(row: dict, tok: str) -> None:
        pick = _normalize_pick_row(row, list_kind=list_kind)
        already = existing_stake_eur(
            pick["match_name"],
            pick["bet_on"],
            telegram_user_id=str(telegram_user_id),
        )
        body = format_pick_telegram_card(pick, already_stake=already)
        send_telegram_message(
            body,
            token=token,
            chat_id=chat_id,
            reply_markup=inline_keyboard_parier(tok),
        )

    if len(picks) <= 1:
        for row, tok in zip(picks, tokens):
            _send_one(row, tok)
            sent += 1
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_send_one, row, tok) for row, tok in zip(picks, tokens)
            ]
            for fut in as_completed(futures):
                fut.result()
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
    picks, meta, scanned = load_live_tracker_day_picks(ev_threshold_pct=ev_threshold_pct)
    picks = filter_telegram_display_picks(picks)
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
    try:
        from scripts.telegram_runtime_cache import get_ml_model, get_today_matches_cached

        matches, meta = get_today_matches_cached()
        ml = get_ml_model()
    except Exception:
        matches, meta = load_today_matches_for_daily_top_proba()
        ml = None
    cal_day = datetime.now(PARIS_TZ).date().isoformat()
    picks = collect_daily_ev_band_picks(
        matches,
        limit=None,
        ev_min_frac=ev_min_pct / 100.0,
        ev_max_frac=ev_max_pct / 100.0,
        calendar_date=cal_day,
        ml=ml,
    )
    picks = filter_telegram_display_picks(picks)
    if limit > 0:
        picks = picks[: int(limit)]
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
    interactive: bool = False,
    telegram_user_id: str | None = None,
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
        target_chat = target_chats[0]
        header = format_top5_interactive_header(
            calendar_date=cal_day,
            pool_size=pool_n,
            snapshot_age_min=age_min,
            source=source,
            n_picks=len(picks),
        )
        if not picks:
            send_telegram_message(
                f"{header}\n\n😴 <i>Aucun match dans la bande EV aujourd'hui.</i>",
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
                list_kind="top5",
                telegram_user_id=telegram_user_id,
            )
        result["chat_id"] = target_chat
    else:
        sent = 0
        for target_chat in target_chats:
            send_telegram_message(text, token=token, chat_id=target_chat)
            sent += 1
        result["sent"] = sent
        result["chat_ids"] = target_chats
        result["chat_id"] = target_chats[0] if len(target_chats) == 1 else None
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

    if interactive:
        header = format_daily_interactive_header(
            calendar_date=cal_day,
            pool_size=pool_n,
            snapshot_age_min=age_min,
            source=source,
            n_picks=len(picks),
        )
        if not picks:
            send_telegram_message(
                f"{header}\n\n😴 <i>Aucun pick proba / EV aujourd'hui.</i>",
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
