"""Politique de mise Kelly prod (live, Telegram, backtests de référence)."""
from __future__ import annotations

# Fraction de Kelly plein avant ajustement Brier segment (juillet 2026 : 0.85, ex-0.65).
KELLY_BASE_FRAC = 0.85

# Plafond par pari (% bankroll / liquidité disponible).
KELLY_MAX_STAKE_FRAC = 0.15


def kelly_base_label(frac: float | None = None) -> str:
    """Libellé UI pour la fraction Kelly de base."""
    f = float(KELLY_BASE_FRAC if frac is None else frac)
    if abs(f - 0.5) < 1e-9:
        return "1/2"
    if abs(f - 0.25) < 1e-9:
        return "1/4"
    if abs(f - 0.75) < 1e-9:
        return "3/4"
    return f"{f:.2f}".rstrip("0").rstrip(".")


def kelly_base_label_long(frac: float | None = None) -> str:
    """Libellé long (Telegram / doc)."""
    return f"Kelly {kelly_base_label(frac)}"
