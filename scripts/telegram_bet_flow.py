#!/usr/bin/env python3
"""Flux interactif Telegram : parier un pick (/jour, /top5) avec cote perso + Kelly."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from scripts.bets_db import (
    _algo_kelly_stake_frac,
    compute_telegram_user_bankroll_eur,
    compute_telegram_user_br_advanced_stats,
    normalize_schedule_date,
    save_bet_enriched,
    set_telegram_user_manual_adjust_eur,
    set_telegram_user_start_br,
)

PARIS_TZ = ZoneInfo("Europe/Paris")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(ROOT, "data", "cache", "telegram_pick_registry.json")
SESSION_PATH = os.path.join(ROOT, "data", "cache", "telegram_bet_sessions.json")
REGISTRY_TTL_SEC = 86400
TOKEN_LEN = 10

_CB_BET = "bp:"  # démarrer pari
_CB_CONFIRM = "by:"  # confirmer
_CB_CANCEL = "bn:"  # annuler
_CB_CUSTOM_STAKE = "bs:"  # saisir une autre mise


def _escape_html(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _load_json(path: str) -> dict:
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _purge_registry(registry: dict) -> dict:
    now = time.time()
    out: dict = {}
    for tok, row in registry.items():
        try:
            ts = float(row.get("created_ts") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if now - ts <= REGISTRY_TTL_SEC:
            out[str(tok)] = row
    return out


def _normalize_pick_row(row: dict, *, list_kind: str) -> dict:
    p1 = str(row.get("player1") or "").strip()
    p2 = str(row.get("player2") or "").strip()
    bet_on = str(row.get("bet_on") or row.get("fav_player") or "").strip()
    opponent = str(row.get("opponent") or row.get("underdog_player") or "").strip()
    match_name = str(row.get("match_name") or "").strip()
    if not match_name and p1 and p2:
        match_name = f"{p1} vs {p2}"
    cal = str(row.get("calendar_date") or row.get("match_date") or "")[:10]
    if not cal:
        cal = datetime.now(PARIS_TZ).date().isoformat()
    p_model = row.get("p_model_fav")
    if p_model is None:
        p_model = (float(row.get("p_model_pct") or 0) / 100.0) or None
    try:
        p_model_f = float(p_model) if p_model is not None else None
    except (TypeError, ValueError):
        p_model_f = None
    try:
        seg_brier = float(row.get("segment_brier") or 0.1741)
    except (TypeError, ValueError):
        seg_brier = 0.1741
    try:
        odd_book = float(row.get("odd_book") or row.get("odd_fav") or 0)
    except (TypeError, ValueError):
        odd_book = 0.0
    return {
        "list_kind": list_kind,
        "rank": int(row.get("rank") or 0),
        "bet_on": bet_on,
        "opponent": opponent,
        "match_name": match_name,
        "player1": p1 or None,
        "player2": p2 or None,
        "tour": str(row.get("tour") or "").upper() or None,
        "tournament": row.get("tournament"),
        "surface": row.get("surface"),
        "match_time": row.get("match_time"),
        "match_date": cal,
        "calendar_date": cal,
        "match_id": row.get("match_id"),
        "segment_key": row.get("segment_key"),
        "segment_brier": seg_brier,
        "p_model_fav": p_model_f,
        "odd_book": odd_book,
        "ev_pct": float(row.get("ev_pct") or row.get("ev_fav_pct") or 0),
    }


def telegram_user_id_from_update(
    msg: dict | None = None,
    cq: dict | None = None,
) -> str | None:
    if cq:
        user = (cq.get("from") or {}) if isinstance(cq, dict) else {}
        uid = user.get("id")
        if uid is not None:
            return str(uid).strip()
    if msg:
        user = (msg.get("from") or {}) if isinstance(msg, dict) else {}
        uid = user.get("id")
        if uid is not None:
            return str(uid).strip()
    return None


def register_picks(
    picks: list[dict],
    *,
    list_kind: str,
    chat_id: str,
    telegram_user_id: str,
) -> list[str]:
    """Enregistre les picks ; retourne un token par pick (même ordre)."""
    registry = _purge_registry(_load_json(REGISTRY_PATH))
    tokens: list[str] = []
    now = time.time()
    uid = str(telegram_user_id).strip()
    for row in picks:
        norm = _normalize_pick_row(row, list_kind=list_kind)
        tok = secrets.token_urlsafe(8)[:TOKEN_LEN]
        while tok in registry:
            tok = secrets.token_urlsafe(8)[:TOKEN_LEN]
        registry[tok] = {
            "created_ts": now,
            "chat_id": str(chat_id),
            "telegram_user_id": uid,
            "pick": norm,
        }
        tokens.append(tok)
    _save_json(REGISTRY_PATH, registry)
    return tokens


def get_pick_by_token(token: str, *, telegram_user_id: str | None = None) -> dict | None:
    registry = _purge_registry(_load_json(REGISTRY_PATH))
    row = registry.get(str(token))
    if not row:
        return None
    if telegram_user_id is not None:
        owner = str(row.get("telegram_user_id") or "").strip()
        if owner and owner != str(telegram_user_id).strip():
            return None
    pick = row.get("pick")
    return dict(pick) if isinstance(pick, dict) else None


def _session_key(telegram_user_id: str) -> str:
    return f"u{str(telegram_user_id).strip()}"


def _get_session(telegram_user_id: str) -> dict | None:
    data = _load_json(SESSION_PATH)
    row = data.get(_session_key(telegram_user_id))
    return dict(row) if isinstance(row, dict) else None


def _set_session(telegram_user_id: str, session: dict | None) -> None:
    data = _load_json(SESSION_PATH)
    key = _session_key(telegram_user_id)
    if session is None:
        data.pop(key, None)
    else:
        data[key] = session
    _save_json(SESSION_PATH, data)


def get_telegram_user_bankroll_snapshot(
    telegram_user_id: str,
    db_path: str = "data/bettinghud.db",
) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return compute_telegram_user_bankroll_eur(conn, str(telegram_user_id))
    finally:
        conn.close()


def get_telegram_user_available_eur(
    telegram_user_id: str,
    db_path: str = "data/bettinghud.db",
) -> float:
    snap = get_telegram_user_bankroll_snapshot(telegram_user_id, db_path=db_path)
    return float(snap.get("available_eur") or 0.0)


def format_telegram_user_br_message(
    telegram_user_id: str,
    *,
    username: str | None = None,
) -> str:
    snap = get_telegram_user_bankroll_snapshot(telegram_user_id)
    label = _escape_html(username) if username else f"id <code>{_escape_html(telegram_user_id)}</code>"
    pl = float(snap.get("settled_profit_eur") or 0.0)
    pl_sign = "+" if pl >= 0 else ""
    return "\n".join(
        [
            f"💼 <b>Ta bankroll Telegram</b> · {label}",
            "",
            f"Capital de départ : <b>{snap['start_eur']:.2f} €</b>",
            f"BR disponible : <b>{snap['available_eur']:.2f} €</b>",
            f"Engagé (en cours) : <b>{snap['committed_open_eur']:.2f} €</b>",
            f"Capital total : <b>{snap['equity_eur']:.2f} €</b>",
            f"P/L réglé : <b>{pl_sign}{pl:.2f} €</b>",
            (
                f"Ajustement manuel : <b>{snap['manual_adjust_eur']:+.2f} €</b>"
                if abs(float(snap.get("manual_adjust_eur") or 0)) > 1e-6
                else ""
            ),
            "",
            "<i>Kelly : tous tes paris rattachés (dashboard + Telegram).</i>",
            "Modifier le capital de départ : <code>/brset 80</code>",
            "Ajustement ponctuel : <code>/brajust +10</code> ou <code>/brajust -5</code>",
            "Stats détaillées : <code>/brstats</code>",
        ]
    ).replace("\n\n\n", "\n\n")


def _fmt_pct(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "—"
    if signed:
        sign = "+" if value >= 0 else ""
        return f"{sign}{value:.1f} %"
    return f"{value:.1f} %"


def _fmt_eur(value: float, *, signed: bool = False) -> str:
    if signed:
        sign = "+" if value >= 0 else ""
        return f"{sign}{value:.2f} €"
    return f"{value:.2f} €"


def format_telegram_user_br_advanced_message(
    telegram_user_id: str,
    *,
    username: str | None = None,
) -> str:
    conn = sqlite3.connect("data/bettinghud.db")
    try:
        stats = compute_telegram_user_br_advanced_stats(conn, str(telegram_user_id))
    finally:
        conn.close()

    label = _escape_html(username) if username else f"id <code>{_escape_html(telegram_user_id)}</code>"
    pl = float(stats.get("settled_profit_eur") or 0.0)
    lines: list[str] = [
        f"📊 <b>Bankroll avancée</b> · {label}",
        "",
        "<b>💼 Synthèse</b>",
        f"Capital de départ : <b>{stats['start_eur']:.2f} €</b>",
        f"BR disponible : <b>{stats['available_eur']:.2f} €</b>",
        f"Engagé : <b>{stats['committed_open_eur']:.2f} €</b>"
        + (
            f" ({int(stats.get('open_count') or 0)} pari(s) · {_fmt_pct(stats.get('exposure_pct'))} du capital)"
            if stats.get("open_count")
            else ""
        ),
        f"Capital total : <b>{stats['equity_eur']:.2f} €</b>",
        f"P/L réglé : <b>{_fmt_eur(pl, signed=True)}</b>",
    ]
    if stats.get("growth_on_start_pct") is not None:
        lines.append(
            f"Vs capital départ : <b>{_fmt_pct(stats['growth_on_start_pct'], signed=True)}</b>"
        )
    if abs(float(stats.get("manual_adjust_eur") or 0)) > 1e-6:
        lines.append(f"Ajustement manuel : <b>{stats['manual_adjust_eur']:+.2f} €</b>")

    lines.extend(["", "<b>📈 Performance (paris réglés)</b>"])
    w, l, v = int(stats.get("wins") or 0), int(stats.get("losses") or 0), int(stats.get("voids") or 0)
    lines.append(f"Volume : <b>{w + l + v}</b> paris · <b>{w}G</b> · <b>{l}P</b>" + (f" · <b>{v}A</b>" if v else ""))
    if stats.get("win_rate_pct") is not None:
        lines.append(f"Win rate : <b>{stats['win_rate_pct']:.1f} %</b>")
    lines.append(
        f"Mises réglées : <b>{float(stats.get('settled_stake_eur') or 0):.2f} €</b> · "
        f"ROI : <b>{_fmt_pct(stats.get('roi_pct'), signed=True)}</b>"
    )
    if stats.get("avg_odds_won") is not None or stats.get("avg_odds_lost") is not None:
        ow = stats.get("avg_odds_won")
        ol = stats.get("avg_odds_lost")
        parts = []
        if ow is not None:
            parts.append(f"gagnés @ {ow:.2f}")
        if ol is not None:
            parts.append(f"perdus @ {ol:.2f}")
        lines.append(f"Cote moy. : {' · '.join(parts)}")

    form = list(stats.get("recent_form") or [])
    if form:
        icons = "".join("✅" if s == "Gagné" else "❌" for s in form)
        lines.append(f"Forme (10 dern.) : {icons}")

    by_source = list(stats.get("by_source") or [])
    if by_source:
        lines.extend(["", "<b>📦 Par source</b>"])
        for row in by_source[:8]:
            pl_s = float(row.get("settled_profit_eur") or 0.0)
            open_s = float(row.get("open_stake_eur") or 0.0)
            extra = f" · engagé {open_s:.2f} €" if open_s > 0 else ""
            lines.append(
                f"• <b>{_escape_html(row.get('label') or row.get('source'))}</b> : "
                f"{int(row.get('count') or 0)} · {_fmt_eur(pl_s, signed=True)}{extra}"
            )

    days = list(stats.get("last_7_days") or [])
    if days:
        lines.extend(["", "<b>📅 7 derniers jours</b>"])
        for d in days[:7]:
            pl_d = float(d.get("profit_eur") or 0.0)
            lines.append(
                f"• <code>{_escape_html(d.get('date') or '')}</code> : "
                f"<b>{_fmt_eur(pl_d, signed=True)}</b> "
                f"({int(d.get('count') or 0)} · {float(d.get('stake_eur') or 0):.2f} € mises)"
            )

    open_bets = list(stats.get("open_bets") or [])
    if open_bets:
        lines.extend(["", "<b>🎯 En cours (top mises)</b>"])
        for ob in open_bets[:5]:
            match = _escape_html(str(ob.get("match_name") or "—")[:48])
            player = _escape_html(str(ob.get("bet_on") or ""))
            odd = float(ob.get("odds") or 0.0)
            stake = float(ob.get("stake_eur") or 0.0)
            lines.append(f"• {match} — <b>{player}</b> @ {odd:.2f} · <b>{stake:.2f} €</b>")

    lines.extend(
        [
            "",
            "<i>Tous les paris rattachés à ton compte (app + Telegram).</i>",
            "<code>/br</code> · <code>/brset</code> · <code>/brajust</code>",
        ]
    )
    return "\n".join(lines)


def parse_brset_amount(text: str) -> float | None:
    parts = (text or "").strip().split()
    if len(parts) < 2:
        return None
    try:
        val = float(parts[1].replace(",", "."))
    except ValueError:
        return None
    if val < 0:
        return None
    return round(val, 2)


def parse_brajust_delta(text: str) -> float | None:
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    raw = parts[1].strip().replace(",", ".").replace("€", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def apply_telegram_brset(telegram_user_id: str, amount: float) -> None:
    conn = sqlite3.connect("data/bettinghud.db")
    try:
        set_telegram_user_start_br(conn, telegram_user_id, float(amount))
    finally:
        conn.close()


def apply_telegram_brajust(telegram_user_id: str, delta: float) -> None:
    conn = sqlite3.connect("data/bettinghud.db")
    try:
        from scripts.bets_db import get_telegram_user_manual_adjust_eur

        cur = get_telegram_user_manual_adjust_eur(conn, telegram_user_id) + float(delta)
        set_telegram_user_manual_adjust_eur(conn, telegram_user_id, cur)
    finally:
        conn.close()


def format_user_br_caption(telegram_user_id: str) -> str:
    avail = get_telegram_user_available_eur(telegram_user_id)
    return f"💼 Ta BR Telegram : <b>{avail:.2f} €</b> dispo · <code>/br</code> pour le détail"


def kelly_stake_for_pick(
    pick: dict,
    *,
    custom_odd: float,
    bankroll_avail: float,
) -> tuple[float, float]:
    """Retourne (stake_eur, stake_pct_br)."""
    p_model = float(pick.get("p_model_fav") or 0)
    seg = float(pick.get("segment_brier") or 0.1741)
    frac = _algo_kelly_stake_frac(p_model, custom_odd, seg)
    stake = max(0.0, float(bankroll_avail) * frac)
    return round(stake, 2), round(frac * 100.0, 2)


def existing_stake_eur(
    match_name: str,
    bet_on: str,
    *,
    telegram_user_id: str,
    db_path: str = "data/bettinghud.db",
) -> float:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(stake), 0)
            FROM user_bets
            WHERE match_name = ? AND bet_on = ?
              AND telegram_user_id = ?
            """,
            (match_name, bet_on, str(telegram_user_id).strip()),
        ).fetchone()
        return float(row[0] or 0.0) if row else 0.0
    finally:
        conn.close()


