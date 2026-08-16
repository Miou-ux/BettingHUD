"""
Mise à jour base SQLite ATP + WTA (usage manuel ou planificateur).

- ATP : TennisMyLife -> `matches_recent` (scripts/sync_tml_recent.py).
- WTA : si `data/raw/tennis_wta` est un clone Git -> `git pull` ;
        sinon delta tennis-data : `sync_wta_delta.py` puis `enrich_wta_delta_te_stats.py` sur `data/raw/tennis_wta`.
        Puis ingest + index (scripts/pipeline_quality.py, sans re-entrainement ML).

Usage:
    python scripts/sync_tours_daily.py
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LOG_DIR = os.path.join(ROOT, "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "tours_auto_sync.log")


def _append_log(line: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {line}\n")


def _git_pull_wta() -> None:
    """Met à jour le dépôt cloné Sackmann WTA sous data/raw/tennis_wta."""
    wta_dir = os.path.join(ROOT, "data", "raw", "tennis_wta")
    git_mark = os.path.join(wta_dir, ".git")
    if not os.path.isdir(git_mark):
        _append_log("WTA git pull skipped (pas de depot .git dans data/raw/tennis_wta).")
        return
    exe = shutil.which("git")
    if not exe:
        _append_log("WTA git pull skipped (git absent du PATH).")
        return
    try:
        r = subprocess.run(
            [exe, "-C", wta_dir, "pull", "-q"],
            cwd=ROOT,
            timeout=int(os.getenv("BETTINGHUD_WTA_GIT_PULL_TIMEOUT_SEC", "180")),
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            _append_log("WTA git pull OK.")
        else:
            _append_log(
                f"WTA git pull exit={r.returncode} stderr={(r.stderr or '')[:500]}"
            )
    except Exception as e:
        _append_log(f"WTA git pull error: {e}")


def _wta_raw_dir() -> str:
    return os.path.join(ROOT, "data", "raw", "tennis_wta")


def _run_py_with_args(script_relative: str, extra_args: list[str]) -> int:
    cmd = [sys.executable, os.path.join(ROOT, "scripts", script_relative), *extra_args]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=False, timeout=None)
        return int(p.returncode or 0)
    except Exception as e:
        _append_log(f"{script_relative} exception: {e}")
        return 1


def _wta_delta_extra_args() -> list[str]:
    wta_dir = _wta_raw_dir()
    extra = ["--work-dir", wta_dir]
    try:
        from scripts.wta_sackmann_common import DEFAULT_CUTOFF
    except Exception:
        DEFAULT_CUTOFF = 20260526  # type: ignore[misc, assignment]
    extra += ["--cutoff-date", str(DEFAULT_CUTOFF)]
    return extra


def _run_wta_rank_backfill() -> int:
    """Backfill rangs après sync_wta_delta + pont Flashscore (évite trous FS)."""
    extra = _wta_delta_extra_args()
    _append_log("WTA delta: backfill rangs match-time (post-Flashscore)")
    rc = _run_py_with_args(
        "backfill_wta_delta_ranks.py",
        extra + ["--db-path", os.path.join(ROOT, "data", "bettinghud.db")],
    )
    if rc != 0:
        _append_log("backfill_wta_delta_ranks.py a echoue (non bloquant).")
    else:
        _append_log("backfill_wta_delta_ranks.py OK.")
    return rc


def _run_wta_delta_on_raw() -> int:
    """sync_wta_delta + enrich sur data/raw/tennis_wta (prod)."""
    wta_dir = _wta_raw_dir()
    os.makedirs(wta_dir, exist_ok=True)
    extra = _wta_delta_extra_args()
    _append_log("WTA delta: lancement sync_wta_delta.py")
    rc = _run_py_with_args("sync_wta_delta.py", extra)
    if rc != 0:
        _append_log("sync_wta_delta.py a echoue.")
        return rc
    _append_log("sync_wta_delta.py OK.")
    _append_log("WTA delta: apply name aliases (tennis-data / FS)")
    rc_alias = _run_py_with_args("wta_name_aliases.py", extra)
    if rc_alias != 0:
        _append_log("wta_name_aliases.py a echoue (non bloquant).")
    else:
        _append_log("wta_name_aliases.py OK.")
    _append_log("WTA delta: lancement enrich_wta_delta_metadata.py")
    rc = _run_py_with_args("enrich_wta_delta_metadata.py", extra + ["--dedup"])
    if rc != 0:
        _append_log("enrich_wta_delta_metadata.py a echoue.")
        return rc
    _append_log("enrich_wta_delta_metadata.py OK.")
    _append_log("WTA delta: refresh wta_rankings_current (matchs récents + cache TE)")
    try:
        from scripts.wta_sackmann_common import DEFAULT_CUTOFF as _cutoff
    except Exception:
        _cutoff = 20260526  # type: ignore[misc, assignment]
    rc = _run_py_with_args(
        "refresh_wta_rankings_current.py",
        ["--work-dir", wta_dir, "--min-date", str(_cutoff), "--ingest"],
    )
    if rc != 0:
        _append_log("refresh_wta_rankings_current.py a echoue (non bloquant).")
    _append_log("WTA delta: lancement enrich_wta_delta_te_stats.py")
    rc = _run_py_with_args("enrich_wta_delta_te_stats.py", extra)
    if rc != 0:
        _append_log("enrich_wta_delta_te_stats.py a echoue.")
    else:
        _append_log("enrich_wta_delta_te_stats.py OK.")
    return rc


def _update_wta_sackmann_raw() -> int:
    """Clone Git -> pull ; sinon pipeline delta WTA sur data/raw/tennis_wta."""
    wta_dir = _wta_raw_dir()
    git_mark = os.path.join(wta_dir, ".git")
    if os.path.isdir(git_mark):
        _git_pull_wta()
        return 0
    return _run_wta_delta_on_raw()



def _run_py(script_relative: str) -> int:
    cmd = [sys.executable, os.path.join(ROOT, "scripts", script_relative)]
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=False, timeout=None)
        return int(p.returncode or 0)
    except Exception as e:
        _append_log(f"{script_relative} exception: {e}")
        return 1


def _stamp_sync_meta(meta_key: str, iso: str | None = None) -> None:
    try:
        import sqlite3

        sys.path.insert(0, ROOT)
        from scripts.bets_db import DB_PATH_DEFAULT, ensure_bets_meta, set_meta

        ts = iso or _dt.datetime.utcnow().isoformat(timespec="seconds")
        dbp = (
            os.path.join(ROOT, DB_PATH_DEFAULT)
            if not os.path.isabs(DB_PATH_DEFAULT)
            else DB_PATH_DEFAULT
        )
        conn = sqlite3.connect(dbp)
        try:
            ensure_bets_meta(conn)
            set_meta(conn, meta_key, ts)
        finally:
            conn.close()
        _append_log(f"bets_meta: {meta_key} enregistré.")
    except Exception as e:
        _append_log(f"bets_meta {meta_key}: {e}")


def run_sync_bundle() -> int:
    """Exécute la séquence complète. Retourne 0 si aucune erreur fatale (codes partiels tolérés)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    _append_log("=== debut sync ATP+WTA ===")
    rc = 0
    if _update_wta_sackmann_raw() != 0:
        rc = 1
    if _run_py_with_args(
        "sync_wta_flashscore_results.py",
        ["--work-dir", _wta_raw_dir()],
    ) != 0:
        _append_log("sync_wta_flashscore_results.py a echoue.")
        rc = 1
    else:
        _append_log("sync_wta_flashscore_results.py OK.")
    _run_wta_rank_backfill()
    _append_log("WTA delta: re-apply name aliases post-Flashscore")
    _run_py_with_args("wta_name_aliases.py", _wta_delta_extra_args())
    _append_log("WTA delta: dedup post-Flashscore (C1 doublons)")
    rc_dedup = _run_py_with_args(
        "enrich_wta_delta_metadata.py",
        _wta_delta_extra_args() + ["--dedup", "--no-players-csv"],
    )
    if rc_dedup != 0:
        _append_log("enrich_wta_delta_metadata.py --dedup a echoue (non bloquant).")
    else:
        _append_log("enrich_wta_delta_metadata.py --dedup OK.")
    _append_log("WTA delta: retry serve enrich main tour (lignes w_svpt NULL)")
    rc_serve2 = _run_py_with_args(
        "enrich_wta_delta_te_stats.py",
        _wta_delta_extra_args() + ["--main-tour-only"],
    )
    if rc_serve2 != 0:
        _append_log("enrich_wta_delta_te_stats.py retry a echoue (non bloquant).")
    else:
        _append_log("enrich_wta_delta_te_stats.py retry OK.")
    if _run_py("sync_tml_recent.py") != 0:
        _append_log("sync_tml_recent.py rapporte erreur.")
        rc = 1
    else:
        _append_log("sync_tml_recent.py OK.")
        _stamp_sync_meta("last_tml_sync_ts")
    if _run_py("sync_atp_flashscore_results.py") != 0:
        _append_log("sync_atp_flashscore_results.py a echoue.")
        rc = 1
    else:
        _append_log("sync_atp_flashscore_results.py OK.")

    pq = _run_py("pipeline_quality.py")
    if pq != 0:
        _append_log("pipeline_quality.py rapporte erreur (voir sortie terminal).")
        rc = 1
    else:
        _append_log("pipeline_quality.py OK.")
        _stamp_sync_meta("last_sackmann_sync_ts")
        _stamp_sync_meta("last_tours_sync_ts")
        if os.getenv("BETTINGHUD_POST_SYNC_REBUILD", "1").strip() not in ("0", "false", "False", "no", "NO"):
            if _run_py("build_feature_store.py") != 0:
                _append_log("build_feature_store.py a echoue.")
                rc = 1
            else:
                _append_log("build_feature_store.py OK.")
            if _run_py("refresh_elo_maps_fast.py") != 0:
                _append_log("refresh_elo_maps_fast.py a echoue.")
                rc = 1
            else:
                _append_log("refresh_elo_maps_fast.py OK.")

    _append_log(f"=== fin sync ATP+WTA rc={rc} ===")

    try:
        from scripts.morning_chain_state import record_step

        record_step(
            "tours_sync",
            ok=(rc == 0),
            rc=rc,
            detail={"source": "sync_tours_daily"},
        )
    except Exception as e:
        _append_log(f"morning_chain_state tours_sync: {e}")

    if rc == 0:
        try:
            import sqlite3

            sys.path.insert(0, ROOT)
            from scripts.bets_db import (
                DB_PATH_DEFAULT,
                META_LAST_TOURS_SYNC_TS,
                ensure_bets_meta,
                set_meta,
            )

            dbp = (
                os.path.join(ROOT, DB_PATH_DEFAULT)
                if not os.path.isabs(DB_PATH_DEFAULT)
                else DB_PATH_DEFAULT
            )
            conn = sqlite3.connect(dbp)
            try:
                ensure_bets_meta(conn)
                set_meta(
                    conn,
                    META_LAST_TOURS_SYNC_TS,
                    _dt.datetime.utcnow().isoformat(timespec="seconds"),
                )
            finally:
                conn.close()
            _append_log("bets_meta: last_tours_sync_ts enregistré.")
        except Exception as e:
            _append_log(f"bets_meta last_tours_sync_ts: {e}")

    if rc == 0:
        try:
            from scripts.qc_post_sync import run_qc_post_sync

            qc = run_qc_post_sync()
            for line in qc.summary_lines():
                _append_log(line)
            record_step(
                "qc_post_sync",
                ok=qc.ok,
                rc=0 if qc.ok else 1,
                detail=qc.to_dict(),
            )
            if not qc.ok:
                _append_log("qc_post_sync: échec bloquant.")
                rc = 1
        except Exception as e:
            _append_log(f"qc_post_sync: exception {e}")
            try:
                from scripts.morning_chain_state import record_step as _rec

                _rec("qc_post_sync", ok=False, rc=1, detail={"error": str(e)[:300]})
            except Exception:
                pass
            try:
                from scripts.ops_alert_human import format_simple_ops
                from scripts.ops_telegram_alert import send_ops_alert

                _, body = format_simple_ops(
                    "Contrôle données après sync — erreur technique",
                    [
                        f"• {str(e)[:400]}",
                        "",
                        "Le contrôle qualité n'a pas pu s'exécuter.",
                    ],
                )
                send_ops_alert(
                    "QC post-sync EXCEPTION",
                    body,
                    dedup_key="qc_post_sync_fail",
                )
            except Exception:
                pass
            rc = 1

    return rc


def main() -> None:
    sys.exit(run_sync_bundle())


if __name__ == "__main__":
    main()
