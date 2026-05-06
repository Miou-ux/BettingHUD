import asyncio
from playwright.async_api import async_playwright
import sqlite3
from datetime import datetime, timedelta
import re

class ResultsScraper:
    def __init__(self, db_path="data/bettinghud.db"):
        self.db_path = db_path

    async def scrape_results_for_date(self, target_date_str):
        date_obj = datetime.strptime(target_date_str, "%Y-%m-%d")
        url = f"https://www.tennisexplorer.com/results/?type=all&year={date_obj.year}&month={date_obj.strftime('%m')}&day={date_obj.strftime('%d')}"
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Recherche des résultats pour le {target_date_str}...")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()
            
            try:
                await page.goto(url, timeout=60000)
                await page.wait_for_timeout(2000)
                
                rows = await page.query_selector_all("table.result tbody tr")
                results = {}
                
                current_match = {}
                for row in rows:
                    class_name = await row.get_attribute("class") or ""
                    
                    if "one" in class_name or "two" in class_name:
                        cells = await row.query_selector_all("td")
                        if not cells: continue
                        
                        if "bott" in class_name:
                            p1_name_el = await row.query_selector("td.t-name a")
                            p1_name = await p1_name_el.inner_text() if p1_name_el else ""
                            
                            p1_res_el = await row.query_selector("td.result")
                            p1_res = await p1_res_el.inner_text() if p1_res_el else "0"
                            
                            current_match = {"p1_name": p1_name.strip(), "p1_res": p1_res.strip()}
                        else:
                            p2_name_el = await row.query_selector("td.t-name a")
                            p2_name = await p2_name_el.inner_text() if p2_name_el else ""
                            
                            p2_res_el = await row.query_selector("td.result")
                            p2_res = await p2_res_el.inner_text() if p2_res_el else "0"
                            
                            if current_match.get("p1_name") and p2_name:
                                p1 = current_match["p1_name"]
                                p2 = p2_name.strip()
                                
                                try:
                                    s1 = int(current_match["p1_res"]) if current_match["p1_res"].isdigit() else 0
                                    s2 = int(p2_res.strip()) if p2_res.strip().isdigit() else 0
                                    
                                    winner = p1 if s1 > s2 else (p2 if s2 > s1 else None)
                                    
                                    # Mettre en cache les résultats dans un dictionnaire
                                    # Pour pouvoir trouver "P1 vs P2" ou "P2 vs P1"
                                    # On simplifie les noms pour le matching
                                    key1 = f"{p1} vs {p2}".lower()
                                    key2 = f"{p2} vs {p1}".lower()
                                    
                                    if winner:
                                        results[key1] = winner
                                        results[key2] = winner
                                except Exception as e:
                                    pass
                                    
                            current_match = {}
            except Exception as e:
                print(f"Erreur scraping résultats: {e}")
            finally:
                await browser.close()
                
            return results

    async def scrape_results_for_dates(self, target_dates):
        """
        Scrape plusieurs dates en une seule session Playwright (beaucoup plus rapide).
        Retourne {date_str: {match_key: winner}}
        """
        out = {}
        valid_dates = []
        for d in target_dates:
            try:
                datetime.strptime(d, "%Y-%m-%d")
                valid_dates.append(d)
            except Exception:
                continue
        if not valid_dates:
            return out

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()
            try:
                for d in valid_dates:
                    date_obj = datetime.strptime(d, "%Y-%m-%d")
                    url = f"https://www.tennisexplorer.com/results/?type=all&year={date_obj.year}&month={date_obj.strftime('%m')}&day={date_obj.strftime('%d')}"
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Recherche des résultats pour le {d}...")
                    results = {}
                    try:
                        await page.goto(url, timeout=60000)
                        await page.wait_for_timeout(1200)
                        rows = await page.query_selector_all("table.result tbody tr")
                        current_match = {}
                        for row in rows:
                            class_name = await row.get_attribute("class") or ""
                            if "one" in class_name or "two" in class_name:
                                cells = await row.query_selector_all("td")
                                if not cells:
                                    continue
                                if "bott" in class_name:
                                    p1_name_el = await row.query_selector("td.t-name a")
                                    p1_name = await p1_name_el.inner_text() if p1_name_el else ""
                                    p1_res_el = await row.query_selector("td.result")
                                    p1_res = await p1_res_el.inner_text() if p1_res_el else "0"
                                    current_match = {"p1_name": p1_name.strip(), "p1_res": p1_res.strip()}
                                else:
                                    p2_name_el = await row.query_selector("td.t-name a")
                                    p2_name = await p2_name_el.inner_text() if p2_name_el else ""
                                    p2_res_el = await row.query_selector("td.result")
                                    p2_res = await p2_res_el.inner_text() if p2_res_el else "0"
                                    if current_match.get("p1_name") and p2_name:
                                        p1 = current_match["p1_name"]
                                        p2 = p2_name.strip()
                                        try:
                                            s1 = int(current_match["p1_res"]) if current_match["p1_res"].isdigit() else 0
                                            s2 = int(p2_res.strip()) if p2_res.strip().isdigit() else 0
                                            winner = p1 if s1 > s2 else (p2 if s2 > s1 else None)
                                            key1 = f"{p1} vs {p2}".lower()
                                            key2 = f"{p2} vs {p1}".lower()
                                            if winner:
                                                results[key1] = winner
                                                results[key2] = winner
                                        except Exception:
                                            pass
                                    current_match = {}
                    except Exception as e:
                        print(f"Erreur scraping résultats ({d}): {e}")
                    out[d] = results
            finally:
                await browser.close()
        return out

    def _normalize_name(self, name):
        # Enlever les numéros de têtes de série ex: (3), (Q), (WC)
        name = re.sub(r'\s*\([^)]*\)', '', name)
        return name.strip().lower()

    def _canonical_name(self, name):
        """
        Canonical form robuste pour matcher:
        - "Ugo Carabelli C." -> "carabelli c"
        - "Mannarino A." -> "mannarino a"
        """
        n = self._normalize_name(name)
        n = re.sub(r'[^a-z0-9\s\.]', ' ', n)
        n = re.sub(r'\s+', ' ', n).strip()
        if not n:
            return ""
        tokens = [t for t in n.replace(".", "").split(" ") if t]
        if not tokens:
            return ""
        # cas standard TennisExplorer: "lastname i"
        if len(tokens) == 2 and len(tokens[1]) == 1:
            return f"{tokens[0]} {tokens[1]}"
        # sinon prendre "nom de famille + initiale du dernier token utile"
        surname = tokens[-2] if len(tokens) >= 2 and len(tokens[-1]) == 1 else tokens[-1]
        initial = tokens[-1][0] if len(tokens[-1]) >= 1 else ""
        if len(tokens) >= 2 and len(tokens[-1]) == 1:
            initial = tokens[-1]
        return f"{surname} {initial}".strip()

    async def update_pending_bets(self, fast_mode=True):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Récupérer les paris en cours
        cursor.execute("SELECT id, date, match_name, bet_on, odds, stake FROM user_bets WHERE status = 'En cours'")
        pending_bets = cursor.fetchall()
        
        if not pending_bets:
            conn.close()
            return
            
        # Grouper les paris par date
        dates_to_scrape = set()
        today = datetime.now().date()
        for bet in pending_bets:
            try:
                base_date = datetime.strptime(bet[1], "%Y-%m-%d").date()
            except Exception:
                continue
            if base_date <= today:
                dates_to_scrape.add(base_date.strftime("%Y-%m-%d"))

        # Fast mode: ne scraper que les 2 dates les plus récentes réellement nécessaires.
        dates_sorted = sorted(dates_to_scrape)
        if fast_mode:
            dates_sorted = dates_sorted[-2:]
        else:
            # mode étendu: ajoute voisinage J-1/J+1 sur les dates ciblées
            expanded = set(dates_sorted)
            for d in dates_sorted:
                base_date = datetime.strptime(d, "%Y-%m-%d").date()
                for delta in (-1, 1):
                    nd = base_date + timedelta(days=delta)
                    if nd <= today:
                        expanded.add(nd.strftime("%Y-%m-%d"))
            dates_sorted = sorted(expanded)[-3:]

        by_date = await self.scrape_results_for_dates(dates_sorted)
        results_all = {}
        for _d, res in by_date.items():
            for k, v in (res or {}).items():
                if k not in results_all:
                    results_all[k] = v
                
        # Mettre à jour les paris
        updated_count = 0
        for bet in pending_bets:
            bet_id, bet_date, match_name, bet_on, odds, stake = bet
            
            # Normaliser match_name
            parts = match_name.split(" vs ")
            if len(parts) == 2:
                p1_norm = self._normalize_name(parts[0])
                p2_norm = self._normalize_name(parts[1])
                p1_can = self._canonical_name(parts[0])
                p2_can = self._canonical_name(parts[1])
                
                winner = None
                
                # Chercher une correspondance exacte ou partielle dans tous les résultats collectés
                for k, v in results_all.items():
                    k_parts = k.split(" vs ")
                    if len(k_parts) != 2:
                        continue
                    k1_norm = self._normalize_name(k_parts[0])
                    k2_norm = self._normalize_name(k_parts[1])
                    k1_can = self._canonical_name(k_parts[0])
                    k2_can = self._canonical_name(k_parts[1])
                    norm_ok = (p1_norm in f"{k1_norm} vs {k2_norm}" and p2_norm in f"{k1_norm} vs {k2_norm}")
                    can_ok = ({p1_can, p2_can} == {k1_can, k2_can}) and p1_can and p2_can
                    if norm_ok or can_ok:
                        winner = v
                        break
                        
                if winner:
                    winner_norm = self._normalize_name(winner)
                    bet_on_norm = self._normalize_name(bet_on)
                    winner_can = self._canonical_name(winner)
                    bet_on_can = self._canonical_name(bet_on)
                    
                    if (bet_on_norm in winner_norm or winner_norm in bet_on_norm) or (winner_can and winner_can == bet_on_can):
                        status = 'Gagné'
                        profit = (odds - 1) * stake
                    else:
                        status = 'Perdu'
                        profit = -stake
                        
                    cursor.execute("UPDATE user_bets SET status = ?, profit = ? WHERE id = ?", (status, profit, bet_id))
                    updated_count += 1
                    print(f"Pari {bet_id} mis à jour : {status} ({profit} U)")
        
        # Si fast mode n'a rien trouvé, on tente automatiquement un passage étendu.
        if updated_count == 0 and fast_mode:
            conn.commit()
            conn.close()
            return await self.update_pending_bets(fast_mode=False)

        conn.commit()
        conn.close()
        return updated_count

if __name__ == "__main__":
    scraper = ResultsScraper()
    asyncio.run(scraper.update_pending_bets())
