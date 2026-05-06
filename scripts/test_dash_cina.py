import os
import sys
import pandas as pd

# Add the project root to the python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + '/..'))

from app.dashboard import get_latest_scraped_data

def test():
    matches = get_latest_scraped_data()
    for m in matches:
        if 'Cina' in m['player1'] or 'Cina' in m['player2']:
            print("P1 Stats:", m['p1_stats'])
            print("P2 Stats:", m['p2_stats'])
            print("p1_url:", m.get('p1_url', 'NOT IN M'))
            print("p1_profile:", profile_scraper.scrape_profile('/player/cina/'))

if __name__ == '__main__':
    test()