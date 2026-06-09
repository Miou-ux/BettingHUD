#!/usr/bin/env python3
"""Brouillon Reddit hebdomadaire → Telegram admin (publication manuelle)."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
PARIS = ZoneInfo("Europe/Paris")

TRACK_URL = "https://courtalpha.tech/1-day-1-pick?utm_source=reddit&utm_medium=community&utm_campaign=weekly"
METHODO_URL = "https://courtalpha.tech/methodo?utm_source=reddit&utm_medium=community&utm_campaign=weekly"


def _admin_chat_ids() -> list[str]:
    raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    extra = (os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    ids: list[str] = []
    for part in (raw, extra):
        for bit in part.replace(";", ",").split(","):
            b = bit.strip()
            if b and b not in ids:
                ids.append(b)
    return ids


def _courtalpha_root() -> str:
    raw = (os.getenv("COURTALPHA_ROOT") or "").strip()
    if raw and os.path.isdir(raw):
        return raw
    sibling = os.path.join(os.path.dirname(ROOT), "CourtAlpha")
    return sibling if os.path.isdir(sibling) else "/opt/courtalpha"


def build_reddit_draft(db_path: str) -> str:
    ca = _courtalpha_root()
    if ca not in sys.path:
        sys.path.insert(0, ca)
    from api.services.one_day_one_pick import build_one_day_one_pick_replay

    replay = build_one_day_one_pick_replay(db_path=db_path)
    summary = replay.get("summary") or {}
    picks = list(replay.get("picks") or [])
    today = str(replay.get("today_date") or datetime.now(PARIS).date().isoformat())
    ym = today[:7]
    month_picks = [p for p in picks if str(p.get("calendar_date", "")).startswith(f"{ym}-")]
    month_settled = [p for p in month_picks if p.get("won") or p.get("lost")]
    month_wins = sum(1 for p in month_settled if p.get("won"))
    month_hit = (100.0 * month_wins / len(month_settled)) if month_settled else 0.0

    title = "[Tennis] Track record public — pick/jour auditable"
    body = f"""Je développe CourtAlpha, un outil perso de value bets tennis. Replay public :

→ {TRACK_URL}

<b>Stats {ym}</b> : {len(month_picks)} picks · hit {month_hit:.1f}% · P/L net simulé {summary.get('net_profit_eur', 0):+.0f} € (BR 100 €)

Chaque jour : 1 match majeur ATP/WTA, favori modèle max, EV 15–100 %. Historique complet vérifiable.

Methodo : {METHODO_URL}

(Jeu responsable, 18+ — pas un conseil financier)

---
<b>Titre Reddit</b> : {title}
<b>Subs suggérés</b> : r/parisportifs · r/tennis (angle stats)
<i>Relire avant publication — ne pas spammer.</i>"""
    return body


def run_reddit_draft_notify(*, dry_run: bool = False) -> dict:
    from scripts.bets_db import DB_PATH_DEFAULT
    from scripts.telegram_top5_notify import send_telegram_message

    db_path = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    text = build_reddit_draft(db_path)

    if dry_run:
        print(text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
        return {"ok": True, "dry_run": True}

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = _admin_chat_ids()
    if not token or not chats:
        return {"ok": False, "reason": "missing_telegram_config"}

    for chat_id in chats:
        send_telegram_message(text, token=token, chat_id=chat_id)
    return {"ok": True, "sent": len(chats)}


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = run_reddit_draft_notify(dry_run=args.dry_run)
    print(out)
    if not out.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
