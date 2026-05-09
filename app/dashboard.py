import streamlit as st
import pandas as pd
import numpy as np
import sys
import os
import glob
import asyncio
import sqlite3
import threading
import time
import subprocess
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from streamlit_autorefresh import st_autorefresh

# Ajouter le répertoire parent au path pour les imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.value_detector import ValueDetector
from scripts.ml_model import TennisMLModel
from scripts.stats_engine import TennisStatsEngine
from scripts.player_identity import to_lastname_initial
from scripts.scraper_prematch import FlashscoreScraper
from scripts.backtest_staking_sim import (
    load_and_filter_bets_csv,
    resolve_backtest_csv,
    simulate_sequential_intraday,
)
from scripts.scraper_results import ResultsScraper
import scripts.scraper_profiles as scraper_profiles
from scripts.scraper_profiles import ProfileScraper
from scripts.data_quality import run_data_quality_checks
from scripts.model_monitor import compute_monthly_diagnostics, compute_feature_drift

PROFILE_CACHE_SCHEMA = getattr(scraper_profiles, "PROFILE_CACHE_VERSION", 1)


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


# Qualité max : scrape profil + signaux avancés (surcharge les autres flags sauf FAST_LIVE).
MAX_QUALITY_MODE = _env_flag("BETTINGHUD_MAX_QUALITY_MODE", False)
# Désormais False par défaut : vraies stats / mapping (matches_recent) + labels source hors « fast_default ».
FAST_LIVE_MODE = _env_flag("BETTINGHUD_FAST_LIVE_MODE", False)
# Scraping TennisExplorer profils (forme / fatigue ; rang TE seulement si pas de ligne officielle).
# Activé par défaut ; désactiver : BETTINGHUD_ENABLE_PROFILE_SCRAPE=0
ENABLE_PROFILE_SCRAPE = _env_flag("BETTINGHUD_ENABLE_PROFILE_SCRAPE", True)
ENABLE_ADV_SIGNALS = _env_flag(
    "BETTINGHUD_ENABLE_ADV_SIGNALS",
    (not FAST_LIVE_MODE) or MAX_QUALITY_MODE,
)
ENABLE_PORTFOLIO_AUTO_RESULTS = _env_flag("BETTINGHUD_ENABLE_PORTFOLIO_AUTO_RESULTS", True)
PORTFOLIO_AUTO_RESULTS_INTERVAL_SEC = max(
    30, int(os.getenv("BETTINGHUD_PORTFOLIO_AUTO_RESULTS_INTERVAL_SEC", "180"))
)
ENABLE_IDENTITY_DIAG_AUTO = _env_flag("BETTINGHUD_ENABLE_IDENTITY_DIAG_AUTO", False)
ENABLE_BACKGROUND_PROFILE_PREWARM = _env_flag("BETTINGHUD_ENABLE_BACKGROUND_PROFILE_PREWARM", False)
# Ne traiter dans le Live que les matchs du jour et du lendemain (recommandé perf).
LIVE_ONLY_TODAY_TOMORROW = _env_flag("BETTINGHUD_LIVE_ONLY_TODAY_TOMORROW", True)
# Résolution identité/stats en parallèle (SQLite + CPU) — qualité identique, plus rapide.
IDENTITY_RESOLVE_WORKERS = max(
    1, min(12, int(os.getenv("BETTINGHUD_IDENTITY_WORKERS", "10")))
)
# Logs détaillés de latence pour get_latest_scraped_data (stdout terminal).
PERF_LOG_LIVE_BUILD = _env_flag("BETTINGHUD_PERF_LOG_LIVE_BUILD", False)
# Réentraînement ML périodique (défaut : 1× / semaine après délai initial).
ENABLE_AUTO_ML_TRAIN_WEEKLY = _env_flag("BETTINGHUD_AUTO_ML_TRAIN_WEEKLY", True)
AUTO_ML_TRAIN_INTERVAL_SEC = max(
    3600, int(os.getenv("BETTINGHUD_AUTO_ML_TRAIN_INTERVAL_SEC", "604800"))
)
AUTO_ML_TRAIN_INITIAL_DELAY_SEC = max(
    60, int(os.getenv("BETTINGHUD_AUTO_ML_TRAIN_INITIAL_DELAY_SEC", "7200"))
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ml_bundle_mtime_for_cache() -> float:
    """Invalide le cache Streamlit du modèle quand le fichier .pkl change (après train)."""
    from scripts.bets_db import get_ml_bundle_mtime

    m = get_ml_bundle_mtime()
    return float(m) if m is not None else 0.0


st.set_page_config(page_title="BettingHUD - Tennis", page_icon="🎾", layout="wide")

# Feedback immédiat : load_engines + auto_fix peuvent prendre > 1 min (sinon page blanche).
_init_status = st.status("Chargement du dashboard…", expanded=True)
_init_status.write("Modèle ML + stats (SQLite)…")

# --- CUSTOM CSS (SOFT MODERN DARK) ---
st.markdown("""
<style>
    :root {
        --bg: #0f1115;
        --panel: #171a21;
        --panel-soft: #1d2230;
        --text: #e8ecf3;
        --muted: #aeb7c6;
        --border: #2a3142;
        --accent: #4f8cff;
        --accent-soft: #2f6fdc;
        --success: #4fd18b;
        --warning: #e8c36a;
    }

    /* Fond général et typographie */
    .stApp {
        background: radial-gradient(circle at top right, #151b2a 0%, var(--bg) 38%);
        color: var(--text);
    }

    h1, h2, h3 {
        color: #f6f8fc;
        font-weight: 800 !important;
        letter-spacing: 0.2px;
    }

    p, span, label, .stCaption {
        color: var(--muted);
    }

    /* Tabs plus douces */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        height: 44px;
        background-color: var(--panel);
        border-radius: 10px 10px 0 0;
        padding: 8px 16px;
        border: 1px solid var(--border);
        color: var(--muted);
        font-weight: 700;
    }
    .stTabs [aria-selected="true"] {
        background-color: var(--panel-soft) !important;
        color: #ffffff !important;
        border-color: var(--accent) !important;
        box-shadow: inset 0 -2px 0 0 var(--accent);
    }

    /* Métriques lisibles, accent bleu doux */
    div[data-testid="stMetricValue"] {
        font-size: 1.9rem;
        font-weight: 900;
        color: var(--accent);
    }

    /* Cartes */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background-color: var(--panel);
        border: 1px solid var(--border);
        border-radius: 12px;
        box-shadow: 0 10px 20px rgba(0, 0, 0, 0.22);
        padding: 15px;
    }

    /* Boutons principaux */
    div.stButton > button {
        background: #2f5ea8;
        color: white;
        font-weight: 700;
        border: 1px solid #3b6fc0;
        border-radius: 8px;
        transition: all 0.2s ease;
        box-shadow: none;
        text-shadow: none;
        filter: none;
    }
    div.stButton > button:hover {
        background: #3a6fc3;
        border-color: #4a80d8;
        color: white;
        transform: none;
    }

    /* Inputs/selects */
    div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div,
    .stNumberInput input {
        background-color: #131826 !important;
        border-color: var(--border) !important;
        color: var(--text) !important;
    }

    /* Alertes */
    div[data-testid="stAlert"] {
        background-color: #151b27;
        color: var(--text);
        border: 1px solid var(--border);
        border-radius: 10px;
    }

    /* Badges */
    .odd-highlight {
        background-color: #f0d78f;
        color: #1e2533;
        padding: 3px 8px;
        border-radius: 999px;
        font-weight: bold;
        font-size: 1rem;
    }
    .ev-highlight {
        color: var(--success);
        font-weight: bold;
        font-size: 1rem;
    }
    .ev-highlight-neg {
        color: #e57373;
        font-weight: bold;
        font-size: 1rem;
    }
    .bet-badge {
        display: inline-block;
        margin-top: 8px;
        padding: 4px 10px;
        border-radius: 999px;
        background-color: #294c86;
        color: #ffffff;
        font-size: 0.9rem;
        font-weight: 700;
    }
</style>
""", unsafe_allow_html=True)

# Incrémenter si l’API de TennisStatsEngine / moteurs change (invalide le cache Streamlit).
_ENGINES_CACHE_VERSION = 14  # v3.5 + WTA branch (Return-Elo 60/40, K+15%, BP-resilience)


@st.cache_resource
def load_engines(_cache_version: int, bundle_mtime: float):
    ml = TennisMLModel()
    stats = TennisStatsEngine()
    profiles = ProfileScraper()
    # Force load of ML model to avoid delay in loop
    if ml.model is None and os.path.exists(ml.model_path):
        # Compatibilité: certains runs peuvent encore charger une classe TennisMLModel
        # ancienne sans _load_bundle_if_needed.
        if hasattr(ml, "_load_bundle_if_needed"):
            ml._load_bundle_if_needed()
        else:
            import joblib
            loaded = joblib.load(ml.model_path)
            if isinstance(loaded, dict) and "model" in loaded:
                ml.model = loaded["model"]
                if hasattr(ml, "player_elo"):
                    ml.player_elo = loaded.get("player_elo", {})
                if hasattr(ml, "player_surface_elo"):
                    ml.player_surface_elo = loaded.get("player_surface_elo", {})
                if hasattr(ml, "features") and loaded.get("features"):
                    ml.features = loaded["features"]
            else:
                ml.model = loaded
    return ml, stats, profiles

ml_model, stats_engine, profile_scraper = load_engines(
    _ENGINES_CACHE_VERSION, _ml_bundle_mtime_for_cache()
)

_init_status.update(label="Prêt.", state="complete")

@st.cache_resource
def start_background_scraper():
    def scrape_loop():
        scraper = FlashscoreScraper()
        while True:
            try:
                # 1. Scrape les matchs et les cotes
                matches = asyncio.run(scraper.get_today_matches_and_odds())
                
                # 2. Pré-charger les profils de tous les joueurs trouvés (pour ne pas bloquer l'UI)
                if matches and ENABLE_BACKGROUND_PROFILE_PREWARM:
                    from scripts.scraper_profiles import ProfileScraper
                    ps = ProfileScraper()
                    urls_to_scrape = set()
                    for m in matches:
                        # Ignorer les tournois mineurs pour économiser les requêtes
                        if m.get('category') not in ['ATP', 'WTA']: 
                            continue
                            
                        if m.get('p1_url'): urls_to_scrape.add(m['p1_url'])
                        if m.get('p2_url'): urls_to_scrape.add(m['p2_url'])
                    
                    max_bg_profile_prewarm = 20
                    urls_list = list(urls_to_scrape)[:max_bg_profile_prewarm]
                    print(f"Pré-chargement en arrière-plan de {len(urls_list)} profils...")
                    for i, url in enumerate(urls_list):
                        ps.scrape_profile(url)
                        if i % 10 == 0:
                            print(f"Progression profils : {i}/{len(urls_list)}")

            except Exception as e:
                print("Erreur du scraper en arrière-plan:", e)
            
            # Attendre 60 minutes avant de refaire un grand tour complet
            time.sleep(3600)
            
    t = threading.Thread(target=scrape_loop, daemon=True)
    t.start()
    return t

def _pts_from_rank(rank):
    """Estimation grossière des points ATP/WTA si on n'a que le rang (profil TE sans points)."""
    if rank <= 10:
        return 4000
    if rank <= 50:
        return 1200
    if rank <= 100:
        return 600
    if rank <= 200:
        return 300
    if rank <= 300:
        return 175
    if rank <= 500:
        return 80
    return 20


# Rang / points officiels depuis la DB (ne pas les remplacer par une estimation TennisExplorer).
_OFFICIAL_RANK_STATS_SOURCES = frozenset({"matches_recent", "wta_matches", "rankings_wta_current"})


def _merge_live_profile(stats, profile, tour_hint=None):
    """Met à jour forme / fatigue à partir du profil TennisExplorer scrapé.

    Si le profil est absent (erreur réseau, URL manquante), retombée sur la base
    ATP TennisMyLife (`tour_hint='ATP'`) ou la base WTA Sackmann (`tour_hint='WTA'`).
    La fenêtre temporelle est mesurée par rapport à la **dernière date présente dans
    le jeu de données du tour** (et non aux N jours calendaires courants) : utile
    en intersaison, mais les libellés UI « 14 j / 3 mois » deviennent une approximation.

    Rang / points issus de TML ou Sackmann (`stats_source` officiel) ne sont pas écrasés
    par l’estimation depuis TennisExplorer.
    """
    if not profile:
        return (
            stats_engine.get_recent_form(stats.get("_pid"), tour_hint=tour_hint),
            stats_engine.get_recent_fatigue(stats.get("_pid"), tour_hint=tour_hint),
        )
    form = {"win_pct": profile["win_pct"], "matches": profile["form_matches"]}
    fatigue = {"minutes_played": profile["fatigue_minutes"], "matches": profile["fatigue_matches"]}
    official = str(stats.get("stats_source") or "") in _OFFICIAL_RANK_STATS_SOURCES
    if profile.get("rank") not in (None, 100) and not official:
        stats["rank"] = profile["rank"]
        stats["pts"] = _pts_from_rank(profile["rank"])
        stats["stats_source"] = "tennisexplorer_estimate"
        stats.pop("stats_source_detail", None)
        lu = profile.get("last_update")
        if lu and len(str(lu)) >= 10:
            stats["stats_reference_date"] = str(lu)[:10]
    if profile.get("age") not in (None, 25):
        stats["age"] = profile["age"]
    if profile.get("hand") not in (None, "U"):
        stats["hand"] = profile["hand"]
    lu = profile.get("last_update")
    if lu:
        stats["te_profile_last_sync"] = lu
    return form, fatigue


def _human_stats_source_plain(code):
    """Libellé court pour classement/points."""
    s = str(code or "").lower()
    if s == "matches_recent":
        return "TennisMyLife (ATP · `matches_recent`)"
    if s == "wta_matches":
        return "WTA Sackmann (dernier match · `wta_matches`)"
    if s == "rankings_wta_current":
        return "Classement WTA courant (Sackmann · CSV)"
    if s == "tennisexplorer_estimate":
        return "TennisExplorer (rang estimé uniquement)"
    if s == "fast_default":
        return "Mode rapide (valeurs fictives)"
    if s == "fallback":
        return "Repli interne"
    if s == "no_ranking_source":
        return "Pas de données rang/points en base"
    return f"« {code or '—'} »"


def _fmt_te_sync_human(stats: dict, te_page_scraped: bool) -> str:
    ts = stats.get("te_profile_last_sync")
    if ts:
        return str(ts).replace("T", " ")[:19]
    if not ENABLE_PROFILE_SCRAPE:
        return "(profil TE désactivé)"
    if te_page_scraped:
        return "(scrape TE sans horodatage)"
    return "(pas de scrape TE / cache expiré)"


def _player_provenance_caption(player_label: str, stats: dict, te_page_scraped: bool) -> str:
    ref = stats.get("stats_reference_date")
    ref_s = str(ref) if ref else "—"
    src = _human_stats_source_plain(stats.get("stats_source"))
    te_h = _fmt_te_sync_human(stats, te_page_scraped)
    return f"{player_label} · **Origine rang/points** : {src} · **Réf. date** : {ref_s} · **MAJ profil TE** : {te_h}"


_ROUND_LABELS = {
    1: "Q1/R128", 2: "R64", 3: "R32", 4: "R16/RR",
    5: "QF", 6: "SF", 7: "F", 8: "Vainqueur",
}


def _round_label(depth):
    try:
        d = int(depth or 0)
    except Exception:
        return "—"
    return _ROUND_LABELS.get(d, "—") if d > 0 else "—"


def _fmt_diff(p1_val, p2_val, p1_label, p2_label, *, fmt="{:+.0f}", warning=False, alert_threshold=None):
    """Format the 'Avantage' column.

    Returns a string '<player_label> ({+diff})' or '—'. If `warning` is True, append ⚠️.
    """
    try:
        v1 = float(p1_val) if p1_val is not None else None
        v2 = float(p2_val) if p2_val is not None else None
    except Exception:
        return "—"
    if v1 is None or v2 is None:
        return "—"
    diff = v1 - v2
    if abs(diff) < 1e-9:
        return "Égal"
    label = p1_label if diff > 0 else p2_label
    flag = ""
    if warning or (alert_threshold is not None and abs(diff) >= alert_threshold):
        flag = " ⚠️"
    return f"{label} ({fmt.format(diff)}){flag}"


def _confidence_level_emoji(conf_value) -> str:
    try:
        c = float(conf_value)
    except Exception:
        return "⚪"
    c = max(0.0, min(1.0, c))
    if c < 0.25:
        return "🔻"
    if c < 0.5:
        return "🟨"
    if c < 0.75:
        return "🟩"
    return "⭐"


def _highlight_comparison_row(row: pd.Series) -> list[str]:
    """Styles pour le tableau comparatif (Elo + confiance mis en avant)."""
    ind = str(row.iloc[0])
    n = len(row)

    def _all(css: str) -> list[str]:
        return [css] * n

    # Confiance avant ⭐ (le palier « très forte » utilise aussi ⭐)
    if "Confiance IA" in ind:
        if ind.startswith("🔻"):
            return _all("background-color: #3d2222; color: #ffcccc; font-weight: 600")
        if ind.startswith("🟨"):
            return _all("background-color: #3d3520; color: #ffe8b0; font-weight: 600")
        if ind.startswith("🟩"):
            return _all("background-color: #1e3d28; color: #c8ffd4; font-weight: 600")
        if ind.startswith("⭐"):
            return _all("background-color: #1a3d24; color: #d8ffe8; font-weight: 700")
        return _all("background-color: #333; color: #e0e0e0; font-weight: 600")
    if ind.startswith("🌐"):
        return _all("background-color: #1a2d42; color: #d6e8ff; font-weight: 600")
    if ind.startswith("🏟️"):
        return _all("background-color: #153d38; color: #c8f5ee; font-weight: 600")
    if ind.startswith("🎾"):
        return _all("background-color: #3d351a; color: #fff0c2; font-weight: 600")
    if ind.startswith("⭐") or ind.startswith("↩️"):
        return _all("background-color: #2a1f45; color: #e8ddff; font-weight: 600")
    return [""] * n


def _style_comparison_popover_df(df: pd.DataFrame):
    """Retourne un Styler Streamlit pour le bloc comparatif."""
    try:
        return df.style.apply(_highlight_comparison_row, axis=1)
    except Exception:
        return df


def _build_comparison_rows(match: dict, p1_label: str, p2_label: str, p_num: int, hand_text_fn):
    """Build the per-indicator comparison rows shown at the top of the popover.

    p1_label / p2_label are the *display* names from the perspective of the user
    (player_name first, opponent second). p_num tells us whether the current view
    is the P1 side (p_num=1) or the P2 side (p_num=2) of the underlying model so
    we can pull stats from the right key.
    """
    if p_num == 1:
        p1_stats = match.get("p1_stats", {}) or {}
        p2_stats = match.get("p2_stats", {}) or {}
        p1_form = match.get("p1_form", {}) or {}
        p2_form = match.get("p2_form", {}) or {}
        p1_fat = match.get("p1_fatigue", {}) or {}
        p2_fat = match.get("p2_fatigue", {}) or {}
        p1_mq = match.get("p1_match_quality", {}) or {}
        p2_mq = match.get("p2_match_quality", {}) or {}
        h2h = match.get("h2h", {}) or {}
        p1_h2h = h2h.get("p1_wins", 0)
        p2_h2h = h2h.get("p2_wins", 0)
        fs = match.get("feature_snapshot", {}) or {}
        p1_global_elo = fs.get("p1_global_elo")
        p2_global_elo = fs.get("p2_global_elo")
        p1_surface_elo = fs.get("p1_surface_elo")
        p2_surface_elo = fs.get("p2_surface_elo")
        surface_speed = fs.get("surface_speed")
        p1_svc_elo = fs.get("p1_service_elo")
        p2_svc_elo = fs.get("p2_service_elo")
        p1_ret_elo = fs.get("p1_return_elo")
        p2_ret_elo = fs.get("p2_return_elo")
    else:
        p1_stats = match.get("p2_stats", {}) or {}
        p2_stats = match.get("p1_stats", {}) or {}
        p1_form = match.get("p2_form", {}) or {}
        p2_form = match.get("p1_form", {}) or {}
        p1_fat = match.get("p2_fatigue", {}) or {}
        p2_fat = match.get("p1_fatigue", {}) or {}
        p1_mq = match.get("p2_match_quality", {}) or {}
        p2_mq = match.get("p1_match_quality", {}) or {}
        h2h = match.get("h2h", {}) or {}
        p1_h2h = h2h.get("p2_wins", 0)
        p2_h2h = h2h.get("p1_wins", 0)
        fs = match.get("feature_snapshot", {}) or {}
        p1_global_elo = fs.get("p2_global_elo")
        p2_global_elo = fs.get("p1_global_elo")
        p1_surface_elo = fs.get("p2_surface_elo")
        p2_surface_elo = fs.get("p1_surface_elo")
        surface_speed = fs.get("surface_speed")
        p1_svc_elo = fs.get("p2_service_elo")
        p2_svc_elo = fs.get("p1_service_elo")
        p1_ret_elo = fs.get("p2_return_elo")
        p2_ret_elo = fs.get("p1_return_elo")

    surface = match.get("surface", "Surface")

    def _fmt_n(v, suffix=""):
        if v is None:
            return "—"
        try:
            return f"{int(v)}{suffix}"
        except Exception:
            try:
                return f"{float(v):.0f}{suffix}"
            except Exception:
                return "—"

    def _fmt_f(v, suffix=""):
        if v is None:
            return "—"
        try:
            return f"{float(v):.0f}{suffix}"
        except Exception:
            return "—"

    rows = []
    # Classement (smaller rank = better, so we invert for "Avantage")
    p1_rank = p1_stats.get("rank")
    p2_rank = p2_stats.get("rank")
    rank_adv = "—"
    if p1_rank is not None and p2_rank is not None:
        try:
            v1 = float(p1_rank); v2 = float(p2_rank)
            if abs(v1 - v2) < 1e-9:
                rank_adv = "Égal"
            else:
                lab = p1_label if v1 < v2 else p2_label
                rank_adv = f"{lab} ({(v1 - v2):+.0f})"
        except Exception:
            pass
    rows.append(["Classement", _fmt_n(p1_rank), _fmt_n(p2_rank), rank_adv])

    # Points
    rows.append([
        "Points",
        _fmt_n(p1_stats.get("pts")),
        _fmt_n(p2_stats.get("pts")),
        _fmt_diff(p1_stats.get("pts"), p2_stats.get("pts"), p1_label, p2_label, fmt="{:+.0f}"),
    ])

    # Âge / Taille / Main forte (combined)
    def _age_ht_hand(stats):
        age = stats.get("age")
        ht = stats.get("ht")
        hand = hand_text_fn(stats.get("hand", "U"))
        age_s = f"{int(age)}" if age is not None else "—"
        ht_s = f"{int(ht)}" if ht is not None else "—"
        return f"{age_s} / {ht_s} ({hand.lower()})"

    rows.append(["Âge / Taille", _age_ht_hand(p1_stats), _age_ht_hand(p2_stats), "—"])

    # Elo global
    rows.append([
        "🌐 Elo global",
        _fmt_f(p1_global_elo),
        _fmt_f(p2_global_elo),
        _fmt_diff(p1_global_elo, p2_global_elo, p1_label, p2_label, fmt="{:+.0f}"),
    ])

    # Elo surface (with warning if opposed by ≥150 to global Elo)
    elo_surface_alert = False
    try:
        if p1_global_elo is not None and p2_global_elo is not None and p1_surface_elo is not None and p2_surface_elo is not None:
            global_diff = float(p1_global_elo) - float(p2_global_elo)
            surface_diff = float(p1_surface_elo) - float(p2_surface_elo)
            if abs(surface_diff) >= 100 and global_diff * surface_diff < 0:
                elo_surface_alert = True
    except Exception:
        pass
    rows.append([
        f"🏟️ Elo {surface}",
        _fmt_f(p1_surface_elo),
        _fmt_f(p2_surface_elo),
        _fmt_diff(
            p1_surface_elo, p2_surface_elo, p1_label, p2_label,
            fmt="{:+.0f}", warning=elo_surface_alert,
        ),
    ])

    def _fmt_speed(v):
        if v is None:
            return "—"
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "—"

    spd_s = _fmt_speed(surface_speed)
    rows.append([
        "Vitesse surface (TA)",
        spd_s,
        spd_s,
        "—",
    ])
    is_wta = str(match.get("tour", "")).upper() == "WTA"
    ret_icon = "⭐" if is_wta else "↩️"
    return_label = f"{ret_icon} Elo retour (micro)"
    service_label = "🎾 Elo service (micro)"
    rows.append([
        service_label,
        _fmt_f(p1_svc_elo),
        _fmt_f(p2_svc_elo),
        _fmt_diff(p1_svc_elo, p2_svc_elo, p1_label, p2_label, fmt="{:+.0f}"),
    ])
    rows.append([
        return_label,
        _fmt_f(p1_ret_elo),
        _fmt_f(p2_ret_elo),
        _fmt_diff(p1_ret_elo, p2_ret_elo, p1_label, p2_label, fmt="{:+.0f}"),
    ])

    # Forme 90j (win_pct + W-L)
    def _fmt_form(form):
        wp = form.get("win_pct")
        w = form.get("wins", 0); l = form.get("losses", 0)
        if wp is None:
            return "—"
        return f"{float(wp):.0f}% ({w}-{l})"

    p1_wp = p1_form.get("win_pct")
    p2_wp = p2_form.get("win_pct")
    form_adv = "—"
    if p1_wp is not None and p2_wp is not None:
        try:
            d = float(p1_wp) - float(p2_wp)
            if abs(d) < 0.5:
                form_adv = "Égal"
            else:
                form_adv = f"{p1_label if d > 0 else p2_label} ({d:+.0f}pp)"
        except Exception:
            pass
    rows.append(["Forme 90j", _fmt_form(p1_form), _fmt_form(p2_form), form_adv])

    # Fatigue 14j (less is better → invert sign in advantage column)
    p1_min = p1_fat.get("minutes_played", 0)
    p2_min = p2_fat.get("minutes_played", 0)
    p1_m = p1_fat.get("matches", 0)
    p2_m = p2_fat.get("matches", 0)
    fatigue_str_1 = f"{int(p1_min or 0)}min / {int(p1_m or 0)} matchs"
    fatigue_str_2 = f"{int(p2_min or 0)}min / {int(p2_m or 0)} matchs"
    fatigue_adv = "—"
    try:
        d = float(p1_min or 0) - float(p2_min or 0)
        if abs(d) < 30:
            fatigue_adv = "Égal"
        else:
            # smaller minutes = advantage (less tired)
            fatigue_adv = f"{p1_label if d < 0 else p2_label} (autre +{abs(d):.0f}min)"
    except Exception:
        pass
    rows.append(["Fatigue 14j", fatigue_str_1, fatigue_str_2, fatigue_adv])

    # Wins last 7d
    rows.append([
        "Wins 7j",
        _fmt_n(p1_mq.get("wins_last7d")),
        _fmt_n(p2_mq.get("wins_last7d")),
        _fmt_diff(p1_mq.get("wins_last7d"), p2_mq.get("wins_last7d"), p1_label, p2_label, fmt="{:+.0f}"),
    ])

    # 3-setters last 14d (less is better → invert)
    p1_three = p1_mq.get("three_setters_last14d", 0) or 0
    p2_three = p2_mq.get("three_setters_last14d", 0) or 0
    three_adv = "—"
    if abs(p1_three - p2_three) < 1:
        three_adv = "Égal"
    else:
        three_adv = f"{p1_label if p1_three < p2_three else p2_label} (moins de marathons)"
    rows.append([
        "3-setters 14j",
        _fmt_n(p1_three),
        _fmt_n(p2_three),
        three_adv,
    ])

    # Last round reached
    p1_lr = p1_mq.get("last_round_reached", 0) or 0
    p2_lr = p2_mq.get("last_round_reached", 0) or 0
    lr_adv = "—"
    if p1_lr or p2_lr:
        if p1_lr == p2_lr:
            lr_adv = "Égal"
        else:
            lr_adv = p1_label if p1_lr > p2_lr else p2_label
    rows.append(["Phase au dernier tournoi", _round_label(p1_lr), _round_label(p2_lr), lr_adv])

    # H2H
    h2h_adv = "—"
    total = (p1_h2h or 0) + (p2_h2h or 0)
    if total > 0:
        if p1_h2h == p2_h2h:
            h2h_adv = "Égal"
        else:
            warn = " ⚠️" if (p_num == 1 and p2_h2h > p1_h2h) or (p_num == 2 and p1_h2h > p2_h2h) else ""
            # Note: the warning fires when the OPPONENT leads H2H (relevant for the value-bet view)
            lead_label = p1_label if p1_h2h > p2_h2h else p2_label
            h2h_adv = f"{lead_label} ({max(p1_h2h, p2_h2h)}-{min(p1_h2h, p2_h2h)}){warn}"
    rows.append(["H2H", str(p1_h2h or 0), str(p2_h2h or 0), h2h_adv])

    if match.get("confidence") is not None:
        try:
            cf = float(match["confidence"])
        except Exception:
            cf = None
        if cf is not None:
            lvl, _ = _confidence_explained(cf)
            ic = _confidence_level_emoji(cf)
            rows.append(
                [
                    f"{ic} Confiance IA — {lvl}",
                    f"{cf:.2f}",
                    "—",
                    "—",
                ]
            )

    return rows


def _confidence_explained(conf_value):
    """Retourne un libellé simple + conseil d'usage pour la confiance IA."""
    try:
        c = float(conf_value)
    except Exception:
        return "Inconnue", "Aucune règle: vérifier d'autres signaux."
    c = max(0.0, min(1.0, c))
    if c < 0.25:
        return "Faible", "Match très incertain: éviter sauf value exceptionnelle."
    if c < 0.5:
        return "Moyenne", "Match ouvert: mise prudente."
    if c < 0.75:
        return "Forte", "Signal exploitable si la value est présente."
    return "Très forte", "Signal clair du modèle (toujours respecter le bankroll management)."


def _style_advantage_label(style_adv):
    try:
        s = float(style_adv)
    except Exception:
        return "Neutre", "Donnée style indisponible (fallback neutre)."
    if s >= 0.58:
        return "Avantage style", "Le profil de jeu de P1 a historiquement bien performé contre ce type d'adversaire."
    if s <= 0.42:
        return "Désavantage style", "Le profil de jeu de P1 est historiquement moins performant contre ce type d'adversaire."
    return "Neutre", "Match-up de style équilibré."


def _clutch_label(clutch_diff):
    try:
        d = float(clutch_diff)
    except Exception:
        return "Neutre", "Donnée clutch indisponible (fallback neutre)."
    if d >= 0.07:
        return "Edge clutch P1", "P1 est meilleur sur les points à pression (BP/TB) sur l'historique récent."
    if d <= -0.07:
        return "Edge clutch P2", "P2 est meilleur sur les points à pression (BP/TB) sur l'historique récent."
    return "Neutre", "Niveau clutch similaire."


def _inactivity_label(inactivity_diff, p1_days=None, p2_days=None):
    try:
        d = float(inactivity_diff)
    except Exception:
        return "Neutre", "Aucune pénalisation d'inactivité détectée."
    p1d = None if p1_days is None else float(p1_days)
    p2d = None if p2_days is None else float(p2_days)
    if (p1d is not None and p1d > 45) or (p2d is not None and p2d > 45):
        if d <= -0.1:
            return "Risque inactivité P1", "Retour après inactivité côté P1: confiance du modèle volontairement plafonnée."
        if d >= 0.1:
            return "Risque inactivité P2", "Retour après inactivité côté P2: confiance du modèle volontairement plafonnée."
        return "Risque inactivité", "Inactivité élevée détectée, le modèle réduit l'extrême des probabilités."
    return "Stable", "Pas de signal fort de retour après longue inactivité."


def _infobulle_hand_label(hand_code):
    if hand_code == "R":
        return "Droitier"
    if hand_code == "L":
        return "Gaucher"
    return "Inconnu"


def _infobulle_value_context_df(val: dict, odd_book: float, odd_true: float, match: dict) -> pd.DataFrame:
    edge_pp = (1 / odd_true * 100) - (1 / odd_book * 100)
    rows = [
        ["EV détectée", f"+{float(val.get('value_pct', 0)):.1f} %"],
        ["Proba modèle (perspective value)", f"{1 / odd_true * 100:.1f} %"],
        ["Proba implicite book", f"{1 / odd_book * 100:.1f} %"],
        ["Écart (points de proba)", f"{edge_pp:+.1f} pp"],
        ["Cote estimée (juste)", f"{odd_true:.2f}"],
        ["Cote book", f"{odd_book:.2f}"],
        ["Calibration", str(match.get("calibration_used", "Globale"))],
    ]
    if match.get("confidence") is not None:
        cf = float(match["confidence"])
        lvl, _ = _confidence_explained(cf)
        ic = _confidence_level_emoji(cf)
        rows.append([f"{ic} Confiance IA", f"{lvl} ({cf:.2f})"])
    return pd.DataFrame(rows, columns=["Élément", "Valeur"])


def _infobulle_provenance_df(match: dict) -> pd.DataFrame:
    rows = []
    for pname, ps, loaded in (
        (match["player1"], match.get("p1_stats") or {}, match.get("p1_profile_loaded")),
        (match["player2"], match.get("p2_stats") or {}, match.get("p2_profile_loaded")),
    ):
        ref = ps.get("stats_reference_date") or "—"
        rows.append(
            [
                pname,
                _human_stats_source_plain(ps.get("stats_source")),
                str(ref),
                _fmt_te_sync_human(ps, bool(loaded)),
                "oui" if loaded else "non",
            ]
        )
    return pd.DataFrame(
        rows,
        columns=["Joueur", "Source rang/points", "Réf. date", "MAJ profil TE", "Page TE OK"],
    )


def _infobulle_dynamics_df(match: dict, player_name: str, opp_name: str, p_num: int) -> pd.DataFrame:
    p1s = match.get("p1_stats") or {}
    p2s = match.get("p2_stats") or {}
    h2h = match.get("h2h") or {}
    pw1, pw2 = h2h.get("p1_wins", 0), h2h.get("p2_wins", 0)
    if p_num == 1:
        h2h_s = f"{player_name} {pw1} — {pw2} {opp_name}"
        f_self, f_opp = match.get("p1_form") or {}, match.get("p2_form") or {}
        t_self, t_opp = match.get("p1_fatigue") or {}, match.get("p2_fatigue") or {}
    else:
        h2h_s = f"{player_name} {pw2} — {pw1} {opp_name}"
        f_self, f_opp = match.get("p2_form") or {}, match.get("p1_form") or {}
        t_self, t_opp = match.get("p2_fatigue") or {}, match.get("p1_fatigue") or {}
    form_s = (
        f"{player_name}: {float(f_self.get('win_pct', 50)):.0f}% ({f_self.get('matches', 0)} m.) · "
        f"{opp_name}: {float(f_opp.get('win_pct', 50)):.0f}% ({f_opp.get('matches', 0)} m.)"
    )
    fat_s = (
        f"{player_name}: ~{t_self.get('minutes_played', 0)} min / {t_self.get('matches', 0)} m. · "
        f"{opp_name}: ~{t_opp.get('minutes_played', 0)} min / {t_opp.get('matches', 0)} m."
    )
    phy_s = (
        f"Âge {p1s.get('age', '—')} vs {p2s.get('age', '—')} · taille {p1s.get('ht', '—')} vs {p2s.get('ht', '—')} cm · "
        f"{_infobulle_hand_label(p1s.get('hand', 'U'))} / {_infobulle_hand_label(p2s.get('hand', 'U'))}"
    )
    rows = [
        ["H2H (historique base)", h2h_s],
        ["Forme ~90 j", form_s],
        ["Fatigue ~14 j (durées estim.)", fat_s],
        ["Profil physique / main", phy_s],
        ["Surface (modèle)", str(match.get("surface", "—"))],
    ]
    return pd.DataFrame(rows, columns=["Thème", "Détail"])


def _infobulle_advanced_df(match: dict) -> pd.DataFrame:
    fs = match.get("feature_snapshot") or {}
    style_lbl, style_hint = _style_advantage_label(fs.get("style_advantage_score"))
    clutch_lbl, clutch_hint = _clutch_label(fs.get("clutch_index_diff"))
    inact_lbl, inact_hint = _inactivity_label(
        fs.get("inactivity_decay_weight"),
        fs.get("p1_days_since_last_match"),
        fs.get("p2_days_since_last_match"),
    )
    rows = [
        ["Style match-up", style_lbl, style_hint],
        ["Clutch / pression", clutch_lbl, clutch_hint],
        ["Inactivité / retour", inact_lbl, inact_hint],
    ]
    fst = fs.get("first_srv_win10_diff")
    fbp = fs.get("bp_conv10_diff")
    fd = fs.get("dominance_ratio_diff")
    if fst is not None:
        rows.append(
            [
                "1re balle gagnée (Δ 10 m.)",
                f"{float(fst) * 100:+.1f} pts (vs adversaire)",
                "Micro-stats récentes",
            ]
        )
    if fbp is not None:
        rows.append(
            [
                "BP convertis (Δ 10 m.)",
                f"{float(fbp) * 100:+.1f} pts",
                "Micro-stats récentes",
            ]
        )
    if fd is not None:
        dom_val = float(fd)
        note = (
            "dominance P1" if dom_val > 0.05 else "dominance P2" if dom_val < -0.05 else "équilibré"
        )
        rows.append(
            [
                "Dominance ratio (Δ)",
                f"{dom_val:+.2f} ({note})",
                "Micro-stats récentes",
            ]
        )
    return pd.DataFrame(rows, columns=["Signal", "Lecture", "Contexte"])


def _infobulle_model_audit_df(match: dict) -> pd.DataFrame | None:
    fs = match.get("feature_snapshot") or {}
    if fs.get("raw_p1_prob") is None or fs.get("capped_p1_prob") is None:
        return None
    caps = fs.get("caps_applied", []) or []
    rows = [
        ["Proba brute (P1 tableau)", f"{float(fs.get('raw_p1_prob')) * 100:.1f} %"],
        ["Proba finale (après caps)", f"{float(fs.get('capped_p1_prob')) * 100:.1f} %"],
        ["Garde-fous activés", ", ".join(caps) if caps else "aucun"],
    ]
    return pd.DataFrame(rows, columns=["Audit", "Valeur"])


def _infobulle_top_features_df(match: dict) -> pd.DataFrame | None:
    tf = match.get("top_features") or []
    if not tf:
        return None
    rows = []
    for t in tf[:15]:
        try:
            v = float(t.get("value", 0))
        except Exception:
            v = 0.0
        rows.append([str(t.get("feature", "")), f"{v:.4f}"])
    return pd.DataFrame(rows, columns=["Facteur", "Valeur"])


def _infobulle_analysis_markdown(
    match: dict,
    player_name: str,
    opp_name: str,
    odd_book: float,
    odd_true: float,
    val: dict,
    *,
    skip_edge_paragraph: bool = False,
) -> str:
    edge_pp = (1 / odd_true * 100) - (1 / odd_book * 100)
    parts: list[str] = []
    if not skip_edge_paragraph:
        parts.append(
            f"**Paire cote / proba.** — Le book sous-estime **{player_name}** au regard du modèle "
            f"(~**{1/odd_book*100:.1f}%** implicite vs **{1/odd_true*100:.1f}%** attendu). "
            f"Écart **{edge_pp:+.1f}** points de probabilité ⇒ EV **+{float(val.get('value_pct', 0)):.1f}%**."
        )
    tf = match.get("top_features") or []
    if tf:
        top = tf[:5]
        parts.append(
            "\n**Poids du modèle (facteurs locaux)**\n"
            + "\n".join(
                f"- **{t.get('feature')}** : `{float(t.get('value', 0)):.4f}`" for t in top
            )
        )
    conf = match.get("confidence")
    if conf is not None:
        lvl, hint = _confidence_explained(conf)
        ic = _confidence_level_emoji(conf)
        parts.append(f"\n{ic} **Confiance globale** : **{lvl}** — _{hint}_")
    if str(match.get("tour", "")).upper() == "WTA":
        parts.append(
            "\n*Contexte WTA* : retour pondéré plus fort que le service ; signal M1000 et résilience break-points peuvent déplacer la proba."
        )
    return "\n".join(parts)


def _infobulle_quick_summary(
    player_name: str,
    odd_book: float,
    odd_true: float,
    val: dict,
    match: dict,
) -> str:
    edge_pp = (1 / odd_true * 100) - (1 / odd_book * 100)
    parts = [
        f"**{player_name}** · **+{float(val.get('value_pct', 0)):.1f} % EV**",
        f"proba modèle **{1 / odd_true * 100:.1f} %** vs book **{1 / odd_book * 100:.1f} %**",
        f"écart **{edge_pp:+.1f} pp**",
    ]
    conf = match.get("confidence")
    if conf is not None:
        lvl, _ = _confidence_explained(conf)
        ic = _confidence_level_emoji(conf)
        parts.append(f"{ic} confiance **{lvl}**")
    parts.append(f"cal. **{match.get('calibration_used', 'Globale')}**")
    return " · ".join(parts)


_INFOBULLE_DF_HEIGHT = 220


def _infer_surface(tournament: str) -> str:
    t = (tournament or "").lower()
    clay_hints = ["rome", "madrid", "monte-carlo", "roland", "barcelona", "hamburg", "marrakech", "bastad", "kitzbuhel", "geneva", "estoril", "parma"]
    grass_hints = ["wimbledon", "halle", "queens", "eastbourne", "mallorca", "stuttgart", "s-hertogenbosch"]
    hard_hints = ["australian", "us open", "miami", "indian wells", "dubai", "doha", "brisbane", "tokyo", "shanghai", "beijing", "montreal", "toronto", "cincinnati"]

    if any(k in t for k in clay_hints):
        return "Clay"
    if any(k in t for k in grass_hints):
        return "Grass"
    if any(k in t for k in hard_hints):
        return "Hard"
    return "Hard"


def _name_key(name: str):
    n = str(name or "").lower().strip()
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n)
    parts = [p for p in n.split(" ") if p]
    if not parts:
        return None
    if len(parts) == 2 and len(parts[1]) == 1:
        return f"{parts[0]} {parts[1]}"
    last = parts[-2] if len(parts) >= 2 and len(parts[-1]) == 1 else parts[-1]
    ini = parts[-1][0] if parts[-1] else ""
    if len(parts) >= 2 and len(parts[-1]) == 1:
        ini = parts[-1]
    return f"{last} {ini}".strip()


