import urllib.request
from bs4 import BeautifulSoup
import re
from datetime import datetime
import json
import os
import time

PROFILE_CACHE_VERSION = 5  # + te_last_match_date_iso (pont inactivité vs TML)


class ProfileScraper:
    def __init__(self, cache_dir="data/cache"):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5'
        }

    def _get_cache_path(self, player_url):
        safe_name = player_url.strip('/').replace('/', '_')
        if not safe_name:
            return None
        return os.path.join(self.cache_dir, f"{safe_name}.json")

    def _load_from_cache(self, player_url, max_age_hours=None):
        if max_age_hours is None:
            max_age_hours = max(1, int(os.getenv("BETTINGHUD_PROFILE_CACHE_HOURS", "24")))
        path = self._get_cache_path(player_url)
        if not path or not os.path.exists(path):
            return None

        file_age = time.time() - os.path.getmtime(path)
        if file_age > max_age_hours * 3600:
            return None

        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('_cache_v', 0) < PROFILE_CACHE_VERSION:
                return None
            return data
        except Exception:
            return None

    def _save_to_cache(self, player_url, data):
        path = self._get_cache_path(player_url)
        if path:
            data = dict(data)
            data['_cache_v'] = PROFILE_CACHE_VERSION
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f)

    @staticmethod
    def _infer_match_datetime(day: int, month: int, ref: datetime) -> datetime | None:
        """jj.mm. sans année : la plus récente date <= ref (évite mai 2025 compté comme mai 2026)."""
        y = ref.year
        for _ in range(4):
            try:
                d = datetime(y, month, day)
            except ValueError:
                return None
            if d <= ref:
                return d
            y -= 1
        return None

    @staticmethod
    def _estimate_minutes_from_score(score_text: str) -> int:
        """Compte les sets (pas les tie-breaks entre parenthèses, ex. 7-6(7-5))."""
        plain = re.sub(r'<[^>]+>', '', score_text)
        plain = re.sub(r'\([^)]*\)', '', plain)
        sets = re.findall(r'\d+\s*-\s*\d+', plain)
        n = len(sets)
        if n == 0:
            return 90
        return max(n, 1) * 45

    @staticmethod
    def _match_detail_id(score_td) -> str | None:
        if not score_td:
            return None
        a = score_td.find('a', href=re.compile(r'match-detail'))
        if a and a.get('href'):
            m = re.search(r'id=(\d+)', a['href'])
            if m:
                return m.group(1)
        return None

    def _skip_results_table(self, table) -> bool:
        """Exclut récaps W/L, prochain match, titres, historique Rome par année, etc."""
        tid = (table.get('id') or '').strip()
        if tid == 'playerPastTournamentResults0':
            return True

        classes = table.get('class') or []
        if 'gamedetail' in classes or 'titles' in classes:
            return True

        # Tableau balance = soit récap par année (th.year), soit liste des matchs joués
        if 'balance' in classes:
            if table.find('th', class_='year'):
                return True

        tr = table.find_parent('tr')
        if tr:
            tr_classes = tr.get('class') or []
            if 'pastTournamentGames' in tr_classes:
                return True

        if table.find_parent('div', id='lasttour-1-data'):
            return True

        parent_matches = table.find_parent('div', id=re.compile(r'^matches-\d+-\d+-data$'))
        if parent_matches:
            mid = parent_matches.get('id') or ''
            if '-2-data' in mid or '-3-data' in mid:
                return True

        return False

    @staticmethod
    def _detect_row_layout(cols):
        """7 colonnes : date + surface + joueurs | 6 colonnes : historique compact."""
        if len(cols) < 6:
            return None
        c1 = cols[1].get('class') or []
        if 's-color' in c1 and len(cols) >= 7:
            return {'player_idx': 2, 'score_idx': 4}
        if 't-name' in c1 and len(cols) >= 6:
            return {'player_idx': 1, 'score_idx': 3}
        return None

    def scrape_profile(self, player_url, force_refresh: bool = False):
        """Scrape le profil d'un joueur et calcule sa forme et fatigue."""
        if not player_url or not isinstance(player_url, str):
            return None

        if force_refresh:
            path = self._get_cache_path(player_url)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        else:
            cached_data = self._load_from_cache(player_url)
            if cached_data:
                return cached_data

        url = f"https://www.tennisexplorer.com{player_url}"

        try:
            req = urllib.request.Request(url, headers=self.headers)
            html = urllib.request.urlopen(req, timeout=6).read().decode('utf-8')
            soup = BeautifulSoup(html, 'html.parser')

            rank = 100
            age = 25
            hand = 'U'

            date_divs = soup.find_all('div', class_='date')
            for div in date_divs:
                text = div.get_text()
                if "Current/Highest rank - singles:" in text:
                    match = re.search(r'singles:\s*(\d+)\.', text)
                    if match:
                        rank = int(match.group(1))
                elif "Age:" in text:
                    match = re.search(r'Age:\s*(\d+)', text)
                    if match:
                        age = int(match.group(1))
                elif "Plays:" in text:
                    if "left" in text.lower():
                        hand = "L"
                    elif "right" in text.lower():
                        hand = "R"

            matches = []
            seen_detail_ids = set()
            now = datetime.now()

            tables = soup.find_all('table', class_='result')
            for table in tables:
                if self._skip_results_table(table):
                    continue

                rows = table.find_all('tr')
                for row in rows:
                    class_name = row.get('class', [])
                    if 'head' in class_name:
                        continue

                    cols = row.find_all('td')
                    layout = self._detect_row_layout(cols)
                    if not layout:
                        continue

                    date_str = cols[0].get_text(strip=True)
                    match_date_match = re.search(
                        r'^\s*(\d{1,2})\.(\d{1,2})\.\s*(?:(\d{4}))?\s*$', date_str
                    )
                    if not match_date_match:
                        continue

                    score_td = cols[layout['score_idx']]
                    score_text = score_td.get_text(strip=True)
                    if '-' not in score_text:
                        continue

                    day = int(match_date_match.group(1))
                    month = int(match_date_match.group(2))
                    year_str = match_date_match.group(3)

                    if year_str:
                        try:
                            match_date = datetime(int(year_str), month, day)
                        except ValueError:
                            continue
                    else:
                        match_date = self._infer_match_datetime(day, month, now)
                        if not match_date:
                            continue

                    if match_date > now:
                        continue

                    detail_id = self._match_detail_id(score_td)
                    if detail_id:
                        if detail_id in seen_detail_ids:
                            continue
                        seen_detail_ids.add(detail_id)

                    player_col = cols[layout['player_idx']]
                    links = player_col.find_all('a')
                    is_win = False
                    if len(links) >= 1:
                        first_link = links[0]
                        first_classes = first_link.get('class', [])
                        if 'notU' in first_classes:
                            is_win = True

                    estimated_minutes = self._estimate_minutes_from_score(score_text)

                    matches.append({
                        'date': match_date,
                        'minutes': estimated_minutes,
                        'won': is_win
                    })

            fatigue_minutes = 0
            fatigue_matches = 0
            form_matches = 0
            form_wins = 0

            for m in matches:
                days_ago = (now - m['date']).days
                if days_ago < 0:
                    continue
                if days_ago <= 14:
                    fatigue_minutes += m['minutes']
                    fatigue_matches += 1
                if days_ago <= 90:
                    form_matches += 1
                    if m['won']:
                        form_wins += 1

            win_pct = (form_wins / form_matches * 100) if form_matches > 0 else 50.0

            last_te_iso = None
            if matches:
                last_te_dt = max(m["date"] for m in matches)
                last_te_iso = last_te_dt.date().isoformat()

            data = {
                'fatigue_minutes': fatigue_minutes,
                'fatigue_matches': fatigue_matches,
                'form_matches': form_matches,
                'form_wins': form_wins,
                'win_pct': win_pct,
                'rank': rank,
                'age': age,
                'hand': hand,
                'te_last_match_date_iso': last_te_iso,
                'last_update': now.isoformat(),
                '_cache_v': PROFILE_CACHE_VERSION,
            }

            self._save_to_cache(player_url, data)
            return data

        except Exception as e:
            print(f"Erreur lors du scraping de {url}: {e}")
            return None


if __name__ == "__main__":
    scraper = ProfileScraper()
    res = scraper.scrape_profile("/player/dzumhur/")
    print(res)
