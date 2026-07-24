#!/usr/bin/env python3
"""Patch built CourtAlpha dist bundle with HYB P75+P80-all UI strings."""
from __future__ import annotations

from pathlib import Path

DIST = Path("/opt/courtalpha/frontend/dist/assets/index-BFK6pEdH.js")

REPLACEMENTS = [
    (
        "P≥77 % · rel≥75 · gap≤30 pp · EV tier1 15–35 % + tier2 30–55 % · tri EV ↓ — majeurs 250+.",
        "HYB P75+P80-all : P75-TIER (p≥73 %, rel≥80, EV 6–55 %, max 6/j) + compléments P≥80 % rel≥80 (EV libre), tri proba ↓ — majeurs 250+.",
    ),
    (
        "Proba ≥77% · rel≥75 · gap≤30pp · EV tier1 15–35% + tier2 30–55% · sorted by EV ↓ — 250+ majors only.",
        "HYB P75+P80-all: P75-TIER (p≥73%, rel≥80, EV 6–55%, max 6/day) plus P≥80% rel≥80 add-ons (any EV), sorted by proba ↓ — 250+ majors only.",
    ),
    (
        "Critères P≥77 %, rel≥75, gap≤30 pp — consulte le Live Tracker ou attends le prochain snapshot.",
        "Critères HYB P75+P80 — consulte le Live Tracker ou attends le prochain snapshot.",
    ),
    (
        "Criteria: proba ≥77%, rel≥75, gap≤30pp — check Live Tracker or wait for the next snapshot.",
        "HYB P75+P80 criteria — check Live Tracker or wait for the next snapshot.",
    ),
    (
        "Un pick par jour : rang 1 de la sélection hybride Top 5 (P≥77 %, rel≥75, gap≤30 pp, EV tier1/tier2, tri EV ↓). BR initiale 100 € ; replay depuis le snapshot live.",
        "Un pick par jour : meilleure proba fav dans HYB P75+P80-all (même union que Top 5). BR initiale 100 € ; replay depuis le snapshot live.",
    ),
    (
        "One pick per day: rank 1 of the hybrid Top 5 selection (proba ≥77%, rel≥75, gap≤30pp, EV tier1/tier2, sorted by EV ↓). 100 € starting BR; replay from the live snapshot.",
        "One pick per day: highest model proba in HYB P75+P80-all (same union as Top 5). 100 € starting BR; replay from the live snapshot.",
    ),
    (
        "Top 5 hybride : proba ≥ 77 %, fiabilité ≥ 75, gap ≤ 30 pp, EV tier 1 : 15–35 % puis tier 2 : 30–55 % (max 5/jour), tri EV ↓. 1 Day 1 Pick : rang 1 de cette même sélection. Paris du jour (/jour) reste en value bets EV ≥ 15 %.",
        "Top 5 : HYB P75+P80-all — P75-TIER (p≥73 %, rel≥80, EV 6–55 %, max 6/j) + compléments P≥80 % rel≥80 (EV libre), tri proba ↓. 1 Day 1 Pick : meilleure proba fav dans cette union. Paris du jour (/jour) reste en value bets EV ≥ 15 %.",
    ),
    (
        "Hybrid Top 5: proba ≥ 77%, reliability ≥ 75, book gap ≤ 30pp, EV tier 1: 15–35% then tier 2: 30–55% (max 5/day), sorted by EV ↓. 1 Day 1 Pick: rank 1 of that same selection. Today's picks (/today) remain value bets EV ≥ 15%.",
        "Top 5: HYB P75+P80-all — P75-TIER (p≥73%, rel≥80, EV 6–55%, max 6/day) plus P≥80% rel≥80 add-ons (any EV), sorted by proba ↓. 1 Day 1 Pick: highest model proba in that union. Today's picks (/today) remain value bets EV ≥ 15%.",
    ),
    (
        "Strategy: hybrid Top 5 (P≥77%, rel≥75, gap≤30pp, EV tiers 15–35 / 30–55%, sorted by EV ↓), majors 250+, Kelly 0.85",
        "Strategy: HYB P75+P80-all (P75-TIER + P≥80 rel≥80, sorted by proba ↓), majors 250+, Kelly 0.85",
    ),
    (
        "sélection hybride Top 5 (P≥77 %, rel≥75, gap≤30 pp, EV tiers 15–35 / 30–55 %, tri EV ↓), modèle entraîné avant chaque année testée. ROI année = Kelly 0,65 × Brier (cap 15 % liquidité). ROI 1D1P = rang 1 hybride/jour, mise fixe 1 unité",
        "sélection HYB P75+P80-all (P75-TIER + P≥80 rel≥80, tri proba ↓), modèle entraîné avant chaque année testée. ROI année = Kelly 0,65 × Brier (cap 15 % liquidité). ROI 1D1P = meilleure proba HYB/jour, mise fixe 1 unité",
    ),
    (
        "hybrid Top 5 selection (P≥77%, rel≥75, gap≤30pp, EV tiers 15–35 / 30–55%, sorted by EV ↓), model trained before each test year. Year ROI = Kelly 0.85 × Brier (15% liquidity cap). 1D1P ROI = hybrid rank 1/day, fixed 1-unit stake",
        "HYB P75+P80-all selection (P75-TIER + P≥80 rel≥80, sorted by proba ↓), model trained before each test year. Year ROI = Kelly 0.85 × Brier (15% liquidity cap). 1D1P ROI = best HYB proba/day, fixed 1-unit stake",
    ),
    (
        "Top 5 hybride · P≥77 % · EV tiers 15–35 / 30–55 %",
        "Top 5 · HYB P75+P80-all",
    ),
]


def main() -> int:
    if not DIST.is_file():
        raise SystemExit(f"missing {DIST}")
    text = DIST.read_text(encoding="utf-8")
    n = 0
    for old, new in REPLACEMENTS:
        if old not in text:
            print(f"WARN missing: {old[:60]}...")
            continue
        text = text.replace(old, new)
        n += 1
        print(f"ok: {old[:50]}...")
    DIST.write_text(text, encoding="utf-8")
    print(f"patched {n} strings in {DIST.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
