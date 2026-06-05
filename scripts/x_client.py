"""Client minimal pour publier sur X (API v2)."""
from __future__ import annotations

import os
from typing import Any

import requests

X_TWEET_URL = "https://api.x.com/2/tweets"
X_MAX_CHARS = 280


def x_posting_enabled() -> bool:
    return os.getenv("COURTALPHAX_X_ENABLED", "0").strip().lower() in {"1", "true", "yes"}


def is_prod_env() -> bool:
    return (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower() == "prod"


def require_prod_for_x_post(*, force: bool = False, dry_run: bool = False) -> None:
    """Bloque la publication réelle hors PROD (comme Telegram matin)."""
    if dry_run or force:
        return
    if not is_prod_env():
        raise SystemExit(
            "Publication X désactivée en PREPROD (BETTINGHUD_ENV != prod). "
            "Utiliser --dry-run pour prévisualiser. "
            "Credentials + cron sur le serveur PROD (/opt/bettinghud/.env). "
            "Override explicite : --force (déconseillé)."
        )


def _oauth2_user_token() -> str | None:
    for key in ("X_USER_ACCESS_TOKEN", "X_OAUTH2_ACCESS_TOKEN", "X_ACCESS_TOKEN_BEARER"):
        val = os.getenv(key, "").strip()
        if val:
            return val
    return None


def _oauth1_session():
    try:
        from requests_oauthlib import OAuth1
    except ImportError as exc:
        raise RuntimeError(
            "requests-oauthlib requis pour OAuth 1.0a (pip install requests-oauthlib)"
        ) from exc

    api_key = os.getenv("X_API_KEY", "").strip() or os.getenv("X_CONSUMER_KEY", "").strip()
    api_secret = os.getenv("X_API_SECRET", "").strip() or os.getenv("X_CONSUMER_SECRET", "").strip()
    access_token = os.getenv("X_ACCESS_TOKEN", "").strip()
    access_secret = os.getenv("X_ACCESS_TOKEN_SECRET", "").strip()
    missing = [n for n, v in [
        ("X_API_KEY", api_key),
        ("X_API_SECRET", api_secret),
        ("X_ACCESS_TOKEN", access_token),
        ("X_ACCESS_TOKEN_SECRET", access_secret),
    ] if not v]
    if missing:
        raise RuntimeError(f"Variables OAuth 1.0a manquantes : {', '.join(missing)}")
    return OAuth1(api_key, api_secret, access_token, access_secret)


def truncate_tweet(text: str, *, limit: int = X_MAX_CHARS) -> str:
    t = str(text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def post_tweet(text: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Publie un tweet texte (sans URL volontaire — coût API plus bas)."""
    body_text = truncate_tweet(text)
    if dry_run:
        return {"ok": True, "dry_run": True, "text": body_text, "char_count": len(body_text)}

    if not x_posting_enabled():
        raise RuntimeError(
            "Publication X désactivée. Définir COURTALPHAX_X_ENABLED=1 dans .env"
        )

    payload = {"text": body_text}
    bearer = _oauth2_user_token()
    if bearer:
        resp = requests.post(
            X_TWEET_URL,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
    else:
        auth = _oauth1_session()
        resp = requests.post(X_TWEET_URL, auth=auth, json=payload, timeout=30)

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        raise RuntimeError(f"X API {resp.status_code}: {data}")

    tweet_id = str((data.get("data") or {}).get("id") or "")
    return {
        "ok": True,
        "tweet_id": tweet_id,
        "text": body_text,
        "char_count": len(body_text),
        "response": data,
    }


def delete_tweet(tweet_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Supprime un tweet (OAuth user, scope tweet.write)."""
    tid = str(tweet_id or "").strip()
    if not tid:
        raise ValueError("tweet_id requis")

    if dry_run:
        return {"ok": True, "dry_run": True, "tweet_id": tid, "deleted": False}

    if not x_posting_enabled():
        raise RuntimeError(
            "Publication X désactivée. Définir COURTALPHAX_X_ENABLED=1 dans .env"
        )

    url = f"{X_TWEET_URL}/{tid}"
    bearer = _oauth2_user_token()
    if bearer:
        resp = requests.delete(
            url,
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=30,
        )
    else:
        auth = _oauth1_session()
        resp = requests.delete(url, auth=auth, timeout=30)

    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        raise RuntimeError(f"X API {resp.status_code}: {data}")

    deleted = bool((data.get("data") or {}).get("deleted"))
    return {"ok": True, "tweet_id": tid, "deleted": deleted, "response": data}
