"""Sélection hybride prod — Top picks du jour, 1D1P, Telegram.

Prod (juillet 2026) : **HYB P75+P80-all** — P75-TIER (p≥73 %, rel≥80, EV 6–55 %,
tier fill, max 6) + compléments **P≥80 % rel≥80** (sans filtre EV), tri proba ↓.

Legacy EV tier1/tier2 (P77) : voir ``select_hybrid_picks_legacy`` si besoin replay.
"""
from __future__ import annotations

from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match
from scripts.match_rank_quality import (
    duplicate_model_prob_keys,
    passes_public_pick_gates,
)

HYBRID_MIN_PROBA_FRAC = 0.77
HYBRID_MIN_RELIABILITY_SCORE = 85
HYBRID_FALLBACK_RELIABILITY_SCORE = 80
HYBRID_BOOK_GAP_MAX_PP = 30.0
HYBRID_SORT = "proba"
HYBRID_TIER1_EV_MIN_PCT = 15.0
HYBRID_TIER1_EV_MAX_PCT = 35.0
HYBRID_TIER2_EV_MIN_PCT = 30.0
HYBRID_TIER2_EV_MAX_PCT = 55.0
HYBRID_POOL_EV_MIN_PCT = HYBRID_TIER1_EV_MIN_PCT
HYBRID_POOL_EV_MAX_PCT = HYBRID_TIER2_EV_MAX_PCT
HYBRID_DEFAULT_LIMIT = 6


def ev_fav_pct(row: dict) -> float:
    ev_pct = row.get("ev_fav_pct")
    if ev_pct is not None:
        return float(ev_pct)
    return float(row.get("ev_fav") or row.get("ev") or 0.0) * 100.0


def _book_gap_ok(row: dict) -> bool:
    gap = row.get("book_gap_pp")
    if gap is None:
        return True
    try:
        return float(gap) <= float(HYBRID_BOOK_GAP_MAX_PP)
    except (TypeError, ValueError):
        return True


def hybrid_base_ok(
    row: dict,
    *,
    duplicate_keys: set | None = None,
    min_score: int | None = None,
) -> bool:
    rel = int(min_score if min_score is not None else HYBRID_MIN_RELIABILITY_SCORE)
    if not passes_public_pick_gates(
        row,
        duplicate_keys=duplicate_keys,
        min_score=rel,
    ):
        return False
    if float(row.get("p_model_fav") or 0.0) < HYBRID_MIN_PROBA_FRAC:
        return False
    if not _book_gap_ok(row):
        return False
    return True


def hybrid_pool_ok(
    row: dict,
    *,
    duplicate_keys: set | None = None,
    min_score: int | None = None,
) -> bool:
    """Éligible au pool hybride (union tier 1 + tier 2 EV)."""
    if not hybrid_base_ok(row, duplicate_keys=duplicate_keys, min_score=min_score):
        return False
    ev = ev_fav_pct(row)
    return HYBRID_POOL_EV_MIN_PCT <= ev <= HYBRID_POOL_EV_MAX_PCT


def _in_tier1(row: dict) -> bool:
    ev = ev_fav_pct(row)
    return HYBRID_TIER1_EV_MIN_PCT <= ev <= HYBRID_TIER1_EV_MAX_PCT


def _in_tier2(row: dict) -> bool:
    ev = ev_fav_pct(row)
    return HYBRID_TIER2_EV_MIN_PCT < ev <= HYBRID_TIER2_EV_MAX_PCT


def _sort_key(row: dict) -> tuple:
    p = float(row.get("p_model_fav") or 0.0)
    ev = ev_fav_pct(row) / 100.0
    name = str(row.get("match_name") or "").lower()
    sort = str(HYBRID_SORT or "proba").lower()
    if sort == "ev":
        return (-ev, -p, name)
    if sort == "edge":
        return (-(ev * p), -p, name)
    return (-p, -ev, name)


def _select_hybrid_picks_at_rel(
    candidates: list[dict],
    *,
    rel_min: int,
    limit: int | None,
    duplicate_keys: set | None,
    apply_telegram_proba_filter: bool,
) -> list[dict]:
    pool = [r for r in candidates if hybrid_pool_ok(r, duplicate_keys=duplicate_keys, min_score=rel_min)]
    tier1 = [r for r in pool if _in_tier1(r)]
    tier2 = [r for r in pool if _in_tier2(r)]
    uncapped = limit is None or int(limit) <= 0
    cap = int(limit) if limit is not None and int(limit) > 0 else 0

    def rank(rows: list[dict]) -> list[dict]:
        ranked = sorted(rows, key=_sort_key)
        ranked = dedupe_top_proba_rows_by_match(ranked)
        if apply_telegram_proba_filter:
            from scripts.telegram_top5_notify import filter_telegram_display_picks

            ranked = filter_telegram_display_picks(ranked, apply_proba_filter=True)
        return ranked

    picked: list[dict] = []
    seen: set[str] = set()
    for row in rank(tier1):
        key = str(row.get("match_name") or "").lower()
        if key in seen:
            continue
        picked.append(row)
        seen.add(key)
        if not uncapped and len(picked) >= cap:
            break
    if uncapped or len(picked) < cap:
        for row in rank(tier2):
            key = str(row.get("match_name") or "").lower()
            if key in seen:
                continue
            picked.append(row)
            seen.add(key)
            if not uncapped and len(picked) >= cap:
                break

    out: list[dict] = []
    for rank_i, row in enumerate(picked, start=1):
        pick = dict(row)
        pick["rank"] = rank_i
        pick["hybrid_tier"] = "tier1" if _in_tier1(pick) else "tier2"
        pick["hybrid_rel_min"] = rel_min
        out.append(pick)
    return out


