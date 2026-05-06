import asyncio
import json
from playwright.async_api import async_playwright
import time

class FlashscoreLiveScraper:
    def __init__(self, match_id):
        # Format attendu par flashscore ex: "hUZ4xV7a"
        self.match_id = match_id
        # URL du détail du match (onglet summary ou odds)
        self.url = f"https://www.flashscore.com/match/{match_id}/#/match-summary/match-summary"
        self.live_data = {
            "score": {"set_scores": [], "current_game": [0, 0], "server": None},
            "odds": {"p1": None, "p2": None}
        }
        self.is_running = True

    async def _intercept_websocket(self, route, request):
        """Intercepte les requêtes réseau (si applicable) pour les cotes live"""
        # Note: Flashscore utilise souvent une connexion WS ou SSE pour le live
        # Cette fonction est un placeholder avancé pour capturer directement les flux
        await route.continue_()

    async def _parse_score(self, page):
        """Parse le score depuis le DOM"""
        try:
            # Récupérer le score des sets
            p1_sets = await page.query_selector_all(".smh__part--home")
            p2_sets = await page.query_selector_all(".smh__part--away")
            
            # Récupérer le score du jeu en cours (ex: 15, 30, 40, A)
            current_game_home = await page.query_selector(".smh__home .smh__part--current")
            current_game_away = await page.query_selector(".smh__away .smh__part--current")
            
            p1_game = await current_game_home.inner_text() if current_game_home else "0"
            p2_game = await current_game_away.inner_text() if current_game_away else "0"
            
            # Identifier le serveur
            server_icon = await page.query_selector(".smv__participantRow--home .icon--srv")
            server = 1 if server_icon else 2
            
            self.live_data["score"]["current_game"] = [p1_game, p2_game]
            self.live_data["score"]["server"] = server
            print(f"[LIVE] Score Jeu: {p1_game}-{p2_game} | Service: Joueur {server}")
        except Exception as e:
            pass

    async def _parse_odds(self, page):
        """Parse les cotes live depuis le DOM"""
        try:
            # Sélecteurs à adapter selon la structure exacte du live Flashscore
            odds_container = await page.query_selector(".ui-table__row")
            if odds_container:
                odds = await odds_container.query_selector_all(".odds__odd")
                if len(odds) >= 2:
                    p1_odd = await odds[0].inner_text()
                    p2_odd = await odds[1].inner_text()
                    self.live_data["odds"]["p1"] = float(p1_odd) if p1_odd != "-" else None
                    self.live_data["odds"]["p2"] = float(p2_odd) if p2_odd != "-" else None
                    print(f"[LIVE] Cotes: P1({p1_odd}) - P2({p2_odd})")
        except Exception as e:
            pass

    async def start_tracking(self, duration_seconds=60):
        """Lance le tracking pour une durée donnée (pour test)"""
        print(f"Démarrage du tracking Live pour le match {self.match_id}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            
            # page.route("**/*", self._intercept_websocket)
            
            await page.goto(self.url)
            
            start_time = time.time()
            while time.time() - start_time < duration_seconds and self.is_running:
                await self._parse_score(page)
                await self._parse_odds(page)
                # Attendre 5 secondes entre chaque check
                await asyncio.sleep(5)
                
            await browser.close()
            print("Fin du tracking Live.")
            return self.live_data

if __name__ == "__main__":
    # Remplacer par un ID valide trouvé via scraper_prematch.py s'il y a un match en direct
    # Exemple fictif: "hUZ4xV7a"
    dummy_match_id = "test_match_id"
    scraper = FlashscoreLiveScraper(dummy_match_id)
    # Lance pendant 10 secondes pour tester l'initialisation
    asyncio.run(scraper.start_tracking(duration_seconds=10))