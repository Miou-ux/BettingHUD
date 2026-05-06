import os
from datetime import datetime
import requests

BASE = "http://tennis-data.co.uk"


def _year_paths(year: int):
    # tennis-data expose un workbook annuel .xlsx (ATP+WTA en onglets)
    return [
        (f"{BASE}/{year}/{year}.xlsx", f"{year}.xlsx"),
    ]


def sync_tennis_data_odds(start_year=2010, end_year=None, target_dir="data/raw/tennis_data"):
    if end_year is None:
        end_year = datetime.utcnow().year
    os.makedirs(target_dir, exist_ok=True)

    downloaded = 0
    skipped = 0
    failed = 0

    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (BettingHUD odds sync)"})

    for year in range(int(start_year), int(end_year) + 1):
        for url, fname in _year_paths(year):
            path = os.path.join(target_dir, fname)
            try:
                r = s.get(url, timeout=45, allow_redirects=True)
                if r.status_code != 200 or len(r.content) < 2048:
                    failed += 1
                    print(f"MISS {fname} ({r.status_code})")
                    continue

                # idempotent write: skip if unchanged size
                if os.path.exists(path) and os.path.getsize(path) == len(r.content):
                    skipped += 1
                    print(f"SKIP {fname}")
                    continue

                with open(path, "wb") as f:
                    f.write(r.content)
                downloaded += 1
                print(f"OK   {fname} ({len(r.content)} bytes)")
            except Exception as e:
                failed += 1
                print(f"ERR  {fname}: {e}")

    print(f"DONE downloaded={downloaded} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    sync_tennis_data_odds(start_year=2010)
