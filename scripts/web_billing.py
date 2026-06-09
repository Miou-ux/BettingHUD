"""Facturation CourtAlpha — entitlements premium + commandes ETH."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

DB_PATH_DEFAULT = os.path.join("data", "bettinghud.db")

DEFAULT_PLAN_ID = "premium_30d"
BASE_MAINNET_CHAIN_ID = 8453
ORDER_TTL_HOURS = 1
MAX_PENDING_ORDERS_PER_USER = 5


def billing_enabled() -> bool:
    raw = os.getenv("COURTALPHA_BILLING_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _db_path(db_path: str | None = None) -> str:
    return db_path or DB_PATH_DEFAULT


def ensure_billing_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_plans (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            duration_days INTEGER NOT NULL,
            price_wei TEXT NOT NULL,
            chain_id INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_orders (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            payment_ref TEXT NOT NULL,
            price_wei TEXT NOT NULL,
            chain_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            payer_address TEXT,
            tx_hash TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            paid_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS web_user_entitlements (
            username TEXT PRIMARY KEY,
            premium_until TEXT,
            wallet_address TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_billing_orders_username ON billing_orders(username)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_billing_orders_payment_ref ON billing_orders(payment_ref)"
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(billing_orders)").fetchall()}
    if "deposit_address" not in cols:
        try:
            conn.execute("ALTER TABLE billing_orders ADD COLUMN deposit_address TEXT")
        except sqlite3.OperationalError:
            pass
    if "address_index" not in cols:
        try:
            conn.execute("ALTER TABLE billing_orders ADD COLUMN address_index INTEGER")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_billing_orders_deposit_address ON billing_orders(deposit_address)"
    )
    conn.commit()
    _seed_default_plan(conn)


def _seed_default_plan(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM billing_plans WHERE id = ?", (DEFAULT_PLAN_ID,)
    ).fetchone()
    if row:
        return
    conn.execute(
        """
        INSERT INTO billing_plans (id, label, duration_days, price_wei, chain_id, active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (
            DEFAULT_PLAN_ID,
            "Premium 30 jours",
            30,
            str(int(os.getenv("COURTALPHA_BILLING_PRICE_WEI", str(5 * 10**14)))),
            int(os.getenv("COURTALPHA_BILLING_CHAIN_ID", str(BASE_MAINNET_CHAIN_ID))),
        ),
    )
    conn.commit()


def get_premium_until(username: str, *, db_path: str | None = None) -> Optional[str]:
    uname = str(username or "").strip().lower()
    if not uname:
        return None
    conn = sqlite3.connect(_db_path(db_path))
    try:
        ensure_billing_schema(conn)
        row = conn.execute(
            "SELECT premium_until FROM web_user_entitlements WHERE username = ?",
            (uname,),
        ).fetchone()
        return str(row[0]) if row and row[0] else None
    finally:
        conn.close()


def is_premium(username: str | None, *, user: dict[str, Any] | None = None) -> bool:
    if not billing_enabled():
        return True
    from scripts.web_auth import is_admin

    u = user or {}
    if is_admin(u) or is_admin({"username": username}):
        return True
    uname = str(username or u.get("username") or "").strip().lower()
    if not uname:
        return False
    until = get_premium_until(uname)
    if not until:
        return False
    try:
        dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except ValueError:
        return False


def user_tier(user: dict[str, Any] | None) -> str:
    from scripts.web_auth import is_admin

    if not user:
        return "visitor"
    if is_admin(user):
        return "admin"
    if is_premium(str(user.get("username") or ""), user=user):
        return "premium"
    return "free"


def enrich_user_session(session: dict[str, Any]) -> dict[str, Any]:
    out = dict(session)
    tier = user_tier(out)
    until = get_premium_until(str(out.get("username") or ""))
    out["tier"] = tier
    out["premium_active"] = tier in {"premium", "admin"}
    out["premium_until"] = until
    return out


