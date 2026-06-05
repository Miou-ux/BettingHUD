#!/usr/bin/env python3
"""Vérifie les credentials X dans .env (sans publier de tweet)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import requests
from requests_oauthlib import OAuth1


def main() -> int:
    for key in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        if not os.getenv(key, "").strip():
            print(f"MANQUANT: {key}", file=sys.stderr)
            return 1

    auth = OAuth1(
        os.environ["X_API_KEY"],
        os.environ["X_API_SECRET"],
        os.environ["X_ACCESS_TOKEN"],
        os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    r = requests.get(
        "https://api.x.com/2/users/me",
        auth=auth,
        params={"user.fields": "username,name"},
        timeout=30,
    )
    print(f"HTTP {r.status_code}")
    if not r.ok:
        print(r.text[:400], file=sys.stderr)
        return 1
    data = r.json().get("data") or {}
    print(f"OK — compte @{data.get('username')} ({data.get('name')})")
    print(f"COURTALPHAX_X_ENABLED={os.getenv('COURTALPHAX_X_ENABLED', '0')}")
    print(f"BETTINGHUD_ENV={os.getenv('BETTINGHUD_ENV', 'preprod')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
