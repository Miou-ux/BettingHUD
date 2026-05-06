import sqlite3
import pandas as pd
import numpy as np
import time

t0 = time.time()
conn = sqlite3.connect('data/bettinghud.db')
df = pd.read_sql("SELECT tourney_date, winner_id, loser_id, minutes FROM matches WHERE tourney_date > '2022-01-01' ORDER BY tourney_date ASC", conn)
conn.close()

print(f"Loaded {len(df)} matches in {time.time()-t0:.2f}s")
