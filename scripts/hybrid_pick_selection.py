"""Sélection hybride prod — Top 5 et 1 Day 1 Pick.

Tier 1 : proba modèle ≥ 77 %, fiabilité data ≥ 85, EV favori 15–35 % (inclus).
Tier 2 : complément si < ``limit`` picks, EV 30–55 % (exclus 30, inclus 55).
Tri proba modèle ↓ (puis EV), cap book_gap ≤ 30 pp, dédup match, exclusion duplicate_model_prob.
"""
from __future__ import annotations

from scripts.daily_top_proba_store import dedupe_top_proba_rows_by_match
from scripts.match_rank_quality import (
    duplicate_model_prob_keys,
    passes_public_pick_gates,
)

HYBRID_MIN_PROBA_FRAC = 0.77
HYBRID_MIN_RELIABILITY_SCORE = 85
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


def hybrid_base_ok(row: dict, *, duplicate_keys: set | None = None) -> bool:
    if not passes_public_pick_gates(
        row,
        duplicate_keys=duplicate_keys,
        min_score=HYBRID_MIN_RELIABILITY_SCORE,
    ):
        return False
    if float(row.get("p_model_fav") or 0.0) < HYBRID_MIN_PROBA_FRAC:
        return False
    if not _book_gap_ok(row):
        return False
    return True


def hybrid_pool_ok(row: dict, *, duplicate_keys: set | None = None) -> bool:
    """Éligible au pool hybride (union tier 1 + tier 2 EV)."""
    if not hybrid_base_ok(row, duplicate_keys=duplicate_keys):
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


def select_hybrid_picks(
    candidates: list[dict],
    *,
    limit: int | None = HYBRID_DEFAULT_LIMIT,
    duplicate_keys: set | None = None,
    apply_telegram_proba_filter: bool = False,
) -> list[dict]:
    """Sélection hybride pour un jour (candidats déjà normalisés)."""
    pool = [r for r in candidates if hybrid_pool_ok(r, duplicate_keys=duplicate_keys)]
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
        out.append(pick)
    return out


def best_hybrid_pick(candidates: list[dict], **kwargs) -> dict | None:
    picks = select_hybrid_picks(candidates, **kwargs)
    if not picks:
        return None
    out = dict(picks[0])
    out["selection_mode"] = "hybrid_best"
    return out


def count_hybrid_pool_candidates(candidates: list[dict], *, duplicate_keys: set | None = None) -> int:
    return sum(1 for r in candidates if hybrid_pool_ok(r, duplicate_keys=duplicate_keys))


def hybrid_criteria_line(*, english: bool | None = None) -> str:
    from scripts.comms_locale import comms_is_english

    en = comms_is_english() if english is None else bool(english)
    rel = HYBRID_MIN_RELIABILITY_SCORE
    _sort = str(HYBRID_SORT).lower()
    sort_label = "EV" if _sort == "ev" else ("Proba" if _sort == "proba" else str(HYBRID_SORT))
    if en:
        return (
            f"📊 Model proba <code>≥{HYBRID_MIN_PROBA_FRAC * 100:.0f}%</code> · "
            f"EV tier1 <code>{HYBRID_TIER1_EV_MIN_PCT:.0f}–{HYBRID_TIER1_EV_MAX_PCT:.0f}%</code> · "
            f"tier2 <code>{HYBRID_TIER2_EV_MIN_PCT:.0f}–{HYBRID_TIER2_EV_MAX_PCT:.0f}%</code> "
            f"(fill to {HYBRID_DEFAULT_LIMIT}/day) · reliability ≥{rel} · "
            f"book gap ≤{HYBRID_BOOK_GAP_MAX_PP:.0f}pp · sorted by {sort_label} ↓"
        )
    return (
        f"📊 Proba <code>≥{HYBRID_MIN_PROBA_FRAC * 100:.0f}%</code> · "
        f"EV tier1 <code>{HYBRID_TIER1_EV_MIN_PCT:.0f}–{HYBRID_TIER1_EV_MAX_PCT:.0f}%</code> · "
        f"tier2 <code>{HYBRID_TIER2_EV_MIN_PCT:.0f}–{HYBRID_TIER2_EV_MAX_PCT:.0f}%</code> "
        f"(complément max {HYBRID_DEFAULT_LIMIT}/jour) · fiabilité ≥{rel} · "
        f"écart cote ≤{HYBRID_BOOK_GAP_MAX_PP:.0f} pp · tri {sort_label} ↓"
    )


def hybrid_criteria_plain(*, english: bool | None = None, rank1: bool = False) -> str:
    """Texte critères hybride sans balises Telegram (web / API CourtAlpha)."""
    from scripts.comms_locale import comms_is_english

    en = comms_is_english() if english is None else bool(english)
    rel = HYBRID_MIN_RELIABILITY_SCORE
    _sort = str(HYBRID_SORT).lower()
    sort_label = "EV" if _sort == "ev" else ("Proba" if _sort == "proba" else str(HYBRID_SORT))
    p_pct = f"{HYBRID_MIN_PROBA_FRAC * 100:.0f}"
    if en:
        core = (
            f"model proba >= {p_pct}%, data reliability >= {rel}, "
            f"book gap <= {HYBRID_BOOK_GAP_MAX_PP:.0f}pp, "
            f"EV tier 1: {HYBRID_TIER1_EV_MIN_PCT:.0f}-{HYBRID_TIER1_EV_MAX_PCT:.0f}% "
            f"then tier 2: {HYBRID_TIER2_EV_MIN_PCT:.0f}-{HYBRID_TIER2_EV_MAX_PCT:.0f}% "
            f"(fill to {HYBRID_DEFAULT_LIMIT}/day), sorted by {sort_label} down, majors 250+."
        )
        prefix = "Rank 1 of hybrid Top 5 selection: " if rank1 else "Hybrid Top 5: "
        return prefix + core
    core = (
        f"proba modèle ≥ {p_pct} %, fiabilité ≥ {rel}, écart cote ≤ {HYBRID_BOOK_GAP_MAX_PP:.0f} pp, "
        f"EV tier 1 : {HYBRID_TIER1_EV_MIN_PCT:.0f}–{HYBRID_TIER1_EV_MAX_PCT:.0f} % "
        f"puis tier 2 : {HYBRID_TIER2_EV_MIN_PCT:.0f}–{HYBRID_TIER2_EV_MAX_PCT:.0f} % "
        f"(complément max {HYBRID_DEFAULT_LIMIT}/jour), tri {sort_label} ↓, majeurs 250+."
    )
    prefix = "Rang 1 de la sélection hybride Top 5 : " if rank1 else "Top 5 hybride : "
    return prefix + core
