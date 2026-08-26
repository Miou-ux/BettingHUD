#!/usr/bin/env python3
"""Chaîne matinale séquentielle : sync tours → build snapshot → publications.

Chaque étape doit réussir avant la suivante. Aucune notif 1D1P / Top 5 si une
étape échoue ou si les garde-fous fraîcheur ne passent pas.

Usage :
  py -3 scripts/morning_orchestrator.py --publish
  py -3 scripts/morning_live_pipeline.py --morning-publish   # délègue ici
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

PARIS_TZ = ZoneInfo("Europe/Paris")


def _date_int_from_db(val: object) -> int | None:
    s = str(val or "").strip().replace("-", "")[:8]
    return int(s) if s.isdigit() and len(s) == 8 else None


def _max_lag_days() -> int:
    try:
        return max(1, int(os.getenv("BETTINGHUD_MORNING_TOURS_MAX_LAG_DAYS", "5")))
    except ValueError:
        return 5


def _snapshot_max_age_sec() -> int:
    try:
        return max(300, int(os.getenv("BETTINGHUD_MORNING_SNAPSHOT_MAX_AGE_SEC", "7200")))
    except ValueError:
        return 7200


def _min_te_profile_ratio() -> float:
    try:
        return float(os.getenv("BETTINGHUD_MORNING_MIN_TE_PROFILE_RATIO", "0.5"))
    except ValueError:
        return 0.5


def _publish_fallback_snapshot_max_age_sec() -> int:
    try:
        return max(3600, int(os.getenv("BETTINGHUD_MORNING_PUBLISH_FALLBACK_MAX_AGE_SEC", "21600")))
    except ValueError:
        return 21600


def _sync_meta_ok_today(tours_iso: str, cal: str) -> bool:
    if not tours_iso:
        return False
    try:
        ts = datetime.fromisoformat(tours_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=PARIS_TZ)
        return ts.astimezone(PARIS_TZ).date().isoformat() == cal
    except ValueError:
        return tours_iso[:10] == cal


def validate_tours_data(*, _log) -> bool:
    """Vérifie que les tables tours ont des matchs récents."""
    from scripts.bets_db import DB_PATH_DEFAULT, get_data_freshness_snapshot

    dbp = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    cutoff = int(
        (datetime.now(PARIS_TZ).date() - timedelta(days=_max_lag_days())).strftime("%Y%m%d")
    )

    conn = sqlite3.connect(dbp)
    try:
        atp_max = _date_int_from_db(conn.execute("SELECT MAX(tourney_date) FROM matches_recent").fetchone()[0])
        wta_max = _date_int_from_db(conn.execute("SELECT MAX(tourney_date) FROM wta_matches").fetchone()[0])
    finally:
        conn.close()

    fresh = get_data_freshness_snapshot(dbp)
    tours_iso = str(fresh.get("last_tours_sync_iso") or "")
    cal = datetime.now(PARIS_TZ).date().isoformat()
    ok_atp = atp_max is not None and atp_max >= cutoff
    ok_wta = wta_max is not None and wta_max >= cutoff
    ok_meta = _sync_meta_ok_today(tours_iso, cal)

    _log(
        f"Garde-fou tours : ATP max={atp_max} WTA max={wta_max} "
        f"(cutoff>={cutoff}) meta_today={ok_meta} sync_iso={tours_iso or '—'}"
    )
    if not ok_atp:
        _log(f"ÉCHEC garde-fou : données ATP trop anciennes (max={atp_max}).")
    if not ok_wta:
        _log(f"ÉCHEC garde-fou : données WTA trop anciennes (max={wta_max}).")
    if not ok_meta:
        _log("ÉCHEC garde-fou : last_tours_sync_ts pas à jour aujourd'hui (Paris).")

    return ok_atp and ok_wta and ok_meta


def ensure_tours_sync(*, _log, force: bool = False) -> bool:
    from scripts.morning_chain_state import record_step, step_ok_today
    from scripts.sync_tours_daily import run_sync_bundle

    if not force and step_ok_today("tours_sync"):
        _log("Sync tours déjà OK aujourd'hui (cron 03:30 ou passe précédente) — skip exécution.")
        if validate_tours_data(_log=_log):
            return True
        _log("Garde-fou données échoué malgré sync marqué OK — relance sync.")

    _log("Lancement sync tours (ATP+WTA)…")
    t0 = time.time()
    rc = run_sync_bundle()
    elapsed = time.time() - t0
    ok = rc == 0
    record_step("tours_sync", ok=ok, rc=rc, detail={"elapsed_sec": round(elapsed, 1)})
    if not ok:
        _log(f"Sync tours ÉCHEC (rc={rc}, {elapsed:.0f} s).")
        if validate_tours_data(_log=_log):
            _log("Données tours encore valides malgré l'échec sync — poursuite chaîne.")
            return True
        return False
    _log(f"Sync tours OK ({elapsed:.0f} s).")
    return validate_tours_data(_log=_log)


def validate_build(*, _log) -> bool:
    from scripts.bets_db import DB_PATH_DEFAULT, ensure_daily_top_proba_schema
    from scripts.live_snapshot import load_latest_live_snapshot

    matches, meta = load_latest_live_snapshot(max_age_sec=24 * 3600)
    built_at = float((meta or {}).get("built_at") or 0.0)
    age_sec = time.time() - built_at if built_at > 0 else -1.0
    max_age = _snapshot_max_age_sec()

    if not matches:
        _log("ÉCHEC garde-fou build : snapshot vide.")
        return False
    if built_at <= 0 or age_sec < 0 or age_sec > max_age:
        _log(
            f"ÉCHEC garde-fou build : snapshot trop vieux "
            f"(âge={age_sec:.0f}s, max={max_age}s)."
        )
        return False

    cal = datetime.now(PARIS_TZ).date().isoformat()
    dbp = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    conn = sqlite3.connect(dbp)
    try:
        ensure_daily_top_proba_schema(conn)
        n_top = conn.execute(
            "SELECT COUNT(*) FROM daily_top_proba_picks WHERE calendar_date = ?",
            (cal,),
        ).fetchone()[0]
    finally:
        conn.close()

    if int(n_top or 0) < 1:
        _log(
            "ATTENTION : aucune ligne daily_top_proba_picks pour aujourd'hui "
            "(publication matinale à vide autorisée)."
        )

    try:
        from app.dashboard import _count_te_profiles_complete

        te_ok, te_total = _count_te_profiles_complete(matches)
        ratio = (te_ok / te_total) if te_total else 1.0
        min_ratio = _min_te_profile_ratio()
        _log(f"Profils TE : {te_ok}/{te_total} ({ratio:.0%}), seuil {min_ratio:.0%}.")
        if te_total and ratio < min_ratio:
            _log("ÉCHEC garde-fou build : trop de profils TE incomplets.")
            return False
    except Exception as exc:
        _log(f"Vérif profils TE ignorée : {exc}")

    _log(
        f"Garde-fou build OK : {len(matches)} match(s), âge snapshot {age_sec:.0f}s, "
        f"top_proba_rows={n_top}."
    )
    return True


def snapshot_ok_for_publish_fallback(*, _log) -> bool:
    """Snapshot existant utilisable pour publier si le build 05:00 a échoué."""
    from scripts.live_snapshot import load_latest_live_snapshot

    matches, meta = load_latest_live_snapshot(max_age_sec=24 * 3600)
    built_at = float((meta or {}).get("built_at") or 0.0)
    age_sec = time.time() - built_at if built_at > 0 else -1.0
    max_age = _publish_fallback_snapshot_max_age_sec()
    if not matches:
        _log("Fallback publication impossible : snapshot vide.")
        return False
    if built_at <= 0 or age_sec < 0 or age_sec > max_age:
        _log(
            f"Fallback publication impossible : snapshot trop vieux "
            f"(âge={age_sec:.0f}s, max fallback={max_age}s)."
        )
        return False
    _log(
        f"Fallback publication : snapshot utilisable ({len(matches)} match(s), "
        f"âge {age_sec:.0f}s)."
    )
    return True


def run_build_step(*, _log) -> bool:
    from scripts.morning_chain_state import record_step
    from scripts.morning_live_pipeline import run_build_phase

    _log("Lancement build (scrape TE + snapshot)…")
    t0 = time.time()
    rc = run_build_phase(_log=_log)
    elapsed = time.time() - t0
    ok = rc == 0 and validate_build(_log=_log)
    qc_reports: list = []
    if ok:
        try:
            from scripts.qc_live_snapshot import run_qc_live_snapshot
            from scripts.morning_chain_state import record_step as _record

            qc = run_qc_live_snapshot()
            qc_reports.append(qc)
            for line in qc.summary_lines():
                _log(line)
            _record(
                "qc_live_snapshot",
                ok=qc.ok,
                rc=0 if qc.ok else 1,
                detail=qc.to_dict(),
            )
            if not qc.ok:
                _log("ÉCHEC QC live snapshot (bloquant) — publications annulées.")
                ok = False
        except Exception as exc:
            _log(f"QC live snapshot ignoré : {exc}")

    record_step(
        "build",
        ok=ok,
        rc=rc,
        detail={"elapsed_sec": round(elapsed, 1), "validated": ok},
    )
    if not ok:
        _log(f"Build ÉCHEC (rc={rc}, {elapsed:.0f} s).")
        try:
            from scripts.qc_notify_ops import notify_qc_reports

            notify_qc_reports(qc_reports, publish_ok=False)
        except Exception:
            pass
        return False
    _log(f"Build OK ({elapsed:.0f} s).")
    try:
        from scripts.qc_notify_ops import notify_qc_reports

        notify_qc_reports(qc_reports, publish_ok=True)
    except Exception as exc:
        _log(f"QC notify ops : {exc}")
    return True


def run_publish_step(*, _log) -> bool:
    from scripts.morning_chain_state import record_step
    from scripts.morning_live_pipeline import run_telegram_phase

    try:
        from scripts.telegram_runtime_cache import invalidate_snapshot_cache

        invalidate_snapshot_cache()
        _log("Cache Telegram picks invalidé avant publications.")
    except Exception as exc:
        _log(f"Invalidation cache Telegram ignorée : {exc}")

    _log("Lancement publications (1D1P, Top 5, canal)…")
    t0 = time.time()
    rc = run_telegram_phase(_log=_log, source="morning-sync")
    elapsed = time.time() - t0
    ok = rc == 0
    record_step("publish", ok=ok, rc=rc, detail={"elapsed_sec": round(elapsed, 1)})
    if not ok:
        _log(f"Publications ÉCHEC (rc={rc}, {elapsed:.0f} s).")
        return False
    _log(f"Publications OK ({elapsed:.0f} s).")
    return True


def run_shadow_step(*, _log) -> None:
    """Non-blocking shadow capture for strategy experiments."""
    try:
        from scripts.shadow_top5 import capture_shadow_suite

        out = capture_shadow_suite()
        cand_a = out.get("candidate_a") or {}
        cand_b = out.get("candidate_b") or {}
        prod = out.get("prod") or {}
        _log(
            "Shadow Top5 capturé : "
            f"A={cand_a.get('n_picks', 0)} pick(s), "
            f"B={cand_b.get('n_picks', 0)} pick(s), "
            f"prod={prod.get('n_picks', 0)} pick(s)."
        )
    except Exception as exc:
        _log(f"Shadow Top5 ignoré (erreur non bloquante) : {exc}")


def run_publish_only_chain(*, _log, allow_emergency_build: bool = True) -> int:
    """Cron 05:00 — publications uniquement (sync + build doivent être terminés avant)."""
    _log("=== Publications matin 05:00 (sans sync — prep crons 00:30→04:35) ===")

    if not validate_tours_data(_log=_log):
        _log("ÉCHEC : données tours invalides — publication annulée.")
        return 1

    build_ready = validate_build(_log=_log)
    if not build_ready:
        from scripts.morning_chain_state import step_ok_today

        if step_ok_today("build"):
            _log("Garde-fou build strict KO mais build cron OK aujourd'hui — fallback snapshot.")
            build_ready = snapshot_ok_for_publish_fallback(_log=_log)
        elif allow_emergency_build:
            _log("Build pas prêt — tentative build d'urgence (dernier recours).")
            build_ready = run_build_step(_log=_log)
        else:
            build_ready = snapshot_ok_for_publish_fallback(_log=_log)

    if not build_ready:
        _log("Chaîne interrompue : snapshot/build indisponible (publications non envoyées).")
        return 2

    if not run_publish_step(_log=_log):
        _log("Chaîne interrompue : publications en échec.")
        return 3

    run_shadow_step(_log=_log)
    _log("=== Publications matin 05:00 terminées avec succès ===")
    return 0


def run_publish_chain(*, _log, force_tours_sync: bool = False) -> int:
    """Chaîne complète sync → build → publish (manuel / rattrapage). Retourne 0 si tout OK."""
    _log("=== Chaîne matinale : sync tours → build → publications ===")

    tours_ok = ensure_tours_sync(_log=_log, force=force_tours_sync)
    if not tours_ok:
        _log(
            "Sync tours en échec — poursuite build/publications "
            "(scrape TE + snapshot ne dépendent pas du sync tours)."
        )
        try:
            from scripts.ops_telegram_alert import send_ops_alert

            send_ops_alert(
                "Morning publish 05:00 — sync tours soft-fail",
                "ensure_tours_sync=False : build/publications poursuivis malgré sync tours KO. "
                "Vérifier tours_cron.log / qc_post_sync.",
                dedup_key="morning_tours_soft_fail",
            )
        except Exception as exc:
            _log(f"Alerte soft-fail tours ignorée : {exc}")

    build_ok = run_build_step(_log=_log)
    if not build_ok:
        if snapshot_ok_for_publish_fallback(_log=_log):
            _log("Build échoué — tentative publications avec snapshot existant.")
        else:
            _log("Chaîne interrompue après build (publications non envoyées).")
            return 2

    if not run_publish_step(_log=_log):
        _log("Chaîne interrompue : publications en échec.")
        return 3

    run_shadow_step(_log=_log)
    _log("=== Chaîne matinale terminée avec succès ===")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    os.environ.setdefault("BETTINGHUD_ENV", "prod")

    ap = argparse.ArgumentParser(description="Orchestrateur chaîne matinale BettingHUD")
    ap.add_argument(
        "--publish",
        action="store_true",
        help="Sync tours (si besoin) + build + publications",
    )
    ap.add_argument(
        "--force-tours-sync",
        action="store_true",
        help="Relance sync tours même si déjà OK aujourd'hui",
    )
    args = ap.parse_args(argv)

    if not args.publish:
        ap.print_help()
        return 2

    from scripts.morning_live_pipeline import _install_streamlit_mock, _open_log

    _install_streamlit_mock()
    _log, log_path = _open_log("orchestrator-publish")
    rc = run_publish_chain(_log=_log, force_tours_sync=args.force_tours_sync)
    _log(f"Fin orchestrateur (code {rc}). Journal : {log_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
