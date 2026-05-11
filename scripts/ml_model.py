import os
import sqlite3
import re
import sys
from collections import defaultdict, deque
from typing import Optional, Tuple

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import accuracy_score, brier_score_loss, classification_report
from sklearn.cluster import KMeans
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

_ML_DIR = os.path.dirname(os.path.abspath(__file__))
if _ML_DIR not in sys.path:
    sys.path.insert(0, _ML_DIR)
from tournament_geo import haversine_km, tournament_site_lon_lat_tz  # noqa: E402
from micro_elo_engine import run_micro_elo_scan  # noqa: E402
from surface_speed import (  # noqa: E402
    effective_surface_speed_cpi,
    infer_outdoor,
    lookup_surface_speed,
    weather_impact_scalars,
)
from value_detector import ValueDetector  # noqa: E402

# Rang sémantique 0->3 après tri déterministe (moyenne Ace% dans le cluster sur les points d'ajustement) :
STYLE_SEMANTIC_LABELS = (
    "Big Server",  # 0 — plus forte moyenne Ace%
    "Aggressive Baseliner",  # 1 — 2e par Ace% (référence attendue : 1st won % relativement haute au sein du jeu WTA/ATP varié)
    "Tactical / Slicer",  # 2 — profil intermédiaire
    "Counter-Puncher",  # 3 — plus faible moyenne Ace% (référence : résilience BP / jeu de contenance dans la matrice de matchup)
)


def build_style_semantic_rank_map(
    km: KMeans,
    X_fit: np.ndarray,
) -> Tuple[np.ndarray, dict]:
    """
    Après kmeans.fit(X_fit) : pour chaque id brut de cluster sklearn, attribue un rang sémantique
    stable 0..k-1 en triant par **moyenne de la colonne Ace%** (première coordonnée) sur les points
    qui lui sont assignés (`labels_`). En cas de cluster vide, repli sur l’Ace centroïdale.

    En cas d’égalité flottante : tri secondaire centroïde Ace décroissant, puis **id brut croissant**
    pour une reproductibilité totale à données fixées avec `random_state=42`.

    Retourne (rank_map, calibration_dict_jsonable) où `rank_map[cid_brut]` = rang sémantique.
    """
    X = np.asarray(X_fit, dtype=float)
    labels = np.asarray(km.labels_, dtype=int)
    k = int(km.n_clusters)
    mean_aces = np.zeros(k, dtype=float)
    counts = np.zeros(k, dtype=int)
    cent_ace = np.asarray(km.cluster_centers_[:, 0], dtype=float)

    for cid in range(k):
        sel = labels == cid
        counts[cid] = int(np.sum(sel))
        if counts[cid] == 0:
            mean_aces[cid] = float(cent_ace[cid])
        else:
            mean_aces[cid] = float(np.mean(X[sel, 0]))

    order_cids = sorted(range(k), key=lambda c: (-mean_aces[c], -float(cent_ace[c]), int(c)))
    rank_map = np.empty(k, dtype=int)
    for semantic_r, cid in enumerate(order_cids):
        rank_map[int(cid)] = int(semantic_r)

    calibration = {
        "mean_ace_by_raw_cluster_id": mean_aces.tolist(),
        "centroid_ace_col0_by_raw_cluster_id": cent_ace.tolist(),
        "count_assignments_on_fit_rows": counts.tolist(),
        "deterministic_semantic_sort_raw_cluster_ids_desc": order_cids,
        "style_semantic_labels_rank_0_is_big_server": list(STYLE_SEMANTIC_LABELS),
    }
    return rank_map, calibration


def assign_player_style(
    stats_52w: np.ndarray,
    km: KMeans,
    rank_of_cluster: np.ndarray,
) -> Tuple[int, str]:
    """Retourne (semantic_rank, label) à partir du vecteur tactique 52s [ace, 1st%, bp_saved, hold]."""
    v = np.asarray(stats_52w, dtype=float).reshape(1, -1)
    cid = int(km.predict(v)[0])
    rk = int(rank_of_cluster[cid])
    rk = max(0, min(len(STYLE_SEMANTIC_LABELS) - 1, rk))
    return rk, STYLE_SEMANTIC_LABELS[rk]


def style_matchup_bias_from_ranks(rank_p1: int, rank_p2: int, surface_slow: bool) -> float:
    """Pondère le matchup style×surface (antisymétrique en (p1,p2))."""
    r1, r2 = int(rank_p1), int(rank_p2)
    r1 = max(0, min(3, r1))
    r2 = max(0, min(3, r2))
    base = np.zeros((4, 4), dtype=float)
    slow = np.zeros((4, 4), dtype=float)
    # Petit edge de base (symétrique antisymétrisé ci-dessous).
    base[3, 0], base[0, 3] = 0.012, -0.012
    base[3, 1], base[1, 3] = 0.007, -0.007
    base[1, 2], base[2, 1] = 0.005, -0.005
    base[2, 0], base[0, 2] = -0.004, 0.004
    # Bonus contre-puncher vs gros serveur sur surface lente (consigne utilisateur).
    slow[3, 0], slow[0, 3] = 0.028, -0.028
    slow[2, 0], slow[0, 2] = 0.010, -0.010
    s = 1.0 if surface_slow else 0.0
    v = (base[r1, r2] - base[r2, r1]) + s * (slow[r1, r2] - slow[r2, r1])
    return float(np.clip(v, -0.08, 0.08))


def style_interaction_key(p1_style: str, p2_style: str, surface: str) -> str:
    s1 = str(p1_style or "Unknown").strip() or "Unknown"
    s2 = str(p2_style or "Unknown").strip() or "Unknown"
    sf = str(surface or "Hard").strip().title() or "Hard"
    return f"{s1}_vs_{s2}_{sf}"


def _normalize_style_label(style_name: str) -> str:
    s = str(style_name or "").strip().lower().replace("  ", " ")
    if s in {"big server", "big_server"}:
        return "Big Server"
    if s in {"aggressive baseliner", "aggressive", "aggressive_baseliner"}:
        return "Aggressive"
    if s in {"tactical / slicer", "tactical/slicer", "tactical slicer"}:
        return "Tactical/Slicer"
    if s in {"counter-puncher", "counter puncher", "counter_puncher"}:
        return "Counter-Puncher"
    return "Unknown"


# (Circuit, P1_Style, P2_Style, Slow_Surface) -> winrate delta prior (P1 perspective).
TACTICAL_PRIORS = {
    # ATP: service dominance patterns.
    ("ATP", "Big Server", "Counter-Puncher", True): -0.10,
    ("ATP", "Big Server", "Counter-Puncher", False): +0.08,
    ("ATP", "Counter-Puncher", "Aggressive", True): +0.06,
    # WTA: return/rhythm patterns.
    ("WTA", "Aggressive", "Counter-Puncher", True): -0.05,
    ("WTA", "Aggressive", "Counter-Puncher", False): +0.07,
    ("WTA", "Tactical/Slicer", "Aggressive", True): +0.08,
}


def compute_matchup_synergy(
    p1_style: str,
    p2_style: str,
    tour_label: str,
    surface: str,
    surface_speed: float,
    style_surface_winrate_index: Optional[dict] = None,  # key: "TOUR|style_vs_style_surface"
) -> dict:
    """Bias bayésien: prior métier + empirical style matchup par circuit/surface."""
    p1s = _normalize_style_label(p1_style)
    p2s = _normalize_style_label(p2_style)
    surf = str(surface or "Hard").strip().title() or "Hard"
    speed = float(surface_speed) if surface_speed is not None else 0.75
    is_slow = (surf.lower() == "clay") or (speed < 0.65)
    tour_u = (str(tour_label or "ATP").upper() if tour_label else "ATP")
    if tour_u not in {"ATP", "WTA"}:
        tour_u = "ATP"

    key_local = style_interaction_key(p1s, p2s, surf)
    key = f"{tour_u}|{key_local}"
    stats = (style_surface_winrate_index or {}).get(key, {})
    n = int(stats.get("n", 0) or 0)
    wr = float(stats.get("winrate", 0.5) or 0.5)
    wr = float(np.clip(wr, 0.0, 1.0))
    prior_value = float(TACTICAL_PRIORS.get((tour_u, p1s, p2s, bool(is_slow)), 0.0))
    empirical_delta = float(wr - 0.5)
    # weight = 1 / (1 + n/30). Unknown matchup -> full prior.
    weight = 1.0 / (1.0 + (float(n) / 30.0))
    bias = float(weight * prior_value + (1.0 - weight) * empirical_delta)

    return {
        "style_interaction_key": key,
        "style_prior_value": prior_value,
        "style_is_slow_surface": bool(is_slow),
        "style_surface_winrate": wr,
        "style_surface_samples": n,
        "style_matchup_bias": float(np.clip(bias, -0.16, 0.16)),
    }


def resolve_segment_brier_score(ml: object, seg_key: object) -> float:
    """Brier du segment (test) ou repli global — utilisable depuis le dashboard même si
    Streamlit a mis en cache une instance de `TennisMLModel` créée avant l’ajout de
    la méthode `segment_brier_for_key` sur la classe.
    """
    try:
        loader = getattr(ml, "_load_bundle_if_needed", None)
        if callable(loader):
            loader()
    except Exception:
        pass
    sk = str(seg_key or "").strip()
    d = getattr(ml, "segment_brier_scores", None) or {}
    if sk and sk in d:
        return float(d[sk])
    return float(getattr(ml, "global_test_brier", 0.12))


