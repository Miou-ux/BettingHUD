import pandas as pd
import numpy as np
import os
import sqlite3
from scripts.player_identity import PlayerIdentityResolver

class TennisStatsEngine:
    def __init__(self, db_path="data/bettinghud.db"):
        self.db_path = db_path
        self.matches_df = None
        self.players_df = None
        self.identity = PlayerIdentityResolver(db_path=db_path)
        self._load_data()

    def _load_data(self):
        """Charge les données depuis la base SQLite"""
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Base de données {self.db_path} non trouvée. Exécutez d'abord ingest_atp_data.py")
        
        # On utilise sqlite3 directement car plus rapide pour des requêtes simples
        conn = sqlite3.connect(self.db_path)
        self.matches_df = pd.read_sql("SELECT * FROM matches", conn)
        self.players_df = pd.read_sql("SELECT * FROM players", conn)
        conn.close()
        
        # Conversion des dates si nécessaire
        self.matches_df['tourney_date'] = pd.to_datetime(self.matches_df['tourney_date'])

    def get_player_id(self, player_name):
        """Récupère l'ID d'un joueur à partir de son nom"""
        pid = self.identity.resolve_player_id(player_name, self.players_df)
        return pid

    def get_player_stats(self, player_id):
        """Récupère les stats d'un joueur (rank, age, ht, pts) à partir de son dernier match connu"""
        # Cherche le joueur en tant que vainqueur
        wins = self.matches_df[self.matches_df['winner_id'] == player_id]
        losses = self.matches_df[self.matches_df['loser_id'] == player_id]
        
        last_win = wins.iloc[-1] if not wins.empty else None
        last_loss = losses.iloc[-1] if not losses.empty else None
        
        # Trouver le match le plus récent
        last_match = None
        if last_win is not None and last_loss is not None:
            last_match = last_win if last_win['tourney_date'] > last_loss['tourney_date'] else last_loss
        elif last_win is not None:
            last_match = last_win
        elif last_loss is not None:
            last_match = last_loss
            
        if last_match is None:
            return {"rank": 100, "age": 25, "ht": 185, "pts": 1000} # Valeurs par défaut moyennes
            
        is_winner = last_match['winner_id'] == player_id
        
        rank = last_match['winner_rank'] if is_winner else last_match['loser_rank']
        age = last_match['winner_age'] if is_winner else last_match['loser_age']
        ht = last_match['winner_ht'] if is_winner else last_match['loser_ht']
        pts = last_match['winner_rank_points'] if is_winner else last_match['loser_rank_points']
        hand = last_match['winner_hand'] if is_winner else last_match['loser_hand']
        
        # Gérer les NaNs
        rank = 100 if pd.isna(rank) else rank
        age = 25 if pd.isna(age) else age
        ht = 185 if pd.isna(ht) else ht
        pts = 1000 if pd.isna(pts) else pts
        hand = 'U' if pd.isna(hand) else hand
        
        return {"rank": rank, "age": age, "ht": ht, "pts": pts, "hand": hand}

    def get_h2h(self, p1_id, p2_id):
        """Récupère l'historique des confrontations directes"""
        if not p1_id or not p2_id:
            return {"p1_wins": 0, "p2_wins": 0}
            
        p1_wins = len(self.matches_df[(self.matches_df['winner_id'] == p1_id) & (self.matches_df['loser_id'] == p2_id)])
        p2_wins = len(self.matches_df[(self.matches_df['winner_id'] == p2_id) & (self.matches_df['loser_id'] == p1_id)])
        
        return {"p1_wins": p1_wins, "p2_wins": p2_wins}

    def get_recent_form(self, player_id, days=90):
        """Récupère la forme récente (% de victoires sur les derniers X jours)"""
        if not player_id:
            return {"win_pct": 50.0, "matches": 0}
            
        max_date = self.matches_df['tourney_date'].max()
        recent_date = max_date - pd.Timedelta(days=days)
        recent_matches = self.matches_df[self.matches_df['tourney_date'] >= recent_date]
        
        wins = len(recent_matches[recent_matches['winner_id'] == player_id])
        losses = len(recent_matches[recent_matches['loser_id'] == player_id])
        total = wins + losses
        
        win_pct = (wins / total * 100) if total > 0 else 50.0
        return {"win_pct": win_pct, "matches": total, "wins": wins, "losses": losses}

    def get_recent_fatigue(self, player_id, days=14):
        """Calcule le temps passé sur le court récemment (en minutes) pour déceler la fatigue"""
        if not player_id:
            return {"minutes_played": 0, "matches": 0}
            
        max_date = self.matches_df['tourney_date'].max()
        recent_date = max_date - pd.Timedelta(days=days)
        recent_matches = self.matches_df[self.matches_df['tourney_date'] >= recent_date]
        
        wins_df = recent_matches[recent_matches['winner_id'] == player_id]
        losses_df = recent_matches[recent_matches['loser_id'] == player_id]
        
        total_mins = wins_df['minutes'].sum() + losses_df['minutes'].sum()
        total_matches = len(wins_df) + len(losses_df)
        
        return {"minutes_played": int(total_mins) if pd.notna(total_mins) else 0, "matches": total_matches}

    def get_service_hold_probability(self, player_id, surface=None, pressure=False):
        """
        Calcule la probabilité qu'un joueur gagne son jeu de service.
        Si pressure=True, on filtre sur des situations de pression (ex: balle de break contre, ou fin de set)
        Note: Les données ATP de base (Jeff Sackmann) contiennent w_svpt (points servis), w_1stIn, etc.
        """
        # Filtrer les matchs où le joueur est le vainqueur ou le perdant
        wins = self.matches_df[self.matches_df['winner_id'] == player_id].copy()
        losses = self.matches_df[self.matches_df['loser_id'] == player_id].copy()
        
        if surface and surface in ['Hard', 'Clay', 'Grass', 'Carpet']:
            wins = wins[wins['surface'] == surface]
            losses = losses[losses['surface'] == surface]
            
        # Calcul des jeux de service gagnés
        # w_SvGms = jeux de service du vainqueur
        # w_bpSaved = balles de break sauvées
        # w_bpFaced = balles de break affrontées
        
        # Jeux de service perdus par le vainqueur = balles de break affrontées - sauvées
        wins['service_games_lost'] = wins['w_bpFaced'] - wins['w_bpSaved']
        wins['service_games_won'] = wins['w_SvGms'] - wins['service_games_lost']
        
        # Idem pour le perdant
        losses['service_games_lost'] = losses['l_bpFaced'] - losses['l_bpSaved']
        losses['service_games_won'] = losses['l_SvGms'] - losses['service_games_lost']
        
        total_service_games = wins['w_SvGms'].sum() + losses['l_SvGms'].sum()
        total_service_games_won = wins['service_games_won'].sum() + losses['service_games_won'].sum()
        
        if total_service_games == 0 or pd.isna(total_service_games):
            return 0.5 # Valeur par défaut
            
        hold_prob = total_service_games_won / total_service_games
        
        if pressure:
            # Approximation grossière : un joueur sous pression performe différemment
            # On utilise le taux de balles de break sauvées comme proxy de la "pression"
            total_bp_faced = wins['w_bpFaced'].sum() + losses['l_bpFaced'].sum()
            total_bp_saved = wins['w_bpSaved'].sum() + losses['l_bpSaved'].sum()
            
            if total_bp_faced > 0:
                bp_save_rate = total_bp_saved / total_bp_faced
                # On ajuste la probabilité de hold par la capacité à sauver des BP
                hold_prob = (hold_prob + bp_save_rate) / 2
                
        return float(hold_prob)

    def calculate_situational_probability(self, server_name, returner_name, surface, set_score, games_score):
        """
        Calcule la probabilité que le serveur gagne le jeu en cours selon le contexte.
        Ex: Nadal (serveur) vs Federer (relanceur), Terre Battue, 4-5.
        """
        server_id = self.get_player_id(server_name)
        returner_id = self.get_player_id(returner_name)
        
        if not server_id or not returner_id:
            print(f"Joueur introuvable: {server_name} ou {returner_name}")
            return 0.5
            
        # Déterminer si situation de pression (ex: sert pour rester dans le set à 4-5 ou 5-6)
        games_split = [int(g) for g in games_score.split('-')]
        pressure = False
        if len(games_split) == 2:
            s_games, r_games = games_split
            if r_games >= 5 and s_games < r_games:
                pressure = True
                
        # 1. Probabilité que le serveur tienne son service
        server_hold_prob = self.get_service_hold_probability(server_id, surface=surface, pressure=pressure)
        
        # 2. Probabilité que le relanceur breake
        # (À implémenter: get_return_break_probability)
        # Pour simplifier dans cette v1, on prend l'inverse de son propre hold rate
        returner_hold_prob = self.get_service_hold_probability(returner_id, surface=surface, pressure=False)
        return_break_prob = 1 - returner_hold_prob
        
        # Combinaison (Formule de base d'ajustement: Moyenne entre la force du serveur et la force du relanceur)
        # En réalité, la formule mathématique exacte de combinatoire est plus complexe.
        combined_hold_prob = server_hold_prob - (0.5 - (1 - return_break_prob))
        
        # Borner entre 0.01 et 0.99
        combined_hold_prob = max(0.01, min(0.99, combined_hold_prob))
        
        return {
            "server": server_name,
            "returner": returner_name,
            "surface": surface,
            "situation": f"{set_score} {games_score}",
            "pressure_detected": pressure,
            "raw_server_hold_prob": server_hold_prob,
            "adjusted_hold_prob": combined_hold_prob,
            "true_odd": 1 / combined_hold_prob if combined_hold_prob > 0 else 0
        }

