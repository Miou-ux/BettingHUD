import sys
import os
import asyncio
from scripts.stats_engine import TennisStatsEngine
from scripts.ml_model import TennisMLModel
from scripts.value_detector import ValueDetector

def main():
    print("========================================")
    print("   [TENNIS] BETTING HUD - DEMO PIPELINE [TENNIS]    ")
    print("========================================\n")
    
    print("[1] Initialisation des moteurs d'analyse...")
    try:
        stats_engine = TennisStatsEngine()
        ml_model = TennisMLModel()
        detector = ValueDetector(min_value_threshold=0.03) # 3% ROI
        
        # S'assurer que le modèle est chargé
        if ml_model.model is None:
            # On simule un entraînement si le modèle n'est pas encore sur disque
            # ml_model.train() 
            pass
            
        print("[OK] Moteurs initialisés avec succès.\n")
    except Exception as e:
        print(f"[ERREUR] Erreur lors de l'initialisation: {e}")
        print("Veuillez d'abord exécuter ingest_atp_data.py et ml_model.py")
        return

    print("[2] Simulation d'une détection de Value Bet...")
    print("Scénario: Alexander Zverev (Rank 4, 27 ans, 198cm, 6000pts) affronte")
    print("          Gael Monfils (Rank 15, 37 ans, 193cm, 2000pts) sur Terre Battue.\n")
    
    # 1. On estime les vraies cotes avec le ML
    try:
        predictions = ml_model.predict_match(
            surface="Clay",
            p1_rank=4, p2_rank=15,
            p1_age=27, p2_age=37,
            p1_ht=198, p2_ht=193,
            p1_pts=6000, p2_pts=2000
        )
        print("--- Évaluation du Modèle ---")
        print(f"Zverev (P1) Probabilité: {predictions['p1_win_prob']:.2%} -> True Odd: {predictions['p1_true_odd']:.2f}")
        print(f"Monfils (P2) Probabilité: {predictions['p2_win_prob']:.2%} -> True Odd: {predictions['p2_true_odd']:.2f}")
    except Exception as e:
        print(f"Erreur prédiction: {e}")
        return

    print("\n[3] Comparaison avec les cotes Bookmaker...")
    # Scénario: Le bookmaker sur-estime les chances de Monfils (Cote 2.50 au lieu de > 3.00)
    # et Zverev est à 1.55 (Value Bet potentiel car notre modèle le donne à 1.25)
    bookmaker_p1_odd = 1.55
    bookmaker_p2_odd = 2.50
    
    print(f"Cotes Bookmaker: Zverev ({bookmaker_p1_odd}) - Monfils ({bookmaker_p2_odd})")
    
    p1_eval = detector.detect_value(bookmaker_p1_odd, predictions["p1_true_odd"])
    p2_eval = detector.detect_value(bookmaker_p2_odd, predictions["p2_true_odd"])
    
    if p1_eval["is_value"]:
        print(f"\n[VALUE BET DETECTE SUR ZVEREV]")
        print(f"Cote actuelle: {bookmaker_p1_odd} | Vraie cote calculée: {predictions['p1_true_odd']:.2f}")
        print(f"Espérance de gain (Yield): {p1_eval['value_pct']:.2f}%")
        
    if p2_eval["is_value"]:
        print(f"\n[VALUE BET DETECTE SUR MONFILS]")
        print(f"Cote actuelle: {bookmaker_p2_odd} | Vraie cote calculée: {predictions['p2_true_odd']:.2f}")
        print(f"Espérance de gain (Yield): {p2_eval['value_pct']:.2f}%")

    if not p1_eval["is_value"] and not p2_eval["is_value"]:
        print("\n[AUCUN VALUE BET] Aucun Value Bet détecté sur ce match. Il vaut mieux s'abstenir.")

    print("\n========================================")
    print("Pipeline de démonstration terminé.")
    print("Pour lancer l'interface complète: streamlit run app/dashboard.py")
    
if __name__ == "__main__":
    main()