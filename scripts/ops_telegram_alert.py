#!/usr/bin/env python3
"""Alerte admin Telegram (ops / P0) avec anti-doublon optionnel."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_DEDUP_PATH = Path(ROOT) / "data" / "cache" / "ops_alert_dedup.json"
_DEFAULT_COOLDOWN_SEC = 1200  # 20 min


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass


def admin_chat_ids() -> list[str]:
    from scripts.telegram_access import admin_notify_chat_id

    ids: list[str] = []
    primary = admin_notify_chat_id()
    if primary:
        ids.append(primary)
    raw = (os.getenv("TELEGRAM_ADMIN_USER_IDS") or "").strip()
    for x in raw.split(","):
        x = x.strip()
        if x and x not in ids:
            ids.append(x)
    return ids


def _dedup_enabled() -> bool:
    return os.getenv("BETTINGHUD_OPS_ALERT_DEDUP", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _cooldown_sec() -> int:
    try:
        return max(60, int(os.getenv("BETTINGHUD_OPS_ALERT_COOLDOWN_SEC", str(_DEFAULT_COOLDOWN_SEC))))
    except ValueError:
        return _DEFAULT_COOLDOWN_SEC


def _normalize_dedup_key(subject: str, explicit: str | None) -> str:
    if explicit:
        return explicit.strip().lower()[:120]
    # Normalise titres proches (ÉCHEC / FAIL) pour regrouper QC + cron sync
    s = subject.strip().lower()
    if "qc post-sync" in s and ("fail" in s or "échec" in s or "echec" in s):
        return "qc_post_sync_fail"
    if "sync tours" in s and ("échec" in s or "echec" in s or "fail" in s):
        return "qc_post_sync_fail"  # même famille que QC bloquant (souvent la cause)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:24]


def _should_skip_dedup(key: str, cooldown: int) -> bool:
    if not _dedup_enabled():
        return False
    try:
        if not _DEDUP_PATH.is_file():
            return False
        data = json.loads(_DEDUP_PATH.read_text(encoding="utf-8"))
        last = float((data.get(key) or {}).get("ts") or 0)
        return (time.time() - last) < cooldown
    except Exception:
        return False


def _remember_dedup(key: str, subject: str) -> None:
    try:
        _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if _DEDUP_PATH.is_file():
            try:
                data = json.loads(_DEDUP_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        # prune > 24h
        cutoff = time.time() - 86400
        data = {k: v for k, v in data.items() if float((v or {}).get("ts") or 0) >= cutoff}
        data[key] = {"ts": time.time(), "subject": subject[:200]}
        _DEDUP_PATH.write_text(json.dumps(data, indent=0), encoding="utf-8")
    except Exception:
        pass


def send_ops_alert(
    subject: str,
    body: str = "",
    *,
    dry_run: bool = False,
    dedup_key: str | None = None,
    cooldown_sec: int | None = None,
    force: bool = False,
) -> bool:
    """Envoie une alerte ops. Retourne False si envoi échoué ; True si OK ou skip dedup."""
    if os.getenv("BETTINGHUD_OPS_ALERT", "1").strip().lower() in ("0", "false", "no"):
        return True

    cool = _cooldown_sec() if cooldown_sec is None else max(0, int(cooldown_sec))
    key = _normalize_dedup_key(subject, dedup_key)
    if not force and cool > 0 and _should_skip_dedup(key, cool):
        print(f"[ops-alert] skip dedup key={key} ({cool}s)", flush=True)
        return True

    if body and ("<b>" in body or body.lstrip().startswith("<")):
        text = body[:3900]
    else:
        text = f"🚨 OPS — {subject}"
        if body:
            text = f"{text}\n{body}"[:3900]
    if dry_run:
        print(text, flush=True)
        _remember_dedup(key, subject)
        return True
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = admin_chat_ids()
    if not token or not chats:
        print("[ops-alert] TELEGRAM_BOT_TOKEN ou admin chat absent", flush=True)
        return False
    from scripts.telegram_top5_notify import send_telegram_message

    ok = True
    for chat_id in chats:
        try:
            send_telegram_message(text, token=token, chat_id=chat_id)
        except Exception as exc:
            print(f"[ops-alert] échec chat {chat_id}: {exc}", flush=True)
            ok = False
    if ok:
        _remember_dedup(key, subject)
    return ok


def main() -> int:
    import argparse

    _load_env()
    ap = argparse.ArgumentParser(description="Alerte ops Telegram")
    ap.add_argument("subject", help="Titre court")
    ap.add_argument("body", nargs="*", default=[], help="Détail optionnel")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="Ignore anti-doublon")
    ap.add_argument("--dedup-key", default=None)
    args = ap.parse_args()
    ok = send_ops_alert(
        args.subject,
        " ".join(args.body),
        dry_run=args.dry_run,
        force=args.force,
        dedup_key=args.dedup_key,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
