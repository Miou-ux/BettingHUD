"""Description sélection hybride — source de vérité BettingHUD scripts."""
from __future__ import annotations


def hybrid_selection_description(*, rank1: bool = False) -> str:
    try:
        from scripts.hybrid_pick_selection import hybrid_criteria_plain

        return hybrid_criteria_plain(english=True, rank1=rank1)
    except Exception:
        base = (
            "HYB P75+P80-all: P75-TIER (p≥73%, rel≥80, EV 6–55%, max 6/day) plus "
            "P≥80% rel≥80 add-ons (any EV), deduped by match, sorted by proba ↓, majors 250+."
        )
        if rank1:
            return "Highest model proba in HYB P75+P80-all: " + base
        return base
