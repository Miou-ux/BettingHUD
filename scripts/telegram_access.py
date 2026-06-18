#!/usr/bin/env python3
"""Demandes d'accès Telegram : notification admin + approbation inline."""
from __future__ import annotations

import json
import os
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTRA_ALLOWED_PATH = os.path.join(ROOT, "data", "cache", "telegram_allowed_chats.json")
REQUESTS_PATH = os.path.join(ROOT, "data", "cache", "telegram_access_requests.json")
NOTIFY_COOLDOWN_SEC = 3600

_CB_APPROVE = "tgacc:a:"
_CB_DENY = "tgacc:d:"


def _escape_html(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _load_json(path: str) -> dict[str, Any]:
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def admin_notify_chat_id() -> str:
    return (os.getenv("TELEGRAM_CHAT_ID") or "").strip()


def admin_user_ids() -> set[str]:
    raw = (os.getenv("TELEGRAM_ADMIN_USER_IDS") or "").strip()
    ids = {x.strip() for x in raw.split(",") if x.strip()}
    primary = (os.getenv("TELEGRAM_ADMIN_USER_ID") or admin_notify_chat_id() or "").strip()
    if primary:
        ids.add(primary)
    return ids


def is_telegram_admin(telegram_user_id: str | None) -> bool:
    uid = str(telegram_user_id or "").strip()
    if not uid:
        return False
    return uid in admin_user_ids()


def load_extra_allowed_chat_ids() -> set[str]:
    data = _load_json(EXTRA_ALLOWED_PATH)
    rows = data.get("chat_ids")
    if not isinstance(rows, list):
        return set()
    return {str(x).strip() for x in rows if str(x).strip()}


def load_broadcast_chat_ids() -> list[str]:
    """Chats validés : admin (.env) + utilisateurs approuvés (cache JSON)."""
    ids: set[str] = set()
    primary = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if primary:
        ids.add(primary)
    raw = (os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    for part in raw.split(","):
        part = part.strip()
        if part:
            ids.add(part)
    ids |= load_extra_allowed_chat_ids()
    return sorted(ids)


def add_extra_allowed_chat_id(chat_id: str) -> None:
    cid = str(chat_id or "").strip()
    if not cid:
        return
    data = _load_json(EXTRA_ALLOWED_PATH)
    ids = list(data.get("chat_ids") or [])
    if cid not in ids:
        ids.append(cid)
    data["chat_ids"] = ids
    data["updated_ts"] = time.time()
    _save_json(EXTRA_ALLOWED_PATH, data)


def _request_key(chat_id: str) -> str:
    return str(chat_id).strip()


def get_access_request(chat_id: str) -> dict[str, Any] | None:
    doc = _load_json(REQUESTS_PATH)
    row = doc.get("requests", {}).get(_request_key(chat_id))
    return dict(row) if isinstance(row, dict) else None


def _save_access_request(chat_id: str, row: dict[str, Any]) -> None:
    doc = _load_json(REQUESTS_PATH)
    reqs = dict(doc.get("requests") or {})
    reqs[_request_key(chat_id)] = row
    doc["requests"] = reqs
    _save_json(REQUESTS_PATH, doc)


def format_user_label(from_user: dict) -> str:
    uid = from_user.get("id")
    uname = from_user.get("username")
    first = from_user.get("first_name") or ""
    last = from_user.get("last_name") or ""
    name = " ".join(x for x in (first, last) if x).strip()
    parts = []
    if name:
        parts.append(_escape_html(name))
    if uname:
        parts.append(f"@{_escape_html(uname)}")
    if uid is not None:
        parts.append(f"<code>{uid}</code>")
    return " · ".join(parts) if parts else "?"


def format_admin_access_notification(
    *,
    chat_id: str,
    from_user: dict,
    chat_type: str = "private",
) -> str:
    user_line = format_user_label(from_user)
    tg_uid = str(from_user.get("id") or "").strip()
    lines = [
        "🔔 <b>Nouvelle demande d'accès</b> (commande /start)",
        "",
        f"👤 {user_line}",
        f"💬 chat_id : <code>{_escape_html(chat_id)}</code>",
        f"📱 user_id : <code>{_escape_html(tg_uid)}</code>",
        f"Type : <code>{_escape_html(chat_type)}</code>",
        "",
    ]
    if chat_type == "private" and tg_uid and tg_uid != chat_id:
        lines.append(
            "<i>En DM, chat_id et user_id sont en général identiques.</i>"
        )
    elif chat_type != "private":
        lines.append(
            "<i>Groupe : ajoute le chat_id du groupe pour autoriser tout le groupe.</i>"
        )
    lines.append(
        "\nApprouver = accès bot pour ce chat. "
        "Pour lier le dashboard web plus tard : "
        f"<code>init_web_user.py --telegram-user-id {tg_uid or '…'}</code>"
    )
    return "\n".join(lines)


def format_admin_web_registration_notification(
    username: str,
    email: str,
    display_name: str,
) -> str:
    uname = str(username or "").strip().lower()
    addr = str(email or "").strip().lower()
    name = str(display_name or uname or "?").strip()
    lines = [
        "🔔 <b>Nouvelle inscription web</b>",
        "",
        f"👤 {_escape_html(name)}",
        f"📧 <code>{_escape_html(addr)}</code>",
        f"🔑 identifiant : <code>{_escape_html(uname)}</code>",
    ]
    base = (os.getenv("BETTINGHUD_WEB_BASE_URL") or "").strip().rstrip("/")
    if base:
        lines.append(f"🌐 {_escape_html(base)}")
    return "\n".join(lines)


def notify_admin_web_registration(
    username: str,
    email: str,
    display_name: str,
) -> None:
    from scripts.telegram_top5_notify import send_telegram_message

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    admin_chat = admin_notify_chat_id()
    if not token or not admin_chat:
        return
    try:
        send_telegram_message(
            format_admin_web_registration_notification(username, email, display_name),
            token=token,
            chat_id=admin_chat,
        )
    except Exception:
        pass


def admin_approve_keyboard(chat_id: str) -> dict:
    cid = str(chat_id).strip()
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approuver", "callback_data": f"{_CB_APPROVE}{cid}"},
                {"text": "❌ Refuser", "callback_data": f"{_CB_DENY}{cid}"},
            ]
        ]
    }