def parse_user_stake_eur(text: str) -> float | None:
    t = (text or "").strip().lower().replace(",", ".").replace("€", "").replace("eur", "").strip()
    if not t:
        return None
    m = re.match(r"^(\d+(?:\.\d{1,2})?)\s*$", t)
    if not m:
        return None
    try:
        stake = float(m.group(1))
    except ValueError:
        return None
    if stake < 0.01 or stake > 50_000.0:
        return None
    return round(stake, 2)


def parse_user_odd(text: str) -> float | None:
    t = (text or "").strip().replace(",", ".")
    if not t:
        return None
    m = re.match(r"^@?\s*(\d+(?:\.\d+)?)\s*$", t)
    if not m:
        return None
    try:
        o = float(m.group(1))
    except ValueError:
        return None
    if o < 1.01 or o > 100.0:
        return None
    return round(o, 4)


def callback_token(data: str, prefix: str) -> str | None:
    d = str(data or "")
    if not d.startswith(prefix):
        return None
    tok = d[len(prefix) :].strip()
    return tok if tok else None


def inline_keyboard_parier(token: str) -> dict:
    return {
        "inline_keyboard": [
            [{"text": "💰 Parier", "callback_data": f"{_CB_BET}{token}"}],
        ]
    }


def inline_keyboard_confirm(token: str, stake_eur: float) -> dict:
    try:
        stake = round(float(stake_eur), 2)
    except (TypeError, ValueError):
        stake = 0.0
    confirm_label = f"✅ {stake:.2f} €" if stake >= 0.01 else "✅ Confirmer"
    if len(confirm_label) > 64:
        confirm_label = "✅ Confirmer"
    return {
        "inline_keyboard": [
            [{"text": confirm_label, "callback_data": f"{_CB_CONFIRM}{token}"}],
            [
                {"text": "✏️ Autre mise", "callback_data": f"{_CB_CUSTOM_STAKE}{token}"},
                {"text": "❌ Annuler", "callback_data": f"{_CB_CANCEL}{token}"},
            ],
        ]
    }


