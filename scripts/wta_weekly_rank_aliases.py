#!/usr/bin/env python3
"""Maintenance hebdo WTA — probe gaps, apply-top aliases, backfill rangs, ingest.

Usage :
  python scripts/wta_weekly_rank_aliases.py
  python scripts/wta_weekly_rank_aliases.py --apply-top 5 --dry-run
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

DEFAULT_WORK_DIR = ROOT / "data" / "raw" / "tennis_wta"
DEFAULT_DB = ROOT / "data" / "bettinghud.db"
_HASH_SUFFIX_RE = re.compile(r"\s[a-f0-9]{5}$", re.I)


def _safe_alias_target(target: str) -> bool:
    """Évite les cibles type clé interne TE (ex. 'oliveira 7b451')."""
    t = str(target or "").strip()
    if not t or len(t) < 3:
        return False
    if _HASH_SUFFIX_RE.search(t):
        return False
    if not any(ch.isalpha() for ch in t):
        return False
    return True


def _pick_aliases(proposed: dict[str, str], *, apply_top: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for wrong, target in proposed.items():
        if not _safe_alias_target(target):
            print(f"[wta-weekly] skip alias unsafe: {wrong!r} -> {target!r}", flush=True)
            continue
        out[wrong] = target
        if len(out) >= apply_top:
            break
    return out


def _run_py(script: str, extra: list[str] | None = None) -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / script), *(extra or [])]
    print(f"[wta-weekly] {' '.join(cmd)}", flush=True)
    return int(subprocess.run(cmd, cwd=ROOT).returncode or 0)


def run_wta_weekly_rank_aliases(
    *,
    work_dir: Path = DEFAULT_WORK_DIR,
    db_path: Path = DEFAULT_DB,
    apply_top: int = 5,
    dry_run: bool = False,
) -> dict:
    from scripts.probe_wta_name_rank_gaps import apply_aliases, probe_gaps
    from scripts.wta_name_aliases import DEFAULT_ALIASES_PATH

    before = probe_gaps(work_dir, db_path=str(db_path))
    if before.get("error"):
        return {"ok": False, "error": before["error"]}

    cov_before = float(before.get("rank_coverage_pct") or 0.0)
    aliases_added = 0
    applied: dict[str, str] = {}

    if apply_top > 0 and before.get("proposed_aliases"):
        applied = _pick_aliases(before["proposed_aliases"], apply_top=apply_top)
        if dry_run:
            print(f"[wta-weekly] dry-run: {len(applied)} alias(es) proposés", flush=True)
            for k, v in applied.items():
                print(f"  {k} -> {v}", flush=True)
        else:
            aliases_added = apply_aliases(DEFAULT_ALIASES_PATH, applied)
            print(f"[wta-weekly] aliases ajoutés: {aliases_added}", flush=True)

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "coverage_before_pct": cov_before,
            "aliases_added": 0,
            "aliases_proposed": len(applied),
        }

    rc_alias = _run_py(
        "wta_name_aliases.py",
        ["--work-dir", str(work_dir)],
    )
    rc_backfill = _run_py(
        "backfill_wta_delta_ranks.py",
        ["--work-dir", str(work_dir), "--db-path", str(db_path)],
    )
    rc_pq = _run_py("pipeline_quality.py")
    rc_qc = 0
    qc_rate = None
    qc_ok = True
    try:
        from scripts.qc_post_sync import run_qc_post_sync

        qc = run_qc_post_sync(str(db_path))
        for line in qc.summary_lines():
            print(line, flush=True)
        rc_qc = 0 if qc.ok else 1
        qc_ok = qc.ok
        for issue in qc.warnings:
            if issue.code == "wta_sqlite_ranks_post_cutoff":
                qc_rate = float((issue.context or {}).get("pct") or 0)
    except Exception as exc:
        print(f"[wta-weekly] qc_post_sync: {exc}", flush=True)
        rc_qc = 1
        qc_ok = False

    after = probe_gaps(work_dir, db_path=str(db_path))
    cov_after = float(after.get("rank_coverage_pct") or cov_before)

    ok = rc_alias == 0 and rc_backfill == 0 and rc_pq == 0 and qc_ok
    summary = {
        "ok": ok,
        "coverage_before_pct": cov_before,
        "coverage_after_pct": cov_after,
        "coverage_delta_pp": round(cov_after - cov_before, 1),
        "aliases_added": aliases_added,
        "aliases_applied": applied,
        "rc_alias": rc_alias,
        "rc_backfill": rc_backfill,
        "rc_pipeline_quality": rc_pq,
        "rc_qc": rc_qc,
        "qc_ok": qc_ok,
        "qc_sqlite_rank_pct": qc_rate,
    }

    if os.getenv("BETTINGHUD_WTA_WEEKLY_ALERT", "1").strip().lower() not in ("0", "false", "no"):
        try:
            from scripts.ops_telegram_alert import send_ops_alert

            lines = [
                f"Couverture CSV: {cov_before:.1f}% → {cov_after:.1f}% ({summary['coverage_delta_pp']:+.1f} pp)",
                f"Aliases ajoutés: {aliases_added}",
            ]
            if qc_rate is not None:
                lines.append(f"SQLite post-cutoff: {qc_rate:.1f}%")
            if not ok:
                lines.append(
                    f"RC alias={rc_alias} backfill={rc_backfill} pq={rc_pq} qc={rc_qc}"
                )
            subject = "WTA rangs hebdo OK" if ok and qc_ok else "WTA rangs hebdo — vérif"
            send_ops_alert(subject, "\n".join(lines))
        except Exception as exc:
            print(f"[wta-weekly] alerte ops: {exc}", flush=True)

    return summary


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Maintenance hebdo aliases/rangs WTA")
    ap.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--apply-top", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    out = run_wta_weekly_rank_aliases(
        work_dir=Path(args.work_dir),
        db_path=Path(args.db_path),
        apply_top=max(0, int(args.apply_top)),
        dry_run=args.dry_run,
    )
    print(out, flush=True)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
