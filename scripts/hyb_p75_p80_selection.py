"""Sélection prod HYB P75+P80-all — P75-TIER + compléments P≥80 % rel≥80.

Utilisé partout en prod (Top 5, Telegram, dashboard, 1D1P) depuis juillet 2026.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from scripts.experiment_july_expert_kelly import EXPERT_SCOUT, ExpertCfg, select_expert_picks
from scripts.match_rank_quality import duplicate_model_prob_keys, passes_public_pick_gates

SCOUT_P75 = replace(EXPERT_SCOUT, min_proba=0.73, max_picks=6)
P75_TIER_CFG = replace(SCOUT_P75, tier_fill=True)

P80_MIN_PROBA_FRAC = 0.80
P80_MIN_REL = 80


def _match_key(row: dict) -> str:
    return str(row.get("match_name") or "").strip().lower()


def select_p80_rel80_picks(
    candidates: list[dict],
    *,
    duplicate_keys: set | None = None,
) -> list[dict]:
    """Tous les matchs P≥80 % rel≥80 (sans filtre EV)."""
    dup = duplicate_keys if duplicate_keys is not None else duplicate_model_prob_keys(candidates)
    out: list[dict] = []
    seen: set[str] = set()
    for row in sorted(candidates, key=lambda r: -float(r.get("p_model_fav") or 0)):
        if float(row.get("p_model_fav") or 0) < P80_MIN_PROBA_FRAC:
            continue
        if not passes_public_pick_gates(row, duplicate_keys=dup, min_score=P80_MIN_REL):
            continue
        mk = _match_key(row)
        if not mk or mk in seen:
            continue
        seen.add(mk)
        pick = dict(row)
        pick["hybrid_segment"] = "p80_addon"
        out.append(pick)
    return out


def select_hyb_p75_p80_all(
    candidates: list[dict],
    *,
    duplicate_keys: set | None = None,
    limit: int | None = None,
) -> list[dict]:
    """P75-TIER (max 6) ∪ P≥80 rel≥80, tri proba ↓, dédup match."""
    dup = duplicate_keys if duplicate_keys is not None else duplicate_model_prob_keys(candidates)
    base = select_expert_picks(candidates, duplicate_keys=dup, cfg=P75_TIER_CFG)
    extra = select_p80_rel80_picks(candidates, duplicate_keys=dup)

    merged: list[dict] = []
    seen: set[str] = set()
    base_keys = {_match_key(p) for p in base}

    for row in base:
        mk = _match_key(row)
        if not mk or mk in seen:
            continue
        seen.add(mk)
        pick = dict(row)
        pick["hybrid_segment"] = "p75_tier"
        merged.append(pick)

    for row in extra:
        mk = _match_key(row)
        if not mk or mk in seen:
            continue
        seen.add(mk)
        merged.append(dict(row))

    merged.sort(key=lambda r: (-float(r.get("p_model_fav") or 0), _match_key(r)))

    cap = len(merged) if limit is None or int(limit) <= 0 else int(limit)
    out: list[dict] = []
    for rank_i, row in enumerate(merged[:cap], start=1):
        pick = dict(row)
        pick["rank"] = rank_i
        pick["hybrid_tier"] = pick.get("hybrid_segment") or "p75_tier"
        out.append(pick)
    return out


def best_1d1p_pick_from_hyb(picks: list[dict]) -> dict[str, Any] | None:
    """Meilleur pick 1D1P = proba fav max dans HYB P75+P80-all."""
    if not picks:
        return None
    best = max(picks, key=lambda r: float(r.get("p_model_fav") or 0))
    out = dict(best)
    out["selection_mode"] = "hyb_p75_p80_best_proba"
    return out


def count_hyb_pool_candidates(candidates: list[dict], *, duplicate_keys: set | None = None) -> int:
    """Candidats éligibles P75-TIER ou P≥80 rel≥80 (union, dédup match)."""
    dup = duplicate_keys if duplicate_keys is not None else duplicate_model_prob_keys(candidates)
    keys: set[str] = set()
    for row in candidates:
        mk = _match_key(row)
        if not mk or mk in keys:
            continue
        p75_cfg = P75_TIER_CFG
        from scripts.experiment_july_expert_kelly import _expert_eligible

        ok_p75, _ = _expert_eligible(row, duplicate_keys=dup, cfg=p75_cfg)
        ok_p80 = (
            float(row.get("p_model_fav") or 0) >= P80_MIN_PROBA_FRAC
            and passes_public_pick_gates(row, duplicate_keys=dup, min_score=P80_MIN_REL)
        )
        if ok_p75 or ok_p80:
            keys.add(mk)
    return len(keys)
