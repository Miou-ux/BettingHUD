#!/usr/bin/env python3
"""QC post-sync : fraîcheur DB tours + âge feature store."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.qc_common import QcReport

PARIS_TZ = ZoneInfo("Europe/Paris")
FEATURE_STORE_PATH = os.path.join(ROOT, "data", "cache", "player_feature_store.joblib")


def _max_lag_days() -> int:
    try:
        return max(1, int(os.getenv("BETTINGHUD_MORNING_TOURS_MAX_LAG_DAYS", "5")))
    except ValueError:
        return 5


def _feature_store_max_age_hours() -> float:
    try:
        return max(1.0, float(os.getenv("BETTINGHUD_QC_FEATURE_STORE_MAX_AGE_HOURS", "36")))
    except ValueError:
        return 36.0


def _date_int(val: object) -> int | None:
    s = str(val or "").strip().replace("-", "")[:8]
    return int(s) if s.isdigit() and len(s) == 8 else None


def _qc_alert_enabled() -> bool:
    return os.getenv("BETTINGHUD_WTA_QC_ALERT", "1").strip().lower() not in ("0", "false", "no")


def _send_qc_ops_alert(report: QcReport) -> None:
    if not _qc_alert_enabled():
        return
    if report.ok and not report.warnings:
        return
    try:
        from scripts.ops_telegram_alert import send_ops_alert

        lines = report.summary_lines()[1:6]
        body = "\n".join(lines)
        if report.blocking:
            send_ops_alert("QC post-sync FAIL", body)
        elif report.warnings:
            send_ops_alert("QC post-sync WARN", body)
    except Exception:
        pass


def run_qc_post_sync(db_path: str | None = None) -> QcReport:
    from scripts.bets_db import DB_PATH_DEFAULT, get_data_freshness_snapshot

    report = QcReport("post_sync")
    dbp = db_path or (
        os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    )
    cutoff = int(
        (datetime.now(PARIS_TZ).date() - timedelta(days=_max_lag_days())).strftime("%Y%m%d")
    )

    conn = sqlite3.connect(dbp)
    try:
        atp_max = _date_int(conn.execute("SELECT MAX(tourney_date) FROM matches_recent").fetchone()[0])
        wta_max = _date_int(conn.execute("SELECT MAX(tourney_date) FROM wta_matches").fetchone()[0])
    finally:
        conn.close()

    if atp_max is None or atp_max < cutoff:
        report.add_blocking(
            "atp_stale",
            f"ATP matches_recent max={atp_max} < cutoff {cutoff}",
            atp_max=atp_max,
            cutoff=cutoff,
        )
    if wta_max is None or wta_max < cutoff:
        report.add_blocking(
            "wta_stale",
            f"WTA wta_matches max={wta_max} < cutoff {cutoff}",
            wta_max=wta_max,
            cutoff=cutoff,
        )

    fresh = get_data_freshness_snapshot(dbp)
    tours_iso = str(fresh.get("last_tours_sync_iso") or "")
    cal = datetime.now(PARIS_TZ).date().isoformat()
    if not tours_iso or tours_iso[:10] != cal:
        report.add_warning(
            "tours_sync_meta_stale",
            f"last_tours_sync_iso pas aujourd'hui ({tours_iso or '—'})",
        )

    if os.path.isfile(FEATURE_STORE_PATH):
        age_h = (time.time() - os.path.getmtime(FEATURE_STORE_PATH)) / 3600.0
        max_h = _feature_store_max_age_hours()
        if age_h > max_h:
            report.add_warning(
                "feature_store_old",
                f"player_feature_store.joblib âge {age_h:.1f}h > {max_h:.0f}h",
                age_hours=round(age_h, 2),
            )
    else:
        report.add_warning("feature_store_missing", "player_feature_store.joblib absent")

    try:
        from scripts.wta_delta_qc_gates import run_wta_delta_qc_gates

        wta_gates = run_wta_delta_qc_gates(db_path=dbp)
        report.merge(wta_gates)
    except Exception as exc:
        report.add_warning("wta_delta_gates_error", str(exc)[:200])

    _send_qc_ops_alert(report)
    return report


def main() -> int:
    report = run_qc_post_sync()
    for line in report.summary_lines():
        print(line, flush=True)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
