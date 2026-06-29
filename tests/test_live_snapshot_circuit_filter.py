"""Snapshot circuit filter — Option A: no Challenger/ITF by default."""
from __future__ import annotations

import pandas as pd

from app.dashboard import _filter_df_atp_wta_circuit_vectorized


def test_default_snapshot_excludes_challenger_and_itf():
    df = pd.DataFrame(
        [
            {"category": "ATP", "tournament": "Wimbledon"},
            {"category": "Challenger", "tournament": "Cary challenger"},
            {"category": "ATP", "tournament": "Cary challenger"},
            {"category": "ITF", "tournament": "Futures"},
            {"category": "WTA", "tournament": "Bad Homburg"},
        ]
    )
    out = _filter_df_atp_wta_circuit_vectorized(df, include_challengers=False)
    tournaments = set(out["tournament"].tolist())
    assert tournaments == {"Wimbledon", "Bad Homburg"}


def test_opt_in_challengers_keeps_challenger_category():
    df = pd.DataFrame(
        [
            {"category": "Challenger", "tournament": "Cary"},
            {"category": "ATP", "tournament": "Cary ITF qualif"},
        ]
    )
    out = _filter_df_atp_wta_circuit_vectorized(df, include_challengers=True)
    assert len(out) == 1
    assert out.iloc[0]["category"] == "Challenger"
