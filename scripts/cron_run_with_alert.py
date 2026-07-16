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


def _chain_state_tail() -> str:
    """Ajoute le dernier état morning_chain si dispo (contexte échec)."""
    try:
        from scripts.morning_chain_state import get_step

        lines = []
        for name in ("tours_sync", "qc_post_sync", "build", "publish"):
            info = get_step(name)
            if not info:
                continue
            ok = info.get("ok")
            rc = info.get("rc")
            day = str(info.get("calendar_date") or "")[:10]
            lines.append(f"chain.{name}: ok={ok} rc={rc} day={day}")
        return "\n".join(lines)
    except Exception:
        return ""


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
    ap.add_argument(
        "--dedup-key",
        default="",
        help="Clé anti-doublon ops (sinon dérivée du titre job)",
    )
    ap.add_argument("cmd", nargs=argparse.REMAINDER, help="Commande après --")
    args = ap.parse_args()
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: cron_run_with_alert.py --job NAME [--log path] -- cmd...", file=sys.stderr)
        return 2

    # Permet aux sous-processus (QC) de savoir qu'un wrapper cron alertera déjà.
    env = os.environ.copy()
    env["BETTINGHUD_IN_CRON_ALERT"] = "1"
    env["BETTINGHUD_CRON_JOB"] = str(args.job)

    ts = datetime.now(PARIS).strftime("%Y-%m-%d %H:%M:%S")
    header = f"\n=== [{ts}] {args.job} ===\n"
    print(header, end="", flush=True)

    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if out:
        print(out, end="", flush=True)
    if args.log:
        log_path = args.log if os.path.isabs(args.log) else os.path.join(ROOT, args.log)
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(header)
            fh.write(out)
            if out and not out.endswith("\n"):
                fh.write("\n")
            fh.write(f"exit_code={proc.returncode}\n")

    from scripts.ops_telegram_alert import send_ops_alert

    if proc.returncode != 0:
        tail_lines = out.strip().splitlines()[-14:] if out.strip() else ["(pas de sortie)"]
        # Surligne les lignes QC / BLOCK si présentes
        highlight = [ln for ln in tail_lines if "BLOCK" in ln or "qc_post_sync" in ln or "ÉCHEC" in ln]
        body_parts = []
        if highlight:
            body_parts.append("Faits saillants:\n" + "\n".join(highlight[-8:]))
        body_parts.append("Log (fin):\n" + "\n".join(tail_lines))
        chain = _chain_state_tail()
        if chain:
            body_parts.append("État chaîne:\n" + chain)
        dedup = (args.dedup_key or "").strip() or None
        # Famille sync tours / QC
        job_l = args.job.lower()
        if not dedup and "sync tours" in job_l:
            dedup = "qc_post_sync_fail"
        send_ops_alert(
            f"{args.job} — ÉCHEC (code {proc.returncode})",
            "\n\n".join(body_parts),
            dedup_key=dedup,
        )
        return int(proc.returncode)

    if args.success_alert:
        send_ops_alert(f"{args.job} — OK", "", force=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