def _present_confirm_screen(
    chat_id: str,
    pick: dict,
    *,
    odd: float,
    stake_eur: float,
    stake_pct: float,
    br_avail: float,
    kelly_eur: float,
    token: str,
    send_message,
) -> None:
    bet_on = _escape_html(pick.get("bet_on") or "?")
    custom = abs(float(stake_eur) - float(kelly_eur)) > 0.02
    stake_line = (
        f"Mise : <b>{stake_eur:.2f} €</b> <i>(personnalisée)</i>"
        if custom
        else f"Mise Kelly : <b>{stake_eur:.2f} €</b> ({stake_pct:.1f}% BR)"
    )
    send_message(
        chat_id,
        (
            f"💰 <b>Récap</b> — {bet_on}\n"
            f"Cote : <b>{odd:.2f}</b>\n"
            f"{stake_line}\n"
            f"BR dispo : <b>{br_avail:.2f} €</b>\n\n"
            "Confirmer, modifier la mise (<b>✏️ Autre mise</b>) ou envoyer un montant en € "
            "(ex. <code>2.50</code>)."
        ),
        reply_markup=inline_keyboard_confirm(token, stake_eur),
    )


def format_pick_telegram_card(pick: dict, *, already_stake: float = 0.0) -> str:
    r = int(pick.get("rank") or 0)
    icons = {1: "🥇", 2: "🥈", 3: "🥉", 4: "4️⃣", 5: "5️⃣"}
    icon = icons.get(r, f"{r}." if r else "🎾")
    bet_on = _escape_html(pick.get("bet_on") or "?")
    opp = _escape_html(pick.get("opponent") or "?")
    tour = _escape_html(pick.get("tour") or "")
    tourn = _escape_html(pick.get("tournament") or "")
    when = _escape_html(pick.get("match_time") or "")
    p_pct = float(pick.get("p_model_fav") or 0) * 100.0
    ev_pct = float(pick.get("ev_pct") or 0)
    odd = float(pick.get("odd_book") or 0)
    lines = [f"{icon} <b>{bet_on}</b> vs {opp}"]
    meta: list[str] = []
    if tour:
        meta.append(tour)
    if tourn:
        meta.append(tourn)
    if when:
        meta.append(when)
    if meta:
        lines.append(" · ".join(meta))
    lines.append(
        f"📊 Proba <b>{p_pct:.1f}%</b> · EV <b>{ev_pct:+.1f}%</b> · Book <b>@{odd:.2f}</b>"
    )
    if already_stake > 0:
        lines.append(
            f"📌 Déjà <b>{already_stake:.2f} €</b> sur ce pick — tu peux <b>ajouter</b> une mise"
        )
    return "\n".join(lines) + "\n"


