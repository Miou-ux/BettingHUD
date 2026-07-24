#!/usr/bin/env python3
"""Patch CourtAlpha UI/API defaults for prod Kelly fraction (run on prod)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path("/opt/courtalpha")
KELLY = 0.85
KELLY_FR = "0,85"
KELLY_EN = "0.85"

REPLACEMENTS: list[tuple[Path, list[tuple[str, str]]]] = [
    (
        ROOT / "frontend/src/i18n/locales/fr.json",
        [
            ("Kelly ½ × Brier", f"Kelly {KELLY_FR} × Brier"),
            ("Kelly 0,65 × Brier", f"Kelly {KELLY_FR} × Brier"),
        ],
    ),
    (
        ROOT / "frontend/src/i18n/locales/en.json",
        [
            ("Kelly ½ × Brier", f"Kelly {KELLY_EN} × Brier"),
            ("Kelly 0.65 × Brier", f"Kelly {KELLY_EN} × Brier"),
        ],
    ),
    (
        ROOT / "frontend/src/lib/liveMetrics.ts",
        [
            ("const KELLY_BASE = 0.5", f"const KELLY_BASE = {KELLY}"),
            ("const KELLY_BASE = 0.65", f"const KELLY_BASE = {KELLY}"),
        ],
    ),
    (
        ROOT / "frontend/src/components/ValueBetCard.tsx",
        [
            ("Kelly ½ × Brier", f"Kelly {KELLY_EN} × Brier"),
            ("Kelly 0.65 × Brier", f"Kelly {KELLY_EN} × Brier"),
        ],
    ),
    (
        ROOT / "frontend/src/pages/BacktestPage.tsx",
        [
            ("useState(0.5)", f"useState({KELLY})"),
            ("useState(0.65)", f"useState({KELLY})"),
        ],
    ),
    (
        ROOT / "api/routes/backtest.py",
        [
            ("kelly_multiplier: float = Field(0.5,", f"kelly_multiplier: float = Field({KELLY},"),
            ("kelly_multiplier: float = Field(0.65,", f"kelly_multiplier: float = Field({KELLY},"),
            ("adaptive_kelly_base_fraction: float = Field(0.5,", f"adaptive_kelly_base_fraction: float = Field({KELLY},"),
            ("adaptive_kelly_base_fraction: float = Field(0.65,", f"adaptive_kelly_base_fraction: float = Field({KELLY},"),
        ],
    ),
    (
        ROOT / "docs/API.md",
        [
            ('"kelly_multiplier": 0.5,', f'"kelly_multiplier": {KELLY},'),
            ('"kelly_multiplier": 0.65,', f'"kelly_multiplier": {KELLY},'),
        ],
    ),
]


def main() -> int:
    for path, pairs in REPLACEMENTS:
        if not path.is_file():
            print(f"SKIP missing {path}")
            continue
        text = path.read_text(encoding="utf-8")
        orig = text
        for old, new in pairs:
            if old not in text:
                print(f"WARN {path}: not found: {old!r}")
            text = text.replace(old, new)
        if text != orig:
            path.write_text(text, encoding="utf-8")
            print(f"OK {path}")
        else:
            print(f"UNCHANGED {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
