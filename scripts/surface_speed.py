"""Tennis Abstract–style surface speed ratings (0–1 scale, ~median ~0.75).

Higher = faster court (more serve dominance). Used for Micro-Elo K-weighting and
ML interaction features. Defaults to 0.75 when unknown.

Outdoor matches can adjust CPI using humidity / temperature when those values are
known before play (forecast or official conditions).

References (approximate TA CPI-derived indices): Rome clay slow (~0.57), Madrid
altitude clay faster (~0.89).
"""

import re
import json
import os
from functools import lru_cache
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
    "rabat": 0.60,
    "morocco open": 0.60,
    "moroccan open": 0.60,
    "lalla meryem": 0.60,
    "marrakech": 0.60,
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
    "bad homburg": 0.91,
    "eastbourne": 0.90,
    "nottingham": 0.90,
    "rosmalen": 0.90,
}

# Grass before clay: "hamburg" (clay) must not match "bad homburg" / "homburg" (grass).
_GRASS_TOKENS = (
    "bad homburg",
    "s-hertogenbosch",
    "'s-hertogenbosch",
    "queen's club",
    "queens club",
    "eastbourne",
    "nottingham",
    "rosmalen",
    "wimbledon",
    "halle",
    "mallorca",
    "homburg",
    "stuttgart",
)
_CLAY_TOKENS = (
    "french open",
    "roland garros",
    "roland-garros",
    "roland",
    "garros",
    "rome",
    "madrid",
    "monte-carlo",
    "monte carlo",
    "barcelona",
    "hamburg",
    "marrakech",
    "rabat",
    "morocco open",
    "moroccan open",
    "lalla meryem",
    "bastad",
    "kitzbuhel",
    "geneva",
    "estoril",
    "parma",
    "terre battue",
)
_CARPET_TOKENS = ("carpet",)
_HARD_TOKENS = (
    "australian",
    "us open",
    "miami",
    "indian wells",
    "dubai",
    "doha",
    "brisbane",
    "tokyo",
    "shanghai",
    "beijing",
    "montreal",
    "toronto",
    "cincinnati",
)


def _normalize_tournament_name(tournament_name: object) -> str:
    n = str(tournament_name or "").lower().strip()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _name_has_token(n: str, token: str) -> bool:
    token = str(token or "").strip().lower()
    if not token or not n:
        return False
    if " " in token:
        return token in n
    return bool(re.search(rf"\b{re.escape(token)}\b", n))


def infer_surface_category(tournament_name: object) -> str:
    """Infer Hard|Clay|Grass|Carpet from tournament label (word-boundary safe)."""
    n = _normalize_tournament_name(tournament_name)
    if not n:
        return "Hard"
    for token in _GRASS_TOKENS:
        if _name_has_token(n, token):
            return "Grass"
    for token in _CLAY_TOKENS:
        if _name_has_token(n, token):
            return "Clay"
    for token in _CARPET_TOKENS:
        if _name_has_token(n, token):
            return "Carpet"
    for token in _HARD_TOKENS:
        if _name_has_token(n, token):
            return "Hard"
    return "Hard"


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SURFACE_REGISTRY_PATH = os.path.join(_ROOT, "config", "tournament_surface.json")


@lru_cache(maxsize=1)
def _load_surface_registry_entries() -> tuple[tuple[str, str], ...]:
    """(normalized_key, surface) pairs — longest keys should be listed first in JSON."""
    path = _SURFACE_REGISTRY_PATH
    if not os.path.isfile(path):
        return ()
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return ()
    out: list[tuple[str, str]] = []
    for entry in payload.get("entries") or []:
        surf = str(entry.get("surface") or "").strip().title()
        if surf not in ("Hard", "Clay", "Grass", "Carpet"):
            continue
        for key in entry.get("keys") or []:
            nk = _normalize_tournament_name(key)
            if nk:
                out.append((nk, surf))
    out.sort(key=lambda x: len(x[0]), reverse=True)
    return tuple(out)


def _registry_surface(tournament_name: object) -> str | None:
    n = _normalize_tournament_name(tournament_name)
    if not n:
        return None
    for key, surf in _load_surface_registry_entries():
        if key in n or n in key:
            return surf
    return None


def resolve_tournament_surface(
    tournament_name: object,
    *,
    tournament_url: object = None,
    te_surface: object = None,
) -> str:
    """Surface canonique : registre JSON → heuristiques → TE (secours) → Hard."""
    from scripts.tournament_surface_te import fetch_surface_from_te_url, normalize_te_surface_label

    reg = _registry_surface(tournament_name)
    if reg:
        return reg
    inferred = infer_surface_category(tournament_name)
    if inferred != "Hard":
        return inferred
    te_label = normalize_te_surface_label(te_surface) if te_surface else None
    if te_label:
        return te_label
    if tournament_url:
        te = fetch_surface_from_te_url(tournament_url)
        if te:
            return te
    return inferred


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
    n = _normalize_tournament_name(tournament_name)
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
