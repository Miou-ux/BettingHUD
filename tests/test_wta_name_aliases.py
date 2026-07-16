#!/usr/bin/env python3
"""Tests alias WTA : remap ID lors du rename Quevedo → Lys."""
from __future__ import annotations

import pandas as pd

from scripts.wta_name_aliases import apply_aliases_to_dataframe


def test_alias_remaps_loser_id_to_canonical():
    df = pd.DataFrame(
        [
            {
                "tourney_date": 20260710,
                "tourney_name": "Bastad WTA",
                "winner_name": "Waltert S.",
                "winner_id": 215899,
                "loser_name": "lys e.",
                "loser_id": 220332,
            },
            {
                "tourney_date": 20260710,
                "tourney_name": "Bastad WTA",
                "winner_name": "Waltert S.",
                "winner_id": 215899,
                "loser_name": "Quevedo K.",
                "loser_id": 259733,
            },
        ]
    )
    name_to_id = {"lys e.": 220332, "waltert s.": 215899, "quevedo k.": 259733}
    n = apply_aliases_to_dataframe(df, name_to_id=name_to_id)
    assert n >= 1
    assert str(df.loc[1, "loser_name"]).lower() == "lys e."
    assert int(df.loc[1, "loser_id"]) == 220332
    assert int(df.loc[0, "loser_id"]) == 220332


def test_alias_fixes_already_renamed_wrong_id():
    df = pd.DataFrame(
        [
            {
                "tourney_date": 20260710,
                "tourney_name": "Bastad WTA",
                "winner_name": "Waltert S.",
                "winner_id": 215899,
                "loser_name": "lys e.",
                "loser_id": 259733,  # mauvais ID Quevedo
            }
        ]
    )
    name_to_id = {"lys e.": 220332, "waltert s.": 215899}
    apply_aliases_to_dataframe(df, name_to_id=name_to_id)
    assert int(df.loc[0, "loser_id"]) == 220332
