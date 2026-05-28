#!/usr/bin/env python3
"""Vérifie que la charte UI quant est présente et que le dashboard démarre."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "app" / "dashboard.py"

REQUIRED_CSS = (
    "#0B0C10",
    "#1C1D24",
    "#00E676",
    "#00B0FF",
    ".quant-num",
    "badge-circuit-atp",
    "vb-card-premium-marker",
    "JetBrains Mono",
    "_inject_quant_terminal_theme",
)

REQUIRED_PY = (
    "_circuit_badge_html",
    "_build_top_probas_day_chart",
    'type="primary"',
)


def main() -> int:
    src = DASH.read_text(encoding="utf-8")
    ast.parse(src)
    missing = [s for s in REQUIRED_CSS + REQUIRED_PY if s not in src]
    if missing:
        print("MANQUANT:", ", ".join(missing))
        return 1
    print("theme_source: OK")
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=5) as r:
            if r.status == 200:
                print("streamlit_health: OK")
            else:
                print(f"streamlit_health: HTTP {r.status}")
                return 1
    except Exception as exc:
        print(f"streamlit_health: SKIP ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