if __name__ == "__main__":
    engine = TennisStatsEngine()
    
    # Exemple 1: Nadal sert contre Federer sur terre battue à 4-5
    result = engine.calculate_situational_probability(
        server_name="Rafael Nadal",
        returner_name="Roger Federer",
        surface="Clay",
        set_score="0-0",
        games_score="4-5"
    )
    
    print(f"\n--- Analyse de situation ---")
    print(f"Serveur: {result['server']}")
    print(f"Relanceur: {result['returner']}")
    print(f"Surface: {result['surface']}")
    print(f"Situation de pression: {result['pressure_detected']}")
    print(f"Probabilité ajustée de gagner le jeu: {result['adjusted_hold_prob']:.2%}")
    print(f"Vraie cote estimée pour le gain du jeu: {result['true_odd']:.2f}")

    # Exemple 2: Isner sert sur gazon à 1-1
    result2 = engine.calculate_situational_probability(
        server_name="John Isner",
        returner_name="Novak Djokovic",
        surface="Grass",
        set_score="0-0",
        games_score="1-1"
    )
    print(f"\n--- Analyse de situation ---")
    print(f"Serveur: {result2['server']}")
    print(f"Relanceur: {result2['returner']}")
    print(f"Surface: {result2['surface']}")
    print(f"Probabilité ajustée de gagner le jeu: {result2['adjusted_hold_prob']:.2%}")
    print(f"Vraie cote estimée pour le gain du jeu: {result2['true_odd']:.2f}")