def select_hybrid_picks(
    candidates: list[dict],
    *,
    limit: int | None = None,
    duplicate_keys: set | None = None,
    apply_telegram_proba_filter: bool = False,
) -> list[dict]:
    """Sélection prod HYB P75+P80-all (sans plafond par défaut)."""
    from scripts.hyb_p75_p80_selection import select_hyb_p75_p80_all

    _ = apply_telegram_proba_filter  # legacy kwarg — HYB pur partout (plus de filtre TG)
    return select_hyb_p75_p80_all(candidates, duplicate_keys=duplicate_keys, limit=limit)


def select_hybrid_picks_legacy(
    candidates: list[dict],
    *,
    limit: int | None = HYBRID_DEFAULT_LIMIT,
    duplicate_keys: set | None = None,
    apply_telegram_proba_filter: bool = False,
) -> list[dict]:
    """Ancienne hybride P77 tier1/tier2 (backtests comparatifs)."""
    out = _select_hybrid_picks_at_rel(
        candidates,
        rel_min=HYBRID_MIN_RELIABILITY_SCORE,
        limit=limit,
        duplicate_keys=duplicate_keys,
        apply_telegram_proba_filter=apply_telegram_proba_filter,
    )
    if out:
        return out
    fb = int(HYBRID_FALLBACK_RELIABILITY_SCORE)
    if fb >= int(HYBRID_MIN_RELIABILITY_SCORE):
        return out
    out = _select_hybrid_picks_at_rel(
        candidates,
        rel_min=fb,
        limit=limit,
        duplicate_keys=duplicate_keys,
        apply_telegram_proba_filter=apply_telegram_proba_filter,
    )
    for pick in out:
        pick["hybrid_rel_fallback"] = True
    return out


def best_hybrid_pick(candidates: list[dict], **kwargs) -> dict | None:
    from scripts.hyb_p75_p80_selection import best_1d1p_pick_from_hyb

    picks = select_hybrid_picks(candidates, **kwargs)
    return best_1d1p_pick_from_hyb(picks)


def count_hybrid_pool_candidates(candidates: list[dict], *, duplicate_keys: set | None = None) -> int:
    from scripts.hyb_p75_p80_selection import count_hyb_pool_candidates

    return count_hyb_pool_candidates(candidates, duplicate_keys=duplicate_keys)


def hybrid_criteria_line(*, english: bool | None = None) -> str:
    from scripts.comms_locale import comms_is_english
    from scripts.hyb_p75_p80_selection import P80_MIN_PROBA_FRAC, P80_MIN_REL

    en = comms_is_english() if english is None else bool(english)
    if en:
        return (
            f"📊 <b>HYB P75+P80</b> · P75-TIER (p≥73%, rel≥80, EV 6–55%, max 6) + "
            f"add-ons p≥{P80_MIN_PROBA_FRAC * 100:.0f}% rel≥{P80_MIN_REL} (any EV) · sorted by proba ↓"
        )
    return (
        f"📊 <b>HYB P75+P80</b> · P75-TIER (p≥73 %, rel≥80, EV 6–55 %, max 6) + "
        f"compléments p≥{P80_MIN_PROBA_FRAC * 100:.0f} % rel≥{P80_MIN_REL} (EV libre) · tri proba ↓"
    )


def hybrid_criteria_plain(*, english: bool | None = None, rank1: bool = False) -> str:
    """Texte critères hybride sans balises Telegram (web / API CourtAlpha)."""
    from scripts.comms_locale import comms_is_english
    from scripts.hyb_p75_p80_selection import P80_MIN_PROBA_FRAC, P80_MIN_REL

    en = comms_is_english() if english is None else bool(english)
    if en:
        core = (
            f"P75-TIER (p≥73%, rel≥80, EV 6–55%, tier fill, max 6/day) plus "
            f"P≥{P80_MIN_PROBA_FRAC * 100:.0f}% rel≥{P80_MIN_REL} add-ons (no EV cap), "
            f"deduped by match, sorted by model proba down, majors 250+."
        )
        prefix = "Rank 1 by highest proba in HYB P75+P80: " if rank1 else "HYB P75+P80-all: "
        return prefix + core
    core = (
        f"P75-TIER (p≥73 %, rel≥80, EV 6–55 %, tiers, max 6/j) + "
        f"compléments P≥{P80_MIN_PROBA_FRAC * 100:.0f} % rel≥{P80_MIN_REL} (EV libre), "
        f"dédoublonnés par match, tri proba ↓, majeurs 250+."
    )
    prefix = "Rang 1 = meilleure proba HYB P75+P80 : " if rank1 else "HYB P75+P80-all : "
    return prefix + core
