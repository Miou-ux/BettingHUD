#!/usr/bin/env python3
"""Daemon Telegram : commandes sur demande (/jour, /top5, paris interactifs).

Long polling getUpdates. Répond uniquement aux chat_id autorisés.
Documentation : docs/TELEGRAM_TOP5.md

Usage PROD :
  python scripts/telegram_bot_daemon.py

Variables :
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  TELEGRAM_ALLOWED_CHAT_IDS   (optionnel, liste séparée par virgules)
  TELEGRAM_BOT_POLL_TIMEOUT_SEC  (defaut 15)
  TELEGRAM_UPDATE_WORKERS        (defaut 3 — commandes /jour /top5 en parallèle)
"""
from __future__ import annotations

import argparse
import logging
import os
import queue
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

import requests

from scripts.telegram_bet_flow import (
    apply_telegram_brajust,
    apply_telegram_brset,
    format_telegram_user_br_advanced_message,
    format_telegram_user_br_message,
    handle_callback_query,
    handle_text_message,
    parse_brajust_delta,
    parse_brset_amount,
    telegram_user_id_from_update,
)
from scripts.comms_locale import telegram_command_aliases
from scripts.telegram_top5_notify import (
    answer_telegram_callback_query,
    format_bot_help_message,
    format_bot_strategy_message,
    format_bot_welcome_message,
    format_telegram_error_message,
    main_menu_reply_keyboard,
    register_bot_commands,
    resolve_menu_button_text,
    run_1d1p_notify,
    run_challenger_daily_picks_notify,
    run_daily_picks_notify,
    run_major_daily_picks_notify,
    run_notify,
    send_telegram_chat_action,
    send_telegram_message,
)

