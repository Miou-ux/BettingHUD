import sqlite3
import pandas as pd
c = sqlite3.connect('data/bettinghud.db')
df = pd.read_sql("SELECT id,date,match_name,bet_on,odds,stake,status FROM user_bets WHERE status='En cours' ORDER BY id DESC LIMIT 20", c)
print('pending', len(df))
print(df.to_string(index=False) if len(df) else 'no pending bets')
c.close()
