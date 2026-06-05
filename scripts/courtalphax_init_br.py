#!/usr/bin/env python3
import sqlite3
import sys
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from scripts.bets_db import ensure_bets_meta, set_telegram_user_start_br, compute_telegram_user_bankroll_eur
from scripts.courtalphax_config import COURTALPHAX_TELEGRAM_USER_ID, COURTALPHAX_START_BR_EUR

conn = sqlite3.connect("data/bettinghud.db")
ensure_bets_meta(conn)
set_telegram_user_start_br(conn, COURTALPHAX_TELEGRAM_USER_ID, COURTALPHAX_START_BR_EUR)
snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)
conn.close()
print(f"BR CourtAlphaX: {float(snap['available_eur']):.2f} EUR")
