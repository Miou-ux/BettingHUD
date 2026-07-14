"""Garde-fous rang/points pour Live Tracker, Paris du jour, API PREPROD."""
from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

PARIS_TZ = ZoneInfo("Europe/Paris")

# Exclusion UI si stats_reference_date (TML / WTA) a plus de N jours vs date du match.
STALE_RANK_STATS_MAX_DAYS = max(
    30, int(os.getenv("BETTINGHUD_STALE_RANK_STATS_MAX_DAYS", "365"))
)

MIN_DATA_RELIABILITY_SCORE = max(
    0, min(100, int(os.getenv("BETTINGHUD_MIN_DATA_RELIABILITY", "80")))
)

# Seuil doc / flag ``book_gap_high`` (pénalité score fiabilité — pas filtre de sélection).
BOOK_GAP_HIGH_PP = float(os.getenv("BETTINGHUD_BOOK_GAP_HIGH_PP", "25"))

DUPLICATE_MODEL_PROB_PENALTY = max(
    0, min(50, int(os.getenv("BETTINGHUD_DUP_PROB_PENALTY", "20")))
)

MODEL_ODDS_INCONSISTENT_TOLERANCE_PP = max(
    0.5,
    float(os.getenv("BETTINGHUD_MODEL_ODDS_INCONSISTENT_PP", "3.0")),
)
MODEL_ODDS_INCONSISTENT_PENALTY = max(
    0, min(50, int(os.getenv("BETTINGHUD_MODEL_ODDS_INCONSISTENT_PENALTY", "25")))
)

# Incrémenter si la formule de score change (force rescore hors snapshot rebuild).
RELIABILITY_SCORE_VERSION = 4

# Repli ``stats_engine.get_player_stats`` / preview live quand aucune source rang/points.
_DEFAULT_STATS_RANK = 100
_DEFAULT_STATS_PTS = 1000.0
_DEFAULT_STATS_SOURCES = frozenset(
    {"no_ranking_source", "preview_default", "fast_default", "cache_default"}
)


def is_default_player_stats(stats: dict | None) -> bool:
    """True si rang/points sont le paquet fictif (pas de données réelles en base)."""
    if not stats:
        return False
    src = str(stats.get("stats_source") or "").strip().lower()
    if src in _DEFAULT_STATS_SOURCES:
        return True
    try:
        rank = int(stats.get("rank") or 0)
        pts = float(stats.get("pts") or 0)
    except (TypeError, ValueError):
        return True
    return rank == _DEFAULT_STATS_RANK and abs(pts - _DEFAULT_STATS_PTS) < 1e-6


def match_both_default_player_stats(match: dict) -> bool:
    """Les deux joueurs ont le repli rank=100 / pts=1000 (vecteur features quasi identique)."""
    if not isinstance(match, dict):
        return False
    return is_default_player_stats(match.get("p1_stats")) and is_default_player_stats(
        match.get("p2_stats")
    )


def book_gap_pp_from_favorite(p_model_fav: object, odd_fav: object) -> float | None:
    """Écart |p_modèle(favori) − p_implicite(book)| en points de pourcentage.

    Règle unique prod : cote **publique** du favori modèle (pas ``true_odd`` / marge).
    """
    try:
        p = float(p_model_fav)
        o = float(odd_fav)
    except (TypeError, ValueError):
        return None
    if o <= 1.0 or not (0.0 <= p <= 1.0):
        return None
    return abs(p - (1.0 / o)) * 100.0


def book_gap_pp_from_match(match: dict) -> float | None:
    """``book_gap_pp`` canonique depuis une ligne snapshot (``feature_snapshot`` + cotes)."""
    if not isinstance(match, dict):
        return None
    fs = match.get("feature_snapshot") or {}
    try:
        p1 = float(fs.get("capped_p1_prob") or 0.5)
        odd_p1 = float(match.get("odd_p1") or 0.0)
        odd_p2 = float(match.get("odd_p2") or 0.0)
    except (TypeError, ValueError):
        return None
    if odd_p1 <= 1.0 or odd_p2 <= 1.0:
        return None
    fav_side = 1 if p1 >= 0.5 else 2
    fav_p = max(p1, 1.0 - p1)
    odd_fav = odd_p1 if fav_side == 1 else odd_p2
    return book_gap_pp_from_favorite(fav_p, odd_fav)