def grant_premium_days(
    username: str,
    days: int,
    *,
    db_path: str | None = None,
) -> dict[str, Any]:
    uname = str(username or "").strip().lower()
    if not uname:
        raise ValueError("username requis")
    if days <= 0:
        raise ValueError("days doit être > 0")
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(_db_path(db_path))
    try:
        ensure_billing_schema(conn)
        row = conn.execute(
            "SELECT premium_until FROM web_user_entitlements WHERE username = ?",
            (uname,),
        ).fetchone()
        base = now
        if row and row[0]:
            try:
                prev = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                if prev.tzinfo is None:
                    prev = prev.replace(tzinfo=timezone.utc)
                if prev > now:
                    base = prev
            except ValueError:
                pass
        new_until = (base + timedelta(days=days)).isoformat(timespec="seconds")
        updated = now.isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO web_user_entitlements (username, premium_until, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                premium_until = excluded.premium_until,
                updated_at = excluded.updated_at
            """,
            (uname, new_until, updated),
        )
        conn.commit()
        return {"username": uname, "premium_until": new_until, "days_added": days}
    finally:
        conn.close()


def list_active_plans(*, db_path: str | None = None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(_db_path(db_path))
    try:
        ensure_billing_schema(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, label, duration_days, price_wei, chain_id FROM billing_plans WHERE active = 1"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def billing_public_config() -> dict[str, Any]:
    from scripts.billing_chain import billing_public_config as chain_cfg

    cfg = chain_cfg()
    cfg["payments_enabled"] = bool(cfg.get("payments_enabled")) and billing_enabled()
    return cfg


def _payment_ref_for_order(order_id: str) -> str:
    digest = hashlib.sha256(f"courtalpha|{order_id}".encode("utf-8")).hexdigest()
    return "0x" + digest


def _row_to_order(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "username": row["username"],
        "plan_id": row["plan_id"],
        "payment_ref": row["payment_ref"],
        "price_wei": row["price_wei"],
        "chain_id": row["chain_id"],
        "status": row["status"],
        "payer_address": row["payer_address"],
        "tx_hash": row["tx_hash"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "paid_at": row["paid_at"],
        "deposit_address": row["deposit_address"] if "deposit_address" in keys else None,
        "address_index": row["address_index"] if "address_index" in keys else None,
    }


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


def create_order(
    username: str,
    plan_id: str = DEFAULT_PLAN_ID,
    *,
    db_path: str | None = None,
) -> dict[str, Any]:
    from scripts.billing_chain import billing_public_config

    cfg = billing_public_config()
    if not cfg.get("payments_enabled"):
        raise ValueError("Paiements ETH non configurés sur le serveur")

    uname = str(username or "").strip().lower()
    if not uname:
        raise ValueError("username requis")

    now = datetime.now(timezone.utc)
    order_id = str(uuid.uuid4())
    payment_ref = _payment_ref_for_order(order_id)
    expires = (now + timedelta(hours=ORDER_TTL_HOURS)).isoformat(timespec="seconds")

    conn = sqlite3.connect(_db_path(db_path))
    try:
        ensure_billing_schema(conn)
        conn.row_factory = sqlite3.Row
        _expire_stale_orders(conn)
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM billing_orders
            WHERE username = ? AND status = 'pending' AND expires_at >= ?
            """,
            (uname, now.isoformat(timespec="seconds")),
        ).fetchone()[0]
        if int(pending or 0) >= MAX_PENDING_ORDERS_PER_USER:
            raise ValueError("Trop de commandes en attente — réessayez plus tard")

        plan = conn.execute(
            "SELECT id, label, duration_days, price_wei, chain_id FROM billing_plans WHERE id = ? AND active = 1",
            (plan_id,),
        ).fetchone()
        if not plan:
            raise ValueError(f"Plan inconnu: {plan_id}")

        from scripts.billing_hd import allocate_address_index, derive_deposit_address, hd_configured

        deposit_address = None
        address_index = None
        if hd_configured():
            address_index = allocate_address_index(conn)
            deposit_address = derive_deposit_address(address_index)

        conn.execute(
            """
            INSERT INTO billing_orders (
                id, username, plan_id, payment_ref, price_wei, chain_id,
                status, created_at, expires_at, deposit_address, address_index
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                order_id,
                uname,
                plan["id"],
                payment_ref,
                str(plan["price_wei"]),
                int(plan["chain_id"]),
                now.isoformat(timespec="seconds"),
                expires,
                deposit_address,
                address_index,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM billing_orders WHERE id = ?", (order_id,)
        ).fetchone()
        out = _row_to_order(row)
        out.update(cfg)
        out["plan_label"] = plan["label"]
        out["duration_days"] = plan["duration_days"]
        return out
    finally:
        conn.close()


def get_order(order_id: str, *, username: str | None = None, db_path: str | None = None) -> dict[str, Any] | None:
    conn = sqlite3.connect(_db_path(db_path))
    try:
        ensure_billing_schema(conn)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM billing_orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return None
        if username and str(row["username"]).lower() != str(username).strip().lower():
            return None
        return _row_to_order(row)
    finally:
        conn.close()


def fulfill_order_from_payment(
    *,
    payment_ref: str,
    payer_address: str,
    amount_wei: int,
    tx_hash: str,
    db_path: str | None = None,
) -> bool:
    ref = str(payment_ref or "").strip().lower()
    if not ref:
        return False
    conn = sqlite3.connect(_db_path(db_path))
    try:
        ensure_billing_schema(conn)
        conn.row_factory = sqlite3.Row
        if tx_hash:
            dup = conn.execute(
                "SELECT 1 FROM billing_orders WHERE tx_hash = ? AND status = 'paid'",
                (tx_hash,),
            ).fetchone()
            if dup:
                return False

        row = conn.execute(
            "SELECT * FROM billing_orders WHERE lower(payment_ref) = ? AND status = 'pending'",
            (ref,),
        ).fetchone()
        if not row:
            return False

        now = datetime.now(timezone.utc)
        expires = str(row["expires_at"] or "")
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt < now:
                conn.execute(
                    "UPDATE billing_orders SET status = 'expired' WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                return False
        except ValueError:
            pass

        required = int(str(row["price_wei"] or "0"))
        if int(amount_wei) < required:
            return False

        plan = conn.execute(
            "SELECT duration_days FROM billing_plans WHERE id = ?",
            (row["plan_id"],),
        ).fetchone()
        days = int(plan["duration_days"]) if plan else 30
        paid_at = now.isoformat(timespec="seconds")

        conn.execute(
            """
            UPDATE billing_orders
            SET status = 'paid', payer_address = ?, tx_hash = ?, paid_at = ?
            WHERE id = ?
            """,
            (payer_address, tx_hash, paid_at, row["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    grant_premium_days(str(row["username"]), days, db_path=db_path)
    if payer_address:
        conn2 = sqlite3.connect(_db_path(db_path))
        try:
            ensure_billing_schema(conn2)
            conn2.execute(
                """
                UPDATE web_user_entitlements SET wallet_address = ?
                WHERE username = ?
                """,
                (payer_address, str(row["username"]).lower()),
            )
            conn2.commit()
        finally:
            conn2.close()
    return True


def fulfill_order_from_deposit(
    *,
    order_id: str | None = None,
    deposit_address: str | None = None,
    amount_wei: int,
    payer_address: str = "",
    tx_hash: str = "",
    db_path: str | None = None,
) -> bool:
    oid = str(order_id or "").strip()
    addr = str(deposit_address or "").strip().lower()
    if not oid and not addr:
        return False

    conn = sqlite3.connect(_db_path(db_path))
    try:
        ensure_billing_schema(conn)
        conn.row_factory = sqlite3.Row
        if tx_hash and tx_hash != "balance_poll":
            dup = conn.execute(
                "SELECT 1 FROM billing_orders WHERE tx_hash = ? AND status = 'paid'",
                (tx_hash,),
            ).fetchone()
            if dup:
                return False

        if oid:
            row = conn.execute(
                "SELECT * FROM billing_orders WHERE id = ? AND status = 'pending'",
                (oid,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM billing_orders WHERE lower(deposit_address) = ? AND status = 'pending'",
                (addr,),
            ).fetchone()
        if not row:
            return False

        now = datetime.now(timezone.utc)
        expires = str(row["expires_at"] or "")
        try:
            exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt < now:
                conn.execute(
                    "UPDATE billing_orders SET status = 'expired' WHERE id = ?",
                    (row["id"],),
                )
                conn.commit()
                return False
        except ValueError:
            pass

        required = int(str(row["price_wei"] or "0"))
        if int(amount_wei) < required:
            return False

        plan = conn.execute(
            "SELECT duration_days FROM billing_plans WHERE id = ?",
            (row["plan_id"],),
        ).fetchone()
        days = int(plan["duration_days"]) if plan else 30
        paid_at = now.isoformat(timespec="seconds")
        tx = tx_hash or "balance_poll"

        conn.execute(
            """
            UPDATE billing_orders
            SET status = 'paid', payer_address = ?, tx_hash = ?, paid_at = ?
            WHERE id = ?
            """,
            (payer_address or None, tx, paid_at, row["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    grant_premium_days(str(row["username"]), days, db_path=db_path)
    if payer_address:
        conn2 = sqlite3.connect(_db_path(db_path))
        try:
            ensure_billing_schema(conn2)
            conn2.execute(
                """
                UPDATE web_user_entitlements SET wallet_address = ?
                WHERE username = ?
                """,
                (payer_address, str(row["username"]).lower()),
            )
            conn2.commit()
        finally:
            conn2.close()
    return True
