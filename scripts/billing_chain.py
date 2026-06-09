"""RPC JSON + constantes contrat CourtAlphaPay (indexer)."""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

# keccak256("Paid(bytes32,address,uint256)")
PAID_EVENT_TOPIC = (
    "0x606160394df2742a67735645b7e783304d61d2dbc5b550e8c6acfaf27b6291d7"
)

BASE_MAINNET_CHAIN_ID = 8453
BASE_SEPOLIA_CHAIN_ID = 84532

META_LAST_BLOCK_PREFIX = "billing_last_block_"


def billing_contract_address() -> str | None:
    raw = (os.getenv("COURTALPHA_BILLING_CONTRACT") or "").strip()
    return raw if raw.startswith("0x") and len(raw) == 42 else None


def billing_rpc_url() -> str | None:
    raw = (os.getenv("COURTALPHA_BILLING_RPC_URL") or "").strip()
    return raw or None


def billing_chain_id() -> int:
    return int(os.getenv("COURTALPHA_BILLING_CHAIN_ID", str(BASE_MAINNET_CHAIN_ID)))


def payments_configured() -> bool:
    if not billing_rpc_url():
        return False
    from scripts.billing_hd import hd_configured

    return hd_configured() or bool(billing_contract_address())


def billing_public_config() -> dict[str, Any]:
    from scripts.billing_hd import hd_configured

    hd = hd_configured()
    contract = billing_contract_address()
    return {
        "payment_mode": "deposit" if hd else ("contract" if contract else None),
        "deposit_enabled": hd,
        "contract_address": contract if not hd else contract,
        "chain_id": billing_chain_id(),
        "payments_enabled": payments_configured(),
    }


def fetch_balance(address: str) -> int:
    addr = str(address or "").strip()
    if not addr.startswith("0x"):
        return 0
    result = rpc_call("eth_getBalance", [addr, "latest"])
    return hex_to_int(result)


def rpc_call(method: str, params: list[Any], *, url: str | None = None) -> Any:
    endpoint = url or billing_rpc_url()
    if not endpoint:
        raise RuntimeError("COURTALPHA_BILLING_RPC_URL manquant")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body.get("result")


def hex_to_int(value: str | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


def decode_paid_log(log: dict[str, Any]) -> dict[str, Any] | None:
    topics = log.get("topics") or []
    if len(topics) < 3:
        return None
    if str(topics[0]).lower() != PAID_EVENT_TOPIC.lower():
        return None
    payment_ref = str(topics[1]).lower()
    payer = "0x" + str(topics[2])[-40:]
    amount = hex_to_int(log.get("data"))
    return {
        "payment_ref": payment_ref,
        "payer_address": payer,
        "amount_wei": amount,
        "tx_hash": str(log.get("transactionHash") or ""),
        "block_number": hex_to_int(log.get("blockNumber")),
    }


def fetch_paid_logs(*, from_block: int, to_block: int | str = "latest") -> list[dict[str, Any]]:
    contract = billing_contract_address()
    if not contract:
        return []
    to_arg = hex(to_block) if isinstance(to_block, int) else to_block
    raw = rpc_call(
        "eth_getLogs",
        [
            {
                "fromBlock": hex(from_block),
                "toBlock": to_arg,
                "address": contract,
                "topics": [PAID_EVENT_TOPIC],
            }
        ],
    )
    out: list[dict[str, Any]] = []
    for log in raw or []:
        decoded = decode_paid_log(log)
        if decoded:
            out.append(decoded)
    return out
