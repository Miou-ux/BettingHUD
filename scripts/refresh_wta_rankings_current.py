#!/usr/bin/env python3
"""Rafraîchit ``wta_rankings_current.csv`` sans dépendre du snapshot Sackmann figé.

Sources (par priorité de fraîcheur par joueuse) :
  1. Dernier rang/points observé dans les CSV ``wta_matches*.csv`` (delta tennis-data).
  2. Cache profil Tennis Explorer (``data/cache/player_*.json``) si plus récent ou absent en (1).

Le fichier produit reste compatible ``ingest_rankings_current.py`` (colonnes Sackmann).

Usage :
  python scripts/refresh_wta_rankings_current.py
  python scripts/refresh_wta_rankings_current.py --work-dir data/raw/tennis_wta --ingest
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from scripts.wta_sackmann_common import build_name_to_player_id, parse_yyyymmdd  # noqa: E402


def _rank_val_ok(val: object) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    s = str(val).strip()
    return bool(s) and s.lower() not in {"nan", "none"}


def _build_rank_history(matches_df: pd.DataFrame) -> dict[int, list[tuple[int, float, float]]]:
    out: dict[int, list[tuple[int, float, float]]] = {}
    if matches_df.empty:
        return out
    for _, row in matches_df.iterrows():
        td = parse_yyyymmdd(row.get("tourney_date"))
        if td is None:
            continue
        for pid_col, rank_col, pts_col in (
            ("winner_id", "winner_rank", "winner_rank_points"),
            ("loser_id", "loser_rank", "loser_rank_points"),
        ):
            try:
                pid = int(float(row.get(pid_col)))
            except (TypeError, ValueError):
                continue
            if pid >= 900000:
                continue
            if not _rank_val_ok(row.get(rank_col)):
                continue
            try:
                rank_f = float(row.get(rank_col))
                pts_f = float(row.get(pts_col)) if _rank_val_ok(row.get(pts_col)) else float("nan")
            except (TypeError, ValueError):
                continue
            out.setdefault(pid, []).append((td, rank_f, pts_f))
    for pid in out:
        out[pid].sort(key=lambda x: x[0])
    return out

CACHE_DIR = ROOT / "data" / "cache"
DEFAULT_WORK = ROOT / "data" / "raw" / "tennis_wta"
OUT_NAME = "wta_rankings_current.csv"
SOURCE_MATCHES = "wta_matches_derived"
SOURCE_TE = "tennisexplorer_profile"


def _yyyymmdd_to_iso(td: int) -> str:
    y, mo, d = td // 10000, (td // 100) % 100, td % 100
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _iso_to_yyyymmdd(iso: str) -> int | None:
    s = str(iso or "")[:10].strip()
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.year * 10000 + dt.month * 100 + dt.day
    except ValueError:
        return None


def _load_match_frames(work_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for p in sorted(work_dir.glob("wta_matches*.csv")):
        if "doubles" in p.name.lower():
            continue
        try:
            frames.append(pd.read_csv(p, low_memory=False))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def ranks_from_matches(matches_df: pd.DataFrame) -> dict[int, dict]:
    """player_id -> {rank, points, ranking_date_iso, source}."""
    history = _build_rank_history(matches_df)
    out: dict[int, dict] = {}
    for pid, recs in history.items():
        if not recs:
            continue
        td, rank, pts = recs[-1]
        if rank is None:
            continue
        try:
            rank_i = int(float(rank))
            pts_f = float(pts) if pts is not None and not (isinstance(pts, float) and pd.isna(pts)) else float("nan")
        except (TypeError, ValueError):
            continue
        if rank_i >= 1500 or (pts_f == pts_f and pts_f < 10):
            continue
        out[int(pid)] = {
            "rank": rank_i,
            "points": pts_f if pts_f == pts_f else 0.0,
            "ranking_date_iso": _yyyymmdd_to_iso(int(td)),
            "source": SOURCE_MATCHES,
        }
    return out


def _slug_from_cache_path(path: Path) -> str:
    name = path.stem
    if name.startswith("player_"):
        return name[7:]
    return name


def ranks_from_te_cache(
    name_to_pid: dict[str, int],
    *,
    existing: dict[int, dict],
    work_dir: Path,
) -> dict[int, dict]:
    """Complète avec les profils TE en cache (nom → player_id)."""
    if not CACHE_DIR.is_dir():
        return {}

    # wta_players.csv : first_name + last_name → player_id
    players_path = work_dir / "wta_players.csv"
    if players_path.is_file():
        try:
            pdf = pd.read_csv(players_path, low_memory=False)
            for _, row in pdf.iterrows():
                fn = str(row.get("first_name") or "").strip()
                ln = str(row.get("last_name") or "").strip()
                try:
                    pid = int(float(row.get("player_id")))
                except (TypeError, ValueError):
                    continue
                from scripts.player_identity import canonical_name, to_lastname_initial

                for nm in (f"{fn} {ln}".strip(), ln):
                    nk = canonical_name(to_lastname_initial(nm)) or nm.lower()
                    if nk:
                        name_to_pid.setdefault(nk, pid)
        except Exception:
            pass

    added: dict[int, dict] = {}
    for path in CACHE_DIR.glob("player_*.json"):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        rank = data.get("rank")
        if rank is None:
            continue
        try:
            rank_i = int(rank)
        except (TypeError, ValueError):
            continue
        if rank_i <= 0 or rank_i >= 1500:
            continue
        ref_iso = str(data.get("te_last_match_date_iso") or data.get("last_update") or "")[:10]
        if not ref_iso or len(ref_iso) < 10:
            ref_iso = datetime.now(timezone.utc).date().isoformat()
        slug = _slug_from_cache_path(path)
        norm_slug = re.sub(r"[^a-z0-9]+", "", slug.lower())
        pid = None
        for norm, p in name_to_pid.items():
            if norm_slug and (norm_slug in norm or norm in norm_slug):
                pid = p
                break
        if pid is None:
            continue
        prev = existing.get(pid)
        if prev:
            try:
                if str(prev.get("ranking_date_iso") or "")[:10] >= ref_iso[:10]:
                    continue
            except Exception:
                pass
        added[int(pid)] = {
            "rank": rank_i,
            "points": float(prev.get("points", 0.0) if prev else 0.0),
            "ranking_date_iso": ref_iso[:10],
            "source": SOURCE_TE,
        }
    return added


def refresh_rankings_csv(
    work_dir: Path | str | None = None,
    *,
    use_te_cache: bool = True,
    min_date_yyyymmdd: int | None = None,
) -> pd.DataFrame:
    work = Path(work_dir or DEFAULT_WORK)
    matches_df = _load_match_frames(work)
    derived = ranks_from_matches(matches_df)
    if min_date_yyyymmdd is not None:
        derived = {
            pid: row
            for pid, row in derived.items()
            if _iso_to_yyyymmdd(row.get("ranking_date_iso", "")) is not None
            and _iso_to_yyyymmdd(row["ranking_date_iso"]) >= int(min_date_yyyymmdd)
        }

    if use_te_cache and not matches_df.empty:
        name_to_pid = build_name_to_player_id(matches_df)
        te_extra = ranks_from_te_cache(name_to_pid, existing=derived, work_dir=work)
        for pid, row in te_extra.items():
            derived[pid] = row

    if not derived:
        return pd.DataFrame(columns=["ranking_date", "rank", "player_id", "points"])

    rows = []
    for pid, row in sorted(derived.items(), key=lambda x: x[1]["rank"]):
        iso = row["ranking_date_iso"]
        ymd = _iso_to_yyyymmdd(iso) or int(datetime.now(timezone.utc).strftime("%Y%m%d"))
        rows.append(
            {
                "ranking_date": ymd,
                "rank": int(row["rank"]),
                "player_id": int(pid),
                "points": float(row.get("points") or 0.0),
            }
        )
    out = pd.DataFrame(rows)
    out_path = work / OUT_NAME
    work.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".csv.part")
    out.to_csv(tmp, index=False)
    tmp.replace(out_path)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rafraîchit wta_rankings_current.csv (hors Sackmann GitHub)")
    ap.add_argument("--work-dir", default=str(DEFAULT_WORK))
    ap.add_argument("--no-te-cache", action="store_true", help="Ne pas compléter via cache profils TE")
    ap.add_argument("--ingest", action="store_true", help="Ingérer dans SQLite après écriture CSV")
    ap.add_argument("--min-date", default="", help="YYYYMMDD — ignorer matchs antérieurs (défaut: toutes)")
    args = ap.parse_args(argv)

    min_d = None
    if str(args.min_date).strip():
        try:
            min_d = int(str(args.min_date).strip())
        except ValueError:
            print("min-date invalide", file=sys.stderr)
            return 2

    df = refresh_rankings_csv(
        args.work_dir,
        use_te_cache=not args.no_te_cache,
        min_date_yyyymmdd=min_d,
    )
    out_path = Path(args.work_dir) / OUT_NAME
    if df.empty:
        print(f"{OUT_NAME} : aucune ligne produite ({out_path})")
        return 1
    max_iso = df["ranking_date"].max()
    print(f"{OUT_NAME} : {len(df)} joueuses · ranking_date max={max_iso} · {out_path}")

    if args.ingest:
        from scripts.ingest_rankings_current import main as ingest_main

        ingest_main(argv=["--wta-csv", str(out_path)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