def save_telegram_bet(
    pick: dict,
    *,
    odd: float,
    stake: float,
    telegram_user_id: str,
    db_path: str = "data/bettinghud.db",
) -> int:
    ev_frac = float(pick.get("ev_pct") or 0) / 100.0
    p_model = pick.get("p_model_fav")
    return int(
        save_bet_enriched(
            match_name=str(pick["match_name"]),
            bet_on=str(pick["bet_on"]),
            odds=float(odd),
            stake=float(stake),
            db_path=db_path,
            match_date=normalize_schedule_date(pick.get("match_date")),
            tour=pick.get("tour"),
            surface=pick.get("surface"),
            tournament=pick.get("tournament"),
            match_id=pick.get("match_id"),
            segment_key=pick.get("segment_key"),
            p_model=p_model,
            ev_at_bet=ev_frac if ev_frac else None,
            bookmaker_source="telegram_custom",
            notes=f"telegram_{pick.get('list_kind', 'pick')}",
            tracker_source="telegram_bet",
            telegram_user_id=str(telegram_user_id).strip(),
        )
    )


def handle_callback_query(
    cq: dict,
    *,
    token: str,
    chat_id: str,
    telegram_user_id: str,
    send_message,
    answer_callback,
) -> bool:
    """Traite un callback_query. Retourne True si consommé."""
    data = str(cq.get("data") or "")
    from_user = cq.get("from") or {}
    user_label = from_user.get("username") or from_user.get("first_name") or "?"

    tok_bet = callback_token(data, _CB_BET)
    if tok_bet:
        pick = get_pick_by_token(tok_bet, telegram_user_id=telegram_user_id)
        if not pick:
            answer_callback(cq, "Pick expiré — relance /jour ou /top5", alert=True)
            return True
        already = existing_stake_eur(
            pick["match_name"],
            pick["bet_on"],
            telegram_user_id=telegram_user_id,
        )
        _set_session(
            telegram_user_id,
            {
                "state": "await_odd",
                "token": tok_bet,
                "user": str(user_label),
            },
        )
        answer_callback(cq, "En attente de ta cote…")
        bet_on = _escape_html(pick.get("bet_on") or "?")
        try:
            odd_book = float(pick.get("odd_book") or 0)
        except (TypeError, ValueError):
            odd_book = 0.0
        book_hint = (
            f" <b>@ {odd_book:.2f}</b> <i>(cote book snapshot)</i>"
            if odd_book > 1.0
            else ""
        )
        extra = ""
        if already > 0:
            extra = f"\n📌 Portefeuille : déjà <b>{already:.2f} €</b> sur ce pick (cumul autorisé).\n"
        send_message(
            chat_id,
            (
                f"📝 <b>Cote réelle</b> — <b>{bet_on}</b>{book_hint}\n"
                f"{extra}\n"
                "Envoie la cote obtenue chez ton bookmaker "
                "(ex. <code>1.92</code> — tu peux reprendre la cote book si identique).\n"
                "Annuler : <code>/annuler</code>"
            ),
        )
        return True

    tok_confirm = callback_token(data, _CB_CONFIRM)
    if tok_confirm:
        sess = _get_session(telegram_user_id)
        if not sess or sess.get("state") != "await_confirm" or sess.get("token") != tok_confirm:
            answer_callback(cq, "Session expirée — recommence", alert=True)
            return True
        pick = get_pick_by_token(tok_confirm, telegram_user_id=telegram_user_id)
        if not pick:
            _set_session(telegram_user_id, None)
            answer_callback(cq, "Pick expiré", alert=True)
            return True
        odd = float(sess.get("odd") or 0)
        stake = float(sess.get("stake_eur") or 0)
        if stake < 0.01:
            answer_callback(cq, "Mise nulle — vérifie la cote", alert=True)
            return True
        br = get_telegram_user_available_eur(telegram_user_id)
        if stake > br + 1e-6:
            answer_callback(cq, "BR insuffisante", alert=True)
            return True
        try:
            bet_id = save_telegram_bet(
                pick,
                odd=odd,
                stake=stake,
                telegram_user_id=telegram_user_id,
            )
        except Exception as exc:
            answer_callback(cq, "Erreur enregistrement", alert=True)
            send_message(chat_id, f"⚠️ Échec portefeuille : <code>{_escape_html(str(exc))[:200]}</code>")
            return True
        _set_session(telegram_user_id, None)
        answer_callback(cq, "Pari enregistré ✓")
        send_message(
            chat_id,
            (
                f"✅ <b>Pari #{bet_id} enregistré</b>\n"
                f"{_escape_html(pick.get('match_name') or '')}\n"
                f"→ <b>{_escape_html(pick.get('bet_on') or '')}</b> @ <b>{odd:.2f}</b> · "
                f"<b>{stake:.2f} €</b>\n"
                f"<i>Visible dans le portefeuille (sync résultats auto).</i>"
            ),
        )
        return True

    tok_stake = callback_token(data, _CB_CUSTOM_STAKE)
    if tok_stake:
        sess = _get_session(telegram_user_id)
        if not sess or sess.get("token") != tok_stake:
            answer_callback(cq, "Session expirée", alert=True)
            return True
        pick = get_pick_by_token(tok_stake, telegram_user_id=telegram_user_id)
        if not pick:
            _set_session(telegram_user_id, None)
            answer_callback(cq, "Pick expiré", alert=True)
            return True
        br = float(sess.get("br_avail") or get_telegram_user_available_eur(telegram_user_id))
        sess["state"] = "await_custom_stake"
        sess["br_avail"] = br
        _set_session(telegram_user_id, sess)
        answer_callback(cq, "Saisis ta mise…")
        send_message(
            chat_id,
            (
                f"✏️ <b>Mise en euros</b> pour {_escape_html(pick.get('bet_on') or '?')}\n"
                f"Cote : <b>{float(sess.get('odd') or 0):.2f}</b> · "
                f"Kelly suggéré : <b>{float(sess.get('kelly_eur') or 0):.2f} €</b>\n"
                f"BR dispo : <b>{br:.2f} €</b>\n\n"
                "Envoie un montant (ex. <code>2.50</code> ou <code>5</code>).\n"
                "Annuler : <code>/annuler</code>"
            ),
        )
        return True

    tok_cancel = callback_token(data, _CB_CANCEL)
    if tok_cancel:
        _set_session(telegram_user_id, None)
        answer_callback(cq, "Annulé")
        send_message(chat_id, "❌ Pari annulé.")
        return True

    return False


