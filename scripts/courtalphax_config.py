"""Identité et paramètres du compte public CourtAlpha sur X."""
from __future__ import annotations

import os

# Utilisateur web CourtAlpha (web_users.json)
COURTALPHAX_USERNAME = "courtalphax"
COURTALPHAX_DISPLAY_NAME = "CourtAlpha X"

# ID logique pour scoper bankroll + paris dans user_bets (pas un vrai Telegram user)
COURTALPHAX_TELEGRAM_USER_ID = os.getenv("COURTALPHAX_TELEGRAM_USER_ID", "9000000001").strip()

# Bankroll de départ (100 € = 100 u si 1 u = 1 €)
COURTALPHAX_START_BR_EUR = float(os.getenv("COURTALPHAX_START_BR_EUR", "100"))

# tracker_source en base pour filtrer les paris publiés sur X
COURTALPHAX_TRACKER_SOURCE = "courtalphax_x"
