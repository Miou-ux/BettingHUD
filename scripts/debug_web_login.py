#!/usr/bin/env python3
"""Diagnostic login web."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

from scripts.web_auth import (
    _users_path,
    authenticate,
    list_web_users,
    sync_users_from_env,
    verify_password,
    _find_user_record,
)


def main() -> int:
    print("users_file:", _users_path(), "exists:", _users_path().is_file())
    print("BETTINGHUD_WEB_PASSWORD_MIOUPPY set:", bool(os.getenv("BETTINGHUD_WEB_PASSWORD_MIOUPPY")))
    sync_users_from_env()
    users = list_web_users()
    print("users:", [u.get("username") for u in users])
    for u in users:
        print(" ", u.get("username"), "email=", u.get("email"))
    for name, pwd in [("miouppy", "Miouppy"), ("Miouppy", "Miouppy")]:
        ok = authenticate(name, pwd)
        print(f"authenticate({name!r}, Miouppy) ->", "OK" if ok else "FAIL")
    rec = _find_user_record("miouppy")
    if rec:
        print("verify Miouppy:", verify_password("Miouppy", str(rec.get("password_hash") or "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
