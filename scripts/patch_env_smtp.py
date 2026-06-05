#!/usr/bin/env python3
"""Ajoute ou met à jour les variables SMTP / web dans .env (PROD)."""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = Path(os.getenv("BETTINGHUD_ENV_FILE", ROOT / ".env"))


def _load_lines() -> list[str]:
    if not ENV_PATH.is_file():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def _set_kv(lines: list[str], key: str, value: str) -> list[str]:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    out = [ln for ln in lines if not pat.match(ln)]
    if value != "":
        out.append(f"{key}={value}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.getenv("BETTINGHUD_WEB_BASE_URL", ""))
    ap.add_argument("--smtp-host", default="smtp.gmail.com")
    ap.add_argument("--smtp-port", default="587")
    ap.add_argument("--smtp-user", default="miouppy86@gmail.com")
    ap.add_argument("--smtp-from", default="miouppy86@gmail.com")
    ap.add_argument(
        "--smtp-password",
        default=os.getenv("BETTINGHUD_SMTP_PASSWORD", ""),
        help="Ou variable d'environnement BETTINGHUD_SMTP_PASSWORD",
    )
    args = ap.parse_args()

    lines = _load_lines()
    if args.base_url:
        lines = _set_kv(lines, "BETTINGHUD_WEB_BASE_URL", args.base_url.strip())
    lines = _set_kv(lines, "BETTINGHUD_SMTP_HOST", args.smtp_host.strip())
    lines = _set_kv(lines, "BETTINGHUD_SMTP_PORT", str(args.smtp_port).strip())
    lines = _set_kv(lines, "BETTINGHUD_SMTP_USER", args.smtp_user.strip())
    lines = _set_kv(lines, "BETTINGHUD_SMTP_FROM", args.smtp_from.strip())
    lines = _set_kv(lines, "BETTINGHUD_SMTP_USE_TLS", "1")
    if args.smtp_password:
        lines = _set_kv(lines, "BETTINGHUD_SMTP_PASSWORD", args.smtp_password)

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    print(f"OK — {ENV_PATH} mis à jour (SMTP user={args.smtp_user})")
    if not args.smtp_password:
        print("⚠️  BETTINGHUD_SMTP_PASSWORD absent — passe --smtp-password ou la variable d'env.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
