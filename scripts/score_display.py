"""Formatage lisible des scores tennis (cache TE → UI)."""
from __future__ import annotations


def _expand_side_tokens(tokens: list[str]) -> list[int]:
    out: list[int] = []
    for tok in tokens:
        t = str(tok or "").strip()
        if not t.isdigit():
            continue
        if len(t) == 2:
            a, b = int(t[0]), int(t[1])
            if a <= 7 and b <= 7:
                out.extend([a, b])
                continue
        out.append(int(t))
    return out


def format_tennis_score_display(raw: object) -> str | None:
    """
    Convertit le format cache « jeux P1 | jeux P2 » (ex. ``7 6 | 6 2``, ``7 6 | 62 2``)
    en sets lisibles (ex. ``7-6, 6-2``).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "|" not in s:
        return s

    left, right = s.split("|", 1)
    g1 = _expand_side_tokens(left.split())
    g2 = _expand_side_tokens(right.split())
    if not g1 and not g2:
        return s

    n = min(len(g1), len(g2)) if g1 and g2 else max(len(g1), len(g2))
    if g1 and g2:
        n = min(len(g1), len(g2))
        sets = [f"{g1[i]}-{g2[i]}" for i in range(n)]
        return ", ".join(sets)

    side = g1 or g2
    return " ".join(str(x) for x in side)
