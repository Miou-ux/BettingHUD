"""Jobs système CourtAlpha — lancement manuel + état (snapshot, scraper TE, ML, sources)."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_DIR = os.path.join(ROOT, "data", "cache", "web_jobs")
LOG_DIR = os.path.join(ROOT, "data", "logs")
PREMATCH_LOCK = os.path.join(ROOT, "data", "scraped", ".prematch_scrape.lock")
PREMATCH_LOCK_TTL_SEC = max(60, int(os.getenv("BETTINGHUD_PREMATCH_LOCK_TTL_SEC", "600")))

JOB_IDS = (
    "ml_train",
    "sync_sackmann",
    "sync_tml",
    "rebuild_snapshot",
    "scrape_te",
)


def _job_state_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _read_state(job_id: str) -> dict[str, Any] | None:
    path = _job_state_path(job_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(job_id: str, state: dict[str, Any]) -> None:
    os.makedirs(JOBS_DIR, exist_ok=True)
    with open(_job_state_path(job_id), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and int(code.value) == STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def prematch_scrape_in_progress() -> bool:
    if not os.path.exists(PREMATCH_LOCK):
        return False
    try:
        age = time.time() - os.path.getmtime(PREMATCH_LOCK)
    except OSError:
        return False
    if age > PREMATCH_LOCK_TTL_SEC:
        try:
            os.remove(PREMATCH_LOCK)
        except OSError:
            pass
        return False
    return True


def job_running(job_id: str) -> bool:
    state = _read_state(job_id)
    if not state:
        return False
    if str(state.get("status")) != "running":
        return False
    pid = state.get("pid")
    if _pid_alive(int(pid) if pid else None):
        return True
    state["status"] = "failed"
    state["finished_at"] = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    state["error"] = "processus terminé sans mise à jour d'état"
    _write_state(job_id, state)
    return False


def get_job_status(job_id: str) -> dict[str, Any]:
    state = _read_state(job_id) or {
        "job_id": job_id,
        "status": "idle",
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "log_path": None,
        "error": None,
    }
    state["running"] = job_running(job_id)
    if state["running"]:
        state["status"] = "running"
    return state


def _run_sackmann() -> int:
    sys.path.insert(0, ROOT)
    from scripts.sync_tours_daily import _run_py, _stamp_sync_meta, _update_wta_sackmann_raw

    rc = 0
    if _update_wta_sackmann_raw() != 0:
        rc = 1
    for script in ("ingest_rankings_current.py", "ingest_sackmann_wta.py", "apply_sqlite_indexes.py"):
        if _run_py(script) != 0 and script != "apply_sqlite_indexes.py":
            rc = 1
    if rc == 0:
        _stamp_sync_meta("last_sackmann_sync_ts")
    return rc


def _run_tml() -> int:
    sys.path.insert(0, ROOT)
    from scripts.sync_tours_daily import _run_py, _stamp_sync_meta

    rc = _run_py("sync_tml_recent.py")
    if rc == 0:
        _stamp_sync_meta("last_tml_sync_ts")
    return rc


def _run_job_sync(job_id: str) -> int:
    if job_id == "ml_train":
        cmd = [
            sys.executable,
            os.path.join(ROOT, "scripts", "update_model_tml.py"),
            "--skip-sync",
            "--min-year",
            "2020",
        ]
        return int(subprocess.run(cmd, cwd=ROOT).returncode or 0)
    if job_id == "sync_sackmann":
        return _run_sackmann()
    if job_id == "sync_tml":
        return _run_tml()
    if job_id == "rebuild_snapshot":
        cmd = [sys.executable, os.path.join(ROOT, "scripts", "rebuild_live_projection.py")]
        return int(subprocess.run(cmd, cwd=ROOT).returncode or 0)
    if job_id == "scrape_te":
        return int(subprocess.run([sys.executable, "-m", "scripts.scraper_prematch"], cwd=ROOT).returncode or 0)
    raise ValueError(f"job inconnu: {job_id}")


def _busy_reason(job_id: str) -> str | None:
    if job_running(job_id):
        return "job déjà en cours"
    if job_id in ("rebuild_snapshot",) or job_id == "scrape_te":
        sys.path.insert(0, ROOT)
        from scripts.live_snapshot import snapshot_build_in_progress

        if snapshot_build_in_progress():
            return "rebuild snapshot déjà en cours"
    if job_id == "scrape_te" and prematch_scrape_in_progress():
        return "scraper Tennis Explorer déjà en cours"
    return None


def start_job(job_id: str) -> dict[str, Any]:
    if job_id not in JOB_IDS:
        return {"ok": False, "error": f"job inconnu: {job_id}"}
    busy = _busy_reason(job_id)
    if busy:
        return {"ok": False, "error": busy, "job_id": job_id}

    os.makedirs(LOG_DIR, exist_ok=True)
    ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(LOG_DIR, f"web_job_{job_id}_{ts}.log")
    started = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200

    cmd = [sys.executable, os.path.join(ROOT, "scripts", "system_jobs.py"), "--run", job_id]
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=(os.name != "nt"),
        )

    _write_state(
        job_id,
        {
            "job_id": job_id,
            "status": "running",
            "pid": proc.pid,
            "started_at": started,
            "finished_at": None,
            "exit_code": None,
            "log_path": log_path,
            "error": None,
        },
    )
    return {"ok": True, "job_id": job_id, "status": "running", "pid": proc.pid, "log_path": log_path}


def _run_cli(job_id: str) -> int:
    state = _read_state(job_id) or {}
    try:
        rc = _run_job_sync(job_id)
    except Exception as exc:
        state["status"] = "failed"
        state["finished_at"] = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
        state["error"] = str(exc)
        state["exit_code"] = 1
        _write_state(job_id, state)
        return 1
    state["status"] = "done" if rc == 0 else "failed"
    state["finished_at"] = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    state["exit_code"] = rc
    if rc != 0:
        state["error"] = f"exit code {rc}"
    _write_state(job_id, state)
    return rc


def main() -> None:
    ap = argparse.ArgumentParser(description="CourtAlpha system jobs")
    ap.add_argument("--run", choices=JOB_IDS, help="exécute un job (sous-processus)")
    args = ap.parse_args()
    if not args.run:
        ap.print_help()
        sys.exit(2)
    sys.exit(_run_cli(args.run))


if __name__ == "__main__":
    main()
