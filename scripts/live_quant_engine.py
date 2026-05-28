"""Moteur quantitatif In-Play : prior V47 + Markov sets (BO3/BO5) + EV live."""
from __future__ import annotations

import re
from dataclasses import dataclass
from math import comb
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class LiveProbabilityResult:
    prematch_proba: float
    set_win_proba: float
    live_proba: float
    live_odd: float | None
    ev_live: float | None
    best_of: int
    sets_p1: int
    sets_p2: int


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _match_win_from_set_prob(q: float, best_of: int) -> float:
    """Probabilité de gagner le match si probabilité de gagner chaque set = q."""
    target = best_of // 2 + 1
    return sum(
        comb(target + losses - 1, losses) * (q**target) * ((1.0 - q) ** losses)
        for losses in range(target)
    )


def _set_prob_from_match_prior(p_match: float, best_of: int, *, iterations: int = 48) -> float:
    """Inverse : trouve q tel que P(match|q) ~= prior prematch."""
    p_match = _clamp01(p_match)
    lo, hi = 0.0, 1.0
    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        if _match_win_from_set_prob(mid, best_of) < p_match:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _race_probability(q: float, sets_p1: int, sets_p2: int, best_of: int) -> float:
    """Probabilité que P1 gagne le match étant donné le score en sets."""
    target = best_of // 2 + 1
    if sets_p1 >= target:
        return 1.0
    if sets_p2 >= target:
        return 0.0
    need_p1 = target - sets_p1
    need_p2 = target - sets_p2
    return sum(
        comb(need_p1 + opp_extra - 1, opp_extra) * (q**need_p1) * ((1.0 - q) ** opp_extra)
        for opp_extra in range(need_p2)
    )


def calculate_live_probability(
    prematch_proba: float,
    current_score: tuple[int, int],
    *,
    best_of: int = 3,
    live_odd: float | None = None,
    set_state_weight: float = 1.0,
) -> LiveProbabilityResult:
    """
    Recalcule la probabilité live de victoire P1 à partir du prior prematch et du score en sets.

    live_proba = prior + w * (markov_live - prior)
    w=1 applique entièrement le Markov ; w=0 conserve le prior.
  """
    if best_of not in (3, 5):
        raise ValueError("best_of must be 3 or 5")
    sets_p1, sets_p2 = int(current_score[0]), int(current_score[1])
    target = best_of // 2 + 1
    if sets_p1 < 0 or sets_p2 < 0 or sets_p1 > target or sets_p2 > target:
        raise ValueError("invalid set score for best_of")
    if sets_p1 >= target or sets_p2 >= target:
        raise ValueError("match already decided at set level")

    prior = _clamp01(prematch_proba)
    q = _set_prob_from_match_prior(prior, best_of)
    markov_live = _race_probability(q, sets_p1, sets_p2, best_of)
    w = _clamp01(set_state_weight)
    live_p = _clamp01(prior + w * (markov_live - prior))

    ev = None
    if live_odd is not None:
        try:
            odd_f = float(live_odd)
            if odd_f > 1.0:
                ev = live_p * odd_f - 1.0
        except (TypeError, ValueError):
            pass

    return LiveProbabilityResult(
        prematch_proba=prior,
        set_win_proba=q,
        live_proba=live_p,
        live_odd=float(live_odd) if live_odd is not None else None,
        ev_live=ev,
        best_of=best_of,
        sets_p1=sets_p1,
        sets_p2=sets_p2,
    )


