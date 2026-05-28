#!/usr/bin/env python3
"""Rebuild snapshot Live Tracker (projection J+0 / J+1) hors Streamlit."""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

# Pas de jobs arrière-plan ni UI Streamlit pendant le rebuild CLI.
os.environ["BETTINGHUD_HEADLESS"] = "1"
os.environ.setdefault("BETTINGHUD_LIVE_DATA_DAEMON", "0")
os.environ.setdefault("BETTINGHUD_AUTO_SYNC_TOURS", "0")
os.environ.setdefault("BETTINGHUD_ENABLE_AUTO_ML_TRAIN_WEEKLY", "0")
os.environ.setdefault("BETTINGHUD_LIVE_PROJECTION_WARMUP", "0")


class _MockSessionState(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key, value):
        self[key] = value


def _passthrough_cache(*_cache_args, **_cache_kwargs):
    """Remplace st.cache_data / st.cache_resource (ignore ttl, show_spinner, etc.)."""

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


def main() -> int:
    _install_streamlit_mock()

    from scripts.live_snapshot import (
        FULL_SNAPSHOT_PATH,
        NEXTDAY_SNAPSHOT_PATH,
        SNAPSHOT_PATH,
        snapshot_build_in_progress,
    )

    from scripts.db_indexes import ensure_bettinghud_query_indexes

    from app.dashboard import (  # noqa: E402
        _purge_all_live_player_caches,
        _try_build_live_snapshot_if_missing,
    )

    ensure_bettinghud_query_indexes()

    if snapshot_build_in_progress():
        print("Un build est déjà en cours — abandon.", file=sys.stderr)
        return 2

    print("Vidage cache joueur + anciens snapshots…")
    _purge_all_live_player_caches()
    for path in (SNAPSHOT_PATH, FULL_SNAPSHOT_PATH, NEXTDAY_SNAPSHOT_PATH):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    os.environ.setdefault("BETTINGHUD_SYNC_FULL_AFTER_PREVIEW", "1")
    os.environ.setdefault("BETTINGHUD_LIVE_INCREMENTAL_ENRICH", "0")

    t0 = time.time()
    print("Construction projection complète (aujourd’hui + demain)…")
    ok = _try_build_live_snapshot_if_missing(
        identity_workers=None,
        label="rebuild-cli",
        force_full=True,
    )
    elapsed = time.time() - t0
    if not ok:
        print("Échec ou snapshot déjà à jour / verrou indisponible.", file=sys.stderr)
        return 1 if not ok else 0
    print(f"OK — snapshot enregistré ({elapsed:.0f} s).")
    try:
        from app.dashboard import sync_algo_report_from_snapshot

        n_sync = sync_algo_report_from_snapshot(force=True)
        print(f"[rebuild-cli] report algo synchronisé : {n_sync} opportunité(s).")
    except Exception as exc:
        print(f"[rebuild-cli] sync report algo ignorée : {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
