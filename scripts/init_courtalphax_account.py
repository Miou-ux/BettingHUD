#!/usr/bin/env python3
"""Crée le compte CourtAlphaX (web + bankroll dédiée 100 €).

Usage :
  py -3 scripts/init_courtalphax_account.py --password "…"
  py -3 scripts/init_courtalphax_account.py --password "…" --dry-run

Voir docs/COURTALPHAX_X.md pour le flux de publication X.
"""
from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.bets_db import (  # noqa: E402
    compute_telegram_user_bankroll_eur,
    ensure_bets_meta,
    set_telegram_user_start_br,
)
from scripts.courtalphax_config import (  # noqa: E402
    COURTALPHAX_DISPLAY_NAME,
    COURTALPHAX_START_BR_EUR,
    COURTALPHAX_TELEGRAM_USER_ID,
    COURTALPHAX_USERNAME,
)
from scripts.web_auth import _users_path, upsert_web_user  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Initialiser le compte CourtAlphaX (web + BR 100 €)")
    ap.add_argument("--password", default="", help="Mot de passe web (sinon interactif)")
    ap.add_argument("--email", default="", help="E-mail optionnel (reset MDP)")
    ap.add_argument("--start-br", type=float, default=COURTALPHAX_START_BR_EUR, help="Capital de départ (défaut 100)")
    ap.add_argument("--db", default="data/bettinghud.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pwd = str(args.password or "").strip()
    if not pwd and not args.dry_run:
        pwd = getpass.getpass("Mot de passe CourtAlphaX: ")
        pwd2 = getpass.getpass("Confirmer: ")
        if pwd != pwd2:
            print("Les mots de passe ne correspondent pas.", file=sys.stderr)
            return 1
    if not pwd and not args.dry_run:
        print("Mot de passe requis.", file=sys.stderr)
        return 1

    uid = COURTALPHAX_TELEGRAM_USER_ID
    start = float(args.start_br)

    print(f"Compte web     : {COURTALPHAX_USERNAME}")
    print(f"Display name   : {COURTALPHAX_DISPLAY_NAME}")
    print(f"telegram_user_id (scope BR) : {uid}")
    print(f"Bankroll start : {start:.2f} €")
    print(f"Fichier users  : {_users_path()}")
    print(f"Base SQLite    : {args.db}")

    if args.dry_run:
        print("dry-run — rien écrit.")
        return 0

    upsert_web_user(
        COURTALPHAX_USERNAME,
        pwd,
        display_name=COURTALPHAX_DISPLAY_NAME,
        role="user",
        telegram_user_id=uid,
        email=str(args.email).strip() or None,
    )

    conn = sqlite3.connect(args.db)
    try:
        ensure_bets_meta(conn)
        set_telegram_user_start_br(conn, uid, start)
        snap = compute_telegram_user_bankroll_eur(conn, uid)
    finally:
        conn.close()

    print(f"OK — utilisateur '{COURTALPHAX_USERNAME}' prêt.")
    print(
        f"BR CourtAlphaX : {float(snap['available_eur']):.2f} € dispo "
        f"(start {float(snap['start_eur']):.2f} €)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
