"""Identité et paramètres du compte public CourtAlpha sur X."""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(_ROOT / ".env")
    except ImportError:
        pass


_load_env()
os.environ.setdefault("BETTINGHUD_ENV", "prod")
os.environ.setdefault("BETTINGHUD_HEADLESS", "1")

# Utilisateur web CourtAlpha (web_users.json)
COURTALPHAX_USERNAME = "courtalphax"
COURTALPHAX_DISPLAY_NAME = "CourtAlpha X"

# ID logique pour scoper bankroll + paris dans user_bets (pas un vrai Telegram user)
COURTALPHAX_TELEGRAM_USER_ID = os.getenv("COURTALPHAX_TELEGRAM_USER_ID", "9000000001").strip()

# Bankroll de départ (100 € = 100 u si 1 u = 1 €)
COURTALPHAX_START_BR_EUR = float(os.getenv("COURTALPHAX_START_BR_EUR", "100"))

# tracker_source en base pour filtrer les paris publiés sur X
COURTALPHAX_TRACKER_SOURCE = "courtalphax_x"

COURTALPHA_PUBLIC_URL = (os.getenv("COURTALPHA_PUBLIC_URL") or "https://courtalpha.tech").rstrip("/")


def courtalpha_track_url(*, campaign: str = "daily") -> str:
    """Lien track record avec UTM pour tweets X."""
    from urllib.parse import urlencode

    q = urlencode(
        {
            "utm_source": "twitter",
            "utm_medium": "x_auto",
            "utm_campaign": campaign,
        }
    )
    return f"{COURTALPHA_PUBLIC_URL}/1-day-1-pick?{q}"
