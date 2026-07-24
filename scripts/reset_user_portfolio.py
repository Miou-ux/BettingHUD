#!/usr/bin/env python3
"""Reset bankroll + historique paris utilisateur (web / Telegram lié)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def reset_user_portfolio(
    conn,
    *,
    web_username: str | None,
    telegram_user_id: str | None,
    bankroll_eur: float,
    archive_path: str | None = None,
) -> dict:
    from scripts.bets_db import (
        ensure_user_bets_schema,
        set_telegram_user_manual_adjust_eur,
        set_telegram_user_start_br,
        set_web_user_manual_adjust_eur,
        set_web_user_start_br,
    )

    ensure_user_bets_schema(conn)
    uname = str(web_username or "").strip().lower() or None
    uid = str(telegram_user_id or "").strip() or None
    clauses: list[str] = []
    params: list[str] = []
    if uname:
        clauses.append("LOWER(COALESCE(web_username, '')) = ?")
        params.append(uname)
    if uid:
        clauses.append("telegram_user_id = ?")
        params.append(uid)
    if not clauses:
        raise ValueError("web_username ou telegram_user_id requis")

    where = " OR ".join(f"({c})" for c in clauses)
    rows = conn.execute(f"SELECT * FROM user_bets WHERE {where}", params).fetchall()
    archived = [dict(r) for r in rows]
    if archive_path and archived:
        os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
        with open(archive_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                    "web_username": uname,
                    "telegram_user_id": uid,
                    "n_bets": len(archived),
                    "bets": archived,
                },
                f,
                ensure_ascii=False,
                indent=2,
                default=str,
            )

    conn.execute(f"DELETE FROM user_bets WHERE {where}", params)
    conn.commit()

    if uname:
        set_web_user_start_br(conn, uname, bankroll_eur, user_custom=True)
        set_web_user_manual_adjust_eur(conn, uname, 0.0)
    if uid:
        set_telegram_user_start_br(conn, uid, bankroll_eur, user_custom=True)
        set_telegram_user_manual_adjust_eur(conn, uid, 0.0)

    return {
        "deleted_bets": len(archived),
        "bankroll_eur": float(bankroll_eur),
        "web_username": uname,
        "telegram_user_id": uid,
        "archive_path": archive_path,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Reset historique paris + bankroll utilisateur")
    p.add_argument("--username", default="miouppy")
    p.add_argument("--telegram-id", default="7113749284")
    p.add_argument("--bankroll", type=float, default=300.0)
    p.add_argument(
        "--portfolio-bankroll",
        type=float,
        default=None,
        help="Si défini, réinit aussi le suivi théorique Top5/1D1P",
    )
    p.add_argument(
        "--portfolio-start-date",
        default=datetime.now().date().isoformat(),
        help="Date départ suivi théorique (YYYY-MM-DD)",
    )
    p.add_argument("--db", default=os.path.join(ROOT, "data", "bettinghud.db"))
    p.add_argument(
        "--archive",
        default=os.path.join(ROOT, "data", "exports", "archives"),
        help="Dossier archive JSON des paris supprimés",
    )
    p.add_argument("--no-archive", action="store_true")
    args = p.parse_args()

    from scripts.bets_db import (
        compute_telegram_user_bankroll_eur,
        compute_web_user_bankroll_eur,
        open_db,
    )

    conn = open_db(args.db)
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        archive = None
        if not args.no_archive:
            archive = os.path.join(
                args.archive,
                f"user_bets_reset_{args.username}_{ts}.json",
            )
        out = reset_user_portfolio(
            conn,
            web_username=args.username,
            telegram_user_id=args.telegram_id,
            bankroll_eur=float(args.bankroll),
            archive_path=archive,
        )
        print("user reset:", out)

        if args.portfolio_bankroll is not None:
            from scripts.init_portfolio_tracking import main as _unused  # noqa: F401
            from scripts.portfolio_tracking_store import (
                MODE_1D1P,
                MODE_TOP5,
                backfill_portfolio_from_published,
                init_portfolio_tracking,
            )

            for mode in (MODE_TOP5, MODE_1D1P):
                init_portfolio_tracking(
                    conn,
                    mode=mode,
                    start_date=args.portfolio_start_date,
                    bankroll_start_eur=float(args.portfolio_bankroll),
                    clear_ledger=True,
                )
                n = backfill_portfolio_from_published(
                    conn, mode, from_date=args.portfolio_start_date
                )
                print(f"portfolio {mode}: start={args.portfolio_start_date} br={args.portfolio_bankroll:.2f}€ rows={n}")

        if args.username:
            print("web BR:", compute_web_user_bankroll_eur(conn, args.username))
        if args.telegram_id:
            print("tg BR:", compute_telegram_user_bankroll_eur(conn, args.telegram_id))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