def _safe_ratio(num, den, default=np.nan):
    try:
        num = float(num)
        den = float(den)
        if den <= 0:
            return default
        return num / den
    except Exception:
        return default


def _infer_style_cluster(ace_rate, serve_win_rate, break_rate):
    if ace_rate >= 0.13 and serve_win_rate >= 0.66 and break_rate >= 0.23:
        return "serve_volley"
    if ace_rate >= 0.12 and serve_win_rate >= 0.64 and break_rate <= 0.22:
        return "big_server"
    if break_rate >= 0.27 and ace_rate <= 0.08:
        return "counterpuncher"
    if serve_win_rate >= 0.60 and break_rate >= 0.24:
        return "aggressive_baseliner"
    return "all_court"


def _tb_won_played(score_text: str, player_is_winner: bool):
    if not isinstance(score_text, str):
        return 0, 0
    cleaned = score_text.split("RET")[0].split("W/O")[0].split("DEF")[0]
    sets = re.findall(r"(\d+)\s*-\s*(\d+)", cleaned)
    won = 0
    played = 0
    for a, b in sets:
        try:
            sa = int(a); sb = int(b)
        except Exception:
            continue
        if (sa == 7 and sb == 6) or (sa == 6 and sb == 7):
            played += 1
            won += 1 if ((player_is_winner and sa == 7) or ((not player_is_winner) and sb == 7)) else 0
    return won, played


