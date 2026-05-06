import sys
import os
import glob
import pandas as pd
from datetime import datetime
import json

def simulate():
    data_dir = os.path.join("data", "scraped")
    files = glob.glob(os.path.join(data_dir, "*.csv"))
    if not files:
        print("No files")
        return
    latest_file = max(files, key=os.path.getctime)
    print("Latest file:", latest_file)
    df = pd.read_csv(latest_file)
    
    matches = []
    for _, row in df.iterrows():
        odd_p1 = float(row['odd_p1']) if pd.notna(row['odd_p1']) else 0.0
        odd_p2 = float(row['odd_p2']) if pd.notna(row['odd_p2']) else 0.0
        
        matches.append({
            "time": row['time'],
            "odd_p1": odd_p1,
            "odd_p2": odd_p2
        })
        
    print(f"Total parsed: {len(matches)}")
    
    real_matches = [m for m in matches if m['odd_p1'] > 1.0 and m['odd_p2'] > 1.0]
    print(f"After odds filter: {len(real_matches)}")
    
    current_time = datetime.now().time()
    print("Current time:", current_time)
    
    def is_future_match(time_str):
        if str(time_str).startswith("Demain"):
            return True
        try:
            match_time = datetime.strptime(str(time_str).strip(), "%H:%M").time()
            return match_time >= current_time
        except ValueError:
            return True
            
    real_matches = [m for m in real_matches if is_future_match(m['time'])]
    print(f"After future filter: {len(real_matches)}")
    
    demain = [m for m in real_matches if str(m['time']).startswith("Demain")]
    print(f"Demain count: {len(demain)}")
    if demain:
        print(demain[:2])

simulate()