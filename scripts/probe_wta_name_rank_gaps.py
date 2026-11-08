#!/usr/bin/env python3
"""Trouve noms WTA delta sans rang résolu — candidats alias P0-A.

Usage:
  py -3 scripts/probe_wta_name_rank_gaps.py --work-dir data/raw/tennis_wta
  py -3 scripts/probe_wta_name_rank_gaps.py --work-dir data/raw/tennis_wta --apply-top 5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.wta_name_aliases import (  # noqa: E402
    DEFAULT_ALIASES_PATH,
    canonical_wta_name_key,
    load_alias_config,
)
from scripts.wta_sackmann_common import (  # noqa: E402
    DEFAULT_CUTOFF,
    _rank_val_ok,
    build_name_to_player_id,
    build_rank_history,
    build_te_cache_rank_maps,
    build_wta_players_last_index,
    fill_ranks_if_missing,
    load_current_rankings_map,
    norm_name_key,
    parse_yyyymmdd,
    te_name_key_safe,
)


def _load_post_cutoff_rows(work_dir: Path, cutoff: int) -> pd.DataFrame:
    frames = []
    for p in sorted(work_dir.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        try:
            df = pd.read_csv(p, low_memory=False)
        except Exception:
            continue
        if df.empty:
            continue
        df["_td"] = df["tourney_date"].apply(parse_yyyymmdd)
        df = df[df["_td"] >= cutoff]
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _player_display_names(work_dir: Path) -> dict[str, str]:
    """norm_key -> 'Last I.' depuis wta_players.csv."""
    path = work_dir / "wta_players.csv"
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    pdf = pd.read_csv(path, low_memory=False)
    for _, row in pdf.iterrows():
        ln = str(row.get("last_name") or "").strip()
        fn = str(row.get("first_name") or "").strip()
        if not ln:
            continue
        ini = (fn[:1] + ".") if fn else ""
        disp = f"{ln} {ini}".strip()
        nk = norm_name_key(disp)
        if nk and nk not in out:
            out[nk] = disp
        nk2 = norm_name_key(ln)
        if nk2 and nk2 not in out:
            out[nk2] = disp
    return out


def _te_name_ranks() -> dict[str, tuple[float, float]]:
    _, by_name = build_te_cache_rank_maps()
    return by_name


def probe_gaps(
    work_dir: Path,
    *,
    cutoff: int = DEFAULT_CUTOFF,
    db_path: str | None = None,
    min_occurrences: int = 2,
) -> dict:
    work_dir = Path(work_dir)
    post = _load_post_cutoff_rows(work_dir, cutoff)
    if post.empty:
        return {"error": "no post-cutoff rows", "cutoff": cutoff}

    all_paths = [p for p in work_dir.glob("wta_matches*.csv") if "doubles" not in p.name.lower()]
    all_df = pd.concat([pd.read_csv(p, low_memory=False) for p in all_paths], ignore_index=True)
    name_to_id = build_name_to_player_id(all_df)
    rank_history = build_rank_history(all_df)
    current_rankings = load_current_rankings_map(work_dir, db_path=db_path)
    te_by_pid, te_by_name = build_te_cache_rank_maps(work_dir, name_to_id)
    wta_last_index = build_wta_players_last_index(work_dir)
    player_names = _player_display_names(work_dir)
    te_ranks = _te_name_ranks()
    cfg = load_alias_config()
    existing = set((cfg.get("name_aliases") or {}).keys())

    missing_names: Counter[str] = Counter()
    suggestions: list[dict] = []

    for _, row in post.iterrows():
        td = parse_yyyymmdd(row.get("tourney_date"))
        if td is None:
            continue
        for side in ("winner", "loser"):
            rank_col = f"{side}_rank"
            if _rank_val_ok(row.get(rank_col)):
                continue
            raw = str(row.get(f"{side}_name") or "").strip()
            if not raw:
                continue
            nk = norm_name_key(raw)
            cnk = canonical_wta_name_key(raw)
            if cnk in name_to_id and cnk != nk:
                missing_names[nk] += 1
                continue
            rec = {c: row.get(c) for c in post.columns if not str(c).startswith("_")}
            filled = fill_ranks_if_missing(
                rec,
                tourney_date=td,
                rank_history=rank_history,
                current_rankings=current_rankings,
                name_to_id=name_to_id,
                te_rank_by_pid=te_by_pid,
                te_rank_by_name=te_by_name,
                wta_players_last_index=wta_last_index,
            )
            if filled > 0 and _rank_val_ok(rec.get(rank_col)):
                continue
            missing_names[nk] += 1

    for nk, cnt in missing_names.most_common():
        if cnt < min_occurrences:
            continue
        if nk in existing:
            continue
        # Last-token match against known players
        parts = nk.split()
        last = parts[-1] if parts else nk
        candidates: list[tuple[str, str, str]] = []
        for pk, disp in player_names.items():
            if last and last in pk.split():
                candidates.append((pk, disp, "players_csv"))
        for tk, (rk, _) in te_ranks.items():
            if not te_name_key_safe(tk):
                continue
            if last and last in tk.split():
                disp = player_names.get(tk) or tk
                candidates.append((tk, disp, f"te_rank={int(rk)}"))
        if not candidates:
            continue
        # Prefer players_csv over TE; shortest key / exact last name
        candidates.sort(
            key=lambda x: (0 if x[2] == "players_csv" else 1, len(x[0].split()), x[0])
        )
        best_pk, best_disp, reason = candidates[0]
        if reason != "players_csv":
            continue
        if norm_name_key(best_disp) == nk:
            continue
        if not te_name_key_safe(norm_name_key(best_disp)):
            continue
        suggestions.append(
            {
                "wrong_key": nk,
                "suggested_display": best_disp,
                "suggested_key": norm_name_key(best_disp),
                "occurrences": int(cnt),
                "reason": reason,
            }
        )

    both = int(
        (
            post["winner_rank"].apply(_rank_val_ok).astype(bool)
            & post["loser_rank"].apply(_rank_val_ok).astype(bool)
        ).sum()
    )
    return {
        "cutoff": cutoff,
        "post_cutoff_rows": int(len(post)),
        "post_cutoff_both_ranks": both,
        "rank_coverage_pct": round(100.0 * both / len(post), 1) if len(post) else 0.0,
        "missing_name_keys": len(missing_names),
        "suggestions": suggestions[:40],
        "proposed_aliases": {
            s["wrong_key"]: s["suggested_display"]
            for s in suggestions
            if s["wrong_key"] != s["suggested_key"]
        },
    }


def apply_aliases(path: Path, new_aliases: dict[str, str]) -> int:
    cfg = load_alias_config(path)
    merged = dict(cfg.get("name_aliases") or {})
    added = 0
    for k, v in new_aliases.items():
        if k not in merged:
            merged[k] = v
            added += 1
    cfg["name_aliases"] = merged
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe alias/rank gaps WTA delta.")
    ap.add_argument("--work-dir", default=str(ROOT / "data" / "raw" / "tennis_wta"))
    ap.add_argument("--cutoff-date", type=int, default=DEFAULT_CUTOFF)
    ap.add_argument("--db-path", default=str(ROOT / "data" / "bettinghud.db"))
    ap.add_argument("--apply-top", type=int, default=0, help="Ajoute les N meilleurs alias au JSON")
    ap.add_argument(
        "--apply-all-safe",
        action="store_true",
        help="Ajoute tous les alias sûrs (players_csv uniquement)",
    )
    ap.add_argument("--aliases-file", default=str(DEFAULT_ALIASES_PATH))
    args = ap.parse_args(argv)

    out = probe_gaps(
        Path(args.work_dir),
        cutoff=int(args.cutoff_date),
        db_path=args.db_path if args.db_path else None,
    )
    if out.get("error"):
        print(f"ERREUR: {out['error']}", file=sys.stderr)
        return 1

    print(
        f"post_cutoff={out['post_cutoff_rows']} "
        f"both_ranks={out['post_cutoff_both_ranks']} ({out['rank_coverage_pct']}%) "
        f"missing_keys={out['missing_name_keys']}"
    )
    for s in out.get("suggestions") or []:
        print(
            f"  {s['wrong_key']:28} -> {s['suggested_display']:20} "
            f"(n={s['occurrences']}, {s['reason']})"
        )

    if args.apply_all_safe and out.get("proposed_aliases"):
        n = apply_aliases(Path(args.aliases_file), dict(out["proposed_aliases"]))
        print(f"\n[apply-all-safe] +{n} alias dans {args.aliases_file}")
    elif int(args.apply_top) > 0 and out.get("proposed_aliases"):
        top = dict(list(out["proposed_aliases"].items())[: int(args.apply_top)])
        n = apply_aliases(Path(args.aliases_file), top)
        print(f"\n[apply] +{n} alias dans {args.aliases_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
