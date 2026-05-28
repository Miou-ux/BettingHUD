"""Score de priorité composite (Sharpe unitaire × qualité calibration segment V47)."""
from __future__ import annotations

BRIER_CALIBRATION_CAP = 0.25
PREMIUM_SEGMENT_BRIER_MAX = 0.18
SEGMENT_BRIER_FLOOR = 0.05


def segment_brier_safe(segment_brier: float, *, floor: float = SEGMENT_BRIER_FLOOR) -> float:
    try:
        b = float(segment_brier)
    except (TypeError, ValueError):
        b = BRIER_CALIBRATION_CAP
    return max(float(floor), b)


def calibration_quality_factor(
    segment_brier: float,
    *,
    cap: float = BRIER_CALIBRATION_CAP,
) -> float:
    """Facteur (1 - Brier/cap), identique au Kelly adaptatif du dashboard."""
    b = segment_brier_safe(segment_brier)
    return max(0.0, 1.0 - b / float(cap))


def sharpe_per_brier(sharpe_ratio: float, segment_brier: float) -> float:
    """Sharpe ajusté : rapport de Sharpe unitaire / Brier segment."""
    try:
        s = float(sharpe_ratio)
    except (TypeError, ValueError):
        s = 0.0
    b = segment_brier_safe(segment_brier)
    return float(s / b)


def priority_score_composite(
    sharpe_ratio: float,
    segment_brier: float,
    *,
    cap: float = BRIER_CALIBRATION_CAP,
) -> float:
    """priority_score = (sharpe / segment_brier) × (1 - segment_brier / cap)."""
    b = segment_brier_safe(segment_brier)
    s_adj = sharpe_per_brier(sharpe_ratio, b)
    return float(s_adj * calibration_quality_factor(b, cap=cap))


def is_premium_segment(
    segment_brier: float,
    *,
    threshold: float = PREMIUM_SEGMENT_BRIER_MAX,
) -> bool:
    try:
        return float(segment_brier) < float(threshold)
    except (TypeError, ValueError):
        return False


def enrich_value_metrics(
    val: dict,
    *,
    sharpe_ratio: float | None = None,
    segment_brier: float,
) -> dict:
    """Ajoute segment_brier, sharpe_per_brier, priority_score à un dict detect_value."""
    out = dict(val)
    sr = float(sharpe_ratio if sharpe_ratio is not None else out.get("sharpe_ratio") or 0.0)
    b = segment_brier_safe(segment_brier)
    out["segment_brier"] = float(b)
    out["sharpe_per_brier"] = sharpe_per_brier(sr, b)
    out["priority_score"] = priority_score_composite(sr, b)
    return out
