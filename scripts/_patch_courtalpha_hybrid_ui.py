"""One-shot patch CourtAlpha Top5/1D1P UI text for P77 hybrid rules."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path("/opt/courtalpha")


def _patch_file(path: Path, replacements: list[tuple[str, str]]) -> None:
    text = path.read_text(encoding="utf-8")
    orig = text
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"MISSING in {path}: {old[:80]!r}...")
        text = text.replace(old, new)
    if text != orig:
        path.write_text(text, encoding="utf-8")
        print(f"patched {path}")


def patch_top5_page() -> None:
    path = ROOT / "frontend/src/pages/Top5Page.tsx"
    text = path.read_text(encoding="utf-8")
    if "useTranslation" not in text:
        text = text.replace(
            "import { useState } from 'react'\n",
            "import { useState } from 'react'\nimport { useTranslation } from 'react-i18next'\n",
        )
    text = text.replace(
        "export function Top5Page({ picks, pool, loading, onBetSuccess }: Top5PageProps) {\n"
        "  const { user } = useAuth()\n",
        "export function Top5Page({ picks, pool, loading, onBetSuccess }: Top5PageProps) {\n"
        "  const { t } = useTranslation()\n"
        "  const { user } = useAuth()\n",
    )
    text = text.replace(
        '        kicker="Best pick"\n'
        '        title="Top 5 proba"\n'
        '        subtitle="Bande EV 15–100 % · tri proba modèle décroissante."\n',
        '        kicker={t(\'top5.kicker\')}\n'
        '        title={t(\'top5.title\')}\n'
        '        subtitle={t(\'top5.subtitle\')}\n',
    )
    text = text.replace(
        '        <EmptyState title="Aucun pick dans la bande EV 15–100 %" hint="Les meilleurs picks du jour peuvent avoir une EV > 100 % (hors bande Top 5)." />',
        '        <EmptyState title={t(\'top5.emptyTitle\')} hint={t(\'top5.emptyHint\')} />',
    )
    path.write_text(text, encoding="utf-8")
    print(f"patched {path}")


def patch_i18n() -> None:
    for lang, data in {
        "fr": {
            "top5.title": "Top 5 hybride",
            "top5.subtitle": "P≥77 % · rel≥75 · gap≤30 pp · EV tier1 15–35 % + tier2 30–55 % · tri EV ↓ — majeurs 250+.",
            "top5.emptyTitle": "Aucun pick hybride éligible",
            "top5.emptyHint": "Critères P≥77 %, rel≥75, gap≤30 pp — consulte le Live Tracker ou attends le prochain snapshot.",
            "oneDayOnePick.subtitle": (
                "Un pick par jour : rang 1 de la sélection hybride Top 5 "
                "(P≥77 %, rel≥75, gap≤30 pp, EV tier1/tier2, tri EV ↓). "
                "BR initiale 100 € ; replay depuis le snapshot live."
            ),
        },
        "en": {
            "top5.title": "Hybrid Top 5",
            "top5.subtitle": "Proba ≥77% · rel≥75 · gap≤30pp · EV tier1 15–35% + tier2 30–55% · sorted by EV ↓ — 250+ majors only.",
            "top5.emptyTitle": "No eligible hybrid picks",
            "top5.emptyHint": "Criteria: proba ≥77%, rel≥75, gap≤30pp — check Live Tracker or wait for the next snapshot.",
            "oneDayOnePick.subtitle": (
                "One pick per day: rank 1 of the hybrid Top 5 selection "
                "(proba ≥77%, rel≥75, gap≤30pp, EV tier1/tier2, sorted by EV ↓). "
                "100 € starting BR; replay from the live snapshot."
            ),
        },
    }.items():
        path = ROOT / f"frontend/src/i18n/locales/{lang}.json"
        obj = json.loads(path.read_text(encoding="utf-8"))
        for key, val in data.items():
            section, field = key.split(".", 1)
            obj[section][field] = val
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"patched {path}")


def patch_api() -> None:
    for rel, rank1 in (
        ("api/services/top5_replay.py", False),
        ("api/services/one_day_one_pick.py", True),
    ):
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        if "hybrid_criteria_plain" not in text:
            text = text.replace(
                "from __future__ import annotations\n\n",
                "from __future__ import annotations\n\n"
                "def _hybrid_selection_description(*, rank1: bool = False) -> str:\n"
                "    from scripts.hybrid_pick_selection import hybrid_criteria_plain\n"
                "    return hybrid_criteria_plain(english=True, rank1=rank1)\n\n",
                1,
            )
        text = re.sub(
            r'"description": \(\s*"[^"]*(?:\n\s*"[^"]*)*"\s*\),',
            '"description": _hybrid_selection_description(rank1=%s),' % ("True" if rank1 else "False"),
            text,
            count=1,
        )
        path.write_text(text, encoding="utf-8")
        print(f"patched {path}")


def patch_methodo() -> None:
    _patch_file(
        ROOT / "frontend/src/lib/methodoContent.ts",
        [
            (
                "body: 'Top 5 hybride : proba ≥ 80 %, EV tier 1 : 15–30 % puis tier 2 : 30–50 % (max 5/jour), tri proba ↓. 1 Day 1 Pick : rang 1 de cette même sélection. Paris du jour (/jour) reste en value bets EV ≥ 15 %.',",
                "body: 'Top 5 hybride : proba ≥ 77 %, fiabilité ≥ 75, gap ≤ 30 pp, EV tier 1 : 15–35 % puis tier 2 : 30–55 % (max 5/jour), tri EV ↓. 1 Day 1 Pick : rang 1 de cette même sélection. Paris du jour (/jour) reste en value bets EV ≥ 15 %.',",
            ),
            (
                "Protocole : ATP+WTA, tournois G/M/A, sélection hybride Top 5 (P≥80 %, EV tiers 15–30 / 30–50 %),",
                "Protocole : ATP+WTA, tournois G/M/A, sélection hybride Top 5 (P≥77 %, rel≥75, gap≤30 pp, EV tiers 15–35 / 30–55 %, tri EV ↓),",
            ),
        ],
    )
    _patch_file(
        ROOT / "frontend/src/lib/methodoContentEn.ts",
        [
            (
                "body: 'Hybrid Top 5: proba ≥ 80%, EV tier 1: 15–30% then tier 2: 30–50% (max 5/day), sorted by proba ↓. 1 Day 1 Pick: rank 1 of that same selection. Today\\'s picks (/today) remain value bets EV ≥ 15%.',",
                "body: 'Hybrid Top 5: proba ≥ 77%, reliability ≥ 75, book gap ≤ 30pp, EV tier 1: 15–35% then tier 2: 30–55% (max 5/day), sorted by EV ↓. 1 Day 1 Pick: rank 1 of that same selection. Today\\'s picks (/today) remain value bets EV ≥ 15%.',",
            ),
            (
                "Protocol: ATP+WTA, G/M/A tournaments, hybrid Top 5 selection (P≥80%, EV tiers 15–30 / 30–50%),",
                "Protocol: ATP+WTA, G/M/A tournaments, hybrid Top 5 selection (P≥77%, rel≥75, gap≤30pp, EV tiers 15–35 / 30–55%, sorted by EV ↓),",
            ),
        ],
    )
    _patch_file(
        ROOT / "frontend/src/pages/MethodoPage.tsx",
        [
            ('<Badge tone="default">Top 5 proba · EV 15–100 %</Badge>', '<Badge tone="default">Top 5 hybride · P≥77 %</Badge>'),
        ],
    )
    _patch_file(
        ROOT / "frontend/src/lib/seoData.ts",
        [
            (
                "body: 'Parmi les matchs majeurs du jour, on retient le favori modèle le plus confiant (proba max) dont l’EV est dans la bande 15–100 %. Un seul pick par jour calendaire, publié en replay transparent.',",
                "body: 'Un pick par jour : rang 1 de la sélection hybride Top 5 (P≥77 %, rel≥75, gap≤30 pp, EV tier1/tier2, tri EV ↓). Replay public transparent.',",
            ),
        ],
    )


def main() -> None:
    patch_top5_page()
    patch_i18n()
    patch_api()
    patch_methodo()
    print("OK")


if __name__ == "__main__":
    main()
