#!/usr/bin/env python3
"""Crée ou met à jour un utilisateur web (data/web_users.json)."""
from __future__ import annotations

import argparse
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.web_auth import upsert_web_user, _users_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Ajouter / mettre à jour un utilisateur BettingHUD web")
    ap.add_argument("--username", default="miouppy")
    ap.add_argument("--display-name", default="Miouppy")
    ap.add_argument("--role", default="owner")
    ap.add_argument("--password", default="", help="Sinon demandé en interactif")
    ap.add_argument("--telegram-user-id", default="", help="Optionnel (ex. 7113749284)")
    ap.add_argument("--email", default="", help="E-mail (réinitialisation mot de passe)")
    args = ap.parse_args()

    pwd = str(args.password or "").strip()
    if not pwd:
        pwd = getpass.getpass("Mot de passe: ")
        pwd2 = getpass.getpass("Confirmer: ")
        if pwd != pwd2:
            print("Les mots de passe ne correspondent pas.", file=sys.stderr)
            return 1
    if not pwd:
        print("Mot de passe vide.", file=sys.stderr)
        return 1

    row = upsert_web_user(
        args.username,
        pwd,
        display_name=args.display_name or args.username,
        role=args.role,
        telegram_user_id=str(args.telegram_user_id).strip() or None,
        email=str(args.email).strip() or None,
    )
    print(f"OK — utilisateur '{row['username']}' enregistré dans {_users_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
