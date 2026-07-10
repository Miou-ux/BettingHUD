"""Alias et corrections de noms WTA (delta post-Sackmann)."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.wta_sackmann_common import DEFAULT_CUTOFF, norm_name_key, parse_yyyymmdd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALIASES_PATH = ROOT / "config" / "wta_name_aliases.json"
LEGACY_ALIASES_PATH = ROOT / "data" / "wta_name_aliases.json"


@lru_cache(maxsize=1)
def load_alias_config(path: str | Path | None = None) -> dict[str, Any]:
    if path:
        p = Path(path)
    elif DEFAULT_ALIASES_PATH.is_file():
        p = DEFAULT_ALIASES_PATH
    elif LEGACY_ALIASES_PATH.is_file():
        p = LEGACY_ALIASES_PATH
    else:
        return {"name_aliases": {}, "row_corrections": []}
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    data.setdefault("name_aliases", {})
    data.setdefault("row_corrections", [])
    return data


def canonicalize_wta_display_name(name: object, *, aliases_path: str | Path | None = None) -> str:
    """Applique un alias connu puis retourne le nom affiché (Last I. si possible)."""
    raw = str(name or "").strip()
    if not raw:
        return raw
    cfg = load_alias_config(aliases_path)
    aliases: dict[str, str] = cfg.get("name_aliases") or {}
    nk = norm_name_key(raw)
    target = aliases.get(nk)
    if not target:
        return raw
    # target is usually already "Last I." form
    return str(target).strip() or raw


def canonical_wta_name_key(name: object, *, aliases_path: str | Path | None = None) -> str:
    """norm_name_key après résolution alias."""
    return norm_name_key(canonicalize_wta_display_name(name, aliases_path=aliases_path))


def _row_matches_correction(row: pd.Series, corr: dict[str, Any]) -> bool:
    try:
        td = parse_yyyymmdd(row.get("tourney_date"))
    except Exception:
        td = None
    if corr.get("tourney_date") and td != int(corr["tourney_date"]):
        return False
    wn = str(row.get("winner_name") or "")
    if corr.get("winner_substr") and corr["winner_substr"].lower() not in wn.lower():
        return False
    side = str(corr.get("side") or "loser").lower()
    name_col = "loser_name" if side == "loser" else "winner_name"
    current = str(row.get(name_col) or "")
    wrong = str(corr.get("wrong_name_substr") or "")
    if wrong and wrong.lower() not in current.lower():
        return False
    if corr.get("tourney_name_substr"):
        tn = str(row.get("tourney_name") or "")
        if corr["tourney_name_substr"].lower() not in tn.lower():
            return False
    return True


def apply_aliases_to_dataframe(df: pd.DataFrame, *, aliases_path: str | Path | None = None) -> int:
    """Corrige winner_name/loser_name in-place. Retourne le nombre de cellules modifiées."""
    if df.empty:
        return 0
    cfg = load_alias_config(aliases_path)
    changed = 0
    for col in ("winner_name", "loser_name"):
        if col not in df.columns:
            continue
        for i, val in df[col].items():
            new = canonicalize_wta_display_name(val, aliases_path=aliases_path)
            if new and new != str(val or "").strip():
                df.at[i, col] = new
                changed += 1
    for corr in cfg.get("row_corrections") or []:
        correct = str(corr.get("correct_name") or "").strip()
        if not correct:
            continue
        side = str(corr.get("side") or "loser").lower()
        name_col = "loser_name" if side == "loser" else "winner_name"
        for i, row in df.iterrows():
            if not _row_matches_correction(row, corr):
                continue
            if str(row.get(name_col) or "").strip() != correct:
                df.at[i, name_col] = correct
                changed += 1
    return changed


def apply_aliases_to_work_dir(
    work_dir: Path | str,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    aliases_path: str | Path | None = None,
) -> dict[str, Any]:
    work = Path(work_dir)
    stats: dict[str, Any] = {"files": 0, "cells_changed": 0, "cutoff": cutoff}
    if not work.is_dir():
        stats["message"] = "work_dir missing"
        return stats
    for p in sorted(work.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        df = pd.read_csv(p, low_memory=False)
        if df.empty:
            continue
        n = apply_aliases_to_dataframe(df, aliases_path=aliases_path)
        if n:
            df.to_csv(p, index=False)
            stats["files"] += 1
            stats["cells_changed"] += n
    stats["message"] = "ok"
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Applique alias/corrections noms WTA sur work-dir CSV.")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "raw" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--aliases-file", default=str(DEFAULT_ALIASES_PATH))
    args = ap.parse_args(argv)
    stats = apply_aliases_to_work_dir(
        Path(args.work_dir),
        cutoff=args.cutoff_date,
        aliases_path=args.aliases_file,
    )
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
