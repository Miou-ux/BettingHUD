import hashlib
import sqlite3
from datetime import datetime, timedelta
import urllib.request
from bs4 import BeautifulSoup

from scripts.scraper_profiles import ProfileScraper


def infer_surface(tournament: str) -> str:
    t = (tournament or "").lower()
    clay_hints = ["rome", "madrid", "monte-carlo", "roland", "barcelona", "hamburg", "marrakech", "bastad", "kitzbuhel", "geneva", "estoril", "parma"]
    grass_hints = ["wimbledon", "halle", "queens", "eastbourne", "mallorca", "stuttgart", "s-hertogenbosch"]
    hard_hints = ["australian", "us open", "miami", "indian wells", "dubai", "doha", "brisbane", "tokyo", "shanghai", "beijing", "montreal", "toronto", "cincinnati"]

    if any(k in t for k in clay_hints):
        return "Clay"
    if any(k in t for k in grass_hints):
        return "Grass"
    if any(k in t for k in hard_hints):
        return "Hard"
    return "Hard"


def infer_tourney_level(tournament: str) -> str:
    t = (tournament or "").lower()
    if any(k in t for k in ["wimbledon", "roland", "australian", "us open"]):
        return "G"
    if any(k in t for k in ["masters", "rome", "madrid", "miami", "indian wells", "monte-carlo", "paris bercy", "cincinnati", "shanghai"]):
        return "M"
    return "A"


def make_player_id(player_url: str, player_name: str) -> int:
    key = (player_url or player_name or "unknown").encode("utf-8", errors="ignore")
    h = hashlib.sha1(key).hexdigest()[:12]
    return int(h, 16) % 900000000 + 100000000


def ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches_recent (
            tourney_name TEXT,
            surface TEXT,
            tourney_level TEXT,
            tourney_date TEXT,
            winner_id INTEGER,
            winner_name TEXT,
            winner_hand TEXT,
            winner_ht REAL,
            winner_ioc TEXT,
            winner_age REAL,
            loser_id INTEGER,
            loser_name TEXT,
            loser_hand TEXT,
            loser_ht REAL,
            loser_ioc TEXT,
            loser_age REAL,
            score TEXT,
            minutes REAL,
            winner_rank REAL,
            winner_rank_points REAL,
            loser_rank REAL,
            loser_rank_points REAL,
            winner_url TEXT,
            loser_url TEXT,
            source TEXT,
            UNIQUE(tourney_name, tourney_date, winner_name, loser_name)
        )
        """
    )
    conn.commit()


def scrape_day(target_date: datetime, profile_scraper: ProfileScraper):
    y, m, d = target_date.year, target_date.month, target_date.day
    url = f"https://www.tennisexplorer.com/results/?type=all&year={y}&month={m:02d}&day={d:02d}"
    req = urllib.request.Request(url, headers=profile_scraper.headers)
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    soup = BeautifulSoup(html, "html.parser")

    rows = soup.select("table.result tbody tr")
    results = []
    current_tournament = None

    i = 0
    while i < len(rows):
        row = rows[i]
        classes = row.get("class") or []

        if "head" in classes and "flags" in classes:
            tn = row.select_one("td.t-name a")
            current_tournament = tn.get_text(" ", strip=True) if tn else None
            i += 1
            continue

        # pair rows: bott + next normal row
        if "bott" in classes:
            if i + 1 >= len(rows):
                i += 1
                continue
            row2 = rows[i + 1]

            cells1 = row.find_all("td")
            cells2 = row2.find_all("td")
            if len(cells1) < 3 or len(cells2) < 2:
                i += 2
                continue

            p1_el = row.select_one("td.t-name a")
            p2_el = row2.select_one("td.t-name a")
            p1_name = p1_el.get_text(" ", strip=True) if p1_el else ""
            p2_name = p2_el.get_text(" ", strip=True) if p2_el else ""
            p1_url = p1_el.get("href") if p1_el else None
            p2_url = p2_el.get("href") if p2_el else None

            try:
                s1 = int(cells1[2].get_text(strip=True))
                s2 = int(cells2[1].get_text(strip=True))
            except Exception:
                i += 2
                continue

            # sets detail (if present)
            set_scores = []
            max_sets = min(8, len(cells1) - 3, len(cells2) - 2)
            for j in range(max_sets):
                a = cells1[3 + j].get_text(strip=True)
                b = cells2[2 + j].get_text(strip=True)
                if a.isdigit() and b.isdigit():
                    set_scores.append(f"{a}-{b}")
            score_text = " ".join(set_scores) if set_scores else f"{s1}-{s2}"

            if s1 > s2:
                winner_name, loser_name = p1_name, p2_name
                winner_url, loser_url = p1_url, p2_url
            elif s2 > s1:
                winner_name, loser_name = p2_name, p1_name
                winner_url, loser_url = p2_url, p1_url
            else:
                i += 2
                continue

            # enrich from profile scraper cache/network
            def profile_for(u):
                if not u or not str(u).startswith("/player/"):
                    return None
                return profile_scraper.scrape_profile(u)

            wprof = profile_for(winner_url)
            lprof = profile_for(loser_url)

            results.append(
                {
                    "tourney_name": current_tournament or "Unknown",
                    "surface": infer_surface(current_tournament or ""),
                    "tourney_level": infer_tourney_level(current_tournament or ""),
                    "tourney_date": target_date.strftime("%Y-%m-%d"),
                    "winner_id": make_player_id(winner_url or "", winner_name),
                    "winner_name": winner_name,
                    "winner_hand": (wprof or {}).get("hand", "U"),
                    "winner_ht": None,
                    "winner_ioc": None,
                    "winner_age": (wprof or {}).get("age", 25),
                    "loser_id": make_player_id(loser_url or "", loser_name),
                    "loser_name": loser_name,
                    "loser_hand": (lprof or {}).get("hand", "U"),
                    "loser_ht": None,
                    "loser_ioc": None,
                    "loser_age": (lprof or {}).get("age", 25),
                    "score": score_text,
                    "minutes": 45.0 * max(s1, s2),
                    "winner_rank": (wprof or {}).get("rank", 100),
                    "winner_rank_points": None,
                    "loser_rank": (lprof or {}).get("rank", 100),
                    "loser_rank_points": None,
                    "winner_url": winner_url,
                    "loser_url": loser_url,
                    "source": "tennisexplorer_results",
                }
            )
            i += 2
            continue

        i += 1

    return results


def upsert_rows(conn, rows):
    if not rows:
        return 0
    sql = """
    INSERT OR REPLACE INTO matches_recent (
        tourney_name, surface, tourney_level, tourney_date,
        winner_id, winner_name, winner_hand, winner_ht, winner_ioc, winner_age,
        loser_id, loser_name, loser_hand, loser_ht, loser_ioc, loser_age,
        score, minutes,
        winner_rank, winner_rank_points, loser_rank, loser_rank_points,
        winner_url, loser_url, source
    ) VALUES (
        :tourney_name, :surface, :tourney_level, :tourney_date,
        :winner_id, :winner_name, :winner_hand, :winner_ht, :winner_ioc, :winner_age,
        :loser_id, :loser_name, :loser_hand, :loser_ht, :loser_ioc, :loser_age,
        :score, :minutes,
        :winner_rank, :winner_rank_points, :loser_rank, :loser_rank_points,
        :winner_url, :loser_url, :source
    )
    """
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def ingest_range(start_date: str, end_date: str):
    conn = sqlite3.connect("data/bettinghud.db")
    ensure_table(conn)
    ps = ProfileScraper()

    d0 = datetime.strptime(start_date, "%Y-%m-%d")
    d1 = datetime.strptime(end_date, "%Y-%m-%d")
    if d1 < d0:
        d0, d1 = d1, d0

    total = 0
    day = d0
    while day <= d1:
        try:
            rows = scrape_day(day, ps)
            n = upsert_rows(conn, rows)
            total += n
            print(day.strftime("%Y-%m-%d"), "rows", n)
        except Exception as e:
            print(day.strftime("%Y-%m-%d"), "ERR", e)
        day += timedelta(days=1)

    conn.close()
    print("TOTAL_UPSERTED", total)


if __name__ == "__main__":
    # Ajustable: on prend 2026 à aujourd'hui par défaut
    ingest_range("2026-01-01", datetime.now().strftime("%Y-%m-%d"))
