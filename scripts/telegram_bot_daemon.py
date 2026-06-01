#!/usr/bin/env python3
"""Daemon Telegram : commandes sur demande (/jour, /jourchallenger, /top5, /strategie).

Long polling getUpdates. Répond uniquement aux chat_id autorisés.
Documentation : docs/TELEGRAM_TOP5.md

Usage PROD :
  python scripts/telegram_bot_daemon.py

Variables :
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
  TELEGRAM_ALLOWED_CHAT_IDS   (optionnel, liste séparée par virgules)
  TELEGRAM_BOT_POLL_TIMEOUT_SEC  (defaut 25)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

import requests

from scripts.telegram_top5_notify import (
    format_bot_help_message,
    format_bot_strategy_message,
    format_bot_welcome_message,
    format_telegram_error_message,
    run_challenger_daily_picks_notify,
    run_daily_picks_notify,
    run_notify,
    send_telegram_chat_action,
    send_telegram_message,
)

LOG_DIR = os.path.join(ROOT, "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "telegram_bot_daemon.log")
OFFSET_PATH = os.path.join(ROOT, "data", "cache", ".telegram_bot_offset")

TOP5_COMMANDS = frozenset({"/top5", "/top", "/top5@bettinghudbot"})
DAILY_PICKS_COMMANDS = frozenset({
    "/jour",
    "/picks",
    "/picksdujour",
    "/jour@bettinghudbot",
    "/picks@bettinghudbot",
})
CHALLENGER_PICKS_COMMANDS = frozenset({
    "/jourchallenger",
    "/challengers",
    "/jourchallenger@bettinghudbot",
    "/challengers@bettinghudbot",
})
HELP_COMMANDS = frozenset({"/help", "/help@bettinghudbot"})
STRATEGY_COMMANDS = frozenset({
    "/strategie",
    "/strategy",
    "/strategie@bettinghudbot",
    "/strategy@bettinghudbot",
})
START_COMMANDS = frozenset({"/start", "/start@bettinghudbot"})


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
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    ids = {x.strip() for x in raw.split(",") if x.strip()}
    primary = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if primary:
        ids.add(primary)
    return ids


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

    cmd = _normalize_command(text)

    if chat_id not in allowed:
        LOGGER.warning("Commande refusee (chat_id=%s) : %s", chat_id, cmd or text[:40])
        send_telegram_message(
            "🔒 <i>Chat non autorise.</i>",
            token=token,
            chat_id=chat_id,
        )
        return

    if cmd in START_COMMANDS or cmd == "/start":
        send_telegram_message(format_bot_welcome_message(), token=token, chat_id=chat_id)
        LOGGER.info("Commande /start — chat_id=%s", chat_id)
        return

    if cmd in HELP_COMMANDS or cmd == "/help":
        send_telegram_message(format_bot_help_message(), token=token, chat_id=chat_id)
        LOGGER.info("Commande /help — chat_id=%s", chat_id)
        return

    if cmd in STRATEGY_COMMANDS or cmd in ("/strategie", "/strategy"):
        send_telegram_message(format_bot_strategy_message(), token=token, chat_id=chat_id)
        LOGGER.info("Commande /strategie — chat_id=%s", chat_id)
        return

    if cmd in CHALLENGER_PICKS_COMMANDS or cmd in ("/jourchallenger", "/challengers"):
        LOGGER.info("Commande /jourchallenger — chat_id=%s", chat_id)
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")
        try:
            out = run_challenger_daily_picks_notify(chat_id=chat_id, source="manual")
            LOGGER.info(
                "Challengers du jour envoyes a %s : %d pick(s), %d message(s) (%s)",
                chat_id,
                int(out.get("n_picks") or 0),
                int(out.get("sent") or out.get("n_messages") or 0),
                out.get("calendar_date"),
            )
        except Exception as exc:
            LOGGER.exception("Echec /jourchallenger : %s", exc)
            send_telegram_message(
                format_telegram_error_message("Erreur Challengers du jour", exc),
                token=token,
                chat_id=chat_id,
            )
        return

    if cmd in DAILY_PICKS_COMMANDS or cmd in ("/jour", "/picks", "/picksdujour"):
        LOGGER.info("Commande /jour — chat_id=%s", chat_id)
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")
        try:
            out = run_daily_picks_notify(chat_id=chat_id, source="manual")
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
                format_telegram_error_message("Erreur picks du jour (/jour)", exc),
                token=token,
                chat_id=chat_id,
            )
        return

    if cmd in TOP5_COMMANDS or cmd in ("/top5", "/top"):
        LOGGER.info("Commande /top5 — chat_id=%s", chat_id)
        send_telegram_chat_action(token=token, chat_id=chat_id, action="typing")
        try:
            out = run_notify(chat_id=chat_id, source="manual")
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
            "❓ Commande inconnue. Essaie /jour, /jourchallenger, /top5, /strategie ou /help.",
            token=token,
            chat_id=chat_id,
        )


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

    offset = _read_offset()
    LOGGER.info(
        "Telegram bot daemon demarre — poll %ds, chats autorises : %s",
        poll_timeout_sec,
        ", ".join(sorted(allowed)),
    )

    while True:
        try:
            updates = _fetch_updates(token, offset, poll_timeout_sec)
            for upd in updates:
                uid = int(upd.get("update_id") or 0)
                if uid >= offset:
                    offset = uid + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    _handle_message(msg, token=token, allowed=allowed)
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
    ap = argparse.ArgumentParser(description="Daemon commandes Telegram BettingHUD")
    ap.add_argument(
        "--poll-timeout",
        type=int,
        default=int(os.getenv("TELEGRAM_BOT_POLL_TIMEOUT_SEC", "25")),
    )
    ap.add_argument("--once", action="store_true", help="Une passe de polling puis quitter")
    args = ap.parse_args()
    return run_daemon(poll_timeout_sec=max(5, int(args.poll_timeout)), once=bool(args.once))


if __name__ == "__main__":
    raise SystemExit(main())