_SIGNAL_COLS = (
    "tourney_date, surface, winner_name, loser_name, score, "
    "w_ace, w_svpt, w_1stIn, w_1stWon, w_2ndWon, w_SvGms, w_bpSaved, w_bpFaced, "
    "l_ace, l_svpt, l_1stIn, l_1stWon, l_2ndWon, l_SvGms, l_bpSaved, l_bpFaced"
)


@st.cache_data(ttl=900)
def _load_signal_matches(tour: str = "ATP"):
    """Charge les matchs récents (>=2023) pour calcul des signaux avancés.

    - tour='ATP' -> matches_recent (TennisMyLife)
    - tour='WTA' -> wta_matches (Sackmann)

    Les noms WTA Sackmann sont au format 'Prénom Nom' : on convertit donc
    via `to_lastname_initial` avant `_name_key`, sinon les clés ne correspondent
    pas aux noms Flashscore (format 'Nom P.').
    """
    t = (tour or "ATP").strip().upper()
    conn = sqlite3.connect("data/bettinghud.db")
    if t == "WTA":
        q = (
            f"SELECT {_SIGNAL_COLS} FROM wta_matches "
            "WHERE CAST(substr(tourney_date,1,4) AS INTEGER) >= 2023"
        )
    else:
        q = (
            f"SELECT {_SIGNAL_COLS} FROM matches_recent "
            "WHERE source='tennismylife' AND CAST(substr(tourney_date,1,4) AS INTEGER) >= 2023"
        )
    try:
        df = pd.read_sql(q, conn)
    except Exception:
        df = pd.DataFrame()
    finally:
        conn.close()
    if df.empty:
        return df
    df["tourney_date"] = pd.to_datetime(df["tourney_date"], errors="coerce")
    df = df.dropna(subset=["tourney_date"])
    # TML et Sackmann stockent les noms au format 'Prénom Nom', alors que Flashscore
    # nous envoie 'Nom I.'. On normalise systématiquement les deux côtés via
    # to_lastname_initial → _name_key pour qu'ils convergent vers 'nom i'.
    df["w_key"] = df["winner_name"].map(lambda n: _name_key(to_lastname_initial(str(n or ""))))
    df["l_key"] = df["loser_name"].map(lambda n: _name_key(to_lastname_initial(str(n or ""))))
    return df


@st.cache_data(ttl=900)
def _compute_live_advanced_signals(p1_name: str, p2_name: str, surface: str, tour_hint: str = "ATP"):
    df = _load_signal_matches(tour_hint)
    p1k, p2k = _name_key(p1_name), _name_key(p2_name)
    if df is None or df.empty:
        # Aucune donnée disponible pour ce tour : on retourne les défauts neutres.
        return {
            "style_advantage_score": 0.5,
            "p1_clutch_index": 0.5,
            "p2_clutch_index": 0.5,
            "p1_days": 7,
            "p2_days": 7,
            "p1_first_srv_win10": 0.68,
            "p2_first_srv_win10": 0.68,
            "p1_bp_conv10": 0.38,
            "p2_bp_conv10": 0.38,
            "p1_dominance_ratio": 1.0,
            "p2_dominance_ratio": 1.0,
        }
    if not p1k or not p2k:
        return {
            "style_advantage_score": 0.5,
            "p1_clutch_index": 0.5,
            "p2_clutch_index": 0.5,
            "p1_days": 7,
            "p2_days": 7,
            "p1_first_srv_win10": 0.68,
            "p2_first_srv_win10": 0.68,
            "p1_bp_conv10": 0.38,
            "p2_bp_conv10": 0.38,
            "p1_dominance_ratio": 1.0,
            "p2_dominance_ratio": 1.0,
        }
    max_date = df["tourney_date"].max()
    p1_rows = df[(df["w_key"] == p1k) | (df["l_key"] == p1k)].copy().sort_values("tourney_date")
    p2_rows = df[(df["w_key"] == p2k) | (df["l_key"] == p2k)].copy().sort_values("tourney_date")

    def _days(rows):
        return 7 if rows.empty else int(max(0, (max_date - rows["tourney_date"].max()).days))

    def _player_cluster(rows, pk):
        if rows.empty:
            return "all_court"
        r = rows.tail(20)
        ace_rates, serve_wins, break_rates = [], [], []
        for x in r.itertuples(index=False):
            if x.w_key == pk:
                ace_rates.append(_safe_ratio(x.w_ace, x.w_svpt, 0.06))
                serve_wins.append(_safe_ratio((x.w_1stWon or 0) + (x.w_2ndWon or 0), x.w_svpt, 0.60))
                opp_breaks = max(0.0, (float(x.l_bpFaced) if pd.notna(x.l_bpFaced) else 0.0) - (float(x.l_bpSaved) if pd.notna(x.l_bpSaved) else 0.0))
                break_rates.append(_safe_ratio(opp_breaks, x.l_SvGms, 0.20))
            else:
                ace_rates.append(_safe_ratio(x.l_ace, x.l_svpt, 0.06))
                serve_wins.append(_safe_ratio((x.l_1stWon or 0) + (x.l_2ndWon or 0), x.l_svpt, 0.60))
                opp_breaks = max(0.0, (float(x.w_bpFaced) if pd.notna(x.w_bpFaced) else 0.0) - (float(x.w_bpSaved) if pd.notna(x.w_bpSaved) else 0.0))
                break_rates.append(_safe_ratio(opp_breaks, x.w_SvGms, 0.20))
        return _infer_style_cluster(float(np.nanmean(ace_rates)), float(np.nanmean(serve_wins)), float(np.nanmean(break_rates)))

    p1_cluster = _player_cluster(p1_rows, p1k)
    p2_cluster = _player_cluster(p2_rows, p2k)
    # Matrice style-vs-style (rock-paper-scissors) + fallback forme
    style_matrix = {
        ("counterpuncher", "big_server"): 0.58,
        ("big_server", "counterpuncher"): 0.42,
        ("aggressive_baseliner", "counterpuncher"): 0.54,
        ("counterpuncher", "aggressive_baseliner"): 0.46,
        ("serve_volley", "aggressive_baseliner"): 0.55,
        ("aggressive_baseliner", "serve_volley"): 0.45,
        ("all_court", "all_court"): 0.50,
    }
    base_style = style_matrix.get((p1_cluster, p2_cluster), 0.50)
    p1_form = 0.5 if p1_rows.empty else float((p1_rows["w_key"] == p1k).tail(20).mean())
    p2_form = 0.5 if p2_rows.empty else float((p2_rows["w_key"] == p2k).tail(20).mean())
    style_adv = max(0.0, min(1.0, base_style + (p1_form - p2_form) * 0.20))

    def _clutch(rows, pk):
        if rows.empty:
            return 0.5
        cutoff = max_date - pd.Timedelta(days=365)
        r = rows[rows["tourney_date"] >= cutoff]
        if r.empty:
            return 0.5
        bp_saved_vals, bp_conv_vals = [], []
        tb_won, tb_played = 0.0, 0.0
        for x in r.itertuples(index=False):
            is_w = x.w_key == pk
            if is_w:
                bp_saved_vals.append(_safe_ratio(x.w_bpSaved, x.w_bpFaced, np.nan))
                opp_breaks = max(0.0, (float(x.l_bpFaced) if pd.notna(x.l_bpFaced) else 0.0) - (float(x.l_bpSaved) if pd.notna(x.l_bpSaved) else 0.0))
                bp_conv_vals.append(_safe_ratio(opp_breaks, x.l_bpFaced, np.nan))
            else:
                bp_saved_vals.append(_safe_ratio(x.l_bpSaved, x.l_bpFaced, np.nan))
                opp_breaks = max(0.0, (float(x.w_bpFaced) if pd.notna(x.w_bpFaced) else 0.0) - (float(x.w_bpSaved) if pd.notna(x.w_bpSaved) else 0.0))
                bp_conv_vals.append(_safe_ratio(opp_breaks, x.w_bpFaced, np.nan))
            w, p = _tb_won_played(x.score, is_w)
            tb_won += w; tb_played += p
        bps = float(np.nanmean([v for v in bp_saved_vals if not pd.isna(v)])) if any(not pd.isna(v) for v in bp_saved_vals) else 0.5
        bpc = float(np.nanmean([v for v in bp_conv_vals if not pd.isna(v)])) if any(not pd.isna(v) for v in bp_conv_vals) else 0.5
        tbr = (tb_won / tb_played) if tb_played > 0 else 0.5
        return max(0.0, min(1.0, 0.4 * bps + 0.4 * bpc + 0.2 * tbr))

    def _micro(rows, pk):
        if rows.empty:
            return 0.68, 0.38, 1.0
        r = rows.tail(10)
        first_vals, bp_conv_vals, dom_vals = [], [], []
        for x in r.itertuples(index=False):
            is_w = x.w_key == pk
            if is_w:
                first = _safe_ratio(x.w_1stWon, x.w_1stIn, np.nan)
                opp_breaks = max(0.0, (float(x.l_bpFaced) if pd.notna(x.l_bpFaced) else 0.0) - (float(x.l_bpSaved) if pd.notna(x.l_bpSaved) else 0.0))
                bp_conv = _safe_ratio(opp_breaks, x.l_bpFaced, np.nan)
                serv_win = _safe_ratio((x.w_1stWon or 0) + (x.w_2ndWon or 0), x.w_svpt, np.nan)
                ret_win = _safe_ratio((x.l_svpt or 0) - ((x.l_1stWon or 0) + (x.l_2ndWon or 0)), x.l_svpt, np.nan)
            else:
                first = _safe_ratio(x.l_1stWon, x.l_1stIn, np.nan)
                opp_breaks = max(0.0, (float(x.w_bpFaced) if pd.notna(x.w_bpFaced) else 0.0) - (float(x.w_bpSaved) if pd.notna(x.w_bpSaved) else 0.0))
                bp_conv = _safe_ratio(opp_breaks, x.w_bpFaced, np.nan)
                serv_win = _safe_ratio((x.l_1stWon or 0) + (x.l_2ndWon or 0), x.l_svpt, np.nan)
                ret_win = _safe_ratio((x.w_svpt or 0) - ((x.w_1stWon or 0) + (x.w_2ndWon or 0)), x.w_svpt, np.nan)
            serv_lost = np.nan if pd.isna(serv_win) else max(0.01, 1.0 - float(serv_win))
            dom = np.nan if pd.isna(ret_win) or pd.isna(serv_lost) else float(ret_win) / float(serv_lost)
            first_vals.append(first)
            bp_conv_vals.append(bp_conv)
            dom_vals.append(dom)
        first_avg = float(np.nanmean([v for v in first_vals if not pd.isna(v)])) if any(not pd.isna(v) for v in first_vals) else 0.68
        bp_avg = float(np.nanmean([v for v in bp_conv_vals if not pd.isna(v)])) if any(not pd.isna(v) for v in bp_conv_vals) else 0.38
        dom_avg = float(np.nanmean([v for v in dom_vals if not pd.isna(v)])) if any(not pd.isna(v) for v in dom_vals) else 1.0
        return first_avg, bp_avg, dom_avg

    p1_first, p1_bp10, p1_dom = _micro(p1_rows, p1k)
    p2_first, p2_bp10, p2_dom = _micro(p2_rows, p2k)

    return {
        "style_advantage_score": style_adv if p2_cluster else 0.5,
        "p1_clutch_index": _clutch(p1_rows, p1k),
        "p2_clutch_index": _clutch(p2_rows, p2k),
        "p1_days": _days(p1_rows),
        "p2_days": _days(p2_rows),
        "p1_first_srv_win10": p1_first,
        "p2_first_srv_win10": p2_first,
        "p1_bp_conv10": p1_bp10,
        "p2_bp_conv10": p2_bp10,
        "p1_dominance_ratio": p1_dom,
        "p2_dominance_ratio": p2_dom,
    }


def _prematch_csv_signature():
    """Chemin + mtime du CSV prematch le plus récent (mtime = contenu réellement mis à jour)."""
    data_dir = os.path.join("data", "scraped")
    files = glob.glob(os.path.join(data_dir, "*.csv"))
    if not files:
        return "", 0.0
    latest_file = max(files, key=os.path.getmtime)
    return latest_file, os.path.getmtime(latest_file)


def _ml_model_mtime() -> float:
    """mtime du bundle XGBoost. Sert de clé d'invalidation pour les caches Streamlit
    qui dépendent des prédictions du modèle (sinon `get_latest_scraped_data` peut
    rendre des prédictions obsolètes après un retraining)."""
    path = os.path.join("models", "xgb_model_tml_v1.pkl")
    try:
        return float(os.path.getmtime(path))
    except OSError:
        return 0.0


# Auto-refresh du fichier prematch (scrape Tennis Explorer en arrière-plan).
# - BETTINGHUD_PREMATCH_AUTO_REFRESH : active/désactive (défaut: True)
# - BETTINGHUD_PREMATCH_TTL_MIN      : âge maximal du dernier CSV avant re-scrape (défaut: 30 min)
# - BETTINGHUD_PREMATCH_LOCK_TTL_SEC : durée max d’un lock (anti-blocage si crash) (défaut: 600s)
PREMATCH_AUTO_REFRESH = _env_flag("BETTINGHUD_PREMATCH_AUTO_REFRESH", True)
PREMATCH_TTL_MIN = max(1, int(os.getenv("BETTINGHUD_PREMATCH_TTL_MIN", "30")))
PREMATCH_LOCK_TTL_SEC = max(60, int(os.getenv("BETTINGHUD_PREMATCH_LOCK_TTL_SEC", "600")))


def _prematch_lock_path() -> str:
    return os.path.join("data", "scraped", ".prematch_scrape.lock")


def _prematch_scrape_in_progress() -> bool:
    """True si un lock est présent et récent (< PREMATCH_LOCK_TTL_SEC)."""
    lock_path = _prematch_lock_path()
    if not os.path.exists(lock_path):
        return False
    try:
        age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        return False
    if age > PREMATCH_LOCK_TTL_SEC:
        try:
            os.remove(lock_path)
        except OSError:
            pass
        return False
    return True


def _start_prematch_scraper_subprocess() -> bool:
    """Lance le scraper en arrière-plan (non bloquant). Idempotent via lock fichier.

    Renvoie True si le scrape vient d'être démarré, False s'il était déjà en cours
    ou si le lancement a échoué.
    """
    if _prematch_scrape_in_progress():
        return False
    lock_path = _prematch_lock_path()
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        return False
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        creationflags = 0
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP : le scraper survit à la sortie de Streamlit.
            creationflags = 0x00000008 | 0x00000200
        # Le scraper supprimera le lock à la fin (voir scraper_prematch.__main__).
        subprocess.Popen(
            [sys.executable, "-m", "scripts.scraper_prematch"],
            cwd=project_root,
            creationflags=creationflags if os.name == "nt" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=(os.name != "nt"),
        )
        return True
    except Exception:
        # Nettoyage du lock si on n'a pas réussi à lancer le process.
        try:
            os.remove(lock_path)
        except OSError:
            pass
        return False


def _maybe_auto_refresh_prematch(csv_path: str, csv_mtime: float) -> bool:
    """Déclenche un re-scrape si TTL dépassé. Non bloquant.

    Retourne True si un scrape vient d'être lancé, False sinon.
    """
    if not PREMATCH_AUTO_REFRESH:
        return False
    age_min = (time.time() - csv_mtime) / 60.0 if csv_mtime else float("inf")
    needs_refresh = (not csv_path) or age_min >= PREMATCH_TTL_MIN
    if not needs_refresh:
        return False
    # Anti-spam : ne pas re-déclencher trop souvent côté UI (5 min entre tentatives).
    last = st.session_state.get("_prematch_refresh_last_started", 0.0)
    if time.time() - last < 300:
        return False
    started = _start_prematch_scraper_subprocess()
    if started:
        st.session_state["_prematch_refresh_last_started"] = time.time()
    return started


def _is_major_atp_wta(category, tournament_name):
    """
    Conserve uniquement les gros tournois ATP/WTA.
    Exclut explicitement Challenger/ITF et circuits mineurs assimilés.
    """
    c = str(category or "").strip().upper()
    if c not in {"ATP", "WTA"}:
        return False
    t = str(tournament_name or "").lower()
    minor_tokens = (
        "challenger",
        "itf",
        "utr",
        "utr pro tennis",
        "universal tennis",
        "futures",
        "future",
        "m15",
        "m25",
        "m35",
        "m50",
        "m60",
        "m80",
        "m100",
        "w15",
        "w25",
        "w35",
        "w50",
        "w60",
        "w80",
        "w100",
    )
    return not any(tok in t for tok in minor_tokens)


def _cap_live_build_prioritize_demain(df: pd.DataFrame, max_rows: int) -> pd.DataFrame:
    """
    Limite la volumétrie ML/profils tout en gardant les lignes « Demain » si possible.

    Le scraper TE concatène d'abord *toutes* les lignes du jour puis celles du lendemain.
    Un simple ``head(max)`` coupe la queue : les matchs de demain (ex. Rome ATP) peuvent
    disparaître entièrement dès que le jour courant suffit à remplir la fenêtre.
    """
    if len(df) <= max_rows:
        return df
    if "time" not in df.columns:
        return df.iloc[:max_rows].copy()
    dem = df["time"].astype(str).str.strip().str.startswith("Demain", na=False)
    df_tom = df.loc[dem]
    df_tod = df.loc[~dem]
    n_tom = len(df_tom)
    if n_tom == 0:
        return df.iloc[:max_rows].copy()
    if n_tom >= max_rows:
        return df_tom.iloc[:max_rows].copy()
    slack = max_rows - n_tom
    return pd.concat([df_tom, df_tod.iloc[:slack]], axis=0)


def _filter_df_exclude_doubles_prematch(df: pd.DataFrame) -> pd.DataFrame:
    """Retire les lignes doubles (TE: `/doubles-team/`, noms « J1 / J2 », libellé tournoi)."""
    if df is None or df.empty:
        return df
    u1 = df.get("p1_url", pd.Series("", index=df.index)).astype(str)
    u2 = df.get("p2_url", pd.Series("", index=df.index)).astype(str)
    url_dbl = u1.str.contains("/doubles-team/", na=False, regex=False) | u2.str.contains(
        "/doubles-team/", na=False, regex=False
    )
    p1 = df.get("player1", pd.Series("", index=df.index)).astype(str)
    p2 = df.get("player2", pd.Series("", index=df.index)).astype(str)
    name_dbl = p1.str.contains(" / ", na=False, regex=False) | p2.str.contains(
        " / ", na=False, regex=False
    )
    tour = df.get("tournament", pd.Series("", index=df.index)).astype(str).str.lower()
    tour_dbl = tour.str.contains(r"\bdoubles\b", na=False, regex=True)
    drop = url_dbl | name_dbl | tour_dbl
    return df.loc[~drop].copy()


def _filter_df_major_atp_wta_vectorized(df: pd.DataFrame) -> pd.DataFrame:
    """Équivalent à _is_major_atp_wta sur chaque ligne, sans apply (pandas vectorisé)."""
    if df is None or df.empty:
        return df
    c = df.get("category", pd.Series("", index=df.index)).astype(str).str.strip().str.upper()
    ok_cat = c.isin(["ATP", "WTA"])
    t = df.get("tournament", pd.Series("", index=df.index)).astype(str).str.lower().fillna("")
    minor = pd.Series(False, index=df.index)
    minor_tokens = (
        "challenger",
        "itf",
        "utr",
        "utr pro tennis",
        "universal tennis",
        "futures",
        "future",
        "m15",
        "m25",
        "m35",
        "m50",
        "m60",
        "m80",
        "m100",
        "w15",
        "w25",
        "w35",
        "w50",
        "w60",
        "w80",
        "w100",
    )
    for tok in minor_tokens:
        minor = minor | t.str.contains(tok, case=False, na=False, regex=False)
    return df.loc[ok_cat & ~minor].copy()


def _filter_df_today_tomorrow_only(df: pd.DataFrame) -> pd.DataFrame:
    """
    Garde uniquement les matchs calendaires « aujourd'hui » et « demain » (heure locale).
    Le scraper joint already today+tomorrow ; la colonne date est fiable par ligne.
    Les lignes dont l'heure commence par « Demain » sont traitées comme lendemain.
    """
    if df is None or df.empty:
        return df
    if "date" not in df.columns:
        return df
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    dseries = pd.to_datetime(df["date"], errors="coerce")
    day_ok = dseries.dt.date.isin([today, tomorrow])

    demain = pd.Series([False] * len(df), index=df.index)
    if "time" in df.columns:
        demain = df["time"].astype(str).str.strip().str.startswith("Demain")

    keep = day_ok | demain
    out = df.loc[keep].copy()
    return out if not out.empty else df


