class ValueDetector:
    def __init__(self, min_value_threshold=0.15):
        self.min_value_threshold = min_value_threshold

    @staticmethod
    def bet_sharpe_ratio(true_prob, bookmaker_odd):
        """Rapport de Sharpe unitaire : EV / écart-type du gain sur 1 unité misée.

        Pour un pari binaire à cote O et proba modèle p :
          EV = p*(O-1) - (1-p)
          Var = p*(O-1)² + (1-p) - EV²
        """
        try:
            p = float(true_prob)
            o = float(bookmaker_odd)
        except (TypeError, ValueError):
            return 0.0
        if p <= 0.0 or p >= 1.0 or o <= 1.0:
            return 0.0
        ev = p * (o - 1.0) - (1.0 - p)
        er2 = p * (o - 1.0) ** 2 + (1.0 - p)
        var = er2 - ev * ev
        if var <= 1e-12:
            return 0.0
        return float(ev / (var ** 0.5))

    @staticmethod
    def calculate_line_drift(opening_odd, current_odd):
        """Relative move between opening and current decimal odds.

        Positive when the quoted price lengthens (odds rise).
        Returns 0 when inputs are invalid or missing.
        """
        try:
            o = float(opening_odd)
            c = float(current_odd)
        except (TypeError, ValueError):
            return 0.0
        if o <= 1.0 or c <= 1.0:
            return 0.0
        return (c - o) / o

    @staticmethod
    def implied_prob(odd):
        try:
            x = float(odd)
        except (TypeError, ValueError):
            return None
        if x <= 1.0:
            return None
        return 1.0 / x

    @staticmethod
    def market_sentiment_signal_p1(opening_odd_p1, current_odd_p1):
        """Change in implied win probability for P1 between open and current (book).

        Negative when odds lengthen (market rates P1 weaker). Training rows use 0 when odds unknown.
        """
        po = ValueDetector.implied_prob(opening_odd_p1)
        pc = ValueDetector.implied_prob(current_odd_p1)
        if po is None or pc is None:
            return 0.0
        return float(pc - po)

    @staticmethod
    def calculate_clv_score(odd_taken, closing_odd):
        """Closing Line Value (indépendant du résultat du match).

        CLV = (cote_prise / cote_closing) - 1
        > 0 : meilleur prix que le closing.
        """
        try:
            o_taken = float(odd_taken)
            o_close = float(closing_odd)
        except (TypeError, ValueError):
            return None
        if o_taken <= 1.0 or o_close <= 1.0:
            return None
        return float((o_taken / o_close) - 1.0)

    def detect_value(
        self,
        bookmaker_odd,
        true_odd,
        confidence=None,
        opening_odd=None,
        current_odd=None,
    ):
        """
        Calcule la value d'un pari.
        bookmaker_odd: la cote affichée par le bookmaker (ex: 2.10)
        true_odd: la "vraie" cote estimée par notre modèle (ex: 1.80)

        optional opening_odd / current_odd (même côté que bookmaker_odd) pour malus de confiance
        quand la ligne s'éloigne de notre proba (ex.: cote qui monte alors qu'on soutient ce joueur).
        """
        if not bookmaker_odd or not true_odd or bookmaker_odd <= 1.0 or true_odd <= 1.0:
            return {"is_value": False, "value_pct": 0, "expected_yield": 0, "sharpe_ratio": 0.0}

        true_prob = 1 / true_odd

        expected_yield = (true_prob * (bookmaker_odd - 1)) - ((1 - true_prob) * 1)

        confidence_penalty = 0.0
        if opening_odd is not None and current_odd is not None:
            try:
                o0 = float(opening_odd)
                c0 = float(current_odd)
            except (TypeError, ValueError):
                o0 = c0 = None
            if o0 and c0 and o0 > 1.0 and c0 > 1.0:
                drift = self.calculate_line_drift(o0, c0)
                impl_open = 1.0 / o0
                impl_cur = 1.0 / c0
                if drift > 0 and impl_cur < impl_open:
                    confidence_penalty = min(0.35, drift * 0.85)

        threshold = self.min_value_threshold
        conf_eff = None
        if confidence is not None:
            try:
                conf_eff = max(0.0, min(1.0, float(confidence)))
            except Exception:
                conf_eff = None
            if conf_eff is not None:
                if confidence_penalty > 0:
                    conf_eff = max(0.0, conf_eff * (1.0 - confidence_penalty))
                threshold = self.min_value_threshold * (2.0 - conf_eff)
        elif confidence_penalty > 0:
            threshold = self.min_value_threshold * (1.0 + 0.5 * confidence_penalty)

        is_value = expected_yield >= threshold
        sharpe_ratio = ValueDetector.bet_sharpe_ratio(true_prob, bookmaker_odd)

        return {
            "is_value": is_value,
            "value_pct": expected_yield * 100,
            "sharpe_ratio": sharpe_ratio,
            "bookmaker_odd": bookmaker_odd,
            "true_odd": true_odd,
            "true_prob": true_prob * 100,
            "confidence_penalty": confidence_penalty,
            "effective_confidence": conf_eff if confidence is not None else None,
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