def _access_eta_hours() -> str:
    return (os.getenv("TELEGRAM_ACCESS_ETA_HOURS") or "24").strip() or "24"


def _pending_access_extra_lines() -> list[str]:
    from scripts.comms_locale import PUBLIC_SITE_URL

    lines = [
        "",
        f"⏱ Typical approval: within <b>{_access_eta_hours()}h</b> (manual review).",
        f"🌐 While you wait, explore the <b>free</b> public track record on the web.",
    ]
    support = (
        (os.getenv("SUPPORT_EMAIL") or os.getenv("SMTP_FROM") or "").strip()
    )
    if support:
        lines.append(f"✉️ Questions: <code>{_escape_html(support)}</code>")
    web_base = (os.getenv("BETTINGHUD_WEB_BASE_URL") or PUBLIC_SITE_URL).rstrip("/")
    lines.append(f"🔗 <a href=\"{web_base}/1-day-1-pick\">Open 1 Day 1 Pick</a>")
    return lines


def _pending_access_user_message(*, already_requested: bool) -> str:
    if already_requested:
        return "\n".join(
            [
                "⏳ <b>Access request pending</b>",
                "",
                "An admin was already notified. You'll receive a welcome message "
                "with the button menu here once approved.",
                "",
                "No need to send <code>/start</code> again for now.",
                *_pending_access_extra_lines(),
            ]
        )
    return "\n".join(
        [
            f"👋 <b>Welcome to {(os.getenv('COMMS_BRAND_NAME') or 'CourtAlpha').strip() or 'CourtAlpha'} Bot</b>",
            "",
            "⏳ <b>Your access request was sent.</b>",
            "You'll get a notification here when approved, with a quick menu "
            "(1 Day 1 Pick, Top 5, Today, Bet, bankroll…).",
            *_pending_access_extra_lines(),
        ]
    )


def process_unauthorized_access_request(
    *,
    chat_id: str,
    from_user: dict,
    chat_type: str,
    token: str,
    send_message,
    trigger: str = "start",
) -> None:
    """Enregistre la demande, informe l'utilisateur, notifie l'admin (anti-spam 1 h)."""
    from scripts.telegram_top5_notify import send_telegram_message

    cid = str(chat_id or "").strip()
    if not cid:
        return

    now = time.time()
    prev = get_access_request(cid) or {}
    status = str(prev.get("status") or "")
    last_notify = float(prev.get("admin_notified_ts") or 0)

    if status == "approved":
        send_message(
            cid,
            "✅ Your access is already active — send <code>/start</code> for the menu.",
        )
        return

    already_pending = status == "pending" and (now - last_notify) < NOTIFY_COOLDOWN_SEC
    from scripts.telegram_top5_notify import pending_access_inline_keyboard

    send_message(
        cid,
        _pending_access_user_message(already_requested=already_pending),
        pending_access_inline_keyboard(),
    )

    admin_chat = admin_notify_chat_id()
    if not admin_chat or already_pending:
        return

    row = {
        **prev,
        "chat_id": cid,
        "telegram_user_id": str(from_user.get("id") or "").strip(),
        "username": from_user.get("username"),
        "first_name": from_user.get("first_name"),
        "last_name": from_user.get("last_name"),
        "chat_type": str(chat_type or "private"),
        "status": "pending",
        "requested_ts": prev.get("requested_ts") or now,
        "admin_notified_ts": now,
        "trigger": trigger,
    }
    _save_access_request(cid, row)

    try:
        send_telegram_message(
            format_admin_access_notification(
                chat_id=cid,
                from_user=from_user,
                chat_type=str(chat_type or "private"),
            ),
            token=token,
            chat_id=admin_chat,
            reply_markup=admin_approve_keyboard(cid),
        )
    except Exception:
        pass


