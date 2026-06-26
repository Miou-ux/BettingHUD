#!/usr/bin/env python3
"""Remap synthetic WTA player ids (>=900000) to known Sackmann ids by name."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.wta_sackmann_common import build_name_to_player_id  # noqa: E402
from scripts.player_identity import canonical_name, to_lastname_initial  # noqa: E402


def _nk(name: object) -> str:
    raw = str(name or "").strip()
    return canonical_name(to_lastname_initial(raw)) or str(name or "").strip().lower()


def main() -> int:
    raw = ROOT / "data" / "raw" / "tennis_wta"
    path = raw / "wta_matches_2026.csv"
    df = pd.read_csv(path, low_memory=False)
    name_to_id = build_name_to_player_id(df)
    changed = 0
    for col_w, col_l in (("winner_name", "winner_id"), ("loser_name", "loser_id")):
        for i, row in df.iterrows():
            try:
                pid = int(float(row[col_l]))
            except (TypeError, ValueError):
                continue
            if pid < 900000:
                continue
            nk = _nk(row[col_w])
            canon = name_to_id.get(nk)
            if canon and canon < 900000:
                df.at[i, col_l] = canon
                changed += 1
    df.to_csv(path, index=False)
    print(f"remapped_cells={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
