import sqlite3
import pandas as pd
import numpy as np
import random
import sys
import os
import glob
import re
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ml_model import TennisMLModel
from scripts.value_detector import ValueDetector


def _norm_player_name(name: str) -> str:
    n = str(name or "").lower().strip()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n)
    return n


def _load_tennis_data_odds_index(year_start: int, year_end: int):
    """
    Charge un index odds depuis data/raw/tennis_data/*.csv (format tennis-data.co.uk).
    Clé: (date, winner_norm, loser_norm) -> (odd_w, odd_l)
    Colonnes attendues (souples): Date/Winner/Loser + bookmaker odds (B365W/B365L, PSW/PSL, MaxW/MaxL...).
    """
    odds_dir = os.path.join("data", "raw", "tennis_data")
    files = (
        glob.glob(os.path.join(odds_dir, "*.csv"))
        + glob.glob(os.path.join(odds_dir, "*.xls"))
        + glob.glob(os.path.join(odds_dir, "*.xlsx"))
    )
    if not files:
        return {}

    idx = {}
    y0, y1 = min(int(year_start), int(year_end)), max(int(year_start), int(year_end))

    for f in files:
        ext = os.path.splitext(f)[1].lower()
        try:
            if ext in (".xls", ".xlsx"):
                sheets = pd.read_excel(f, sheet_name=None)
                frames = []
                for _sn, sdf in sheets.items():
                    if isinstance(sdf, pd.DataFrame) and len(sdf.columns) > 0:
                        frames.append(sdf)
                if not frames:
                    continue
                df = pd.concat(frames, ignore_index=True)
            else:
                df = pd.read_csv(f, low_memory=False)
        except Exception:
            continue

        cols = {c.lower(): c for c in df.columns}
        if "winner" not in cols or "loser" not in cols:
            continue

        # Date parse
        date_col = cols.get("date")
        if not date_col:
            continue
        dser = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True)
        df = df.assign(_date=dser)
        df = df[df["_date"].notna()]
        df = df[(df["_date"].dt.year >= y0) & (df["_date"].dt.year <= y1)]
        if df.empty:
            continue

        # Select preferred odds columns
        odd_pairs = [("b365w", "b365l"), ("psw", "psl"), ("maxw", "maxl"), ("avgw", "avgl"), ("cbw", "cbl")]
        chosen = None
        for w, l in odd_pairs:
            if w in cols and l in cols:
                chosen = (cols[w], cols[l])
                break
        if not chosen:
            continue
        ow_col, ol_col = chosen

        for _, row in df.iterrows():
            try:
                dt = row["_date"].strftime("%Y-%m-%d")
                wname = _norm_player_name(row[cols["winner"]])
                lname = _norm_player_name(row[cols["loser"]])
                ow = float(row[ow_col]) if pd.notna(row[ow_col]) else None
                ol = float(row[ol_col]) if pd.notna(row[ol_col]) else None
                if not ow or not ol or ow <= 1.0 or ol <= 1.0:
                    continue
                idx[(dt, wname, lname)] = (ow, ol)
            except Exception:
                continue

    return idx

def generate_bookmaker_odds(p1_pts, p2_pts, margin=0.05):
    """
    Génère des cotes fictives de bookmaker basées sur le ratio de points ATP.
    Ajoute une marge de bookmaker (overround) typique de 5%.
    """
    total_pts = p1_pts + p2_pts
    if total_pts == 0:
        p1_prob = 0.5
    else:
        p1_prob = p1_pts / total_pts
        
    # Ajouter un peu de bruit aléatoire (les bookmakers ont leurs propres ajustements)
    noise = random.uniform(-0.05, 0.05)
    p1_prob = max(0.1, min(0.9, p1_prob + noise))
    p2_prob = 1 - p1_prob
    
    # Appliquer la marge (overround)
    # proba_impliquée = vraie_proba * (1 + margin)
    implied_p1_prob = p1_prob * (1 + margin)
    implied_p2_prob = p2_prob * (1 + margin)
    
    odd_p1 = 1 / implied_p1_prob
    odd_p2 = 1 / implied_p2_prob
    
    return round(odd_p1, 2), round(odd_p2, 2), p1_prob


