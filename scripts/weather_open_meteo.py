"""
Météo via Open-Meteo (gratuit, sans clé API pour usage raisonnable).

- Géocodage : https://geocoding-api.open-meteo.com/v1/search
- Historique : https://archive-api.open-meteo.com/v1/archive
- Prévision  : https://api.open-meteo.com/v1/forecast

Données utilisées pour le ML : température 2 m, humidité relative 2 m.
Pour les matchs sans heure exacte, on prend la moyenne des heures « jour » (8–20h locales).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Tuple

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_TIMEOUT_SEC = 25
REQUEST_SLEEP_SEC = 0.35  # petit respect rate-limit

# Libellés TML / Flashscore trop génériques pour l’API de recherche Open-Meteo.
GEOCODE_QUERY_ALIASES: Dict[str, str] = {
    "us open": "USTA Billie Jean King National Tennis Center New York USA",
    "australian open": "Melbourne Park Melbourne Australia",
    "roland garros": "Stade Roland Garros Paris France",
    "french open": "Stade Roland Garros Paris France",
    "wimbledon": "All England Lawn Tennis Club Wimbledon London UK",
    "indian wells": "Indian Wells Tennis Garden California USA",
}

# Coordonnées manuelles : construites depuis `tournament_geo.TOURNAMENT_GPS` (ATP/WTA 1000–500–250
# et sites fréquents) + raffinage Slams ci-dessous. Évite l’API géocode pour la majorité des tournois.
_WEATHER_MANUAL_COORDS_TABLE: Optional[Dict[str, Tuple[float, float, str]]] = None

# Raffinement stade (prioritaire sur les paires lat/lon du fichier geo).
_GEOCODE_SLAM_AND_VENUE_PRECISE: Dict[str, Tuple[float, float, str]] = {
    "us open": (40.750556, -73.845278, "USTA Billie Jean King National Tennis Center, USA"),
    "australian open": (-37.822, 144.981, "Melbourne Park, Australia"),
    "roland garros": (48.848, 2.249, "Stade Roland Garros, France"),
    "french open": (48.848, 2.249, "Stade Roland Garros, France"),
    "wimbledon": (51.434, -0.214, "All England Lawn Tennis Club, UK"),
    "indian wells": (33.7237, -116.3052, "Indian Wells Tennis Garden, USA"),
}


def _weather_manual_coords_table() -> Dict[str, Tuple[float, float, str]]:
    global _WEATHER_MANUAL_COORDS_TABLE
    if _WEATHER_MANUAL_COORDS_TABLE is not None:
        return _WEATHER_MANUAL_COORDS_TABLE
    from tournament_geo import TOURNAMENT_GPS  # noqa: E402 — path _SCRIPTS_DIR ci-dessus

    tbl: Dict[str, Tuple[float, float, str]] = {}
    for site_key, (la, lo, _tz) in TOURNAMENT_GPS.items():
        nk = normalize_geo_search_key(site_key)
        if not nk:
            continue
        lbl = str(site_key).replace("_", " ").strip()
        tbl[nk] = (float(la), float(lo), f"{lbl}, tennis venue")
    for k, tup in _GEOCODE_SLAM_AND_VENUE_PRECISE.items():
        tbl[normalize_geo_search_key(k)] = tup
    _WEATHER_MANUAL_COORDS_TABLE = tbl
    return _WEATHER_MANUAL_COORDS_TABLE


def _manual_key_in_tourney_norm_key(norm_tourney: str, site_norm: str) -> bool:
    """True si le site (clé normalisée) apparaît comme token / préfixe / suffixe dans le nom tournoi."""
    if not norm_tourney or not site_norm:
        return False
    if norm_tourney == site_norm:
        return True
    if norm_tourney.startswith(site_norm + " "):
        return True
    if norm_tourney.endswith(" " + site_norm):
        return True
    return f" {site_norm} " in f" {norm_tourney} "


def _manual_coords_for_search_key(key: str) -> Optional[Tuple[float, float, str]]:
    if not key:
        return None
    table = _weather_manual_coords_table()
    if key in table:
        return table[key]
    for mk in sorted(table.keys(), key=len, reverse=True):
        if _manual_key_in_tourney_norm_key(key, mk):
            return table[mk]
    return None


def _http_get_json(url: str, timeout: float = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "BettingHUD/1.0 (weather)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_geo_search_key(tourney_name: object) -> str:
    """Clé stable pour cache géocodage."""
    s = str(tourney_name or "").lower().strip()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160] if s else ""


def geocode_tournament(
    tourney_name: object,
    *,
    conn: Optional[sqlite3.Connection] = None,
    country_code: Optional[str] = None,
) -> Optional[Tuple[float, float, str]]:
    """
    Retourne (lat, lon, label) ou None.
    Utilise le cache SQLite `weather_geo_cache` si `conn` est fourni.
    """
    key = normalize_geo_search_key(tourney_name)
    if not key:
        return None

    manual = _manual_coords_for_search_key(key)
    if manual is not None:
        lat, lon, label = manual
        if conn is not None:
            cur = conn.execute(
                "SELECT lat, lon FROM weather_geo_cache WHERE search_key = ?", (key,)
            )
            row = cur.fetchone()
            if row is None or abs(float(row[0]) - lat) > 1e-5 or abs(float(row[1]) - lon) > 1e-5:
                conn.execute(
                    """
                    INSERT INTO weather_geo_cache(search_key, lat, lon, label, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(search_key) DO UPDATE SET
                        lat=excluded.lat, lon=excluded.lon, label=excluded.label, updated_at=excluded.updated_at
                    """,
                    (key, lat, lon, label, datetime.now(timezone.utc).isoformat()),
                )
                conn.commit()
        return lat, lon, label

    if conn is not None:
        cur = conn.execute(
            "SELECT lat, lon, label FROM weather_geo_cache WHERE search_key = ?", (key,)
        )
        row = cur.fetchone()
        if row is not None:
            return float(row[0]), float(row[1]), str(row[2] or "")

    q = str(tourney_name or "").strip()
    if not q:
        return None
    if key in GEOCODE_QUERY_ALIASES:
        q = GEOCODE_QUERY_ALIASES[key]
    params: Dict[str, Any] = {"name": q, "count": 5, "language": "en", "format": "json"}
    if country_code:
        params["country"] = country_code
    url = f"{GEOCODE_URL}?{urllib.parse.urlencode(params)}"
    try:
        data = _http_get_json(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    time.sleep(REQUEST_SLEEP_SEC)
    results = data.get("results") or []
    if not results:
        # second essai : premier « mot significatif » long (ex. Rome, Miami)
        parts = key.split()
        significant = [p for p in parts if len(p) >= 4 and p not in {"open", "masters", "atp", "wta", "1000", "500", "250"}]
        if significant:
            params["name"] = significant[0]
            url2 = f"{GEOCODE_URL}?{urllib.parse.urlencode(params)}"
            try:
                data = _http_get_json(url2)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                return None
            time.sleep(REQUEST_SLEEP_SEC)
            results = data.get("results") or []
    if not results:
        return None
    r0 = results[0]
    lat = float(r0["latitude"])
    lon = float(r0["longitude"])
    label = str(r0.get("name", "")) + ", " + str(r0.get("country", ""))

    if conn is not None:
        conn.execute(
            """
            INSERT INTO weather_geo_cache(search_key, lat, lon, label, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(search_key) DO UPDATE SET
                lat=excluded.lat, lon=excluded.lon, label=excluded.label, updated_at=excluded.updated_at
            """,
            (key, lat, lon, label, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return lat, lon, label


def _mean_daytime_hours(
    times: list,
    temps: list,
    humidities: list,
    start_h: int = 8,
    end_h: int = 20,
) -> Tuple[Optional[float], Optional[float]]:
    """Moyenne sur les heures locales dont l’heure est dans [start_h, end_h]."""
    if not times or not temps or not humidities:
        return None, None
    t_acc: list[float] = []
    h_acc: list[float] = []
    for tiso, tc, rh in zip(times, temps, humidities):
        if tc is None or rh is None:
            continue
        try:
            dt = datetime.fromisoformat(str(tiso).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                loc_h = dt.astimezone().hour
            else:
                loc_h = dt.hour
            if start_h <= loc_h <= end_h:
                t_acc.append(float(tc))
                h_acc.append(float(rh))
        except Exception:
            continue
    if not t_acc:
        # repli : toute la série
        for tc, rh in zip(temps, humidities):
            if tc is None or rh is None:
                continue
            try:
                t_acc.append(float(tc))
                h_acc.append(float(rh))
            except Exception:
                continue
    if not t_acc:
        return None, None
    return sum(t_acc) / len(t_acc), sum(h_acc) / len(h_acc)


def fetch_archive_daytime_means(
    lat: float,
    lon: float,
    d: date,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """Température (°C) et humidité (%) moyennes « journée » pour une date passée."""
    lat_r = round(float(lat), 4)
    lon_r = round(float(lon), 4)
    day_s = d.isoformat()

    if conn is not None:
        cur = conn.execute(
            "SELECT temp_c, humidity_pct FROM weather_day_cache WHERE lat=? AND lon=? AND day=?",
            (lat_r, lon_r, day_s),
        )
        row = cur.fetchone()
        if row is not None and row[0] is not None and row[1] is not None:
            return float(row[0]), float(row[1])

    params = urllib.parse.urlencode(
        {
            "latitude": lat_r,
            "longitude": lon_r,
            "start_date": day_s,
            "end_date": day_s,
            "hourly": "temperature_2m,relative_humidity_2m",
            "timezone": "auto",
        }
    )
    url = f"{ARCHIVE_URL}?{params}"
    try:
        data = _http_get_json(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None, None
    time.sleep(REQUEST_SLEEP_SEC)

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    humidities = hourly.get("relative_humidity_2m") or []
    tc, rh = _mean_daytime_hours(times, temps, humidities)

    if conn is not None and tc is not None and rh is not None:
        conn.execute(
            """
            INSERT INTO weather_day_cache(lat, lon, day, temp_c, humidity_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(lat, lon, day) DO UPDATE SET
                temp_c=excluded.temp_c, humidity_pct=excluded.humidity_pct,
                updated_at=excluded.updated_at
            """,
            (lat_r, lon_r, day_s, tc, rh, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return tc, rh


def fetch_forecast_daytime_means(
    lat: float,
    lon: float,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Tuple[Optional[float], Optional[float]]:
    """Prévision : moyenne heures 8–20h locales sur les 24 prochaines heures disponibles."""
    lat_r = round(float(lat), 4)
    lon_r = round(float(lon), 4)
    today = date.today().isoformat()

    if conn is not None:
        cur = conn.execute(
            "SELECT temp_c, humidity_pct FROM weather_forecast_day_cache WHERE lat=? AND lon=? AND day=?",
            (lat_r, lon_r, today),
        )
        row = cur.fetchone()
        if row is not None and row[0] is not None and row[1] is not None:
            age = conn.execute(
                "SELECT updated_at FROM weather_forecast_day_cache WHERE lat=? AND lon=? AND day=?",
                (lat_r, lon_r, today),
            ).fetchone()
            if age and age[0]:
                try:
                    ua = datetime.fromisoformat(str(age[0]).replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - ua.replace(tzinfo=timezone.utc)).total_seconds() < 3 * 3600:
                        return float(row[0]), float(row[1])
                except Exception:
                    pass

    params = urllib.parse.urlencode(
        {
            "latitude": lat_r,
            "longitude": lon_r,
            "hourly": "temperature_2m,relative_humidity_2m",
            "forecast_days": 2,
            "timezone": "auto",
        }
    )
    url = f"{FORECAST_URL}?{params}"
    try:
        data = _http_get_json(url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None, None
    time.sleep(REQUEST_SLEEP_SEC)

    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    humidities = hourly.get("relative_humidity_2m") or []
    tc, rh = _mean_daytime_hours(times[:48], temps[:48], humidities[:48], 8, 20)

    if conn is not None and tc is not None and rh is not None:
        conn.execute(
            """
            INSERT INTO weather_forecast_day_cache(lat, lon, day, temp_c, humidity_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(lat, lon, day) DO UPDATE SET
                temp_c=excluded.temp_c, humidity_pct=excluded.humidity_pct,
                updated_at=excluded.updated_at
            """,
            (lat_r, lon_r, today, tc, rh, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return tc, rh


def weather_for_tournament_day(
    tourney_name: object,
    match_date: date,
    *,
    conn: Optional[sqlite3.Connection] = None,
    prefer_forecast_if_today: bool = True,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Retourne (temp_c, humidity_pct) pour un lieu de tournoi et une date calendaire.
    - Passé : archive ERA5.
    - Aujourd'hui (option) : prévision courte.
    """
    g = geocode_tournament(tourney_name, conn=conn)
    if g is None:
        return None, None
    lat, lon, _ = g

    today = date.today()
    if prefer_forecast_if_today and match_date == today:
        t, h = fetch_forecast_daytime_means(lat, lon, conn=conn)
        if t is not None and h is not None:
            return t, h

    if match_date > today:
        return fetch_forecast_daytime_means(lat, lon, conn=conn)

    return fetch_archive_daytime_means(lat, lon, match_date, conn=conn)


def ensure_weather_schema(conn: sqlite3.Connection) -> None:
    """Tables de cache + colonnes sur matches_recent / wta_matches si présentes."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS weather_geo_cache (
            search_key TEXT PRIMARY KEY,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            label TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS weather_day_cache (
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            day TEXT NOT NULL,
            temp_c REAL,
            humidity_pct REAL,
            updated_at TEXT,
            PRIMARY KEY (lat, lon, day)
        );
        CREATE TABLE IF NOT EXISTS weather_forecast_day_cache (
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            day TEXT NOT NULL,
            temp_c REAL,
            humidity_pct REAL,
            updated_at TEXT,
            PRIMARY KEY (lat, lon, day)
        );
        """
    )
    cols_a = {r[1] for r in conn.execute("PRAGMA table_info(matches_recent)").fetchall()}
    if "humidity_pct" not in cols_a:
        conn.execute("ALTER TABLE matches_recent ADD COLUMN humidity_pct REAL")
    if "temp_c" not in cols_a:
        conn.execute("ALTER TABLE matches_recent ADD COLUMN temp_c REAL")
    try:
        cols_w = {r[1] for r in conn.execute("PRAGMA table_info(wta_matches)").fetchall()}
        if cols_w and "humidity_pct" not in cols_w:
            conn.execute("ALTER TABLE wta_matches ADD COLUMN humidity_pct REAL")
        if cols_w and "temp_c" not in cols_w:
            conn.execute("ALTER TABLE wta_matches ADD COLUMN temp_c REAL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
