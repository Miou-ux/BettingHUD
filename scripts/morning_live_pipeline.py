#!/usr/bin/env python3
"""Pipeline matinal BettingHUD (scrape + snapshot, notifications Telegram séparées).

Phases (Europe/Paris, cron PROD) :
  - **02:00** : ``--build-only`` — scrape TE, snapshot full, report algo
  - **04:00** : ``--telegram-only`` — Top 5 Telegram (``TELEGRAM_TOP5_AFTER_MORNING=1``)

Sans argument : build + Telegram (comportement historique, déconseillé en prod).

Usage :
  py -3 scripts/morning_live_pipeline.py --build-only
  py -3 scripts/morning_live_pipeline.py --telegram-only
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
        from scripts.ingest_rankings_current import main as ingest_wta_rankings

        _log("Ingest classements WTA courants (Sackmann)…")
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
        from scripts.telegram_runtime_cache import invalidate_snapshot_cache

        invalidate_snapshot_cache()
        _log("Cache Telegram invalidé (snapshot à jour pour 04:00).")
    except Exception as exc:
        _log(f"Invalidation cache Telegram ignorée : {exc}")

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


def run_telegram_phase(*, _log) -> int:
    if os.getenv("TELEGRAM_TOP5_AFTER_MORNING", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        _log("TELEGRAM_TOP5_AFTER_MORNING désactivé — rien à envoyer.")
        return 0

    env_name = (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower()
    if env_name != "prod":
        _log("Telegram Top 5 ignoré (PREPROD — envoi réservé à PROD).")
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

        tg = run_notify(dry_run=False, source="morning")
        n_chats = tg.get("sent") or 0
        _log(
            f"Telegram Top 5 envoyé : {tg.get('n_picks', 0)} pick(s) "
            f"→ {n_chats} chat(s)."
        )
    except Exception as exc:
        _log(f"Telegram Top 5 ERREUR: {exc}")
        return 1

    _log("Phase Telegram terminée.")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    ap = argparse.ArgumentParser(description="Pipeline matin BettingHUD")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--build-only",
        action="store_true",
        help="02:00 Paris : scrape + snapshot + report algo (pas de Telegram)",
    )
    mode.add_argument(
        "--telegram-only",
        action="store_true",
        help="04:00 Paris : envoi Top 5 Telegram (snapshot déjà construit)",
    )
    args = ap.parse_args(argv)

    _install_streamlit_mock()

    if args.build_only:
        _log, log_path = _open_log("build")
        _log("Démarrage phase build (scrape + snapshot).")
        rc = run_build_phase(_log=_log)
        _log(f"Fin (code {rc}). Journal : {log_path}")
        return rc

    if args.telegram_only:
        _log, log_path = _open_log("telegram")
        _log("Démarrage phase Telegram (Top 5 matin).")
        rc = run_telegram_phase(_log=_log)
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
