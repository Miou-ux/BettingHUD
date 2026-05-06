"""
Audit ponctuel : échantillonne des URLs joueur depuis le dernier CSV prematch,
appelle ProfileScraper (cache disque habituel) et signale les anomalies.
Usage : python scripts/audit_random_profiles.py
"""
from __future__ import annotations

import glob
import os
import random
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.scraper_profiles import ProfileScraper


def _latest_csv() -> str | None:
    data_dir = os.path.join("data", "scraped")
    files = glob.glob(os.path.join(data_dir, "*.csv"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def main():
    path = _latest_csv()
    if not path:
        print("Aucun CSV dans data/scraped")
        return 1
    df = pd.read_csv(path)
    df = df[df["category"].isin(["ATP", "WTA"])]
    urls: set[str] = set()
    names: dict[str, str] = {}
    for _, row in df.iterrows():
        for col_url, col_name in (("p1_url", "player1"), ("p2_url", "player2")):
            u = row.get(col_url)
            if pd.isna(u) or not isinstance(u, str):
                continue
            u = u.strip()
            if not u.startswith("/player/"):
                continue
            urls.add(u)
            names[u] = str(row.get(col_name, ""))

    if len(urls) < 1:
        print("Pas d'URL /player/ ATP/WTA dans le CSV")
        return 1

    rng = random.Random(42)
    sample = rng.sample(sorted(urls), k=min(10, len(urls)))
    scraper = ProfileScraper()

    print(f"CSV: {path}")
    print(f"Échantillon ({len(sample)} joueurs), seed=42\n")

    issues = 0
    for url in sample:
        label = names.get(url, "?")
        data = scraper.scrape_profile(url)
        if data is None:
            print(f"FAIL {label:22} {url} -> scrape_profile None")
            issues += 1
            continue
        fm = data.get("fatigue_minutes", 0)
        fmatches = data.get("fatigue_matches", 0)
        form_n = data.get("form_matches", 0)
        wp = data.get("win_pct", 50)
        rk = data.get("rank", 0)

        flags = []
        # ~14 jours × jusqu'à ~4h/match extrême reste sous 3500 ; au-delà = suspect
        if fm > 2400:
            flags.append(f"fatigue_minutes très élevées ({fm})")
        if fmatches > 18:
            flags.append(f"fatigue_matches={fmatches} (dense)")
        if form_n > 45:
            flags.append(f"form_matches={form_n} (>45 en 90j)")
        if not (0 <= wp <= 100):
            flags.append(f"win_pct hors bornes ({wp})")
        if rk is not None and (rk < 1 or rk > 2500):
            flags.append(f"rank suspect ({rk})")

        status = "WARN " + "; ".join(flags) if flags else "OK"
        if flags:
            issues += 1
        print(
            f"{status:40} | {label:22} | fat {fm:4}m/{fmatches}m | "
            f"form {form_n} @ {wp:.0f}% | rank {rk}"
        )

    print(f"\nRésumé: {issues} ligne(s) avec avertissement ou échec sur {len(sample)}")
    return 0 if issues == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
