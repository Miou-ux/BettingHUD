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
from scripts.shadow_top5 import PROD_STRATEGY_KEY, STRATEGY_KEY, TABLE, sync_shadow_results


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
        reasons.append("sample trop faible (<10 settled)")
    if shadow["flat_roi_pct"] <= prod["flat_roi_pct"]:
        reasons.append("flat ROI shadow <= prod")
    if shadow["kelly_profit"] <= prod["kelly_profit"]:
        reasons.append("kelly profit shadow <= prod")
    if reasons:
        return "NO-GO", reasons
    return "GO", ["shadow > prod en flat ROI et Kelly profit"]


def format_report(prod: dict, shadow: dict, start: str, end: str) -> str:
    decision, reasons = _go_no_go(prod, shadow)
    icon = "✅" if decision == "GO" else "🟡"
    now = datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")

    def line(label: str, m: dict) -> str:
        return (
            f"{label}: n={m['n_total']} settled={m['n_settled']} | "
            f"hit {m['hit_pct']:.1f}% | flat {m['flat_pnl']:+.0f}€ ({m['flat_roi_pct']:+.1f}%) | "
            f"Kelly {m['kelly_profit']:+.1f}€"
        )

    out = [
        "<b>🧪 Shadow Top5 hebdo</b>",
        f"<i>{now}</i>",
        f"Période: <code>{start}</code> → <code>{end}</code>",
        "",
        line("Prod", prod),
        line("Shadow", shadow),
        "",
        f"{icon} <b>Verdict: {decision}</b>",
    ]
    for r in reasons:
        out.append(f"· {r}")
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
        shadow = _metrics(conn, STRATEGY_KEY, start, end)
    finally:
        conn.close()

    text = format_report(prod, shadow, start, end)
    if dry_run:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(text.replace("<b>", "**").replace("</b>", "**").replace("<i>", "_").replace("</i>", "_").replace("<code>", "`").replace("</code>", "`"))
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
