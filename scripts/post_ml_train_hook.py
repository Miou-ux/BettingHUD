#!/usr/bin/env python3
"""Post-train PROD : rebuild snapshot + restart services + alerte ops."""
from __future__ import annotations

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PY = os.path.join(ROOT, "venv", "bin", "python")
if not os.path.isfile(PY):
    PY = sys.executable


def _log(msg: str) -> None:
    print(f"[post-ml-train] {msg}", flush=True)


def _run(cmd: list[str], *, timeout: int | None = None) -> int:
    _log(" ".join(cmd))
    try:
        r = subprocess.run(cmd, cwd=ROOT, timeout=timeout, check=False)
        return int(r.returncode)
    except subprocess.TimeoutExpired:
        _log(f"timeout après {timeout}s")
        return 124
    except Exception as exc:
        _log(f"erreur: {exc}")
        return 1


def run_post_ml_train_hook(*, skip_rebuild: bool = False, skip_restart: bool = False) -> int:
    if os.getenv("BETTINGHUD_POST_ML_TRAIN_HOOK", "").strip().lower() in ("0", "false", "no"):
        _log("hook désactivé (BETTINGHUD_POST_ML_TRAIN_HOOK=0)")
        return 0

    rc = 0
    if not skip_rebuild:
        _log("Rebuild live projection…")
        rc_rebuild = _run([PY, os.path.join(ROOT, "scripts", "rebuild_live_projection.py")], timeout=3600)
        if rc_rebuild != 0:
            rc = rc_rebuild
            from scripts.ops_telegram_alert import send_ops_alert

            send_ops_alert("Post-train — rebuild_live_projection ÉCHEC", f"code={rc_rebuild}")
            return rc

    if not skip_restart:
        services = ["bettinghud-dashboard", "bettinghud-daemon"]
        for svc in services:
            _log(f"systemctl restart {svc}")
            rc_svc = _run(["sudo", "systemctl", "restart", svc], timeout=120)
            if rc_svc != 0:
                rc = rc_svc
        time.sleep(5)
        for svc in services:
            rc_active = _run(["systemctl", "is-active", "--quiet", svc], timeout=30)
            if rc_active != 0:
                rc = rc_active
                from scripts.ops_telegram_alert import send_ops_alert

                send_ops_alert(f"Post-train — service {svc} inactif après restart", "")
                return rc

    from scripts.ops_telegram_alert import send_ops_alert

    send_ops_alert("Post-train ML — OK", "rebuild snapshot + restart dashboard/daemon terminés.")
    return rc


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-rebuild", action="store_true")
    ap.add_argument("--skip-restart", action="store_true")
    args = ap.parse_args()
    return run_post_ml_train_hook(skip_rebuild=args.skip_rebuild, skip_restart=args.skip_restart)


if __name__ == "__main__":
    raise SystemExit(main())
