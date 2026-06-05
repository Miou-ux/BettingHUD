#!/usr/bin/env python3
"""Supprime le tweet de test CourtAlphaX (usage ponctuel)."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.x_client import delete_tweet

DEFAULT_TEST_TWEET_ID = "2062881002347765939"


def main() -> int:
    ap = argparse.ArgumentParser(description="Supprimer un tweet X par ID")
    ap.add_argument("tweet_id", nargs="?", default=DEFAULT_TEST_TWEET_ID)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    res = delete_tweet(args.tweet_id, dry_run=args.dry_run)
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