LOG_DIR = os.path.join(ROOT, "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "telegram_bot_daemon.log")
OFFSET_PATH = os.path.join(ROOT, "data", "cache", ".telegram_bot_offset")

ONE_PICK_ONE_DAY_COMMANDS = telegram_command_aliases("/1pick1day", "/1d1p")
TOP5_COMMANDS = telegram_command_aliases("/top5", "/top")
TODAY_COMMANDS = telegram_command_aliases("/today", "/jour", "/picks", "/picksdujour")
# Alias historiques → redirigés vers /top5 ou /today
CHALLENGER_PICKS_COMMANDS = telegram_command_aliases("/jourchallenger", "/challengers")
MAJOR_PICKS_COMMANDS = telegram_command_aliases("/jourmajor", "/majors")
DAILY_PICKS_COMMANDS = TODAY_COMMANDS
HELP_COMMANDS = telegram_command_aliases("/help")
STRATEGY_COMMANDS = telegram_command_aliases("/strategie", "/strategy")
START_COMMANDS = telegram_command_aliases("/start")
CANCEL_COMMANDS = frozenset({"/annuler", "/cancel"})
BR_COMMANDS = telegram_command_aliases("/br")
BRSTATS_COMMANDS = telegram_command_aliases("/brstats", "/bradv", "/brdetail")
BRSET_COMMANDS = telegram_command_aliases("/brset")
BRAJUST_COMMANDS = telegram_command_aliases("/brajust")
# Commandes lourdes (snapshot + ML + envoi multi-messages) → file d'attente.
SLOW_COMMANDS = frozenset(
    ONE_PICK_ONE_DAY_COMMANDS
    | TOP5_COMMANDS
    | TODAY_COMMANDS
    | CHALLENGER_PICKS_COMMANDS
    | MAJOR_PICKS_COMMANDS
)
_UPDATE_QUEUE: queue.Queue[tuple[dict, str, frozenset[str]]] | None = None


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("telegram_bot_daemon")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


LOGGER = _setup_logger()


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass


def _allowed_chat_ids() -> set[str]:
    try:
        from scripts.telegram_access import load_broadcast_chat_ids

        return set(load_broadcast_chat_ids())
    except Exception:
        primary = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        return {primary} if primary else set()


def _read_offset() -> int:
    try:
        if os.path.isfile(OFFSET_PATH):
            return int(open(OFFSET_PATH, encoding="utf-8").read().strip() or "0")
    except (OSError, ValueError):
        pass
    return 0


def _write_offset(offset: int) -> None:
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    with open(OFFSET_PATH, "w", encoding="utf-8") as f:
        f.write(str(int(offset)))


def _normalize_command(text: str) -> str:
    t = (text or "").strip().lower()
    if not t:
        return ""
    first = t.split()[0]
    return first.split("@")[0] if "@" in first else first


def _handle_my_chat_member(
    update: dict,
    *,
    token: str,
    allowed: set[str],
) -> None:
    chat = update.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    if not chat_id or chat_id in allowed:
        return
    try:
        from scripts.telegram_access import handle_bot_joined_chat

        def _send(cid: str, body: str, reply_markup: dict | None = None) -> None:
            send_telegram_message(
                body,
                token=token,
                chat_id=cid,
                reply_markup=reply_markup,
            )

        handle_bot_joined_chat(update, token=token, send_message=_send)
        LOGGER.info("Bot ajoute chat non autorise — chat_id=%s", chat_id)
    except Exception as exc:
        LOGGER.warning("my_chat_member : %s", exc)


def _fetch_updates(token: str, offset: int, timeout_sec: int) -> list[dict]:
    url = f"https://api.telegram.org/bot{token.strip()}/getUpdates"
    params: dict = {"timeout": max(1, timeout_sec)}
    if offset > 0:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=timeout_sec + 10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates error: {data}")
    return list(data.get("result") or [])


def _handle_callback(
    cq: dict,
    *,
    token: str,
    allowed: set[str],
) -> None:
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    telegram_user_id = telegram_user_id_from_update(cq=cq)
    if not chat_id:
        return

    def send_message(
        cid: str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        send_telegram_message(
            text,
            token=token,
            chat_id=cid,
            reply_markup=reply_markup,
        )

    def answer_callback(cq_obj: dict, text: str | None, alert: bool = False) -> None:
        answer_telegram_callback_query(
            str(cq_obj.get("id") or ""),
            token=token,
            text=text,
            show_alert=alert,
        )

    try:
        from scripts.telegram_access import try_handle_access_admin_callback

        if try_handle_access_admin_callback(
            cq,
            token=token,
            telegram_user_id=telegram_user_id,
            answer_callback=answer_callback,
            send_message=send_message,
        ):
            LOGGER.info(
                "Callback acces admin — user=%s data=%s",
                telegram_user_id,
                cq.get("data"),
            )
            return
    except Exception as exc:
        LOGGER.warning("Callback acces admin : %s", exc)

    if chat_id not in allowed:
        LOGGER.warning("Callback refuse (chat_id=%s)", chat_id)
        answer_telegram_callback_query(
            str(cq.get("id") or ""),
            token=token,
            text="Chat non autorise",
            show_alert=True,
        )
        return

    if not telegram_user_id:
        answer_telegram_callback_query(
            str(cq.get("id") or ""),
            token=token,
            text="Utilisateur inconnu",
            show_alert=True,
        )
        return

    cq_id = str(cq.get("id") or "")
    if cq_id:
        answer_telegram_callback_query(cq_id, token=token)

    if handle_callback_query(
        cq,
        token=token,
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        send_message=send_message,
        answer_callback=answer_callback,
    ):
        LOGGER.info(
            "Callback traite — chat_id=%s user=%s data=%s",
            chat_id,
            telegram_user_id,
            cq.get("data"),
        )


def _handle_bet_flow_text(
    msg: dict,
    *,
    token: str,
    allowed: set[str],
) -> bool:
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    telegram_user_id = telegram_user_id_from_update(msg=msg)
    text = str(msg.get("text") or "").strip()
    if not chat_id or not text or not telegram_user_id:
        return False
    if chat_id not in allowed:
        return False

    cmd = _normalize_command(text)
    if text.startswith("/") and cmd not in CANCEL_COMMANDS:
        return False

    def send_message(
        cid: str,
        body: str,
        reply_markup: dict | None = None,
    ) -> None:
        send_telegram_message(
            body,
            token=token,
            chat_id=cid,
            reply_markup=reply_markup,
        )

    return handle_text_message(
        msg,
        token=token,
        chat_id=chat_id,
        telegram_user_id=telegram_user_id,
        send_message=send_message,
    )


def _handle_message(
    msg: dict,
    *,
    token: str,
    allowed: set[str],
) -> None:
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    text = str(msg.get("text") or "").strip()
    if not chat_id or not text:
        return

    if _handle_bet_flow_text(msg, token=token, allowed=allowed):
        return

    menu_cmd = resolve_menu_button_text(text)
    if menu_cmd:
        text = menu_cmd

    cmd = _normalize_command(text)

    if chat_id not in allowed:
        try:
            from scripts.telegram_access import (
                handle_unauthorized_start,
                process_unauthorized_access_request,
            )

            def _send_unauth(cid: str, body: str, reply_markup: dict | None = None) -> None:
                send_telegram_message(
                    body,
                    token=token,
                    chat_id=cid,
                    reply_markup=reply_markup,
                )

            from_user = dict(msg.get("from") or {})
            chat_type = str(chat.get("type") or "private")
            if cmd in START_COMMANDS or cmd == "/start":
                handle_unauthorized_start(msg, token=token, send_message=_send_unauth)
                LOGGER.info("Demande acces /start — chat_id=%s", chat_id)
            else:
                process_unauthorized_access_request(
                    chat_id=chat_id,
                    from_user=from_user,
                    chat_type=chat_type,
                    token=token,
                    send_message=_send_unauth,
                    trigger="message",
                )
                LOGGER.info("Demande acces (message) — chat_id=%s", chat_id)
        except Exception as exc:
            LOGGER.exception("Demande acces : %s", exc)
            from scripts.telegram_top5_notify import pending_access_inline_keyboard

            send_telegram_message(
                "🔒 <b>Access required</b>\n\n"
                "Send <code>/start</code> to request access to CourtAlpha Bot.",
                token=token,
                chat_id=chat_id,
                reply_markup=pending_access_inline_keyboard(),
            )
        return

    if cmd in START_COMMANDS or cmd == "/start":
        send_telegram_message(
            format_bot_welcome_message(),
            token=token,
            chat_id=chat_id,
            reply_markup=main_menu_reply_keyboard(),
        )
        send_telegram_message(
            "💡 Full guide: <code>/help</code> · Strategy & staking: <code>/strategy</code>",
            token=token,
            chat_id=chat_id,
        )
        LOGGER.info("Commande /start — chat_id=%s", chat_id)
        return

    if cmd in HELP_COMMANDS or cmd == "/help":
        send_telegram_message(
            format_bot_help_message(),
            token=token,
            chat_id=chat_id,
            reply_markup=main_menu_reply_keyboard(),
        )
        LOGGER.info("Commande /help — chat_id=%s", chat_id)
        return

    telegram_user_id = telegram_user_id_from_update(msg=msg)
    from_user = msg.get("from") or {}
    username = from_user.get("username") or from_user.get("first_name")

    if cmd in BR_COMMANDS or cmd == "/br":
        if not telegram_user_id:
            send_telegram_message("⚠️ Could not identify your Telegram account.", token=token, chat_id=chat_id)
            return
        send_telegram_message(
            format_telegram_user_br_message(telegram_user_id, username=username),
            token=token,
            chat_id=chat_id,
        )
        LOGGER.info("Commande /br — user=%s chat_id=%s", telegram_user_id, chat_id)
        return

    if cmd in BRSTATS_COMMANDS or cmd in ("/brstats", "/bradv", "/brdetail"):
        if not telegram_user_id:
            send_telegram_message("⚠️ Could not identify your Telegram account.", token=token, chat_id=chat_id)
            return
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")
        send_telegram_message(
            format_telegram_user_br_advanced_message(telegram_user_id, username=username),
            token=token,
            chat_id=chat_id,
        )
        LOGGER.info("Commande /brstats — user=%s chat_id=%s", telegram_user_id, chat_id)
        return

    if cmd in BRSET_COMMANDS or cmd == "/brset":
        if not telegram_user_id:
            send_telegram_message("⚠️ Could not identify your Telegram account.", token=token, chat_id=chat_id)
            return
        amount = parse_brset_amount(text)
        if amount is None:
            send_telegram_message(
                "Usage: <code>/brset 80</code> (starting bankroll in €).",
                token=token,
                chat_id=chat_id,
            )
            return
        apply_telegram_brset(telegram_user_id, amount)
        send_telegram_message(
            f"✅ Starting bankroll set to <b>{amount:.2f} €</b>.\n\n"
            + format_telegram_user_br_message(telegram_user_id, username=username),
            token=token,
            chat_id=chat_id,
        )
        LOGGER.info("Commande /brset — user=%s amount=%s", telegram_user_id, amount)
        return

    if cmd in BRAJUST_COMMANDS or cmd == "/brajust":
        if not telegram_user_id:
            send_telegram_message("⚠️ Could not identify your Telegram account.", token=token, chat_id=chat_id)
            return
        delta = parse_brajust_delta(text)
        if delta is None:
            send_telegram_message(
                "Usage: <code>/brajust +10</code> or <code>/brajust -5</code>.",
                token=token,
                chat_id=chat_id,
            )
            return
        apply_telegram_brajust(telegram_user_id, delta)
        send_telegram_message(
            f"✅ Adjustment <b>{delta:+.2f} €</b> applied.\n\n"
            + format_telegram_user_br_message(telegram_user_id, username=username),
            token=token,
            chat_id=chat_id,
        )
        LOGGER.info("Commande /brajust — user=%s delta=%s", telegram_user_id, delta)
        return

    if cmd in STRATEGY_COMMANDS or cmd in ("/strategie", "/strategy"):
        send_telegram_message(format_bot_strategy_message(), token=token, chat_id=chat_id)
        LOGGER.info("Commande /strategie — chat_id=%s", chat_id)
        return

    if cmd in ONE_PICK_ONE_DAY_COMMANDS or cmd in ("/1pick1day", "/1d1p"):
        if not telegram_user_id:
            send_telegram_message(
                "⚠️ Could not identify your Telegram account.",
                token=token,
                chat_id=chat_id,
            )
            return
        LOGGER.info("Commande /1pick1day — chat_id=%s user=%s", chat_id, telegram_user_id)
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")
        try:
            out = run_1d1p_notify(
                chat_id=chat_id,
                source="manual",
                interactive=True,
                telegram_user_id=telegram_user_id,
            )
            LOGGER.info(
                "1pick1day envoye a %s : n=%s (%s)",
                chat_id,
                out.get("n_picks"),
                out.get("calendar_date"),
            )
        except Exception as exc:
            LOGGER.exception("Echec /1pick1day : %s", exc)
            send_telegram_message(
                format_telegram_error_message("Erreur 1 Day 1 Pick", exc),
                token=token,
                chat_id=chat_id,
            )
        return

    if cmd in CHALLENGER_PICKS_COMMANDS or cmd in ("/jourchallenger", "/challengers"):
        LOGGER.info("Alias /jourchallenger → /today — chat_id=%s", chat_id)
        cmd = "/today"

    if cmd in MAJOR_PICKS_COMMANDS or cmd in ("/jourmajor", "/majors"):
        LOGGER.info("Alias /jourmajor → /top5 — chat_id=%s", chat_id)
        cmd = "/top5"

    if cmd in TODAY_COMMANDS or cmd in DAILY_PICKS_COMMANDS or cmd in ("/today", "/jour", "/picks", "/picksdujour"):
        if not telegram_user_id:
            send_telegram_message(
                "⚠️ Could not identify your Telegram account.",
                token=token,
                chat_id=chat_id,
            )
            return
        LOGGER.info("Commande /today — chat_id=%s user=%s", chat_id, telegram_user_id)
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")
        try:
            out = run_daily_picks_notify(
                chat_id=chat_id,
                source="manual",
                interactive=True,
                telegram_user_id=telegram_user_id,
            )
            LOGGER.info(
                "Picks du jour envoyes a %s : %d pick(s), %d message(s) (%s)",
                chat_id,
                int(out.get("n_picks") or 0),
                int(out.get("sent") or out.get("n_messages") or 0),
                out.get("calendar_date"),
            )
        except Exception as exc:
            LOGGER.exception("Echec /jour : %s", exc)
            send_telegram_message(
                format_telegram_error_message("Erreur Today's Pick (/today)", exc),
                token=token,
                chat_id=chat_id,
            )
        return

    if cmd in TOP5_COMMANDS or cmd in ("/top5", "/top"):
        if not telegram_user_id:
            send_telegram_message(
                "⚠️ Could not identify your Telegram account.",
                token=token,
                chat_id=chat_id,
            )
            return
        LOGGER.info("Commande /top5 — chat_id=%s user=%s", chat_id, telegram_user_id)
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")
        try:
            out = run_notify(
                chat_id=chat_id,
                source="manual",
                interactive=True,
                telegram_user_id=telegram_user_id,
            )
            LOGGER.info(
                "Top 5 envoye a %s : %d pick(s) (%s)",
                chat_id,
                int(out.get("n_picks") or 0),
                out.get("calendar_date"),
            )
        except Exception as exc:
            LOGGER.exception("Echec /top5 : %s", exc)
            send_telegram_message(
                format_telegram_error_message("Erreur Top 5", exc),
                token=token,
                chat_id=chat_id,
            )
        return

    if text.startswith("/"):
        send_telegram_message(
            "❓ Commande inconnue. Essaie /1pick1day, /top5, /today, /strategie ou /help.",
            token=token,
            chat_id=chat_id,
        )


def _message_command(msg: dict) -> str | None:
    text = str(msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    return _normalize_command(text)


def _update_requires_background_worker(upd: dict) -> bool:
    """Seules les commandes picks passent par la file (évite de bloquer /help, /br…)."""
    if upd.get("callback_query") or upd.get("my_chat_member"):
        return False
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return False
    cmd = _message_command(msg)
    return bool(cmd and cmd in SLOW_COMMANDS)


def _presignal_typing(upd: dict, *, token: str, allowed: set[str]) -> None:
    """Indicateur « écrit… » avant commandes lourdes (file d'attente)."""
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "").strip()
    if chat_id not in allowed:
        return
    cmd = _message_command(msg)
    if cmd and cmd in SLOW_COMMANDS:
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")


def _process_update(upd: dict, *, token: str, allowed: frozenset[str]) -> None:
    allowed_set = set(allowed)
    cq = upd.get("callback_query")
    if cq:
        _handle_callback(cq, token=token, allowed=allowed_set)
        return
    my_cm = upd.get("my_chat_member")
    if my_cm:
        _handle_my_chat_member(my_cm, token=token, allowed=allowed_set)
        return
    msg = upd.get("message") or upd.get("edited_message")
    if msg:
        _handle_message(msg, token=token, allowed=allowed_set)


def _update_worker(worker_id: int) -> None:
    assert _UPDATE_QUEUE is not None
    while True:
        upd, token, allowed = _UPDATE_QUEUE.get()
        try:
            _process_update(upd, token=token, allowed=allowed)
        except Exception as exc:
            LOGGER.exception("Erreur traitement update (worker %s) : %s", worker_id, exc)
        finally:
            _UPDATE_QUEUE.task_done()


def run_daemon(*, poll_timeout_sec: int = 25, once: bool = False) -> int:
    _load_dotenv()
    env = (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower()
    if env != "prod":
        LOGGER.error("Daemon Telegram reserve a PROD (BETTINGHUD_ENV=%s)", env)
        return 1

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        LOGGER.error("TELEGRAM_BOT_TOKEN manquant")
        return 1

    allowed = _allowed_chat_ids()
    if not allowed:
        LOGGER.error("TELEGRAM_CHAT_ID (ou TELEGRAM_ALLOWED_CHAT_IDS) manquant")
        return 1

    global _UPDATE_QUEUE
    _UPDATE_QUEUE = queue.Queue(maxsize=200)
    n_workers = max(1, int(os.getenv("TELEGRAM_UPDATE_WORKERS", "3")))
    for i in range(n_workers):
        threading.Thread(
            target=_update_worker,
            args=(i + 1,),
            daemon=True,
            name=f"tg-update-worker-{i + 1}",
        ).start()
    threading.Thread(
        target=lambda: __import__(
            "scripts.telegram_runtime_cache", fromlist=["warm_telegram_runtime_cache"]
        ).warm_telegram_runtime_cache(),
        name="tg-warm-cache",
        daemon=True,
    ).start()

    offset = _read_offset()
    if register_bot_commands(token):
        LOGGER.info("Menu commandes Telegram enregistre (setMyCommands)")
    else:
        LOGGER.warning("setMyCommands Telegram non applique")
    LOGGER.info(
        "Telegram bot daemon demarre — poll %ds, %d worker(s) picks, chats autorises : %s",
        poll_timeout_sec,
        n_workers,
        ", ".join(sorted(allowed)),
    )

    while True:
        try:
            updates = _fetch_updates(token, offset, poll_timeout_sec)
            allowed_now = frozenset(_allowed_chat_ids())
            for upd in updates:
                uid = int(upd.get("update_id") or 0)
                if uid >= offset:
                    offset = uid + 1
                if _update_requires_background_worker(upd):
                    _presignal_typing(upd, token=token, allowed=set(allowed_now))
                    _UPDATE_QUEUE.put((upd, token, allowed_now))
                else:
                    _process_update(upd, token=token, allowed=allowed_now)
            if updates:
                _write_offset(offset)
        except requests.RequestException as exc:
            LOGGER.warning("Polling Telegram : %s", exc)
            time.sleep(5)
        except Exception as exc:
            LOGGER.exception("Erreur polling : %s", exc)
            time.sleep(5)

        if once:
            break

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="CourtAlpha Telegram bot command daemon")
    ap.add_argument(
        "--poll-timeout",
        type=int,
        default=int(os.getenv("TELEGRAM_BOT_POLL_TIMEOUT_SEC", "15")),
    )
    ap.add_argument("--once", action="store_true", help="Une passe de polling puis quitter")
    args = ap.parse_args()
    return run_daemon(poll_timeout_sec=max(5, int(args.poll_timeout)), once=bool(args.once))


if __name__ == "__main__":
    raise SystemExit(main())
