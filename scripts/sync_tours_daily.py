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


def _run_wta_delta_on_raw() -> int:
    """sync_wta_delta + enrich sur data/raw/tennis_wta (prod)."""
    wta_dir = _wta_raw_dir()
    os.makedirs(wta_dir, exist_ok=True)
    extra = ["--work-dir", wta_dir]
    try:
        from scripts.wta_sackmann_common import DEFAULT_CUTOFF
    except Exception:
        DEFAULT_CUTOFF = 20260526  # type: ignore[misc, assignment]
    extra += ["--cutoff-date", str(DEFAULT_CUTOFF)]
    _append_log("WTA delta: lancement sync_wta_delta.py")
    rc = _run_py_with_args("sync_wta_delta.py", extra)
    if rc != 0:
        _append_log("sync_wta_delta.py a echoue.")
        return rc
    _append_log("sync_wta_delta.py OK.")
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
    if _run_py("sync_tml_recent.py") != 0:
        _append_log("sync_tml_recent.py rapporte erreur.")
        rc = 1
    else:
        _append_log("sync_tml_recent.py OK.")
        _stamp_sync_meta("last_tml_sync_ts")

    pq = _run_py("pipeline_quality.py")
    if pq != 0:
        _append_log("pipeline_quality.py rapporte erreur (voir sortie terminal).")
        rc = 1
    else:
        _append_log("pipeline_quality.py OK.")
        _stamp_sync_meta("last_sackmann_sync_ts")

    _append_log(f"=== fin sync ATP+WTA rc={rc} ===")

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

    return rc


def main() -> None:
    sys.exit(run_sync_bundle())


if __name__ == "__main__":
    main()
