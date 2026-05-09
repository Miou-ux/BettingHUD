"""Télécharge les CSV WTA Jeff Sackmann sans clone git local.

écrit sous ``data/raw/tennis_wta`` :
  - ``wta_rankings_current.csv``
  - ``wta_matches_YYYY.csv`` pour chaque année de ``min_year`` à l’année UTC courante

Source : ``https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/``

``sync_tours_daily.py`` invoque ce script si ``data/raw/tennis_wta`` n’a pas de ``.git``.

Usage ::

    python scripts/fetch_wta_sackmann_raw.py

Env optionnel : ``BETTINGHUD_WTA_SACKMANN_MIN_YEAR`` (déf. 1968),
``BETTINGHUD_WTA_FETCH_TIMEOUT_SEC`` (déf. 120).
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master"
UA = "BettingHUD-wta-fetch/1.0"


def _utc_year() -> int:
    return datetime.now(timezone.utc).year


def _download(url: str, dest: str, *, timeout: int) -> bool:
    """Retourne True si le fichier a été écrit (HTTP 200)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", resp.getcode())
            if code != 200:
                return False
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return True


def fetch_wta_raw(
    raw_dir: str,
    *,
    min_year: int,
    max_year: int,
    timeout: int,
) -> tuple[bool, int]:
    """Télécharge classement + matchs. Retourne (rankings_ok, nb_fichiers_matchs)."""
    rankings_path = os.path.join(raw_dir, "wta_rankings_current.csv")
    rankings_ok = _download(f"{BASE}/wta_rankings_current.csv", rankings_path, timeout=timeout)
    n_matches = 0
    for year in range(min_year, max_year + 1):
        name = f"wta_matches_{year}.csv"
        dest = os.path.join(raw_dir, name)
        if _download(f"{BASE}/{name}", dest, timeout=timeout):
            n_matches += 1
    return rankings_ok, n_matches


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Téléchargement CSV WTA Sackmann (sans git).")
    p.add_argument(
        "--raw-dir",
        default=os.path.join("data", "raw", "tennis_wta"),
        help="Dossier cible pour les CSV",
    )
    p.add_argument(
        "--min-year",
        type=int,
        default=int(os.getenv("BETTINGHUD_WTA_SACKMANN_MIN_YEAR", "1968")),
        help="Première année wta_matches_YYYY.csv à récupérer",
    )
    p.add_argument(
        "--max-year",
        type=int,
        default=None,
        help="Dernière année incluse (défaut : année UTC courante)",
    )
    args = p.parse_args(argv)

    cy = _utc_year()
    max_year = cy if args.max_year is None else args.max_year
    if max_year < args.min_year:
        print("erreur : max-year < min-year", file=sys.stderr)
        return 1

    timeout = int(os.getenv("BETTINGHUD_WTA_FETCH_TIMEOUT_SEC", "120"))
    raw_dir = os.path.abspath(args.raw_dir)
    rk_ok, n_m = fetch_wta_raw(
        raw_dir, min_year=args.min_year, max_year=max_year, timeout=max(10, timeout)
    )
    print(
        f"fetch_wta_sackmann_raw : dossier={raw_dir} "
        f"rankings={'OK' if rk_ok else 'absent/404'} match_years={n_m}"
    )
    if n_m == 0:
        print("erreur : aucun wta_matches_YYYY.csv téléchargé", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
