"""Tests centralisés infer_surface_category (surface_speed)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.surface_speed import infer_surface_category, resolve_tournament_surface
from scripts.tournament_surface_te import normalize_te_surface_label, parse_surface_from_te_html


def test_bad_homburg_is_grass():
    assert infer_surface_category("Bad Homburg Open") == "Grass"
    assert resolve_tournament_surface("Bad Homburg Open") == "Grass"


def test_hamburg_is_clay_not_grass():
    assert infer_surface_category("Hamburg ATP") == "Clay"
    assert resolve_tournament_surface("Hamburg ATP") == "Clay"


def test_wimbledon_is_grass():
    assert infer_surface_category("Wimbledon") == "Grass"


def test_te_parse_surface_clay():
    html = "<tr><th>Surface:</th><td>Clay</td></tr>"
    assert parse_surface_from_te_html(html) == "Clay"
    assert normalize_te_surface_label("Terre battue") == "Clay"
