"""Tennis Abstract–style surface speed ratings (0–1 scale, ~median ~0.75).

Higher = faster court (more serve dominance). Used for Micro-Elo K-weighting and
ML interaction features. Defaults to 0.75 when unknown.

Outdoor matches can adjust CPI using humidity / temperature when those values are
known before play (forecast or official conditions).

References (approximate TA CPI-derived indices): Rome clay slow (~0.57), Madrid
altitude clay faster (~0.89).
"""

import re
from typing import Optional, Tuple

SURFACE_DEFAULT = 0.75

CPI_CLIP_MIN = 0.35
CPI_CLIP_MAX = 0.98

# Overlap with ml_model._infer_tournament_context indoor keywords (outdoor = not these).
_INDOOR_TOURNAMENT_KEYWORDS = (
    "indoor",
    "atp finals",
    "next gen",
    "metz",
    "basel",
    "paris bercy",
    "vienna",
    "st petersburg",
    "rotterdam",
)

_HARD_DEFAULT = 0.82
_CLAY_DEFAULT = 0.62
_GRASS_DEFAULT = 0.94

# Longest substring wins (same strategy as CPI overrides in ml_model).
_TOURNAMENT_SURFACE_SPEED = {
    # Grand Slams
    "australian open": 0.84,
    "roland garros": 0.58,
    "french open": 0.58,
    "wimbledon": 0.96,
    "us open": 0.80,
    # Masters / 1000
    "indian wells": 0.78,
    "miami": 0.76,
    "monte carlo": 0.56,
    "monte-carlo": 0.56,
    "madrid": 0.89,
    "rome": 0.57,
    "italian open": 0.57,
    "internazionali bnl": 0.57,
    "canadian open": 0.81,
    "rogers cup": 0.81,
    "national bank open": 0.81,
    "cincinnati": 0.83,
    "western & southern open": 0.83,
    "shanghai": 0.85,
    "paris masters": 0.88,
    "rolex paris masters": 0.88,
    "bercy": 0.88,
    # Notable 500/250
    "halle": 0.93,
    "queen's club": 0.92,
    "stuttgart": 0.94,
    "barcelona": 0.59,
    "hamburg": 0.57,
    "rio open": 0.58,
    "buenos aires": 0.59,
    "estoril": 0.60,
    "geneva": 0.59,
    "munich": 0.58,
    "doha": 0.84,
    "dubai": 0.85,
    "rotterdam": 0.87,
    "vienna": 0.88,
    "basel": 0.88,
    "atp finals": 0.86,
    "wta finals": 0.82,
    "stuttgart open": 0.61,
    "charleston": 0.58,
    "guadalajara": 0.81,
}


def infer_outdoor(indoor_field: object, tournament_name: object) -> bool:
    """True if the match is played outdoor (O / not explicitly indoor).

    Uses ATP/WTA ``indoor`` column when present (I=indoor, O=outdoor), else name heuristics.
    """
    s = str(indoor_field or "").strip().upper()
    if s in ("I", "INDOOR"):
        return False
    if s in ("O", "OUTDOOR"):
        return True
    n = str(tournament_name or "").lower()
    return not any(k in n for k in _INDOOR_TOURNAMENT_KEYWORDS)


def weather_impact_scalars(
    outdoor: bool,
    humidity_pct: Optional[float],
    temp_c: Optional[float],
) -> Tuple[float, float]:
    """Leak-safe scalars: humidity >70% → -0.10 on speed; temp >28°C → +0.05 (outdoor only)."""
    h_imp = 0.0
    t_imp = 0.0
    if not outdoor:
        return h_imp, t_imp
    try:
        if humidity_pct is not None and float(humidity_pct) > 70.0:
            h_imp = -0.10
    except (TypeError, ValueError):
        pass
    try:
        if temp_c is not None and float(temp_c) > 28.0:
            t_imp = 0.05
    except (TypeError, ValueError):
        pass
    return h_imp, t_imp


def effective_surface_speed_cpi(
    base_cpi: float,
    outdoor: bool,
    humidity_pct: Optional[float],
    temp_c: Optional[float],
) -> float:
    """Apply outdoor humidity/temperature adjustments to CPI (clipped)."""
    try:
        x = float(base_cpi)
    except (TypeError, ValueError):
        x = SURFACE_DEFAULT
    mult = 1.0
    if outdoor:
        try:
            if humidity_pct is not None and float(humidity_pct) > 70.0:
                mult *= 0.90
        except (TypeError, ValueError):
            pass
        try:
            if temp_c is not None and float(temp_c) > 28.0:
                mult *= 1.05
        except (TypeError, ValueError):
            pass
    return float(max(CPI_CLIP_MIN, min(CPI_CLIP_MAX, x * mult)))


def lookup_surface_speed(tournament_name: object, surface: object) -> float:
    """Return surface speed index in [0.35, 0.98] with default 0.75."""
    n = str(tournament_name or "").lower().strip()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n)
    if n:
        best_key, best_len = None, 0
        for k in _TOURNAMENT_SURFACE_SPEED:
            if k in n and len(k) > best_len:
                best_key, best_len = k, len(k)
        if best_key is not None:
            return float(_TOURNAMENT_SURFACE_SPEED[best_key])
    s = str(surface or "").strip().title()
    if s == "Hard":
        return _HARD_DEFAULT
    if s == "Clay":
        return _CLAY_DEFAULT
    if s == "Grass":
        return _GRASS_DEFAULT
    if s == "Carpet":
        return 0.86
    return SURFACE_DEFAULT
