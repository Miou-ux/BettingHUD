#!/usr/bin/env python3
"""Accorde l'accès premium à un utilisateur web (admin / test phase 0)."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.web_billing import grant_premium_days


def main() -> None:
    ap = argparse.ArgumentParser(description="Grant premium access to a web user")
    ap.add_argument("--username", required=True)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    try:
        out = grant_premium_days(args.username.strip(), args.days)
    except ValueError as exc:
        print(f"Erreur: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"OK {out['username']} premium_until={out['premium_until']} (+{out['days_added']}j)")


if __name__ == "__main__":
    main()
