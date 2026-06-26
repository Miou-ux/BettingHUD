"""Contrôle surface sur le dernier CSV prematch (alertes ops, non bloquant)."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd

from scripts.surface_speed import infer_surface_category, resolve_tournament_surface


def _latest_prematch_csv(scraped_dir: str) -> str | None:
    paths = sorted(glob.glob(os.path.join(scraped_dir, "prematch_odds_*.csv")))
    return paths[-1] if paths else None


def qc_prematch_surface_csv(csv_path: str) -> dict:
    df = pd.read_csv(csv_path, low_memory=False)
    if df.empty or "tournament" not in df.columns:
        return {"csv": csv_path, "warnings": [], "tournaments": 0}

    warnings: list[dict] = []
    seen: set[str] = set()
    for _, row in df.drop_duplicates(subset=["tournament"]).iterrows():
        tname = str(row.get("tournament") or "").strip()
        if not tname or tname in seen:
            continue
        seen.add(tname)
        url = str(row.get("tournament_url") or "").strip() or None
        csv_surf = str(row.get("surface") or "").strip().title()
        resolved = resolve_tournament_surface(
            tname,
            tournament_url=url,
            te_surface=csv_surf if csv_surf in ("Hard", "Clay", "Grass", "Carpet") else None,
        )
        inferred = infer_surface_category(tname)
        if csv_surf in ("Hard", "Clay", "Grass", "Carpet") and csv_surf != resolved:
            warnings.append(
                {
                    "tournament": tname,
                    "reason": "csv_surface_mismatch",
                    "csv_surface": csv_surf,
                    "resolved": resolved,
                }
            )
        elif resolved == "Hard" and inferred == "Hard" and not url:
            warnings.append(
                {
                    "tournament": tname,
                    "reason": "unknown_hard_default",
                    "resolved": resolved,
                }
            )

    return {
        "csv": csv_path,
        "tournaments": len(seen),
        "warnings": warnings,
        "ok": len(warnings) == 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="QC surface tournois prematch (warnings only).")
    ap.add_argument("--csv", help="Chemin CSV prematch (défaut: dernier fichier scraped)")
    ap.add_argument("--json", action="store_true", help="Sortie JSON")
    args = ap.parse_args()
    csv_path = args.csv or _latest_prematch_csv(os.path.join(ROOT, "data", "scraped"))
    if not csv_path or not os.path.isfile(csv_path):
        print("[qc-prematch-surface] aucun CSV prematch trouvé", flush=True)
        return 0
    report = qc_prematch_surface_csv(csv_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[qc-prematch-surface] csv={report['csv']} tournaments={report['tournaments']}", flush=True)
        for w in report.get("warnings") or []:
            print(f"  WARN {w.get('reason')}: {w.get('tournament')} ({w})", flush=True)
        if report.get("ok"):
            print("[qc-prematch-surface] OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
