#!/usr/bin/env python3
"""Pipeline matinal : scrape prematch + preview + enrichissement complet (headless)."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

os.environ["BETTINGHUD_HEADLESS"] = "1"
os.environ.setdefault("BETTINGHUD_LIVE_DATA_DAEMON", "0")
os.environ.setdefault("BETTINGHUD_AUTO_SYNC_TOURS", "0")
os.environ.setdefault("BETTINGHUD_ENABLE_AUTO_ML_TRAIN_WEEKLY", "0")
os.environ.setdefault("BETTINGHUD_LIVE_PROJECTION_WARMUP", "0")
os.environ.setdefault("BETTINGHUD_SYNC_FULL_AFTER_PREVIEW", "1")
os.environ.setdefault("BETTINGHUD_LIVE_INCREMENTAL_ENRICH", "0")
os.environ.setdefault("BETTINGHUD_LIVE_DELTA_ENRICH", "1")


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


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    log_dir = os.path.join(ROOT, "data", "cache", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, f"morning_pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    def _log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    _install_streamlit_mock()

    from scripts.db_indexes import ensure_bettinghud_query_indexes
    from scripts.live_snapshot import snapshot_build_in_progress
    from scripts.scraper_prematch import FlashscoreScraper

    from app.dashboard import _try_build_live_snapshot_if_missing  # noqa: E402

    if snapshot_build_in_progress():
        _log("Build déjà en cours — abandon.")
        return 2

    _log("Index SQLite…")
    ensure_bettinghud_query_indexes()

    try:
        from scripts.ingest_rankings_current import main as ingest_wta_rankings

        _log("Ingest classements WTA courants (Sackmann)…")
        ingest_wta_rankings()
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

    _log("Build snapshot (preview puis enrichissement synchrone)…")
    t1 = time.time()
    ok = _try_build_live_snapshot_if_missing(
        identity_workers=None,
        label="morning-pipeline",
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

    if os.getenv("TELEGRAM_TOP5_AFTER_MORNING", "").strip().lower() in ("1", "true", "yes"):
        env_name = (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower()
        if env_name != "prod":
            _log("Telegram Top 5 ignore (PREPROD — envoi reserve a PROD).")
        else:
            try:
                from scripts.telegram_top5_notify import run_notify

                tg = run_notify(dry_run=False, source="morning")
                _log(f"Telegram Top 5 envoye : {tg.get('n_picks', 0)} pick(s).")
            except Exception as exc:
                _log(f"Telegram Top 5 ignore : {exc}")

    _log(f"Pipeline terminé ({elapsed:.0f} s build). Journal : {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