def _build_no_leak_model(df_matches: pd.DataFrame):
    """
    Entraîne un modèle isolé sans fuite:
    - train uniquement sur matches_recent (TML) avant le premier match du backtest.
    """
    first_test_date = str(df_matches["tourney_date"].min())
    cutoff = str(int(first_test_date) - 1)

    conn = sqlite3.connect("data/bettinghud.db")
    train_df = pd.read_sql(
        """
        SELECT * FROM matches_recent
        WHERE source = 'tennismylife'
          AND CAST(tourney_date AS TEXT) <= ?
        """,
        conn,
        params=(cutoff,),
    )
    conn.close()

    if train_df.empty:
        return None, cutoff, 0

    with tempfile.TemporaryDirectory() as td:
        tmp_db = os.path.join(td, "backtest_no_leak.db")
        tmp_conn = sqlite3.connect(tmp_db)
        train_df.to_sql("matches_recent", tmp_conn, if_exists="replace", index=False)
        tmp_conn.close()

        ml = TennisMLModel(db_path=tmp_db)
        ml.model_path = os.path.join(td, "xgb_model_no_leak.pkl")
        ml.feature_plot_path = os.path.join(td, "feature_importance_no_leak.png")
        ml.train()
        return ml, cutoff, len(train_df)

def simulate_day(
    date="2024-05-06",
    tournament="Rome Masters",
    return_data=False,
    year_start=2024,
    year_end=2024,
    max_matches=None,
    random_sample=True,
    random_seed=42,
    no_leak=True,
    min_value_threshold=0.05,
    base_stake=1.0,
    confidence_staking=False,
    staking_policy="fixed",  # fixed|adaptive|kelly_0_1|kelly_0_25
    bankroll_start=1000.0,
    drawdown_limit_pct=35.0,
    capture_delay_prob=0.0,
    market_unavailable_prob=0.0,
    slippage_pct=0.0,
    max_stake_pct_bankroll=10.0,
):
    # Reproductibilité des randomisations (ordre P1/P2 + odds simulées fallback)
    random_seed = int(random_seed)
    random.seed(random_seed)

    if not return_data:
        print(f"[TENNIS] SIMULATION DE LA JOURNÉE: {date} - {tournament}")
        print("="*60)
    
    conn = sqlite3.connect('data/bettinghud.db')
    year_start = int(year_start)
    year_end = int(year_end)
    if year_start > year_end:
        year_start, year_end = year_end, year_start

    # Fenêtre inclusive [year_start0101, year_end1231] sur tourney_date (YYYYMMDD)
    date_min = f"{year_start}0101"
    date_max = f"{year_end}1231"
    query = (
        "SELECT * FROM matches_recent "
        "WHERE tourney_name = ? "
        "AND source = 'tennismylife' "
        "AND CAST(tourney_date AS TEXT) BETWEEN ? AND ?"
    )
    df_matches = pd.read_sql(query, conn, params=(tournament, date_min, date_max))
    conn.close()
    odds_idx = _load_tennis_data_odds_index(year_start, year_end)

    if max_matches is not None:
        max_matches = int(max_matches)
        if max_matches > 0 and len(df_matches) > max_matches:
            if random_sample:
                df_matches = df_matches.sample(n=max_matches, random_state=random_seed)
            else:
                # Sinon on garde les plus récents
                df_matches = df_matches.sort_values('tourney_date', ascending=False).head(max_matches)
            df_matches = df_matches.reset_index(drop=True)
    
    if df_matches.empty:
        if not return_data:
            print("Aucun match trouvé pour cette date.")
        return None, []
        
    if not return_data:
        print(f"Nombre total de matchs ce jour-là: {len(df_matches)}")
    
    # Initialisation IA
    cutoff = None
    train_rows = None
    if no_leak:
        ml_model, cutoff, train_rows = _build_no_leak_model(df_matches)
        if ml_model is None:
            if not return_data:
                print("Impossible de construire le modèle no-leak (jeu d'entraînement vide).")
            return None, []
        if not return_data:
            print(f"Mode NO-LEAK actif: entraînement <= {cutoff} ({train_rows} lignes).")
    else:
        ml_model = TennisMLModel()
        # Charge bundle modèle + paramètres auxiliaires
        if ml_model.model is None and os.path.exists(ml_model.model_path):
            ml_model._load_bundle_if_needed()
        
    detector = ValueDetector(min_value_threshold=float(min_value_threshold))

    def _stake_multiplier(conf):
        try:
            c = float(conf)
        except Exception:
            return 1.0
        if c < 0.25:
            return 0.50
        if c < 0.50:
            return 0.75
        if c < 0.75:
            return 1.00
        return 1.25
    
    stats = {
        "total_matches": len(df_matches),
        "correct_predictions": 0,
        "total_bets_placed": 0,
        "winning_bets": 0,
        "net_profit": 0.0,  # en devise de mise (base_stake)
        "total_staked": 0.0,
        "bankroll_start": float(bankroll_start),
        "bankroll": float(bankroll_start),
        "max_bankroll": float(bankroll_start),
        "max_drawdown_pct": 0.0,
        "skipped_market_unavailable": 0,
        "skipped_drawdown": 0,
    }
    
    if not return_data:
        print("\n--- ANALYSE MATCH PAR MATCH ---")
    
    # Pour afficher un échantillon
    sample_size = 15
    count = 0
    matches_results = []
    
    for _, row in df_matches.iterrows():
        # Pour ne pas que le modèle "sache" qui gagne, on randomize P1 et P2
        is_p1_winner = random.choice([True, False])
        
        if is_p1_winner:
            p1_name, p1_rank, p1_age, p1_ht, p1_pts = row['winner_name'], row['winner_rank'], row['winner_age'], row['winner_ht'], row['winner_rank_points']
            p2_name, p2_rank, p2_age, p2_ht, p2_pts = row['loser_name'], row['loser_rank'], row['loser_age'], row['loser_ht'], row['loser_rank_points']
        else:
            p1_name, p1_rank, p1_age, p1_ht, p1_pts = row['loser_name'], row['loser_rank'], row['loser_age'], row['loser_ht'], row['loser_rank_points']
            p2_name, p2_rank, p2_age, p2_ht, p2_pts = row['winner_name'], row['winner_rank'], row['winner_age'], row['winner_ht'], row['winner_rank_points']
            
        # Gérer les NaNs
        p1_rank = 100 if pd.isna(p1_rank) else p1_rank
        p2_rank = 100 if pd.isna(p2_rank) else p2_rank
        p1_age = 25 if pd.isna(p1_age) else p1_age
        p2_age = 25 if pd.isna(p2_age) else p2_age
        p1_ht = 185 if pd.isna(p1_ht) else p1_ht
        p2_ht = 185 if pd.isna(p2_ht) else p2_ht
        p1_pts = 1000 if pd.isna(p1_pts) else p1_pts
        p2_pts = 1000 if pd.isna(p2_pts) else p2_pts
        
        # 1. Obtenir les cotes bookmakers (réelles tennis-data si disponibles, sinon fictives)
        raw_dt = str(row.get("tourney_date", ""))
        match_date = f"{raw_dt[:4]}-{raw_dt[4:6]}-{raw_dt[6:8]}" if len(raw_dt) >= 8 else raw_dt
        key = (match_date, _norm_player_name(row["winner_name"]), _norm_player_name(row["loser_name"]))
        real_pair = odds_idx.get(key)
        if real_pair:
            odd_w, odd_l = real_pair
            if is_p1_winner:
                bm_odd_p1, bm_odd_p2 = odd_w, odd_l
            else:
                bm_odd_p1, bm_odd_p2 = odd_l, odd_w
        else:
            bm_odd_p1, bm_odd_p2, _ = generate_bookmaker_odds(p1_pts, p2_pts)
        
        # 2. Obtenir nos prédictions ML
        try:
            preds = ml_model.predict_match(
                surface=row['surface'],
                p1_name=p1_name,
                p2_name=p2_name,
                p1_rank=p1_rank, p2_rank=p2_rank,
                p1_age=p1_age, p2_age=p2_age,
                p1_ht=p1_ht, p2_ht=p2_ht,
                p1_pts=p1_pts, p2_pts=p2_pts,
                p1_id=row['winner_id'] if is_p1_winner else row['loser_id'],
                p2_id=row['loser_id'] if is_p1_winner else row['winner_id'],
                p1_form_win_pct_90=50,
                p2_form_win_pct_90=50,
                p1_fatigue_minutes_14=0,
                p2_fatigue_minutes_14=0,
                p1_hand=row['winner_hand'] if is_p1_winner else row['loser_hand'],
                p2_hand=row['loser_hand'] if is_p1_winner else row['winner_hand'],
                tournament_name=row['tourney_name'],
                p1_ioc=row['winner_ioc'] if is_p1_winner else row['loser_ioc'],
                p2_ioc=row['loser_ioc'] if is_p1_winner else row['winner_ioc'],
            )
        except Exception as e:
            continue
            
        # Vérifier si notre modèle a trouvé le bon gagnant
        predicted_winner_is_p1 = preds['p1_win_prob'] > 0.5
        if predicted_winner_is_p1 == is_p1_winner:
            stats["correct_predictions"] += 1
            
        # 3. Chercher de la Value
        p1_val = detector.detect_value(bm_odd_p1, preds['p1_true_odd'], confidence=preds.get('confidence'))
        p2_val = detector.detect_value(bm_odd_p2, preds['p2_true_odd'], confidence=preds.get('confidence'))
        
        bet_placed_on = None
        bet_odd = 0
        is_bet_won = False
        ev = 0.0
        
        if p1_val['is_value'] and p1_val['value_pct'] > p2_val['value_pct']:
            bet_placed_on = "P1"
            bet_odd = bm_odd_p1
            is_bet_won = is_p1_winner
            ev = p1_val['value_pct']
        elif p2_val['is_value']:
            bet_placed_on = "P2"
            bet_odd = bm_odd_p2
            is_bet_won = not is_p1_winner
            ev = p2_val['value_pct']
            
        # 4. Bilan financier
        profit_on_match = 0
        stake_amount = float(base_stake)
        kelly_reco_stake = None
        if bet_placed_on:
            # execution-aware frictions
            if random.random() < float(market_unavailable_prob):
                stats["skipped_market_unavailable"] += 1
                continue
            # capture delay can degrade displayed odds
            if random.random() < float(capture_delay_prob):
                if bet_placed_on == "P1":
                    bet_odd = max(1.01, bet_odd * (1.0 - abs(float(slippage_pct))))
                else:
                    bet_odd = max(1.01, bet_odd * (1.0 - abs(float(slippage_pct))))

            # Recommended stake (independent guidance):
            # quarter-Kelly, no stop, max 5% of current bankroll.
            p_side = float(preds["p1_win_prob"]) if bet_placed_on == "P1" else float(preds["p2_win_prob"])
            b_side = max(0.01, float(bet_odd) - 1.0)
            kelly_full = max(0.0, (b_side * p_side - (1.0 - p_side)) / b_side)
            kelly_reco_stake = float(stats["bankroll"]) * 0.25 * kelly_full
            kelly_reco_stake = min(kelly_reco_stake, float(stats["bankroll"]) * 0.05)
            kelly_reco_stake = max(0.0, kelly_reco_stake)

            # staking policy
            if staking_policy == "adaptive" or confidence_staking:
                stake_amount *= _stake_multiplier(preds.get("confidence"))
            elif staking_policy in ("kelly_0_1", "kelly_0_25"):
                frac = 0.1 if staking_policy == "kelly_0_1" else 0.25
                p = float(preds["p1_win_prob"]) if bet_placed_on == "P1" else float(preds["p2_win_prob"])
                b = max(0.01, float(bet_odd) - 1.0)
                k = max(0.0, (b * p - (1 - p)) / b)
                stake_amount = float(stats["bankroll"]) * frac * k

            # max stake dynamic cap
            stake_amount = min(
                stake_amount,
                float(stats["bankroll"]) * (float(max_stake_pct_bankroll) / 100.0),
            )
            stake_amount = max(0.0, stake_amount)
            if stake_amount <= 0.0:
                continue

            # drawdown constraint
            if float(stats["max_bankroll"]) > 0:
                dd_now = (float(stats["max_bankroll"]) - float(stats["bankroll"])) / float(stats["max_bankroll"]) * 100.0
                if dd_now >= float(drawdown_limit_pct):
                    stats["skipped_drawdown"] += 1
                    continue
            stats["total_bets_placed"] += 1
            stats["total_staked"] += stake_amount
            if is_bet_won:
                stats["winning_bets"] += 1
                profit_on_match = (bet_odd - 1) * stake_amount
            else:
                profit_on_match = -stake_amount
                
            stats["net_profit"] += profit_on_match
            stats["bankroll"] += profit_on_match
            stats["max_bankroll"] = max(float(stats["max_bankroll"]), float(stats["bankroll"]))
            if float(stats["max_bankroll"]) > 0:
                dd = (float(stats["max_bankroll"]) - float(stats["bankroll"])) / float(stats["max_bankroll"]) * 100.0
                stats["max_drawdown_pct"] = max(float(stats["max_drawdown_pct"]), float(dd))
            
        player_bet = p1_name if bet_placed_on == "P1" else p2_name if bet_placed_on == "P2" else None
        
        match_res_dict = {
            "Match": f"{p1_name} vs {p2_name}",
            "Cotes BM": f"{bm_odd_p1} / {bm_odd_p2}",
            "Cotes IA": f"{preds['p1_true_odd']:.2f} / {preds['p2_true_odd']:.2f}",
            "Pari Placé": player_bet if bet_placed_on else "Aucun",
            "Mise": round(stake_amount, 2) if bet_placed_on else None,
            "Mise reco Kelly 1/4 (cap 5% BR)": round(kelly_reco_stake, 2) if kelly_reco_stake is not None else None,
            "Cote Pari": bet_odd if bet_placed_on else None,
            "EV (%)": f"{ev:.1f}%" if bet_placed_on else None,
            "Résultat": "✅ GAGNÉ" if bet_placed_on and is_bet_won else "❌ PERDU" if bet_placed_on else "-",
            "Profit (U)": round(profit_on_match, 2) if bet_placed_on else 0.0
        }
        matches_results.append(match_res_dict)
            
        # Affichage
        if not return_data:
            count += 1
            if count <= sample_size:
                print(f"\nMatch: {p1_name} vs {p2_name}")
                print(f"Cotes Bookmaker: {bm_odd_p1} / {bm_odd_p2}")
                print(f"Cotes BettingHUD: {preds['p1_true_odd']:.2f} / {preds['p2_true_odd']:.2f}")
                if bet_placed_on:
                    print(f"[PARI PLACÉ]: {stake_amount:.2f} sur {player_bet} @{bet_odd} (EV: +{ev:.1f}%)")
                    if is_bet_won:
                        print(f"[GAGNÉ] ! Profit: +{profit_on_match:.2f} U")
                    else:
                        print(f"[PERDU] ! Profit: {profit_on_match:.2f} U")
                else:
                    print("[PAS DE PARI] Aucune Value")
                
    if return_data:
        if no_leak and stats is not None:
            stats["no_leak"] = True
            stats["train_cutoff"] = cutoff
            stats["train_rows"] = train_rows
        return stats, matches_results
        
    # --- BILAN GLOBAL ---
    print("\n" + "="*60)
    print("[BILAN GLOBAL] DE LA JOURNÉE")
    print("="*60)
    accuracy = (stats["correct_predictions"] / stats["total_matches"]) * 100
    print(f"Matchs analysés: {stats['total_matches']}")
    print(f"Précision brute du modèle (choix du vainqueur): {accuracy:.1f}%")
    
    print("\n[RESULTATS FINANCIERS] (Mise = 1 Unité par pari)")
    print(f"Paris placés (Value Bets > 5% EV): {stats['total_bets_placed']}")
    if stats['total_bets_placed'] > 0:
        winrate_bets = (stats['winning_bets'] / stats['total_bets_placed']) * 100
        roi = (stats["net_profit"] / stats["total_bets_placed"]) * 100
        print(f"Taux de réussite des paris: {winrate_bets:.1f}%")
        print(f"Profit Net: {stats['net_profit']:.2f} Unités")
        print(f"ROI (Retour sur investissement): {roi:.1f}%")
    else:
        print("Aucun pari placé (le bookmaker était trop précis).")

if __name__ == "__main__":
    simulate_day()