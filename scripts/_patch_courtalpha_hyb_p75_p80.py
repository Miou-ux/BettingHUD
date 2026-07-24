#!/usr/bin/env python3
"""Patch CourtAlpha UI (FR+EN) for HYB P75+P80-all prod rules."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path("/opt/courtalpha")

TOP5_FR = (
    "HYB P75+P80-all : P75-TIER (p≥73 %, rel≥80, EV 6–55 %, max 6/j) + "
    "compléments P≥80 % rel≥80 (EV libre), tri proba ↓ — majeurs 250+."
)
TOP5_EN = (
    "HYB P75+P80-all: P75-TIER (p≥73%, rel≥80, EV 6–55%, max 6/day) plus "
    "P≥80% rel≥80 add-ons (any EV), sorted by proba ↓ — 250+ majors only."
)
OD1P_FR = (
    "Un pick par jour : meilleure proba fav dans HYB P75+P80-all "
    "(même union que Top 5). BR initiale 100 € ; replay depuis le snapshot live."
)
OD1P_EN = (
    "One pick per day: highest model proba in HYB P75+P80-all "
    "(same union as Top 5). 100 € starting BR; replay from the live snapshot."
)
EMPTY_FR = "Critères HYB P75+P80 — consulte le Live Tracker ou attends le prochain snapshot."
EMPTY_EN = "HYB P75+P80 criteria — check Live Tracker or wait for the next snapshot."

METHOD_BODY_FR = (
    "Top 5 : HYB P75+P80-all — P75-TIER (p≥73 %, rel≥80, EV 6–55 %, max 6/j) + "
    "compléments P≥80 % rel≥80 (EV libre), tri proba ↓. "
    "1 Day 1 Pick : meilleure proba fav dans cette union. "
    "Paris du jour (/jour) reste en value bets EV ≥ 15 %."
)
METHOD_BODY_EN = (
    "Top 5: HYB P75+P80-all — P75-TIER (p≥73%, rel≥80, EV 6–55%, max 6/day) plus "
    "P≥80% rel≥80 add-ons (any EV), sorted by proba ↓. "
    "1 Day 1 Pick: highest model proba in that union. "
    "Today's picks (/today) remain value bets EV ≥ 15%."
)
METHOD_PROTO_FR = (
    "Protocole : ATP+WTA, tournois G/M/A, sélection HYB P75+P80-all "
    "(P75-TIER + P≥80 rel≥80, tri proba ↓), modèle entraîné avant chaque année testée. "
    "ROI année = Kelly 0,65 × Brier (cap 15 % liquidité). "
    "ROI 1D1P = meilleure proba HYB/jour, mise fixe 1 unité. "
    "Détails complets non publiés — résultats indicatifs, passé ≠ futur."
)
METHOD_PROTO_EN = (
    "Protocol: ATP+WTA, G/M/A tournaments, HYB P75+P80-all selection "
    "(P75-TIER + P≥80 rel≥80, sorted by proba ↓), model trained before each test year. "
    "Year ROI = Kelly 0.85 × Brier (15% liquidity cap). "
    "1D1P ROI = best HYB proba/day, fixed 1-unit stake. "
    "Full details not published — indicative results, past ≠ future."
)


def _patch_i18n() -> None:
    updates = {
        "fr": {
            "top5.subtitle": TOP5_FR,
            "top5.emptyHint": EMPTY_FR,
            "oneDayOnePick.subtitle": OD1P_FR,
        },
        "en": {
            "top5.subtitle": TOP5_EN,
            "top5.emptyHint": EMPTY_EN,
            "oneDayOnePick.subtitle": OD1P_EN,
        },
    }
    for lang, fields in updates.items():
        path = ROOT / f"frontend/src/i18n/locales/{lang}.json"
        obj = json.loads(path.read_text(encoding="utf-8"))
        for key, val in fields.items():
            section, field = key.split(".", 1)
            obj[section][field] = val
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"patched {path}")


def _replace_in(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"MISSING in {path}: {old[:100]!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"patched {path}")


def _patch_methodo() -> None:
    _replace_in(
        ROOT / "frontend/src/lib/methodoContent.ts",
        "body: 'Top 5 hybride : proba ≥ 77 %, fiabilité ≥ 75, gap ≤ 30 pp, EV tier 1 : 15–30 % puis tier 2 : 30–50 % (max 5/jour), tri EV ↓. 1 Day 1 Pick : rang 1 de cette même sélection. Paris du jour (/jour) reste en value bets EV ≥ 15 %.',",
        f"body: '{METHOD_BODY_FR}',",
    )
    _replace_in(
        ROOT / "frontend/src/lib/methodoContent.ts",
        "Protocole : ATP+WTA, tournois G/M/A, sélection hybride Top 5 (P≥77 %, rel≥75, gap≤30 pp, EV tiers 15–30 / 30–50 %, tri EV ↓), modèle entraîné avant chaque année testée. ROI année = Kelly 0,65 × Brier (cap 15 % liquidité). ROI 1D1P = rang 1 hybride/jour, mise fixe 1 unité. Détails complets non publiés — résultats indicatifs, passé ≠ futur.",
        METHOD_PROTO_FR,
    )
    _replace_in(
        ROOT / "frontend/src/lib/methodoContentEn.ts",
        "body: 'Hybrid Top 5: proba ≥ 77%, reliability ≥ 75, book gap ≤ 30pp, EV tier 1: 15–30% then tier 2: 30–50% (max 5/day), sorted by EV ↓. 1 Day 1 Pick: rank 1 of that same selection. Today\\'s picks (/today) remain value bets EV ≥ 15%.',",
        f"body: '{METHOD_BODY_EN}',",
    )
    _replace_in(
        ROOT / "frontend/src/lib/methodoContentEn.ts",
        "Protocol: ATP+WTA, G/M/A tournaments, hybrid Top 5 selection (P≥77%, rel≥75, gap≤30pp, EV tiers 15–30 / 30–50%, sorted by EV ↓), model trained before each test year. Year ROI = Kelly 0.85 × Brier (15% liquidity cap). 1D1P ROI = hybrid rank 1/day, fixed 1-unit stake. Full details not published — indicative results, past ≠ future.",
        METHOD_PROTO_EN,
    )


def _patch_hybrid_fallback() -> None:
    path = ROOT / "api/services/hybrid_selection_text.py"
    text = path.read_text(encoding="utf-8")
    if "HYB P75+P80" in text and "rank 1" not in text.lower().split("except")[1][:200]:
        print(f"skip {path} (already patched)")
        return
    new = '''"""Description sélection hybride — source de vérité BettingHUD scripts."""
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
'''
    path.write_text(new, encoding="utf-8")
    print(f"patched {path}")


def main() -> None:
    _patch_i18n()
    _patch_methodo()
    _patch_hybrid_fallback()
    print("OK")


if __name__ == "__main__":
    main()
