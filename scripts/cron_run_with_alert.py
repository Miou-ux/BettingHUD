#!/usr/bin/env python3
"""Exécute une commande cron et alerte Telegram admin si échec."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PARIS = ZoneInfo("Europe/Paris")


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, help="Nom lisible du job")
    ap.add_argument("--log", default="", help="Fichier log (append stdout+stderr)")
    ap.add_argument("--success-alert", action="store_true", help="Notifier aussi en cas de succès")
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="Commande après --")
    args = ap.parse_args()
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: cron_run_with_alert.py --job NAME [--log path] -- cmd...", file=sys.stderr)
        return 2

    ts = datetime.now(PARIS).strftime("%Y-%m-%d %H:%M:%S")
    header = f"\n=== [{ts}] {args.job} ===\n"
    print(header, end="", flush=True)

    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if out:
        print(out, end="", flush=True)
    if args.log:
        log_path = args.log if os.path.isabs(args.log) else os.path.join(ROOT, args.log)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(header)
            fh.write(out)
            if out and not out.endswith("\n"):
                fh.write("\n")
            fh.write(f"exit_code={proc.returncode}\n")

    from scripts.ops_telegram_alert import send_ops_alert

    if proc.returncode != 0:
        tail = "\n".join(out.strip().splitlines()[-12:]) if out.strip() else "(pas de sortie)"
        send_ops_alert(
            f"{args.job} — ÉCHEC (code {proc.returncode})",
            tail,
        )
        return int(proc.returncode)

    if args.success_alert:
        send_ops_alert(f"{args.job} — OK", "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
