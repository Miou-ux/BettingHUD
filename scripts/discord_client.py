"""Client minimal Discord (webhooks entrants)."""
from __future__ import annotations

import os
from typing import Any

import requests

DISCORD_WEBHOOK_TIMEOUT_SEC = 30
DISCORD_API_BASE = "https://discord.com/api/v10"


def _webhook_wait_url(url: str) -> str:
    base = (url or "").strip()
    if not base:
        return base
    return base if "wait=" in base else f"{base}{'&' if '?' in base else '?'}wait=true"


def discord_general_webhook_url() -> str | None:
    """Webhook for #general — never falls back to 1D1P."""
    url = (os.getenv("DISCORD_GENERAL_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    return url or None


def discord_webhook_url() -> str | None:
    url = (os.getenv("DISCORD_1D1P_WEBHOOK_URL") or "").strip()
    if not url:
        # Legacy: single webhook before 1D1P split — only if general webhook not set.
        if not discord_general_webhook_url():
            url = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()
    return url or None


def discord_posting_enabled() -> bool:
    raw = (os.getenv("DISCORD_1D1P_ENABLED") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return bool(discord_webhook_url())


def post_webhook(
    *,
    content: str | None = None,
    embeds: list[dict[str, Any]] | None = None,
    username: str | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    url = (webhook_url or discord_webhook_url() or "").strip()
    if not url:
        raise ValueError("DISCORD_1D1P_WEBHOOK_URL manquant")
    payload: dict[str, Any] = {}
    if content:
        payload["content"] = content[:2000]
    if embeds:
        payload["embeds"] = embeds[:10]
    if username:
        payload["username"] = username[:80]
    resp = requests.post(
        _webhook_wait_url(url), json=payload, timeout=DISCORD_WEBHOOK_TIMEOUT_SEC
    )
    resp.raise_for_status()
    if resp.content:
        try:
            return resp.json()
        except Exception:
            return {}
    return {}


def patch_webhook_message(
    message_id: str,
    *,
    content: str | None = None,
    embeds: list[dict[str, Any]] | None = None,
    username: str | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    url = (webhook_url or discord_webhook_url() or "").strip()
    mid = str(message_id or "").strip()
    if not url or not mid:
        raise ValueError("webhook URL and message_id required")
    payload: dict[str, Any] = {}
    if content is not None:
        payload["content"] = content[:2000]
    if embeds is not None:
        payload["embeds"] = embeds[:10]
    if username:
        payload["username"] = username[:80]
    api = f"{url.rstrip('/')}/messages/{mid}"
    resp = requests.patch(api, json=payload, timeout=DISCORD_WEBHOOK_TIMEOUT_SEC)
    resp.raise_for_status()
    if resp.content:
        try:
            return resp.json()
        except Exception:
            return {}
    return {}


def discord_1d1p_channel_id() -> str | None:
    raw = (os.getenv("DISCORD_1D1P_CHANNEL_ID") or "").strip()
    if raw:
        return raw
    return None


def try_pin_channel_message(
    channel_id: str,
    message_id: str,
    *,
    bot_token: str | None = None,
) -> bool:
    """Pin a message (requires bot token with PIN_MESSAGES). Webhooks cannot pin."""
    token = (bot_token or os.getenv("DISCORD_BOT_TOKEN") or "").strip()
    cid = str(channel_id or "").strip()
    mid = str(message_id or "").strip()
    if not token or not cid or not mid:
        return False
    url = f"{DISCORD_API_BASE}/channels/{cid}/pins/{mid}"
    resp = requests.put(
        url,
        headers={"Authorization": f"Bot {token}"},
        timeout=DISCORD_WEBHOOK_TIMEOUT_SEC,
    )
    return resp.status_code in (200, 204)
