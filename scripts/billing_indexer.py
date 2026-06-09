#!/usr/bin/env python3
"""Détecte les dépôts ETH (adresses HD ou contrat legacy) et crédite le premium."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts.billing_chain import (
    META_LAST_BLOCK_PREFIX,
    billing_chain_id,
    billing_contract_address,
    fetch_balance,
    fetch_paid_logs,
    hex_to_int,
    payments_configured,
    rpc_call,
)
from scripts.billing_hd import hd_configured
from scripts.bets_db import DB_PATH_DEFAULT, ensure_bets_meta, get_meta, set_meta
from scripts.web_billing import (
    ensure_billing_schema,
    fulfill_order_from_deposit,
    fulfill_order_from_payment,
)


def _meta_key(chain_id: int) -> str:
    return f"{META_LAST_BLOCK_PREFIX}{chain_id}"


def _get_last_block(conn: sqlite3.Connection, chain_id: int) -> int | None:
    ensure_bets_meta(conn)
    raw = get_meta(conn, _meta_key(chain_id))
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _set_last_block(conn: sqlite3.Connection, chain_id: int, block: int) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, _meta_key(chain_id), str(block))


def _expire_stale_orders(conn: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE billing_orders
        SET status = 'expired'
        WHERE status = 'pending' AND expires_at < ?
        """,
        (now,),
    )


def _index_deposit_orders(conn: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _expire_stale_orders(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, deposit_address, price_wei FROM billing_orders
        WHERE status = 'pending' AND expires_at >= ? AND deposit_address IS NOT NULL
        """,
        (now,),
    ).fetchall()

    checked = 0
    credited = 0
    for row in rows:
        addr = str(row["deposit_address"] or "").strip()
        if not addr:
            continue
        checked += 1
        balance = fetch_balance(addr)
        required = int(str(row["price_wei"] or "0"))
        if balance < required:
            continue
        if fulfill_order_from_deposit(
            order_id=str(row["id"]),
            amount_wei=balance,
            tx_hash="balance_poll",
        ):
            credited += 1
    return {"checked": checked, "credited": credited}


def _index_contract_logs(*, lookback_blocks: int) -> dict:
    chain_id = billing_chain_id()
    latest_hex = rpc_call("eth_blockNumber", [])
    latest = hex_to_int(latest_hex)

    dbp = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    conn = sqlite3.connect(dbp)
    try:
        ensure_billing_schema(conn)
        last = _get_last_block(conn, chain_id)
        if last is None:
            last = max(0, latest - lookback_blocks)
        from_block = last + 1 if last else max(0, latest - lookback_blocks)
        if from_block > latest:
            return {"indexed": 0, "credited": 0, "from_block": from_block, "latest": latest}
    finally:
        conn.close()

    logs = fetch_paid_logs(from_block=from_block, to_block=latest)
    credited = 0
    for entry in logs:
        if fulfill_order_from_payment(
            payment_ref=str(entry["payment_ref"]),
            payer_address=str(entry["payer_address"]),
            amount_wei=int(entry["amount_wei"]),
            tx_hash=str(entry["tx_hash"]),
        ):
            credited += 1

    conn = sqlite3.connect(dbp)
    try:
        _set_last_block(conn, chain_id, latest)
        conn.commit()
    finally:
        conn.close()

    return {
        "indexed": len(logs),
        "credited": credited,
        "from_block": from_block,
        "latest": latest,
    }


def run_indexer(*, lookback_blocks: int = 2000) -> dict:
    if not payments_configured():
        return {"ok": False, "reason": "payments_not_configured"}

    out: dict = {"ok": True, "mode": []}

    if hd_configured():
        dbp = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
        conn = sqlite3.connect(dbp)
        try:
            ensure_billing_schema(conn)
            dep = _index_deposit_orders(conn)
            conn.commit()
        finally:
            conn.close()
        out["mode"].append("deposit")
        out.update({f"deposit_{k}": v for k, v in dep.items()})

    if billing_contract_address():
        legacy = _index_contract_logs(lookback_blocks=lookback_blocks)
        out["mode"].append("contract")
        out.update({f"contract_{k}": v for k, v in legacy.items()})

    if not out["mode"]:
        return {"ok": False, "reason": "no_payment_backend"}

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=2000)
    args = ap.parse_args()
    out = run_indexer(lookback_blocks=args.lookback)
    print(out)
    if not out.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
