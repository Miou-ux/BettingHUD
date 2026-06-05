#!/usr/bin/env python3
"""Vérifie que scrape TE + snapshot matin sont prêts avant le tweet CourtAlphaX.

Contrôles (Europe/Paris) :
  - pas de build snapshot en cours
  - CSV prematch TE récent (data/scraped/*.csv)
  - snapshot full présent, construit aujourd'hui, âge ≤ seuil
  - optionnel : log pipeline matin contient « Phase build terminée »

Usage :
  py -3 scripts/courtalphax_preflight.py
  py -3 scripts/courtalphax_preflight.py --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

PARIS_TZ = ZoneInfo("Europe/Paris")
MORNING_BUILD_LOG = os.path.join("data", "logs", "morning_build_cron.log")
DEFAULT_MAX_AGE_MIN = 150.0


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _latest_scrape_csv() -> tuple[str, float]:
    files = glob.glob(os.path.join("data", "scraped", "*.csv"))
    if not files:
        return "", 0.0
    best_mtime = 0.0
    best_path = ""
    for fp in files:
        try:
            mt = float(os.path.getmtime(fp))
        except OSError:
            continue
        if mt >= best_mtime:
            best_mtime = mt
            best_path = fp
    return best_path, best_mtime


def _paris_date_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, PARIS_TZ).date().isoformat()


def _age_min(ts: float) -> float | None:
    if ts <= 0:
        return None
    return max(0.0, (time.time() - ts) / 60.0)


def _morning_build_ok() -> tuple[bool, str]:
    if not os.path.isfile(MORNING_BUILD_LOG):
        return False, f"log absent ({MORNING_BUILD_LOG})"
    try:
        with open(MORNING_BUILD_LOG, encoding="utf-8", errors="replace") as f:
            tail = f.read()[-12000:]
    except OSError as exc:
        return False, f"log illisible: {exc}"
    if "Phase build terminée" not in tail:
        return False, "« Phase build terminée » introuvable dans le log matin"
    if "Build snapshot échec" in tail.split("Phase build terminée")[-1]:
        return False, "échec build après dernier succès logué"
    return True, "pipeline matin OK (log)"


def check_courtalphax_preflight(
    *,
    calendar_date: str | None = None,
    max_snapshot_age_min: float | None = None,
    max_scrape_age_min: float | None = None,
    require_morning_log: bool = False,
) -> dict:
    cal_day = calendar_date or datetime.now(PARIS_TZ).date().isoformat()
    max_snap = max_snapshot_age_min if max_snapshot_age_min is not None else _env_float(
        "COURTALPHAX_MAX_SNAPSHOT_AGE_MIN", DEFAULT_MAX_AGE_MIN
    )
    max_scrape = max_scrape_age_min if max_scrape_age_min is not None else _env_float(
        "COURTALPHAX_MAX_SCRAPE_AGE_MIN", DEFAULT_MAX_AGE_MIN
    )

    from scripts.live_snapshot import load_latest_live_snapshot, snapshot_build_in_progress

    checks: dict[str, dict] = {}
    messages: list[str] = []

    build_busy = snapshot_build_in_progress()
    checks["snapshot_not_building"] = {
        "ok": not build_busy,
        "detail": "build en cours" if build_busy else "aucun build en cours",
    }
    if build_busy:
        messages.append("Snapshot en cours de construction — attendre.")

    csv_path, csv_mtime = _latest_scrape_csv()
    csv_age = _age_min(csv_mtime)
    csv_today = _paris_date_from_ts(csv_mtime) == cal_day if csv_mtime > 0 else False
    scrape_ok = bool(csv_path) and csv_today and csv_age is not None and csv_age <= max_scrape
    checks["te_scrape_csv"] = {
        "ok": scrape_ok,
        "path": csv_path or None,
        "mtime_paris": _paris_date_from_ts(csv_mtime) if csv_mtime else None,
        "age_min": round(csv_age, 1) if csv_age is not None else None,
        "max_age_min": max_scrape,
    }
    if not scrape_ok:
        if not csv_path:
            messages.append("Aucun CSV prematch TE dans data/scraped/.")
        elif not csv_today:
            messages.append(f"CSV TE pas du jour ({checks['te_scrape_csv']['mtime_paris']} ≠ {cal_day}).")
        else:
            messages.append(
                f"CSV TE trop ancien (~{csv_age:.0f} min > {max_scrape:.0f} min)."
            )

    matches, meta = load_latest_live_snapshot(max_age_sec=24 * 3600)
    built_at = float((meta or {}).get("built_at") or 0.0)
    snap_age = _age_min(built_at)
    built_today = _paris_date_from_ts(built_at) == cal_day if built_at > 0 else False
    n_matches = len(matches or [])
    snap_ok = (
        n_matches > 0
        and built_today
        and snap_age is not None
        and snap_age <= max_snap
        and not build_busy
    )
    checks["snapshot_full"] = {
        "ok": snap_ok,
        "n_matches": n_matches,
        "built_at_paris": _paris_date_from_ts(built_at) if built_at else None,
        "age_min": round(snap_age, 1) if snap_age is not None else None,
        "max_age_min": max_snap,
    }
    if not snap_ok:
        if n_matches <= 0:
            messages.append("Snapshot vide ou absent.")
        elif not built_today:
            messages.append(
                f"Snapshot pas reconstruit aujourd'hui "
                f"({checks['snapshot_full']['built_at_paris']} ≠ {cal_day})."
            )
        elif snap_age is not None and snap_age > max_snap:
            messages.append(
                f"Snapshot trop ancien (~{snap_age:.0f} min > {max_snap:.0f} min)."
            )

    log_ok, log_detail = _morning_build_ok()
    checks["morning_build_log"] = {"ok": log_ok, "detail": log_detail}
    if require_morning_log and not log_ok:
        messages.append(log_detail)

    hard_ok = checks["snapshot_not_building"]["ok"] and checks["te_scrape_csv"]["ok"] and checks["snapshot_full"]["ok"]
    if require_morning_log:
        hard_ok = hard_ok and log_ok

    return {
        "ok": hard_ok,
        "ready": hard_ok,
        "calendar_date": cal_day,
        "checks": checks,
        "messages": messages,
        "summary": "OK — prêt pour CourtAlphaX" if hard_ok else "KO — " + "; ".join(messages),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="CourtAlphaX — vérif scrape TE + snapshot avant tweet")
    ap.add_argument("--json", action="store_true", help="Sortie JSON")
    ap.add_argument("--require-morning-log", action="store_true", help="Exiger succès log 02:00")
    ap.add_argument("--max-snapshot-age-min", type=float, default=None)
    ap.add_argument("--max-scrape-age-min", type=float, default=None)
    args = ap.parse_args()

    report = check_courtalphax_preflight(
        max_snapshot_age_min=args.max_snapshot_age_min,
        max_scrape_age_min=args.max_scrape_age_min,
        require_morning_log=args.require_morning_log,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(report["summary"])
        for name, chk in report["checks"].items():
            flag = "OK" if chk.get("ok") else "KO"
            detail = chk.get("detail") or chk
            print(f"  [{flag}] {name}: {detail}")

    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
