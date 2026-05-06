class ValueDetector:
    def __init__(self, min_value_threshold=0.05):
        self.min_value_threshold = min_value_threshold

    def detect_value(self, bookmaker_odd, true_odd, confidence=None):
        """
        Calcule la value d'un pari.
        bookmaker_odd: la cote affichée par le bookmaker (ex: 2.10)
        true_odd: la "vraie" cote estimée par notre modèle (ex: 1.80)
        
        Retourne un dictionnaire avec la value en % et un booléen indiquant si c'est un value bet.
        """
        if not bookmaker_odd or not true_odd or bookmaker_odd <= 1.0 or true_odd <= 1.0:
            return {"is_value": False, "value_pct": 0, "expected_yield": 0}

        # Probabilité implicite du bookmaker
        implied_prob = 1 / bookmaker_odd
        
        # Notre probabilité estimée
        true_prob = 1 / true_odd
        
        # Espérance de gain (Expected Value / Yield)
        # EV = (Probabilité de Gagner * Profit en cas de victoire) - (Probabilité de Perdre * Mise)
        # Profit net pour une mise de 1 unité = bookmaker_odd - 1
        expected_yield = (true_prob * (bookmaker_odd - 1)) - ((1 - true_prob) * 1)
        
        # Alternative: Ratio entre notre proba et la proba du bookmaker
        # value_pct = (true_prob / implied_prob) - 1
        
        # Si on a une mesure de confiance, on devient plus strict quand la confiance est faible.
        # confidence est supposé dans [0, 1] (0 = incertain, 1 = très confiant).
        threshold = self.min_value_threshold
        if confidence is not None:
            try:
                conf = float(confidence)
                conf = max(0.0, min(1.0, conf))
                # conf=0 -> seuil x2 ; conf=1 -> seuil x1
                threshold = self.min_value_threshold * (2.0 - conf)
            except Exception:
                threshold = self.min_value_threshold

        is_value = expected_yield >= threshold
        
        return {
            "is_value": is_value,
            "value_pct": expected_yield * 100, # En pourcentage d'espérance de gain
            "bookmaker_odd": bookmaker_odd,
            "true_odd": true_odd,
            "true_prob": true_prob * 100
        }

    def analyze_match(self, match_data, model_predictions):
        """
        Analyse un match avec les cotes P1/P2 du bookmaker et les prédictions du modèle
        """
        p1_eval = self.detect_value(match_data.get("odd_p1"), model_predictions.get("p1_true_odd"))
        p2_eval = self.detect_value(match_data.get("odd_p2"), model_predictions.get("p2_true_odd"))
        
        return {
            "p1_value": p1_eval,
            "p2_value": p2_eval
        }

if __name__ == "__main__":
    detector = ValueDetector(min_value_threshold=0.03) # 3% de ROI minimum attendu
    
    # Exemple: Le modèle estime la vraie cote à 1.80 (55.5%), le bookmaker offre 2.10 (47.6%)
    res = detector.detect_value(bookmaker_odd=2.10, true_odd=1.80)
    print(f"Test Value Bet: Bookmaker=2.10 | True=1.80")
    print(f"Est Value ? {res['is_value']}")
    print(f"ROI espéré : {res['value_pct']:.2f}%")
    
    # Exemple 2: Faux value bet (bookmaker offre 1.70, vraie cote = 1.80)
    res2 = detector.detect_value(bookmaker_odd=1.70, true_odd=1.80)
    print(f"\nTest Mauvais Bet: Bookmaker=1.70 | True=1.80")
    print(f"Est Value ? {res2['is_value']}")
    print(f"ROI espéré : {res2['value_pct']:.2f}%")