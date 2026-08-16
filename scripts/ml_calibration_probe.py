#!/usr/bin/env python3
"""Sonde calibration ML live — picks Top5 prod réglés (daily_top_proba_picks).

Calcule Brier + écart proba/hit sur fenêtres glissantes et compare au hold-out train.
Usage :
  python scripts/ml_calibration_probe.py --dry-run
  python scripts/ml_calibration_probe.py --json
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PARIS_TZ = ZoneInfo("Europe/Paris")
CACHE_PATH = ROOT / "data" / "cache" / "ml_calibration_probe_latest.json"
DEFAULT_MODEL = ROOT / "models" / "xgb_model_tml_v47.pkl"

CALIB_BUCKETS = (
    (0.80, 0.85, "80-85%"),
    (0.85, 0.90, "85-90%"),
    (0.90, 1.01, "90%+"),
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _train_brier_ref(model_path: Path | None = None) -> float:
    path = model_path or DEFAULT_MODEL
    if not path.is_file():
        return _env_float("BETTINGHUD_CALIB_BRIER_REF", 0.174)
    try:
        import joblib

        obj = joblib.load(path)
        if isinstance(obj, dict):
            v = obj.get("global_test_brier")
        else:
            v = getattr(obj, "global_test_brier", None)
        if v is not None:
            return float(v)
    except Exception:
        pass
    return _env_float("BETTINGHUD_CALIB_BRIER_REF", 0.174)


def _pick_calendar_date(p: dict) -> date | None:
    raw = str(p.get("calendar_date") or p.get("match_date") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_void(p: dict) -> bool:
    st = str(p.get("status") or "").strip().lower()
    return st in ("annulé", "annule", "void", "cancelled", "canceled")


def _is_settled(p: dict) -> bool:
    if _is_void(p):
        return False
    st = str(p.get("status") or "").strip()
    if st in ("Gagné", "Perdu"):
        return True
    if p.get("fav_won") is not None:
        return True
    if p.get("settled") is True:
        return True
    return False


def _pick_won(p: dict) -> bool:
    if p.get("fav_won") is not None:
        return bool(int(p.get("fav_won")))
    st = str(p.get("status") or "").strip()
    return st == "Gagné"


def _pick_prob(p: dict) -> float:
    try:
        return float(p.get("p_model_fav") or p.get("p_model") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def load_prod_top5_picks(*, db_path: str | None = None) -> list[dict]:
    from collections import defaultdict

    from scripts.bets_db import DB_PATH_DEFAULT, open_db, read_daily_top_proba_picks, sync_daily_top_proba_from_results
    from scripts.hybrid_pick_selection import select_hybrid_picks
    from scripts.match_rank_quality import duplicate_model_prob_keys

    dbp = db_path or os.path.join(ROOT, DB_PATH_DEFAULT)
    conn = open_db(dbp)
    try:
        sync_daily_top_proba_from_results(conn)
    finally:
        conn.close()
    raw = read_daily_top_proba_picks(db_path=dbp)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in raw:
        by_day[str(r.get("calendar_date") or "")[:10]].append(dict(r))
    out: list[dict] = []
    for day in sorted(by_day):
        day_rows = by_day[day]
        dup = duplicate_model_prob_keys(day_rows)
        out.extend(select_hybrid_picks(day_rows, limit=5, duplicate_keys=dup))
    return out


def _filter_picks(
    picks: list[dict],
    *,
    days: int | None = None,
    year: int | None = None,
    today: date | None = None,
) -> list[dict]:
    today = today or datetime.now(PARIS_TZ).date()
    out: list[dict] = []
    for p in picks:
        cd = _pick_calendar_date(p)
        if cd is None:
            continue
        if year is not None and cd.year != year:
            continue
        if days is not None and (today - cd).days > days:
            continue
        out.append(p)
    return out


def compute_window_metrics(picks: list[dict]) -> dict[str, Any]:
    settled = [p for p in picks if _is_settled(p)]
    n_settled = len(settled)
    if not n_settled:
        return {
            "n_picks": len(picks),
            "n_settled": 0,
            "brier": None,
            "pred_avg": None,
            "hit_rate": None,
            "cal_gap_pp": None,
            "high_bucket": None,
        }

    probs = [_pick_prob(p) for p in settled]
    outcomes = [1.0 if _pick_won(p) else 0.0 for p in settled]
    pred_avg = sum(probs) / n_settled
    hit_rate = sum(outcomes) / n_settled
    brier = sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / n_settled

    high = [p for p in settled if _pick_prob(p) >= 0.80]
    high_n = len(high)
    high_metrics = None
    if high_n:
        h_probs = [_pick_prob(p) for p in high]
        h_out = [1.0 if _pick_won(p) else 0.0 for p in high]
        high_metrics = {
            "n": high_n,
            "pred_avg": sum(h_probs) / high_n,
            "hit_rate": sum(h_out) / high_n,
            "cal_gap_pp": (sum(h_out) / high_n - sum(h_probs) / high_n) * 100.0,
            "brier": sum((p - y) ** 2 for p, y in zip(h_probs, h_out)) / high_n,
        }

    buckets: list[dict[str, Any]] = []
    for lo, hi, label in CALIB_BUCKETS:
        sub = [p for p in settled if lo <= _pick_prob(p) < hi]
        if not sub:
            buckets.append({"label": label, "n": 0})
            continue
        sn = len(sub)
        sp = sum(_pick_prob(p) for p in sub) / sn
        sh = sum(1.0 if _pick_won(p) else 0.0 for p in sub) / sn
        buckets.append(
            {
                "label": label,
                "n": sn,
                "pred_avg": sp,
                "hit_rate": sh,
                "cal_gap_pp": (sh - sp) * 100.0,
            }
        )

    dates = [_pick_calendar_date(p) for p in settled]
    dates = [d for d in dates if d is not None]

    return {
        "n_picks": len(picks),
        "n_settled": n_settled,
        "brier": brier,
        "pred_avg": pred_avg,
        "hit_rate": hit_rate,
        "cal_gap_pp": (hit_rate - pred_avg) * 100.0,
        "high_bucket": high_metrics,
        "buckets": buckets,
        "last_settled": max(dates).isoformat() if dates else None,
    }


def _window_alerts(
    label: str,
    metrics: dict[str, Any],
    *,
    brier_ref: float,
    min_n: int,
    brier_warn_delta: float,
    brier_alert_delta: float,
    gap_warn_pp: float,
) -> list[str]:
    alerts: list[str] = []
    n = int(metrics.get("n_settled") or 0)
    if n < min_n:
        return alerts
    brier = metrics.get("brier")
    if brier is not None:
        delta = float(brier) - brier_ref
        if delta >= brier_alert_delta:
            alerts.append(
                f"{label}: Brier live {float(brier):.4f} (+{delta:.4f} vs train {brier_ref:.4f})"
            )
        elif delta >= brier_warn_delta:
            alerts.append(
                f"{label}: Brier live {float(brier):.4f} (+{delta:.4f} vs train, watch)"
            )
    gap = metrics.get("cal_gap_pp")
    if gap is not None and abs(float(gap)) >= gap_warn_pp:
        direction = "sous-perf" if float(gap) < 0 else "sur-perf"
        alerts.append(
            f"{label}: écart calibration {float(gap):+.1f} pp ({direction}, n={n})"
        )
    high = metrics.get("high_bucket") or {}
    hg = high.get("cal_gap_pp")
    hn = int(high.get("n") or 0)
    if hn >= max(3, min_n // 2) and hg is not None and abs(float(hg)) >= gap_warn_pp:
        alerts.append(
            f"{label} P≥80%: écart {float(hg):+.1f} pp (n={hn})"
        )
    return alerts


def collect_calibration_probe(
    *,
    db_path: str | None = None,
    model_path: Path | None = None,
) -> dict[str, Any]:
    today = datetime.now(PARIS_TZ)
    brier_ref = _train_brier_ref(model_path)
    min_n = _env_int("BETTINGHUD_CALIB_MIN_N", 8)
    brier_warn = _env_float("BETTINGHUD_CALIB_BRIER_WARN_DELTA", 0.03)
    brier_alert = _env_float("BETTINGHUD_CALIB_BRIER_ALERT_DELTA", 0.05)
    gap_warn = _env_float("BETTINGHUD_CALIB_GAP_WARN_PP", 8.0)

    picks = load_prod_top5_picks(db_path=db_path)
    windows = {
        "7d": compute_window_metrics(_filter_picks(picks, days=7)),
        "30d": compute_window_metrics(_filter_picks(picks, days=30)),
        "ytd": compute_window_metrics(_filter_picks(picks, year=today.year)),
    }

    alerts: list[str] = []
    for key in ("7d", "30d"):
        alerts.extend(
            _window_alerts(
                key,
                windows[key],
                brier_ref=brier_ref,
                min_n=min_n,
                brier_warn_delta=brier_warn,
                brier_alert_delta=brier_alert,
                gap_warn_pp=gap_warn,
            )
        )

    return {
        "ts": today.isoformat(),
        "brier_ref_train": brier_ref,
        "thresholds": {
            "min_n": min_n,
            "brier_warn_delta": brier_warn,
            "brier_alert_delta": brier_alert,
            "gap_warn_pp": gap_warn,
        },
        "windows": windows,
        "alerts": alerts,
        "status": "alert" if any("Brier live" in a and "watch" not in a for a in alerts) else (
            "warn" if alerts else "ok"
        ),
        "n_prod_picks_total": len(picks),
    }


def save_probe_cache(report: dict[str, Any], path: Path | None = None) -> Path:
    out = path or CACHE_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def format_calibration_telegram(report: dict[str, Any]) -> str:
    ts = datetime.now(PARIS_TZ).strftime("%d/%m/%Y %H:%M")
    ref = float(report.get("brier_ref_train") or 0.174)
    status = str(report.get("status") or "ok")
    icon = "✅" if status == "ok" else ("⚠️" if status == "warn" else "🔴")

    lines = [
        f"<b>{icon} Calibration ML live</b>",
        f"<i>{ts} · Top5 prod réglés</i>",
        f"Réf. train Brier : <b>{ref:.4f}</b>",
        "",
    ]

    for key, label in (("7d", "7 jours"), ("30d", "30 jours"), ("ytd", "YTD")):
        m = (report.get("windows") or {}).get(key) or {}
        n = int(m.get("n_settled") or 0)
        if not n:
            lines.append(f"<b>{label}</b> : pas encore de picks réglés")
            continue
        brier = m.get("brier")
        gap = m.get("cal_gap_pp")
        hit = m.get("hit_rate")
        pred = m.get("pred_avg")
        hit_txt = f"{float(hit)*100:.0f}%" if hit is not None else "—"
        pred_txt = f"{float(pred)*100:.0f}%" if pred is not None else "—"
        lines.append(f"<b>{label}</b> : {n} picks réglés · taux réel <b>{hit_txt}</b> (modèle {pred_txt})")
        if brier is not None:
            delta = float(brier) - ref
            mood = "✅" if abs(delta) < 0.02 else ("⚠️" if abs(delta) < 0.04 else "🔴")
            lines.append(f"  Écart vs entraînement : {delta:+.3f} {mood}")
        if pred is not None and hit is not None and gap is not None:
            lines.append(f"  Sur-confiance / sous-confiance : {float(gap):+.1f} pts")
        high = m.get("high_bucket") or {}
        if int(high.get("n") or 0) >= 3:
            lines.append(
                f"  Picks ≥80% proba : {high['n']} · écart {float(high.get('cal_gap_pp') or 0):+.1f} pts"
            )

    alerts = report.get("alerts") or []
    lines.append("")
    if alerts:
        lines.append("<b>Points d'attention</b>")
        for a in alerts[:6]:
            lines.append(f"· {a}")
        if len(alerts) > 6:
            lines.append(f"<i>… +{len(alerts) - 6} autre(s)</i>")
    else:
        lines.append("<b>✅ Rien d'inquiétant</b> sur les 7 et 30 derniers jours")

    return "\n".join(lines)


def run_calibration_probe_notify(*, dry_run: bool = False, db_path: str | None = None) -> dict[str, Any]:
    report = collect_calibration_probe(db_path=db_path)
    save_probe_cache(report)
    text = format_calibration_telegram(report)

    if dry_run:
        print(
            text.replace("<b>", "**")
            .replace("</b>", "**")
            .replace("<i>", "_")
            .replace("</i>", "_")
            .replace("&amp;", "&")
        )
        return {"ok": True, "dry_run": True, "status": report.get("status"), "alerts": len(report.get("alerts") or [])}

    from scripts.ml_weekly_telegram_notify import _admin_chat_ids
    from scripts.telegram_top5_notify import send_telegram_message

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = _admin_chat_ids()
    if not token or not chats:
        return {"ok": False, "reason": "missing_telegram_config", "status": report.get("status")}

    sent = 0
    for chat_id in chats:
        send_telegram_message(text, token=token, chat_id=chat_id)
        sent += 1

    if report.get("status") == "alert":
        try:
            from scripts.ops_telegram_alert import send_ops_alert

            body = "\n".join((report.get("alerts") or [])[:5])
            send_ops_alert("Calibration ML — dérive", body)
        except Exception:
            pass

    return {
        "ok": True,
        "sent": sent,
        "status": report.get("status"),
        "alerts": len(report.get("alerts") or []),
    }


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    import argparse

    ap = argparse.ArgumentParser(description="Sonde calibration ML live → Telegram admin")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="Imprimer le rapport JSON (pas d'envoi TG)")
    ap.add_argument("--db-path", default="")
    args = ap.parse_args()

    db_path = args.db_path.strip() or None
    if args.json:
        report = collect_calibration_probe(db_path=db_path)
        save_probe_cache(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    out = run_calibration_probe_notify(dry_run=args.dry_run, db_path=db_path)
    print(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
