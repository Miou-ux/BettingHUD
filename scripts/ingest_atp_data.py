import os
import pandas as pd
import sqlite3
from sqlalchemy import create_engine
import glob

def ingest_data():
    raw_data_dir = os.path.join('data', 'raw', 'tennis_atp')
    processed_data_dir = os.path.join('data', 'processed')
    os.makedirs(processed_data_dir, exist_ok=True)
    
    # 1. Ingestion des matchs
    print("Ingestion des matchs ATP...")
    match_files = glob.glob(os.path.join(raw_data_dir, 'atp_matches_*.csv'))
    # Exclure les doubles, les tournois futures/challengers si on ne veut que le circuit principal pour commencer
    match_files = [f for f in match_files if 'doubles' not in f and 'futures' not in f and 'qual_chall' not in f]
    
    df_matches = pd.concat((pd.read_csv(f, low_memory=False) for f in match_files), ignore_index=True)
    
    # Conversion des colonnes mixtes en string pour éviter les erreurs pyarrow
    for col in df_matches.columns:
        if df_matches[col].dtype == 'object':
            df_matches[col] = df_matches[col].astype(str)

    # Conversion des dates
    df_matches['tourney_date'] = pd.to_datetime(df_matches['tourney_date'], format='%Y%m%d', errors='coerce')
    df_matches = df_matches.sort_values('tourney_date').reset_index(drop=True)
    
    # Sauvegarde en Parquet pour accès rapide
    matches_parquet_path = os.path.join(processed_data_dir, 'atp_matches.parquet')
    df_matches.to_parquet(matches_parquet_path, index=False)
    print(f"Sauvegardé {len(df_matches)} matchs dans {matches_parquet_path}")

    # 2. Ingestion des joueurs
    print("Ingestion des joueurs ATP...")
    players_file = os.path.join(raw_data_dir, 'atp_players.csv')
    df_players = pd.read_csv(players_file)
    players_parquet_path = os.path.join(processed_data_dir, 'atp_players.parquet')
    df_players.to_parquet(players_parquet_path, index=False)
    print(f"Sauvegardé {len(df_players)} joueurs dans {players_parquet_path}")
    
    # 3. Base de données SQLite locale pour croiser facilement
    # (plus simple que PostgreSQL pour démarrer sans setup complexe de serveur db)
    print("Création de la base SQLite locale...")
    db_path = os.path.join('data', 'bettinghud.db')
    engine = create_engine(f'sqlite:///{db_path}')
    
    df_matches.to_sql('matches', engine, if_exists='replace', index=False)
    df_players.to_sql('players', engine, if_exists='replace', index=False)
    print(f"Base de données SQLite créée: {db_path}")
    print("Ingestion terminée avec succès.")

if __name__ == "__main__":
    ingest_data()
