#!/usr/bin/env python3
"""Weekly Telegram admin report: Top5 prod baseline vs shadow candidate."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from scripts.bets_db import DB_PATH_DEFAULT
from scripts.shadow_top5 import (
    PROD_STRATEGY_KEY,
    STRATEGY_B_KEY,
    STRATEGY_KEY,
    TABLE,
    sync_shadow_results,
)


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


def _window_dates(today: date | None = None) -> tuple[str, str]:
    t = today or date.today()
    end = t - timedelta(days=1)
    start = end - timedelta(days=6)
    return start.isoformat(), end.isoformat()


def _metrics(conn: sqlite3.Connection, strategy_key: str, start: str, end: str) -> dict:
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS n_total,
            SUM(CASE WHEN status IN ('Gagné','Perdu','Annulé') THEN 1 ELSE 0 END) AS n_settled,
            SUM(CASE WHEN status='Gagné' THEN 1 ELSE 0 END) AS n_wins,
            SUM(CASE WHEN status='Perdu' THEN 1 ELSE 0 END) AS n_losses,
            SUM(CASE WHEN status='Annulé' THEN 1 ELSE 0 END) AS n_void,
            SUM(COALESCE(theoretical_profit,0)) AS kelly_profit
        FROM {TABLE}
        WHERE strategy_key = ?
          AND calendar_date >= ?
          AND calendar_date <= ?
        """,
        (strategy_key, start, end),
    ).fetchone()
    n_set = int(row["n_settled"] or 0)
    n_w = int(row["n_wins"] or 0)
    hit = (n_w / n_set * 100.0) if n_set else 0.0

    pnl_flat = conn.execute(
        f"""
        SELECT
            SUM(
                CASE
                    WHEN status='Gagné' THEN 5.0 * (COALESCE(odd_fav,0) - 1.0)
                    WHEN status='Perdu' THEN -5.0
                    ELSE 0.0
                END
            ) AS flat_pnl
        FROM {TABLE}
        WHERE strategy_key = ?
          AND calendar_date >= ?
          AND calendar_date <= ?
          AND status IN ('Gagné','Perdu','Annulé')
        """,
        (strategy_key, start, end),
    ).fetchone()
    flat = float((pnl_flat["flat_pnl"] if pnl_flat else 0.0) or 0.0)
    flat_roi = (flat / (n_set * 5.0) * 100.0) if n_set else 0.0

    return {
        "strategy_key": strategy_key,
        "n_total": int(row["n_total"] or 0),
        "n_settled": n_set,
        "hit_pct": hit,
        "flat_pnl": flat,
        "flat_roi_pct": flat_roi,
        "kelly_profit": float(row["kelly_profit"] or 0.0),
    }


