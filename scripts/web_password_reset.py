#!/usr/bin/env python3
"""Jetons de réinitialisation mot de passe (fichier JSON local)."""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TOKENS_PATH = _ROOT / "data" / "web_password_reset_tokens.json"
_TOKEN_TTL_SEC = 3600


def _tokens_path() -> Path:
    raw = (os.getenv("BETTINGHUD_WEB_RESET_TOKENS_FILE") or "").strip()
    return Path(raw) if raw else _DEFAULT_TOKENS_PATH


def _load_doc() -> dict[str, Any]:
    path = _tokens_path()
    if not path.is_file():
        return {"tokens": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("tokens"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"tokens": {}}


def _save_doc(doc: dict[str, Any]) -> None:
    path = _tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _purge_expired(tokens: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    return {
        k: v
        for k, v in tokens.items()
        if isinstance(v, dict) and float(v.get("expires_ts") or 0) > now
    }


def create_reset_token(username: str) -> str:
    uname = str(username or "").strip().lower()
    if not uname:
        raise ValueError("username requis")
    token = secrets.token_urlsafe(32)
    doc = _load_doc()
    tokens = _purge_expired(dict(doc.get("tokens") or {}))
    tokens[token] = {
        "username": uname,
        "created_ts": time.time(),
        "expires_ts": time.time() + _TOKEN_TTL_SEC,
    }
    doc["tokens"] = tokens
    _save_doc(doc)
    return token


def validate_reset_token(token: str) -> str | None:
    tok = str(token or "").strip()
    if not tok:
        return None
    doc = _load_doc()
    tokens = _purge_expired(dict(doc.get("tokens") or {}))
    entry = tokens.get(tok)
    if not isinstance(entry, dict):
        doc["tokens"] = tokens
        _save_doc(doc)
        return None
    uname = str(entry.get("username") or "").lower()
    if not uname:
        return None
    return uname


def consume_reset_token(token: str) -> str | None:
    tok = str(token or "").strip()
    if not tok:
        return None
    doc = _load_doc()
    tokens = _purge_expired(dict(doc.get("tokens") or {}))
    entry = tokens.pop(tok, None)
    doc["tokens"] = tokens
    _save_doc(doc)
    if not isinstance(entry, dict):
        return None
    if float(entry.get("expires_ts") or 0) < time.time():
        return None
    return str(entry.get("username") or "").lower() or None
