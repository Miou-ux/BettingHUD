"""
Coordonnées GPS approximatives et fuseaux (UTC±) pour tournois ATP/WTA.

Inféré à partir du nom (`tourney_name`) par plus long substring match — pas d’API.
Utilisé pour distance Haversine + jetlag cumulatif entre événements consécutifs.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Optional, Tuple

# (lat°, lon°, offset_UTC_hours) — offset entier indicative (DST ignoré dans le malus brut).
_SITE_LIST: Iterable[Tuple[str, float, float, int]] = (
    ("melbourne park", -37.822, 144.981, 11),
    ("australian open", -37.822, 144.981, 11),
    ("indian wells", 33.722, -116.412, -8),
    ("miami", 25.768, -80.337, -5),
    ("monte carlo", 43.751, 7.439, 1),
    ("monte-carlo", 43.751, 7.439, 1),
    ("rome", 41.925, 12.478, 1),
    ("madrid", 40.424, -3.694, 1),
    ("roland garros", 48.848, 2.249, 1),
    ("french open", 48.848, 2.249, 1),
    ("wimbledon", 51.435, -0.214, 0),
    ("queen", 51.489, -0.196, 0),
    ("halle", 52.069, 8.774, 1),
    ("stuttgart", 48.790, 9.229, 1),
    ("eastbourne", 50.764, 0.285, 0),
    ("newport", 41.489, -71.309, -5),
    ("basel", 47.547, 7.596, 1),
    ("vienna", 48.239, 16.379, 1),
    ("paris bercy", 48.840, 2.378, 1),
    ("paris masters", 48.840, 2.378, 1),
    ("metz", 49.120, 6.179, 1),
    ("rotterdam", 51.925, 4.478, 1),
    ("dubai", 25.239, 55.279, 4),
    ("doha", 25.285, 51.547, 3),
    ("abu dhabi", 24.496, 54.613, 4),
    ("acapulco", 16.764, -99.834, -6),
    ("rio de janeiro", -22.999, -43.362, -3),
    ("buenos aires", -34.560, -58.449, -3),
    ("santiago", -33.435, -70.594, -3),
    ("us open", 40.750, -73.849, -5),
    ("flushing", 40.750, -73.849, -5),
    ("cincinnati", 39.118, -84.519, -5),
    ("toronto", 43.648, -79.379, -5),
    ("montreal", 45.567, -73.743, -5),
    ("washington", 38.910, -77.014, -5),
    ("atlanta", 33.875, -84.459, -5),
    ("shanghai", 31.206, 121.478, 8),
    ("beijing", 39.925, 116.389, 8),
    ("tokyo", 35.660, 139.694, 9),
    ("geneva", 46.229, 6.146, 1),
    ("gstaad", 46.475, 7.297, 1),
    ("kitzbuhel", 47.439, 12.389, 1),
    ("marrakech", 31.630, -7.986, 0),
    ("estoril", 38.694, -9.379, 0),
    ("barcelona", 41.410, 2.183, 1),
    ("munich", 48.191, 11.579, 1),
    ("hamburg", 53.573, 9.986, 1),
    ("stockholm", 59.345, 18.069, 1),
    ("moscow", 55.753, 37.619, 3),
    ("st petersburg", 59.914, 30.389, 3),
    ("antwerp", 51.229, 4.489, 1),
    ("atp finals turin", 45.069, 7.694, 1),
    ("turin", 45.069, 7.694, 1),
    ("atp finals london", 51.519, -0.104, 0),
    ("next gen finals", -34.917, 138.596, 10),
    # --- ATP/WTA 1000 / 500 / 250 : sites fréquents (substring la plus longue gagne) ---
    # Australie / Océanie
    ("adelaide", -34.9285, 138.6007, 10),
    ("brisbane", -27.4709, 153.0235, 10),
    ("sydney", -33.8688, 151.2093, 10),
    ("hobart", -42.8821, 147.3272, 10),
    ("auckland", -36.8485, 174.7633, 12),
    # USA / Canada (hors déjà listés)
    ("dallas", 32.7767, -96.7970, -6),
    ("houston", 29.7604, -95.3698, -6),
    ("delray beach", 26.4615, -80.0728, -5),
    ("winston salem", 36.0999, -80.2442, -5),
    ("los cabos", 22.8905, -109.9167, -7),
    ("memphis", 35.1495, -90.0490, -6),
    ("charleston", 32.7803, -79.9319, -5),
    ("san diego", 32.7157, -117.1611, -8),
    ("cleveland", 41.4993, -81.6944, -5),
    ("orlando", 28.5383, -81.3792, -5),
    ("austin", 30.2672, -97.7431, -6),
    ("charlotte", 35.2271, -80.8431, -5),
    ("san jose", 37.3382, -121.8863, -8),
    ("stanford", 37.4241, -122.1661, -8),
    ("carlsbad", 33.1581, -117.3506, -8),
    ("los angeles", 34.0522, -118.2437, -8),
    ("guadalajara", 20.6767, -103.3476, -6),
    ("monterrey", 25.6866, -100.3161, -6),
    ("bogota", 4.7110, -74.0721, -5),
    ("sao paulo", -23.5505, -46.6333, -3),
    ("santiago chile", -33.4489, -70.6693, -3),
    ("cordoba", -31.4201, -64.1888, -3),
    ("kraljevo", 43.7258, 20.6896, 1),
    ("banja luka", 44.7722, 17.1910, 1),
    ("umag", 45.4484, 13.5240, 1),
    ("bastad", 56.4349, 12.8254, 1),
    ("mallorca", 39.5696, 2.6502, 1),
    ("gijon", 43.5322, -5.6611, 1),
    ("valencia", 39.4699, -0.3763, 1),
    ("barcelona open", 41.410, 2.183, 1),
    ("lyon", 45.7640, 4.8357, 1),
    ("marseille", 43.2965, 5.3698, 1),
    ("nice", 43.7102, 7.2620, 1),
    ("montpellier", 43.6108, 3.8767, 1),
    ("strasbourg", 48.5734, 7.7521, 1),
    ("bordeaux", 44.8378, -0.5792, 1),
    ("den bosch", 51.6978, 5.3037, 1),
    ("s hertogenbosch", 51.6978, 5.3037, 1),
    ("nottingham", 52.9548, -1.1581, 0),
    ("birmingham", 52.4862, -1.8904, 0),
    ("berlin", 52.5200, 13.4050, 1),
    ("bad homburg", 50.2268, 8.6181, 1),
    ("palermo", 38.1157, 13.3615, 1),
    ("prague", 50.0755, 14.4378, 1),
    ("budapest", 47.4979, 19.0402, 1),
    ("warsaw", 52.2297, 21.0122, 1),
    ("cluj", 46.7712, 23.6236, 2),
    ("iasi", 47.1585, 27.6014, 2),
    ("belgrade", 44.7866, 20.4489, 1),
    ("bucharest", 44.4268, 26.1025, 2),
    ("sofia", 42.6977, 23.3219, 2),
    ("porto", 41.1579, -8.6291, 0),
    ("estoril", 38.694, -9.379, 0),
    ("marrakesh", 31.630, -7.986, 0),
    ("casablanca", 33.5731, -7.5898, 0),
    ("tel aviv", 32.0853, 34.7818, 2),
    ("almaty", 43.2220, 76.8512, 5),
    ("astana", 51.1694, 71.4491, 5),
    ("chengdu", 30.5728, 104.0668, 8),
    ("zhuhai", 22.2769, 113.5678, 8),
    ("hangzhou", 30.2741, 120.1551, 8),
    ("wuhan", 30.5928, 114.3055, 8),
    ("zhengzhou", 34.7466, 113.6253, 8),
    ("hong kong", 22.3193, 114.1694, 8),
    ("guangzhou", 23.1291, 113.2644, 8),
    ("shenzhen", 22.5431, 114.0579, 8),
    ("seoul", 37.5665, 126.9780, 9),
    ("busan", 35.1796, 129.0756, 9),
    ("hiroshima", 34.3853, 132.4553, 9),
    ("nagoya", 35.1815, 136.9066, 9),
    ("osaka", 34.6937, 135.5023, 9),
    ("pune", 18.5204, 73.8567, 5),
    ("bengaluru", 12.9716, 77.5946, 5),
    ("chennai", 13.0827, 80.2707, 5),
    ("hyderabad", 17.3850, 78.4867, 5),
    ("bnp paribas open", 33.722, -116.412, -8),
    ("national bank open", 43.648, -79.379, -5),
    ("omni cincinnati", 39.118, -84.519, -5),
    ("western southern", 39.118, -84.519, -5),
    ("china open", 39.925, 116.389, 8),
    ("wuhan open", 30.5928, 114.3055, 8),
    ("dubai duty free", 25.239, 55.279, 4),
    ("qatar total", 25.285, 51.547, 3),
    ("qatar open", 25.285, 51.547, 3),
    ("abn amro", 51.925, 4.478, 1),
    ("swiss indoors", 47.547, 7.596, 1),
    ("european open", 51.229, 4.489, 1),
    ("stockholm open", 59.345, 18.069, 1),
    ("st petersburg open", 59.914, 30.389, 3),
    ("sydney international", -33.8688, 151.2093, 10),
    ("hobart international", -42.8821, 147.3272, 10),
    ("asuncion", -25.2637, -57.5759, -4),
    ("cordoba open", -31.4201, -64.1888, -3),
    ("los cabos open", 22.8905, -109.9167, -7),
    ("atlanta open", 33.875, -84.459, -5),
    ("los angeles open", 34.0522, -118.2437, -8),
    ("dallas open", 32.7767, -96.7970, -6),
    ("mallorca championships", 39.5696, 2.6502, 1),
    ("eastbourne international", 50.764, 0.285, 0),
    ("queens club", 51.489, -0.196, 0),
    ("queen s club", 51.489, -0.196, 0),
    ("libema open", 51.6978, 5.3037, 1),
    ("rosmalen", 51.6978, 5.3037, 1),
    ("porsche tennis", 48.790, 9.229, 1),
    ("ladies open lausanne", 46.5197, 6.6323, 1),
    ("lausanne", 46.5197, 6.6323, 1),
    ("gstaad open", 46.475, 7.297, 1),
    ("kitzbuhel open", 47.439, 12.389, 1),
    ("geneva open", 46.229, 6.146, 1),
    ("munich open", 48.191, 11.579, 1),
    ("hamburg european", 53.5511, 9.9937, 1),
    ("croatia open", 45.4484, 13.5240, 1),
    ("austrian open", 47.439, 12.389, 1),
    ("swedish open", 56.4349, 12.8254, 1),
    ("nordea open", 56.4349, 12.8254, 1),
    ("winston salem open", 36.0999, -80.2442, -5),
    ("citi open", 38.910, -77.014, -5),
    ("mifel open", 20.6767, -103.3476, -6),
    ("abierto mexicano", 16.764, -99.834, -6),
    ("rio open", -22.999, -43.362, -3),
    ("argentina open", -34.560, -58.449, -3),
    ("chile open", -33.4489, -70.6693, -3),
    ("colombia open", 4.7110, -74.0721, -5),
    ("firenze", 43.7696, 11.2558, 1),
    ("florence", 43.7696, 11.2558, 1),
    ("parma", 44.8015, 10.3279, 1),
    ("cagliari", 39.2238, 9.1217, 1),
    ("sardinia", 39.2238, 9.1217, 1),
    ("genoa", 44.4056, 8.9463, 1),
    ("turin atp", 45.069, 7.694, 1),
    ("japan open tennis", 35.660, 139.694, 9),
    ("korea open", 37.5665, 126.9780, 9),
    ("thailand open", 13.7563, 100.5018, 7),
    ("singapore", 1.3521, 103.8198, 8),
    ("malaysia", 3.1390, 101.6869, 8),
    ("jakarta", -6.2088, 106.8456, 7),
    ("nanchang", 28.6820, 115.8579, 8),
    ("jiujiang", 29.7051, 115.9928, 8),
    ("montpellier open", 43.6108, 3.8767, 1),
    ("rome masters", 41.925, 12.478, 1),
    ("roma", 41.925, 12.478, 1),
    ("foro italico", 41.925, 12.478, 1),
    ("milano", 45.4642, 9.1900, 1),
    ("madrid masters", 40.424, -3.694, 1),
    ("miami open", 25.768, -80.337, -5),
    ("canada masters", 43.648, -79.379, -5),
    ("wta finals", 25.285, 51.547, 3),
    ("wta finals shenzhen", 22.5431, 114.0579, 8),
    ("wta finals cancun", 21.1619, -86.8515, -5),
    ("wta finals fort worth", 32.7767, -96.7970, -6),
    ("wta finals riyadh", 24.7136, 46.6753, 3),
    ("united cup", -33.8688, 151.2093, 10),
    ("hopman cup", -31.9523, 115.8613, 8),
)

# Fallback par surface / région vaguement européenne
_DEFAULT_EURO: Tuple[float, float, int] = (46.2276, 2.2137, 1)
DEFAULT_US_HARD: Tuple[float, float, int] = (40.0, -97.0, -6)


def _norm(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tournament_site_lon_lat_tz(name: object) -> Tuple[float, float, int]:
    """Retourne (lat, lon, utc_offset_hr)."""
    n = _norm(name)
    best_k, best_l = "", 0
    for key, *_ in _SITE_LIST:
        kn = key
        if kn in n and len(kn) > best_l:
            best_k, best_l = kn, len(kn)
    if best_k:
        for tup in _SITE_LIST:
            if tup[0] == best_k:
                return tup[1], tup[2], tup[3]
    if "north american" in n or " atlanta " in n or "usa" in n:
        lat, lon, tz = DEFAULT_US_HARD
        return lat, lon, tz
    return _DEFAULT_EURO


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    r = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    d_phi = radians(lat2 - lat1)
    d_lbd = radians(lon2 - lon1)
    h = sin(d_phi / 2) ** 2 + cos(p1) * cos(p2) * sin(d_lbd / 2) ** 2
    return 2 * r * asin(min(1.0, sqrt(h)))


# Export dict pour compat avec consigne sync_tml (lookup par clé lisible).
TOURNAMENT_GPS: Dict[str, Tuple[float, float, int]] = {key: (la, lo, tz) for key, la, lo, tz in _SITE_LIST}
