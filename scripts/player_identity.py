"""Utilitaires de normalisation de noms de joueurs.

Le `PlayerIdentityResolver` historique (qui s'appuyait sur les tables Sackmann
`players`, `player_aliases`, `players_mapping`) a été retiré : la résolution
identité passe désormais par les indexes par tour construits dans
`scripts.stats_engine.TennisStatsEngine` (matches_recent pour l'ATP, wta_matches
pour la WTA).
"""
from __future__ import annotations

import re
import unicodedata
from typing import List


def canonical_name(name: str) -> str:
    """Réduit un nom à sa forme `nom initiale` (ex. 'Sabalenka A.' -> 'sabalenka a').

    Cette fonction attend un nom au format **'Nom I.'** (le format courant des
    flux Flashscore). Pour normaliser un nom plein 'Prénom Nom' (Sackmann), passe
    d'abord par `to_lastname_initial`.
    """
    n = str(name or "").lower().strip()
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"#\d+", " ", n)
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n)
    parts = [p for p in n.split(" ") if p]
    while parts and parts[-1].isdigit():
        parts.pop()
    if not parts:
        return ""
    if len(parts) == 2 and len(parts[1]) == 1:
        return f"{parts[0]} {parts[1]}"
    last = parts[-2] if len(parts) >= 2 and len(parts[-1]) == 1 else parts[-1]
    ini = parts[-1][0] if parts[-1] else ""
    if len(parts) >= 2 and len(parts[-1]) == 1:
        ini = parts[-1]
    return f"{last} {ini}".strip()


def normalize_name(name: str) -> str:
    n = str(name or "").lower().strip()
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"#\d+", " ", n)
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


_NAME_PUNCT_RE = re.compile(r"[^A-Za-z\u00C0-\u017F\s\-]")
_NAME_SPLIT_RE = re.compile(r"[\s\-]+")


def to_lastname_initial(name: str) -> str:
    """Convertit un nom plein 'Prénom Nom' au format 'Nom P' (canonical-friendly).

    Si le nom est déjà sous la forme 'Nom I' ou 'Nom I.' (dernier token de 1 ou 2
    lettres), il est retourné tel quel.

    Exemples :
      'Aryna Sabalenka'         -> 'Sabalenka A'
      'Sabalenka A.'            -> 'Sabalenka A'
      'Felix Auger-Aliassime'   -> 'Aliassime F'
    """
    n = str(name or "")
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"#\d+", " ", n)
    n = _NAME_PUNCT_RE.sub(" ", n)
    parts = [p for p in _NAME_SPLIT_RE.split(n.strip()) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    last_tok = parts[-1].rstrip(".")
    if len(last_tok) <= 2:
        return " ".join(parts)
    first = parts[0]
    last = parts[-1]
    return f"{last} {first[0]}"


def name_variants(name: str) -> List[str]:
    """Variantes simples 'Prénom Nom' / 'Nom Prénom' (utile pour matching rapide)."""
    norm = normalize_name(name)
    if not norm:
        return []
    parts = norm.split()
    variants = {norm}
    if len(parts) >= 2:
        variants.add(" ".join(parts[::-1]))
    return [v for v in variants if v]