def _normalize_game_pair(raw: Any) -> tuple[int, int] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        m = re.search(r"(\d+)\s*[-:]\s*(\d+)", raw.strip())
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def _parse_set_pair(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\s*[-:]\s*(\d+)", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _status_indicates_set_break(status: str) -> bool:
    s = (status or "").strip().lower()
    if not s:
        return False
    tokens = (
        "set break",
        "setbreak",
        "between sets",
        "pause",
        "inter-set",
        "inter set",
        "fin de set",
        "fin du set",
    )
    return any(t in s for t in tokens)


def _match_is_over(sets_p1: int, sets_p2: int, best_of: int | None) -> bool:
    if best_of not in (3, 5):
        best_of = 3
    target = best_of // 2 + 1
    return sets_p1 >= target or sets_p2 >= target


def build_manual_score_state(
    *,
    sets_p1: int,
    sets_p2: int,
    game_p1: int = 0,
    game_p2: int = 0,
    status_set_break: bool = False,
    status_first_set_in_progress: bool = False,
    best_of: int = 3,
) -> dict[str, Any]:
    """Construit un état score normalisé depuis les champs UI manuels."""
    return {
        "sets_p1": max(0, int(sets_p1)),
        "sets_p2": max(0, int(sets_p2)),
        "current_game": (max(0, int(game_p1)), max(0, int(game_p2))),
        "completed_sets": [],
        "status_set_break": bool(status_set_break),
        "status_first_set_in_progress": bool(status_first_set_in_progress),
        "best_of": int(best_of) if int(best_of) in (3, 5) else 3,
        "match_over": _match_is_over(int(sets_p1), int(sets_p2), int(best_of)),
        "source": "manual",
    }


def parse_live_score_state(raw: Any) -> dict[str, Any]:
    """
    Parse un score live depuis dict API/scraper ou string libre.

    Formats supportés (exemples) :
    - {"status": "Set Break", "score": {"set_scores": [[6,4]], "current_game": [0,0]}}
    - "6-4 0-0"
    - "sets 1-0, game 0-0, Set Break"
    - {"sets_p1": 1, "sets_p2": 0, "current_game": [0, 0], "status_set_break": true}
    """
    out: dict[str, Any] = {
        "sets_p1": 0,
        "sets_p2": 0,
        "current_game": None,
        "completed_sets": [],
        "status_set_break": False,
        "status_first_set_in_progress": False,
        "best_of": 3,
        "match_over": False,
        "source": "unknown",
    }

    if raw is None:
        return out

    if isinstance(raw, Mapping):
        out["source"] = "dict"
        status = str(raw.get("status") or raw.get("match_status") or "")
        out["status_set_break"] = _status_indicates_set_break(status) or bool(
            raw.get("status_set_break") or raw.get("set_break")
        )
        out["status_first_set_in_progress"] = bool(
            raw.get("status_first_set_in_progress")
            or raw.get("first_set_in_progress")
            or raw.get("in_first_set")
        )
        try:
            bo = int(raw.get("best_of") or raw.get("bestOf") or 3)
            if bo in (3, 5):
                out["best_of"] = bo
        except (TypeError, ValueError):
            pass

        if "sets_p1" in raw or "sets_p2" in raw:
            try:
                out["sets_p1"] = int(raw.get("sets_p1") or 0)
                out["sets_p2"] = int(raw.get("sets_p2") or 0)
            except (TypeError, ValueError):
                pass

        score_obj = raw.get("score") if isinstance(raw.get("score"), Mapping) else raw
        if isinstance(score_obj, Mapping):
            if "sets_p1" in score_obj or "sets_p2" in score_obj:
                try:
                    out["sets_p1"] = int(score_obj.get("sets_p1") or out["sets_p1"])
                    out["sets_p2"] = int(score_obj.get("sets_p2") or out["sets_p2"])
                except (TypeError, ValueError):
                    pass
            cg = _normalize_game_pair(score_obj.get("current_game"))
            if cg is not None:
                out["current_game"] = cg
            set_scores = score_obj.get("set_scores") or score_obj.get("sets") or []
            completed: list[tuple[int, int]] = []
            p1_sets = p2_sets = 0
            if isinstance(set_scores, Sequence) and not isinstance(set_scores, (str, bytes)):
                for item in set_scores:
                    pair = None
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        try:
                            pair = (int(item[0]), int(item[1]))
                        except (TypeError, ValueError):
                            pair = None
                    elif isinstance(item, str):
                        pair = _parse_set_pair(item)
                    if pair is None:
                        continue
                    completed.append(pair)
                    if pair[0] > pair[1]:
                        p1_sets += 1
                    elif pair[1] > pair[0]:
                        p2_sets += 1
            if completed:
                out["completed_sets"] = completed
                if "sets_p1" not in raw and "sets_p2" not in raw:
                    out["sets_p1"] = p1_sets
                    out["sets_p2"] = p2_sets

        out["match_over"] = _match_is_over(out["sets_p1"], out["sets_p2"], out["best_of"])
        return out

    text = str(raw).strip()
    if not text:
        return out
    out["source"] = "string"
    lower = text.lower()
    out["status_set_break"] = _status_indicates_set_break(lower)
    out["status_first_set_in_progress"] = any(
        token in lower
        for token in (
            "first set",
            "1st set",
            "1er set",
            "premier set",
            "set 1",
        )
    )

    bo_m = re.search(r"\bbo\s*([35])\b", lower)
    if bo_m:
        out["best_of"] = int(bo_m.group(1))

    game_m = re.search(
        r"(?:game|jeu|current|actuel)\s*[:\s]*(\d+)\s*[-:]\s*(\d+)",
        lower,
    )
    if game_m:
        out["current_game"] = (int(game_m.group(1)), int(game_m.group(2)))

    sets_m = re.search(
        r"(?:sets?|score)\s*[:\s]*(\d+)\s*[-:]\s*(\d+)",
        lower,
    )
    if sets_m:
        out["sets_p1"] = int(sets_m.group(1))
        out["sets_p2"] = int(sets_m.group(2))
    else:
        pairs = [_parse_set_pair(p) for p in re.findall(r"\d+\s*[-:]\s*\d+", text)]
        pairs = [p for p in pairs if p is not None]
        if pairs:
            if out["current_game"] is None and len(pairs) >= 2:
                out["current_game"] = pairs[-1]
                completed_pairs = pairs[:-1]
            else:
                completed_pairs = pairs
            out["completed_sets"] = completed_pairs
            p1_sets = sum(1 for a, b in completed_pairs if a > b)
            p2_sets = sum(1 for a, b in completed_pairs if b > a)
            if sets_m is None:
                out["sets_p1"] = p1_sets
                out["sets_p2"] = p2_sets

    if out["current_game"] is None:
        all_pairs = [_parse_set_pair(p) for p in re.findall(r"\d+\s*[-:]\s*\d+", text)]
        all_pairs = [p for p in all_pairs if p is not None]
        if len(all_pairs) == 1 and not out["completed_sets"]:
            out["current_game"] = all_pairs[0]

    out["match_over"] = _match_is_over(out["sets_p1"], out["sets_p2"], out["best_of"])
    return out


def is_end_of_set(raw_score: Any, *, best_of: int | None = None) -> bool:
    """
    True uniquement en pause entre deux sets :
    - flag explicite Set Break / pause, ou
    - au moins un set terminé ET jeu courant 0-0 (pas en cours de set).
    """
    state = parse_live_score_state(raw_score)
    if best_of in (3, 5):
        state = {**state, "best_of": int(best_of)}

    if state.get("match_over"):
        return False
    if state.get("status_set_break"):
        return True

    sets_p1 = int(state.get("sets_p1") or 0)
    sets_p2 = int(state.get("sets_p2") or 0)
    completed = state.get("completed_sets") or []
    current_game = _normalize_game_pair(state.get("current_game"))

    has_finished_set = (sets_p1 + sets_p2) > 0 or len(completed) > 0
    at_set_start = current_game in ((0, 0), None)
    return bool(has_finished_set and at_set_start)


def is_first_set_in_progress(raw_score: Any, *, best_of: int | None = None) -> bool:
    """
    True si le match est encore dans le 1er set.

    Pour éviter d'afficher tout le prematch par défaut, un score 0-0 / 0-0
    nécessite un flag explicite (API ou UI) indiquant que le match est lancé.
    """
    state = parse_live_score_state(raw_score)
    if best_of in (3, 5):
        state = {
            **state,
            "best_of": int(best_of),
            "match_over": _match_is_over(
                int(state.get("sets_p1") or 0),
                int(state.get("sets_p2") or 0),
                int(best_of),
            ),
        }

    if state.get("match_over") or state.get("status_set_break"):
        return False

    sets_p1 = int(state.get("sets_p1") or 0)
    sets_p2 = int(state.get("sets_p2") or 0)
    completed = state.get("completed_sets") or []
    if sets_p1 != 0 or sets_p2 != 0 or completed:
        return False

    current_game = _normalize_game_pair(state.get("current_game"))
    explicit_live = bool(state.get("status_first_set_in_progress"))
    if current_game == (0, 0):
        return explicit_live
    return current_game is not None


def live_ev_pct(ev_live: float | None) -> float:
    if ev_live is None:
        return 0.0
    return float(ev_live) * 100.0