@st.cache_data(ttl=300)
def get_latest_scraped_data(
    csv_path: str,
    _prematch_mtime: float,
    _profile_cache_schema: int,
    _model_mtime: float = 0.0,
    _engines_cache_version: int = 0,
):
    """Cache invalidation keys:
    - _prematch_mtime    : nouveau scrape de cotes
    - _profile_cache_schema : nouveau schéma profils
    - _model_mtime       : nouveau modèle ML entraîné (mtime du fichier .pkl)
    - _engines_cache_version : bump manuel après changement d'API stats/ML
    """
    t0 = time.perf_counter()
    t_last = t0

    def _mark(step: str):
        nonlocal t_last
        if PERF_LOG_LIVE_BUILD:
            now = time.perf_counter()
            print(
                f"[live-build] {step}: +{(now - t_last):.2f}s (total {(now - t0):.2f}s)",
                flush=True,
            )
            t_last = now

    if not csv_path or not os.path.isfile(csv_path):
        return []
    df = pd.read_csv(csv_path)
    _mark("read_csv")

    df = _filter_df_exclude_doubles_prematch(df)
    _mark("filter_doubles")
    if df.empty:
        return []
    
    # Filtrer uniquement gros tournois ATP/WTA (exclut Challenger/ITF/mineurs).
    df = _filter_df_major_atp_wta_vectorized(df)
    _mark("filter_major")
    if df.empty:
        return []

    if LIVE_ONLY_TODAY_TOMORROW:
        df = _filter_df_today_tomorrow_only(df)
        _mark("filter_today_tomorrow")
        if df.empty:
            return []

    # Garde-fou principal de latence: limiter le volume traité AVANT tout calcul coûteux.
    # En mode rapide, on ne construit qu'un sous-ensemble de matchs plausiblement à venir.
    now_time = datetime.now().time()
    max_live_build = int(os.getenv("BETTINGHUD_MAX_LIVE_MATCHES_BUILD", "24" if FAST_LIVE_MODE else "120"))

    def _is_probably_upcoming(time_str):
        s = str(time_str or "").strip()
        if not s:
            return True
        if s.startswith("Demain"):
            return True
        try:
            t = datetime.strptime(s, "%H:%M").time()
            return t >= now_time
        except Exception:
            return True

    df = df[df.get("time", pd.Series(dtype=str)).map(_is_probably_upcoming)] if "time" in df.columns else df
    _mark("filter_upcoming")
    if df.empty:
        return []
    if len(df) > max_live_build:
        df = _cap_live_build_prioritize_demain(df, max_live_build)
    
    # Une seule lecture par joueur (URL unique), pas une par ligne de match
    urls_to_fetch = []
    seen_urls = set()
    for _, row in df.iterrows():
        u1, u2 = row.get("p1_url"), row.get("p2_url")
        if pd.notna(u1) and isinstance(u1, str) and u1.strip():
            x = u1.strip()
            if x not in seen_urls:
                seen_urls.add(x)
                urls_to_fetch.append(x)
        if pd.notna(u2) and isinstance(u2, str) and u2.strip():
            x = u2.strip()
            if x not in seen_urls:
                seen_urls.add(x)
                urls_to_fetch.append(x)
    # Garde-fou perf: au-delà, on laisse les autres joueurs passer via base/fallback.
    max_profile_fetch = max(1, min(300, int(os.getenv("BETTINGHUD_MAX_PROFILE_FETCH", "100"))))
    profile_fetch_workers = max(1, min(8, int(os.getenv("BETTINGHUD_PROFILE_SCRAPE_WORKERS", "6"))))
    profiles_by_url = {}
    if ENABLE_PROFILE_SCRAPE:
        targets = urls_to_fetch[:max_profile_fetch]

        def _scrape_u(url: str):
            return url, profile_scraper.scrape_profile(url)

        if len(targets) <= 1:
            profiles_by_url = {u: profile_scraper.scrape_profile(u) for u in targets}
        else:
            with ThreadPoolExecutor(max_workers=profile_fetch_workers) as pool:
                futs = [pool.submit(_scrape_u, u) for u in targets]
                for fut in as_completed(futs):
                    try:
                        u, prof = fut.result()
                        profiles_by_url[u] = prof
                    except Exception:
                        pass
    _mark("profile_scrape")

    # Pré-calcul identité/stats par joueur (évite recalcul coûteux à chaque ligne)
    unique_players = pd.concat([df["player1"], df["player2"]]).dropna().astype(str).str.strip().unique().tolist()
    player_url_map = {}
    tour_by_player = {}
    for _, row in df.iterrows():
        cat = str(row.get("category") or "").upper()
        th = "WTA" if "WTA" in cat else "ATP"
        p1_name = str(row.get("player1") or "").strip()
        p2_name = str(row.get("player2") or "").strip()
        p1_url_raw = row.get("p1_url")
        p2_url_raw = row.get("p2_url")
        p1_url = str(p1_url_raw).strip() if pd.notna(p1_url_raw) and str(p1_url_raw).strip() else None
        p2_url = str(p2_url_raw).strip() if pd.notna(p2_url_raw) and str(p2_url_raw).strip() else None
        for pname, purl in ((p1_name, p1_url), (p2_name, p2_url)):
            if pname and pname not in tour_by_player:
                tour_by_player[pname] = th
            if pname and pname not in player_url_map and purl:
                player_url_map[pname] = purl
    pid_by_name = {}
    stats_by_name = {}
    # Cache session des résolutions (évite de recalculer le même joueur à chaque rerun/refresh).
    player_resolve_cache = st.session_state.setdefault("player_resolve_cache", {})
    if st.session_state.get("_stats_provenance_cache_v", 0) < 2:
        player_resolve_cache.clear()
        st.session_state["_stats_provenance_cache_v"] = 2
    cache_hits = 0
    if FAST_LIVE_MODE:
        # Mode ultra-rapide: on évite les résolutions identité/stats (coût principal CPU).
        for pname in unique_players:
            pid_by_name[pname] = None
            stats_by_name[pname] = {
                "rank": 100, "age": 25, "ht": 185, "pts": 1000, "hand": "U",
                "stats_source": "fast_default",
                "stats_reference_date": None,
            }
    else:

        def _resolve_one_player(pname: str):
            cache_key = (
                pname,
                str(player_url_map.get(pname) or ""),
                str(tour_by_player.get(pname) or ""),
            )
            cached = player_resolve_cache.get(cache_key)
            if cached is not None:
                return pname, cached.get("pid"), dict(cached.get("stats") or {}), True
            th = tour_by_player.get(pname)
            meta = stats_engine.get_player_id_meta(
                pname,
                source_name="flashscore",
                source_url=player_url_map.get(pname),
                tour_hint=th,
            )
            pid = meta.get("player_id")
            st_out = stats_engine.get_player_stats(pid, pname, tour_hint=th)
            player_resolve_cache[cache_key] = {"pid": pid, "stats": dict(st_out)}
            return pname, pid, st_out, False

        workers = min(IDENTITY_RESOLVE_WORKERS, max(1, len(unique_players)))
        if workers <= 1 or len(unique_players) <= 2:
            for pname in unique_players:
                pname2, pid, st_out, from_cache = _resolve_one_player(pname)
                pid_by_name[pname2] = pid
                stats_by_name[pname2] = st_out
                if from_cache:
                    cache_hits += 1
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_resolve_one_player, pn): pn for pn in unique_players}
                for fut in as_completed(futures):
                    pname2, pid, st_out, from_cache = fut.result()
                    pid_by_name[pname2] = pid
                    stats_by_name[pname2] = st_out
                    if from_cache:
                        cache_hits += 1
    _mark("identity+stats")
    if PERF_LOG_LIVE_BUILD:
        print(
            f"[live-build] identity cache hits: {cache_hits}/{len(unique_players)}",
            flush=True,
        )

    # Prépare une fois les stats finalisées + forme/fatigue par joueur (évite recalcul par ligne).
    prepared_stats_by_name = {}
    form_by_name = {}
    fatigue_by_name = {}
    match_quality_by_name = {}
    speed_profile_by_name = {}
    bp_resilience_by_name = {}
    for pname in unique_players:
        base = dict(
            stats_by_name.get(
                pname,
                {"rank": 100, "age": 25, "ht": 185, "pts": 1000, "hand": "U",
                 "stats_source": "no_ranking_source", "stats_reference_date": None},
            )
        )
        base["_pid"] = pid_by_name.get(pname)
        prof = profiles_by_url.get(player_url_map.get(pname)) if ENABLE_PROFILE_SCRAPE else None
        frm, fat = _merge_live_profile(base, prof, tour_hint=tour_by_player.get(pname))
        base.pop("_pid", None)
        prepared_stats_by_name[pname] = base
        form_by_name[pname] = frm
        fatigue_by_name[pname] = fat
        # Causal "workload" features (replace the v1 spurious workload7_* features):
        # wins_last7d (form), three_setters_last14d (true fatigue), last_round_reached.
        try:
            mq = stats_engine.get_recent_match_quality(
                pid_by_name.get(pname), tour_hint=tour_by_player.get(pname)
            )
        except Exception:
            mq = {"wins_last7d": 0, "three_setters_last14d": 0, "last_round_reached": 0}
        match_quality_by_name[pname] = mq
        try:
            sp = stats_engine.get_speed_profile(
                pid_by_name.get(pname), tour_hint=tour_by_player.get(pname)
            )
        except Exception:
            sp = {"speed_affinity": 0.0, "speed_performance_delta": 0.0, "samples": 0}
        speed_profile_by_name[pname] = sp
        try:
            bp_resilience_by_name[pname] = float(stats_engine.get_bp_resilience(
                pid_by_name.get(pname), tour_hint=tour_by_player.get(pname)
            ))
        except Exception:
            bp_resilience_by_name[pname] = 0.5
    _mark("prepare_player_forms")

    h2h_cache = {}
    
    matches = []
    for _, row in df.iterrows():
        # Obtenir les IDs et stats
        p1_name = str(row["player1"]).strip()
        p2_name = str(row["player2"]).strip()
        p1_id = pid_by_name.get(p1_name)
        p2_id = pid_by_name.get(p2_name)
        
        p1_stats = dict(
            prepared_stats_by_name.get(
                p1_name,
                {"rank": 100, "age": 25, "ht": 185, "pts": 1000, "hand": "U",
                 "stats_source": "no_ranking_source", "stats_reference_date": None},
            )
        )
        p2_stats = dict(
            prepared_stats_by_name.get(
                p2_name,
                {"rank": 100, "age": 25, "ht": 185, "pts": 1000, "hand": "U",
                 "stats_source": "no_ranking_source", "stats_reference_date": None},
            )
        )
        
        surface = _infer_surface(row.get("tournament", ""))
        
        true_odd_p1 = 2.0
        true_odd_p2 = 2.0
        confidence = None
        calibration_used = "Globale"
        feature_snapshot = {}
        top_features = []
        
        # Advanced Stats
        match_tour = tour_by_player.get(p1_name) or tour_by_player.get(p2_name)
        h2h_key = (str(p1_id), str(p2_id), p1_name, p2_name, match_tour)
        if FAST_LIVE_MODE:
            h2h = {"p1_wins": 0, "p2_wins": 0}
        elif h2h_key in h2h_cache:
            h2h = h2h_cache[h2h_key]
        else:
            h2h = stats_engine.get_h2h(p1_id, p2_id, p1_name, p2_name, tour_hint=match_tour)
            h2h_cache[h2h_key] = h2h
        
        p1_form = form_by_name.get(p1_name, {"win_pct": 50.0, "matches": 0})
        p2_form = form_by_name.get(p2_name, {"win_pct": 50.0, "matches": 0})
        p1_fatigue = fatigue_by_name.get(p1_name, {"minutes_played": 0, "matches": 0})
        p2_fatigue = fatigue_by_name.get(p2_name, {"minutes_played": 0, "matches": 0})
        
        try:
            adv_signals = (
                {
                    "style_advantage_score": 0.5,
                    "p1_clutch_index": 0.5,
                    "p2_clutch_index": 0.5,
                    "p1_days": 7,
                    "p2_days": 7,
                    "p1_first_srv_win10": 0.68,
                    "p2_first_srv_win10": 0.68,
                    "p1_bp_conv10": 0.38,
                    "p2_bp_conv10": 0.38,
                    "p1_dominance_ratio": 1.0,
                    "p2_dominance_ratio": 1.0,
                }
                if not ENABLE_ADV_SIGNALS
                else _compute_live_advanced_signals(p1_name, p2_name, surface, tour_hint=match_tour or "ATP")
            )
            preds = ml_model.predict_match(
                surface=surface,
                p1_name=p1_name,
                p2_name=p2_name,
                p1_rank=p1_stats['rank'], p2_rank=p2_stats['rank'],
                p1_age=p1_stats['age'], p2_age=p2_stats['age'],
                p1_ht=p1_stats['ht'], p2_ht=p2_stats['ht'],
                p1_pts=p1_stats['pts'], p2_pts=p2_stats['pts'],
                p1_id=p1_id, p2_id=p2_id,
                p1_form_win_pct_90=p1_form.get("win_pct", 50),
                p2_form_win_pct_90=p2_form.get("win_pct", 50),
                p1_fatigue_minutes_14=p1_fatigue.get("minutes_played", 0),
                p2_fatigue_minutes_14=p2_fatigue.get("minutes_played", 0),
                p1_fatigue_matches_14=p1_fatigue.get("matches", 0),
                p2_fatigue_matches_14=p2_fatigue.get("matches", 0),
                p1_hand=p1_stats.get("hand", "U"),
                p2_hand=p2_stats.get("hand", "U"),
                h2h_p1_wins=h2h.get("p1_wins", 0),
                h2h_p2_wins=h2h.get("p2_wins", 0),
                p1_style_advantage_score=adv_signals.get("style_advantage_score"),
                p1_clutch_index=adv_signals.get("p1_clutch_index"),
                p2_clutch_index=adv_signals.get("p2_clutch_index"),
                p1_first_srv_win10=adv_signals.get("p1_first_srv_win10"),
                p2_first_srv_win10=adv_signals.get("p2_first_srv_win10"),
                p1_bp_conv10=adv_signals.get("p1_bp_conv10"),
                p2_bp_conv10=adv_signals.get("p2_bp_conv10"),
                p1_dominance_ratio=adv_signals.get("p1_dominance_ratio"),
                p2_dominance_ratio=adv_signals.get("p2_dominance_ratio"),
                p1_days_since_last_match=adv_signals.get("p1_days"),
                p2_days_since_last_match=adv_signals.get("p2_days"),
                p1_wins_last7d=match_quality_by_name.get(p1_name, {}).get("wins_last7d", 0),
                p2_wins_last7d=match_quality_by_name.get(p2_name, {}).get("wins_last7d", 0),
                p1_three_setters_last14d=match_quality_by_name.get(p1_name, {}).get("three_setters_last14d", 0),
                p2_three_setters_last14d=match_quality_by_name.get(p2_name, {}).get("three_setters_last14d", 0),
                p1_last_round_reached=match_quality_by_name.get(p1_name, {}).get("last_round_reached", 0),
                p2_last_round_reached=match_quality_by_name.get(p2_name, {}).get("last_round_reached", 0),
                p1_speed_affinity=speed_profile_by_name.get(p1_name, {}).get("speed_affinity", 0.0),
                p2_speed_affinity=speed_profile_by_name.get(p2_name, {}).get("speed_affinity", 0.0),
                p1_speed_performance_delta=speed_profile_by_name.get(p1_name, {}).get("speed_performance_delta", 0.0),
                p2_speed_performance_delta=speed_profile_by_name.get(p2_name, {}).get("speed_performance_delta", 0.0),
                p1_bp_resilience=bp_resilience_by_name.get(p1_name, 0.5),
                p2_bp_resilience=bp_resilience_by_name.get(p2_name, 0.5),
                tournament_name=str(row.get("tournament", "") or ""),
                tour=match_tour or "ATP",
            )
            true_odd_p1 = preds['p1_true_odd']
            true_odd_p2 = preds['p2_true_odd']
            confidence = preds.get("confidence", None)
            calibration_used = preds.get("calibration_used", "Globale")
            feature_snapshot = preds.get("feature_snapshot", {}) or {}
            top_features = preds.get("top_features", []) or []
        except Exception as _exc:
            if PERF_LOG_LIVE_BUILD:
                import traceback as _tb
                print(f"[live-build][ERR predict] {p1_name} vs {p2_name}: {_exc!r}", flush=True)
                _tb.print_exc()

        # Gestion des cotes Flashscore manquantes
        odd_p1 = float(row['odd_p1']) if pd.notna(row['odd_p1']) else 0.0
        odd_p2 = float(row['odd_p2']) if pd.notna(row['odd_p2']) else 0.0
        
        # Si Flashscore n'affiche pas les cotes sur la page d'accueil, on les simule pour l'affichage MVP
        if odd_p1 == 0.0 or odd_p2 == 0.0:
            from scripts.simulate_day import generate_bookmaker_odds
            odd_p1, odd_p2, _ = generate_bookmaker_odds(p1_stats['pts'], p2_stats['pts'])

        prematch_id = row.get("id")
        if pd.isna(prematch_id):
            prematch_id = ""
        else:
            prematch_id = str(prematch_id)

        p1_url = player_url_map.get(p1_name)
        p2_url = player_url_map.get(p2_name)
        p1_profile_loaded = bool(profiles_by_url.get(p1_url)) if p1_url else False
        p2_profile_loaded = bool(profiles_by_url.get(p2_url)) if p2_url else False

        matches.append({
            "prematch_id": prematch_id,
            "date": row.get('date', row['scraped_at'][:10]),
            "tournament": row.get('tournament', 'Tournoi en cours'),
            "category": row.get('category', 'ATP/WTA'),
            "tour": match_tour or "ATP",
            "time": row['time'],
            "player1": row['player1'],
            "player2": row['player2'],
            "surface": surface,
            "odd_p1": odd_p1,
            "odd_p2": odd_p2,
            "true_odd_p1": true_odd_p1,
            "true_odd_p2": true_odd_p2,
            "confidence": confidence,
            "calibration_used": calibration_used,
            "feature_snapshot": feature_snapshot,
            "top_features": top_features,
            "p1_stats": p1_stats,
            "p2_stats": p2_stats,
            "h2h": h2h,
            "p1_form": p1_form,
            "p2_form": p2_form,
            "p1_fatigue": p1_fatigue,
            "p2_fatigue": p2_fatigue,
            "p1_match_quality": match_quality_by_name.get(p1_name, {}),
            "p2_match_quality": match_quality_by_name.get(p2_name, {}),
            "p1_profile_loaded": p1_profile_loaded,
            "p2_profile_loaded": p2_profile_loaded,
        })
    _mark("rows_build+predict")
    return matches

st.title("🎾 BettingHUD - Value Bets Tracker")
st.markdown("---")

from scripts.bets_db import (
    compute_live_tracker_bankroll_eur,
    get_data_freshness_snapshot,
    init_all as _init_bets_db,
    save_bet_enriched as _save_bet_enriched,
    set_live_tracker_manual_adjust_eur,
    set_live_tracker_start_br,
)


