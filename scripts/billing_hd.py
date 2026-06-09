"""Adresses de dépôt HD (BIP-44) pour la facturation CourtAlpha."""
from __future__ import annotations

import os
import sqlite3

from scripts.bets_db import ensure_bets_meta, get_meta, set_meta

META_HD_NEXT_INDEX = "billing_hd_next_index"
HD_PATH_TEMPLATE = "m/44'/60'/0'/0/{index}"


def hd_configured() -> bool:
    mnemonic = (os.getenv("COURTALPHA_BILLING_MNEMONIC") or "").strip()
    return bool(mnemonic)


def _mnemonic() -> str:
    raw = (os.getenv("COURTALPHA_BILLING_MNEMONIC") or "").strip()
    if not raw:
        raise RuntimeError("COURTALPHA_BILLING_MNEMONIC manquant")
    return raw


def allocate_address_index(conn: sqlite3.Connection) -> int:
    ensure_bets_meta(conn)
    raw = get_meta(conn, META_HD_NEXT_INDEX)
    idx = int(raw) if raw is not None else 0
    set_meta(conn, META_HD_NEXT_INDEX, str(idx + 1))
    conn.commit()
    return idx


def derive_deposit_address(index: int) -> str:
    from eth_account import Account

    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(_mnemonic(), account_path=HD_PATH_TEMPLATE.format(index=index))
    return acct.address