def attach_book_gap_pp(match: dict) -> dict:
    """Attache ``book_gap_pp`` si absent (idempotent)."""
    if not isinstance(match, dict):
        return match
    if match.get("book_gap_pp") is not None:
        return match
    gap = book_gap_pp_from_match(match)
    if gap is not None:
        match["book_gap_pp"] = gap
    return match


def _rank_stats_source_key(stats: dict | None) -> str | None:
    if not stats:
        return None
    s = str(stats.get("stats_source") or "").strip().lower()
    if not s or s == "no_ranking_source":
        return None
    return s


def stats_reference_age_days(
    stats: dict | None,
    *,
    anchor_date: str | None = None,
) -> int | None:
    """Âge en jours de ``stats_reference_date`` par rapport à la date du match (ou aujourd'hui)."""
    if not stats:
        return None
    ref = stats.get("stats_reference_date")
    if not ref:
        return None
    anchor = str(anchor_date or datetime.now(PARIS_TZ).strftime("%Y-%m-%d"))[:10]
    try:
        d_ref = pd.Timestamp(str(ref)[:10]).normalize()
        d_anchor = pd.Timestamp(anchor).normalize()
        return int(max(0, (d_anchor - d_ref).days))
    except Exception:
        return None


def player_rank_stats_fresh(
    stats: dict | None,
    *,
    anchor_date: str | None = None,
    max_days: int | None = None,
) -> bool:
    """True si la référence rang/points est présente et ≤ max_days (défaut 12 mois)."""
    limit = STALE_RANK_STATS_MAX_DAYS if max_days is None else max(1, int(max_days))
    age = stats_reference_age_days(stats, anchor_date=anchor_date)
    if age is None:
        return False
    return age <= limit


def match_rank_exclude_reason(
    match: dict,
    *,
    anchor_date: str | None = None,
    max_days: int | None = None,
) -> str | None:
    """Raison d'exclusion UI (None = éligible)."""
    if not isinstance(match, dict):
        return "invalid_match"
    if match_both_default_player_stats(match):
        return "default_stats_placeholder"
    p1 = match.get("p1_stats") or {}
    p2 = match.get("p2_stats") or {}
    k1 = _rank_stats_source_key(p1)
    k2 = _rank_stats_source_key(p2)
    if not k1 or not k2:
        return "missing_rank_source"
    if k1 == "tennisexplorer_estimate" or k2 == "tennisexplorer_estimate":
        return "tennisexplorer_estimate"
    anchor = anchor_date or str(match.get("date") or "")[:10] or None
    if not player_rank_stats_fresh(p1, anchor_date=anchor, max_days=max_days):
        return "stale_rank_stats"
    if not player_rank_stats_fresh(p2, anchor_date=anchor, max_days=max_days):
        return "stale_rank_stats"
    return None


def match_has_rank_points_source(match: dict) -> bool:
    """Même garde-fou que le Live Tracker / report algo / API PREPROD."""
    return match_rank_exclude_reason(match) is None


def match_duplicate_prob_identity_key(match: dict) -> tuple:
    """Clé stable pour détecter deux matchs distincts partageant la même proba modèle."""
    if not isinstance(match, dict):
        return ("", "", "", "")
    return (
        str(match.get("p1_player_id") or ""),
        str(match.get("p2_player_id") or ""),
        str(match.get("player1") or "").strip().lower(),
        str(match.get("player2") or "").strip().lower(),
    )


def capped_p1_prob_from_match(match: dict) -> float | None:
    """Proba modèle P1 (après caps) — source de vérité pour l'affichage public."""
    if not isinstance(match, dict):
        return None
    fs = match.get("feature_snapshot") or {}
    try:
        raw = fs.get("capped_p1_prob")
        if raw is None:
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def model_prob_for_side(match: dict, side: int) -> float | None:
    """Proba modèle pour le joueur 1 ou 2 (alignée ``capped_p1_prob``)."""
    p1 = capped_p1_prob_from_match(match)
    if p1 is None:
        return None
    side_i = int(side)
    if side_i == 1:
        return p1
    if side_i == 2:
        return 1.0 - p1
    return None


def model_true_odd_for_side(match: dict, side: int) -> float | None:
    """Cote juste modèle pour un côté, dérivée des caps (pas du champ snapshot stale)."""
    p = model_prob_for_side(match, side)
    if p is None or p <= 0.0:
        return None
    return 1.0 / min(1.0, max(0.01, float(p)))