class TennisMLModel:
    def __init__(self, db_path="data/bettinghud.db"):
        self.db_path = db_path
        self.model = None
        self.model_clay = None
        self.model_segments = {}
        self.player_elo = {}
        self.player_surface_elo = {}  # legacy bundle compat (unused in Micro-Elo path)
        self.player_name_elo = {}
        self.player_name_surface_elo = {}
        self.player_surface_count = {}
        self.player_surface_count_name = {}
        self.player_last_seen = {}
        self.player_last_seen_name = {}
        # Micro-Elo (service / return) — globals + per-surface parallel tracks
        self.player_service_elo = {}
        self.player_return_elo = {}
        self.player_service_elo_surface = {}
        self.player_return_elo_surface = {}
        self.player_surface_micro_count = {}
        self.player_name_service_elo = {}
        self.player_name_return_elo = {}
        self.segment_train_sizes = {}
        # Tunables
        self.elo_decay_tau_days = 365.0
        self.surface_blend_n0 = 30.0
        self.micro_elo_scale = 200.0
        self.micro_elo_k_serve = 0.45
        # Tennis Abstract surface speed index (~0.55 slow clay … ~0.95 fast grass)
        self.micro_speed_baseline = 0.75
        self.micro_speed_alpha = 0.35
        self.segment_blend_weight = 0.7
        self.segment_brier_scores = {}
        self.global_test_brier = 0.12
        self.style_kmeans: Optional[KMeans] = None
        self.style_rank_map = None  # np.ndarray length K : brut sklearn -> rang sémantique 0..K-1
        self.style_semantic_calibration: Optional[dict] = None  # mean_aces par brut, logs entraînement / bundle
        self.style_surface_winrate_index: dict = {}
        # ~43 features — v4.5 Human Factors: style KMeans, voyage, clutch 52s
        self.features = [
            "surface_encoded",
            "tournament_level_encoded",
            "tour_encoded",
            "rank_diff",
            "age_diff",
            "ht_diff",
            "points_diff",
            "service_elo_diff",
            "return_elo_diff",
            "speed_affinity",
            "speed_performance_delta",
            "serve_speed_interaction",
            "wta_weighted_advantage",
            "wta_speed_power_impact",
            "wta_break_point_resilience",
            "wins_last7d_diff",
            "three_setters_last14d_diff",
            "last_round_reached_diff",
            "momentum5_diff",
            "form90_surface_diff",
            "second_srv_ratio3_diff",
            "hold_surface_diff",
            "break_surface_diff",
            "first_srv_win10_diff",
            "bp_conv10_diff",
            "dominance_ratio_diff",
            "elo_surface_recent_diff",
            "hand_diff",
            "h2h_ratio",
            "h2h_significant",
            "style_advantage_score",
            "clutch_index_diff",
            "inactivity_decay_weight",
            "home_adv_diff",
            "altitude",
            "indoor",
            "humidity_impact",
            "temperature_impact",
            "market_sentiment_signal",
            "points_defending_pct",
            "pre_slam_fatigue",
            "style_drift_detected",
            "style_cluster_distance_diff",
            "style_matchup_bias",
            "travel_fatigue_index",
            "style_cross_surface_impact",
            "clutch_diff",
        ]
        self.model_path = "models/xgb_model_tml_v45.pkl"
        self.feature_plot_path = "models/feature_importance_tml_v45.png"

    @staticmethod
    def _expected_score(r_a, r_b):
        return 1.0 / (1.0 + (10.0 ** ((r_b - r_a) / 400.0)))

    @staticmethod
    def _encode_hand(hand_code):
        if hand_code == "R":
            return 1.0
        if hand_code == "L":
            return -1.0
        return 0.0

    @staticmethod
    def _pid_key(pid):
        if pd.isna(pid):
            return None
        return str(pid).strip()

    @staticmethod
    def _name_key(name):
        n = str(name or "").lower().strip()
        n = re.sub(r"[^a-z0-9 ]+", " ", n)
        n = re.sub(r"\s+", " ", n)
        if not n:
            return None
        parts = [p for p in n.split(" ") if p]
        if not parts:
            return None
        # canonical "lastname initial"
        if len(parts) == 2 and len(parts[1]) == 1:
            return f"{parts[0]} {parts[1]}"
        last = parts[-2] if len(parts) >= 2 and len(parts[-1]) == 1 else parts[-1]
        initial = parts[-1][0] if len(parts[-1]) > 0 else ""
        if len(parts) >= 2 and len(parts[-1]) == 1:
            initial = parts[-1]
        return f"{last} {initial}".strip()

    @staticmethod
    def _aligned_index_name_key(name):
        """Même logique que les index `stats_engine` / Sackmann (Prénom Nom -> Nom I.)."""
        try:
            from scripts.player_identity import canonical_name, to_lastname_initial

            raw = str(name or "").strip()
            if not raw:
                return None
            c = canonical_name(to_lastname_initial(raw))
            return c if c else TennisMLModel._name_key(raw)
        except Exception:
            return TennisMLModel._name_key(name)

    def _get_refined_matchup_bias(
        self,
        *,
        p1_style_name: str,
        p2_style_name: str,
        surface: str,
        surface_speed: float,
        tour_label: str,
    ) -> dict:
        return compute_matchup_synergy(
            p1_style=p1_style_name,
            p2_style=p2_style_name,
            tour_label=tour_label,
            surface=surface,
            surface_speed=surface_speed,
            style_surface_winrate_index=getattr(self, "style_surface_winrate_index", {}) or {},
        )

    @staticmethod
    def _safe_second_srv_ratio(points_won_2nd, svpt, first_in):
        if pd.isna(points_won_2nd) or pd.isna(svpt) or pd.isna(first_in):
            return np.nan
        denom = float(svpt) - float(first_in)
        if denom <= 0:
            return np.nan
        return float(points_won_2nd) / denom

    @staticmethod
    def _infer_sets_from_score(score_text):
        if not isinstance(score_text, str):
            return 0
        cleaned = score_text
        cleaned = cleaned.split("RET")[0].split("W/O")[0].split("DEF")[0]
        import re
        return len(re.findall(r"\d+\s*-\s*\d+", cleaned))

    @staticmethod
    def _infer_tiebreaks_from_score(score_text):
        import re
        if not isinstance(score_text, str):
            return 0, 0, 0
        cleaned = score_text.split("RET")[0].split("W/O")[0].split("DEF")[0]
        sets = re.findall(r"(\d+)\s*-\s*(\d+)", cleaned)
        w_tb = 0
        l_tb = 0
        for a, b in sets:
            try:
                sa = int(a)
                sb = int(b)
            except Exception:
                continue
            if sa == 7 and sb == 6:
                w_tb += 1
            elif sa == 6 and sb == 7:
                l_tb += 1
        return w_tb, l_tb, (w_tb + l_tb)

    @staticmethod
    def _safe_ratio(num, den, default=np.nan):
        try:
            num = float(num)
            den = float(den)
            if den <= 0:
                return default
            return num / den
        except Exception:
            return default

    @staticmethod
    def _inactivity_decay(days_rest):
        d = max(0.0, float(days_rest))
        if d <= 45.0:
            return 1.0
        # décroissance progressive après 45 jours d'absence
        return max(0.65, float(np.exp(-(d - 45.0) / 120.0)))

    @staticmethod
    def _round_depth(round_str):
        """Convert a round label to a numeric depth (1=earliest qualifying, 9=tournament won).

        Higher = deeper run. Returns 0 if unknown — used only as a relative scale, so
        defaulting to 0 means "no signal" (will be neutral after diff)."""
        if round_str is None:
            return 0
        s = str(round_str).strip().upper()
        if not s:
            return 0
        # Direct mapping
        mapping = {
            "Q1": 1, "Q2": 2, "Q3": 3,
            "RR": 4,  # Round-robin (treat as a mid-stage)
            "BR": 5,  # Bronze-medal round
            "ER": 1,  # Early round (TML legacy)
            "R128": 1, "R64": 2, "R32": 3, "R16": 4,
            "QF": 5, "SF": 6, "F": 7, "W": 8,  # W = won the title
        }
        return mapping.get(s, 0)

    @staticmethod
    def _is_three_plus_setter(score_text, best_of=None):
        """True if the match went to a deciding set: 3+ sets in BO3 or 4+ sets in BO5."""
        if not isinstance(score_text, str):
            return False
        cleaned = score_text.split("RET")[0].split("W/O")[0].split("DEF")[0]
        sets = re.findall(r"\d+\s*-\s*\d+", cleaned)
        n = len(sets)
        try:
            bo = int(best_of) if best_of is not None and not pd.isna(best_of) else 3
        except Exception:
            bo = 3
        return (bo == 3 and n >= 3) or (bo == 5 and n >= 4)

    @staticmethod
    def _infer_style_cluster(ace_rate, serve_win_rate, break_rate):
        # heuristique simple et robuste à partir des stats historiques
        if ace_rate >= 0.13 and serve_win_rate >= 0.66 and break_rate >= 0.23:
            return "serve_volley"
        if ace_rate >= 0.12 and serve_win_rate >= 0.64 and break_rate <= 0.22:
            return "big_server"
        if break_rate >= 0.27 and ace_rate <= 0.08:
            return "counterpuncher"
        if serve_win_rate >= 0.60 and break_rate >= 0.24:
            return "aggressive_baseliner"
        return "all_court"

    @staticmethod
    def _infer_tournament_context(tourney_name):
        name = str(tourney_name or "").lower()

        altitude_map = {
            "madrid": 667,
            "quito": 2850,
            "bogota": 2640,
            "gstaad": 1050,
            "kitzbuhel": 760,
            "geneva": 375,
            "rome": 21,
            "paris": 35,
            "wimbledon": 20,
            "melbourne": 31,
            "new york": 10,
            "miami": 2,
            "indian wells": 27,
            "cincinnati": 180,
            "shanghai": 4,
            "beijing": 44,
            "tokyo": 40,
            "doha": 10,
            "dubai": 16,
        }
        country_ioc_map = {
            "rome": "ITA",
            "madrid": "ESP",
            "barcelona": "ESP",
            "wimbledon": "GBR",
            "paris": "FRA",
            "roland": "FRA",
            "us open": "USA",
            "new york": "USA",
            "miami": "USA",
            "indian wells": "USA",
            "cincinnati": "USA",
            "australian": "AUS",
            "melbourne": "AUS",
            "tokyo": "JPN",
            "shanghai": "CHN",
            "beijing": "CHN",
            "doha": "QAT",
            "dubai": "UAE",
            "hamburg": "GER",
            "munich": "GER",
            "geneva": "SUI",
            "gstaad": "SUI",
            "monte-carlo": "MON",
        }
        indoor_keywords = (
            "indoor",
            "atp finals",
            "next gen",
            "metz",
            "basel",
            "paris bercy",
            "vienna",
            "st petersburg",
            "rotterdam",
        )

        altitude = 0.0
        for key, val in altitude_map.items():
            if key in name:
                altitude = float(val)
                break

        country_ioc = None
        for key, ioc in country_ioc_map.items():
            if key in name:
                country_ioc = ioc
                break

        indoor = 1.0 if any(k in name for k in indoor_keywords) else 0.0
        return altitude, indoor, country_ioc

    @staticmethod
    def _infer_tourney_level_from_name(tourney_name):
        n = str(tourney_name or "").lower()
        if any(k in n for k in ["wimbledon", "roland", "australian", "us open"]):
            return "G"
        if any(k in n for k in ["masters", "rome", "madrid", "miami", "indian wells", "monte-carlo", "cincinnati", "shanghai", "paris bercy"]):
            return "M"
        return "A"

    @staticmethod
    def _encode_tourney_level(level):
        # WTA Sackmann uses different codes (PM = Premier Mandatory ≈ Masters 1000,
        # I = International, P = Premier, G = Grand Slam). Normalize to {A, M, G}.
        raw = str(level or "A").strip()
        wta_alias = {"PM": "M", "P": "M", "I": "A", "D": "A", "F": "M", "O": "A", "W": "A"}
        raw = wta_alias.get(raw, raw)
        m = {"A": 1.0, "M": 2.0, "G": 3.0}
        return m.get(raw, 1.0)

    @staticmethod
    def _normalize_tourney_key(name):
        n = str(name or "").lower()
        n = re.sub(r"[^a-z0-9 ]+", " ", n)
        n = re.sub(r"\s+", " ", n).strip()
        return n[:96] if n else ""

    @staticmethod
    def _approx_defending_points(round_depth: int, tourney_level) -> float:
        """Rough ATP/WTA scale for points historically tied to finishing round (defending proxy)."""
        raw = str(tourney_level or "A").strip()
        wta_alias = {"PM": "M", "P": "M", "I": "A", "D": "A", "F": "M", "O": "A", "W": "A"}
        code = wta_alias.get(raw, raw)
        try:
            rd = int(round_depth) if round_depth is not None else 0
        except Exception:
            rd = 0
        rd = max(0, min(int(rd), 8))
        if code == "G":
            table = {0: 0, 1: 10, 2: 45, 3: 90, 4: 180, 5: 360, 6: 720, 7: 1200, 8: 2000}
        elif code == "M":
            table = {0: 0, 1: 10, 2: 45, 3: 90, 4: 180, 5: 360, 6: 600, 7: 1000, 8: 1000}
        else:
            table = {0: 0, 1: 8, 2: 20, 3: 45, 4: 90, 5: 150, 6: 250, 7: 300, 8: 500}
        return float(table.get(rd, 0))

    @staticmethod
    def _is_pre_slam_fatigue_week(ts) -> bool:
        """Binary week *before* a major — calendar approximation (no outcome leakage)."""
        if ts is None:
            return False
        try:
            if pd.isna(ts):
                return False
        except Exception:
            return False
        d = pd.Timestamp(ts).normalize()
        y = int(d.year)
        slam_mondays = [
            pd.Timestamp(y, 1, 15),
            pd.Timestamp(y, 5, 27),
            pd.Timestamp(y, 7, 1),
            pd.Timestamp(y, 8, 26),
        ]
        for sm in slam_mondays:
            start = sm - pd.Timedelta(days=7)
            if start <= d < sm:
                return True
        return False

    def _build_elo_features(self, matches_df, base_elo=1500.0, decay_tau_days=365.0):
        """Micro-Elo service/return (Tennis Abstract surface-speed weighting)."""
        out = run_micro_elo_scan(
            matches_df,
            name_key_fn=self._name_key,
            base_elo=base_elo,
            decay_tau_days=decay_tau_days,
            micro_elo_scale=float(self.micro_elo_scale),
            micro_elo_k_serve=float(self.micro_elo_k_serve),
            speed_baseline=float(self.micro_speed_baseline),
            speed_alpha=float(self.micro_speed_alpha),
        )
        self.player_service_elo = out["serve_g"]
        self.player_return_elo = out["ret_g"]
        self.player_service_elo_surface = out["serve_sf"]
        self.player_return_elo_surface = out["ret_sf"]
        self.player_surface_micro_count = out["n_sf"]
        self.player_elo = {k: (out["serve_g"][k] + out["ret_g"][k]) / 2.0 for k in out["serve_g"]}
        self.player_surface_elo = {}
        self.player_name_elo = {}
        self.player_name_surface_elo = {}
        self.player_surface_count = {}
        self.player_surface_count_name = {}
        self.player_last_seen = {k: pd.Timestamp(v).isoformat() for k, v in out["last_seen"].items()}
        self.player_last_seen_name = {}
        print(f"  Micro-Elo (surface speed): {out['matches_with_stats']} matchs stats serve/return utilisés")
        return (
            out["winner_elo_pre"],
            out["loser_elo_pre"],
            out["winner_surf_elo_pre"],
            out["loser_surf_elo_pre"],
            out["winner_service_elo_pre"],
            out["winner_return_elo_pre"],
            out["loser_service_elo_pre"],
            out["loser_return_elo_pre"],
        )

    def _build_temporal_features(self, df):
        # per-player rolling history: (date, won, minutes, sets, second_srv_ratio)
        hist = {}
        # per-player/per-surface rolling history: (date, won, hold_rate, break_rate, surf_elo_pre)
        hist_surface = {}
        # per-player rolling style signals: (date, ace_rate, serve_win_rate, break_rate)
        hist_style = {}
        # per-player rolling clutch signals: (date, bp_saved_ratio, bp_conv_ratio, tb_won, tb_played)
        hist_clutch = {}
        # per-player rolling micro stats: (date, first_srv_win_pct, bp_conv_pct, dominance_ratio)
        hist_micro = {}
        # per-player (date, surface_speed, win_flag) for speed affinity / correlation features
        hist_speed = {}
        # Tactical KMeans input: (dt, ace%, 1st serve won / 1st in, BP saved%, hold%)
        hist_tac = defaultdict(lambda: deque(maxlen=260))
        player_last_geo = {}
        last_date = {}

        # H2H directional stats
        pair_wins = {}
        pair_matches = {}
        # per-player performance vs opponent style cluster
        style_vs_wins = {}
        style_vs_matches = {}
        # Same tournament last year : best round reached (for points-defending proxy)
        edition_player_best_round = {}

        w_days_rest, l_days_rest = [], []
        # Smarter "workload" features that disambiguate "few minutes because winning easy"
        # vs "few minutes because losing early" (the spurious correlation that biased v1):
        #   wins_last7d:       count of victories in last 7 days (recent winning streak)
        #   three_setters14:   count of 3+/4+ setter matches in last 14 days (true fatigue)
        #   last_round_reached: depth reached at most-recent tournament (1=Q1 ... 8=W)
        w_wins7, l_wins7 = [], []
        w_three14, l_three14 = [], []
        w_last_round, l_last_round = [], []
        w_momentum5, l_momentum5 = [], []
        w_form90_surface, l_form90_surface = [], []
        w_ssr3, l_ssr3 = [], []
        w_hold_surface, l_hold_surface = [], []
        w_break_surface, l_break_surface = [], []
        w_first_srv10, l_first_srv10 = [], []
        w_bp_conv10, l_bp_conv10 = [], []
        w_dom_ratio, l_dom_ratio = [], []
        w_elo_surface_recent, l_elo_surface_recent = [], []
        w_speed_affinity, l_speed_affinity = [], []
        w_speed_perf_delta, l_speed_perf_delta = [], []
        w_h2h_ratio, l_h2h_ratio = [], []
        w_h2h_sig, l_h2h_sig = [], []
        w_style_adv, l_style_adv = [], []
        w_clutch_idx, l_clutch_idx = [], []
        w_bp_resilience, l_bp_resilience = [], []
        w_inact_decay, l_inact_decay = [], []
        w_home, l_home = [], []
        altitude_list, indoor_list = [], []
        w_pts_def_ratio, l_pts_def_ratio = [], []
        pre_slam_fatigue_list = []
        w_tac_ace, l_tac_ace = [], []
        w_tac_f1, l_tac_f1 = [], []
        w_tac_bp, l_tac_bp = [], []
        w_tac_hold, l_tac_hold = [], []
        w_tac10_ace, l_tac10_ace = [], []
        w_tac10_f1, l_tac10_f1 = [], []
        w_tac10_bp, l_tac10_bp = [], []
        w_tac10_hold, l_tac10_hold = [], []
        w_travel_pen, l_travel_pen = [], []
        w_clutch52, l_clutch52 = [], []

        td7 = pd.Timedelta(days=7)
        td90 = pd.Timedelta(days=90)
        td365 = pd.Timedelta(days=365)

        for row in df.itertuples(index=False):
            dt = row.tourney_date
            wid = self._pid_key(row.winner_id)
            lid = self._pid_key(row.loser_id)
            if not wid or not lid:
                # default neutral row and skip updates
                w_days_rest.append(14)
                l_days_rest.append(14)
                w_wins7.append(0); l_wins7.append(0)
                w_three14.append(0); l_three14.append(0)
                w_last_round.append(0); l_last_round.append(0)
                w_momentum5.append(0.5); l_momentum5.append(0.5)
                w_form90_surface.append(0.5); l_form90_surface.append(0.5)
                w_ssr3.append(0.5); l_ssr3.append(0.5)
                w_hold_surface.append(0.75); l_hold_surface.append(0.75)
                w_break_surface.append(0.20); l_break_surface.append(0.20)
                w_first_srv10.append(0.68); l_first_srv10.append(0.68)
                w_bp_conv10.append(0.38); l_bp_conv10.append(0.38)
                w_dom_ratio.append(1.00); l_dom_ratio.append(1.00)
                w_elo_surface_recent.append(0.0); l_elo_surface_recent.append(0.0)
                w_h2h_ratio.append(0.5); l_h2h_ratio.append(0.5)
                w_h2h_sig.append(0.0); l_h2h_sig.append(0.0)
                w_style_adv.append(0.5); l_style_adv.append(0.5)
                w_clutch_idx.append(0.5); l_clutch_idx.append(0.5)
                w_inact_decay.append(1.0); l_inact_decay.append(1.0)
                w_home.append(0.0); l_home.append(0.0)
                altitude_list.append(0.0); indoor_list.append(0.0)
                w_speed_affinity.append(0.0); l_speed_affinity.append(0.0)
                w_speed_perf_delta.append(0.0); l_speed_perf_delta.append(0.0)
                w_bp_resilience.append(0.5); l_bp_resilience.append(0.5)
                w_pts_def_ratio.append(0.0); l_pts_def_ratio.append(0.0)
                pre_slam_fatigue_list.append(0.0)
                w_tac_ace.append(0.08); l_tac_ace.append(0.08)
                w_tac_f1.append(0.62); l_tac_f1.append(0.62)
                w_tac_bp.append(0.58); l_tac_bp.append(0.58)
                w_tac_hold.append(0.75); l_tac_hold.append(0.75)
                w_travel_pen.append(0.0); l_travel_pen.append(0.0)
                w_clutch52.append(0.5); l_clutch52.append(0.5)
                continue

            altitude, indoor, country_ioc = self._infer_tournament_context(row.tourney_name)
            altitude_list.append(altitude)
            indoor_list.append(indoor)

            w_home.append(1.0 if country_ioc and row.winner_ioc == country_ioc else 0.0)
            l_home.append(1.0 if country_ioc and row.loser_ioc == country_ioc else 0.0)

            # rest
            if wid in last_date:
                w_days_rest.append(max(0, int((dt - last_date[wid]).days)))
            else:
                w_days_rest.append(14)
            if lid in last_date:
                l_days_rest.append(max(0, int((dt - last_date[lid]).days)))
            else:
                l_days_rest.append(14)
            w_inact_decay.append(self._inactivity_decay(w_days_rest[-1]))
            l_inact_decay.append(self._inactivity_decay(l_days_rest[-1]))

            nt = self._normalize_tourney_key(getattr(row, "tourney_name", None))
            y = int(pd.Timestamp(dt).year)
            lvl_cell = getattr(row, "tourney_level", None)
            if lvl_cell is None or (isinstance(lvl_cell, float) and np.isnan(lvl_cell)):
                lvl_cell = self._infer_tourney_level_from_name(getattr(row, "tourney_name", None))
            def_prev_w = edition_player_best_round.get((wid, nt, y - 1), 0)
            def_prev_l = edition_player_best_round.get((lid, nt, y - 1), 0)
            wyn = float(row.winner_rank_points) if pd.notna(getattr(row, "winner_rank_points", np.nan)) else 1.0
            lyn = float(row.loser_rank_points) if pd.notna(getattr(row, "loser_rank_points", np.nan)) else 1.0
            wyn = max(1.0, wyn)
            lyn = max(1.0, lyn)
            pts_w = self._approx_defending_points(def_prev_w, lvl_cell) / wyn
            pts_l = self._approx_defending_points(def_prev_l, lvl_cell) / lyn
            pre_sl = 1.0 if self._is_pre_slam_fatigue_week(dt) else 0.0
            w_pts_def_ratio.append(float(pts_w))
            l_pts_def_ratio.append(float(pts_l))
            pre_slam_fatigue_list.append(float(pre_sl))

            rspeed = getattr(row, "surface_speed", np.nan)
            if rspeed is not None and not (isinstance(rspeed, float) and np.isnan(rspeed)):
                spd = float(rspeed)
            else:
                spd = float(lookup_surface_speed(getattr(row, "tourney_name", None), getattr(row, "surface", None)))

            clat, clon, ctz = tournament_site_lon_lat_tz(getattr(row, "tourney_name", None))

            def _mean_tac_vec(dq, ref_dt):
                if dq is None or len(dq) == 0:
                    return np.array([0.08, 0.62, 0.58, 0.75], dtype=float)
                rec = [x for x in dq if x[0] >= ref_dt - td365]
                if len(rec) < 5:
                    return np.array([0.08, 0.62, 0.58, 0.75], dtype=float)
                return np.array(
                    [
                        float(np.mean([x[1] for x in rec])),
                        float(np.mean([x[2] for x in rec])),
                        float(np.mean([x[3] for x in rec])),
                        float(np.mean([x[4] for x in rec])),
                    ],
                    dtype=float,
                )

            def _mean_clutch52_scalar(cdq, ref_dt):
                if cdq is None or len(cdq) == 0:
                    return 0.5
                c12 = [x for x in cdq if x[0] >= ref_dt - td365]
                if len(c12) < 3:
                    return 0.5
                bp_saved_vals = [x[1] for x in c12 if not pd.isna(x[1])]
                bp_conv_vals = [x[2] for x in c12 if not pd.isna(x[2])]
                tb_w = float(np.nansum([x[3] for x in c12]))
                tb_p = float(np.nansum([x[4] for x in c12]))
                m1 = float(np.mean(bp_saved_vals)) if bp_saved_vals else 0.5
                m2 = float(np.mean(bp_conv_vals)) if bp_conv_vals else 0.5
                m3 = (tb_w / tb_p) if tb_p > 0 else 0.5
                return float(np.clip((m1 + m2 + m3) / 3.0, 0.0, 1.0))

            def _travel_pen(prev_geo, rest_d):
                if prev_geo is None:
                    return 0.0
                try:
                    rd = float(rest_d)
                except Exception:
                    rd = 14.0
                if rd >= 4:
                    return 0.0
                olat, olon, otz = prev_geo
                dist_km = haversine_km(float(olat), float(olon), float(clat), float(clon))
                if dist_km > 4000.0 or abs(float(ctz) - float(otz)) > 4:
                    return 0.05
                return 0.0

            tw_vec = _mean_tac_vec(hist_tac.get(wid), dt)
            tl_vec = _mean_tac_vec(hist_tac.get(lid), dt)
            def _mean_tac_vec_recent10(dq):
                if dq is None or len(dq) == 0:
                    return np.array([0.08, 0.62, 0.58, 0.75], dtype=float)
                rec = list(dq)[-10:]
                if len(rec) < 3:
                    return np.array([0.08, 0.62, 0.58, 0.75], dtype=float)
                return np.array(
                    [
                        float(np.mean([x[1] for x in rec])),
                        float(np.mean([x[2] for x in rec])),
                        float(np.mean([x[3] for x in rec])),
                        float(np.mean([x[4] for x in rec])),
                    ],
                    dtype=float,
                )
            tw10_vec = _mean_tac_vec_recent10(hist_tac.get(wid))
            tl10_vec = _mean_tac_vec_recent10(hist_tac.get(lid))
            w_tac_ace.append(float(tw_vec[0]))
            w_tac_f1.append(float(tw_vec[1]))
            w_tac_bp.append(float(tw_vec[2]))
            w_tac_hold.append(float(tw_vec[3]))
            l_tac_ace.append(float(tl_vec[0]))
            l_tac_f1.append(float(tl_vec[1]))
            l_tac_bp.append(float(tl_vec[2]))
            l_tac_hold.append(float(tl_vec[3]))
            w_tac10_ace.append(float(tw10_vec[0]))
            w_tac10_f1.append(float(tw10_vec[1]))
            w_tac10_bp.append(float(tw10_vec[2]))
            w_tac10_hold.append(float(tw10_vec[3]))
            l_tac10_ace.append(float(tl10_vec[0]))
            l_tac10_f1.append(float(tl10_vec[1]))
            l_tac10_bp.append(float(tl10_vec[2]))
            l_tac10_hold.append(float(tl10_vec[3]))
            w_clutch52.append(_mean_clutch52_scalar(hist_clutch.get(wid), dt))
            l_clutch52.append(_mean_clutch52_scalar(hist_clutch.get(lid), dt))
            w_travel_pen.append(_travel_pen(player_last_geo.get(wid), w_days_rest[-1]))
            l_travel_pen.append(_travel_pen(player_last_geo.get(lid), l_days_rest[-1]))

            def _speed_metrics(pid):
                dq = hist_speed.get(pid)
                if not dq:
                    return 0.0, 0.0
                cutoff = dt - td365
                rec = [(float(s), float(w)) for t, s, w in dq if t >= cutoff]
                if len(rec) < 5:
                    return 0.0, 0.0
                speeds = np.array([a[0] for a in rec], dtype=float)
                wins = np.array([a[1] for a in rec], dtype=float)
                if np.std(speeds) < 1e-9 or np.std(wins) < 1e-9:
                    corr = 0.0
                else:
                    c = np.corrcoef(speeds, wins)[0, 1]
                    corr = 0.0 if np.isnan(c) else float(c)
                fast = wins[speeds >= 0.75]
                slow = wins[speeds <= 0.65]
                aff = 0.0
                if len(fast) >= 2 and len(slow) >= 2:
                    aff = float(np.mean(fast) - np.mean(slow))
                return aff, corr

            w_aff_m, w_corr_m = _speed_metrics(wid)
            l_aff_m, l_corr_m = _speed_metrics(lid)
            w_speed_affinity.append(w_aff_m)
            l_speed_affinity.append(l_aff_m)
            w_speed_perf_delta.append(w_corr_m)
            l_speed_perf_delta.append(l_corr_m)

            # rolling history helpers
            # hist[pid] stores tuples: (dt, won, mins, sets, ssr, round_depth, is_3plus_setter)
            cutoff7 = dt - td7
            cutoff14 = dt - pd.Timedelta(days=14)
            for pid, is_winner, out_wins7, out_three14, out_last_round, out_mom, out_ssr in (
                (wid, True, w_wins7, w_three14, w_last_round, w_momentum5, w_ssr3),
                (lid, False, l_wins7, l_three14, l_last_round, l_momentum5, l_ssr3),
            ):
                dq = hist.get(pid)
                if dq is None:
                    dq = deque()
                    hist[pid] = dq

                # wins in last 7 days (clear positive signal: a player who keeps winning
                # accumulates matches because they advance, but here we count the WINS,
                # not the time spent on court — which decouples from "early-round losses").
                items7 = [x for x in dq if x[0] >= cutoff7]
                out_wins7.append(int(sum(1 for x in items7 if x[1])))

                # 3-set/4-set matches in last 14 days (true fatigue indicator).
                items14 = [x for x in dq if x[0] >= cutoff14]
                out_three14.append(int(sum(1 for x in items14 if x[6])))

                # depth reached at most-recent tournament:
                # we look at the last few entries and take the max round_depth we can find.
                # In practice, the most recent tournament's deepest round is what matters
                # (a player who reached SF then plays final has last_round=SF).
                last_few = list(dq)[-5:]
                last_round = max((x[5] for x in last_few), default=0)
                out_last_round.append(int(last_round))

                # momentum 5 latest (weighted recent higher)
                last5 = list(dq)[-5:]
                if not last5:
                    out_mom.append(0.5)
                else:
                    w = np.array([1, 2, 3, 4, 5], dtype=float)[-len(last5):]
                    vals = np.array([1.0 if x[1] else 0.0 for x in last5], dtype=float)
                    out_mom.append(float((vals * w).sum() / w.sum()))

                # second serve ratio over last 3
                last3 = [x[4] for x in list(dq)[-3:] if not pd.isna(x[4])]
                if last3:
                    out_ssr.append(float(np.mean(last3)))
                else:
                    out_ssr.append(0.5)

            # Surface-specific rolling features (last 90 days on this surface)
            surface = str(row.surface)
            for pid, out_form_s, out_hold_s, out_break_s, out_elo_recent, current_surf_elo in (
                (wid, w_form90_surface, w_hold_surface, w_break_surface, w_elo_surface_recent, float(row.winner_surf_elo_pre)),
                (lid, l_form90_surface, l_hold_surface, l_break_surface, l_elo_surface_recent, float(row.loser_surf_elo_pre)),
            ):
                skey = (pid, surface)
                sdq = hist_surface.get(skey)
                if sdq is None:
                    sdq = deque()
                    hist_surface[skey] = sdq

                cutoff90 = dt - td90
                s90 = [x for x in sdq if x[0] >= cutoff90]

                if s90:
                    out_form_s.append(float(np.mean([1.0 if x[1] else 0.0 for x in s90])))
                    out_hold_s.append(float(np.mean([x[2] for x in s90])))
                    out_break_s.append(float(np.mean([x[3] for x in s90])))
                else:
                    out_form_s.append(0.5)
                    out_hold_s.append(0.75)
                    out_break_s.append(0.20)

                last3_elo = [x[4] for x in list(sdq)[-3:]]
                if last3_elo:
                    out_elo_recent.append(current_surf_elo - float(np.mean(last3_elo)))
                else:
                    out_elo_recent.append(0.0)

            # Micro-stats rolling (10 derniers matchs)
            for pid, out_first, out_bp_conv, out_dom in (
                (wid, w_first_srv10, w_bp_conv10, w_dom_ratio),
                (lid, l_first_srv10, l_bp_conv10, l_dom_ratio),
            ):
                mdq = hist_micro.get(pid)
                if mdq is None:
                    mdq = deque()
                    hist_micro[pid] = mdq
                m10 = list(mdq)[-10:]
                if m10:
                    first_vals = [x[1] for x in m10 if not pd.isna(x[1])]
                    bp_vals = [x[2] for x in m10 if not pd.isna(x[2])]
                    dom_vals = [x[3] for x in m10 if not pd.isna(x[3])]
                    out_first.append(float(np.mean(first_vals)) if first_vals else 0.68)
                    out_bp_conv.append(float(np.mean(bp_vals)) if bp_vals else 0.38)
                    out_dom.append(float(np.mean(dom_vals)) if dom_vals else 1.00)
                else:
                    out_first.append(0.68)
                    out_bp_conv.append(0.38)
                    out_dom.append(1.00)

            # H2H pre-match
            w_key = (wid, lid)
            l_key = (lid, wid)
            w_tot = pair_matches.get(w_key, 0)
            l_tot = pair_matches.get(l_key, 0)

            if w_tot > 0:
                w_h2h_ratio.append(pair_wins.get(w_key, 0) / w_tot)
            else:
                w_h2h_ratio.append(0.5)
            if l_tot > 0:
                l_h2h_ratio.append(pair_wins.get(l_key, 0) / l_tot)
            else:
                l_h2h_ratio.append(0.5)

            w_h2h_sig.append(1.0 if w_tot >= 4 else 0.0)
            l_h2h_sig.append(1.0 if l_tot >= 4 else 0.0)

            # pre-match style clusters + style matchup edge
            def _current_style(pid):
                sdq = hist_style.get(pid)
                if not sdq:
                    return "all_court"
                last20 = list(sdq)[-20:]
                ace_rate = float(np.mean([x[1] for x in last20]))
                serve_win = float(np.mean([x[2] for x in last20]))
                break_rate = float(np.mean([x[3] for x in last20]))
                return self._infer_style_cluster(ace_rate, serve_win, break_rate)

            w_style = _current_style(wid)
            l_style = _current_style(lid)
            w_sv_key = (wid, l_style)
            l_sv_key = (lid, w_style)
            w_style_tot = style_vs_matches.get(w_sv_key, 0)
            l_style_tot = style_vs_matches.get(l_sv_key, 0)
            w_style_adv.append((style_vs_wins.get(w_sv_key, 0) / w_style_tot) if w_style_tot > 0 else 0.5)
            l_style_adv.append((style_vs_wins.get(l_sv_key, 0) / l_style_tot) if l_style_tot > 0 else 0.5)

            # pre-match clutch index (12 mois glissants)
            def _clutch_idx(pid):
                cdq = hist_clutch.get(pid)
                if not cdq:
                    return 0.5
                c12 = [x for x in cdq if x[0] >= (dt - td365)]
                if not c12:
                    return 0.5
                bp_saved_vals = [x[1] for x in c12 if not pd.isna(x[1])]
                bp_conv_vals = [x[2] for x in c12 if not pd.isna(x[2])]
                bp_saved_avg = float(np.mean(bp_saved_vals)) if bp_saved_vals else 0.5
                bp_conv_avg = float(np.mean(bp_conv_vals)) if bp_conv_vals else 0.5
                tb_w = float(np.nansum([x[3] for x in c12]))
                tb_p = float(np.nansum([x[4] for x in c12]))
                tb_rate = (tb_w / tb_p) if tb_p > 0 else 0.5
                return max(0.0, min(1.0, 0.4 * bp_saved_avg + 0.4 * bp_conv_avg + 0.2 * tb_rate))

            w_clutch_idx.append(_clutch_idx(wid))
            l_clutch_idx.append(_clutch_idx(lid))

            def _bp_resilience(pid):
                cdq = hist_clutch.get(pid)
                if not cdq:
                    return 0.5
                c12 = [x for x in cdq if x[0] >= (dt - td365)]
                vals = [x[1] for x in c12 if not pd.isna(x[1])]
                if not vals:
                    return 0.5
                return float(np.clip(np.mean(vals), 0.0, 1.0))

            w_bp_resilience.append(_bp_resilience(wid))
            l_bp_resilience.append(_bp_resilience(lid))

            # update with current match
            mins = float(row.minutes) if pd.notna(row.minutes) else 0.0
            sets = float(self._infer_sets_from_score(row.score))

            w_ssr_current = self._safe_second_srv_ratio(row.w_2ndWon, row.w_svpt, row.w_1stIn)
            l_ssr_current = self._safe_second_srv_ratio(row.l_2ndWon, row.l_svpt, row.l_1stIn)
            w_tb_won, l_tb_won, tb_played = self._infer_tiebreaks_from_score(row.score)

            # Hold/Break proxies from available match stats
            def _hold_break(sv_gms, bp_faced, bp_saved, opp_sv_gms):
                try:
                    sv_gms = float(sv_gms) if pd.notna(sv_gms) else 0.0
                    bp_faced = float(bp_faced) if pd.notna(bp_faced) else 0.0
                    bp_saved = float(bp_saved) if pd.notna(bp_saved) else 0.0
                    opp_sv_gms = float(opp_sv_gms) if pd.notna(opp_sv_gms) else 0.0
                    breaks_suffered = max(0.0, bp_faced - bp_saved)
                    hold_rate = (sv_gms - breaks_suffered) / sv_gms if sv_gms > 0 else 0.75
                    breaks_made = max(0.0, breaks_suffered)  # proxy from opponent perspective used separately below
                    break_rate = breaks_made / opp_sv_gms if opp_sv_gms > 0 else 0.20
                    return max(0.0, min(1.0, hold_rate)), max(0.0, min(1.0, break_rate))
                except Exception:
                    return 0.75, 0.20

            # winner hold on own serve, winner break on loser serve
            w_hold, _ = _hold_break(row.w_SvGms, row.w_bpFaced, row.w_bpSaved, row.l_SvGms)
            # breaks made by winner derived from loser faced/saved
            loser_breaks_suffered = max(0.0, (float(row.l_bpFaced) if pd.notna(row.l_bpFaced) else 0.0) - (float(row.l_bpSaved) if pd.notna(row.l_bpSaved) else 0.0))
            w_break = (loser_breaks_suffered / float(row.l_SvGms)) if pd.notna(row.l_SvGms) and float(row.l_SvGms) > 0 else 0.20
            w_break = max(0.0, min(1.0, w_break))

            l_hold, _ = _hold_break(row.l_SvGms, row.l_bpFaced, row.l_bpSaved, row.w_SvGms)
            winner_breaks_suffered = max(0.0, (float(row.w_bpFaced) if pd.notna(row.w_bpFaced) else 0.0) - (float(row.w_bpSaved) if pd.notna(row.w_bpSaved) else 0.0))
            l_break = (winner_breaks_suffered / float(row.w_SvGms)) if pd.notna(row.w_SvGms) and float(row.w_SvGms) > 0 else 0.20
            l_break = max(0.0, min(1.0, l_break))

            round_depth = self._round_depth(getattr(row, "round", None))
            edition_player_best_round[(wid, nt, y)] = max(
                edition_player_best_round.get((wid, nt, y), 0), int(round_depth)
            )
            edition_player_best_round[(lid, nt, y)] = max(
                edition_player_best_round.get((lid, nt, y), 0), int(round_depth)
            )
            best_of = getattr(row, "best_of", None)
            three_plus = self._is_three_plus_setter(getattr(row, "score", None), best_of)
            hist[wid].append((dt, True, mins, sets, w_ssr_current, round_depth, three_plus))
            hist[lid].append((dt, False, mins, sets, l_ssr_current, round_depth, three_plus))
            hist_surface[(wid, surface)].append((dt, True, w_hold, w_break, float(row.winner_surf_elo_pre)))
            hist_surface[(lid, surface)].append((dt, False, l_hold, l_break, float(row.loser_surf_elo_pre)))
            hist_speed.setdefault(wid, deque(maxlen=120)).append((dt, spd, 1.0))
            hist_speed.setdefault(lid, deque(maxlen=120)).append((dt, spd, 0.0))

            # update style rolling stats
            w_ace_rate = self._safe_ratio(row.w_ace, row.w_svpt, default=0.06)
            l_ace_rate = self._safe_ratio(row.l_ace, row.l_svpt, default=0.06)
            w_serve_win = self._safe_ratio((float(row.w_1stWon) if pd.notna(row.w_1stWon) else 0.0) + (float(row.w_2ndWon) if pd.notna(row.w_2ndWon) else 0.0), row.w_svpt, default=0.60)
            l_serve_win = self._safe_ratio((float(row.l_1stWon) if pd.notna(row.l_1stWon) else 0.0) + (float(row.l_2ndWon) if pd.notna(row.l_2ndWon) else 0.0), row.l_svpt, default=0.60)
            hist_style.setdefault(wid, deque()).append((dt, w_ace_rate, w_serve_win, w_break))
            hist_style.setdefault(lid, deque()).append((dt, l_ace_rate, l_serve_win, l_break))
            w_fst_in_pct = self._safe_ratio(row.w_1stWon, row.w_1stIn, default=np.nan)
            l_fst_in_pct = self._safe_ratio(row.l_1stWon, row.l_1stIn, default=np.nan)
            w_bp_sv_m = self._safe_ratio(row.w_bpSaved, row.w_bpFaced, default=np.nan)
            l_bp_sv_m = self._safe_ratio(row.l_bpSaved, row.l_bpFaced, default=np.nan)
            hist_tac[wid].append((dt, float(w_ace_rate), float(w_fst_in_pct or 0.62), float(w_bp_sv_m or 0.58), float(w_hold)))
            hist_tac[lid].append((dt, float(l_ace_rate), float(l_fst_in_pct or 0.62), float(l_bp_sv_m or 0.58), float(l_hold)))

            # update clutch rolling stats
            w_bp_saved = self._safe_ratio(row.w_bpSaved, row.w_bpFaced, default=np.nan)
            l_bp_saved = self._safe_ratio(row.l_bpSaved, row.l_bpFaced, default=np.nan)
            w_bp_conv = self._safe_ratio(loser_breaks_suffered, row.l_bpFaced, default=np.nan)
            l_bp_conv = self._safe_ratio(winner_breaks_suffered, row.w_bpFaced, default=np.nan)
            hist_clutch.setdefault(wid, deque()).append((dt, w_bp_saved, w_bp_conv, w_tb_won, tb_played))
            hist_clutch.setdefault(lid, deque()).append((dt, l_bp_saved, l_bp_conv, l_tb_won, tb_played))

            # update micro rolling stats
            w_first_pct = self._safe_ratio(row.w_1stWon, row.w_1stIn, default=np.nan)
            l_first_pct = self._safe_ratio(row.l_1stWon, row.l_1stIn, default=np.nan)
            w_serve_win = self._safe_ratio(
                (float(row.w_1stWon) if pd.notna(row.w_1stWon) else 0.0) + (float(row.w_2ndWon) if pd.notna(row.w_2ndWon) else 0.0),
                row.w_svpt,
                default=np.nan,
            )
            l_serve_win = self._safe_ratio(
                (float(row.l_1stWon) if pd.notna(row.l_1stWon) else 0.0) + (float(row.l_2ndWon) if pd.notna(row.l_2ndWon) else 0.0),
                row.l_svpt,
                default=np.nan,
            )
            w_return_pts = self._safe_ratio(
                (float(row.l_svpt) if pd.notna(row.l_svpt) else 0.0) - ((float(row.l_1stWon) if pd.notna(row.l_1stWon) else 0.0) + (float(row.l_2ndWon) if pd.notna(row.l_2ndWon) else 0.0)),
                row.l_svpt,
                default=np.nan,
            )
            l_return_pts = self._safe_ratio(
                (float(row.w_svpt) if pd.notna(row.w_svpt) else 0.0) - ((float(row.w_1stWon) if pd.notna(row.w_1stWon) else 0.0) + (float(row.w_2ndWon) if pd.notna(row.w_2ndWon) else 0.0)),
                row.w_svpt,
                default=np.nan,
            )
            w_service_lost = np.nan if pd.isna(w_serve_win) else max(0.01, 1.0 - float(w_serve_win))
            l_service_lost = np.nan if pd.isna(l_serve_win) else max(0.01, 1.0 - float(l_serve_win))
            w_dom = np.nan if pd.isna(w_return_pts) or pd.isna(w_service_lost) else float(w_return_pts) / float(w_service_lost)
            l_dom = np.nan if pd.isna(l_return_pts) or pd.isna(l_service_lost) else float(l_return_pts) / float(l_service_lost)
            hist_micro.setdefault(wid, deque()).append((dt, w_first_pct, w_bp_conv, w_dom))
            hist_micro.setdefault(lid, deque()).append((dt, l_first_pct, l_bp_conv, l_dom))

            player_last_geo[wid] = (clat, clon, ctz)
            player_last_geo[lid] = (clat, clon, ctz)
            last_date[wid] = dt
            last_date[lid] = dt

            pair_matches[w_key] = pair_matches.get(w_key, 0) + 1
            pair_wins[w_key] = pair_wins.get(w_key, 0) + 1
            pair_matches[l_key] = pair_matches.get(l_key, 0) + 1
            style_vs_matches[w_sv_key] = style_vs_matches.get(w_sv_key, 0) + 1
            style_vs_wins[w_sv_key] = style_vs_wins.get(w_sv_key, 0) + 1
            style_vs_matches[l_sv_key] = style_vs_matches.get(l_sv_key, 0) + 1

        return {
            "winner_days_rest": pd.Series(w_days_rest),
            "loser_days_rest": pd.Series(l_days_rest),
            "winner_wins7": pd.Series(w_wins7),
            "loser_wins7": pd.Series(l_wins7),
            "winner_three14": pd.Series(w_three14),
            "loser_three14": pd.Series(l_three14),
            "winner_last_round": pd.Series(w_last_round),
            "loser_last_round": pd.Series(l_last_round),
            "winner_momentum5": pd.Series(w_momentum5),
            "loser_momentum5": pd.Series(l_momentum5),
            "winner_form90_surface": pd.Series(w_form90_surface),
            "loser_form90_surface": pd.Series(l_form90_surface),
            "winner_ssr3": pd.Series(w_ssr3),
            "loser_ssr3": pd.Series(l_ssr3),
            "winner_hold_surface": pd.Series(w_hold_surface),
            "loser_hold_surface": pd.Series(l_hold_surface),
            "winner_break_surface": pd.Series(w_break_surface),
            "loser_break_surface": pd.Series(l_break_surface),
            "winner_first_srv10": pd.Series(w_first_srv10),
            "loser_first_srv10": pd.Series(l_first_srv10),
            "winner_bp_conv10": pd.Series(w_bp_conv10),
            "loser_bp_conv10": pd.Series(l_bp_conv10),
            "winner_dom_ratio": pd.Series(w_dom_ratio),
            "loser_dom_ratio": pd.Series(l_dom_ratio),
            "winner_elo_surface_recent": pd.Series(w_elo_surface_recent),
            "loser_elo_surface_recent": pd.Series(l_elo_surface_recent),
            "winner_h2h_ratio": pd.Series(w_h2h_ratio),
            "loser_h2h_ratio": pd.Series(l_h2h_ratio),
            "winner_h2h_sig": pd.Series(w_h2h_sig),
            "loser_h2h_sig": pd.Series(l_h2h_sig),
            "winner_style_adv": pd.Series(w_style_adv),
            "loser_style_adv": pd.Series(l_style_adv),
            "winner_clutch_idx": pd.Series(w_clutch_idx),
            "loser_clutch_idx": pd.Series(l_clutch_idx),
            "winner_bp_resilience": pd.Series(w_bp_resilience),
            "loser_bp_resilience": pd.Series(l_bp_resilience),
            "winner_inactivity_decay": pd.Series(w_inact_decay),
            "loser_inactivity_decay": pd.Series(l_inact_decay),
            "winner_home": pd.Series(w_home),
            "loser_home": pd.Series(l_home),
            "winner_speed_affinity": pd.Series(w_speed_affinity),
            "loser_speed_affinity": pd.Series(l_speed_affinity),
            "winner_speed_perf_delta": pd.Series(w_speed_perf_delta),
            "loser_speed_perf_delta": pd.Series(l_speed_perf_delta),
            "altitude": pd.Series(altitude_list),
            "indoor": pd.Series(indoor_list),
            "winner_pts_def_ratio": pd.Series(w_pts_def_ratio),
            "loser_pts_def_ratio": pd.Series(l_pts_def_ratio),
            "pre_slam_fatigue": pd.Series(pre_slam_fatigue_list),
            "winner_tac_ace": pd.Series(w_tac_ace),
            "loser_tac_ace": pd.Series(l_tac_ace),
            "winner_tac_f1": pd.Series(w_tac_f1),
            "loser_tac_f1": pd.Series(l_tac_f1),
            "winner_tac_bp": pd.Series(w_tac_bp),
            "loser_tac_bp": pd.Series(l_tac_bp),
            "winner_tac_hold": pd.Series(w_tac_hold),
            "loser_tac_hold": pd.Series(l_tac_hold),
            "winner_tac10_ace": pd.Series(w_tac10_ace),
            "loser_tac10_ace": pd.Series(l_tac10_ace),
            "winner_tac10_f1": pd.Series(w_tac10_f1),
            "loser_tac10_f1": pd.Series(l_tac10_f1),
            "winner_tac10_bp": pd.Series(w_tac10_bp),
            "loser_tac10_bp": pd.Series(l_tac10_bp),
            "winner_tac10_hold": pd.Series(w_tac10_hold),
            "loser_tac10_hold": pd.Series(l_tac10_hold),
            "winner_travel_penalty": pd.Series(w_travel_pen),
            "loser_travel_penalty": pd.Series(l_travel_pen),
            "winner_clutch52": pd.Series(w_clutch52),
            "loser_clutch52": pd.Series(l_clutch52),
        }

    def _print_player_style_cluster_report(self) -> None:
        cal = getattr(self, "style_semantic_calibration", None)
        km = getattr(self, "style_kmeans", None)
        rk = getattr(self, "style_rank_map", None)
        if cal is None or km is None or rk is None:
            print("\n--- Player style clusters (KMeans) — désactivé (données tactiques insuffisantes ou bundle sans calibration) ---\n")
            return
        means = np.asarray(cal.get("mean_ace_by_raw_cluster_id", []), dtype=float)
        cents = np.asarray(cal.get("centroid_ace_col0_by_raw_cluster_id", []), dtype=float)
        cnt = np.asarray(cal.get("count_assignments_on_fit_rows", []), dtype=int)
        order = cal.get("deterministic_semantic_sort_raw_cluster_ids_desc", [])
        print("\n--- Player style clusters — tri déterministe (mean Ace sur points d’ajustement, décroissant -> rang 0 = Big Server) ---")
        for sem, raw_cid in enumerate(order):
            rc = int(raw_cid)
            ma = float(means[rc]) if rc < len(means) else float("nan")
            ca = float(cents[rc]) if rc < len(cents) else float("nan")
            nc = int(cnt[rc]) if rc < len(cnt) else -1
            print(
                f"  rang sémantique {sem} ({STYLE_SEMANTIC_LABELS[sem]}): brut cluster_id={rc} | "
                f"mean_ace={ma:.6f} | centroïde_ace={ca:.6f} | n_points_fit={nc}"
            )
        print(f"  style_rank_map (brut->sémantique): {np.asarray(rk, dtype=int).tolist()}")
        mm = getattr(self, "style_surface_winrate_index", {}) or {}
        if mm:
            print(f"  style_surface_winrate_index: {len(mm)} combinaisons style×style×surface")
        print("--- Fin rapport styles joueur ---\n")

    def prepare_data(self):
        print("Chargement et préparation des données (ATP TML + WTA Sackmann)...")
        conn = sqlite3.connect(self.db_path)
        try:
            df_atp = pd.read_sql(
                "SELECT * FROM matches_recent "
                "WHERE source='tennismylife' AND CAST(substr(tourney_date,1,4) AS INTEGER) >= 2010",
                conn,
            )
            print(f"  TennisMyLife (ATP) rows: {len(df_atp)}")
        except Exception as e:
            conn.close()
            raise RuntimeError(f"Table matches_recent indisponible. Lance d'abord sync_tml_recent.py ({e})")
        try:
            df_wta = pd.read_sql(
                "SELECT * FROM wta_matches "
                "WHERE CAST(substr(tourney_date,1,4) AS INTEGER) >= 2010",
                conn,
            )
            print(f"  Sackmann (WTA) rows: {len(df_wta)}")
        except Exception as e:
            print(f"  [WARN] Table wta_matches indisponible — entraînement ATP uniquement ({e})")
            df_wta = pd.DataFrame()
        conn.close()
        if df_atp.empty and df_wta.empty:
            raise RuntimeError("Aucune donnée d'entraînement disponible.")

        # Tag tour and pre-normalize IDs (prefix with tour to avoid ATP/WTA collisions).
        # Dates are normalized PER tour because TML uses 'YYYYMMDD' while Sackmann uses ISO datetime;
        # concatenating raw strings of mixed formats then to_datetime() coerces all WTA dates to NaT.
        if not df_atp.empty:
            df_atp["tour"] = "ATP"
            df_atp["winner_id"] = "ATP::" + df_atp["winner_id"].astype(str)
            df_atp["loser_id"] = "ATP::" + df_atp["loser_id"].astype(str)
            df_atp["tourney_date"] = pd.to_datetime(df_atp["tourney_date"], format="%Y%m%d", errors="coerce")
        if not df_wta.empty:
            df_wta["tour"] = "WTA"
            df_wta["winner_id"] = "WTA::" + df_wta["winner_id"].astype(str)
            df_wta["loser_id"] = "WTA::" + df_wta["loser_id"].astype(str)
            df_wta["tourney_date"] = pd.to_datetime(df_wta["tourney_date"], errors="coerce")
            # Normalize surface casing to avoid 'clay' vs 'Clay' duplicates in the encoded surface
            if "surface" in df_wta.columns:
                df_wta["surface"] = df_wta["surface"].astype(str).str.title().replace({"Nan": pd.NA})
            if "source" not in df_wta.columns:
                df_wta["source"] = "sackmann_wta"
            if "source_updated_at" not in df_wta.columns:
                df_wta["source_updated_at"] = None

        df = pd.concat([df_atp, df_wta], ignore_index=True, sort=False)
        df = df.dropna(subset=["winner_rank", "loser_rank", "winner_age", "loser_age", "surface", "tourney_date"])
        df = df.sort_values("tourney_date").reset_index(drop=True)
        print(f"  Total rows after cleaning: {len(df)} (ATP={int((df['tour']=='ATP').sum())}, WTA={int((df['tour']=='WTA').sum())})")

        if "humidity_pct" not in df.columns:
            df["humidity_pct"] = np.nan
        if "temp_c" not in df.columns:
            df["temp_c"] = np.nan

        def _row_base_cpi(row):
            v = getattr(row, "surface_speed", None)
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                return float(v)
            return float(lookup_surface_speed(getattr(row, "tourney_name", None), getattr(row, "surface", None)))

        df["_base_cpi"] = df.apply(_row_base_cpi, axis=1)
        df["_outdoor"] = df.apply(
            lambda r: infer_outdoor(getattr(r, "indoor", None), getattr(r, "tourney_name", None)),
            axis=1,
        )
        hum_imp, temp_imp, eff_spd = [], [], []
        for _, r in df.iterrows():
            o = bool(r["_outdoor"])
            hp = float(r["humidity_pct"]) if pd.notna(r["humidity_pct"]) else None
            tc = float(r["temp_c"]) if pd.notna(r["temp_c"]) else None
            hi, ti = weather_impact_scalars(o, hp, tc)
            eff = effective_surface_speed_cpi(float(r["_base_cpi"]), o, hp, tc)
            hum_imp.append(hi)
            temp_imp.append(ti)
            eff_spd.append(eff)
        df["humidity_impact"] = hum_imp
        df["temperature_impact"] = temp_imp
        df["surface_speed"] = eff_spd
        df["market_sentiment_signal"] = 0.0
        df.drop(columns=["_base_cpi", "_outdoor"], inplace=True)

        # Micro-Elo + legacy Elo columns (pre-match, no leakage)
        (
            w_elo,
            l_elo,
            w_s_elo,
            l_s_elo,
            w_serv,
            w_ret,
            l_serv,
            l_ret,
        ) = self._build_elo_features(df)
        df["winner_elo_pre"] = w_elo
        df["loser_elo_pre"] = l_elo
        df["winner_surf_elo_pre"] = w_s_elo
        df["loser_surf_elo_pre"] = l_s_elo
        df["winner_service_elo_pre"] = w_serv
        df["winner_return_elo_pre"] = w_ret
        df["loser_service_elo_pre"] = l_serv
        df["loser_return_elo_pre"] = l_ret

        temporal = self._build_temporal_features(df)
        for k, v in temporal.items():
            df[k] = v

        _tac_w_cols = ["winner_tac_ace", "winner_tac_f1", "winner_tac_bp", "winner_tac_hold"]
        _tac_l_cols = ["loser_tac_ace", "loser_tac_f1", "loser_tac_bp", "loser_tac_hold"]
        _tac10_w_cols = ["winner_tac10_ace", "winner_tac10_f1", "winner_tac10_bp", "winner_tac10_hold"]
        _tac10_l_cols = ["loser_tac10_ace", "loser_tac10_f1", "loser_tac10_bp", "loser_tac10_hold"]
        _base_names = ["tac_ace", "tac_f1", "tac_bp", "tac_hold"]
        _A = df[_tac_w_cols].astype(float).replace([np.inf, -np.inf], np.nan).rename(columns=dict(zip(_tac_w_cols, _base_names)))
        _B = df[_tac_l_cols].astype(float).replace([np.inf, -np.inf], np.nan).rename(columns=dict(zip(_tac_l_cols, _base_names)))
        _stack_tac = pd.concat([_A, _B], axis=0, ignore_index=True)
        _med = _stack_tac.median()
        _stack_tac = _stack_tac.fillna(_med).fillna(pd.Series({k: [0.08, 0.62, 0.58, 0.75][i] for i, k in enumerate(_base_names)}))

        def _median_fill_tac(sub_old_names: list) -> np.ndarray:
            sub = df[sub_old_names].astype(float).replace([np.inf, -np.inf], np.nan).rename(columns=dict(zip(sub_old_names, _base_names)))
            out = sub.copy()
            for c in _base_names:
                out[c] = out[c].fillna(float(_med.get(c, 0.08)))
            return np.asarray(out[_base_names].fillna(0.08).values, dtype=float)

        if len(_stack_tac) >= 400:
            X_tac = np.asarray(_stack_tac.values, dtype=float)
            km = KMeans(n_clusters=4, random_state=42, n_init=15, algorithm="lloyd").fit(X_tac)
            self.style_kmeans = km
            self.style_rank_map, self.style_semantic_calibration = build_style_semantic_rank_map(km, X_tac)
            ww = _median_fill_tac(_tac_w_cols)
            lw = _median_fill_tac(_tac_l_cols)
            ww10 = _median_fill_tac(_tac10_w_cols)
            lw10 = _median_fill_tac(_tac10_l_cols)
            pw = km.predict(ww)
            pl = km.predict(lw)
            pw10 = km.predict(ww10)
            pl10 = km.predict(lw10)
            rw = self.style_rank_map[np.asarray(pw, dtype=int)]
            rl = self.style_rank_map[np.asarray(pl, dtype=int)]
            rw10 = self.style_rank_map[np.asarray(pw10, dtype=int)]
            rl10 = self.style_rank_map[np.asarray(pl10, dtype=int)]
            try:
                ss = df["surface_speed"].astype(float)
            except Exception:
                ss = pd.Series(0.75, index=df.index)
            surf_slow = df["surface"].astype(str).str.lower().eq("clay").values | (ss.values < 0.68)
            p1_styles = [STYLE_SEMANTIC_LABELS[int(max(0, min(3, int(r))))] for r in rw]
            p2_styles = [STYLE_SEMANTIC_LABELS[int(max(0, min(3, int(r))))] for r in rl]
            # Build historical style-vs-style winrate matrix on this training corpus
            # with both orientations to avoid directional sparsity.
            matchup_wins = defaultdict(int)
            matchup_n = defaultdict(int)
            for s1, s2, sf, tr in zip(
                p1_styles,
                p2_styles,
                df["surface"].astype(str).values,
                df["tour"].astype(str).values,
            ):
                tr_u = (str(tr).upper() if tr else "ATP")
                if tr_u not in {"ATP", "WTA"}:
                    tr_u = "ATP"
                k_w = f"{tr_u}|{style_interaction_key(s1, s2, sf)}"
                k_l = f"{tr_u}|{style_interaction_key(s2, s1, sf)}"
                matchup_wins[k_w] += 1
                matchup_n[k_w] += 1
                matchup_n[k_l] += 1
            self.style_surface_winrate_index = {
                k: {
                    "wins": int(matchup_wins.get(k, 0)),
                    "n": int(matchup_n.get(k, 0)),
                    "winrate": float(matchup_wins.get(k, 0) / matchup_n[k]) if matchup_n[k] > 0 else 0.5,
                }
                for k in matchup_n.keys()
            }
            df["style_matchup_bias"] = [
                self._get_refined_matchup_bias(
                    p1_style_name=s1,
                    p2_style_name=s2,
                    surface=str(sf),
                    surface_speed=float(sp),
                    tour_label=str(tr),
                )["style_matchup_bias"]
                for s1, s2, sf, sp, tr in zip(
                    p1_styles,
                    p2_styles,
                    df["surface"].astype(str).values,
                    ss.values,
                    df["tour"].astype(str).values,
                )
            ]
            df["winner_style_drift_detected"] = (rw != rw10).astype(float)
            df["loser_style_drift_detected"] = (rl != rl10).astype(float)
            centers = np.asarray(km.cluster_centers_, dtype=float)
            d_w = np.linalg.norm(ww - centers[np.asarray(pw, dtype=int)], axis=1)
            d_l = np.linalg.norm(lw - centers[np.asarray(pl, dtype=int)], axis=1)
            df["winner_style_cluster_distance"] = np.asarray(d_w, dtype=float)
            df["loser_style_cluster_distance"] = np.asarray(d_l, dtype=float)
        else:
            self.style_kmeans = None
            self.style_rank_map = np.arange(4, dtype=int)
            self.style_semantic_calibration = None
            self.style_surface_winrate_index = {}
            df["style_matchup_bias"] = 0.0
            df["winner_style_drift_detected"] = 0.0
            df["loser_style_drift_detected"] = 0.0
            df["winner_style_cluster_distance"] = 0.0
            df["loser_style_cluster_distance"] = 0.0

        # Create oriented binary dataset (winner as p1 + loser as p1)
        df1 = pd.DataFrame()
        df1["surface"] = df["surface"]
        df1["tourney_date"] = df["tourney_date"]
        df1["tournament_level_encoded"] = df["tourney_level"].fillna("A").map(self._encode_tourney_level)
        df1["tour_encoded"] = (df["tour"] == "WTA").astype(float)
        df1["rank_diff"] = df["winner_rank"] - df["loser_rank"]
        df1["age_diff"] = df["winner_age"] - df["loser_age"]
        df1["ht_diff"] = df["winner_ht"] - df["loser_ht"]
        df1["points_diff"] = df["winner_rank_points"] - df["loser_rank_points"]
        df1["service_elo_diff"] = df["winner_service_elo_pre"] - df["loser_service_elo_pre"]
        df1["return_elo_diff"] = df["winner_return_elo_pre"] - df["loser_return_elo_pre"]
        df1["speed_affinity"] = df["winner_speed_affinity"] - df["loser_speed_affinity"]
        df1["speed_performance_delta"] = df["winner_speed_perf_delta"] - df["loser_speed_perf_delta"]
        df1["serve_speed_interaction"] = df1["service_elo_diff"] * df["surface_speed"].astype(float)
        df1["wta_weighted_advantage"] = df1["tour_encoded"] * (
            0.6 * df1["return_elo_diff"] + 0.4 * df1["service_elo_diff"]
        )
        df1["wta_speed_power_impact"] = df1["tour_encoded"] * df1["serve_speed_interaction"]
        df1["wta_break_point_resilience"] = df1["tour_encoded"] * (
            df["winner_bp_resilience"] - df["loser_bp_resilience"]
        )
        df1["wins_last7d_diff"] = df["winner_wins7"] - df["loser_wins7"]
        df1["three_setters_last14d_diff"] = df["winner_three14"] - df["loser_three14"]
        df1["last_round_reached_diff"] = df["winner_last_round"] - df["loser_last_round"]
        df1["momentum5_diff"] = df["winner_momentum5"] - df["loser_momentum5"]
        df1["form90_surface_diff"] = df["winner_form90_surface"] - df["loser_form90_surface"]
        df1["second_srv_ratio3_diff"] = df["winner_ssr3"] - df["loser_ssr3"]
        df1["hold_surface_diff"] = df["winner_hold_surface"] - df["loser_hold_surface"]
        df1["break_surface_diff"] = df["winner_break_surface"] - df["loser_break_surface"]
        df1["first_srv_win10_diff"] = df["winner_first_srv10"] - df["loser_first_srv10"]
        df1["bp_conv10_diff"] = df["winner_bp_conv10"] - df["loser_bp_conv10"]
        df1["dominance_ratio_diff"] = df["winner_dom_ratio"] - df["loser_dom_ratio"]
        df1["elo_surface_recent_diff"] = df["winner_elo_surface_recent"] - df["loser_elo_surface_recent"]
        df1["hand_diff"] = df["winner_hand"].map(self._encode_hand).fillna(0) - df["loser_hand"].map(self._encode_hand).fillna(0)
        df1["h2h_ratio"] = df["winner_h2h_ratio"]
        df1["h2h_significant"] = df["winner_h2h_sig"]
        df1["style_advantage_score"] = df["winner_style_adv"]
        df1["clutch_index_diff"] = df["winner_clutch_idx"] - df["loser_clutch_idx"]
        df1["inactivity_decay_weight"] = df["winner_inactivity_decay"] - df["loser_inactivity_decay"]
        df1["home_adv_diff"] = df["winner_home"] - df["loser_home"]
        df1["altitude"] = df["altitude"]
        df1["indoor"] = df["indoor"]
        df1["humidity_impact"] = df["humidity_impact"]
        df1["temperature_impact"] = df["temperature_impact"]
        df1["market_sentiment_signal"] = df["market_sentiment_signal"]
        df1["points_defending_pct"] = df["winner_pts_def_ratio"] - df["loser_pts_def_ratio"]
        df1["pre_slam_fatigue"] = df["pre_slam_fatigue"]
        df1["style_drift_detected"] = (
            (df["winner_style_drift_detected"] > 0.5) | (df["loser_style_drift_detected"] > 0.5)
        ).astype(float)
        df1["style_cluster_distance_diff"] = (
            df["winner_style_cluster_distance"] - df["loser_style_cluster_distance"]
        )
        df1["style_matchup_bias"] = df["style_matchup_bias"]
        df1["travel_fatigue_index"] = df["winner_travel_penalty"] - df["loser_travel_penalty"]
        df1["style_cross_surface_impact"] = (
            (df["winner_clutch52"] - df["loser_clutch52"]) * df["surface_speed"].astype(float)
        )
        df1["clutch_diff"] = df["winner_clutch52"] - df["loser_clutch52"]
        df1["target"] = 1

        df2 = pd.DataFrame()
        df2["surface"] = df["surface"]
        df2["tourney_date"] = df["tourney_date"]
        df2["tournament_level_encoded"] = df["tourney_level"].fillna("A").map(self._encode_tourney_level)
        df2["tour_encoded"] = (df["tour"] == "WTA").astype(float)
        df2["rank_diff"] = df["loser_rank"] - df["winner_rank"]
        df2["age_diff"] = df["loser_age"] - df["winner_age"]
        df2["ht_diff"] = df["loser_ht"] - df["winner_ht"]
        df2["points_diff"] = df["loser_rank_points"] - df["winner_rank_points"]
        df2["service_elo_diff"] = df["loser_service_elo_pre"] - df["winner_service_elo_pre"]
        df2["return_elo_diff"] = df["loser_return_elo_pre"] - df["winner_return_elo_pre"]
        df2["speed_affinity"] = df["loser_speed_affinity"] - df["winner_speed_affinity"]
        df2["speed_performance_delta"] = df["loser_speed_perf_delta"] - df["winner_speed_perf_delta"]
        df2["serve_speed_interaction"] = df2["service_elo_diff"] * df["surface_speed"].astype(float)
        df2["wta_weighted_advantage"] = df2["tour_encoded"] * (
            0.6 * df2["return_elo_diff"] + 0.4 * df2["service_elo_diff"]
        )
        df2["wta_speed_power_impact"] = df2["tour_encoded"] * df2["serve_speed_interaction"]
        df2["wta_break_point_resilience"] = df2["tour_encoded"] * (
            df["loser_bp_resilience"] - df["winner_bp_resilience"]
        )
        df2["wins_last7d_diff"] = df["loser_wins7"] - df["winner_wins7"]
        df2["three_setters_last14d_diff"] = df["loser_three14"] - df["winner_three14"]
        df2["last_round_reached_diff"] = df["loser_last_round"] - df["winner_last_round"]
        df2["momentum5_diff"] = df["loser_momentum5"] - df["winner_momentum5"]
        df2["form90_surface_diff"] = df["loser_form90_surface"] - df["winner_form90_surface"]
        df2["second_srv_ratio3_diff"] = df["loser_ssr3"] - df["winner_ssr3"]
        df2["hold_surface_diff"] = df["loser_hold_surface"] - df["winner_hold_surface"]
        df2["break_surface_diff"] = df["loser_break_surface"] - df["winner_break_surface"]
        df2["first_srv_win10_diff"] = df["loser_first_srv10"] - df["winner_first_srv10"]
        df2["bp_conv10_diff"] = df["loser_bp_conv10"] - df["winner_bp_conv10"]
        df2["dominance_ratio_diff"] = df["loser_dom_ratio"] - df["winner_dom_ratio"]
        df2["elo_surface_recent_diff"] = df["loser_elo_surface_recent"] - df["winner_elo_surface_recent"]
        df2["hand_diff"] = df["loser_hand"].map(self._encode_hand).fillna(0) - df["winner_hand"].map(self._encode_hand).fillna(0)
        df2["h2h_ratio"] = df["loser_h2h_ratio"]
        df2["h2h_significant"] = df["loser_h2h_sig"]
        df2["style_advantage_score"] = df["loser_style_adv"]
        df2["clutch_index_diff"] = df["loser_clutch_idx"] - df["winner_clutch_idx"]
        df2["inactivity_decay_weight"] = df["loser_inactivity_decay"] - df["winner_inactivity_decay"]
        df2["home_adv_diff"] = df["loser_home"] - df["winner_home"]
        df2["altitude"] = df["altitude"]
        df2["indoor"] = df["indoor"]
        df2["humidity_impact"] = df["humidity_impact"]
        df2["temperature_impact"] = df["temperature_impact"]
        df2["market_sentiment_signal"] = df["market_sentiment_signal"]
        df2["points_defending_pct"] = df["loser_pts_def_ratio"] - df["winner_pts_def_ratio"]
        df2["pre_slam_fatigue"] = df["pre_slam_fatigue"]
        df2["style_drift_detected"] = (
            (df["winner_style_drift_detected"] > 0.5) | (df["loser_style_drift_detected"] > 0.5)
        ).astype(float)
        df2["style_cluster_distance_diff"] = (
            df["loser_style_cluster_distance"] - df["winner_style_cluster_distance"]
        )
        df2["style_matchup_bias"] = -df["style_matchup_bias"]
        df2["travel_fatigue_index"] = df["loser_travel_penalty"] - df["winner_travel_penalty"]
        df2["style_cross_surface_impact"] = (
            (df["loser_clutch52"] - df["winner_clutch52"]) * df["surface_speed"].astype(float)
        )
        df2["clutch_diff"] = df["loser_clutch52"] - df["winner_clutch52"]
        df2["target"] = 0

        dataset = pd.concat([df1, df2]).sort_values("tourney_date").reset_index(drop=True)
        surface_map = {"Hard": 0, "Clay": 1, "Grass": 2, "Carpet": 3}
        dataset["surface_encoded"] = dataset["surface"].map(surface_map).fillna(0)
        dataset = dataset.dropna(subset=self.features)
        return dataset

    def train(self):
        dataset = self.prepare_data()
        X = dataset[self.features]
        y = dataset["target"]

        print(f"Taille du dataset d'entraînement: {len(X)} exemples")

        split_idx = int(len(dataset) * 0.8)
        X_train, y_train = X.iloc[:split_idx], y.iloc[:split_idx]
        X_test, y_test = X.iloc[split_idx:], y.iloc[split_idx:]

        # Sample weights amplify the importance of high-signal matches:
        # - rows with strong Micro-Elo gaps on serve or return
        # - WTA rows get a fixed boost so the ~20 % volume slice keeps signal
        #   strength comparable to ATP during gradient updates (v3.5 WTA branch).
        serv_abs = X_train["service_elo_diff"].abs()
        ret_abs = X_train["return_elo_diff"].abs()
        q75_serv = max(float(serv_abs.quantile(0.75)) if not serv_abs.empty else 100.0, 50.0)
        q75_ret = max(float(ret_abs.quantile(0.75)) if not ret_abs.empty else 100.0, 50.0)
        wta_mask = (X_train["tour_encoded"] == 1.0).astype(float)
        wta_boost = 1.0 + 0.5 * wta_mask  # 1.5× for WTA rows
        w_train = (
            wta_boost.values
            * (
                1.0
                + 0.65 * (serv_abs / q75_serv).clip(upper=1.0)
                + 0.65 * (ret_abs / q75_ret).clip(upper=1.0)
            ).values
        )

        print("Entraînement XGBoost (max_depth=4, more regularization)...")
        base_model = XGBClassifier(
            n_estimators=600,
            max_depth=4,
            min_child_weight=10,
            learning_rate=0.03,
            subsample=0.9,
            colsample_bytree=0.9,
            reg_alpha=0.5,
            reg_lambda=1.5,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
            n_jobs=4,
        )
        base_model.fit(X_train, y_train, sample_weight=w_train)

        print("Calibration des probabilités (sigmoid, TimeSeriesSplit=3)...")
        tscv = TimeSeriesSplit(n_splits=3)
        calibrated = CalibratedClassifierCV(base_model, method="sigmoid", cv=tscv)
        calibrated.fit(X_train, y_train, sample_weight=w_train)
        self.model = calibrated

        # Calibration dédiée Clay pour limiter les biais cross-surface en prédiction sur terre.
        self.model_clay = None
        clay_mask = X_train["surface_encoded"] == 1
        if clay_mask.sum() > 500:
            X_train_clay = X_train[clay_mask]
            y_train_clay = y_train[clay_mask]
            w_train_clay = w_train[clay_mask.values]
            if len(np.unique(y_train_clay)) >= 2:
                print("Calibration spécifique Clay (sigmoid, TimeSeriesSplit=3)...")
                tscv_clay = TimeSeriesSplit(n_splits=3)
                calibrated_clay = CalibratedClassifierCV(base_model, method="sigmoid", cv=tscv_clay)
                calibrated_clay.fit(X_train_clay, y_train_clay, sample_weight=w_train_clay)
                self.model_clay = calibrated_clay

        # Calibration segmentée surface x niveau (v3.4: lower threshold + per-segment volume).
        # The Micro-Elo features now carry stronger point-level signal, so we can
        # afford to calibrate slightly smaller segments (400 instead of 600). Each
        # segment also stores its training-set size so predict_match can adapt the
        # segment-vs-global blend dynamically (small segment -> more global mix).
        self.model_segments = {}
        self.segment_train_sizes = {}
        seg_defs = [
            ("Hard_G", 0.0, 3.0, None),
            ("Hard_M", 0.0, 2.0, None),
            ("Hard_A", 0.0, 1.0, None),
            ("Clay_G", 1.0, 3.0, None),
            ("Clay_M", 1.0, 2.0, None),
            ("Clay_A", 1.0, 1.0, None),
            ("Grass_G", 2.0, 3.0, None),
            # WTA-specific calibration on Clay 1000 (Rome / Madrid) — v3.5 WTA branch.
            ("WTA_Clay_M", 1.0, 2.0, 1.0),
        ]
        SEG_MIN = 400
        for seg_def in seg_defs:
            seg_name, surf_code, lvl_code, tour_code = seg_def
            seg_mask = (X_train["surface_encoded"] == surf_code) & (X_train["tournament_level_encoded"] == lvl_code)
            if tour_code is not None:
                seg_mask = seg_mask & (X_train["tour_encoded"] == tour_code)
            n_seg = int(seg_mask.sum())
            if n_seg < SEG_MIN:
                continue
            y_seg = y_train[seg_mask]
            w_seg = w_train[seg_mask.values]
            if len(np.unique(y_seg)) < 2:
                continue
            seg_model = CalibratedClassifierCV(base_model, method="sigmoid", cv=TimeSeriesSplit(n_splits=3))
            seg_model.fit(X_train[seg_mask], y_seg, sample_weight=w_seg)
            self.model_segments[seg_name] = seg_model
            self.segment_train_sizes[seg_name] = n_seg
            print(f"  Segment {seg_name}: trained on n={n_seg}")

        y_pred = self.model.predict(X_test)
        y_prob = self.model.predict_proba(X_test)[:, 1]

        acc = accuracy_score(y_test, y_pred)
        brier = brier_score_loss(y_test, y_prob)
        print(f"\nPrécision sur jeu de test temporel: {acc:.4f}")
        print(f"Brier score (plus bas = mieux): {brier:.4f}")
        print("\nRapport de classification:")
        print(classification_report(y_test, y_pred))
        print("\nÉvaluation dédiée par segment (surface x niveau):")
        seg_eval = []
        for seg_name, seg_model in self.model_segments.items():
            try:
                srf, lvl = seg_name.split("_", 1)
                surf_code = {"Hard": 0.0, "Clay": 1.0, "Grass": 2.0, "Carpet": 3.0}.get(srf, 0.0)
                lvl_code = self._encode_tourney_level(lvl)
                mask = (X_test["surface_encoded"] == surf_code) & (X_test["tournament_level_encoded"] == lvl_code)
                if int(mask.sum()) < 100:
                    continue
                y_seg = y_test[mask]
                p_seg = seg_model.predict_proba(X_test[mask])[:, 1]
                y_hat = (p_seg >= 0.5).astype(int)
                seg_eval.append((seg_name, int(mask.sum()), accuracy_score(y_seg, y_hat), brier_score_loss(y_seg, p_seg)))
            except Exception:
                continue
        if seg_eval:
            for name, n, acc_s, brier_s in sorted(seg_eval, key=lambda x: x[0]):
                print(f"- {name}: n={n}, acc={acc_s:.4f}, brier={brier_s:.4f}")
        else:
            print("- Aucun segment assez grand pour évaluation dédiée.")

        self.global_test_brier = float(brier_score_loss(y_test, y_prob))
        self.segment_brier_scores = {}
        for seg_name, seg_model in self.model_segments.items():
            try:
                if seg_name.startswith("WTA_"):
                    parts = seg_name.split("_", 2)
                    surf_map = {"Hard": 0.0, "Clay": 1.0, "Grass": 2.0}
                    surf_code = float(surf_map.get(parts[1], 0.0))
                    lvl_code = self._encode_tourney_level(parts[2])
                    mask = (
                        (X_test["surface_encoded"] == surf_code)
                        & (X_test["tournament_level_encoded"] == lvl_code)
                        & (X_test["tour_encoded"] == 1.0)
                    )
                else:
                    srf, lvl = seg_name.split("_", 1)
                    surf_map = {"Hard": 0.0, "Clay": 1.0, "Grass": 2.0}
                    surf_code = float(surf_map.get(srf, 0.0))
                    lvl_code = self._encode_tourney_level(lvl)
                    mask = (X_test["surface_encoded"] == surf_code) & (
                        X_test["tournament_level_encoded"] == lvl_code
                    )
                if int(mask.sum()) < 50:
                    continue
                p_seg = seg_model.predict_proba(X_test[mask])[:, 1]
                self.segment_brier_scores[seg_name] = float(brier_score_loss(y_test[mask], p_seg))
            except Exception:
                continue
        print(f"\nBrier global (test): {self.global_test_brier:.4f}")
        if self.segment_brier_scores:
            print("Brier par segment (test, probas segment):")
            for k, v in sorted(self.segment_brier_scores.items()):
                print(f"  - {k}: {v:.4f}")

        importances = pd.DataFrame({
            "Feature": self.features,
            "Importance": base_model.feature_importances_,
        }).sort_values("Importance", ascending=False)
        print("\nImportance des features:")
        print(importances)

        # Save importance chart
        os.makedirs(os.path.dirname(self.feature_plot_path), exist_ok=True)
        plt.figure(figsize=(10, 6))
        top = importances.head(15).iloc[::-1]
        plt.barh(top["Feature"], top["Importance"])
        plt.title("Feature Importance - XGBoost Tennis v4.5 (human factors)")
        plt.xlabel("Importance")
        plt.tight_layout()
        plt.savefig(self.feature_plot_path, dpi=140)
        plt.close()
        print(f"Graphique feature importance sauvegardé: {self.feature_plot_path}")

        self._print_player_style_cluster_report()

        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        bundle = {
            "model": self.model,
            "model_clay": self.model_clay,
            "model_segments": self.model_segments,
            "segment_train_sizes": self.segment_train_sizes,
            "player_elo": self.player_elo,
            "player_surface_elo": self.player_surface_elo,
            "player_name_elo": self.player_name_elo,
            "player_name_surface_elo": self.player_name_surface_elo,
            "player_surface_count": self.player_surface_count,
            "player_surface_count_name": self.player_surface_count_name,
            "player_last_seen": self.player_last_seen,
            "player_last_seen_name": self.player_last_seen_name,
            "player_service_elo": self.player_service_elo,
            "player_return_elo": self.player_return_elo,
            "player_service_elo_surface": self.player_service_elo_surface,
            "player_return_elo_surface": self.player_return_elo_surface,
            "player_surface_micro_count": self.player_surface_micro_count,
            "player_name_service_elo": self.player_name_service_elo,
            "player_name_return_elo": self.player_name_return_elo,
            "micro_elo_scale": self.micro_elo_scale,
            "micro_elo_k_serve": self.micro_elo_k_serve,
            "micro_speed_baseline": self.micro_speed_baseline,
            "micro_speed_alpha": self.micro_speed_alpha,
            "elo_decay_tau_days": self.elo_decay_tau_days,
            "surface_blend_n0": self.surface_blend_n0,
            "segment_blend_weight": self.segment_blend_weight,
            "features": self.features,
            "segment_brier_scores": self.segment_brier_scores,
            "global_test_brier": self.global_test_brier,
            "style_kmeans": self.style_kmeans,
            "style_rank_map": self.style_rank_map,
            "style_semantic_calibration": self.style_semantic_calibration,
            "style_surface_winrate_index": self.style_surface_winrate_index,
        }
        joblib.dump(bundle, self.model_path)
        print(f"\nModèle sauvegardé sous: {self.model_path}")

    def _load_bundle_if_needed(self):
        if self.model is not None:
            return
        if not os.path.exists(self.model_path):
            raise Exception("Modèle non entraîné et non trouvé.")
        loaded = joblib.load(self.model_path)
        if isinstance(loaded, dict) and "model" in loaded:
            self.model = loaded["model"]
            self.model_clay = loaded.get("model_clay")
            self.model_segments = loaded.get("model_segments", {})
            self.segment_train_sizes = loaded.get("segment_train_sizes", {})
            self.player_elo = loaded.get("player_elo", {})
            self.player_surface_elo = loaded.get("player_surface_elo", {})
            self.player_name_elo = loaded.get("player_name_elo", {})
            self.player_name_surface_elo = loaded.get("player_name_surface_elo", {})
            self.player_surface_count = loaded.get("player_surface_count", {})
            self.player_surface_count_name = loaded.get("player_surface_count_name", {})
            self.player_last_seen = loaded.get("player_last_seen", {})
            self.player_last_seen_name = loaded.get("player_last_seen_name", {})
            self.player_service_elo = loaded.get("player_service_elo", {})
            self.player_return_elo = loaded.get("player_return_elo", {})
            self.player_service_elo_surface = loaded.get("player_service_elo_surface", {})
            self.player_return_elo_surface = loaded.get("player_return_elo_surface", {})
            self.player_surface_micro_count = loaded.get("player_surface_micro_count", {})
            self.player_name_service_elo = loaded.get("player_name_service_elo", {})
            self.player_name_return_elo = loaded.get("player_name_return_elo", {})
            self.micro_elo_scale = float(loaded.get("micro_elo_scale", 200.0))
            self.micro_elo_k_serve = float(loaded.get("micro_elo_k_serve", 0.45))
            self.micro_speed_baseline = float(
                loaded.get("micro_speed_baseline", loaded.get("micro_elo_cpi_baseline", 0.75))
            )
            if self.micro_speed_baseline > 5.0:
                self.micro_speed_baseline = 0.75
            self.micro_speed_alpha = float(loaded.get("micro_speed_alpha", loaded.get("micro_elo_cpi_alpha", 0.35)))
            if self.micro_speed_alpha > 2.0:
                self.micro_speed_alpha = 0.35
            # Macro tunables
            self.elo_decay_tau_days = float(loaded.get("elo_decay_tau_days", 365.0))
            self.surface_blend_n0 = float(loaded.get("surface_blend_n0", 30.0))
            self.segment_blend_weight = float(loaded.get("segment_blend_weight", 0.7))
            self.features = loaded.get("features", self.features)
            self.segment_brier_scores = loaded.get("segment_brier_scores", {})
            self.global_test_brier = float(loaded.get("global_test_brier", 0.12))
            self.style_kmeans = loaded.get("style_kmeans")
            self.style_rank_map = loaded.get("style_rank_map")
            self.style_semantic_calibration = loaded.get("style_semantic_calibration")
            self.style_surface_winrate_index = loaded.get("style_surface_winrate_index", {})
        else:
            self.model = loaded
            self.model_clay = None

    def segment_brier_for_key(self, seg_key: object) -> float:
        """Brier du segment de calibration (test hold-out) ; repli sur le Brier global."""
        return resolve_segment_brier_score(self, seg_key)

    @staticmethod
    def defending_ratio_live(
        db_path: str,
        player_id,
        tourney_name: object,
        match_date,
        current_points: float,
        tour: str,
    ) -> float:
        """Ratio (proxy) points à défendre N-1 / points actuels — données strictement avant match."""
        try:
            pts = max(1.0, float(current_points))
            yp = int(pd.Timestamp(match_date).year) - 1
        except Exception:
            return 0.0
        nt = TennisMLModel._normalize_tourney_key(tourney_name)
        if len(nt) < 3:
            return 0.0
        key_tok = nt.split()[0][:12]
        tour_u = str(tour or "ATP").upper()
        conn = sqlite3.connect(db_path)
        df = None
        try:
            if tour_u == "WTA":
                df = pd.read_sql(
                    "SELECT * FROM wta_matches WHERE CAST(strftime('%Y', tourney_date) AS INTEGER)=?",
                    conn,
                    params=(yp,),
                )
            else:
                df = pd.read_sql(
                    "SELECT * FROM matches_recent WHERE source='tennismylife' "
                    "AND CAST(strftime('%Y', tourney_date) AS INTEGER)=?",
                    conn,
                    params=(yp,),
                )
        except Exception:
            df = None
        finally:
            conn.close()
        if df is None or df.empty:
            return 0.0
        df["_tn"] = df["tourney_name"].astype(str).str.lower()
        sub = df[df["_tn"].str.contains(re.escape(key_tok), regex=False, na=False)]
        if sub.empty:
            return 0.0
        best_rd = 0
        tw = str(tour_u)
        raw_pid = str(player_id or "").strip()
        for _, row in sub.iterrows():
            if tw == "WTA":
                try:
                    wid = int(float(row.get("winner_id")))
                    lid = int(float(row.get("loser_id")))
                    p_int = int(float(str(raw_pid).replace("WTA::", "")))
                except Exception:
                    continue
                if p_int not in (wid, lid):
                    continue
            else:
                wk = f"ATP::{row.get('winner_id')}"
                lk = f"ATP::{row.get('loser_id')}"
                pk = raw_pid if raw_pid.startswith("ATP::") else f"ATP::{raw_pid}"
                if pk not in (wk, lk):
                    continue
            best_rd = max(best_rd, int(TennisMLModel._round_depth(row.get("round"))))
        lvl = sub.iloc[0].get("tourney_level")
        appx = TennisMLModel._approx_defending_points(best_rd, lvl)
        return float(min(3.0, appx / pts))

    def predict_match(
        self,
        surface,
        p1_rank,
        p2_rank,
        p1_age,
        p2_age,
        p1_ht,
        p2_ht,
        p1_pts,
        p2_pts,
        p1_id=None,
        p2_id=None,
        p1_name=None,
        p2_name=None,
        p1_form_win_pct_90=None,
        p2_form_win_pct_90=None,
        p1_fatigue_minutes_14=None,
        p2_fatigue_minutes_14=None,
        p1_fatigue_matches_14=None,
        p2_fatigue_matches_14=None,
        p1_hand=None,
        p2_hand=None,
        h2h_p1_wins=None,
        h2h_p2_wins=None,
        tournament_name=None,
        p1_ioc=None,
        p2_ioc=None,
        p1_days_since_last_match=None,
        p2_days_since_last_match=None,
        p1_workload7_minutes=None,  # Deprecated, kept for backward-compat (ignored)
        p2_workload7_minutes=None,  # Deprecated, kept for backward-compat (ignored)
        p1_workload7_sets=None,     # Deprecated, kept for backward-compat (ignored)
        p2_workload7_sets=None,     # Deprecated, kept for backward-compat (ignored)
        p1_wins_last7d=None,
        p2_wins_last7d=None,
        p1_three_setters_last14d=None,
        p2_three_setters_last14d=None,
        p1_last_round_reached=None,
        p2_last_round_reached=None,
        p1_second_srv_ratio3=None,
        p2_second_srv_ratio3=None,
        p1_form_surface_win_pct_90=None,
        p2_form_surface_win_pct_90=None,
        p1_hold_surface=None,
        p2_hold_surface=None,
        p1_break_surface=None,
        p2_break_surface=None,
        p1_first_srv_win10=None,
        p2_first_srv_win10=None,
        p1_bp_conv10=None,
        p2_bp_conv10=None,
        p1_dominance_ratio=None,
        p2_dominance_ratio=None,
        p1_elo_surface_recent=None,
        p2_elo_surface_recent=None,
        p1_style_advantage_score=None,
        p1_clutch_index=None,
        p2_clutch_index=None,
        p1_speed_affinity=None,
        p2_speed_affinity=None,
        p1_speed_performance_delta=None,
        p2_speed_performance_delta=None,
        p1_bp_resilience=None,
        p2_bp_resilience=None,
        tournament_level=None,
        tour=None,
        humidity_pct=None,
        temp_c=None,
        opening_odd_p1=None,
        current_odd_p1=None,
        p1_points_def_ratio=None,
        p2_points_def_ratio=None,
        p1_tac_ace=None,
        p1_tac_f1_pct=None,
        p1_tac_bp_saved_pct=None,
        p1_tac_hold_pct=None,
        p2_tac_ace=None,
        p2_tac_f1_pct=None,
        p2_tac_bp_saved_pct=None,
        p2_tac_hold_pct=None,
        p1_travel_penalty_index=None,
        p2_travel_penalty_index=None,
        p1_clutch52=None,
        p2_clutch52=None,
        match_date=None,
    ):
        self._load_bundle_if_needed()

        surface_map = {"Hard": 0, "Clay": 1, "Grass": 2, "Carpet": 3}
        surface_encoded = surface_map.get(surface, 0)

        # Tour prefix used for ratings keys ("ATP::id"/"WTA::id" and same for names).
        tour_label = (str(tour).upper() if tour else "ATP")
        if tour_label not in ("ATP", "WTA"):
            tour_label = "ATP"
        tour_encoded = 1.0 if tour_label == "WTA" else 0.0

        base_elo = 1500.0
        p1k_raw = self._pid_key(p1_id)
        p2k_raw = self._pid_key(p2_id)
        # If caller passed a raw id, prefix it; if it already starts with ATP::/WTA::, keep as-is.
        def _prefix_id(raw):
            if not raw:
                return None
            return raw if raw.startswith(("ATP::", "WTA::")) else f"{tour_label}::{raw}"
        p1k = _prefix_id(p1k_raw)
        p2k = _prefix_id(p2k_raw)
        p1n = self._aligned_index_name_key(p1_name)
        p2n = self._aligned_index_name_key(p2_name)
        p1n_key = f"{tour_label}::{p1n}" if p1n else None
        p2n_key = f"{tour_label}::{p2n}" if p2n else None

        # Reference "now" used for time decay. We pick the most recent last_seen
        # across all stored players as a robust proxy for "as of training cutoff",
        # then advance to today. Players who haven't played since training will
        # naturally see their Elo decay toward base_elo.
        try:
            now_ts = pd.Timestamp.now("UTC").tz_localize(None)
        except Exception:
            now_ts = pd.Timestamp.now()

        tau = float(getattr(self, "elo_decay_tau_days", 365.0))
        n0 = float(getattr(self, "surface_blend_n0", 30.0))

        def _decay_to_now(rating, last_seen_iso):
            if last_seen_iso is None:
                return rating
            try:
                last_dt = pd.Timestamp(last_seen_iso)
            except Exception:
                return rating
            delta_days = (now_ts - last_dt).days
            if delta_days <= 14:
                return rating
            factor = float(np.exp(-delta_days / tau))
            return base_elo + (rating - base_elo) * factor

        def _resolve_elo(pk, pn_key):
            """Return (global_elo, surface_elo_raw, surface_count, last_seen, source).

            ``source`` ∈ {"bundle", "default"} — ``bundle`` when a rating was found in
            the trained maps (by player id, legacy name dict, or **name key in player_elo**,
            which is how micro-Elo stores name-only players).
            """
            ge_raw = se_raw = base_elo
            n_surf = 0
            last_seen_iso = None
            if pk and pk in self.player_elo:
                ge_raw = self.player_elo[pk]
                se_raw = self.player_surface_elo.get((pk, surface), ge_raw)
                n_surf = int(self.player_surface_count.get((pk, surface), 0))
                last_seen_iso = self.player_last_seen.get(pk)
            elif pn_key and pn_key in self.player_name_elo:
                ge_raw = self.player_name_elo[pn_key]
                se_raw = self.player_name_surface_elo.get((pn_key, surface), ge_raw)
                n_surf = int(self.player_surface_count_name.get((pn_key, surface), 0))
                last_seen_iso = self.player_last_seen_name.get(pn_key)
            elif pn_key and pn_key in self.player_elo:
                # Name-keyed rows (e.g. WTA::bartunkova k) live in player_elo, not player_name_elo.
                ge_raw = self.player_elo[pn_key]
                se_raw = self.player_surface_elo.get((pn_key, surface), ge_raw)
                n_surf = int(self.player_surface_count.get((pn_key, surface), 0))
                last_seen_iso = self.player_last_seen.get(pn_key)
            else:
                return base_elo, base_elo, 0, None, "default"
            ge = _decay_to_now(ge_raw, last_seen_iso)
            se = _decay_to_now(se_raw, last_seen_iso)
            return ge, se, n_surf, last_seen_iso, "bundle"

        def _effective_micro(pk, pn_key):
            """Blended micro service/return Elos for this surface: α = n/(n+30), plus decay."""
            sid = pk if pk and pk in self.player_service_elo else None
            if sid is None and pn_key and pn_key in getattr(self, "player_name_service_elo", {}):
                sid = pn_key
            if sid is None and pn_key and pn_key in self.player_service_elo:
                sid = pn_key
            if sid is None:
                return base_elo, base_elo, 0.0, "default"
            last_iso = self.player_last_seen.get(sid)
            wg = _decay_to_now(float(self.player_service_elo.get(sid, base_elo)), last_iso)
            wr_g = _decay_to_now(float(self.player_return_elo.get(sid, base_elo)), last_iso)
            sk = (sid, str(surface))
            n_loc = int(self.player_surface_micro_count.get(sk, 0))
            alpha_m = n_loc / (n_loc + n0) if n_loc > 0 else 0.0
            ws_raw = float(self.player_service_elo_surface.get(sk, base_elo))
            wr_raw = float(self.player_return_elo_surface.get(sk, base_elo))
            ws = _decay_to_now(ws_raw, last_iso)
            wrs = _decay_to_now(wr_raw, last_iso)
            eff_s = alpha_m * ws + (1.0 - alpha_m) * wg
            eff_r = alpha_m * wrs + (1.0 - alpha_m) * wr_g
            return eff_s, eff_r, alpha_m, "bundle"

        p1_global_elo, p1_surface_elo_raw, p1_n_surf, p1_last_seen, p1_g_src = _resolve_elo(
            p1k, p1n_key
        )
        p2_global_elo, p2_surface_elo_raw, p2_n_surf, p2_last_seen, p2_g_src = _resolve_elo(
            p2k, p2n_key
        )
        p1_service_elo, p1_return_elo, p1_micro_alpha, p1_m_src = _effective_micro(p1k, p1n_key)
        p2_service_elo, p2_return_elo, p2_micro_alpha, p2_m_src = _effective_micro(p2k, p2n_key)

        # If global lookup missed but micro found a player, approximate global Elo from micro
        # (same formula as train-time player_elo snapshot = (serve+return)/2 at bundle build).
        if p1_g_src == "default" and p1_m_src == "bundle":
            p1_global_elo = (p1_service_elo + p1_return_elo) / 2.0
            p1_g_src = "micro_avg"
        if p2_g_src == "default" and p2_m_src == "bundle":
            p2_global_elo = (p2_service_elo + p2_return_elo) / 2.0
            p2_g_src = "micro_avg"

        # Surface-Elo confidence blending: a player with many surface matches keeps
        # their surface Elo intact (alpha->1); a player with few surface matches has
        # their surface Elo pulled toward their global Elo (alpha->0). This naturally
        # damps noisy surface ratings without needing a post-hoc cap.
        alpha_p1 = p1_n_surf / (p1_n_surf + n0) if p1_n_surf > 0 else 0.0
        alpha_p2 = p2_n_surf / (p2_n_surf + n0) if p2_n_surf > 0 else 0.0
        p1_surface_elo = alpha_p1 * p1_surface_elo_raw + (1.0 - alpha_p1) * p1_global_elo
        p2_surface_elo = alpha_p2 * p2_surface_elo_raw + (1.0 - alpha_p2) * p2_global_elo

        p1_form = 0.5 if p1_form_win_pct_90 is None else float(p1_form_win_pct_90) / 100.0
        p2_form = 0.5 if p2_form_win_pct_90 is None else float(p2_form_win_pct_90) / 100.0

        p1_fat_minutes14 = 0.0 if p1_fatigue_minutes_14 is None else float(p1_fatigue_minutes_14)
        p2_fat_minutes14 = 0.0 if p2_fatigue_minutes_14 is None else float(p2_fatigue_minutes_14)

        p1_fat_matches14 = 0.0 if p1_fatigue_matches_14 is None else float(p1_fatigue_matches_14)
        p2_fat_matches14 = 0.0 if p2_fatigue_matches_14 is None else float(p2_fatigue_matches_14)

        # New "workload" features: defaults to 0 (= no signal, neutral diff).
        # If caller doesn't provide them, the diffs will be 0 and the model relies
        # on Elo/points/momentum/etc.
        p1_w7d = 0 if p1_wins_last7d is None else int(p1_wins_last7d)
        p2_w7d = 0 if p2_wins_last7d is None else int(p2_wins_last7d)
        p1_t14 = 0 if p1_three_setters_last14d is None else int(p1_three_setters_last14d)
        p2_t14 = 0 if p2_three_setters_last14d is None else int(p2_three_setters_last14d)
        p1_lr = 0 if p1_last_round_reached is None else int(p1_last_round_reached)
        p2_lr = 0 if p2_last_round_reached is None else int(p2_last_round_reached)

        p1_days = 7.0 if p1_days_since_last_match is None else float(p1_days_since_last_match)
        p2_days = 7.0 if p2_days_since_last_match is None else float(p2_days_since_last_match)

        p1_ssr3 = 0.5 if p1_second_srv_ratio3 is None else float(p1_second_srv_ratio3)
        p2_ssr3 = 0.5 if p2_second_srv_ratio3 is None else float(p2_second_srv_ratio3)
        p1_form_s = p1_form if p1_form_surface_win_pct_90 is None else float(p1_form_surface_win_pct_90) / 100.0
        p2_form_s = p2_form if p2_form_surface_win_pct_90 is None else float(p2_form_surface_win_pct_90) / 100.0
        p1_hold_s = 0.75 if p1_hold_surface is None else float(p1_hold_surface)
        p2_hold_s = 0.75 if p2_hold_surface is None else float(p2_hold_surface)
        p1_break_s = 0.20 if p1_break_surface is None else float(p1_break_surface)
        p2_break_s = 0.20 if p2_break_surface is None else float(p2_break_surface)
        p1_first10 = 0.68 if p1_first_srv_win10 is None else float(p1_first_srv_win10)
        p2_first10 = 0.68 if p2_first_srv_win10 is None else float(p2_first_srv_win10)
        p1_bp10 = 0.38 if p1_bp_conv10 is None else float(p1_bp_conv10)
        p2_bp10 = 0.38 if p2_bp_conv10 is None else float(p2_bp_conv10)
        p1_dom = 1.00 if p1_dominance_ratio is None else float(p1_dominance_ratio)
        p2_dom = 1.00 if p2_dominance_ratio is None else float(p2_dominance_ratio)
        p1_elo_s_recent = 0.0 if p1_elo_surface_recent is None else float(p1_elo_surface_recent)
        p2_elo_s_recent = 0.0 if p2_elo_surface_recent is None else float(p2_elo_surface_recent)

        p1_h = "U" if p1_hand is None else p1_hand
        p2_h = "U" if p2_hand is None else p2_hand

        total_h2h = (int(h2h_p1_wins) if h2h_p1_wins is not None else 0) + (int(h2h_p2_wins) if h2h_p2_wins is not None else 0)
        h2h_ratio = (float(h2h_p1_wins) / total_h2h) if total_h2h > 0 else 0.5
        h2h_sig = 1.0 if total_h2h >= 4 else 0.0
        style_adv = 0.5 if p1_style_advantage_score is None else float(p1_style_advantage_score)
        p1_clutch = 0.5 if p1_clutch_index is None else float(p1_clutch_index)
        p2_clutch = 0.5 if p2_clutch_index is None else float(p2_clutch_index)
        p1_inact_decay = self._inactivity_decay(p1_days)
        p2_inact_decay = self._inactivity_decay(p2_days)

        altitude, indoor, country_ioc = self._infer_tournament_context(tournament_name)
        p1_home = 1.0 if (country_ioc and p1_ioc and p1_ioc == country_ioc) else 0.0
        p2_home = 1.0 if (country_ioc and p2_ioc and p2_ioc == country_ioc) else 0.0

        hp = None if humidity_pct is None else float(humidity_pct)
        tc = None if temp_c is None else float(temp_c)
        outdoor_m = infer_outdoor("I" if float(indoor) >= 0.5 else None, tournament_name)
        base_cpi = float(lookup_surface_speed(tournament_name, surface))
        match_surface_speed = float(effective_surface_speed_cpi(base_cpi, outdoor_m, hp, tc))
        him, tim = weather_impact_scalars(outdoor_m, hp, tc)

        p1_vec = np.array(
            [
                0.08 if p1_tac_ace is None else float(p1_tac_ace),
                0.62 if p1_tac_f1_pct is None else float(p1_tac_f1_pct),
                0.58 if p1_tac_bp_saved_pct is None else float(p1_tac_bp_saved_pct),
                0.75 if p1_tac_hold_pct is None else float(p1_tac_hold_pct),
            ],
            dtype=float,
        )
        p2_vec = np.array(
            [
                0.08 if p2_tac_ace is None else float(p2_tac_ace),
                0.62 if p2_tac_f1_pct is None else float(p2_tac_f1_pct),
                0.58 if p2_tac_bp_saved_pct is None else float(p2_tac_bp_saved_pct),
                0.75 if p2_tac_hold_pct is None else float(p2_tac_hold_pct),
            ],
            dtype=float,
        )
        km_live = getattr(self, "style_kmeans", None)
        rk_live = getattr(self, "style_rank_map", None)
        # `km.predict` donne un id brut sklearn ; `style_rank_map` (persisté bundle) impose le rang sémantique
        # stable (0 Big Server … 3 CP) avant `style_matchup_bias_from_ranks`.
        rk_np = (
            np.asarray(rk_live, dtype=int).ravel()
            if rk_live is not None
            else np.array([], dtype=int)
        )
        _sy = {
            "style_prior_value": 0.0,
            "style_is_slow_surface": bool(str(surface).lower() == "clay" or match_surface_speed < 0.65),
        }
        n_k = int(km_live.n_clusters) if km_live is not None else 0
        if km_live is not None and rk_np.size == n_k > 0:
            _rp1, p1_style_name = assign_player_style(p1_vec, km_live, rk_np)
            _rp2, p2_style_name = assign_player_style(p2_vec, km_live, rk_np)
            p1_recent_vec = np.array(
                [
                    0.08 if p1_tac_ace is None else float(p1_tac_ace),
                    p1_first10,
                    p1_bp10,
                    p1_hold_s,
                ],
                dtype=float,
            )
            p2_recent_vec = np.array(
                [
                    0.08 if p2_tac_ace is None else float(p2_tac_ace),
                    p2_first10,
                    p2_bp10,
                    p2_hold_s,
                ],
                dtype=float,
            )
            p1_cur_cid = int(km_live.predict(p1_recent_vec.reshape(1, -1))[0])
            p2_cur_cid = int(km_live.predict(p2_recent_vec.reshape(1, -1))[0])
            p1_cur_rank = int(rk_np[p1_cur_cid])
            p2_cur_rank = int(rk_np[p2_cur_cid])
            p1_stable_rank = int(_rp1)
            p2_stable_rank = int(_rp2)
            p1_drift = 1.0 if p1_stable_rank != p1_cur_rank else 0.0
            p2_drift = 1.0 if p2_stable_rank != p2_cur_rank else 0.0
            style_drift_detected = 1.0 if (p1_drift > 0.5 or p2_drift > 0.5) else 0.0
            centers = np.asarray(km_live.cluster_centers_, dtype=float)
            p1_dist = float(np.linalg.norm(p1_vec - centers[int(km_live.predict(p1_vec.reshape(1, -1))[0])]))
            p2_dist = float(np.linalg.norm(p2_vec - centers[int(km_live.predict(p2_vec.reshape(1, -1))[0])]))
            style_cluster_distance_diff = float(p1_dist - p2_dist)
            _sy = self._get_refined_matchup_bias(
                p1_style_name=p1_style_name,
                p2_style_name=p2_style_name,
                surface=str(surface),
                surface_speed=float(match_surface_speed),
                tour_label=tour_label,
            )
            smb = float(_sy["style_matchup_bias"])
            if style_drift_detected > 0.5:
                smb *= 0.8
            style_key = str(_sy["style_interaction_key"])
            style_wr = float(_sy["style_surface_winrate"])
            style_n = int(_sy["style_surface_samples"])
            p1_d = np.linalg.norm(centers - p1_vec.reshape(1, -1), axis=1)
            p2_d = np.linalg.norm(centers - p2_vec.reshape(1, -1), axis=1)
            p1_sim = 1.0 / (1.0 + p1_d)
            p2_sim = 1.0 / (1.0 + p2_d)
            p1_mix = (p1_sim / max(1e-12, float(np.sum(p1_sim)))).tolist()
            p2_mix = (p2_sim / max(1e-12, float(np.sum(p2_sim)))).tolist()
        else:
            p1_style_name = "—"
            p2_style_name = "—"
            smb = 0.0
            style_key = ""
            style_wr = 0.5
            style_n = 0
            p1_drift = p2_drift = 0.0
            style_drift_detected = 0.0
            style_cluster_distance_diff = 0.0
            p1_dist = p2_dist = 0.0
            p1_mix = [0.25, 0.25, 0.25, 0.25]
            p2_mix = [0.25, 0.25, 0.25, 0.25]
        tr1 = 0.0 if p1_travel_penalty_index is None else float(p1_travel_penalty_index)
        tr2 = 0.0 if p2_travel_penalty_index is None else float(p2_travel_penalty_index)
        travel_diff = float(tr1 - tr2)
        c52_1 = 0.5 if p1_clutch52 is None else float(p1_clutch52)
        c52_2 = 0.5 if p2_clutch52 is None else float(p2_clutch52)
        clutch_diff_feat = float(c52_1 - c52_2)
        style_cross_surface_impact = float(clutch_diff_feat * match_surface_speed)

        msig = 0.0
        try:
            if opening_odd_p1 is not None and current_odd_p1 is not None:
                msig = float(
                    ValueDetector.market_sentiment_signal_p1(opening_odd_p1, current_odd_p1)
                )
        except Exception:
            msig = 0.0

        r1 = 0.0 if p1_points_def_ratio is None else float(p1_points_def_ratio)
        r2 = 0.0 if p2_points_def_ratio is None else float(p2_points_def_ratio)
        pts_def_diff = r1 - r2

        try:
            md = match_date if match_date is not None else pd.Timestamp.now()
            md = pd.Timestamp(md)
            pre_sl = 1.0 if self._is_pre_slam_fatigue_week(md) else 0.0
        except Exception:
            pre_sl = 0.0
        sa1 = 0.0 if p1_speed_affinity is None else float(p1_speed_affinity)
        sa2 = 0.0 if p2_speed_affinity is None else float(p2_speed_affinity)
        sd1 = 0.0 if p1_speed_performance_delta is None else float(p1_speed_performance_delta)
        sd2 = 0.0 if p2_speed_performance_delta is None else float(p2_speed_performance_delta)
        speed_affinity_diff = sa1 - sa2
        speed_performance_delta_diff = sd1 - sd2
        service_elo_diff = p1_service_elo - p2_service_elo
        return_elo_diff = p1_return_elo - p2_return_elo
        serve_speed_interaction = service_elo_diff * match_surface_speed
        # WTA-only triplet: zero on ATP via tour_encoded gate.
        is_wta = 1.0 if tour_encoded == 1.0 else 0.0
        p1_bp_res = 0.5 if p1_bp_resilience is None else float(p1_bp_resilience)
        p2_bp_res = 0.5 if p2_bp_resilience is None else float(p2_bp_resilience)
        wta_weighted_advantage = is_wta * (0.6 * return_elo_diff + 0.4 * service_elo_diff)
        wta_speed_power_impact = is_wta * serve_speed_interaction
        wta_break_point_resilience = is_wta * (p1_bp_res - p2_bp_res)

        # garde-fou: si Elo+H2H peu informatifs, on limite encore l'amplitude des nouveaux
        # signaux causaux pour éviter qu'ils dominent dans l'incertitude.
        weak_elo_h2h = (
            total_h2h == 0
            and abs(p1_global_elo - p2_global_elo) < 35
            and abs(p1_surface_elo - p2_surface_elo) < 45
        )
        if weak_elo_h2h:
            p1_w7d = int(np.clip(p1_w7d, 0, 6))
            p2_w7d = int(np.clip(p2_w7d, 0, 6))
            p1_t14 = int(np.clip(p1_t14, 0, 4))
            p2_t14 = int(np.clip(p2_t14, 0, 4))

        X_new = pd.DataFrame([
            {
                "surface_encoded": surface_encoded,
                "tournament_level_encoded": self._encode_tourney_level(tournament_level or self._infer_tourney_level_from_name(tournament_name)),
                "tour_encoded": tour_encoded,
                "rank_diff": float(p1_rank) - float(p2_rank),
                "age_diff": float(p1_age) - float(p2_age),
                "ht_diff": float(p1_ht) - float(p2_ht),
                "points_diff": float(p1_pts) - float(p2_pts),
                "service_elo_diff": service_elo_diff,
                "return_elo_diff": return_elo_diff,
                "speed_affinity": speed_affinity_diff,
                "speed_performance_delta": speed_performance_delta_diff,
                "serve_speed_interaction": serve_speed_interaction,
                "wta_weighted_advantage": wta_weighted_advantage,
                "wta_speed_power_impact": wta_speed_power_impact,
                "wta_break_point_resilience": wta_break_point_resilience,
                "wins_last7d_diff": p1_w7d - p2_w7d,
                "three_setters_last14d_diff": p1_t14 - p2_t14,
                "last_round_reached_diff": p1_lr - p2_lr,
                "momentum5_diff": p1_form - p2_form,
                "form90_surface_diff": p1_form_s - p2_form_s,
                "second_srv_ratio3_diff": p1_ssr3 - p2_ssr3,
                "hold_surface_diff": p1_hold_s - p2_hold_s,
                "break_surface_diff": p1_break_s - p2_break_s,
                "first_srv_win10_diff": p1_first10 - p2_first10,
                "bp_conv10_diff": p1_bp10 - p2_bp10,
                "dominance_ratio_diff": p1_dom - p2_dom,
                "elo_surface_recent_diff": p1_elo_s_recent - p2_elo_s_recent,
                "hand_diff": self._encode_hand(p1_h) - self._encode_hand(p2_h),
                "h2h_ratio": h2h_ratio,
                "h2h_significant": h2h_sig,
                "style_advantage_score": style_adv,
                "clutch_index_diff": p1_clutch - p2_clutch,
                "inactivity_decay_weight": p1_inact_decay - p2_inact_decay,
                "home_adv_diff": p1_home - p2_home,
                "altitude": altitude,
                "indoor": indoor,
                "humidity_impact": him,
                "temperature_impact": tim,
                "market_sentiment_signal": msig,
                "points_defending_pct": pts_def_diff,
                "pre_slam_fatigue": pre_sl,
                "style_drift_detected": style_drift_detected,
                "style_cluster_distance_diff": style_cluster_distance_diff,
                "style_matchup_bias": smb,
                "travel_fatigue_index": travel_diff,
                "style_cross_surface_impact": style_cross_surface_impact,
                "clutch_diff": clutch_diff_feat,
            }
        ])

        # Calibration: blend segment (surface×niveau) avec global pour limiter la sur‑ajustement
        # segment overfit on small samples. v3.4 makes the blend *adaptive*: small
        # segments get more global mix, big segments lean fully on the segment.
        #   blend_w = base_w * min(1, n_seg / 1500)
        # so a 400-example segment uses ~27% of the segment-only weight, while
        # a 1500+-example one uses the full base_w (≈0.7).
        using_clay_calibration = (surface == "Clay" and self.model_clay is not None)
        lvl = tournament_level or self._infer_tourney_level_from_name(tournament_name)
        seg_key = f"{surface}_{lvl}"
        # Prefer WTA-specific segment when applicable (v3.5 WTA branch).
        if tour_label == "WTA":
            wta_seg_key = f"WTA_{surface}_{lvl}"
            if wta_seg_key in self.model_segments:
                seg_key = wta_seg_key
        segment_calibration_key = str(seg_key)
        seg_model = self.model_segments.get(seg_key)
        global_probs = self.model.predict_proba(X_new[self.features])[0]
        global_p1 = float(global_probs[1])
        if seg_model is not None:
            seg_probs = seg_model.predict_proba(X_new[self.features])[0]
            seg_p1 = float(seg_probs[1])
            base_w = float(getattr(self, "segment_blend_weight", 0.7))
            n_seg = int(getattr(self, "segment_train_sizes", {}).get(seg_key, 1500))
            volume_factor = max(0.30, min(1.0, n_seg / 1500.0))
            blend_w = base_w * volume_factor
            p1_prob = blend_w * seg_p1 + (1.0 - blend_w) * global_p1
            calibration_used = (
                f"Segment:{seg_key}(n={n_seg})+Global "
                f"({int(blend_w*100)}/{int((1-blend_w)*100)})"
            )
        elif using_clay_calibration:
            clay_probs = self.model_clay.predict_proba(X_new[self.features])[0]
            p1_prob = float(clay_probs[1])
            calibration_used = "Clay"
        else:
            p1_prob = global_p1
            calibration_used = "Globale"
        raw_p1_prob = float(p1_prob)
        caps_applied = []

        # Cap de confiance pour éviter des extrêmes sur matchs de niveau proche
        rank_gap = abs(float(p1_rank) - float(p2_rank))
        surf_elo_gap = abs(p1_surface_elo - p2_surface_elo)
        lvl = tournament_level or self._infer_tourney_level_from_name(tournament_name)
        if lvl == "G":
            if rank_gap <= 20 and surf_elo_gap <= 80:
                p1_prob = max(0.20, min(0.80, p1_prob))
                caps_applied.append("cap_gs_tight")
            elif rank_gap <= 35 and surf_elo_gap <= 120:
                p1_prob = max(0.16, min(0.84, p1_prob))
                caps_applied.append("cap_gs_mid")
        elif lvl == "M":
            if rank_gap <= 20 and surf_elo_gap <= 80:
                p1_prob = max(0.18, min(0.82, p1_prob))
                caps_applied.append("cap_m_tight")
            elif rank_gap <= 35 and surf_elo_gap <= 120:
                p1_prob = max(0.14, min(0.86, p1_prob))
                caps_applied.append("cap_m_mid")
        else:  # ATP 250/500 et assimilés
            if rank_gap <= 20 and surf_elo_gap <= 80:
                p1_prob = max(0.16, min(0.84, p1_prob))
                caps_applied.append("cap_a_tight")
            elif rank_gap <= 35 and surf_elo_gap <= 120:
                p1_prob = max(0.12, min(0.88, p1_prob))
                caps_applied.append("cap_a_mid")

        # Anti-workload-mismatch cap: when fundamentals (Elo + points) clearly favor one
        # player but the model leans the opposite way, override the probability.
        elo_signal = (p1_global_elo - p2_global_elo) / 100.0
        points_signal = (float(p1_pts) - float(p2_pts)) / 500.0
        fundamental_score = elo_signal + points_signal  # >0 favors P1, <0 favors P2

        # NOTE: cap_surface_specialist removed — replaced by the surface-Elo
        # confidence blending applied at lookup time. With blending, a surface
        # specialist with many matches naturally keeps their full surface Elo in
        # surface_elo_diff, while noisy surface Elos are damped toward global Elo.

        # Graduated clamp on direction-mismatch with fundamentals:
        # |fund_score| ≥ 5  -> strong fundamentals dominate (cap to 0.30 / 0.70 against fund.)
        # |fund_score| ≥ 3  -> clear fundamentals (cap to 0.40 / 0.60 against fund.)
        # |fund_score| ≥ 2  -> mild fundamentals (cap to 0.35 / 0.65 against fund.)
        if fundamental_score <= -2.0 and p1_prob > 0.50:
            if fundamental_score <= -5.0:
                p1_prob = min(p1_prob, 0.30)
                caps_applied.append("cap_fund_strong_p2")
            elif fundamental_score <= -3.0:
                p1_prob = min(p1_prob, 0.40)
                caps_applied.append("cap_fund_clear_p2")
            else:
                p1_prob = min(p1_prob, 0.65)
                caps_applied.append("cap_fund_mild_p2")
        elif fundamental_score >= 2.0 and p1_prob < 0.50:
            if fundamental_score >= 5.0:
                p1_prob = max(p1_prob, 0.70)
                caps_applied.append("cap_fund_strong_p1")
            elif fundamental_score >= 3.0:
                p1_prob = max(p1_prob, 0.60)
                caps_applied.append("cap_fund_clear_p1")
            else:
                p1_prob = max(p1_prob, 0.35)
                caps_applied.append("cap_fund_mild_p1")

        # Pression d'incertitude supplémentaire en cas de retour après longue inactivité
        inactivity_max = max(float(p1_days), float(p2_days))
        if inactivity_max > 90:
            p1_prob = max(0.35, min(0.65, p1_prob))
            caps_applied.append("cap_inactivity_90")
        elif inactivity_max > 60:
            p1_prob = max(0.30, min(0.70, p1_prob))
            caps_applied.append("cap_inactivity_60")
        elif inactivity_max > 45:
            p1_prob = max(0.25, min(0.75, p1_prob))
            caps_applied.append("cap_inactivity_45")

        # Ajustement résilience clutch sur match « serrés » au sens probabilité calibrée brute.
        if 0.45 <= raw_p1_prob <= 0.55:
            p1_prob = float(p1_prob) + 0.04 * float(clutch_diff_feat)
            p1_prob = float(np.clip(p1_prob, 0.02, 0.98))
            caps_applied.append("post_clutch_tight")

        confidence = abs(p1_prob - 0.5) * 2.0
        p2_prob = 1.0 - p1_prob
        top_features = self.explain_local_top_features(X_new, top_k=5)

        return {
            "p1_win_prob": p1_prob,
            "p2_win_prob": p2_prob,
            "p1_true_odd": 1 / p1_prob if p1_prob > 0 else 0,
            "p2_true_odd": 1 / p2_prob if p2_prob > 0 else 0,
            "confidence": confidence,
            "calibration_used": calibration_used,
            "top_features": top_features,
            "feature_snapshot": {
                "style_advantage_score": style_adv,
                "clutch_index_diff": (p1_clutch - p2_clutch),
                "inactivity_decay_weight": (p1_inact_decay - p2_inact_decay),
                "first_srv_win10_diff": (p1_first10 - p2_first10),
                "bp_conv10_diff": (p1_bp10 - p2_bp10),
                "dominance_ratio_diff": (p1_dom - p2_dom),
                "weak_elo_h2h_guardrail": 1.0 if weak_elo_h2h else 0.0,
                "raw_p1_prob": raw_p1_prob,
                "capped_p1_prob": p1_prob,
                "caps_applied": caps_applied,
                "p1_days_since_last_match": p1_days,
                "p2_days_since_last_match": p2_days,
                "p1_global_elo": float(p1_global_elo),
                "p2_global_elo": float(p2_global_elo),
                # bundle=from trained maps; default=1500; micro_avg=fallback (serve+return)/2
                "p1_global_elo_tag": p1_g_src,
                "p2_global_elo_tag": p2_g_src,
                "p1_surface_elo": float(p1_surface_elo),
                "p2_surface_elo": float(p2_surface_elo),
                "p1_surface_elo_raw": float(p1_surface_elo_raw),
                "p2_surface_elo_raw": float(p2_surface_elo_raw),
                "p1_surface_match_count": int(p1_n_surf),
                "p2_surface_match_count": int(p2_n_surf),
                "p1_surface_blend_alpha": float(alpha_p1),
                "p2_surface_blend_alpha": float(alpha_p2),
                "p1_last_seen": p1_last_seen,
                "p2_last_seen": p2_last_seen,
                # Micro-Elo (surface-blended)
                "p1_service_elo": float(p1_service_elo),
                "p2_service_elo": float(p2_service_elo),
                "p1_return_elo": float(p1_return_elo),
                "p2_return_elo": float(p2_return_elo),
                "p1_micro_elo_tag": p1_m_src,
                "p2_micro_elo_tag": p2_m_src,
                "p1_micro_blend_alpha": float(p1_micro_alpha),
                "p2_micro_blend_alpha": float(p2_micro_alpha),
                "surface_speed": float(match_surface_speed),
                "service_elo_diff": float(service_elo_diff),
                "return_elo_diff": float(return_elo_diff),
                "serve_speed_interaction": float(serve_speed_interaction),
                "is_wta": float(is_wta),
                "wta_weighted_advantage": float(wta_weighted_advantage),
                "wta_speed_power_impact": float(wta_speed_power_impact),
                "wta_break_point_resilience": float(wta_break_point_resilience),
                "p1_bp_resilience": float(p1_bp_res),
                "p2_bp_resilience": float(p2_bp_res),
                "segment_calibration_key": segment_calibration_key,
                "human_p1_style": p1_style_name,
                "human_p2_style": p2_style_name,
                "style_matchup_bias": float(smb),
                "style_interaction_key": style_key,
                "style_prior_value": float(_sy.get("style_prior_value", 0.0)),
                "style_is_slow_surface": bool(_sy.get("style_is_slow_surface", False)),
                "style_surface_winrate_p1": float(style_wr),
                "style_surface_winrate_samples": int(style_n),
                "style_drift_detected": float(style_drift_detected),
                "p1_style_drift_detected": float(p1_drift),
                "p2_style_drift_detected": float(p2_drift),
                "p1_style_cluster_distance": float(p1_dist),
                "p2_style_cluster_distance": float(p2_dist),
                "style_cluster_distance_diff": float(style_cluster_distance_diff),
                "p1_style_mix": [float(x) for x in p1_mix],
                "p2_style_mix": [float(x) for x in p2_mix],
                "travel_fatigue_index": float(travel_diff),
                "p1_jetlag_alert": float(tr1) >= 0.05,
                "p2_jetlag_alert": float(tr2) >= 0.05,
                "p1_travel_penalty_index": float(tr1),
                "p2_travel_penalty_index": float(tr2),
                "style_cross_surface_impact": float(style_cross_surface_impact),
                "clutch_diff": float(clutch_diff_feat),
                "p1_clutch52": float(c52_1),
                "p2_clutch52": float(c52_2),
            },
            "segment_calibration_key": segment_calibration_key,
        }

    def explain_local_top_features(self, feature_snapshot_df: pd.DataFrame, top_k: int = 5):
        """
        Approximation locale:
        contribution ~ |x_i| * global_importance_i
        (robuste même avec modèle calibré encapsulé).
        """
        try:
            if self.model is None:
                self._load_bundle_if_needed()
            # récupère estimateur xgb sous-jacent si possible
            est = None
            if hasattr(self.model, "calibrated_classifiers_") and self.model.calibrated_classifiers_:
                est = getattr(self.model.calibrated_classifiers_[0], "estimator", None)
            importances = None
            if est is not None and hasattr(est, "feature_importances_"):
                importances = np.array(est.feature_importances_, dtype=float)
            if importances is None or len(importances) != len(self.features):
                importances = np.ones(len(self.features), dtype=float) / float(max(1, len(self.features)))
            x = feature_snapshot_df[self.features].iloc[0].astype(float).values
            contrib = np.abs(x) * np.abs(importances)
            idx = np.argsort(contrib)[::-1][: int(top_k)]
            return [
                {"feature": self.features[i], "score": float(contrib[i]), "value": float(x[i])}
                for i in idx
            ]
        except Exception:
            return []


if __name__ == "__main__":
    ml_engine = TennisMLModel()
    ml_engine.train()

    print("\n--- Test de prédiction ---")
    pred = ml_engine.predict_match(
        surface="Clay",
        p1_rank=1,
        p2_rank=50,
        p1_age=25,
        p2_age=28,
        p1_ht=185,
        p2_ht=180,
        p1_pts=9000,
        p2_pts=1000,
        p1_form_win_pct_90=72,
        p2_form_win_pct_90=48,
        p1_fatigue_minutes_14=240,
        p2_fatigue_minutes_14=420,
        h2h_p1_wins=3,
        h2h_p2_wins=1,
        p1_hand="R",
        p2_hand="L",
        tournament_name="Rome Masters",
    )
    print(f"Probabilité victoire P1: {pred['p1_win_prob']:.2%}")
    print(f"Vraie cote P1: {pred['p1_true_odd']:.2f}")
    print(f"Probabilité victoire P2: {pred['p2_win_prob']:.2%}")
    print(f"Vraie cote P2: {pred['p2_true_odd']:.2f}")