def handle_unauthorized_start(
    msg: dict,
    *,
    token: str,
    send_message,
) -> None:
    """Utilisateur non autorisé : invitation /start + notification admin."""
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    from_user = dict(msg.get("from") or {})
    if not chat_id:
        return
    process_unauthorized_access_request(
        chat_id=chat_id,
        from_user=from_user,
        chat_type=str(chat.get("type") or "private"),
        token=token,
        send_message=send_message,
        trigger="start",
    )


def handle_bot_joined_chat(
    update: dict,
    *,
    token: str,
    send_message,
) -> None:
    """Bot ajouté à un chat non autorisé : inviter à /start + notifier l'admin."""
    chat = update.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id:
        return
    new_m = update.get("new_chat_member") or {}
    old_m = update.get("old_chat_member") or {}
    new_status = str(new_m.get("status") or "")
    old_status = str(old_m.get("status") or "")
    if new_status not in ("member", "administrator"):
        return
    if old_status in ("member", "administrator"):
        return

    from_user = dict(update.get("from") or {})
    process_unauthorized_access_request(
        chat_id=chat_id,
        from_user=from_user,
        chat_type=str(chat.get("type") or "private"),
        token=token,
        send_message=send_message,
        trigger="bot_joined",
    )


def send_post_approval_onboarding(
    chat_id: str,
    *,
    send_message,
) -> None:
    """Après validation admin : bienvenue + guide d'utilisation."""
    from scripts.telegram_top5_notify import (
        format_bot_onboarding_after_approval,
        format_bot_welcome_message,
        main_menu_reply_keyboard,
    )

    cid = str(chat_id or "").strip()
    if not cid:
        return
    send_message(
        cid,
        "✅ <b>Access granted!</b>\n\n" + format_bot_welcome_message(),
        main_menu_reply_keyboard(),
    )
    send_message(cid, format_bot_onboarding_after_approval())


def try_handle_access_admin_callback(
    cq: dict,
    *,
    token: str,
    telegram_user_id: str | None,
    answer_callback,
    send_message,
) -> bool:
    """Boutons Approuver / Refuser (admin uniquement). Retourne True si traité."""

    data = str(cq.get("data") or "")
    if not (data.startswith(_CB_APPROVE) or data.startswith(_CB_DENY)):
        return False

    if not is_telegram_admin(telegram_user_id):
        answer_callback(cq, "Réservé à l'administrateur.", alert=True)
        return True

    target_chat = data[len(_CB_APPROVE) :] if data.startswith(_CB_APPROVE) else data[len(_CB_DENY) :]
    target_chat = target_chat.strip()
    if not target_chat:
        answer_callback(cq, "Demande invalide.", alert=True)
        return True

    if data.startswith(_CB_APPROVE):
        add_extra_allowed_chat_id(target_chat)
        prev = get_access_request(target_chat) or {}
        _save_access_request(
            target_chat,
            {
                **prev,
                "chat_id": target_chat,
                "status": "approved",
                "approved_ts": time.time(),
                "approved_by": str(telegram_user_id or ""),
            },
        )
        try:
            send_post_approval_onboarding(target_chat, send_message=send_message)
        except Exception:
            pass
        answer_callback(cq, f"Accès accordé pour {target_chat}", alert=False)
        if admin_notify_chat_id():
            send_message(
                admin_notify_chat_id(),
                f"✅ Accès <b>approuvé</b> pour chat <code>{_escape_html(target_chat)}</code>.",
            )
        return True

    prev = get_access_request(target_chat) or {}
    _save_access_request(
        target_chat,
        {
            **prev,
            "chat_id": target_chat,
            "status": "denied",
            "denied_ts": time.time(),
            "denied_by": str(telegram_user_id or ""),
        },
    )
    try:
        send_message(
            target_chat,
            "❌ Your access request to CourtAlpha Bot was not approved.",
        )
    except Exception:
        pass
    answer_callback(cq, "Demande refusée.", alert=False)
    return True