def match_model_odds_inconsistent(
    match: dict,
    *,
    tol_pp: float | None = None,
) -> bool:
    """True si ``true_odd_p*`` diverge de ``capped_p1_prob`` (cause classique des 95 % fantômes)."""
    p1 = capped_p1_prob_from_match(match)
    if p1 is None:
        return True
    tol = (
        float(MODEL_ODDS_INCONSISTENT_TOLERANCE_PP)
        if tol_pp is None
        else float(tol_pp)
    )
    for side, key in ((1, "true_odd_p1"), (2, "true_odd_p2")):
        try:
            to = float(match.get(key) or 0.0)
        except (TypeError, ValueError):
            return True
        if to <= 1.0:
            return True
        expected = p1 if side == 1 else (1.0 - p1)
        if abs((1.0 / to) - expected) * 100.0 > tol:
            return True
    return False


def reconcile_match_true_odds_from_caps(match: dict) -> dict:
    """Réaligne ``true_odd_p1/p2`` sur ``capped_p1_prob`` (mutate in place)."""
    if not isinstance(match, dict):
        return match
    p1 = capped_p1_prob_from_match(match)
    if p1 is None:
        return match
    p1c = min(0.99, max(0.01, float(p1)))
    p2c = 1.0 - p1c
    match["true_odd_p1"] = 1.0 / p1c
    match["true_odd_p2"] = 1.0 / p2c
    return match


def normalize_match_model_probs(match: dict) -> dict:
    """Réaligne les cotes justes modèle avant scoring / picks publics."""
    reconcile_match_true_odds_from_caps(match)
    return match


