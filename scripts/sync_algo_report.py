#!/usr/bin/env python3
"""Synchronise le report journalier algo depuis le snapshot live courant."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

os.environ["BETTINGHUD_HEADLESS"] = "1"
os.environ.setdefault("BETTINGHUD_LIVE_DATA_DAEMON", "0")


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

    class _MockSessionState(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key) from None

        def __setattr__(self, key, value):
            self[key] = value

    st.session_state = _MockSessionState()
    st.cache_resource = _passthrough_cache
    st.cache_data = _passthrough_cache
    sys.modules["streamlit"] = st
    sar = MagicMock()
    sar.st_autorefresh = lambda *a, **k: None
    sys.modules["streamlit_autorefresh"] = sar


def main() -> int:
    _install_streamlit_mock()
    from app.dashboard import sync_algo_report_from_snapshot

    ev_pct = max(1.0, float(os.getenv("BETTINGHUD_LIVE_EV_THRESHOLD_PCT", "15")))
    n = sync_algo_report_from_snapshot(ev_threshold_pct=ev_pct, force=True)
    print(f"OK — {n} opportunité(s) synchronisée(s) depuis le snapshot live.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
