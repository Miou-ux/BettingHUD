"""Alias et corrections de noms WTA (delta post-Sackmann)."""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.wta_sackmann_common import DEFAULT_CUTOFF, norm_name_key, parse_yyyymmdd
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


def _resolve_canonical_player_id(
    name: object,
    *,
    name_to_id: dict[str, int],
    aliases_path: str | Path | None = None,
) -> int | None:
    """ID canonique pour un nom affiché (après alias), ou None si inconnu."""
    nk = canonical_wta_name_key(name, aliases_path=aliases_path)
    if not nk:
        return None
    pid = name_to_id.get(nk)
    return int(pid) if pid is not None else None


def apply_aliases_to_dataframe(
    df: pd.DataFrame,
    *,
    aliases_path: str | Path | None = None,
    name_to_id: dict[str, int] | None = None,
) -> int:
    """Corrige winner_name/loser_name (+ IDs si ``name_to_id``) in-place.

    Retourne le nombre de cellules modifiées (noms + IDs).
    """
    if df.empty:
        return 0
    cfg = load_alias_config(aliases_path)
    changed = 0
    id_map = dict(name_to_id or {})

    for col in ("winner_name", "loser_name"):
        if col not in df.columns:
            continue
        id_col = "winner_id" if col == "winner_name" else "loser_id"
        for i, val in df[col].items():
            old = str(val or "").strip()
            new = canonicalize_wta_display_name(val, aliases_path=aliases_path)
            name_changed = bool(new and new != old)
            if name_changed:
                df.at[i, col] = new
                changed += 1
            display = new if name_changed else old
            if not id_map or id_col not in df.columns:
                continue
            target_id = _resolve_canonical_player_id(
                display, name_to_id=id_map, aliases_path=aliases_path
            )
            if target_id is None:
                continue
            try:
                cur_id = int(float(df.at[i, id_col]))
            except (TypeError, ValueError):
                cur_id = None
            # Remap si nom aliasé, ou si ID courant est synthétique / divergent.
            if name_changed or cur_id is None or cur_id >= 900000 or cur_id != target_id:
                if cur_id != target_id:
                    df.at[i, id_col] = target_id
                    changed += 1

    for corr in cfg.get("row_corrections") or []:
        correct = str(corr.get("correct_name") or "").strip()
        if not correct:
            continue
        side = str(corr.get("side") or "loser").lower()
        name_col = "loser_name" if side == "loser" else "winner_name"
        id_col = "loser_id" if side == "loser" else "winner_id"
        for i, row in df.iterrows():
            if not _row_matches_correction(row, corr):
                continue
            if str(row.get(name_col) or "").strip() != correct:
                df.at[i, name_col] = correct
                changed += 1
            if not id_map or id_col not in df.columns:
                continue
            target_id = _resolve_canonical_player_id(
                correct, name_to_id=id_map, aliases_path=aliases_path
            )
            if target_id is None:
                continue
            try:
                cur_id = int(float(df.at[i, id_col]))
            except (TypeError, ValueError):
                cur_id = None
            if cur_id != target_id:
                df.at[i, id_col] = target_id
                changed += 1
    return changed


def apply_aliases_to_work_dir(
    work_dir: Path | str,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    aliases_path: str | Path | None = None,
) -> dict[str, Any]:
    from scripts.wta_sackmann_common import build_name_to_player_id

    work = Path(work_dir)
    stats: dict[str, Any] = {"files": 0, "cells_changed": 0, "cutoff": cutoff, "ids_remapped": 0}
    if not work.is_dir():
        stats["message"] = "work_dir missing"
        return stats

    # Prefere l'ID le plus bas par nom (Sackmann) — construit AVANT rename Quevedo→Lys
    # pour que « lys e. » pointe déjà vers 220332 si présent dans le socle.
    frames: list[pd.DataFrame] = []
    paths: list[Path] = []
    for p in sorted(work.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        try:
            df = pd.read_csv(p, low_memory=False)
        except Exception:
            continue
        if df.empty:
            continue
        paths.append(p)
        frames.append(df)

    name_to_id: dict[str, int] = {}
    if frames:
        name_to_id = build_name_to_player_id(pd.concat(frames, ignore_index=True))

    ids_remapped = 0
    for p, df in zip(paths, frames):
        id_before = None
        if {"winner_id", "loser_id"} <= set(df.columns):
            id_before = df[["winner_id", "loser_id"]].copy()
        n = apply_aliases_to_dataframe(df, aliases_path=aliases_path, name_to_id=name_to_id)
        if not n:
            continue
        if id_before is not None:
            for col in ("winner_id", "loser_id"):
                ids_remapped += int((id_before[col].astype(str) != df[col].astype(str)).sum())
        df.to_csv(p, index=False)
        stats["files"] += 1
        stats["cells_changed"] += n
    stats["ids_remapped"] = ids_remapped
    stats["message"] = "ok"
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    os.chdir(ROOT)
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