def normalize_matches_model_probs(matches: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in matches:
        if isinstance(m, dict):
            out.append(normalize_match_model_probs(dict(m)))
    return out


def _is_materialized_pick_row(row: dict) -> bool:
    """Ligne Top5/hybride (``collect_top5_proba_picks``) sans ``feature_snapshot``."""
    if capped_p1_prob_from_match(row) is not None:
        return False
    return row.get("p_model_fav") is not None and row.get("data_reliability_score") is not None


def passes_public_pick_gates(
    match_or_pick: dict | None,
    *,
    duplicate_keys: set[tuple] | None = None,
    min_score: int | None = None,
) -> bool:
    """Filtre unifié prod : fiabilité ≥ seuil, pas de duplicate proba, caps cohérents."""
    if not isinstance(match_or_pick, dict):
        return False
    # Pick déjà matérialisé depuis un match snapshot (pas de feature_snapshot embarqué).
    if _is_materialized_pick_row(match_or_pick):
        if not passes_data_reliability_filter(match_or_pick, min_score=min_score):
            return False
        if excluded_duplicate_model_prob_from_top5(
            match_or_pick, duplicate_keys=duplicate_keys
        ):
            return False
        return True
    if capped_p1_prob_from_match(match_or_pick) is None:
        return False
    if match_model_odds_inconsistent(match_or_pick):
        return False
    if not passes_data_reliability_filter(match_or_pick, min_score=min_score):
        return False
    if excluded_duplicate_model_prob_from_top5(
        match_or_pick, duplicate_keys=duplicate_keys
    ):
        return False
    return True


def duplicate_model_prob_keys(
    matches: list[dict],
    *,
    precision: int = 10,
) -> set[tuple]:
    """Identités des matchs dont ``capped_p1_prob`` est partagée dans le **même tournoi**.

    Évite les faux positifs cross-tournoi (deux favoris ~97 % le même jour).
    """
    by_bucket: dict[tuple[float, str], list[tuple]] = defaultdict(list)
    for m in matches:
        if not isinstance(m, dict):
            continue
        fs = m.get("feature_snapshot") or {}
        cp = fs.get("capped_p1_prob")
        if cp is None:
            continue
        try:
            prob_key = round(float(cp), precision)
        except (TypeError, ValueError):
            continue
        tourney = str(m.get("tournament") or "").strip().lower()
        by_bucket[(prob_key, tourney)].append(match_duplicate_prob_identity_key(m))
    bad: set[tuple] = set()
    for keys in by_bucket.values():
        if len(set(keys)) > 1:
            bad.update(keys)
    return bad


def match_in_duplicate_model_prob_cluster(
    match: dict,
    duplicate_keys: set[tuple] | None,
) -> bool:
    if not duplicate_keys:
        return False
    return match_duplicate_prob_identity_key(match) in duplicate_keys


def has_duplicate_model_prob_flag(row: dict | None) -> bool:
    """True si la ligne porte le flag ``duplicate_model_prob`` (bool ou flags persistés)."""
    if not isinstance(row, dict):
        return False
    if row.get("duplicate_model_prob"):
        return True
    flags = row.get("data_reliability_flags")
    if isinstance(flags, list):
        return "duplicate_model_prob" in flags
    return "duplicate_model_prob" in str(flags or "")


def excluded_duplicate_model_prob_from_top5(
    row: dict | None,
    *,
    duplicate_keys: set[tuple] | None = None,
) -> bool:
    """True si le pick ne doit pas être publié en Top 5 (cluster proba modèle dupliquée)."""
    if not isinstance(row, dict):
        return False
    if has_duplicate_model_prob_flag(row):
        return True
    return match_in_duplicate_model_prob_cluster(row, duplicate_keys)


def _player_rank_placeholder(stats: dict | None) -> bool:
    if not stats:
        return False
    if is_default_player_stats(stats):
        return True
    try:
        rank = int(stats.get("rank") or 0)
        pts = float(stats.get("pts") or 0)
        return rank >= 1500 or pts < 10.0
    except (TypeError, ValueError):
        return True


def match_data_reliability_score(
    match: dict,
    *,
    hist_te_conflict: bool = False,
    ref_date_stale_sides: tuple[bool, bool] = (False, False),
    data_stale_sides: tuple[bool, bool] = (False, False),
    duplicate_model_prob: bool = False,
    hist_te_soft_penalty: bool = False,
    model_odds_inconsistent: bool = False,
) -> tuple[int, list[str]]:
    """Score 0–100 de confiance dans les données d'une ligne snapshot (hors qualité tennis pure).

    100 = identités + rangs récents + proba cohérente ; pénalités cumulatives documentées dans
    `docs/DATA_RELIABILITY.md`.

    ``hist_te_soft_penalty`` : conflit Base/TE mais rangs officiels frais → pénalité réduite (-8).
    """
    if not isinstance(match, dict):
        return 0, ["invalid_match"]
    score = 100
    flags: list[str] = []
    anchor = str(match.get("date") or "")[:10] or None
    stale_rank_sides: list[bool] = [False, False]

    if match.get("unreliable"):
        score -= 40
        flags.append("rang_vs_proba")

    if duplicate_model_prob:
        score -= DUPLICATE_MODEL_PROB_PENALTY
        flags.append("duplicate_model_prob")

    if model_odds_inconsistent:
        score -= MODEL_ODDS_INCONSISTENT_PENALTY
        flags.append("model_odds_inconsistent")

    if hist_te_conflict:
        score -= 8 if hist_te_soft_penalty else 20
        flags.append("hist_te_conflict")
        if hist_te_soft_penalty:
            flags.append("hist_te_soft")

    for idx, (side, pk, pid_key, ref_stale, data_stale) in enumerate(
        (
            ("p1", "p1_stats", "p1_player_id", ref_date_stale_sides[0], data_stale_sides[0]),
            ("p2", "p2_stats", "p2_player_id", ref_date_stale_sides[1], data_stale_sides[1]),
        )
    ):
        st = match.get(pk) or {}
        if not match.get(pid_key):
            score -= 20
            flags.append(f"{side}_unresolved_id")
        src = _rank_stats_source_key(st)
        if not src:
            score -= 12
            flags.append(f"{side}_no_rank_source")
        elif src == "tennisexplorer_estimate":
            score -= 15
            flags.append(f"{side}_te_estimate")
        if _player_rank_placeholder(st):
            score -= 20
            flags.append(f"{side}_rank_placeholder")
        if not player_rank_stats_fresh(st, anchor_date=anchor):
            score -= 10
            flags.append(f"{side}_stale_rank_ref")
            stale_rank_sides[idx] = True
        if ref_stale and not stale_rank_sides[idx] and not player_rank_stats_fresh(
            st, anchor_date=anchor
        ):
            score -= 8
            flags.append(f"{side}_ref_date_stale")
        elif data_stale:
            score -= 6
            flags.append(f"{side}_data_stale")

    if str(match.get("snapshot_tier") or "full") == "preview":
        score -= 15
        flags.append("preview_tier")

    try:
        gap = float(match.get("book_gap_pp")) if match.get("book_gap_pp") is not None else None
    except (TypeError, ValueError):
        gap = None
    if gap is not None and gap > BOOK_GAP_HIGH_PP:
        score -= min(20, int((gap - BOOK_GAP_HIGH_PP) / 3.0))
        flags.append("book_gap_high")

    score = max(0, min(100, score))
    return score, flags


def compute_match_reliability(
    match: dict,
    *,
    duplicate_keys: set[tuple] | None = None,
) -> tuple[int, list[str]]:
    """Score complet à partir d'une ligne snapshot (même logique que finalize live build)."""
    from scripts.reliability_context import (
        match_has_hist_te_conflict,
        match_has_official_fresh_ranks,
        player_data_stale,
        player_ref_date_stale,
    )

    dup = False
    if duplicate_keys is not None:
        dup = match_in_duplicate_model_prob_cluster(match, duplicate_keys)
    elif match.get("duplicate_model_prob"):
        dup = True

    hist_te = match_has_hist_te_conflict(match)
    soft_te = hist_te and match_has_official_fresh_ranks(match)

    score, flags = match_data_reliability_score(
        match,
        hist_te_conflict=hist_te,
        hist_te_soft_penalty=soft_te,
        ref_date_stale_sides=(
            player_ref_date_stale(match, 1),
            player_ref_date_stale(match, 2),
        ),
        data_stale_sides=(
            player_data_stale(match, 1),
            player_data_stale(match, 2),
        ),
        duplicate_model_prob=dup,
        model_odds_inconsistent=match_model_odds_inconsistent(match),
    )
    return score, flags


def ensure_match_reliability_scored(
    match: dict,
    *,
    day_matches: list[dict] | None = None,
    duplicate_keys: set[tuple] | None = None,
) -> dict:
    """Calcule et attache le score si absent ou version obsolète."""
    if not isinstance(match, dict):
        return match
    normalize_match_model_probs(match)
    attach_book_gap_pp(match)

    ver = match.get("data_reliability_version")
    needs_rescore = (
        match.get("data_reliability_score") is None
        or int(ver or 0) < RELIABILITY_SCORE_VERSION
    )
    if not needs_rescore:
        return match

    if duplicate_keys is None and day_matches:
        duplicate_keys = duplicate_model_prob_keys(day_matches)

    score, flags = compute_match_reliability(match, duplicate_keys=duplicate_keys)
    match["data_reliability_score"] = score
    match["data_reliability_flags"] = "|".join(flags) if flags else None
    match["data_reliability_version"] = RELIABILITY_SCORE_VERSION
    match["duplicate_model_prob"] = match_in_duplicate_model_prob_cluster(
        match, duplicate_keys
    )
    return match


def reliability_fields_from_match(match: dict | None) -> dict[str, object]:
    """Champs à persister depuis une ligne snapshot."""
    if not isinstance(match, dict):
        return {"data_reliability_score": None, "data_reliability_flags": None}
    flags = match.get("data_reliability_flags")
    if isinstance(flags, list):
        flags_s = "|".join(str(x) for x in flags if x) or None
    else:
        flags_s = str(flags).strip() if flags else None
    score = match.get("data_reliability_score")
    try:
        score_i = int(score) if score is not None else None
    except (TypeError, ValueError):
        score_i = None
    return {"data_reliability_score": score_i, "data_reliability_flags": flags_s}


def passes_data_reliability_filter(
    match_or_pick: dict | None,
    *,
    min_score: int | None = None,
) -> bool:
    """True si la ligne est proposable (pas ``unreliable``, score >= seuil)."""
    if not isinstance(match_or_pick, dict):
        return False
    if match_or_pick.get("unreliable"):
        return False
    threshold = MIN_DATA_RELIABILITY_SCORE if min_score is None else int(min_score)
    score = match_or_pick.get("data_reliability_score")
    if score is None:
        return False
    try:
        return int(score) >= threshold
    except (TypeError, ValueError):
        return False


def count_matches_excluded_by_reason(
    matches: list[dict],
    *,
    anchor_date: str | None = None,
) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in matches:
        if not isinstance(m, dict):
            out["invalid_match"] = out.get("invalid_match", 0) + 1
            continue
        reason = match_rank_exclude_reason(m, anchor_date=anchor_date)
        if reason:
            out[reason] = out.get(reason, 0) + 1
    return out
