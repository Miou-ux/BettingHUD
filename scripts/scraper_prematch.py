import asyncio
from playwright.async_api import async_playwright
import pandas as pd
from datetime import datetime, timedelta
import os

class FlashscoreScraper:
    def __init__(self):
        self.tennis_explorer_url = "https://www.tennisexplorer.com/matches/"
        self.data_dir = os.path.join("data", "scraped")
        os.makedirs(self.data_dir, exist_ok=True)

    async def get_matches_and_odds(self, day_offset=0):
        target_date = datetime.now() + timedelta(days=day_offset)
        
        url = f"https://www.tennisexplorer.com/matches/?type=all&year={target_date.year}&month={target_date.strftime('%m')}&day={target_date.strftime('%d')}"
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Démarrage du scraper Tennis Explorer pour {target_date.strftime('%Y-%m-%d')} (offset={day_offset})")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(2000)
            
            matches_data = []
            current_tournament = "Inconnu"
            current_category = "Inconnu"
            
            rows = await page.query_selector_all("table.result tbody tr")
            
            match_count = 0
            current_match = {}
            for row in rows:
                class_name = await row.get_attribute("class") or ""
                if "head" in class_name:
                    tournament_header_el = await row.query_selector("td.t-name a")
                    if tournament_header_el:
                        tournament_text = await tournament_header_el.inner_text()
                        current_tournament = tournament_text.strip()
                        
                        current_category = "Inconnu"
                        html = await row.inner_html()
                        if "type-men" in html or "-men" in html or "ATP" in current_tournament:
                            current_category = "ATP"
                        elif "type-women" in html or "-women" in html or "WTA" in current_tournament:
                            current_category = "WTA"
                        
                        if "Challenger" in current_tournament: current_category = "Challenger"
                        elif "ITF" in current_tournament: current_category = "ITF"
                    continue
                    
                if "one" in class_name or "two" in class_name:
                    cells = await row.query_selector_all("td")
                    if not cells: continue
                    
                    if "bott" in class_name:
                        # First row of the match
                        time_cell = await cells[0].inner_text()
                        if day_offset == 1:
                            time_text = "Demain " + time_cell.strip()
                        else:
                            time_text = time_cell.strip()
                            
                        player1_cell = await cells[1].inner_text() if len(cells) > 1 else ""
                        player1 = player1_cell.strip()
                        
                        p1_url = ""
                        if len(cells) > 1:
                            a_tag = await cells[1].query_selector("a")
                            if a_tag:
                                p1_url = await a_tag.get_attribute("href")
                        
                        odd_p1, odd_p2 = 0.0, 0.0
                        try:
                            # Les cotes sont dans les colonnes avec la classe "course" ou "coursew"
                            course_cells = await row.query_selector_all("td.course, td.coursew")
                            if len(course_cells) >= 2:
                                odd_p1_text = await course_cells[0].inner_text()
                                odd_p2_text = await course_cells[1].inner_text()
                                # Clean up text (replace nbsp and commas)
                                odd_p1_text = odd_p1_text.replace('\xa0', '').replace(',', '.').strip()
                                odd_p2_text = odd_p2_text.replace('\xa0', '').replace(',', '.').strip()
                                odd_p1 = float(odd_p1_text) if odd_p1_text else 0.0
                                odd_p2 = float(odd_p2_text) if odd_p2_text else 0.0
                        except Exception:
                            pass
                            
                        current_match = {
                            "id": f"te_{match_count}",
                            "date": target_date.strftime('%Y-%m-%d'),
                            "tournament": current_tournament,
                            "category": current_category,
                            "time": time_text,
                            "player1": player1,
                            "p1_url": p1_url,
                            "odd_p1": odd_p1,
                            "odd_p2": odd_p2,
                            "scraped_at": datetime.now().isoformat()
                        }
                    else:
                        # Second row of the match (Player 2)
                        player2_cell = await cells[0].inner_text() if len(cells) > 0 else ""
                        current_match["player2"] = player2_cell.strip()
                        
                        p2_url = ""
                        if len(cells) > 0:
                            a_tag = await cells[0].query_selector("a")
                            if a_tag:
                                p2_url = await a_tag.get_attribute("href")
                        current_match["p2_url"] = p2_url
                        
                        if current_match.get("player1") and current_match.get("player2"):
                            matches_data.append(current_match)
                            match_count += 1
                        current_match = {}
            
            await browser.close()
            return matches_data

    async def get_today_matches_and_odds(self):
        """Scrape les matchs d'aujourd'hui ET de demain"""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Démarrage de l'extraction multi-jours...")
        
        matches_today = await self.get_matches_and_odds(day_offset=0)
        matches_tomorrow = await self.get_matches_and_odds(day_offset=1)
        
        all_matches = matches_today + matches_tomorrow
        
        if all_matches:
            df = pd.DataFrame(all_matches)
            filename = f"prematch_odds_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            filepath = os.path.join(self.data_dir, filename)
            df.to_csv(filepath, index=False)
            print(f"Scraping terminé. {len(all_matches)} matchs sauvegardés dans {filepath}")
            
        return all_matches

if __name__ == "__main__":
    scraper = FlashscoreScraper()
    matches = asyncio.run(scraper.get_today_matches_and_odds())
    for m in matches[:10]:
        print(f"{m['time']} | {m['tournament']} ({m['category']}) | {m['player1']} vs {m['player2']} | Cotes: {m['odd_p1']} - {m['odd_p2']}")