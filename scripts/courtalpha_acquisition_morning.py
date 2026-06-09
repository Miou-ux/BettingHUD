#!/usr/bin/env python3
"""Pipeline acquisition matin : canal Telegram public + CourtAlphaX tweet."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] [acquisition] {msg}"
    print(line, flush=True)


def run_acquisition_morning(*, dry_run: bool = False, skip_x: bool = False) -> dict:
    out: dict = {"ok": True, "steps": []}

    try:
        from scripts.telegram_channel_notify import run_channel_notify

        ch = run_channel_notify(dry_run=dry_run)
        _log(f"Canal TG : {ch}")
        out["steps"].append({"channel": ch})
    except Exception as exc:
        _log(f"Canal TG ERREUR : {exc}")
        out["ok"] = False
        out["steps"].append({"channel": {"ok": False, "error": str(exc)}})

    if not skip_x:
        try:
            if dry_run:
                import subprocess

                r = subprocess.run(
                    [sys.executable, "scripts/courtalphax_daily_pick.py", "--dry-run"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                _log(f"CourtAlphaX dry-run exit={r.returncode}")
                if r.stdout:
                    _log(r.stdout.strip()[-500:])
                out["steps"].append({"courtalphax": {"dry_run": True, "exit": r.returncode}})
            else:
                from scripts.courtalphax_daily_pick import run_daily_pick

                x = run_daily_pick(
                    dry_run=False,
                    skip_preflight=os.getenv("COURTALPHAX_SKIP_PREFLIGHT", "").strip().lower()
                    in ("1", "true", "yes"),
                )
                _log(f"CourtAlphaX : {x}")
                out["steps"].append({"courtalphax": x})
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
            _log(f"CourtAlphaX exit {code}")
            out["steps"].append({"courtalphax": {"exit": code}})
            if code != 0:
                out["ok"] = False
        except Exception as exc:
            _log(f"CourtAlphaX ERREUR : {exc}")
            out["ok"] = False
            out["steps"].append({"courtalphax": {"ok": False, "error": str(exc)}})

    return out


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Acquisition matin CourtAlpha")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-x", action="store_true", help="Canal TG seulement")
    args = ap.parse_args()

    if (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower() != "prod" and not args.dry_run:
        _log("PREPROD — rien envoyé (utiliser --dry-run)")
        return

    out = run_acquisition_morning(dry_run=args.dry_run, skip_x=args.skip_x)
    _log(str(out))
    if not out.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
