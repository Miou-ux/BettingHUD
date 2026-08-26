#!/usr/bin/env python3
"""Pipeline matinal BettingHUD (scrape + snapshot, notifications Telegram séparées).

Phases (Europe/Paris, cron PROD) :
  - **00:30** : sync tours (cron ``data-sync`` — doit finir avant build 04:30)
  - **04:30** : ``--build-only`` — scrape TE, snapshot full, report algo (préparation, pas de publication)
  - **04:56** : preflight (cron séparé)
  - **05:00** : ``--publish-only`` — **publications seules** (Top 5, 1 Day 1 Pick TG+Discord, canal TG)

Modes manuels :
  - ``--telegram-only`` / ``--telegram-only --source morning-sync`` — envoi sans rebuild
  - sans argument : build + Telegram ``morning`` (historique, déconseillé en prod)

Usage :
  py -3 scripts/morning_live_pipeline.py --build-only
  py -3 scripts/morning_live_pipeline.py --morning-publish
  py -3 scripts/morning_live_pipeline.py --telegram-only --source morning-sync
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

os.environ["BETTINGHUD_HEADLESS"] = "1"
os.environ["BETTINGHUD_MORNING_BUILD"] = "1"
os.environ.setdefault("BETTINGHUD_LIVE_DATA_DAEMON", "0")
os.environ.setdefault("BETTINGHUD_AUTO_SYNC_TOURS", "0")
os.environ.setdefault("BETTINGHUD_ENABLE_AUTO_ML_TRAIN_WEEKLY", "0")
os.environ.setdefault("BETTINGHUD_LIVE_PROJECTION_WARMUP", "0")
os.environ.setdefault("BETTINGHUD_SYNC_FULL_AFTER_PREVIEW", "1")
os.environ.setdefault("BETTINGHUD_LIVE_INCREMENTAL_ENRICH", "0")
os.environ.setdefault("BETTINGHUD_LIVE_DELTA_ENRICH", "1")
os.environ.setdefault("BETTINGHUD_LIVE_BUILD_FAST_PREVIEW", "0")
os.environ.setdefault("BETTINGHUD_LIVE_TWO_PHASE_SNAPSHOT", "0")


def _install_streamlit_mock() -> None:
    from unittest.mock import MagicMock

    class _MockSessionState(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key) from None

        def __setattr__(self, key, value):
            self[key] = value

    def _passthrough_cache(*_cache_args, **_cache_kwargs):
        def _decorator(fn):
            cache: dict = {}

            def wrapper(*args, **kwargs):
                key = (args, tuple(sorted(kwargs.items())))
                if key not in cache:
                    cache[key] = fn(*args, **kwargs)
                return cache[key]

            wrapper.clear = cache.clear  # type: ignore[attr-defined]
            return wrapper

        if len(_cache_args) == 1 and callable(_cache_args[0]) and not _cache_kwargs:
            return _decorator(_cache_args[0])
        return _decorator

    st = MagicMock()
    st.session_state = _MockSessionState()
    st.cache_resource = _passthrough_cache
    st.cache_data = _passthrough_cache
    sys.modules["streamlit"] = st
    sar = MagicMock()
    sar.st_autorefresh = lambda *a, **k: None
    sys.modules["streamlit_autorefresh"] = sar


def _open_log(phase: str) -> tuple[callable, str]:
    log_dir = os.path.join(ROOT, "data", "cache", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir,
        f"morning_pipeline_{phase}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
    )

    def _log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] [{phase}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    return _log, log_path


def run_build_phase(*, _log) -> int:
    from scripts.db_indexes import ensure_bettinghud_query_indexes
    from scripts.live_snapshot import (
        FULL_SNAPSHOT_PATH,
        SNAPSHOT_PATH,
        invalidate_live_snapshot_ram_cache,
        snapshot_build_in_progress,
    )
    from scripts.scraper_prematch import FlashscoreScraper

    from app.dashboard import (  # noqa: E402
        _count_te_profiles_complete,
        _purge_all_live_player_caches,
        _prematch_csv_signature,
        _try_build_live_snapshot_if_missing,
        ensure_te_profiles_for_prematch_csv,
    )

    if snapshot_build_in_progress():
        _log("Build déjà en cours — abandon.")
        return 2

    _log("Index SQLite…")
    ensure_bettinghud_query_indexes()

    try:
        from scripts.refresh_wta_rankings_current import refresh_rankings_csv
        from scripts.ingest_rankings_current import main as ingest_wta_rankings

        _log("Refresh + ingest classements WTA (delta matchs / cache TE)…")
        refresh_rankings_csv()
        ingest_wta_rankings(argv=[])
    except Exception as exc:
        _log(f"Ingest rankings WTA ignoré : {exc}")

    _log("Scrape Tennis Explorer (jour + demain)…")
    t0 = time.time()
    try:
        matches = asyncio.run(FlashscoreScraper().get_today_matches_and_odds())
        _log(f"Scrape OK — {len(matches or [])} lignes CSV ({time.time() - t0:.0f} s).")
    except Exception as exc:
        _log(f"Scrape ERREUR: {exc}")
        return 1

    csv_path, _ = _prematch_csv_signature()
    _log("Pré-scrape profils TE (tous les joueurs du CSV filtré)…")
    try:
        te_stats = ensure_te_profiles_for_prematch_csv(csv_path, label="morning-pipeline-te")
        _log(
            f"Profils TE: {te_stats['cached']} cache, {te_stats['scraped']} scrape, "
            f"{te_stats['failed']} échec(s) / {te_stats['urls']} URLs."
        )
    except Exception as exc:
        _log(f"Pré-scrape profils TE ignoré : {exc}")

    _log("Build snapshot complet (purge + reconstruction)…")
    try:
        _purge_all_live_player_caches()
        for path in (SNAPSHOT_PATH, FULL_SNAPSHOT_PATH):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        invalidate_live_snapshot_ram_cache()
    except Exception as exc:
        _log(f"Purge snapshot ignorée : {exc}")

    t1 = time.time()
    ok = _try_build_live_snapshot_if_missing(
        identity_workers=None,
        label="morning-pipeline-build",
        force_full=True,
    )
    elapsed = time.time() - t1
    if not ok:
        _log(f"Build snapshot échec ou verrou ({elapsed:.0f} s).")
        return 1

    try:
        from app.dashboard import sync_algo_report_from_snapshot

        n_sync = sync_algo_report_from_snapshot(force=True)
        _log(f"Report algo synchronisé : {n_sync} opportunité(s).")
    except Exception as exc:
        _log(f"Sync report algo ignorée : {exc}")

    try:
        from scripts.telegram_runtime_cache import invalidate_snapshot_cache, warm_telegram_runtime_cache

        invalidate_snapshot_cache()
        _log("Cache Telegram invalidé (snapshot à jour).")
        warm_telegram_runtime_cache()
        _log("Cache Telegram pré-chargé (ML + picks /top5 /today).")
    except Exception as exc:
        _log(f"Cache Telegram ignoré : {exc}")

    try:
        from scripts.live_snapshot import load_latest_live_snapshot

        matches, _meta = load_latest_live_snapshot(max_age_sec=24 * 3600)
        te_ok, te_total = _count_te_profiles_complete(matches or [])
        _log(f"Profils TE complets dans le snapshot : {te_ok}/{te_total} matchs.")
        if te_total and te_ok < te_total:
            _log(
                "ATTENTION: profils TE incomplets — relancer "
                "`ensure_te_profiles_for_prematch_csv` ou vérifier les échecs réseau TE."
            )
    except Exception as exc:
        _log(f"Vérification profils TE ignorée : {exc}")

    _log(f"Phase build terminée ({elapsed:.0f} s snapshot).")
    return 0


def run_telegram_phase(*, _log, source: str = "morning") -> int:
    env_name = (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower()
    if env_name != "prod":
        _log("Phase Telegram ignorée (PREPROD — envoi réservé à PROD).")
        return 0

    if source == "morning-sync":
        try:
            from scripts.od1p_publish import publish_1d1p_morning

            od1p = publish_1d1p_morning(source=source)
            _log(f"1 Day 1 Pick (TG + Discord) : {od1p}")
        except Exception as exc:
            _log(f"1 Day 1 Pick ERREUR : {exc}")

    if os.getenv("TELEGRAM_TOP5_AFTER_MORNING", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        _log("TELEGRAM_TOP5_AFTER_MORNING désactivé — Top 5 non envoyé.")
        return 0

    from scripts.live_snapshot import load_latest_live_snapshot

    matches, meta = load_latest_live_snapshot(max_age_sec=24 * 3600)
    if not matches:
        _log("Aucun snapshot disponible — lancez --build-only (02:00) d'abord.")
        return 1
    built = float((meta or {}).get("built_at") or 0)
    age_h = (time.time() - built) / 3600.0 if built > 0 else -1.0
    _log(
        f"Snapshot : {len(matches)} match(s), âge ~{age_h:.1f} h "
        f"(built_at={built:.0f})."
    )

    try:
        from scripts.telegram_runtime_cache import warm_telegram_runtime_cache

        warm_telegram_runtime_cache()
        _log("Cache Telegram rechargé depuis le snapshot.")
    except Exception as exc:
        _log(f"Warm cache Telegram ignoré : {exc}")

    try:
        from scripts.telegram_top5_notify import run_notify

        tg = run_notify(dry_run=False, source=source, interactive=True)
        n_chats = tg.get("sent") or 0
        _log(
            f"Telegram Top 5 envoyé : {tg.get('n_picks', 0)} pick(s) "
            f"→ {n_chats} chat(s)."
        )
    except Exception as exc:
        _log(f"Telegram Top 5 ERREUR: {exc}")
        return 1

    try:
        from scripts.telegram_channel_notify import run_channel_notify

        ch = run_channel_notify(dry_run=False)
        _log(f"Canal Telegram public : {ch}")
    except Exception as exc:
        _log(f"Canal Telegram public ignoré : {exc}")

    _log("Phase Telegram terminée.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    os.environ.setdefault("BETTINGHUD_ENV", "prod")

    ap = argparse.ArgumentParser(description="Pipeline matin BettingHUD")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--build-only",
        action="store_true",
        help="04:30 Paris : scrape + snapshot + report algo (pas de Telegram)",
    )
    mode.add_argument(
        "--telegram-only",
        action="store_true",
        help="Envoi Top 5 / 1D1P / canal (snapshot déjà construit)",
    )
    mode.add_argument(
        "--morning-publish",
        action="store_true",
        help="Chaîne complète sync tours → build → publications (rattrapage manuel)",
    )
    mode.add_argument(
        "--publish-only",
        action="store_true",
        help="05:00 Paris : publications uniquement (sync/build via crons précédents)",
    )
    ap.add_argument(
        "--source",
        default="morning",
        choices=("morning", "morning-sync"),
        help="Libellé Telegram pour --telegram-only (morning-sync = passe 05:00)",
    )
    args = ap.parse_args(argv)

    _install_streamlit_mock()

    if args.build_only:
        _log, log_path = _open_log("build")
        _log("Démarrage phase build (scrape + snapshot).")
        try:
            from scripts.morning_chain_state import (
                step_ok_today,
                tours_sync_in_progress,
                wait_for_step_ok,
            )

            if tours_sync_in_progress() or not step_ok_today("tours_sync"):
                _log("Sync tours 00:30 pas terminé — attente (max 60 min).")
                wait_for_step_ok("tours_sync", max_wait_sec=3600, log=_log)
        except Exception as exc:
            _log(f"Attente sync tours ignorée : {exc}")
        rc = run_build_phase(_log=_log)
        if rc == 0:
            try:
                from scripts.morning_chain_state import record_step
                from scripts.morning_orchestrator import validate_build

                build_ok = validate_build(_log=_log)
                record_step(
                    "build",
                    ok=build_ok,
                    rc=0 if build_ok else 1,
                    detail={"source": "build-only", "validated": build_ok},
                )
                if not build_ok:
                    _log("Post-check validate_build ÉCHEC après build 04:30.")
                    try:
                        from scripts.ops_alert_human import format_simple_ops
                        from scripts.ops_telegram_alert import send_ops_alert

                        _, body = format_simple_ops(
                            "Préparation snapshot (04:30) — contrôle final KO",
                            [
                                "Le snapshot a été construit mais ne passe pas les garde-fous qualité.",
                                "• Profils joueurs TE incomplets ou snapshot trop vieux",
                                "",
                                "<b>Impact</b> : la publication de 05:00 risque d'échouer ou de publier des picks incomplets.",
                            ],
                        )
                        send_ops_alert(
                            "Morning build validate FAIL",
                            body,
                            dedup_key="morning_build_validate_fail",
                        )
                    except Exception:
                        pass
                    rc = 1
            except Exception as exc:
                _log(f"Post-check validate_build ignoré : {exc}")
        _log(f"Fin (code {rc}). Journal : {log_path}")
        return rc

    if args.telegram_only:
        _log, log_path = _open_log("telegram" if args.source == "morning" else "telegram-sync")
        label = "Top 5 matin" if args.source == "morning" else "Publications matin 05:00"
        _log(f"Démarrage phase Telegram ({label}).")
        rc = run_telegram_phase(_log=_log, source=args.source)
        _log(f"Fin (code {rc}). Journal : {log_path}")
        return rc

    if args.publish_only:
        from scripts.morning_orchestrator import run_publish_only_chain

        _log, log_path = _open_log("publish-only")
        _log("Démarrage publications matin 05:00 (Top 5 / 1D1P / canal — sans sync).")
        rc = run_publish_only_chain(_log=_log)
        _log(f"Fin (code {rc}). Journal : {log_path}")
        return rc

    if args.morning_publish:
        from scripts.morning_orchestrator import run_publish_chain

        _log, log_path = _open_log("morning-publish")
        _log("Démarrage chaîne complète (sync → build → publications).")
        rc = run_publish_chain(_log=_log)
        _log(f"Fin (code {rc}). Journal : {log_path}")
        return rc

    # Comportement historique : tout en une passe
    _log, log_path = _open_log("full")
    _log("Démarrage pipeline complet (build + Telegram).")
    rc = run_build_phase(_log=_log)
    if rc != 0:
        _log(f"Build échoué (code {rc}) — Telegram non lancé.")
        return rc
    rc_tg = run_telegram_phase(_log=_log)
    _log(f"Fin (build OK, telegram code {rc_tg}). Journal : {log_path}")
    return rc_tg if rc_tg != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