def _format_meta_utc(iso_ts) -> str:
    if not iso_ts:
        return "— (jamais enregistré)"
    try:
        s = str(iso_ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return str(iso_ts)


def _format_mtime_local(ts) -> str:
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M (fichier)")
    except Exception:
        return "—"


def init_db():
    """Run all bets-DB migrations (idempotent)."""
    _init_bets_db('data/bettinghud.db')

init_db()


def live_tracker_bankroll_snapshot(*, include_legacy: bool = True) -> dict:
    conn = sqlite3.connect("data/bettinghud.db")
    try:
        snap = compute_live_tracker_bankroll_eur(conn)
        if not include_legacy:
            return snap

        # Option "mixte": inclut aussi les paris sans tracker_source
        # (anciens/manuels) dans la BR affichée/reco.
        cur = conn.execute(
            """
            SELECT COALESCE(SUM(stake), 0)
            FROM user_bets
            WHERE COALESCE(TRIM(tracker_source), '') = ''
              AND COALESCE(TRIM(status), '') = 'En cours'
            """
        )
        legacy_open = float(cur.fetchone()[0] or 0.0)
        cur2 = conn.execute(
            """
            SELECT COALESCE(SUM(profit), 0)
            FROM user_bets
            WHERE COALESCE(TRIM(tracker_source), '') = ''
              AND COALESCE(TRIM(status), '') != 'En cours'
            """
        )
        legacy_profit = float(cur2.fetchone()[0] or 0.0)

        snap["committed_open_eur"] = float(snap.get("committed_open_eur", 0.0)) + legacy_open
        snap["settled_profit_eur"] = float(snap.get("settled_profit_eur", 0.0)) + legacy_profit
        snap["available_raw_eur"] = (
            float(snap["start_eur"]) + float(snap["settled_profit_eur"]) - float(snap["committed_open_eur"])
        )
        snap["available_eur"] = float(snap["available_raw_eur"]) + float(snap.get("manual_adjust_eur", 0.0))
        snap["equity_eur"] = float(snap["available_eur"]) + float(snap["committed_open_eur"])
        snap["bankroll_mode"] = "mixed"
        return snap
    finally:
        conn.close()


def save_bet(
    match_name,
    bet_on,
    odds,
    stake,
    *,
    tour=None,
    surface=None,
    tournament=None,
    match_id=None,
    p_model=None,
    ev_at_bet=None,
    bookmaker_source=None,
    notes=None,
    tracker_source=None,
) -> int:
    """Persist a bet with optional decision-context fields. Falls back to a
    minimal insert when called with positional arguments only (legacy path)."""
    bet_id = _save_bet_enriched(
        match_name=match_name,
        bet_on=bet_on,
        odds=float(odds),
        stake=float(stake),
        tour=tour,
        surface=surface,
        tournament=tournament,
        match_id=match_id,
        p_model=p_model,
        ev_at_bet=ev_at_bet,
        bookmaker_source=bookmaker_source,
        notes=notes,
        tracker_source=tracker_source,
    )
    st.toast("✅ Pari enregistré avec succès dans votre portefeuille !")
    return int(bet_id or 0)

def update_bet_status(bet_id, status, profit):
    conn = sqlite3.connect('data/bettinghud.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE user_bets SET status = ?, profit = ? WHERE id = ?
    ''', (status, profit, bet_id))
    conn.commit()
    conn.close()


def get_existing_bets_index():
    """
    Index des mises déjà posées par (match_name, bet_on) -> stake_total.
    Sert à afficher une pastille "déjà parié" dans les cartes Live.
    """
    conn = sqlite3.connect('data/bettinghud.db')
    try:
        df = pd.read_sql(
            """
            SELECT match_name, bet_on, SUM(COALESCE(stake, 0)) AS stake_total
            FROM user_bets
            GROUP BY match_name, bet_on
            """,
            conn,
        )
    finally:
        conn.close()

    if df.empty:
        return {}
    return {
        (str(r["match_name"]), str(r["bet_on"])): float(r["stake_total"] or 0.0)
        for _, r in df.iterrows()
    }

@st.cache_data(ttl=900)
def compute_model_diagnostics(year_start: int, year_end: int, max_matches: int, seed: int):
    import random
    from scripts.simulate_day import generate_bookmaker_odds

    y0, y1 = sorted((int(year_start), int(year_end)))
    date_min = f"{y0}0101"
    date_max = f"{y1}1231"

    conn = sqlite3.connect("data/bettinghud.db")
    query = (
        "SELECT * FROM matches_recent "
        "WHERE source = 'tennismylife' "
        "AND CAST(tourney_date AS TEXT) BETWEEN ? AND ? "
        "AND tourney_level IN ('A','M','G') "
        "ORDER BY tourney_date ASC"
    )
    df = pd.read_sql(query, conn, params=(date_min, date_max))
    conn.close()
    if df.empty:
        return None

    if max_matches and len(df) > int(max_matches):
        df = df.sample(n=int(max_matches), random_state=int(seed)).sort_values("tourney_date").reset_index(drop=True)

    rng = random.Random(int(seed))
    detector = ValueDetector(min_value_threshold=0.05)

    preds_rows = []
    bets_rows = []

    for _, row in df.iterrows():
        is_p1_winner = rng.choice([True, False])
        if is_p1_winner:
            p1_rank, p2_rank = row["winner_rank"], row["loser_rank"]
            p1_age, p2_age = row["winner_age"], row["loser_age"]
            p1_ht, p2_ht = row["winner_ht"], row["loser_ht"]
            p1_pts, p2_pts = row["winner_rank_points"], row["loser_rank_points"]
            p1_id, p2_id = row["winner_id"], row["loser_id"]
            p1_hand, p2_hand = row["winner_hand"], row["loser_hand"]
            p1_ioc, p2_ioc = row["winner_ioc"], row["loser_ioc"]
        else:
            p1_rank, p2_rank = row["loser_rank"], row["winner_rank"]
            p1_age, p2_age = row["loser_age"], row["winner_age"]
            p1_ht, p2_ht = row["loser_ht"], row["winner_ht"]
            p1_pts, p2_pts = row["loser_rank_points"], row["winner_rank_points"]
            p1_id, p2_id = row["loser_id"], row["winner_id"]
            p1_hand, p2_hand = row["loser_hand"], row["winner_hand"]
            p1_ioc, p2_ioc = row["loser_ioc"], row["winner_ioc"]

        # robust defaults
        p1_rank = 100 if pd.isna(p1_rank) else p1_rank
        p2_rank = 100 if pd.isna(p2_rank) else p2_rank
        p1_age = 25 if pd.isna(p1_age) else p1_age
        p2_age = 25 if pd.isna(p2_age) else p2_age
        p1_ht = 185 if pd.isna(p1_ht) else p1_ht
        p2_ht = 185 if pd.isna(p2_ht) else p2_ht
        p1_pts = 1000 if pd.isna(p1_pts) else p1_pts
        p2_pts = 1000 if pd.isna(p2_pts) else p2_pts

        try:
            pred = ml_model.predict_match(
                surface=row["surface"],
                p1_name=row["winner_name"] if is_p1_winner else row["loser_name"],
                p2_name=row["loser_name"] if is_p1_winner else row["winner_name"],
                p1_rank=p1_rank, p2_rank=p2_rank,
                p1_age=p1_age, p2_age=p2_age,
                p1_ht=p1_ht, p2_ht=p2_ht,
                p1_pts=p1_pts, p2_pts=p2_pts,
                p1_id=p1_id, p2_id=p2_id,
                p1_hand=p1_hand, p2_hand=p2_hand,
                tournament_name=row["tourney_name"],
                p1_ioc=p1_ioc, p2_ioc=p2_ioc,
                tour="ATP",
            )
        except Exception:
            continue

        y_true = 1 if is_p1_winner else 0
        p1_prob = float(pred["p1_win_prob"])
        conf = float(pred.get("confidence", abs(p1_prob - 0.5) * 2))
        preds_rows.append({"p1_prob": p1_prob, "y_true": y_true, "confidence": conf})

        bm1, bm2, _ = generate_bookmaker_odds(float(p1_pts), float(p2_pts))
        p1_val = detector.detect_value(bm1, pred["p1_true_odd"], confidence=conf)
        p2_val = detector.detect_value(bm2, pred["p2_true_odd"], confidence=conf)

        if p1_val["is_value"] and p1_val["value_pct"] >= p2_val["value_pct"]:
            won = bool(is_p1_winner)
            ret = (bm1 - 1.0) if won else -1.0
            bets_rows.append({"confidence": conf, "ret": ret})
        elif p2_val["is_value"]:
            won = not bool(is_p1_winner)
            ret = (bm2 - 1.0) if won else -1.0
            bets_rows.append({"confidence": conf, "ret": ret})

    if not preds_rows:
        return None

    dff = pd.DataFrame(preds_rows)
    dff["prob_bin"] = pd.cut(dff["p1_prob"], bins=np.linspace(0, 1, 11), include_lowest=True)
    calib = dff.groupby("prob_bin", observed=False).agg(
        mean_prob=("p1_prob", "mean"),
        observed_win_rate=("y_true", "mean"),
        n=("y_true", "size"),
    ).reset_index()
    calib = calib[calib["n"] > 0]

    if bets_rows:
        dfb = pd.DataFrame(bets_rows)
        dfb["conf_bin"] = pd.cut(dfb["confidence"], bins=np.linspace(0, 1, 6), include_lowest=True)
        roi_conf = dfb.groupby("conf_bin", observed=False).agg(
            roi=("ret", "mean"),
            bets=("ret", "size"),
        ).reset_index()
        roi_conf = roi_conf[roi_conf["bets"] > 0]
        total_bets = int(len(dfb))
        total_roi = float(dfb["ret"].mean() * 100.0)
    else:
        roi_conf = pd.DataFrame(columns=["conf_bin", "roi", "bets"])
        total_bets = 0
        total_roi = 0.0

    acc = float(((dff["p1_prob"] >= 0.5).astype(int) == dff["y_true"]).mean())
    brier = float(np.mean((dff["p1_prob"] - dff["y_true"]) ** 2))
    mean_conf = float(dff["confidence"].mean())
    return {
        "n_matches": int(len(dff)),
        "accuracy": acc,
        "brier": brier,
        "mean_confidence": mean_conf,
        "total_bets": total_bets,
        "total_roi": total_roi,
        "calibration": calib,
        "roi_by_conf": roi_conf,
    }


@st.cache_data(ttl=600)
def compute_identity_diagnostics(csv_path: str, _prematch_mtime: float):
    if not csv_path or not os.path.isfile(csv_path):
        return {"summary": {}, "details": pd.DataFrame()}
    df = pd.read_csv(csv_path)
    if df.empty:
        return {"summary": {}, "details": pd.DataFrame()}
    names = pd.concat([df.get("player1", pd.Series(dtype=str)), df.get("player2", pd.Series(dtype=str))]).dropna()
    uniq = sorted(set(str(x).strip() for x in names.tolist() if str(x).strip()))
    rows = []
    for raw in uniq:
        meta = stats_engine.get_player_id_meta(raw)
        rows.append(
            {
                "raw_name": raw,
                "player_id": meta.get("player_id"),
                "method": meta.get("method"),
                "confidence": float(meta.get("confidence", 0.0) or 0.0),
                "reason": meta.get("reason"),
            }
        )
    dfr = pd.DataFrame(rows)
    unresolved = int((dfr["player_id"].isna()).sum()) if not dfr.empty else 0
    low_conf = int((dfr["confidence"] < 0.90).sum()) if not dfr.empty else 0
    summary = {
        "players_seen": int(len(dfr)),
        "resolved": int(len(dfr) - unresolved),
        "unresolved": unresolved,
        "low_conf": low_conf,
        "resolved_pct": float((len(dfr) - unresolved) / len(dfr) * 100.0) if len(dfr) else 0.0,
    }
    dfr = dfr.sort_values(["player_id", "confidence"], ascending=[True, True]) if not dfr.empty else dfr
    return {"summary": summary, "details": dfr}


def _clip01(x):
    return float(np.clip(float(x), 0.01, 0.99))


def _build_alternative_markets(match: dict, detector: ValueDetector):
    """
    Projet annexe: marchés alternatifs (hors vainqueur), sans backtest.
    Génère des opportunités value heuristiques et explicables.
    """
    p1_true = float(match.get("true_odd_p1") or 2.0)
    p2_true = float(match.get("true_odd_p2") or 2.0)
    p1_prob = _clip01(1.0 / max(1.01, p1_true))
    p2_prob = _clip01(1.0 / max(1.01, p2_true))
    confidence = float(match.get("confidence") or abs(p1_prob - 0.5) * 2.0)
    closeness = 1.0 - abs(p1_prob - 0.5) * 2.0

    p1_fat = float((match.get("p1_fatigue") or {}).get("minutes_played", 0) or 0)
    p2_fat = float((match.get("p2_fatigue") or {}).get("minutes_played", 0) or 0)
    fatigue_factor = np.clip((p1_fat + p2_fat) / 420.0, 0.0, 1.0)
    fs = match.get("feature_snapshot") or {}
    inact = abs(float(fs.get("inactivity_decay_weight") or 0.0))
    inact_factor = np.clip(inact, 0.0, 1.0)
    clutch_diff = abs(float(fs.get("clutch_index_diff") or 0.0))

    markets = []

    # 1) Total jeux OVER 22.5
    over_games_prob = _clip01(0.41 + 0.34 * closeness + 0.12 * fatigue_factor + 0.08 * inact_factor)
    over_games_book_prob = _clip01(over_games_prob - (0.045 + 0.02 * max(0.0, closeness - 0.4)))
    over_games_true_odd = 1.0 / over_games_prob
    over_games_book_odd = 1.0 / over_games_book_prob
    over_val = detector.detect_value(over_games_book_odd, over_games_true_odd, confidence=confidence)
    if over_val.get("is_value"):
        markets.append({
            "market": "Total jeux",
            "selection": "Over 22.5 jeux",
            "book_odd": over_games_book_odd,
            "true_odd": over_games_true_odd,
            "value_pct": float(over_val.get("value_pct", 0.0)),
            "why": [
                f"Match potentiellement serré (indice close={closeness:.2f})",
                f"Charge récente cumulée élevée ({p1_fat:.0f}+{p2_fat:.0f} min)",
                "Scénario long plus probable -> valeur sur Over 22.5",
            ],
        })

    # 2) Tie-break OUI
    tiebreak_prob = _clip01(0.18 + 0.22 * closeness + 0.08 * clutch_diff + 0.10 * (1.0 - confidence))
    tiebreak_book_prob = _clip01(tiebreak_prob - 0.04)
    tiebreak_true_odd = 1.0 / tiebreak_prob
    tiebreak_book_odd = 1.0 / tiebreak_book_prob
    tb_val = detector.detect_value(tiebreak_book_odd, tiebreak_true_odd, confidence=confidence)
    if tb_val.get("is_value"):
        markets.append({
            "market": "Tie-break",
            "selection": "Tie-break dans le match: OUI",
            "book_odd": tiebreak_book_odd,
            "true_odd": tiebreak_true_odd,
            "value_pct": float(tb_val.get("value_pct", 0.0)),
            "why": [
                f"Niveau perçu proche (P1={p1_prob*100:.1f}% / P2={p2_prob*100:.1f}%)",
                f"Indice clutch équilibré (|diff|={clutch_diff:.2f})",
                "Risque de set accroché -> value sur tie-break OUI",
            ],
        })

    # 3) Match en 3 sets (Over 2.5 sets)
    three_sets_prob = _clip01(0.25 + 0.40 * closeness + 0.06 * (1.0 - confidence))
    three_sets_book_prob = _clip01(three_sets_prob - 0.05)
    three_sets_true_odd = 1.0 / three_sets_prob
    three_sets_book_odd = 1.0 / three_sets_book_prob
    sets_val = detector.detect_value(three_sets_book_odd, three_sets_true_odd, confidence=confidence)
    if sets_val.get("is_value"):
        markets.append({
            "market": "Nombre de sets",
            "selection": "Over 2.5 sets",
            "book_odd": three_sets_book_odd,
            "true_odd": three_sets_true_odd,
            "value_pct": float(sets_val.get("value_pct", 0.0)),
            "why": [
                f"Confiance modérée ({confidence:.2f}) -> issue moins tranchée",
                "Signal de match disputé (probas proches)",
                "Value détectée sur scénario en 3 sets",
            ],
        })

    return markets


@st.cache_resource
def start_auto_tours_db_sync():
    """Une seule fois par process Streamlit : sync ATP+WTA périodique (défaut 24h)."""
    if not _env_flag("BETTINGHUD_AUTO_SYNC_TOURS", True):
        return None
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sync_script = os.path.join(project_root, "scripts", "sync_tours_daily.py")
    delay = max(30, int(os.getenv("BETTINGHUD_AUTO_SYNC_INITIAL_DELAY_SEC", "120")))
    interval = max(300, int(os.getenv("BETTINGHUD_AUTO_SYNC_INTERVAL_SEC", "86400")))

    def _loop():
        time.sleep(delay)
        while True:
            try:
                run_kw: dict = {
                    "args": [sys.executable, sync_script],
                    "cwd": project_root,
                    "stdin": subprocess.DEVNULL,
                }
                if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    run_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[assignment]
                subprocess.run(**run_kw)
            except Exception as exc:
                print(f"[auto-sync-tours-db] {exc}", flush=True)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="auto-sync-tours-db").start()
    return True


@st.cache_resource
def start_weekly_ml_train():
    """Sync TML + réentraînement ML sur un intervalle long (défaut 7 jours)."""
    if not ENABLE_AUTO_ML_TRAIN_WEEKLY:
        return None
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    train_script = os.path.join(project_root, "scripts", "update_model_tml.py")
    delay = AUTO_ML_TRAIN_INITIAL_DELAY_SEC
    interval = AUTO_ML_TRAIN_INTERVAL_SEC

    def _loop():
        time.sleep(delay)
        while True:
            try:
                run_kw: dict = {
                    "args": [sys.executable, train_script],
                    "cwd": project_root,
                    "stdin": subprocess.DEVNULL,
                }
                if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    run_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[assignment]
                subprocess.run(**run_kw)
            except Exception as exc:
                print(f"[weekly-ml-train] {exc}", flush=True)
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="weekly-ml-train").start()
    return True


start_auto_tours_db_sync()
start_weekly_ml_train()

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(["🎯 Live Tracker", "📊 Backtest Kelly (CSV)", "💼 Mon Portefeuille", "🧪 Diagnostics Modèle", "🧩 Annexe - Paris Alternatifs", "📡 Tracking modèle (réel)"])

with tab1:
    if FAST_LIVE_MODE:
        st.info("Mode rapide actif: profils live et signaux avancés sont allégés pour privilégier la réactivité.")
    _csv_path, _csv_mtime = _prematch_csv_signature()
    # Auto-refresh non bloquant : si le dernier CSV est trop vieux (TTL), on déclenche
    # un re-scrape en arrière-plan. La page courante continue d'utiliser le CSV existant.
    if _maybe_auto_refresh_prematch(_csv_path, _csv_mtime):
        st.toast("🔄 Mise à jour des cotes en cours…", icon="⏳")
    real_matches = get_latest_scraped_data(
        _csv_path,
        _csv_mtime,
        PROFILE_CACHE_SCHEMA,
        _ml_model_mtime(),
        _ENGINES_CACHE_VERSION,
    )
    
    # Filtrer les matchs sans cotes valides (si on n'a pas pu les simuler non plus)
    real_matches = [m for m in real_matches if m['odd_p1'] > 1.0 and m['odd_p2'] > 1.0]

    # Filtrer les matchs passés
    current_time = datetime.now().time()
    def is_future_match(time_str):
        if str(time_str).startswith("Demain"):
            return True
        try:
            match_time = datetime.strptime(str(time_str).strip(), "%H:%M").time()
            return match_time >= current_time
        except ValueError:
            return True # Au cas où c'est un autre format (ex: Inconnu)

    real_matches = [m for m in real_matches if is_future_match(m['time'])]

    st.markdown("---")
    
    col_filter_day, col_filter_tourney = st.columns(2)
    
    with col_filter_day:
        day_filter = st.radio("📅 Filtrer par jour :", ["Aujourd'hui", "Demain", "Tous"], horizontal=True)

    if day_filter == "Aujourd'hui":
        st.caption(
            "Les lignes dont l’heure commence par **Demain** sont exclues ici. "
            "Pour Rome / lendemain (ex. Zverev), choisis **Demain** ou **Tous**."
        )
    
    # 1. Filtre par jour
    if day_filter == "Aujourd'hui":
        filtered_matches = [m for m in real_matches if not str(m['time']).startswith("Demain")]
    elif day_filter == "Demain":
        filtered_matches = [m for m in real_matches if str(m['time']).startswith("Demain")]
    else:
        filtered_matches = real_matches

    # Garde-fou UI: ne proposer que gros tournois ATP/WTA.
    filtered_matches = [
        m for m in filtered_matches
        if _is_major_atp_wta(m.get("category"), m.get("tournament"))
    ]

    # Extraire les tournois uniques basés sur le filtre de jour sélectionné
    tournaments = ["Tous"] + sorted(list(set([m['tournament'] for m in filtered_matches])))
    
    with col_filter_tourney:
        tourney_filter = st.selectbox("🏆 Filtrer par tournoi :", tournaments)
        
    # 2. Filtre par tournoi
    if tourney_filter != "Tous":
        filtered_matches = [m for m in filtered_matches if m['tournament'] == tourney_filter]

    _pf1, _pf2 = st.columns([1, 2])
    with _pf1:
        _player_q = st.text_input(
            "Rechercher un joueur",
            value="",
            key="live_player_name_filter",
            placeholder="ex. Zverev",
            help="Filtre table + opportunités sur un joueur (partie du nom, sans tenir compte de la casse).",
        )
    _pq_l = str(_player_q or "").strip().lower()
    if _pq_l:
        filtered_matches = [
            m
            for m in filtered_matches
            if _pq_l in str(m.get("player1", "")).lower()
            or _pq_l in str(m.get("player2", "")).lower()
        ]

    live_col1, live_col2 = st.columns(2)
    with live_col1:
        live_ev_threshold = st.slider(
            "Seuil EV minimum (Live, %)",
            min_value=1.0,
            max_value=12.0,
            value=8.0,
            step=0.5,
            key="live_ev_threshold",
        )
    with live_col2:
        live_conf_filter = st.selectbox(
            "Filtre confiance IA",
            ["Toutes", "Moyenne+", "Forte+", "Très forte"],
            key="live_conf_filter",
        )

    with st.expander("⚙️ Bankroll Live Tracker (référence + ajustements)", expanded=False):
        st.caption(
            "Par défaut **55 €** au premier lancement. "
            "**Disponible calculée** = référence + PnL paris terminés − mises « en cours », "
            "puis **+ ajustement manuel** si tu en as besoin (dépôt/retrait/correction). "
            "Résultats des matchs : onglet Portefeuille."
        )
        st.caption("Mode calcul simplifié actif: tous les paris sont inclus dans la BR Kelly.")
        _cfg_snap = live_tracker_bankroll_snapshot()
        _ls_c1, _ls_c2 = st.columns([2, 1])
        with _ls_c1:
            live_start_edit = st.number_input(
                "Capital de référence € (point de départ)",
                min_value=1.0,
                max_value=100000.0,
                value=float(_cfg_snap["start_eur"]),
                step=1.0,
                key="live_br_start_editor",
                help="Référence comptable ; les paris déjà en base ne sont pas modifiés.",
            )
        with _ls_c2:
            st.markdown("")
            if st.button("💾 Sauver réf.", key="live_br_save_start"):
                cn = sqlite3.connect("data/bettinghud.db")
                try:
                    set_live_tracker_start_br(cn, float(live_start_edit))
                finally:
                    cn.close()
                st.success("Référence enregistrée.")
                st.rerun()

        st.markdown("**Ajustement manuel (mode correctif)**")
        _lm1, _lm2, _lm3 = st.columns([2, 1, 1])
        with _lm1:
            live_manual_edit = st.number_input(
                "± € ajoutés à la BR disponible",
                min_value=-100000.0,
                max_value=100000.0,
                value=float(_cfg_snap.get("manual_adjust_eur", 0.0)),
                step=1.0,
                key="live_br_manual_editor",
                help="Ex. +20 après un dépôt, −10 après un retrait, ou correction d’écart avec la réalité.",
            )
        with _lm2:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("💾 Sauver corr.", key="live_br_save_manual"):
                cn = sqlite3.connect("data/bettinghud.db")
                try:
                    set_live_tracker_manual_adjust_eur(cn, float(live_manual_edit))
                finally:
                    cn.close()
                st.success("Ajustement manuel enregistré.")
                st.rerun()
        with _lm3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("↺ Corr. → 0", key="live_br_reset_manual"):
                cn = sqlite3.connect("data/bettinghud.db")
                try:
                    set_live_tracker_manual_adjust_eur(cn, 0.0)
                finally:
                    cn.close()
                st.success("Correction remise à zéro.")
                st.rerun()

    br_snap = live_tracker_bankroll_snapshot()
    br_avail = max(0.0, float(br_snap["available_eur"]))
    br_committed = float(br_snap["committed_open_eur"])
    br_equity = float(br_snap["equity_eur"])
    mb1, mb2, mb3, mb4 = st.columns(4)
    mb1.metric("BR référence (€)", f"{br_snap['start_eur']:.2f}")
    mb2.metric("BR disponible (€)", f"{br_avail:.2f}")
    mb3.metric("Engagé (€)", f"{br_committed:.2f}")
    mb4.metric("Capital total (€)", f"{br_equity:.2f}")
    _man = float(br_snap.get("manual_adjust_eur", 0.0))
    _capt = (
        "Reco Kelly : % de la **BR disponible** (après paris en cours et correction manuelle éventuelle). "
        "Les résultats des matchs mettent à jour la base via le Portefeuille."
    )
    if abs(_man) > 1e-6:
        _raw = float(br_snap.get("available_raw_eur", br_avail - _man))
        _capt += f" **Correction manuelle : {_man:+.2f} €** (sans correction : {_raw:.2f} €)."
    _capt += " **Mode BR : tous les paris (hypothèse Kelly 1/4 globale).**"
    st.caption(_capt)

    # Streamlit mémorise les `number_input` par clé : si la BR change (nouveau pari, résultat…),
    # on change artificiellement la clé pour réappliquer la reco Kelly sur la mise pré-remplie.
    _live_br_stake_sig = (
        f"{round(br_avail, 2)}|{round(br_committed, 2)}|"
        f"{round(float(br_snap.get('manual_adjust_eur', 0.0)), 2)}"
    )

    detector = ValueDetector(min_value_threshold=float(live_ev_threshold) / 100.0)

    # Identifier les value bets
    value_bets = []
    for idx, match in enumerate(filtered_matches):
        p1_val = detector.detect_value(match["odd_p1"], match["true_odd_p1"], confidence=match.get("confidence"))
        p2_val = detector.detect_value(match["odd_p2"], match["true_odd_p2"], confidence=match.get("confidence"))
        if p1_val["is_value"]:
            value_bets.append({"match": match, "player": 1, "val": p1_val, "idx": idx})
        if p2_val["is_value"]:
            value_bets.append({"match": match, "player": 2, "val": p2_val, "idx": idx})

    # Top KPIs
    kpi1, kpi2, kpi3 = st.columns(3)
    kpi1.metric("🎾 Matchs scannés", len(filtered_matches))
    kpi2.metric("🚨 Value Bets trouvés", len(value_bets))
    if filtered_matches:
        te_full = sum(
            1 for m in filtered_matches if m.get("p1_profile_loaded") and m.get("p2_profile_loaded")
        )
        kpi3.metric("📡 Profils TE complets", f"{te_full}/{len(filtered_matches)} matchs")
    else:
        kpi3.metric("📡 Profils TE complets", "—")
    if _csv_path:
        fresh = datetime.fromtimestamp(_csv_mtime).strftime("%d/%m %H:%M")
        age_min = max(0.0, (time.time() - _csv_mtime) / 60.0)
        if age_min < 60:
            age_str = f"{int(age_min)} min"
        elif age_min < 1440:
            age_str = f"{age_min / 60:.1f} h"
        else:
            age_str = f"{age_min / 1440:.1f} j"
        cap_cols = st.columns([4, 1])
        with cap_cols[0]:
            scrape_active = _prematch_scrape_in_progress()
            badge = " · 🔄 mise à jour en cours…" if scrape_active else ""
            st.caption(
                f"Fichier prematch : `{os.path.basename(_csv_path)}` · indexé à {fresh} (il y a {age_str}){badge}"
            )
        with cap_cols[1]:
            disabled = _prematch_scrape_in_progress()
            label = "🔄 En cours…" if disabled else "🔄 Rafraîchir"
            help_text = (
                f"Auto-refresh : toutes les {PREMATCH_TTL_MIN} min "
                f"(BETTINGHUD_PREMATCH_TTL_MIN). Force un nouveau scrape Tennis Explorer."
            )
            if st.button(label, help=help_text, key="prematch_refresh_btn", disabled=disabled):
                if _start_prematch_scraper_subprocess():
                    st.session_state["_prematch_refresh_last_started"] = time.time()
                    st.toast("🔄 Scraping prematch lancé en arrière-plan…", icon="⏳")
                    st.rerun()
                else:
                    st.toast("Un scraping est déjà en cours.", icon="ℹ️")
    elif PREMATCH_AUTO_REFRESH:
        st.warning(
            "Aucun fichier prematch trouvé dans `data/scraped/`. "
            "Lancement du scraper en arrière-plan…"
        )
        _start_prematch_scraper_subprocess()

    st.markdown("---")

    if live_conf_filter != "Toutes":
        min_conf = {"Moyenne+": 0.25, "Forte+": 0.5, "Très forte": 0.75}[live_conf_filter]
        value_bets = [
            vb for vb in value_bets
            if float(vb["match"].get("confidence") or 0.0) >= min_conf
        ]

    # Options de tri
    col_sort1, col_sort2 = st.columns([1, 3])
    with col_sort1:
        sort_option = st.selectbox("Trier par :", ["Value EV (Plus haute)", "Value EV (Plus basse)", "Cote (Plus basse)"])
    
    if sort_option == "Value EV (Plus haute)":
        value_bets.sort(key=lambda x: x["val"]["value_pct"], reverse=True)
    elif sort_option == "Value EV (Plus basse)":
        value_bets.sort(key=lambda x: x["val"]["value_pct"])
    elif sort_option == "Cote (Plus basse)":
        value_bets.sort(key=lambda x: x["match"]["odd_p1"] if x["player"] == 1 else x["match"]["odd_p2"])

    bets_index = get_existing_bets_index()

    # Affichage des alertes de Value Bets
    st.header(f"🔥 Top Opportunités ({day_filter})")
    
    if len(value_bets) == 0:
        st.info("Aucun Value Bet détecté pour le moment. Essayez de changer de filtre ou attendez de nouveaux matchs.")
    else:
        for vb in value_bets:
            match = vb["match"]
            idx = vb["idx"]
            val = vb["val"]
            p_num = vb["player"]
            
            player_name = match['player1'] if p_num == 1 else match['player2']
            opp_name = match['player2'] if p_num == 1 else match['player1']
            odd_book = match['odd_p1'] if p_num == 1 else match['odd_p2']
            odd_true = match['true_odd_p1'] if p_num == 1 else match['true_odd_p2']
            match_name = f"{match['player1']} vs {match['player2']}"
            stake_already = float(bets_index.get((match_name, player_name), 0.0))
            
            with st.container(border=True):
                st.markdown(f"### 🎾 {player_name} <span style='font-size: 1rem; color: #aaaaaa;'>vs {opp_name}</span>", unsafe_allow_html=True)
                st.markdown(f"**Tournoi:** {match['tournament']} ({match['category']}) &nbsp;&nbsp;|&nbsp;&nbsp; **Statut:** {match['time']}")
                _ps1, _ps2 = match.get("p1_stats") or {}, match.get("p2_stats") or {}
                st.caption(_player_provenance_caption(match["player1"], _ps1, bool(match.get("p1_profile_loaded"))))
                st.caption(_player_provenance_caption(match["player2"], _ps2, bool(match.get("p2_profile_loaded"))))
                if stake_already > 0:
                    st.markdown(
                        f"<span class='bet-badge'>✅ Déjà parié : {stake_already:.2f} € sur {player_name}</span>",
                        unsafe_allow_html=True,
                    )

                mid = match.get("prematch_id") or f"row_{idx}"
                _odd_key = f"custom_odd_{mid}_p{p_num}"
                _default_odd_live = float(odd_book) if odd_book >= 1.01 else 1.01
                _oc1, _oc2 = st.columns([1.15, 2.85])
                with _oc1:
                    custom_odd = st.number_input(
                        "Cote réelle",
                        min_value=1.01,
                        max_value=100.0,
                        value=_default_odd_live,
                        step=0.05,
                        key=_odd_key,
                        help="Kelly, EV reco et pré-remplissage de la mise utilisent cette cote.",
                    )
                with _oc2:
                    st.caption("")
                    _book_vs = (
                        f"Réf. book agrégé : **{odd_book}** · "
                        if abs(float(custom_odd) - float(odd_book)) > 1e-3
                        else ""
                    )
                    st.caption(
                        f"{_book_vs}"
                        "L’espérance (EV) ci-contre suppose que vous passez votre pari à la cote saisie."
                    )

                _ev_live = detector.detect_value(
                    float(custom_odd), float(odd_true), confidence=None
                )
                ev_live_pct = float(_ev_live.get("value_pct") or 0.0)

                c1, c2, c3 = st.columns([1.5, 2, 1.5])
                with c1:
                    _ev_cls = "ev-highlight" if ev_live_pct >= -1e-9 else "ev-highlight-neg"
                    st.markdown(
                        f"<div style='margin-bottom: 10px;'><span class='{_ev_cls}'>{ev_live_pct:+.1f}% EV</span>"
                        "&nbsp;(à votre cote)</div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"EV liste value (book) : **{float(val['value_pct']):+.1f}%** — référence à la cote book."
                    )
                    st.markdown(f"Cote Bookmaker: <span class='odd-highlight'>{odd_book}</span>", unsafe_allow_html=True)
                    st.markdown(f"Cote Estimée: **{odd_true:.2f}**")
                    st.markdown(f"Probabilité: **{1/odd_true*100:.1f}%**")
                    # Kelly ¼ sur la **cote saisie**, plafond 10 % de la BR disponible.
                    p_model_side = min(1.0, max(0.0, 1.0 / float(odd_true))) if odd_true and odd_true > 0 else 0.0
                    b_side = max(0.01, float(custom_odd) - 1.0)
                    kelly_full = max(0.0, (b_side * p_model_side - (1.0 - p_model_side)) / b_side)
                    kelly_quarter = 0.25 * kelly_full
                    reco_frac = max(0.0, min(kelly_quarter, 0.10))
                    reco_pct = reco_frac * 100.0
                    reco_eur = br_avail * reco_frac
                    st.markdown(
                        f"**Mise reco (Kelly 1/4, cap 10 % BR disponible)** : **{reco_eur:.2f} €**"
                    )
                    st.caption(
                        f"{reco_pct:.2f}% de la BR restante (**{br_avail:.2f} €**)."
                    )
                    
                    with st.popover("ℹ️ Pourquoi cette value ?"):
                        p1_stats = match.get("p1_stats", {})
                        p2_stats = match.get("p2_stats", {})
                        comp_rows = _build_comparison_rows(
                            match, player_name, opp_name, p_num, _infobulle_hand_label
                        )
                        st.markdown(_infobulle_quick_summary(
                            player_name, float(odd_book), float(odd_true), val, match
                        ))
                        with st.expander(
                            "📊 Comparatif modèle · forme · H2H · signaux & micro-stats",
                            expanded=True,
                        ):
                            if str(match.get("tour", "")).upper() == "WTA":
                                st.caption(
                                    "WTA : retour 60 % / service 40 % ; K +15 % sur M1000 ; résilience break-points."
                                )
                            if comp_rows:
                                comp_df = pd.DataFrame(
                                    comp_rows,
                                    columns=["Indicateur", player_name, opp_name, "Avantage"],
                                )
                                try:
                                    st.dataframe(
                                        _style_comparison_popover_df(comp_df),
                                        hide_index=True,
                                        use_container_width=True,
                                        height=320,
                                    )
                                except Exception:
                                    st.dataframe(
                                        comp_df,
                                        hide_index=True,
                                        use_container_width=True,
                                        height=320,
                                    )
                            else:
                                st.caption("—")
                            st.markdown("###### Forme, H2H & contexte")
                            st.dataframe(
                                _infobulle_dynamics_df(match, player_name, opp_name, p_num),
                                hide_index=True,
                                use_container_width=True,
                                height=_INFOBULLE_DF_HEIGHT,
                            )
                            st.markdown("###### Signaux avancés & micro-stats")
                            st.dataframe(
                                _infobulle_advanced_df(match),
                                hide_index=True,
                                use_container_width=True,
                                height=_INFOBULLE_DF_HEIGHT,
                            )
                        st.markdown("##### Facteurs déterminants")
                        st.markdown(
                            _infobulle_analysis_markdown(
                                match,
                                player_name,
                                opp_name,
                                float(odd_book),
                                float(odd_true),
                                val,
                                skip_edge_paragraph=True,
                            )
                        )
                        st.caption("▼ Autres détails (repliables)")
                        with st.expander("Détails : cotes & calibration", expanded=False):
                            st.dataframe(
                                _infobulle_value_context_df(val, float(odd_book), float(odd_true), match),
                                hide_index=True,
                                use_container_width=True,
                                height=_INFOBULLE_DF_HEIGHT,
                            )
                        with st.expander("Sources données & fraîcheur", expanded=False):
                            st.dataframe(
                                _infobulle_provenance_df(match),
                                hide_index=True,
                                use_container_width=True,
                                height=160,
                            )
                            _det_rows = []
                            for _lab, _stx in (
                                (match["player1"], p1_stats),
                                (match["player2"], p2_stats),
                            ):
                                _d = (_stx or {}).get("stats_source_detail")
                                if _d and str((_stx or {}).get("stats_source", "")).lower() == "no_ranking_source":
                                    _det_rows.append([_lab, _d])
                            if _det_rows:
                                st.caption("Manque de données rang/points")
                                st.dataframe(
                                    pd.DataFrame(_det_rows, columns=["Joueur", "Détail"]),
                                    hide_index=True,
                                    use_container_width=True,
                                )
                            st.caption(
                                "Réf. date = contexte du rang. « Phase au dernier tournoi » dans le comparatif ≠ rang ATP/WTA."
                            )
                        with st.expander("Audit probas & facteurs ML", expanded=False):
                            _audit_df = _infobulle_model_audit_df(match)
                            if _audit_df is not None:
                                st.dataframe(
                                    _audit_df, hide_index=True, use_container_width=True
                                )
                            else:
                                st.caption("Pas d’audit probas dans ce snapshot.")
                            _tf_df = _infobulle_top_features_df(match)
                            if _tf_df is not None:
                                st.dataframe(
                                    _tf_df,
                                    hide_index=True,
                                    use_container_width=True,
                                    height=260,
                                )
                            else:
                                st.caption("Pas de facteurs locaux listés.")
                with c2:
                    st.markdown("**📝 Placer un pari virtuel**")
                    default_stake = max(
                        0.01,
                        round(
                            min(
                                reco_eur if reco_eur > 0 else 0.5,
                                max(float(br_avail), 0.01),
                            ),
                            2,
                        ),
                    )
                    _max_stake = max(0.01, float(br_avail))
                    _stake_key = (
                        f"stake_eur_{mid}_p{p_num}_{_live_br_stake_sig}|odd{round(custom_odd, 4)}"
                    )
                    stake_eur = st.number_input(
                        "Mise (€)",
                        min_value=0.01,
                        max_value=_max_stake,
                        value=min(float(default_stake), _max_stake),
                        step=0.5,
                        key=_stake_key,
                        help="Pré-rempli avec la reco Kelly (1/4, cap 10 %) sur la BR **disponible**, "
                        "**selon votre cote réelle**. Remis à jour quand la dispo ou cette cote changent.",
                    )
                    st.caption(
                        "S’aligne sur la reco quand la BR disponible ou la « Cote réelle » changent."
                    )

                with c3:
                    st.markdown("<br><br>", unsafe_allow_html=True)
                    _avail_ok = br_avail >= float(stake_eur) - 1e-6
                    if st.button(
                        f"Parier {float(stake_eur):.2f} € sur {player_name}",
                        key=f"bet_{mid}_p{p_num}",
                        disabled=(not _avail_ok or br_avail < 0.01),
                    ):
                        if not _avail_ok:
                            st.error("Mise supérieure à la bankroll disponible.")
                        else:
                            save_bet(
                                match_name,
                                player_name,
                                custom_odd,
                                float(stake_eur),
                                tour=str(match.get("tour") or match.get("category") or "").upper() or None,
                                surface=match.get("surface"),
                                tournament=match.get("tournament"),
                                match_id=str(mid) if mid else None,
                                p_model=(1.0 / float(odd_true)) if odd_true and odd_true > 0 else None,
                                ev_at_bet=(ev_live_pct / 100.0),
                                bookmaker_source=(
                                    "custom" if abs(float(custom_odd) - float(odd_book)) > 1e-3 else "book_avg"
                                ),
                                notes="paliers_live_kelly_1_4",
                                tracker_source="live_tracker",
                            )
                            st.rerun()

    # Affichage de tous les matchs trackés dans un expander
    with st.expander("📋 Voir tous les matchs trackés (Détails)"):
        df_display = []
        for m in filtered_matches:
            p1_val = detector.detect_value(m["odd_p1"], m["true_odd_p1"], confidence=m.get("confidence"))
            p2_val = detector.detect_value(m["odd_p2"], m["true_odd_p2"], confidence=m.get("confidence"))
            
            df_display.append({
                "Catégorie": m.get("category", "ATP/WTA"),
                "Tournoi": m["tournament"],
                "Statut": m["time"],
                "Joueur 1": m["player1"],
                "Rang P1": m["p1_stats"].get("rank"),
                "Fatigue P1": m["p1_fatigue"].get("minutes_played", 0),
                "Cote P1": m["odd_p1"],
                "True P1": round(m["true_odd_p1"], 2),
                "EV P1 (%)": f"{p1_val['value_pct']:.1f}%",
                "Joueur 2": m["player2"],
                "Rang P2": m["p2_stats"].get("rank"),
                "Fatigue P2": m["p2_fatigue"].get("minutes_played", 0),
                "Cote P2": m["odd_p2"],
                "True P2": round(m["true_odd_p2"], 2),
                "EV P2 (%)": f"{p2_val['value_pct']:.1f}%",
            })

        df = pd.DataFrame(df_display)

        # Mise en forme (couleurs)
        def color_ev(val):
            if isinstance(val, str) and '%' in val:
                num = float(val.replace('%', ''))
                color = 'lightgreen' if num > 3.0 else 'lightcoral' if num < -3.0 else 'white'
                return f'background-color: {color}; color: black'
            return ''

        if not df.empty:
            st.dataframe(df.style.map(color_ev, subset=['EV P1 (%)', 'EV P2 (%)']), use_container_width=True)
        else:
            st.info("Aucun match avec des cotes valides n'a été trouvé.")

with tab2:
    st.header("📈 Backtest (no-leak) — projection Kelly ATP+WTA")
    st.markdown(
        """
Génère d’abord un fichier de paris avec **`python scripts/backtest_2026.py`** (entraînement **avant** le 1er janvier
de l’année cible, cotes tennis-data, filtre EV au choix). Ce panneau rejoue ensuite une **bankroll** avec **Kelly**
et **liquidité intra-jour** pour le **Kelly** (part de Kelly × **liquide restant** après chaque mise ;
le **plafond** reste « X % **de la BR du matin** par pari »). Les PnL s’appliquent en fin de journée.
        """
    )

    st.markdown(
        """
### Méthodologie — bankroll & Kelly

- **Source** : `data/backtest_<ANNÉE>_bets*.csv` (sortie typique de `backtest_2026.py`).
- **Ordre intraday** : tri par date, ordre des lignes inchangé à l’intérieur d’un jour.
- **Liquide du jour** : au matin, `liquide =` bankroll de clôture veille ; chaque mise retire son montant du liquide **avant** la suivante.
- **Kelly** : mise brute = `Kelly_mult × f* × liquide` (`f*` depuis `p_model` et cote).
- **Plafond par pari** : au plus `max% × BR du matin` pour **chaque** pari (référence fixe la journée), tout en restant borné par le **liquide** restant.
- **Soir** : `BR = BR_matin + Σ PnL` du jour.

**Courbe trop vertigineuse (ex. millions avec 100 €)** : ce n’est en général **pas une bug de BR**, mais un **scenario agressif** — filtre EV élevé (ex. ≥ 8 %) sur l’historique, **Kelly + réinvestissement complet** tous les gains, jusqu’à **100 % de la BR du matin** cumulées en mises sur une même journée (beaucoup de paris ⇒ capital presque entièrement « sorti » chaque jour). La réalité bookmaker impose plafonds, dispersion, fermetures. Utilise **`Budget total misé max / jour`** (voir ci‑dessous) pour retrouver quelque chose de lisible — typ. **25–45 %** de la BR du matin.

### Sans triche (no-leak)

- Aucun ré-entraînement ici : on réutilise les **`p_model`** du CSV. Un fichier non produit par `backtest_2026.py` peut **fausser** l’interprétation.
        """
    )
    k_no_leak_ok = st.checkbox(
        "Confirmer : CSV issu du backtest **no-leak** (`scripts/backtest_2026.py`).",
        value=True,
        key="kcsv_no_leak_confirm",
    )
    k_fev = st.checkbox(
        "Filtrer par EV min (colonne `ev` du CSV requise)",
        value=True,
        key="kcsv_use_ev_filter",
    )
    k_row1 = st.columns([1, 1, 1])
    with k_row1[0]:
        k_year = st.number_input("Année cible", min_value=2015, max_value=2035, value=2025, step=1, key="kcsv_year")
    with k_row1[1]:
        k_ev_min = st.number_input(
            "EV min (%)",
            min_value=0.0,
            max_value=100.0,
            value=8.0,
            step=0.5,
            key="kcsv_ev_min",
            disabled=not k_fev,
            help="Conserve uniquement les paris avec ev ≥ CE seuil (colonne décimale du CSV).",
        )
    with k_row1[2]:
        k_br0 = st.number_input(
            "BR début (€)",
            min_value=1.0,
            max_value=500000.0,
            value=100.0,
            step=10.0,
            key="kcsv_br0",
        )
    k_row2 = st.columns([1, 1, 2])
    with k_row2[0]:
        k_frac_label = st.selectbox(
            "Kelly",
            options=["Kelly ¼", "Kelly ½", "Kelly plein"],
            index=0,
            key="kcsv_kmult",
            help="Multiplicateur sur la fraction Kelly pleine.",
        )
        k_frac_map = {"Kelly ¼": 0.25, "Kelly ½": 0.5, "Kelly plein": 1.0}
        k_mult = float(k_frac_map[k_frac_label])
    with k_row2[1]:
        k_cap = st.slider(
            "Mise max (% BR du matin / pari)",
            min_value=0.5,
            max_value=50.0,
            value=10.0,
            step=0.5,
            key="kcsv_cap_pct",
        )
    k_row2b = st.columns([1, 1, 2])
    with k_row2b[0]:
        k_day_budget = st.slider(
            "Budget total misé max / jour (% BR du matin)",
            min_value=10.0,
            max_value=100.0,
            value=100.0,
            step=5.0,
            key="kcsv_day_budget_pct",
            help="Σ des mises sur la journée plafonné à ce % de la BR du matin. "
            "< 100 % désactive la mise « jusqu’à avoir tout mobilisé » et freine une croissance excessive en réinvestissement brutal.",
        )
    with k_row2b[1]:
        st.markdown("")  # alignement avec colonnes voisines
    with k_row2b[2]:
        k_csv_ov = st.text_input(
            "Chemin CSV (vide = auto depuis `data/`)",
            value="",
            key="kcsv_path_ov",
            help="Relatif à la racine du projet ; sinon recherche `backtest_<année>_bets*.csv`.",
        )

    def _csv_path_pick_bt() -> str | None:
        if k_csv_ov.strip():
            p = os.path.join(_PROJECT_ROOT, k_csv_ov.strip().replace("/", os.sep))
            return p if os.path.isfile(p) else None
        return resolve_backtest_csv(_PROJECT_ROOT, int(k_year))

    st.markdown("### Lancer la projection")
    if st.button("Calculer projection Kelly intra-jour", key="kcsv_run"):
        if not k_no_leak_ok:
            st.error(
                "Coche la confirmation **no-leak** pour lancer, ou régénère un CSV avec "
                "`python scripts/backtest_2026.py --year <année> --out data/backtest_<année>_bets.csv`."
            )
        else:
            pth = _csv_path_pick_bt()
            if not pth:
                st.error(
                    "Fichier introuvable. Exemple : "
                    "`python scripts/backtest_2026.py --year <année> --ev-min … --out data/backtest_<année>_bets.csv`."
                )
            else:
                try:
                    kw_f = dict(year=int(k_year))
                    if k_fev:
                        kw_f["ev_min_pct"] = float(k_ev_min)
                    df_bt = load_and_filter_bets_csv(pth, **kw_f)
                    if df_bt.empty:
                        st.warning("Aucun pari après filtres année / EV.")
                    else:
                        res = simulate_sequential_intraday(
                            df_bt,
                            bankroll_start=float(k_br0),
                            kelly_multiplier=k_mult,
                            max_stake_pct=float(k_cap),
                            daily_stake_budget_pct=float(k_day_budget),
                            return_history=True,
                        )
                        st.success(
                            f"**{len(df_bt)}** paris depuis `{os.path.relpath(pth, _PROJECT_ROOT)}`."
                        )
                        st.info(
                            "Hypothèse **no-leak** : `p_model` produites avec entraînement **avant** cutoff année "
                            "(voir `backtest_2026.py`)."
                        )
                        if (
                            float(k_br0) > 0
                            and float(k_day_budget) >= 99.9
                            and res["bankroll_final"] > float(k_br0) * 200
                        ):
                            st.warning(
                                "BR finale très grande par rapport au départ : avec **budget jour = 100 %**, chaque jour "
                                "tu peux engager jusqu’à **toute** la bankroll cumulée en mises, puis recomposer après les "
                                "résultats — combiné avec un filtre EV haut et un historique favorable, ça peut donner une "
                                "courbe en **fusée** très éloignée du terrain (limites books, dispersion). "
                                "**Baisse « Budget total misé max / jour »** vers **25–45 %** pour un scénario plus réaliste."
                            )
                        mcols = st.columns(4)
                        mcols[0].metric("PnL €", f"{res['net_profit_eur']:+,.2f}")
                        mcols[1].metric("BR finale €", f"{res['bankroll_final']:,.2f}")
                        mcols[2].metric("Max drawdown %", f"{res['max_drawdown_pct']:.2f}")
                        mcols[3].metric("ROI sur volume %", f"{res['roi_on_staked_pct']:+.2f}")
                        m2 = st.columns(4)
                        m2[0].metric("Paris", f"{res['n_bets']}")
                        m2[1].metric("Win rate %", f"{res['win_rate_pct']:.1f}")
                        m2[2].metric("Volume misé €", f"{res['total_staked_eur']:,.2f}")
                        m2[3].metric("Mise max en 1 jour €", f"{res['max_daily_stake_eur']:,.2f}")
                        m3 = st.columns(4)
                        m3[0].metric("Meilleur jour €", f"{res['best_day_eur']:+,.2f}")
                        m3[1].metric("Pire jour €", f"{res['worst_day_eur']:+,.2f}")
                        m3[2].metric("PnL moy. / jour actif", f"{res['avg_daily_pnl_eur']:+,.2f}")
                        _pf = res.get("profit_factor")
                        m3[3].metric("Profit factor", "∞" if _pf is None else f"{_pf:.2f}")
                        st.caption(
                            f"Journées avec ligne(s) CSV : **{res['n_trading_days']}** · "
                            f"Sharpe (PnL j.) : **{res['sharpe_daily']:.2f}** · "
                            f"Pic BR : **{res['peak_bankroll']:,.2f} €**"
                        )
                        hist = res.get("history") or []
                        if hist:
                            hdf = pd.DataFrame(hist)
                            hdf["date"] = pd.to_datetime(hdf["date"])
                            ch = hdf.set_index("date")[["bankroll", "pnl_cum_eur"]]
                            st.subheader("Courbe bankroll (clôture jour)")
                            st.line_chart(ch[["bankroll"]])
                            st.subheader("PnL cumulé")
                            st.line_chart(ch[["pnl_cum_eur"]])
                except Exception as e:
                    st.exception(e)

    st.markdown("### Rappels opérationnels")
    st.markdown(
        """
        - Filtres EV courants en prod : privilégier **≥ 8–10 %** selon niveau tournoi.
        - Jugement sur **séries longues** (centaines de paris), pas quelques journées.
        """
    )

with tab5:
    st.header("🧩 Projet annexe: Marchés alternatifs (ATP/WTA)")
    st.caption("Expérimentation hors backtest: value bets sur marchés non-vainqueur (jeux, tie-break, sets).")

    _csv_path_alt, _csv_mtime_alt = _prematch_csv_signature()
    alt_matches = get_latest_scraped_data(
        _csv_path_alt,
        _csv_mtime_alt,
        PROFILE_CACHE_SCHEMA,
        _ml_model_mtime(),
        _ENGINES_CACHE_VERSION,
    )
    alt_matches = [m for m in alt_matches if m.get("odd_p1", 0) > 1.0 and m.get("odd_p2", 0) > 1.0]
    alt_matches = [
        m for m in alt_matches
        if _is_major_atp_wta(m.get("category"), m.get("tournament"))
    ]

    current_time_alt = datetime.now().time()
    def _is_future_match_alt(time_str):
        if str(time_str).startswith("Demain"):
            return True
        try:
            match_time = datetime.strptime(str(time_str).strip(), "%H:%M").time()
            return match_time >= current_time_alt
        except ValueError:
            return True

    alt_matches = [m for m in alt_matches if _is_future_match_alt(m.get("time"))]

    alt_day_filter = st.radio(
        "📅 Journée (annexe)",
        ["Aujourd'hui", "Demain", "Tous"],
        horizontal=True,
        key="alt_day_filter",
    )
    if alt_day_filter == "Aujourd'hui":
        alt_matches = [m for m in alt_matches if not str(m.get("time", "")).startswith("Demain")]
    elif alt_day_filter == "Demain":
        alt_matches = [m for m in alt_matches if str(m.get("time", "")).startswith("Demain")]

    alt_tournaments = ["Tous"] + sorted(list(set([m.get("tournament", "") for m in alt_matches if m.get("tournament")])))
    alt_tourney_filter = st.selectbox("🏆 Tournoi (annexe)", options=alt_tournaments, key="alt_tourney_filter")
    if alt_tourney_filter != "Tous":
        alt_matches = [m for m in alt_matches if m.get("tournament") == alt_tourney_filter]

    alt_ev = st.slider(
        "Seuil EV minimum (Annexe, %)",
        min_value=1.0,
        max_value=20.0,
        value=6.0,
        step=0.5,
        key="alt_ev_threshold",
    )
    alt_markets_enabled = st.multiselect(
        "Marchés activés",
        options=["Total jeux", "Tie-break", "Nombre de sets"],
        default=["Total jeux", "Tie-break", "Nombre de sets"],
        key="alt_markets_enabled",
    )
    alt_detector = ValueDetector(min_value_threshold=float(alt_ev) / 100.0)

    alt_rows = []
    for m in alt_matches[:60]:
        mkts = _build_alternative_markets(m, alt_detector)
        for mk in mkts:
            if alt_markets_enabled and mk.get("market") not in alt_markets_enabled:
                continue
            alt_rows.append({
                "match": m,
                "market": mk,
            })

    alt_rows.sort(key=lambda x: x["market"]["value_pct"], reverse=True)

    st.metric("🧪 Opportunités alternatives", len(alt_rows))
    if not alt_rows:
        st.info("Aucune value détectée sur marchés alternatifs avec ce seuil EV.")
    else:
        for i, row in enumerate(alt_rows[:40]):
            m = row["match"]
            mk = row["market"]
            match_name = f"{m['player1']} vs {m['player2']}"
            bet_label = f"{mk['selection']} ({match_name})"
            with st.container(border=True):
                st.markdown(
                    f"### 🎾 {m['player1']} vs {m['player2']} "
                    f"<span style='font-size: 1rem; color: #aaaaaa;'>· {mk['market']}</span>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"**Tournoi:** {m.get('tournament')} ({m.get('category')}) &nbsp;&nbsp;|&nbsp;&nbsp; "
                    f"**Marché:** {mk['selection']}"
                )
                st.markdown(
                    f"<span class='ev-highlight'>+ {mk['value_pct']:.1f}% EV</span> &nbsp;&nbsp; "
                    f"Cote Book: <span class='odd-highlight'>{mk['book_odd']:.2f}</span> &nbsp;&nbsp; "
                    f"Cote Estimée: **{mk['true_odd']:.2f}**",
                    unsafe_allow_html=True,
                )
                with st.popover("ℹ️ Pourquoi cette value (annexe) ?"):
                    st.markdown("### Pourquoi ce pari alternatif ?")
                    for reason in mk.get("why", []):
                        st.markdown(f"- {reason}")
                    st.markdown("---")
                    st.markdown(
                        f"- **EV**: +{mk['value_pct']:.1f}%  "
                        f"(book {mk['book_odd']:.2f} vs juste {mk['true_odd']:.2f})"
                    )
                    st.markdown(f"- **Calibration vainqueur utilisée**: {m.get('calibration_used', 'Globale')}")
                c1, c2 = st.columns([1.5, 1])
                with c1:
                    stake_alt = st.number_input(
                        "Mise (U)",
                        min_value=0.1,
                        max_value=100.0,
                        value=1.0,
                        step=0.5,
                        key=f"alt_stake_{i}",
                    )
                with c2:
                    st.markdown("<br>", unsafe_allow_html=True)
                    if st.button(f"Parier {stake_alt} U (annexe)", key=f"alt_btn_{i}"):
                        save_bet(
                            match_name,
                            bet_label,
                            float(mk["book_odd"]),
                            float(stake_alt),
                            tour=str(m.get("tour") or m.get("category") or "").upper() or None,
                            surface=m.get("surface"),
                            tournament=m.get("tournament"),
                            match_id=str(m.get("prematch_id")) if m.get("prematch_id") else None,
                            p_model=(1.0 / float(mk["true_odd"])) if mk.get("true_odd") else None,
                            ev_at_bet=float(mk.get("value_pct", 0.0)) / 100.0,
                            bookmaker_source="book_avg",
                            notes=f"market={mk.get('selection')}",
                        )

with tab3:
    st.header("💼 Mon Portefeuille de Paris")
    st.markdown("Suivez vos performances, filtrez vos paris et visualisez rapidement votre risque/rendement.")
    
    conn = sqlite3.connect('data/bettinghud.db')
    df_bets = pd.read_sql("SELECT * FROM user_bets ORDER BY id ASC", conn)
    conn.close()

    # Auto-update des résultats (opt-in via env) avec anti-spam.
    # Exécution en arrière-plan pour ne pas bloquer le rendu.
    auto_now = time.time()
    last_auto = st.session_state.get("portfolio_last_auto_results_ts", 0.0)
    auto_interval_sec = PORTFOLIO_AUTO_RESULTS_INTERVAL_SEC
    has_pending = not df_bets.empty and (df_bets["status"] == "En cours").any()
    if ENABLE_PORTFOLIO_AUTO_RESULTS and has_pending:
        # Rerun périodique pour résoudre les paris sans action utilisateur.
        st_autorefresh(interval=60 * 1000, key="portfolio_results_autorefresh")
    if ENABLE_PORTFOLIO_AUTO_RESULTS and has_pending and (auto_now - float(last_auto) >= auto_interval_sec):
        st.session_state["portfolio_last_auto_results_ts"] = auto_now
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scripts.scraper_results",
                ],
                cwd=os.getcwd(),
            )
            st.caption("Auto-résultats lancés en arrière-plan (rafraîchissement auto actif).")
        except Exception as e:
            st.caption(f"Auto-résultats au chargement: erreur ({e})")
    elif has_pending and not ENABLE_PORTFOLIO_AUTO_RESULTS:
        st.caption("Auto-résultats au chargement désactivé (mode performance). Utilise le bouton de MAJ manuelle.")
    
    if df_bets.empty:
        st.info("Vous n'avez pas encore enregistré de paris. Allez dans l'onglet 'Live Tracker' pour trouver de la Value !")
    else:
        # Base temporelle propre pour un graphe lisible
        df_bets["date_dt"] = pd.to_datetime(df_bets["date"], errors="coerce")
        if df_bets["date_dt"].isna().all():
            df_bets["date_dt"] = pd.Timestamp.today().normalize()
        else:
            df_bets["date_dt"] = df_bets["date_dt"].fillna(pd.Timestamp.today().normalize())
        df_bets = df_bets.sort_values(["date_dt", "id"]).reset_index(drop=True)

        if "tracker_source" in df_bets.columns:
            df_live_pf = df_bets[df_bets["tracker_source"].fillna("") == "live_tracker"].copy()
        else:
            df_live_pf = df_bets.iloc[0:0].copy()

        if not df_live_pf.empty:
            snap_pf = live_tracker_bankroll_snapshot()
            lt_open = df_live_pf[df_live_pf["status"] == "En cours"]
            lt_closed = df_live_pf[df_live_pf["status"] != "En cours"]
            st.markdown("### 📟 Live Tracker — bankroll et performance")
            st.caption(
                "Mode simplifié: BR Kelly calculée sur tous les paris (live + legacy). "
                "La BR disponible se met à jour quand les paris passent Gagné / Perdu / Annulé."
            )
            lx1, lx2, lx3, lx4, lx5 = st.columns(5)
            lx1.metric("Paris live", len(df_live_pf))
            lx2.metric("En cours", len(lt_open))
            lx3.metric("BR dispo (€)", f"{snap_pf['available_eur']:.2f}")
            lx4.metric("Engagé (€)", f"{snap_pf['committed_open_eur']:.2f}")
            lx5.metric("Capital total (€)", f"{snap_pf['equity_eur']:.2f}")
            st_sub = float(lt_closed["stake"].sum()) if not lt_closed.empty else 0.0
            pl_sub = float(lt_closed["profit"].sum()) if not lt_closed.empty else 0.0
            wr_lt = (
                len(lt_closed[lt_closed["status"] == "Gagné"]) / len(lt_closed) * 100.0
                if len(lt_closed)
                else 0.0
            )
            roi_lt = (pl_sub / st_sub * 100.0) if st_sub > 0 else 0.0
            ly1, ly2, ly3 = st.columns(3)
            ly1.metric("Profit net (clos, €)", f"{pl_sub:.2f}")
            ly2.metric("ROI sur mises clôturées", f"{roi_lt:.1f}%")
            ly3.metric("Winrate clos", f"{wr_lt:.1f}%")

        df_bets["cumulative_profit"] = df_bets["profit"].cumsum()
        
        # Statistiques
        total_profit = df_bets['profit'].sum()
        total_staked = df_bets['stake'].sum()
        roi = (total_profit / total_staked * 100) if total_staked > 0 else 0
        
        # Filtrer et compter les statuts
        nb_en_cours = len(df_bets[df_bets['status'] == 'En cours'])
        nb_gagnes = len(df_bets[df_bets['status'] == 'Gagné'])
        nb_perdus = len(df_bets[df_bets['status'] == 'Perdu'])
        nb_annule = len(df_bets[df_bets['status'] == 'Annulé'])
        nb_closed = nb_gagnes + nb_perdus
        winrate_closed = (nb_gagnes / nb_closed * 100.0) if nb_closed > 0 else 0.0
        avg_odd = float(df_bets["odds"].mean()) if len(df_bets) > 0 else 0.0

        # Courbe quotidienne (plus lisible que pari par pari) + drawdown
        daily_curve = (
            df_bets.groupby("date_dt", as_index=False)
            .agg(
                daily_profit=("profit", "sum"),
                daily_stake=("stake", "sum"),
                n_bets=("id", "count"),
            )
            .sort_values("date_dt")
        )
        daily_curve["cumulative_profit"] = daily_curve["daily_profit"].cumsum()
        daily_curve["peak_profit"] = daily_curve["cumulative_profit"].cummax()
        denom = daily_curve["peak_profit"].replace(0, np.nan).abs()
        daily_curve["drawdown_pct"] = ((daily_curve["peak_profit"] - daily_curve["cumulative_profit"]) / denom * 100.0).fillna(0.0)
        max_drawdown = float(daily_curve["drawdown_pct"].max()) if not daily_curve.empty else 0.0
        
        k1, k2, k3, k4, k5, k6, k7 = st.columns(7)
        k1.metric("Paris totaux", len(df_bets))
        k2.metric("En cours", nb_en_cours)
        k3.metric("Gagnés", nb_gagnes)
        k4.metric("Perdus", nb_perdus)
        k5.metric("Annulés", nb_annule, help="Walkover / abandon : la mise est remboursée")
        k6.metric("Profit net", f"{total_profit:.2f} U", f"{roi:.1f}% ROI")
        k7.metric("Winrate (clos)", f"{winrate_closed:.1f}%")

        c1, c2 = st.columns([3, 1])
        with c1:
            st.subheader("📈 Évolution du profit cumulé")
            chart_df = daily_curve.set_index("date_dt")[["cumulative_profit"]]
            st.line_chart(chart_df, use_container_width=True)
            st.caption(
                f"Courbe agrégée par jour · Drawdown max: {max_drawdown:.1f}% · "
                f"Cote moyenne: {avg_odd:.2f}"
            )
            dd_df = daily_curve.set_index("date_dt")[["drawdown_pct"]]
            st.area_chart(dd_df, use_container_width=True)
        with c2:
            st.subheader("⚙️ Actions")
            st.metric("Mises totales", f"{total_staked:.1f} U")
            if ENABLE_PORTFOLIO_AUTO_RESULTS:
                st.caption("🔄 Mise à jour des résultats en mode automatique.")
            else:
                if st.button("🔄 Mettre à jour les résultats"):
                    with st.spinner("Recherche des résultats (TE + Sackmann sur 7 jours)..."):
                        res_scraper = ResultsScraper()
                        try:
                            updated = asyncio.run(res_scraper.update_pending_bets())
                            st.success(f"{updated} pari(s) mis à jour")
                        except Exception as exc:
                            st.error(f"Échec de la MAJ : {exc}. Voir data/logs/scraper_results.log")
                    st.rerun()

            # Reconciliation (palier 2.3) — manual + weekly auto-trigger
            from scripts.reconcile_bets import (
                reconcile,
                days_since_last_reconciliation,
                is_reconciliation_due,
                RECONCILE_INTERVAL_DAYS,
            )
            d_since = days_since_last_reconciliation()
            if d_since is None:
                recon_caption = "🔍 Réconciliation jamais exécutée"
            else:
                recon_caption = f"🔍 Dernière réconciliation : il y a {d_since:.1f} j"
            st.caption(recon_caption)
            recon_btn_label = "🔍 Réconciliation 3 sources (7j)"
            if is_reconciliation_due():
                recon_btn_label += " ⏰"
            if st.button(recon_btn_label, help="Croise Tennis Explorer + Sackmann + tennis-data sur les 7 derniers jours et corrige les divergences."):
                with st.spinner("Réconciliation en cours (TE + Sackmann + tennis-data)..."):
                    try:
                        summary = asyncio.run(reconcile(window_days=RECONCILE_INTERVAL_DAYS))
                        st.success(
                            f"✅ {summary['checked']} paris vérifiés · "
                            f"{summary['pending_resolved']} résolus · "
                            f"{summary['flipped']} re-statués"
                        )
                    except Exception as exc:
                        st.error(f"Échec réconciliation : {exc}")
                st.rerun()

            # Auto-trigger réconciliation hebdo (1 fois par session, non-bloquant)
            if (
                ENABLE_PORTFOLIO_AUTO_RESULTS
                and is_reconciliation_due()
                and not st.session_state.get("_recon_auto_triggered", False)
            ):
                st.session_state["_recon_auto_triggered"] = True
                try:
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "scripts.reconcile_bets",
                            "--no-te-refresh",
                        ],
                        cwd=os.getcwd(),
                    )
                    st.caption("⏰ Réconciliation hebdo lancée en arrière-plan…")
                except Exception:
                    pass

            log_path = os.path.join("data", "logs", "scraper_results.log")
            if os.path.exists(log_path):
                try:
                    log_size = os.path.getsize(log_path)
                    log_mtime = datetime.fromtimestamp(os.path.getmtime(log_path)).strftime("%H:%M")
                    st.caption(f"📜 Log scraper · {log_mtime} · {log_size//1024} KB")
                except Exception:
                    pass
        
        st.markdown("---")

        # Renverser l'ordre pour afficher les plus récents en premier
        df_bets_disp = df_bets.sort_values(by="id", ascending=False)

        f1, f2, f3 = st.columns(3)
        with f1:
            status_filter = st.selectbox("Filtre statut", ["Tous", "En cours", "Gagné", "Perdu", "Annulé"], key="pf_status")
        with f2:
            min_stake = st.number_input("Mise min", min_value=0.0, value=0.0, step=0.5, key="pf_min_stake")
        with f3:
            sort_choice = st.selectbox("Tri", ["Plus récents", "Plus anciens", "Profit décroissant", "Profit croissant"], key="pf_sort")

        df_view = df_bets_disp.copy()
        if status_filter != "Tous":
            df_view = df_view[df_view["status"] == status_filter]
        df_view = df_view[df_view["stake"] >= float(min_stake)]
        if sort_choice == "Plus anciens":
            df_view = df_view.sort_values(by="id", ascending=True)
        elif sort_choice == "Profit décroissant":
            df_view = df_view.sort_values(by="profit", ascending=False)
        elif sort_choice == "Profit croissant":
            df_view = df_view.sort_values(by="profit", ascending=True)

        pending_bets = df_view[df_view['status'] == 'En cours']
        if not pending_bets.empty:
            st.subheader("⏳ Paris en attente (saisie manuelle possible)")
            for _, row in pending_bets.iterrows():
                with st.expander(f"🎾 {row['match_name']} · {row['bet_on']} @ {row['odds']}"):
                    col_w, col_l = st.columns(2)
                    if col_w.button("✅ Gagné", key=f"win_{row['id']}"):
                        update_bet_status(row['id'], 'Gagné', (row['odds'] - 1) * row['stake'])
                        st.rerun()
                    if col_l.button("❌ Perdu", key=f"lose_{row['id']}"):
                        update_bet_status(row['id'], 'Perdu', -row['stake'])
                        st.rerun()

        st.subheader("📋 Historique des paris")
        
        def color_status(val):
            if val == 'Gagné': return 'background-color: lightgreen; color: black'
            elif val == 'Perdu': return 'background-color: lightcoral; color: black'
            elif val == 'En cours': return 'background-color: lightblue; color: black'
            return ''
            
        st.dataframe(
            df_view[['date', 'match_name', 'bet_on', 'odds', 'stake', 'status', 'profit']].style.map(color_status, subset=['status']),
            use_container_width=True
        )

with tab4:
    st.header("🧪 Diagnostics Modèle")
    st.markdown("Calibration des probabilités, ROI par niveau de confiance, et métriques globales (source TennisMyLife / `matches_recent`).")

    c1, c2, c3 = st.columns(3)
    with c1:
        diag_years = st.slider("Période diagnostics", min_value=2010, max_value=2026, value=(2018, 2026), step=1, key="diag_years")
    with c2:
        diag_n = st.number_input("Taille échantillon", min_value=500, max_value=50000, value=5000, step=500, key="diag_n")
    with c3:
        diag_seed = st.number_input("Seed diagnostics", min_value=1, max_value=999999, value=42, step=1, key="diag_seed")

    if st.button("Lancer diagnostics"):
        with st.spinner("Calcul des diagnostics en cours..."):
            diag = compute_model_diagnostics(diag_years[0], diag_years[1], int(diag_n), int(diag_seed))

        if not diag:
            st.warning("Aucune donnée disponible pour ces paramètres.")
        else:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Matchs évalués", diag["n_matches"])
            m2.metric("Accuracy", f"{diag['accuracy']*100:.1f}%")
            m3.metric("Brier", f"{diag['brier']:.4f}")
            m4.metric("Confiance moyenne", f"{diag['mean_confidence']*100:.1f}%")
            m5.metric("ROI value (simulé)", f"{diag['total_roi']:.1f}%", f"{diag['total_bets']} paris")

            st.markdown("---")
            st.subheader("Calibration (proba prédite vs fréquence observée)")
            calib = diag["calibration"].copy()
            if not calib.empty:
                chart = calib[["mean_prob", "observed_win_rate"]].set_index("mean_prob")
                st.line_chart(chart, use_container_width=True)
                st.dataframe(calib, use_container_width=True)
            else:
                st.info("Pas assez de points pour la calibration.")

            st.subheader("ROI par bucket de confiance")
            roi_conf = diag["roi_by_conf"].copy()
            if not roi_conf.empty:
                roi_plot = roi_conf.copy()
                roi_plot["roi_pct"] = roi_plot["roi"] * 100.0
                st.bar_chart(roi_plot.set_index("conf_bin")[["roi_pct"]], use_container_width=True)
                st.dataframe(roi_plot, use_container_width=True)
            else:
                st.info("Aucun pari value détecté sur cette configuration.")

    st.markdown("---")
    st.subheader("🔍 Qualité des données (Sprint 1)")
    if st.button("Lancer audit qualité données"):
        dq = run_data_quality_checks()
        st.markdown("**Résumé dataset**")
        st.dataframe(dq["summary"], use_container_width=True)
        st.markdown("**Couverture IDs joueurs**")
        st.dataframe(dq["id_coverage"], use_container_width=True)
        st.markdown("**Taux de valeurs manquantes (colonnes clés)**")
        st.dataframe(dq["null_rates"], use_container_width=True)
        st.markdown("**Outliers / points de vigilance**")
        st.dataframe(dq["outliers"], use_container_width=True)

    st.markdown("---")
    st.subheader("📆 Walk-forward mensuel (Sprint 2)")
    if st.button("Lancer monitoring mensuel"):
        monthly = compute_monthly_diagnostics(
            year_start=diag_years[0],
            year_end=diag_years[1],
            max_matches_per_month=min(1000, int(diag_n)),
            seed=int(diag_seed),
        )
        if monthly.empty:
            st.info("Pas assez de données pour le monitoring mensuel.")
        else:
            st.dataframe(monthly, use_container_width=True)
            plot_df = monthly.set_index("month")[["accuracy_pct", "roi_value_pct"]]
            st.line_chart(plot_df, use_container_width=True)

    st.subheader("📡 Drift des features (Sprint 2)")
    if st.button("Lancer analyse drift"):
        drift = compute_feature_drift()
        if drift.empty:
            st.info("Pas assez de données pour le drift.")
        else:
            st.dataframe(drift, use_container_width=True)

    st.markdown("---")
    st.subheader("🪪 Résolution identité joueurs")
    _csv_diag, _csv_diag_mtime = _prematch_csv_signature()
    if _csv_diag:
        if st.button("🔎 Lancer diagnostic identité"):
            st.session_state["identity_diag_cached"] = compute_identity_diagnostics(_csv_diag, _csv_diag_mtime)
        st.caption(
            "Le diagnostic résout chaque joueur du dernier prematch via "
            "`matches_recent` (ATP / TennisMyLife) ou `wta_matches` (WTA / Sackmann)."
        )
        diag_id = st.session_state.get("identity_diag_cached")
        if diag_id is None and ENABLE_IDENTITY_DIAG_AUTO:
            diag_id = compute_identity_diagnostics(_csv_diag, _csv_diag_mtime)
            st.session_state["identity_diag_cached"] = diag_id
        if diag_id is None:
            st.info("Diagnostic identité non lancé. Clique sur 'Lancer diagnostic identité'.")
            diag_id = {"summary": {}, "details": pd.DataFrame()}
        sm = diag_id.get("summary", {})
        i1, i2, i3, i4 = st.columns(4)
        i1.metric("Joueurs vus", int(sm.get("players_seen", 0)))
        i2.metric("Résolus", int(sm.get("resolved", 0)), f"{sm.get('resolved_pct', 0.0):.1f}%")
        i3.metric("Non résolus", int(sm.get("unresolved", 0)))
        i4.metric("Low confidence", int(sm.get("low_conf", 0)))
        details = diag_id.get("details", pd.DataFrame())
        if not details.empty:
            st.caption("Détails par joueur")
            st.dataframe(details.head(50), use_container_width=True)

with tab6:
    st.header("📡 Tracking modèle (paris réels)")
    st.markdown(
        "Suivi de la performance **réelle** de tes paris, comparée aux prédictions "
        "stockées au moment du placement (probabilité modèle, EV) et au backtest 2025 de référence."
    )
    from scripts.model_tracking import compute_tracking, BACKTEST_BASELINE

    if st.button("🔄 Recalculer le tracking", key="tracking_refresh_btn"):
        st.session_state.pop("_tracking_cached", None)
    tracking = st.session_state.get("_tracking_cached")
    if tracking is None:
        tracking = compute_tracking()
        st.session_state["_tracking_cached"] = tracking

    if not tracking.get("ok"):
        st.info(tracking.get("reason") or "Données insuffisantes.")
    else:
        g = tracking["global"]
        drift = tracking["drift"]

        # Drift banner
        level = drift.get("level", "info")
        if level == "green":
            st.success(f"🟢 {drift.get('message')}")
        elif level == "amber":
            st.warning(f"🟡 {drift.get('message')}")
        elif level == "red":
            st.error(f"🔴 {drift.get('message')}")
        else:
            st.info(f"ℹ️ {drift.get('message')}")

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Paris clos", g["n"])
        m2.metric(
            "Hit-rate réel",
            f"{g['hit']*100:.1f}%",
            f"prév. {g['expected_hit']*100:.1f}%",
        )
        m3.metric(
            "ROI réel",
            f"{g['realised_roi']*100:.1f}%",
            f"prév. {g['expected_roi']*100:+.1f}%",
        )
        m4.metric(
            "Brier",
            f"{g['brier']:.3f}",
            f"backtest {g['baseline_brier']:.3f}",
            delta_color="inverse",
        )
        m5.metric(
            "vs baseline ROI",
            f"{(g['realised_roi'] - g['baseline_roi'])*100:+.1f} pp",
        )
        m6.metric(
            "vs baseline hit",
            f"{(g['hit'] - g['baseline_hit'])*100:+.1f} pp",
        )

        st.markdown("---")
        st.subheader("📐 Calibration : probabilité prédite vs taux observé")
        calib = tracking.get("calibration", [])
        if calib:
            calib_df = pd.DataFrame(calib)
            calib_df["mean_p_pct"] = calib_df["mean_p"] * 100.0
            calib_df["observed_pct"] = calib_df["observed"] * 100.0
            calib_df["ideal_pct"] = calib_df["mean_p_pct"]
            chart_df = calib_df.set_index("mean_p_pct")[["observed_pct", "ideal_pct"]]
            st.line_chart(chart_df, use_container_width=True)
            st.caption(
                "Ligne `observed_pct` = taux de victoires réel · Ligne `ideal_pct` = courbe parfaite (y=x). "
                "Idéalement les deux se confondent."
            )
            disp = calib_df[["mean_p", "observed", "gap", "n"]].copy()
            disp.columns = ["proba moyenne", "observé", "gap (obs - prév.)", "n"]
            st.dataframe(disp, use_container_width=True, hide_index=True)
        else:
            st.info("Pas assez de paris pour tracer la courbe de calibration.")

        st.markdown("---")
        st.subheader("💰 ROI par bucket EV vs backtest 2025")
        ev_rows = tracking.get("ev_buckets", [])
        if ev_rows:
            ev_df = pd.DataFrame(ev_rows)
            base = pd.DataFrame(
                BACKTEST_BASELINE["buckets"],
                columns=["lo", "hi", "n_baseline", "hit_baseline", "roi_baseline"],
            )
            ev_df = ev_df.merge(base[["lo", "hi", "roi_baseline"]], on=["lo", "hi"], how="left")
            ev_df["roi_pct"] = ev_df["roi"] * 100.0
            ev_df["roi_baseline_pct"] = ev_df["roi_baseline"] * 100.0
            chart_ev = ev_df.set_index("ev_bucket")[["roi_pct", "roi_baseline_pct"]]
            st.bar_chart(chart_ev, use_container_width=True)
            disp_ev = ev_df[["ev_bucket", "n", "hit", "roi", "expected_roi", "delta_roi"]].copy()
            disp_ev.columns = ["EV bucket", "n", "hit", "ROI réel", "ROI prévu", "delta"]
            st.dataframe(disp_ev, use_container_width=True, hide_index=True)
        else:
            st.info("Aucun pari clos avec EV stockée. Place quelques paris depuis la nouvelle UI.")

        c1, c2 = st.columns(2)
        with c1:
            st.subheader("🎾 Par tour (ATP / WTA)")
            by_tour = tracking.get("by_tour", [])
            if by_tour:
                df_tour = pd.DataFrame(by_tour)
                df_tour["roi_pct"] = df_tour["roi"] * 100.0
                df_tour["hit_pct"] = df_tour["hit"] * 100.0
                disp_tour = df_tour[["key", "n", "hit_pct", "roi_pct", "brier", "delta_roi"]].copy()
                disp_tour.columns = ["Tour", "n", "hit %", "ROI %", "Brier", "Δ ROI vs prévu"]
                st.dataframe(disp_tour, use_container_width=True, hide_index=True)
            else:
                st.info("Pas de données par tour.")
        with c2:
            st.subheader("🌱 Par surface")
            by_surf = tracking.get("by_surface", [])
            if by_surf:
                df_s = pd.DataFrame(by_surf)
                df_s["roi_pct"] = df_s["roi"] * 100.0
                df_s["hit_pct"] = df_s["hit"] * 100.0
                disp_s = df_s[["key", "n", "hit_pct", "roi_pct", "brier", "delta_roi"]].copy()
                disp_s.columns = ["Surface", "n", "hit %", "ROI %", "Brier", "Δ ROI vs prévu"]
                st.dataframe(disp_s, use_container_width=True, hide_index=True)
            else:
                st.info("Pas de données par surface.")

        st.markdown("---")
        st.subheader("🧾 Journal de réconciliation")
        try:
            conn = sqlite3.connect("data/bettinghud.db")
            recon_df = pd.read_sql(
                "SELECT run_ts, bet_id, match_name, old_status, new_status, "
                "old_winner, new_winner, sources_agreement, action FROM reconciliation_log "
                "ORDER BY id DESC LIMIT 100",
                conn,
            )
            conn.close()
        except Exception:
            recon_df = pd.DataFrame()
        if recon_df.empty:
            st.info("Aucune divergence enregistrée pour l'instant. Lance une réconciliation depuis l'onglet Portefeuille.")
        else:
            st.dataframe(recon_df, use_container_width=True, hide_index=True)

# Bouton de rafraîchissement
if st.button("🔄 Rafraîchir les données"):
    get_latest_scraped_data.clear()
    st.rerun()

st.sidebar.header("Paramètres")
st.sidebar.caption(
    "Stats live par tour : **ATP → TennisMyLife** (`matches_recent`), "
    "**WTA → Tennis Abstract / Sackmann** (`wta_matches` + `rankings_wta_current`)."
)
if not FAST_LIVE_MODE:
    st.sidebar.caption(
        "Mode qualité : stats réelles + mapping (plus de fast_default). "
        "Après sync TML / backfill, « Rafraîchir les données » invalide le cache."
    )
if LIVE_ONLY_TODAY_TOMORROW:
    st.sidebar.caption(
        "Live limité à aujourd’hui + demain (charge réduite). "
        "Tout afficher : BETTINGHUD_LIVE_ONLY_TODAY_TOMORROW=false."
    )
st.sidebar.slider("Seuil minimal de Value (EV %)", min_value=1.0, max_value=10.0, value=3.0, step=0.5)
st.sidebar.selectbox("Filtre Surface", ["Toutes", "Hard", "Clay", "Grass"])

st.sidebar.header("Automatisation & Scraper")
st.sidebar.caption(
    "Mise à jour base ATP/WTA en arrière-plan (déf. toutes les 24h après 2min). "
    "Sans dépôt git dans `data/raw/tennis_wta`, les CSV WTA sont téléchargés depuis GitHub. "
    "Log : `data/logs/tours_auto_sync.log`. Désactiver : `BETTINGHUD_AUTO_SYNC_TOURS=0`."
)
st.sidebar.caption(
    "Réentraînement ML automatique : défaut **1× / semaine** (après 2h de délai). "
    "Désactiver : `BETTINGHUD_AUTO_ML_TRAIN_WEEKLY=0`. "
    "Intervalle (s) : `BETTINGHUD_AUTO_ML_TRAIN_INTERVAL_SEC` (déf. 604800 = 7 jours). "
    "Délai avant 1er run : `BETTINGHUD_AUTO_ML_TRAIN_INITIAL_DELAY_SEC` (déf. 7200)."
)
_fresh = get_data_freshness_snapshot()
with st.sidebar.expander("Fraîcheur ATP/WTA & modèle ML", expanded=False):
    st.markdown(
        f"**Dernière sync DB** (ATP+WTA, pipeline OK) : `{_format_meta_utc(_fresh.get('last_tours_sync_iso'))}`"
    )
    st.markdown(
        f"**Dernier entraînement ML** (`update_model_tml`) : `{_format_meta_utc(_fresh.get('last_ml_train_iso'))}`"
    )
    st.markdown(
        f"**Fichier modèle** (`xgb_model_tml_v1.pkl`) : `{_format_mtime_local(_fresh.get('model_bundle_mtime'))}`"
    )
    _la = _fresh.get("last_atp_match")
    if _la:
        st.markdown(
            f"**Dernier match ATP** (base TML, même filtre que l’entraînement ML) : **{_la['date']}** — "
            f"{_la['tourney_name']} — {_la['winner_name']} vs {_la['loser_name']}"
        )
    else:
        st.markdown("**Dernier match ATP** : *aucun enregistrement (table ou filtre vide).*")
    _lw = _fresh.get("last_wta_match")
    if _lw:
        st.markdown(
            f"**Dernier match WTA** (base Sackmann, même filtre que l’entraînement ML) : **{_lw['date']}** — "
            f"{_lw['tourney_name']} — {_lw['winner_name']} vs {_lw['loser_name']}"
        )
    else:
        st.markdown("**Dernier match WTA** : *aucun enregistrement (table vide ou indisponible).*")
    st.caption(
        "Les horodatages sync/train sont enregistrés après succès complet du script concerné. "
        "Les dates matchs reflètent le dernier ligne `matches_recent` (TML) et `wta_matches` (≥ 2010), "
        "comme au chargement ML ; certaines lignes peuvent encore être exclues si rangs / âge manquent."
    )

auto_mode = st.sidebar.checkbox("Mode Auto (Scrape 10m / Refresh 1m)", value=False)
if auto_mode:
    start_background_scraper()
    st_autorefresh(interval=60 * 1000, key="data_autorefresh")
    st.sidebar.success("Auto-Refresh activé ! 🔄")
else:
    st.sidebar.info("Mode manuel actif")
    if st.sidebar.button("🚀 Lancer le Scraper manuellement"):
        with st.spinner("Scraping en cours..."):
            scraper = FlashscoreScraper()
            asyncio.run(scraper.get_today_matches_and_odds())
        get_latest_scraped_data.clear()
        st.success("Scraping terminé !")
        st.rerun()
    if st.sidebar.button("🧠 MAJ TML + Réentraîner modèle"):
        with st.spinner("Sync TennisMyLife + entraînement modèle en cours..."):
            subprocess.run([sys.executable, "scripts/update_model_tml.py"], check=False)
        st.success("Mise à jour modèle terminée.")
        st.rerun()