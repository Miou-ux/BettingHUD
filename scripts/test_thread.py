import threading
import time
import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from scripts.scraper_prematch import FlashscoreScraper

def scrape_loop():
    scraper = FlashscoreScraper()
    print("In thread")
    try:
        asyncio.run(scraper.get_today_matches_and_odds())
    except Exception as e:
        print("Erreur du scraper en arrière-plan:", e)

t = threading.Thread(target=scrape_loop, daemon=True)
t.start()
t.join()