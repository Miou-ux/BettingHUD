"""
Tests régression V4 → V4.5 : Human Factors (style KMeans matchup, voyage, clutch 52sem).

Usage (depuis la racine du dépôt) :
    py -3 -m unittest tests.test_v45_human_factors -v

Tests optionnels lent / bundle / DB :
    set BETTINGHUD_TEST_ML_BUNDLE=1
    py -3 -m unittest tests.test_v45_human_factors.TestV45BundlePrediction -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

# Racine repo (parents[1] depuis tests/)
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_scripts = _ROOT / "scripts"
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

from sklearn.cluster import KMeans  # noqa: E402

from ml_model import (  # noqa: E402
    TennisMLModel,
    assign_player_style,
    build_style_semantic_rank_map,
    style_matchup_bias_from_ranks,
)
from tournament_geo import haversine_km, tournament_site_lon_lat_tz  # noqa: E402
from stats_engine import (  # noqa: E402  # pylint: disable=wrong-import-position
    clutch_score_52weeks,
    tactical_vector_52weeks,
    travel_fatigue_index_from_history,
)


class TestV45FeatureSchema(unittest.TestCase):
    """Le vecteur ML inclut bien les trois colonnes v4.5 après les traits v4 précédents."""

    def test_features_contains_v45_columns(self):
        m = TennisMLModel(db_path=str(_ROOT / "data" / "bettinghud.db"))
        names = list(m.features)
        self.assertGreaterEqual(len(names), 43, "liste de features doit couvrir v4.5")
        self.assertEqual(names[-3], "style_matchup_bias")
        self.assertEqual(names[-2], "travel_fatigue_index")
        self.assertEqual(names[-1], "clutch_diff")

    def test_model_path_targets_v45(self):
        m = TennisMLModel()
        self.assertIn("v45", m.model_path)
        self.assertIn("v45", m.feature_plot_path)

    def test_v4_human_factors_not_removed(self):
        m = TennisMLModel()
        for required in ("style_advantage_score", "clutch_index_diff", "pre_slam_fatigue"):
            self.assertIn(required, m.features, f"V4 doit conserver `{required}`")


class TestTournamentGeo(unittest.TestCase):
    def test_haversine_melbourne_london_far(self):
        lat_m, lon_m = -37.822, 144.981
        lat_l, lon_l = 51.435, -0.214  # Wimbledon
        d = haversine_km(lat_m, lon_m, lat_l, lon_l)
        self.assertGreater(d, 4000.0, "Pacifique ↔ Europe doit dépasser 4000 km")

    def test_roland_garros_resolves_paris_tz(self):
        la, lo, tz = tournament_site_lon_lat_tz("French Open Roland Garros")
        self.assertAlmostEqual(la, 48.848, delta=2.0)
        self.assertAlmostEqual(lo, 2.249, delta=2.0)
        self.assertEqual(tz, 1)


class TestStyleMatchupBias(unittest.TestCase):
    """Counter-puncher (rank 3) vs Big Server (rank 0) : bonus sur lent, asymétrique P1/P2."""

    def test_cp_vs_bs_slow_exceeds_fast(self):
        slow = style_matchup_bias_from_ranks(3, 0, surface_slow=True)
        fast = style_matchup_bias_from_ranks(3, 0, surface_slow=False)
        self.assertGreater(slow, fast)

    def test_antisymmetric_swap_ranks_slow(self):
        r1, r2 = 1, 2
        slow = True
        a = style_matchup_bias_from_ranks(r1, r2, slow)
        b = style_matchup_bias_from_ranks(r2, r1, slow)
        self.assertAlmostEqual(a + b, 0.0, places=9)

    def test_build_style_semantic_rank_map_orders_by_mean_ace_desc(self):
        rng = np.random.default_rng(42)
        centers = np.array(
            [
                [0.20, 0.70, 0.60, 0.82],
                [0.10, 0.65, 0.58, 0.76],
                [0.06, 0.62, 0.62, 0.72],
                [0.04, 0.60, 0.65, 0.68],
            ],
            dtype=float,
        )
        X = np.vstack([centers[i] + rng.normal(0.0, 5e-4, (35, 4)) for i in range(4)])
        km = KMeans(n_clusters=4, random_state=42, n_init=15, algorithm="lloyd").fit(X)
        rank_map, cal = build_style_semantic_rank_map(km, X)
        means = np.asarray(cal["mean_ace_by_raw_cluster_id"], dtype=float)
        hi = int(np.argmax(means))
        lo = int(np.argmin(means))
        self.assertEqual(rank_map[hi], 0)
        self.assertEqual(rank_map[lo], 3)


class TestAssignPlayerStyleIntegration(unittest.TestCase):
    """KMeans fit minimal + assign_player_style sur un point proche d’un centroïde."""

    def test_assign_maps_to_semantic_labels(self):
        X = np.array(
            [
                [0.20, 0.70, 0.60, 0.82],
                [0.10, 0.65, 0.58, 0.76],
                [0.06, 0.62, 0.62, 0.72],
                [0.04, 0.60, 0.65, 0.68],
            ],
            dtype=float,
        )
        km = KMeans(n_clusters=4, random_state=42, n_init=15, algorithm="lloyd").fit(X)
        rank_map, _cal = build_style_semantic_rank_map(km, X)
        rk, lbl = assign_player_style(np.array([0.205, 0.71, 0.605, 0.825]), km, rank_map)
        self.assertEqual(lbl, "Big Server")
        self.assertEqual(rk, 0)


class TestStatsEngineHumanFactors(unittest.TestCase):
    def test_tactical_vector_empty_defaults(self):
        empty = pd.DataFrame()
        a, b, c, h = tactical_vector_52weeks(empty, "x", pd.Timestamp("2026-06-01"), min_pts=5)
        self.assertAlmostEqual(a, 0.08)
        self.assertAlmostEqual(b, 0.62)

    def test_clutch_three_equal_rates(self):
        ref = pd.Timestamp("2026-06-01")
        rows = []
        pk = "doe j"
        for day in pd.date_range(ref - pd.Timedelta(days=10), periods=5, freq="D"):
            rows.append(
                {
                    "tourney_date": day,
                    "w_key": pk,
                    "l_key": "other x",
                    "score": "6-4 6-4",
                    "w_bpSaved": 3,
                    "w_bpFaced": 10,
                    "l_bpSaved": 0,
                    "l_bpFaced": 10,
                }
            )
        df = pd.DataFrame(rows)
        sc = clutch_score_52weeks(df, pk, ref, min_pts=3)
        self.assertGreater(sc, 0.0)
        self.assertLessEqual(sc, 1.0)

    def test_travel_jetlag_melbourne_then_wimbledon_short_rest(self):
        ref = pd.Timestamp("2026-07-06")
        pk = "test player"
        rows = pd.DataFrame(
            [
                {
                    "tourney_date": ref - pd.Timedelta(days=3),
                    "tourney_name": "Australian Open",
                    "w_key": pk,
                }
            ]
        )
        pen = travel_fatigue_index_from_history(rows, pk, "wimbledon championships", ref)
        self.assertGreaterEqual(pen, 0.049, "Grand saut + repos < 4j → malus ~0.05")

    def test_travel_rest_four_days_no_penalty(self):
        ref = pd.Timestamp("2026-07-06")
        pk = "test player"
        rows = pd.DataFrame(
            [
                {
                    "tourney_date": ref - pd.Timedelta(days=4),
                    "tourney_name": "Australian Open",
                    "w_key": pk,
                }
            ]
        )
        pen = travel_fatigue_index_from_history(rows, pk, "Wimbledon", ref)
        self.assertEqual(pen, 0.0)


class TestV45TemporalTravelLogic(unittest.TestCase):
    """Même logique que _travel_pen dans ml_model pour rest / distance / TZ."""

    def test_rule_matches_ml_doc(self):
        clat, clon, ctz = tournament_site_lon_lat_tz("Wimbledon")
        prev = tournament_site_lon_lat_tz("Australian Open")
        plat, plon, ptz = prev
        dist_km = haversine_km(float(plat), float(plon), float(clat), float(clon))
        rest = 3
        cond = (
            rest < 4
            and (
                dist_km > 4000.0
                or abs(int(ctz) - int(ptz)) > 4
            )
        )
        self.assertTrue(cond, "Scénario doc AO→Wimbledon, 3j repos doit être flag fatigue")


def _bundle_path():
    return _ROOT / "models" / "xgb_model_tml_v45.pkl"


@unittest.skipUnless(os.getenv("BETTINGHUD_TEST_ML_BUNDLE") == "1", "bundle + DB lent ; BETTINGHUD_TEST_ML_BUNDLE=1")
class TestV45BundlePrediction(unittest.TestCase):
    def test_bundle_loads_three_features_predict_keys(self):
        if not _bundle_path().is_file():
            self.skipTest("Bundle v45 absent — lancer scripts/update_model_tml.py")
        m = TennisMLModel(db_path=str(_ROOT / "data" / "bettinghud.db"))
        m.model_path = str(_bundle_path())
        pred = m.predict_match(
            surface="Clay",
            p1_rank=10,
            p2_rank=20,
            p1_age=25,
            p2_age=26,
            p1_ht=185,
            p2_ht=183,
            p1_pts=3000,
            p2_pts=2000,
            p1_tac_ace=0.12,
            p1_tac_f1_pct=0.68,
            p1_tac_bp_saved_pct=0.55,
            p1_tac_hold_pct=0.78,
            p2_tac_ace=0.07,
            p2_tac_f1_pct=0.62,
            p2_tac_bp_saved_pct=0.58,
            p2_tac_hold_pct=0.74,
            p1_travel_penalty_index=0.05,
            p2_travel_penalty_index=0.0,
            p1_clutch52=0.55,
            p2_clutch52=0.50,
            tournament_name="Monte Carlo Masters",
            tour="ATP",
        )
        snap = pred.get("feature_snapshot") or {}
        for key in ("human_p1_style", "human_p2_style", "p1_jetlag_alert", "p1_clutch52", "p2_clutch52"):
            self.assertIn(key, snap, snap.keys())


if __name__ == "__main__":
    unittest.main()