def _go_no_go(prod: dict, shadow: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if shadow["n_settled"] < 10:
        reasons.append("sample trop faible (moins de 10 settled)")
    if shadow["flat_roi_pct"] <= prod["flat_roi_pct"]:
        reasons.append("flat ROI shadow inferieur ou egal a prod")
    if shadow["kelly_profit"] <= prod["kelly_profit"]:
        reasons.append("kelly profit shadow inferieur ou egal a prod")
    if reasons:
        return "NO-GO", reasons
    return "GO", ["shadow > prod en flat ROI et Kelly profit"]


def _action_reco(prod: dict, a: dict, b: dict) -> tuple[str, str]:
    def beats(m: dict) -> bool:
        return (
            m["n_settled"] >= 10
            and m["flat_roi_pct"] > prod["flat_roi_pct"]
            and m["kelly_profit"] > prod["kelly_profit"]
        )

    a_beats = beats(a)
    b_beats = beats(b)
    if a_beats and b_beats:
        if a["flat_roi_pct"] >= b["flat_roi_pct"]:
            return "SWITCH", f"basculer vers Shadow A ({STRATEGY_KEY})"
        return "SWITCH", f"basculer vers Shadow B ({STRATEGY_B_KEY})"
    if a_beats:
        return "TEST+", f"continuer validation renforcee de Shadow A ({STRATEGY_KEY})"
    if b_beats:
        return "TEST+", f"continuer validation renforcee de Shadow B ({STRATEGY_B_KEY})"
    return "KEEP", "garder prod, echantillon insuffisant ou perfs inferieures"


def format_report(prod: dict, a: dict, b: dict, start: str, end: str) -> str:
    dec_a, reasons_a = _go_no_go(prod, a)
    dec_b, reasons_b = _go_no_go(prod, b)
    action, action_detail = _action_reco(prod, a, b)
    icon = "✅" if action == "SWITCH" else ("🟡" if action == "TEST+" else "⚪")
    now = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")

    def delta_line(label: str, m: dict) -> str:
        d_flat_roi = m["flat_roi_pct"] - prod["flat_roi_pct"]
        d_kelly = m["kelly_profit"] - prod["kelly_profit"]
        d_hit = m["hit_pct"] - prod["hit_pct"]
        return f"{label} delta vs Prod: hit {d_hit:+.1f} pp | flatROI {d_flat_roi:+.1f} pp | Kelly {d_kelly:+.1f} EUR"

    out = [
        "Shadow Top5 hebdo A/B",
        f"Horodatage: {now}",
        f"Periode: {start} -> {end}",
        "",
        "Comparatif",
        (
            "Prod: "
            f"n={prod['n_total']} | settled={prod['n_settled']} | hit={prod['hit_pct']:.1f}% | "
            f"flatROI={prod['flat_roi_pct']:+.1f}% | Kelly={prod['kelly_profit']:+.1f} EUR"
        ),
        (
            "Shadow A (ev25 rel85): "
            f"n={a['n_total']} | settled={a['n_settled']} | hit={a['hit_pct']:.1f}% | "
            f"flatROI={a['flat_roi_pct']:+.1f}% | Kelly={a['kelly_profit']:+.1f} EUR"
        ),
        (
            "Shadow B (p80 ev15-30): "
            f"n={b['n_total']} | settled={b['n_settled']} | hit={b['hit_pct']:.1f}% | "
            f"flatROI={b['flat_roi_pct']:+.1f}% | Kelly={b['kelly_profit']:+.1f} EUR"
        ),
        delta_line("A", a),
        delta_line("B", b),
        "",
        f"{icon} Action recommandee: {action}",
        f"Detail: {action_detail}",
        f"Verdict A: {dec_a}",
    ]
    for r in reasons_a:
        out.append(f"· A: {r}")
    out.append(f"Verdict B: {dec_b}")
    for r in reasons_b:
        out.append(f"· B: {r}")
    return "\n".join(out)


def run_weekly_notify(*, dry_run: bool = False, db_path: str | None = None) -> dict:
    path = db_path or str(ROOT / DB_PATH_DEFAULT)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        # Ensure latest statuses before reporting.
        sync_shadow_results(db_path=path, strategy_key=None)
        start, end = _window_dates()
        prod = _metrics(conn, PROD_STRATEGY_KEY, start, end)
        shadow_a = _metrics(conn, STRATEGY_KEY, start, end)
        shadow_b = _metrics(conn, STRATEGY_B_KEY, start, end)
    finally:
        conn.close()

    text = format_report(prod, shadow_a, shadow_b, start, end)
    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(text)
        return {"ok": True, "dry_run": True, "start": start, "end": end}

    from scripts.telegram_top5_notify import send_telegram_message

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = _admin_chat_ids()
    if not token or not chats:
        return {"ok": False, "reason": "missing_telegram_config"}
    sent = 0
    for c in chats:
        send_telegram_message(text, token=token, chat_id=c)
        sent += 1
    return {"ok": True, "sent": sent, "start": start, "end": end}


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Weekly shadow vs prod report to Telegram admin.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = run_weekly_notify(dry_run=args.dry_run)
    print(out)
    if not out.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
