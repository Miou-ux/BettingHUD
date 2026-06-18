#!/usr/bin/env python3
"""Rapport hebdomadaire ML + WTA → Telegram admin (lundi matin).

Synthèse Brier (focus WTA), fraîcheur données, état des crons sync/train.
Usage :
  python scripts/ml_weekly_telegram_notify.py --dry-run
  python scripts/ml_weekly_telegram_notify.py
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

DEFAULT_MODEL = ROOT / "models" / "xgb_model_tml_v47.pkl"
BASELINE_MODEL = ROOT / "models" / "xgb_model_tml_v47_pre_wta_delta.pkl"
TOURS_LOG = ROOT / "data" / "logs" / "tours_auto_sync.log"
ML_TRAIN_LOG = ROOT / "data" / "logs" / "ml_train_cron.log"

WTA_BRIER_KEYS = (
    "tour_WTA",
    "WTA_Hard",
    "WTA_Clay",
    "WTA_Hard_G",
    "WTA_Clay_G",
    "WTA_Grass",
    "WTA_Clay_M",
)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _fmt_age(dt: datetime | None, *, now: datetime | None = None) -> str:
    if dt is None:
        return "inconnu"
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    h = (now - dt).total_seconds() / 3600.0
    if h < 1:
        return f"{int(h * 60)} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} j"


def _status_icon(ok: bool | None) -> str:
    if ok is True:
        return "✅"
    if ok is False:
        return "🔴"
    return "🟡"


def _admin_chat_ids() -> list[str]:
    from scripts.telegram_access import admin_notify_chat_id

    primary = admin_notify_chat_id()
    raw = (os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()
    ids: list[str] = []
    if primary:
        ids.append(primary)
    for bit in raw.replace(";", ",").split(","):
        b = bit.strip()
        if b and b not in ids:
            ids.append(b)
    return ids


def _read_log_tail(path: Path, n: int = 80) -> list[str]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []


def _parse_last_tours_run(lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"found": False}
    for line in reversed(lines):
        m = re.search(r"=== fin sync ATP\+WTA rc=(\d+)", line)
        if m:
            out["found"] = True
            out["rc"] = int(m.group(1))
            out["line"] = line
            break
    block: list[str] = []
    in_block = False
    for line in reversed(lines):
        if "=== fin sync ATP+WTA" in line:
            in_block = True
            block.append(line)
            continue
        if in_block:
            block.append(line)
            if "=== debut sync ATP+WTA" in line:
                break
    block.reverse()
    text = "\n".join(block)
    out["wta_delta"] = "sync_wta_delta.py OK" in text
    out["wta_delta_fail"] = "sync_wta_delta.py a echoue" in text or "sync_wta_delta.py a échoué" in text
    out["wta_legacy"] = "fetch_wta_sackmann_raw.py" in text
    out["wta_legacy_fail"] = "fetch_wta_sackmann_raw.py a échoué" in text or "fetch_wta_sackmann_raw.py a echoue" in text
    out["tml_fail"] = "sync_tml_recent.py rapporte erreur" in text
    out["pq_fail"] = "pipeline_quality.py rapporte erreur" in text
    if block:
        ts_m = re.match(r"\[([^\]]+)\]", block[0])
        if ts_m:
            out["started"] = ts_m.group(1)
    return out


def _parse_last_ml_train_log(lines: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"found": False}
    for line in reversed(lines):
        if "TERMINÉ" in line or "Fin entraînement ML" in line:
            out["found"] = True
            out["ok"] = "TERMINÉ" in line or "Fin entraînement ML" in line
            ts_m = re.match(r"\[([^\]]+)\]", line)
            if ts_m:
                out["ts"] = ts_m.group(1)
            break
        if "Traceback" in line or "Error" in line:
            out["found"] = True
            out["ok"] = False
            break
    return out


def _wta_db_stats(db_path: str, *, cutoff: int = 20260526) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT MAX(tourney_date), COUNT(*)
            FROM wta_matches
            """
        ).fetchone()
        max_raw, total = row[0], int(row[1] or 0)
        max_s = str(max_raw or "")[:10].replace("-", "")
        recent = conn.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN w_svpt IS NULL OR TRIM(CAST(w_svpt AS TEXT)) = '' THEN 1 ELSE 0 END)
            FROM wta_matches
            WHERE CAST(REPLACE(SUBSTR(CAST(tourney_date AS TEXT), 1, 10), '-', '') AS INTEGER) >= ?
            """,
            (cutoff,),
        ).fetchone()
        delta_n = int(recent[0] or 0)
        null_n = int(recent[1] or 0)
        fill_pct = 100.0 * (delta_n - null_n) / delta_n if delta_n else None
        return {
            "total_rows": total,
            "max_date": max_s,
            "delta_rows": delta_n,
            "delta_null_svpt": null_n,
            "delta_fill_pct": fill_pct,
        }
    finally:
        conn.close()


def collect_report(
    *,
    model_path: Path = DEFAULT_MODEL,
    baseline_path: Path | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    from scripts.bets_db import DB_PATH_DEFAULT, get_data_freshness_snapshot
    from scripts.check_wta_brier_j6 import THRESHOLDS, _load_brier, compare_j6
    from scripts.wta_sackmann_common import DEFAULT_CUTOFF

    db_path = db_path or os.path.join(ROOT, DB_PATH_DEFAULT)
    now = datetime.now(timezone.utc)
    fresh = get_data_freshness_snapshot(db_path)
    alerts: list[str] = []

    model_path = Path(model_path)
    if not model_path.is_file():
        alerts.append("Bundle ML introuvable sur disque.")

    brier_current = _load_brier(model_path) if model_path.is_file() else {}
    j6 = None
    baseline_path = baseline_path or (BASELINE_MODEL if BASELINE_MODEL.is_file() else None)
    if baseline_path and baseline_path.is_file() and model_path.is_file():
        j6 = compare_j6(Path(baseline_path), model_path)

    tours_lines = _read_log_tail(TOURS_LOG, 120)
    ml_lines = _read_log_tail(ML_TRAIN_LOG, 40)
    tours_run = _parse_last_tours_run(tours_lines)
    ml_log = _parse_last_ml_train_log(ml_lines)

    tours_dt = _parse_iso(fresh.get("last_tours_sync_iso"))
    ml_dt = _parse_iso(fresh.get("last_ml_train_iso"))
    bundle_mtime = fresh.get("model_bundle_mtime")
    bundle_dt = (
        datetime.fromtimestamp(float(bundle_mtime), tz=timezone.utc) if bundle_mtime else None
    )

    tours_ok = tours_dt is not None and (now - tours_dt) < timedelta(hours=36)
    if not tours_ok:
        alerts.append(f"Sync tours > 36 h ({_fmt_age(tours_dt, now=now)}).")
    if tours_run.get("rc", 0) != 0:
        alerts.append(f"Dernier sync tours rc={tours_run.get('rc')} (log).")
    if tours_run.get("wta_delta_fail"):
        alerts.append("sync_wta_delta a échoué au dernier run.")
    if tours_run.get("wta_legacy") and not tours_run.get("wta_delta"):
        alerts.append("Ancien chemin fetch_wta_sackmann_raw encore utilisé (pas delta WTA).")
    if tours_run.get("tml_fail") or tours_run.get("pq_fail"):
        alerts.append("sync_tml_recent ou pipeline_quality en erreur au dernier run.")

    ml_ok = ml_dt is not None and (now - ml_dt) < timedelta(days=8)
    if not ml_ok:
        alerts.append(f"Dernier train ML > 8 j ({_fmt_age(ml_dt, now=now)}).")
    if ml_log.get("found") and ml_log.get("ok") is False:
        alerts.append("Dernière ligne ml_train_cron.log indique une erreur.")

    if bundle_dt and ml_dt and abs((bundle_dt - ml_dt).total_seconds()) > 86400:
        alerts.append("mtime bundle ML et last_ml_train_ts divergent (> 24 h).")

    wta = _wta_db_stats(db_path, cutoff=DEFAULT_CUTOFF)
    max_d = wta.get("max_date") or ""
    wta_stale = False
    if len(max_d) == 8:
        try:
            max_dt = datetime.strptime(max_d, "%Y%m%d").replace(tzinfo=timezone.utc)
            wta_stale = (now - max_dt) > timedelta(days=10)
            if wta_stale:
                alerts.append(f"Dernier match WTA en DB > 10 j ({max_d}).")
        except ValueError:
            pass

    fill = wta.get("delta_fill_pct")
    if fill is not None and fill < 85.0:
        alerts.append(f"Stats serve WTA delta < 85 % ({fill:.1f} %).")

    if j6 and not j6.get("go_prod"):
        alerts.append("Gate J6 FAIL vs baseline (régression Brier WTA).")

    return {
        "now": now,
        "fresh": fresh,
        "brier": brier_current,
        "j6": j6,
        "tours_run": tours_run,
        "ml_log": ml_log,
        "wta": wta,
        "alerts": alerts,
        "health_ok": len(alerts) == 0,
        "tours_ok": tours_ok,
        "ml_ok": ml_ok,
        "wta_stale": wta_stale,
        "model_path": str(model_path),
        "baseline_path": str(baseline_path) if baseline_path else None,
    }


def format_report_html(report: dict[str, Any]) -> str:
    fresh = report["fresh"]
    brier = report["brier"]
    seg = brier.get("segment_brier_scores") or {}
    wta = report["wta"]
    now = report["now"]
    if now.tzinfo:
        local_label = now.astimezone().strftime("%d/%m/%Y %H:%M")
    else:
        local_label = now.strftime("%d/%m/%Y %H:%M")

    lines = [
        "<b>📊 Rapport ML hebdo</b>",
        f"<i>{local_label}</i>",
        "",
        "<b>🎯 Brier (hold-out test)</b>",
    ]

    g = brier.get("global_test_brier")
    lines.append(f"Global : <b>{g:.4f}</b>" if g is not None else "Global : <b>n/a</b>")

    j6 = report.get("j6")
    if j6:
        for chk in j6.get("checks") or []:
            if chk.get("key") != "global_test_brier":
                continue
            d = chk.get("delta")
            st = chk.get("status", "?")
            icon = "✅" if st == "PASS" else "🔴"
            if d is not None:
                lines.append(f"  Δ vs baseline : {d:+.4f} {icon}")

    lines.append("")
    lines.append("<b>WTA</b>")
    tw = seg.get("tour_WTA")
    if tw is not None:
        lines.append(f"  tour_WTA : <b>{tw:.4f}</b>")
    for key in WTA_BRIER_KEYS:
        if key == "tour_WTA":
            continue
        v = seg.get(key)
        if v is not None:
            lines.append(f"  {key} : {v:.4f}")

    if j6:
        fails = [c for c in j6.get("checks") or [] if c.get("status") == "FAIL" and str(c.get("key", "")).startswith(("WTA", "tour_WTA"))]
        if fails:
            lines.append(f"  <i>J6 : {len(fails)} segment(s) hors seuil</i>")

    lines.extend(["", "<b>📅 Données &amp; jobs</b>"])

    tours_dt = _parse_iso(fresh.get("last_tours_sync_iso"))
    ml_dt = _parse_iso(fresh.get("last_ml_train_iso"))
    lines.append(
        f"Sync tours (03:30) : {_status_icon(report['tours_ok'])} "
        f"{fresh.get('last_tours_sync_iso') or '—'} ({_fmt_age(tours_dt, now=now)})"
    )
    tr = report.get("tours_run") or {}
    if tr.get("found"):
        wta_pipe = "delta WTA" if tr.get("wta_delta") else ("legacy fetch" if tr.get("wta_legacy") else "?")
        lines.append(f"  Dernier run log : rc={tr.get('rc', '?')} · {wta_pipe}")

    lines.append(
        f"Train ML (dim 04:00) : {_status_icon(report['ml_ok'])} "
        f"{fresh.get('last_ml_train_iso') or '—'} ({_fmt_age(ml_dt, now=now)})"
    )

    lm = fresh.get("last_wta_match") or {}
    max_d = wta.get("max_date") or "—"
    if len(str(max_d)) == 8:
        max_d = f"{max_d[0:4]}-{max_d[4:6]}-{max_d[6:8]}"
    lines.append(f"Dernier match WTA DB : <b>{max_d}</b>")
    if lm.get("winner_name"):
        lines.append(
            f"  {lm.get('tourney_name', '—')} · {lm.get('winner_name')} def {lm.get('loser_name')}"
        )

    fill = wta.get("delta_fill_pct")
    if fill is not None:
        icon = _status_icon(fill >= 85.0)
        lines.append(
            f"Stats serve delta (post-cutoff) : {icon} <b>{fill:.1f}%</b> "
            f"({wta.get('delta_rows', 0) - wta.get('delta_null_svpt', 0)}/{wta.get('delta_rows', 0)} matchs)"
        )

    lines.append(f"Bundle : <code>{Path(report['model_path']).name}</code>")

    alerts = report.get("alerts") or []
    lines.append("")
    if alerts:
        lines.append("<b>⚠️ Alertes</b>")
        for a in alerts[:8]:
            lines.append(f"· {a}")
        if len(alerts) > 8:
            lines.append(f"<i>… +{len(alerts) - 8} autre(s)</i>")
    else:
        lines.append("<b>✅ Aucune alerte</b> — jobs et Brier OK.")

    return "\n".join(lines)


def run_ml_weekly_telegram_notify(*, dry_run: bool = False) -> dict:
    report = collect_report()
    text = format_report_html(report)

    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(
            text.replace("<b>", "**")
            .replace("</b>", "**")
            .replace("<i>", "_")
            .replace("</i>", "_")
            .replace("<code>", "`")
            .replace("</code>", "`")
            .replace("&amp;", "&")
        )
        return {"ok": True, "dry_run": True, "alerts": len(report["alerts"]), "health_ok": report["health_ok"]}

    from scripts.telegram_top5_notify import send_telegram_message

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = _admin_chat_ids()
    if not token or not chats:
        return {"ok": False, "reason": "missing_telegram_config"}

    sent = 0
    for chat_id in chats:
        send_telegram_message(text, token=token, chat_id=chat_id)
        sent += 1
    return {
        "ok": True,
        "sent": sent,
        "alerts": len(report["alerts"]),
        "health_ok": report["health_ok"],
    }


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Rapport ML/WTA hebdo → Telegram admin.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = run_ml_weekly_telegram_notify(dry_run=args.dry_run)
    print(out)
    if not out.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
