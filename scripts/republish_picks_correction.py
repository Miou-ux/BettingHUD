#!/usr/bin/env python3
"""Mise à jour archive publiée + notification TG corrective après refresh snapshot."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

PARIS = ZoneInfo("Europe/Paris")


def _pct(x: object) -> float:
    try:
        v = float(x or 0)
    except (TypeError, ValueError):
        return 0.0
    return v * 100.0 if v <= 1.0 else v


def _load_old_published(db_path: str, cal_day: str, mode: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT fav_player, p_model_fav, ev_fav_pct, odd_fav, data_reliability_score
            FROM daily_published_picks
            WHERE calendar_date = ? AND mode = ?
            ORDER BY publish_rank ASC LIMIT 1
            """,
            (cal_day, mode),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _format_correction_banner(old: dict | None, new: dict, *, label: str) -> str:
    old_p = _pct(old.get("p_model_fav")) if old else None
    new_p = _pct(new.get("p_model_fav"))
    old_ev = float(old.get("ev_fav_pct") or 0) if old else None
    new_ev = float(new.get("ev_fav_pct") or 0)
    old_rel = old.get("data_reliability_score") if old else None
    new_rel = new.get("data_reliability_score")
    lines = [
        f"🔄 <b>Mise à jour {label}</b> — snapshot recalculé",
        "",
        "Le message du matin utilisait un snapshot obsolète (~3h40).",
        "Chiffres à jour ci-dessous :",
        "",
    ]
    if old:
        lines.append(
            f"❌ Matin : proba <b>{old_p:.1f}%</b> · EV <b>{old_ev:+.1f}%</b>"
            + (f" · rel {old_rel}" if old_rel is not None else "")
        )
    lines.append(
        f"✅ Maintenant : proba <b>{new_p:.1f}%</b> · EV <b>{new_ev:+.1f}%</b>"
        + (f" · rel {new_rel}" if new_rel is not None else "")
    )
    lines.append("")
    return "\n".join(lines)


def _prepare_db_row(pick: dict, *, cal_day: str, rank: int = 1) -> dict:
    row = dict(pick)
    tour = str(row.get("tour") or "WTA").upper()
    row.setdefault("calendar_date", cal_day)
    row.setdefault("match_date", cal_day)
    row["rank"] = rank
    row["top_limit"] = rank
    row["pick_key"] = str(row.get("pick_key") or f"{cal_day}|{tour}|{rank:02d}")
    row["capture_source"] = "refresh-correction"
    row["p_model_pct"] = _pct(row.get("p_model_fav"))
    row["ev_pct"] = float(row.get("ev_fav_pct") or 0)
    return row


def run(*, dry_run: bool = False) -> dict:
    from scripts.bets_db import DB_PATH_DEFAULT, upsert_daily_top_proba_picks
    from scripts.pick_modes import PickMode, load_picks
    from scripts.published_picks_store import MODE_1D1P, MODE_TOP5, save_published_picks
    from scripts.telegram_access import load_broadcast_chat_ids
    from scripts.telegram_top5_notify import (
        _join_pick_blocks,
        comms_disclaimer,
        format_snapshot_freshness_line,
        send_telegram_message,
        tg,
        _interactive_footer,
        format_1d1p_interactive_header,
        send_interactive_pick_messages,
    )

    cal_day = datetime.now(PARIS).date().isoformat()
    top_res = load_picks(PickMode.TOP5, channel="telegram")
    pick_res = load_picks(PickMode.ONE_PICK_ONE_DAY)
    picks = top_res.picks
    pick_1d1p = pick_res.pick_today
    age_min = top_res.snapshot_age_min

    old_top5 = _load_old_published(DB_PATH_DEFAULT, cal_day, MODE_TOP5)
    old_1d1p = _load_old_published(DB_PATH_DEFAULT, cal_day, MODE_1D1P)

    result = {
        "calendar_date": cal_day,
        "n_top5": len(picks),
        "dry_run": dry_run,
        "updated_archive": False,
        "sent_top5": 0,
        "sent_1d1p": 0,
    }

    if not picks and not pick_1d1p:
        print("Aucun pick à republier.")
        return result

    db_rows = []
    for i, p in enumerate(picks, start=1):
        db_rows.append(_prepare_db_row(p, cal_day=cal_day, rank=i))

    if not dry_run:
        from scripts.bets_db import open_db

        conn = open_db(DB_PATH_DEFAULT)
        try:
            if db_rows:
                upsert_daily_top_proba_picks(db_rows, db_path=DB_PATH_DEFAULT)
            if picks:
                save_published_picks(
                    conn,
                    mode=MODE_TOP5,
                    calendar_date=cal_day,
                    picks=[_prepare_db_row(p, cal_day=cal_day, rank=i) for i, p in enumerate(picks, 1)],
                    source="refresh-correction",
                )
            if pick_1d1p:
                save_published_picks(
                    conn,
                    mode=MODE_1D1P,
                    calendar_date=cal_day,
                    picks=[_prepare_db_row(pick_1d1p, cal_day=cal_day, rank=1)],
                    source="refresh-correction",
                )
            result["updated_archive"] = True
        finally:
            conn.close()

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = load_broadcast_chat_ids()
    if not token:
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

    # --- Top 5 correction ---
    if picks:
        banner = _format_correction_banner(old_top5, picks[0], label="Top 5")
        body = "\n".join(
            [
                banner,
                format_snapshot_freshness_line(age_min),
                "━━━━━━━━━━━━━━━━━━━━",
                _join_pick_blocks(picks),
                "",
                "━━━━━━━━━━━━━━━━━━━━",
                f"ℹ️ <i>{comms_disclaimer()} Vérifier les cotes live avant pari.</i>",
            ]
        )
        print("=== TOP5 CORRECTION ===")
        print(body)
        if not dry_run and token and chats:
            for chat_id in chats:
                send_telegram_message(body, token=token, chat_id=chat_id)
                result["sent_top5"] += 1

    # --- 1D1P correction ---
    if pick_1d1p:
        pick_row = _prepare_db_row(pick_1d1p, cal_day=cal_day, rank=1)
        banner = _format_correction_banner(old_1d1p, pick_row, label="1 Day 1 Pick")
        header = format_1d1p_interactive_header(
            calendar_date=cal_day,
            pool_size=pick_res.pool_n,
            source="morning-sync",
            n_picks=1,
            snapshot_age_min=age_min,
        )
        header = banner + "\n\n" + header
        print("\n=== 1D1P CORRECTION ===")
        print(header)
        print(_join_pick_blocks([pick_row]))
        if not dry_run and token and chats:
            for chat_id in chats:
                send_interactive_pick_messages(
                    [pick_row],
                    header_text=header,
                    footer_text=_interactive_footer(),
                    token=token,
                    chat_id=chat_id,
                    list_kind="1d1p",
                    telegram_user_id=str(chat_id),
                )
                result["sent_1d1p"] += 1

    print("\nRESULT:", result)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Republish picks after snapshot refresh")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass
    os.environ.setdefault("BETTINGHUD_ENV", "prod")
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