def handle_text_message(
    msg: dict,
    *,
    token: str,
    chat_id: str,
    telegram_user_id: str,
    send_message,
) -> bool:
    """Traite un message texte si une session est active."""
    text = str(msg.get("text") or "").strip()
    if not text:
        return False

    cmd = text.split()[0].lower().split("@")[0]
    if cmd in ("/annuler", "/cancel"):
        if _get_session(telegram_user_id):
            _set_session(telegram_user_id, None)
            send_message(chat_id, "❌ Session annulée.")
            return True
        return False

    sess = _get_session(telegram_user_id)
    if not sess:
        return False

    state = str(sess.get("state") or "")
    tok = str(sess.get("token") or "")
    pick = get_pick_by_token(tok, telegram_user_id=telegram_user_id) if tok else None
    if not pick:
        _set_session(telegram_user_id, None)
        send_message(chat_id, "⚠️ Pick expiré — relance <b>/jour</b> ou <b>/top5</b>.")
        return True

    if state == "await_custom_stake":
        stake = parse_user_stake_eur(text)
        if stake is None:
            send_message(
                chat_id,
                "⚠️ Mise invalide. Exemple : <code>2.50</code> ou <code>5</code> (min 0.01 €).",
            )
            return True
        br = float(sess.get("br_avail") or get_telegram_user_available_eur(telegram_user_id))
        if stake > br + 1e-6:
            send_message(
                chat_id,
                f"⚠️ Mise <b>{stake:.2f} €</b> &gt; BR dispo (<b>{br:.2f} €</b>).",
            )
            return True
        odd = float(sess.get("odd") or 0)
        kelly_eur = float(sess.get("kelly_eur") or 0)
        _, stake_pct = kelly_stake_for_pick(pick, custom_odd=odd, bankroll_avail=br)
        sess.update(
            {
                "state": "await_confirm",
                "stake_eur": stake,
                "stake_pct": stake_pct,
            }
        )
        _set_session(telegram_user_id, sess)
        _present_confirm_screen(
            chat_id,
            pick,
            odd=odd,
            stake_eur=stake,
            stake_pct=stake_pct,
            br_avail=br,
            kelly_eur=kelly_eur,
            token=tok,
            send_message=send_message,
        )
        return True

    if state == "await_confirm":
        stake = parse_user_stake_eur(text)
        if stake is None:
            return False
        br = float(sess.get("br_avail") or get_telegram_user_available_eur(telegram_user_id))
        if stake > br + 1e-6:
            send_message(
                chat_id,
                f"⚠️ Mise <b>{stake:.2f} €</b> &gt; BR dispo (<b>{br:.2f} €</b>).",
            )
            return True
        odd = float(sess.get("odd") or 0)
        kelly_eur = float(sess.get("kelly_eur") or 0)
        _, stake_pct = kelly_stake_for_pick(pick, custom_odd=odd, bankroll_avail=br)
        sess["stake_eur"] = stake
        sess["stake_pct"] = stake_pct
        _set_session(telegram_user_id, sess)
        _present_confirm_screen(
            chat_id,
            pick,
            odd=odd,
            stake_eur=stake,
            stake_pct=stake_pct,
            br_avail=br,
            kelly_eur=kelly_eur,
            token=tok,
            send_message=send_message,
        )
        return True

    if state != "await_odd":
        return False

    odd = parse_user_odd(text)
    if odd is None:
        send_message(
            chat_id,
            "⚠️ Cote invalide. Exemple : <code>1.85</code> (entre 1.01 et 100).",
        )
        return True

    br = get_telegram_user_available_eur(telegram_user_id)
    stake_eur, stake_pct = kelly_stake_for_pick(pick, custom_odd=odd, bankroll_avail=br)
    if stake_eur < 0.01:
        send_message(
            chat_id,
            f"ℹ️ Kelly = 0 € avec cote <b>{odd:.2f}</b> (EV ou BR insuffisante).",
        )
        _set_session(telegram_user_id, None)
        return True

    _set_session(
        telegram_user_id,
        {
            "state": "await_confirm",
            "token": tok,
            "odd": odd,
            "stake_eur": stake_eur,
            "stake_pct": stake_pct,
            "kelly_eur": stake_eur,
            "br_avail": br,
        },
    )
    _present_confirm_screen(
        chat_id,
        pick,
        odd=odd,
        stake_eur=stake_eur,
        stake_pct=stake_pct,
        br_avail=br,
        kelly_eur=stake_eur,
        token=tok,
        send_message=send_message,
    )
    return True
