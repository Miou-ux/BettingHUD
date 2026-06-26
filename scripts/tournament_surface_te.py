"""Surface tournoi depuis la page Tennis Explorer (secours si registre + heuristiques insuffisants)."""
from __future__ import annotations

import re
import urllib.request

_TE_BASE = "https://www.tennisexplorer.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)

_TE_SURFACE_TO_CATEGORY = {
    "clay": "Clay",
    "terre battue": "Clay",
    "terre": "Clay",
    "hard": "Hard",
    "dur": "Hard",
    "grass": "Grass",
    "gazon": "Grass",
    "carpet": "Carpet",
    "indoor hard": "Hard",
    "indoor": "Hard",
}


def normalize_te_surface_label(raw: object) -> str | None:
    s = str(raw or "").strip().lower()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    for key, cat in _TE_SURFACE_TO_CATEGORY.items():
        if key in s:
            return cat
    title = s.title()
    if title in ("Hard", "Clay", "Grass", "Carpet"):
        return title
    return None


def parse_surface_from_te_html(html: str) -> str | None:
    if not html:
        return None
    for pat in (
        r"surface[:\s]*</t[hd]>\s*<t[hd][^>]*>\s*([^<]+)",
        r">surface<[^>]*>\s*</t[hd]>\s*<td[^>]*>\s*([^<]+)",
        r"surface\s*:\s*</[^>]+>\s*<[^>]+>\s*([^<]+)",
    ):
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            hit = normalize_te_surface_label(m.group(1))
            if hit:
                return hit
    m = re.search(
        r"surface[^<]{0,40}?<td[^>]*>\s*([A-Za-zÀ-ÿ /]+)\s*</td>",
        html,
        flags=re.IGNORECASE,
    )
    if m:
        return normalize_te_surface_label(m.group(1))
    return None


def fetch_surface_from_te_url(tournament_url: object, *, timeout: int = 10) -> str | None:
    href = str(tournament_url or "").strip().split("?")[0]
    if not href:
        return None
    url = href if href.startswith("http") else f"{_TE_BASE}{href}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        html = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="replace")
    except Exception:
        return None
    return parse_surface_from_te_html(html)
