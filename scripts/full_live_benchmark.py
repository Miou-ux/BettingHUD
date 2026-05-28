#!/usr/bin/env python3
"""Run complet mesuré du Live Tracker.

Objectif :
- rescraper les matchs prematch Tennis Explorer ;
- forcer la mise à jour des profils TE pour tous les joueurs ATP/WTA live ;
- reconstruire le snapshot complet ;
- produire un journal exploitable pour identifier les goulots.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

os.environ["BETTINGHUD_HEADLESS"] = "1"
os.environ.setdefault("BETTINGHUD_LIVE_DATA_DAEMON", "0")
os.environ.setdefault("BETTINGHUD_AUTO_SYNC_TOURS", "0")
os.environ.setdefault("BETTINGHUD_ENABLE_AUTO_ML_TRAIN_WEEKLY", "0")
os.environ.setdefault("BETTINGHUD_LIVE_PROJECTION_WARMUP", "0")
os.environ.setdefault("BETTINGHUD_SYNC_FULL_AFTER_PREVIEW", "1")
os.environ.setdefault("BETTINGHUD_LIVE_TWO_PHASE_SNAPSHOT", "1")
os.environ.setdefault("BETTINGHUD_LIVE_BUILD_FAST_PREVIEW", "1")
os.environ.setdefault("BETTINGHUD_MAX_LIVE_MATCHES_BUILD", "0")
os.environ.setdefault("BETTINGHUD_MAX_PROFILE_FETCH", "1000")
os.environ.setdefault("BETTINGHUD_PROFILE_SCRAPE_WORKERS", "8")
os.environ.setdefault("BETTINGHUD_IDENTITY_WORKERS", "10")
os.environ.setdefault("BETTINGHUD_PLAYER_FEATURES_WORKERS", "8")
os.environ.setdefault("BETTINGHUD_PERF_LOG_LIVE_BUILD", "1")


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


def _install_streamlit_mock() -> None:
    st = MagicMock()
    st.session_state = _MockSessionState()
    st.cache_resource = _passthrough_cache
    st.cache_data = _passthrough_cache
    st.sidebar = MagicMock()
    st.columns = lambda n: [MagicMock() for _ in range(n)]
    st.tabs = lambda labels: [MagicMock() for _ in labels]
    st.progress = lambda *a, **k: MagicMock()
    st.status = lambda *a, **k: MagicMock(__enter__=lambda s: s, __exit__=lambda *a: None)
    sys.modules["streamlit"] = st
    sar = MagicMock()
    sar.st_autorefresh = lambda *a, **k: None
    sys.modules["streamlit_autorefresh"] = sar


def _rm(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def main() -> int:
    log_dir = os.path.join(ROOT, "data", "cache", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, f"full_live_benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    timings: list[tuple[str, float]] = []

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def timed(label: str, start: float) -> None:
        elapsed = time.time() - start
        timings.append((label, elapsed))
        log(f"{label}: {elapsed:.1f}s")

    _install_streamlit_mock()

    from scripts.db_indexes import ensure_bettinghud_query_indexes
    from scripts.live_snapshot import (
        BUILD_LOCK_PATH,
        BUILD_PROGRESS_PATH,
        SNAPSHOT_PATH,
        SNAPSHOT_META_PATH,
        clear_live_build_progress,
        snapshot_build_in_progress,
    )
    from scripts.scraper_prematch import FlashscoreScraper

    from app.dashboard import (  # noqa: E402
        _load_prematch_df_for_live,
        _prematch_csv_signature,
        _purge_all_live_player_caches,
        _try_build_live_snapshot_if_missing,
        profile_scraper,
    )

    total_start = time.time()
    log(f"Journal: {log_path}")
    log("Nettoyage verrou/progression stale + snapshot précédent…")
    _rm(BUILD_LOCK_PATH)
    _rm(BUILD_PROGRESS_PATH)
    _rm(SNAPSHOT_PATH)
    _rm(SNAPSHOT_META_PATH)
    clear_live_build_progress()

    t = time.time()
    ensure_bettinghud_query_indexes()
    timed("Index SQLite", t)

    t = time.time()
    log("Vidage caches joueurs live (identité/stats/features)…")
    _purge_all_live_player_caches()
    timed("Purge caches joueurs live", t)

    t = time.time()
    log("Scrape Tennis Explorer prematch (aujourd'hui + demain)…")
    scraped = asyncio.run(FlashscoreScraper().get_today_matches_and_odds())
    log(f"Scrape brut: {len(scraped or [])} lignes")
    timed("Scrape prematch TE", t)

    csv_path, _ = _prematch_csv_signature()
    df = _load_prematch_df_for_live(csv_path)
    urls: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        for col in ("p1_url", "p2_url"):
            raw = row.get(col)
            if raw is None:
                continue
            url = str(raw).strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    log(f"CSV live filtré: {len(df)} matchs, {len(urls)} profils TE uniques")

    t = time.time()
    workers = max(1, min(16, int(os.getenv("BETTINGHUD_PROFILE_SCRAPE_WORKERS", "8"))))
    ok_profiles = 0
    failed_profiles = 0

    def refresh_profile(url: str):
        return url, profile_scraper.scrape_profile(url, force_refresh=True)

    log(f"Rafraîchissement forcé profils TE ({workers} workers)…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(refresh_profile, u) for u in urls]
        for idx, fut in enumerate(as_completed(futures), start=1):
            try:
                _, prof = fut.result()
                if prof:
                    ok_profiles += 1
                else:
                    failed_profiles += 1
            except Exception:
                failed_profiles += 1
            if idx % 25 == 0 or idx == len(futures):
                log(f"Profils TE: {idx}/{len(futures)} traités")
    log(f"Profils TE OK={ok_profiles}, échecs/vides={failed_profiles}")
    timed("Refresh forcé profils TE", t)

    if snapshot_build_in_progress():
        log("Build lock inattendu après nettoyage ; abandon.")
        return 2

    t = time.time()
    log("Build snapshot complet (preview puis full synchrone)…")
    ok = _try_build_live_snapshot_if_missing(identity_workers=None, label="full-benchmark")
    timed("Build snapshot preview+full", t)
    if not ok:
        log("Build snapshot échoué.")
        return 1

    timed("TOTAL", total_start)
    log("Résumé timings:")
    for label, elapsed in timings:
        log(f"  - {label}: {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
