import copy
import streamlit as st
import pandas as pd
import numpy as np
import altair as alt
import sys
import os
import glob
import asyncio
import sqlite3
import threading
import time
import subprocess
import re
import json
import functools
import joblib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from streamlit_autorefresh import st_autorefresh

# Ajouter le répertoire parent au path pour les imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.value_detector import ValueDetector
from scripts.live_quant_engine import (
    LiveProbabilityResult,
    build_manual_score_state,
    calculate_live_probability,
    is_first_set_in_progress,
    live_ev_pct,
)
from scripts.ml_model import (
    TennisMLModel,
    resolve_match_brier_segment_key,
    resolve_segment_brier_score,
)
from scripts.priority_scoring import (
    PREMIUM_SEGMENT_BRIER_MAX,
    enrich_value_metrics,
    is_premium_segment,
)
from scripts.stats_engine import (
    TennisStatsEngine,
    _norm_pid_key,
    clutch_score_52weeks,
    tactical_vector_52weeks,
    travel_fatigue_index_from_history,
)
from scripts.player_identity import to_lastname_initial
from scripts.scraper_prematch import FlashscoreScraper
from scripts.backtest_staking_sim import (
    list_backtest_years_with_valid_csv,
    load_and_filter_bets_csv,
    resolve_backtest_csv,
    simulate_sequential_intraday,
)
from scripts.scraper_results import ResultsScraper
import scripts.scraper_profiles as scraper_profiles
from scripts.scraper_profiles import ProfileScraper
from scripts.live_snapshot import (
    SNAPSHOT_PATH,
    acquire_snapshot_build_lock,
    advance_live_build_progress,
    clear_live_build_progress,
    complete_live_build_progress,
    init_live_build_progress,
    invalidate_live_snapshot_ram_cache,
    live_signature_dict,
    load_nextday_snapshot_for_date,
    load_live_snapshot,
    load_live_snapshot_by_model,
    model_signature_dict,
    read_live_build_progress,
    release_snapshot_build_lock,
    save_live_snapshot,
    snapshot_build_in_progress,
    snapshot_meta,
)
from scripts.data_quality import run_data_quality_checks
from scripts.match_rank_quality import (
    STALE_RANK_STATS_MAX_DAYS,
    count_matches_excluded_by_reason,
    match_has_rank_points_source as _match_has_rank_points_source,
)
from scripts.model_monitor import compute_monthly_diagnostics, compute_feature_drift

PROFILE_CACHE_SCHEMA = getattr(scraper_profiles, "PROFILE_CACHE_VERSION", 1)


def _patch_streamlit_width_compat() -> None:
    """Compat Streamlit >= 1.4x: mappe `use_container_width` -> `width`."""
    targets = (
        "dataframe",
        "table",
        "line_chart",
        "area_chart",
        "bar_chart",
        "altair_chart",
        "plotly_chart",
    )
    for name in targets:
        fn = getattr(st, name, None)
        if fn is None or getattr(fn, "_width_compat_patched", False):
            continue

        def _make_wrapper(orig):
            def _wrapped(*args, **kwargs):
                if "use_container_width" in kwargs and "width" not in kwargs:
                    u = bool(kwargs.pop("use_container_width"))
                    kwargs["width"] = "stretch" if u else "content"
                else:
                    kwargs.pop("use_container_width", None)
                return orig(*args, **kwargs)

            setattr(_wrapped, "_width_compat_patched", True)
            return _wrapped

        setattr(st, name, _make_wrapper(fn))


_patch_streamlit_width_compat()


def _env_flag(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _bettinghud_environment() -> str:
    """preprod (défaut, PC local) ou prod (serveur dédié). Voir docs/ENVIRONNEMENTS.md."""
    raw = (os.getenv("BETTINGHUD_ENV") or "preprod").strip().lower()
    if raw in {"prod", "production"}:
        return "prod"
    return "preprod"


def _bettinghud_environment_label() -> str:
    return "PROD" if _bettinghud_environment() == "prod" else "PREPROD"


# CLI / scripts (rebuild snapshot) : charge moteurs sans exécuter l'UI Streamlit.
# Ne PAS définir BETTINGHUD_HEADLESS sur le service systemd « dashboard » en PROD
# (sinon tout le bloc UI sous `if not HEADLESS_APP` est ignoré). Voir docs/OPS_PROD_DEPANNAGE.md.
HEADLESS_APP = _env_flag("BETTINGHUD_HEADLESS", False)


# Qualité max : scrape profil + signaux avancés (surcharge les autres flags sauf FAST_LIVE).
MAX_QUALITY_MODE = _env_flag("BETTINGHUD_MAX_QUALITY_MODE", False)
# Désormais False par défaut : vraies stats / mapping (matches_recent) + labels source hors « fast_default ».
FAST_LIVE_MODE = _env_flag("BETTINGHUD_FAST_LIVE_MODE", False)
_DEFAULT_LIVE_PLAYER_STATS: dict = {
    "rank": 100,
    "age": 25,
    "ht": 185,
    "pts": 1000,
    "hand": "U",
    "stats_source": "preview_default",
    "stats_reference_date": None,
}
_NEUTRAL_LIVE_FORM: dict = {"win_pct": 50.0, "matches": 0}
_NEUTRAL_LIVE_FATIGUE: dict = {"minutes_played": 0, "matches": 0}
_NEUTRAL_LIVE_MATCH_QUALITY: dict = {
    "wins_last7d": 0,
    "three_setters_last14d": 0,
    "last_round_reached": 0,
}
_NEUTRAL_LIVE_SPEED_PROFILE: dict = {
    "speed_affinity": 0.0,
    "speed_performance_delta": 0.0,
    "samples": 0,
}
FEATURE_STORE_PATH = os.path.join("data", "cache", "player_feature_store.joblib")
FEATURE_STORE_MAX_AGE_SEC = max(
    3600,
    int(os.getenv("BETTINGHUD_FEATURE_STORE_MAX_AGE_SEC", str(48 * 3600))),
)
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
ENABLE_BACKGROUND_PROFILE_PREWARM = _env_flag("BETTINGHUD_ENABLE_BACKGROUND_PROFILE_PREWARM", True)
# Job arrière-plan : prematch + préchauffage profils TE + snapshot live (défaut actif).
ENABLE_LIVE_DATA_DAEMON = _env_flag("BETTINGHUD_LIVE_DATA_DAEMON", True)
LIVE_DATA_DAEMON_INTERVAL_SEC = max(
    300, int(os.getenv("BETTINGHUD_LIVE_DATA_DAEMON_INTERVAL_SEC", "900"))
)
LIVE_DATA_DAEMON_INITIAL_DELAY_SEC = max(
    3, int(os.getenv("BETTINGHUD_LIVE_DATA_DAEMON_INITIAL_DELAY_SEC", "8"))
)
# Reporte le rebuild snapshot tant que l'onglet Live Tracker est actif (heartbeat).
LIVE_DAEMON_DEFER_UI_SEC = max(
    30, int(os.getenv("BETTINGHUD_LIVE_DAEMON_DEFER_UI_SEC", "90"))
)
# Workers identité/stats pour le daemon uniquement (UI / refresh manuel = IDENTITY_RESOLVE_WORKERS).
LIVE_DAEMON_IDENTITY_WORKERS = max(
    1, min(12, int(os.getenv("BETTINGHUD_DAEMON_IDENTITY_WORKERS", "2")))
)
LIVE_DAEMON_PROFILE_PREWARM_BATCH = max(
    0, min(24, int(os.getenv("BETTINGHUD_DAEMON_PROFILE_PREWARM_BATCH", "4")))
)
# Précharge snapshot Live (J+0/J+1) dès l'ouverture du dashboard, sans ouvrir Live Tracker.
ENABLE_LIVE_PROJECTION_WARMUP = _env_flag("BETTINGHUD_LIVE_PROJECTION_WARMUP", True)
LIVE_PROJECTION_WARMUP_DELAY_SEC = max(
    2, int(os.getenv("BETTINGHUD_LIVE_PROJECTION_WARMUP_DELAY_SEC", "5"))
)
LIVE_SNAPSHOT_TTL_SEC = int(os.getenv("BETTINGHUD_LIVE_SNAPSHOT_TTL_SEC", str(24 * 3600)))
# Rafraîchissement automatique de l'onglet Live Tracker (snapshot disque 24 h, J+0/J+1).
LIVE_TRACKER_AUTO_REFRESH_SEC = max(
    15, int(os.getenv("BETTINGHUD_LIVE_TRACKER_AUTO_REFRESH_SEC", "30"))
)
# Rafraîchissement auto plus espacé quand le snapshot est déjà chargé (moins de reruns lourds).
LIVE_TRACKER_IDLE_REFRESH_SEC = max(
    LIVE_TRACKER_AUTO_REFRESH_SEC,
    int(os.getenv("BETTINGHUD_LIVE_TRACKER_IDLE_REFRESH_SEC", "120")),
)
# Pendant un build sans données affichables (attente initiale).
LIVE_BUILD_POLL_SEC = max(
    10, int(os.getenv("BETTINGHUD_LIVE_BUILD_POLL_SEC", "20"))
)
# Pendant l'enrichissement alors que le preview est déjà visible (évite refresh toutes les 4 s).
LIVE_ENRICH_POLL_SEC = max(
    LIVE_TRACKER_IDLE_REFRESH_SEC,
    int(os.getenv("BETTINGHUD_LIVE_ENRICH_POLL_SEC", "180")),
)
# Par défaut : pas de rerun auto une fois les matchs affichés (évite scroll / clics perdus).
LIVE_TRACKER_AUTOREFRESH_WHEN_LOADED = _env_flag(
    "BETTINGHUD_LIVE_TRACKER_AUTOREFRESH_WHEN_LOADED", False
)
# Cartes value bets affichées par page (le reste via « Voir plus »).
LIVE_VB_DISPLAY_LIMIT = max(
    3, int(os.getenv("BETTINGHUD_LIVE_VB_DISPLAY_LIMIT", "12"))
)
# Build snapshot en 2 temps : preview rapide (~1–3 min) puis enrichissement complet en arrière-plan.
LIVE_TWO_PHASE_SNAPSHOT = _env_flag("BETTINGHUD_LIVE_TWO_PHASE_SNAPSHOT", True)
# Phase preview : pas de scrape TE ni signaux avancés lourds (ML + stats cache uniquement).
LIVE_BUILD_FAST_PREVIEW = _env_flag("BETTINGHUD_LIVE_BUILD_FAST_PREVIEW", True)
# Enrichissement complet : leviers perf (désactiver signaux coûteux si besoin).
FULL_BUILD_SKIP_DEFENDING = _env_flag("BETTINGHUD_FULL_BUILD_SKIP_DEFENDING", False)
FULL_BUILD_SKIP_WEATHER = _env_flag("BETTINGHUD_FULL_BUILD_SKIP_WEATHER", False)
FULL_BUILD_SKIP_ADV_SIGNALS = _env_flag("BETTINGHUD_FULL_BUILD_SKIP_ADV_SIGNALS", False)
# Enrichissement complet à partir du snapshot preview (ne recalcule pas les lignes déjà ``full``).
LIVE_INCREMENTAL_ENRICH = _env_flag("BETTINGHUD_LIVE_INCREMENTAL_ENRICH", True)
# Après le preview : lancer l'enrichissement complet tout de suite (CLI matinale, pas d'attente UI).
SYNC_FULL_AFTER_PREVIEW = _env_flag("BETTINGHUD_SYNC_FULL_AFTER_PREVIEW", False)
# Delta : ne refaire le ML que si stats / identité joueur ont changé depuis le snapshot full.
LIVE_DELTA_ENRICH = _env_flag("BETTINGHUD_LIVE_DELTA_ENRICH", True)
# Préchauffage profils TE agressif quand l'UI est inactive (nuit / autre onglet).
NIGHT_PROFILE_PREWARM_BATCH = max(
    LIVE_DAEMON_PROFILE_PREWARM_BATCH,
    int(os.getenv("BETTINGHUD_NIGHT_PROFILE_PREWARM_BATCH", "48")),
)
DAEMON_PREWARM_ROUNDS_IDLE = max(
    1, int(os.getenv("BETTINGHUD_DAEMON_PREWARM_ROUNDS_IDLE", "4"))
)
_DB_INDEXES_FLAG = os.path.join("data", "cache", ".db_query_indexes_v1.done")
# Cache mémoire pour builds en thread (st.cache_data inactif hors ScriptRunContext).
_SIGNAL_MATCHES_MEM: dict[str, pd.DataFrame] = {}
_ADV_SIGNALS_MEM_CACHE: dict[tuple, dict] = {}
_SIGNAL_ROWS_BY_PLAYER_MEM: dict[str, dict[str, pd.DataFrame]] = {}
_SIGNAL_PLAYER_KEYS_MEM: dict[str, set[str]] = {}
_WEATHER_DAY_CACHE: dict[tuple, tuple] = {}
PROFILE_CACHE_MAX_AGE_HOURS = max(
    1, int(os.getenv("BETTINGHUD_PROFILE_CACHE_HOURS", "24"))
)
# Ne traiter dans le Live que les matchs du jour et du lendemain (recommandé perf).
LIVE_ONLY_TODAY_TOMORROW = _env_flag("BETTINGHUD_LIVE_ONLY_TODAY_TOMORROW", True)
# Après l'heure prévue (CSV), garder encore N minutes le match dans le build live
# (match probablement en cours — ex. In-Play 1er set). 0 = désactivé (comportement ancien).
LIVE_STARTED_GRACE_MINUTES = max(
    0, int(os.getenv("BETTINGHUD_LIVE_STARTED_GRACE_MINUTES", "90"))
)
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
# Kelly ¼ (value bets) — fraction maximale de la bankroll **disponible** par pari recommandée.
KELLY_RECO_BANKROLL_CAP_FRAC = 0.15
# Fraction de Kelly pleine avant facteur Brier (stratégie live + défaut backtest « adaptatif »).
KELLY_RECO_ADAPTIVE_BASE_FRAC = 0.5
# Seuil EV minimum Live / In-Play / sync report algo (défaut 15 %).
DEFAULT_LIVE_EV_THRESHOLD_PCT = max(
    1.0, float(os.getenv("BETTINGHUD_LIVE_EV_THRESHOLD_PCT", "15"))
)
DEFAULT_LIVE_EV_THRESHOLD_FRAC = DEFAULT_LIVE_EV_THRESHOLD_PCT / 100.0
INPLAY_EV_MIN_FRAC = DEFAULT_LIVE_EV_THRESHOLD_FRAC

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ml_bundle_mtime_for_cache() -> float:
    """Invalide le cache Streamlit du modèle quand le fichier .pkl change (après train)."""
    from scripts.bets_db import get_ml_bundle_mtime

    m = get_ml_bundle_mtime()
    return float(m) if m is not None else 0.0


def _inject_quant_terminal_theme() -> None:
    """Charte UI institutionnelle (Bloomberg / TradingView) — CSS global Streamlit."""
    # st.html (pas st.markdown) : Streamlit 1.57+ n’interprète plus <style> via markdown.
    st.html(
        """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
    :root {
        --bg: #0B0C10;
        --bg-elevated: #12131A;
        --panel: #1C1D24;
        --panel-hover: #23242c;
        --border: #2D3139;
        --text: #FFFFFF;
        --muted: #8A8D98;
        --accent: #00B0FF;
        --success: #00E676;
        --danger: #FF3838;
        --warning: #FFD600;
        --font-ui: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
        --font-mono: "JetBrains Mono", "Roboto Mono", "Consolas", monospace;
        --radius-sm: 4px;
        --radius-md: 6px;
    }

    html, body, [class*="css"] {
        font-family: var(--font-ui);
    }

    .stApp {
        background: var(--bg);
        color: var(--text);
    }

    .main .block-container {
        padding-top: 1rem;
        padding-bottom: 1rem;
        max-width: 100%;
    }

    h1, h2, h3, h4 {
        color: var(--text) !important;
        font-weight: 700 !important;
        letter-spacing: -0.02em;
    }

    p, label, .stMarkdown, .stCaption, [data-testid="stMarkdownContainer"] p {
        color: var(--muted);
    }

  /* —— Chiffres quant (mono, tabular) —— */
    .quant-num,
    .odd-highlight,
    .ev-highlight,
    .ev-highlight-neg,
    div[data-testid="stMetricValue"],
    div[data-testid="stMetricDelta"],
    .stNumberInput input,
    div[data-testid="stDataFrame"] td,
    div[data-testid="stTable"] td {
        font-family: var(--font-mono) !important;
        font-variant-numeric: tabular-nums lining-nums;
        font-feature-settings: "tnum" 1, "lnum" 1;
    }

    div[data-testid="stMetricValue"] {
        font-size: 1.65rem;
        font-weight: 600;
        color: var(--accent) !important;
    }
    div[data-testid="stMetricLabel"] {
        color: var(--muted) !important;
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
    }

  /* —— Tabs —— */
    .stTabs [data-baseweb="tab-list"] {
        gap: 4px;
        background: transparent;
        border-bottom: 1px solid var(--border);
    }
    .stTabs [data-baseweb="tab"] {
        height: 38px;
        background: var(--bg-elevated);
        border-radius: var(--radius-sm) var(--radius-sm) 0 0;
        padding: 6px 14px;
        border: 1px solid var(--border);
        border-bottom: none;
        color: var(--muted);
        font-weight: 600;
        font-size: 0.82rem;
    }
    .stTabs [aria-selected="true"] {
        background: var(--panel) !important;
        color: var(--text) !important;
        border-color: var(--accent) !important;
        box-shadow: inset 0 -2px 0 0 var(--accent);
    }

  /* —— Cartes / conteneurs —— */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background-color: var(--panel);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        box-shadow: none;
        padding: 10px 12px;
    }
    div[data-testid="stVerticalBlockBorderWrapper"]:has(.vb-card-premium-marker) {
        border-color: var(--success);
        box-shadow: 0 0 0 1px rgba(0, 230, 118, 0.35);
    }

    .vb-card-meta {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        margin: 4px 0 8px 0;
    }
    .vb-card-premium-marker {
        display: none;
    }

  /* —— Badges circuit / segment —— */
    .badge-circuit,
    .badge-segment {
        display: inline-block;
        padding: 2px 8px;
        border-radius: var(--radius-sm);
        font-size: 0.68rem;
        font-weight: 700;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        line-height: 1.35;
        vertical-align: middle;
        border: 1px solid transparent;
    }
    .badge-circuit-atp {
        color: #7ec8ff;
        background: rgba(0, 176, 255, 0.12);
        border-color: rgba(0, 176, 255, 0.35);
    }
    .badge-circuit-wta {
        color: #f0a8d8;
        background: rgba(199, 91, 154, 0.14);
        border-color: rgba(199, 91, 154, 0.4);
    }
    .badge-circuit-other {
        color: var(--muted);
        background: rgba(138, 141, 152, 0.12);
        border-color: var(--border);
    }
    .badge-segment-premium {
        color: #9dffc4;
        background: rgba(0, 230, 118, 0.1);
        border-color: rgba(0, 230, 118, 0.35);
        font-family: var(--font-mono);
        font-size: 0.65rem;
    }
    .badge-segment-std {
        color: #a8b4c8;
        background: rgba(0, 176, 255, 0.08);
        border-color: rgba(0, 176, 255, 0.22);
        font-family: var(--font-mono);
        font-size: 0.65rem;
    }

  /* —— Value / cotes —— */
    .odd-highlight {
        background: rgba(255, 214, 0, 0.15);
        color: var(--warning);
        padding: 2px 7px;
        border-radius: var(--radius-sm);
        font-weight: 600;
        font-size: 0.95rem;
        border: 1px solid rgba(255, 214, 0, 0.35);
    }
    .ev-highlight {
        color: var(--success);
        font-weight: 700;
        font-size: 1.05rem;
    }
    .ev-highlight-neg {
        color: var(--danger);
        font-weight: 700;
        font-size: 1.05rem;
    }
    .top-proba-fav-cell {
        background: rgba(0, 176, 255, 0.18);
        color: var(--text);
        font-weight: 600;
        border: 1px solid rgba(0, 176, 255, 0.4);
    }
    .bet-badge {
        display: inline-block;
        margin-top: 6px;
        padding: 3px 9px;
        border-radius: var(--radius-sm);
        background: rgba(0, 230, 118, 0.12);
        color: var(--success);
        font-size: 0.78rem;
        font-weight: 600;
        border: 1px solid rgba(0, 230, 118, 0.35);
        font-family: var(--font-mono);
    }
    .stale-data-badge {
        display: inline-block;
        margin-left: 4px;
        padding: 1px 6px;
        border-radius: var(--radius-sm);
        background: rgba(255, 214, 0, 0.12);
        color: var(--warning);
        font-size: 0.72rem;
        font-weight: 700;
        border: 1px solid rgba(255, 214, 0, 0.45);
        cursor: help;
    }
    .hist-te-conflict-badge,
    .wta-te-conflict-badge {
        display: inline-block;
        margin-left: 4px;
        padding: 1px 7px;
        border-radius: var(--radius-sm);
        background: rgba(255, 56, 56, 0.12);
        color: #ff8a8a;
        font-size: 0.68rem;
        font-weight: 700;
        border: 1px solid rgba(255, 56, 56, 0.45);
        cursor: help;
    }
    .brier-badge-premium {
        display: inline-block;
        padding: 2px 7px;
        border-radius: var(--radius-sm);
        background: rgba(0, 230, 118, 0.1);
        color: var(--success);
        font-size: 0.72rem;
        font-weight: 600;
        border: 1px solid rgba(0, 230, 118, 0.4);
        font-family: var(--font-mono);
        cursor: help;
    }
    .brier-badge-std {
        display: inline-block;
        padding: 2px 7px;
        border-radius: var(--radius-sm);
        background: rgba(138, 141, 152, 0.1);
        color: var(--muted);
        font-size: 0.72rem;
        font-weight: 600;
        border: 1px solid var(--border);
        font-family: var(--font-mono);
        cursor: help;
    }

  /* —— Boutons —— */
    div.stButton > button {
        background: var(--bg-elevated);
        color: var(--text);
        font-weight: 600;
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
        box-shadow: none;
        padding: 0.35rem 0.85rem;
        min-height: 34px;
    }
    div.stButton > button:hover {
        background: var(--panel-hover);
        border-color: var(--muted);
        color: var(--text);
    }
    div.stButton > button[kind="primary"],
    div.stButton > button[data-testid="stBaseButton-primary"] {
        background: #0a3d22;
        color: #ffffff;
        border: 1px solid #0f5c34;
        font-weight: 700;
    }
    div.stButton > button[kind="primary"]:hover,
    div.stButton > button[data-testid="stBaseButton-primary"]:hover {
        background: var(--success);
        color: #0B0C10;
        border-color: var(--success);
    }
    div.stButton > button[kind="primary"]:disabled,
    div.stButton > button[data-testid="stBaseButton-primary"]:disabled {
        background: #1a2e24 !important;
        color: #6a7a72 !important;
        border-color: var(--border) !important;
    }

  /* —— Sliders / radios (éviter le rouge Streamlit par défaut) —— */
    [data-testid="stSlider"] [data-baseweb="slider"] [role="slider"] {
        background-color: var(--success) !important;
    }
    [data-testid="stSlider"] [data-baseweb="slider"] > div > div {
        background-color: rgba(0, 176, 255, 0.35) !important;
    }
    [data-testid="stSlider"] [data-baseweb="slider"] [data-testid="stThumbValue"] {
        color: var(--text) !important;
        background: var(--panel) !important;
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"] > div:first-child {
        border-color: var(--border) !important;
    }
    div[data-testid="stRadio"] label[data-baseweb="radio"] input:checked ~ div:first-child {
        border-color: var(--success) !important;
        background-color: var(--success) !important;
    }
    div[data-testid="stCheckbox"] input:checked + div {
        background-color: var(--success) !important;
        border-color: var(--success) !important;
    }

  /* —— Panneau comparatif EV / cotes —— */
    .ev-compare-panel {
        background: var(--bg-elevated);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        padding: 10px 12px;
        margin: 8px 0 12px 0;
    }
    .ev-compare-panel.ev-tier-strong {
        border-color: rgba(0, 230, 118, 0.55);
        box-shadow: 0 0 0 1px rgba(0, 230, 118, 0.2);
    }
    .ev-compare-panel.ev-tier-ok {
        border-color: rgba(0, 176, 255, 0.45);
    }
    .ev-compare-grid {
        display: grid;
        grid-template-columns: 1fr 1fr 1fr;
        gap: 8px;
        margin-top: 6px;
    }
    @media (max-width: 768px) {
        .ev-compare-grid { grid-template-columns: 1fr; }
    }
    .ev-compare-col {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        padding: 8px 10px;
        text-align: center;
    }
    .ev-compare-col-fair {
        border-color: rgba(0, 176, 255, 0.35);
    }
    .ev-compare-col-user {
        border-color: rgba(255, 214, 0, 0.35);
    }
    .ev-compare-label {
        display: block;
        font-size: 0.68rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--muted);
        margin-bottom: 4px;
    }
    .ev-compare-odd {
        display: block;
        font-family: var(--font-mono);
        font-size: 1.25rem;
        font-weight: 700;
        color: var(--text);
        line-height: 1.2;
    }
    .ev-compare-proba {
        display: block;
        font-family: var(--font-mono);
        font-size: 0.82rem;
        color: var(--muted);
        margin-top: 2px;
    }
    .ev-chip {
        display: inline-block;
        margin-top: 6px;
        padding: 3px 8px;
        border-radius: var(--radius-sm);
        font-family: var(--font-mono);
        font-size: 0.85rem;
        font-weight: 700;
    }
    .ev-chip-strong {
        background: rgba(0, 230, 118, 0.18);
        color: var(--success);
        border: 1px solid rgba(0, 230, 118, 0.45);
    }
    .ev-chip-ok {
        background: rgba(0, 176, 255, 0.12);
        color: #7ec8ff;
        border: 1px solid rgba(0, 176, 255, 0.35);
    }
    .ev-chip-weak {
        background: rgba(138, 141, 152, 0.12);
        color: var(--muted);
        border: 1px solid var(--border);
    }
    .ev-chip-neg {
        background: rgba(255, 56, 56, 0.12);
        color: #ff8a8a;
        border: 1px solid rgba(255, 56, 56, 0.35);
    }
    .ev-compare-footer {
        margin-top: 8px;
        padding-top: 8px;
        border-top: 1px solid var(--border);
        font-size: 0.82rem;
        color: var(--muted);
        text-align: center;
    }
    .ev-compare-footer strong {
        color: var(--text);
        font-family: var(--font-mono);
    }
    .ev-verdict {
        text-align: center;
        font-size: 0.95rem;
        font-weight: 700;
        margin-bottom: 4px;
    }
    .ev-verdict-strong { color: var(--success); }
    .ev-verdict-ok { color: #7ec8ff; }
    .ev-verdict-weak { color: var(--muted); }
    .ev-verdict-neg { color: #ff8a8a; }

  /* —— Inputs —— */
    div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div,
    .stTextInput input,
    .stNumberInput input {
        background-color: var(--bg-elevated) !important;
        border-color: var(--border) !important;
        color: var(--text) !important;
        border-radius: var(--radius-sm) !important;
    }

  /* —— Alertes —— */
    div[data-testid="stAlert"] {
        background-color: var(--panel);
        color: var(--text);
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        padding: 0.5rem 0.75rem;
    }

  /* —— Tableaux compacts —— */
    div[data-testid="stDataFrame"],
    div[data-testid="stTable"] {
        overflow-x: auto;
    }
    div[data-testid="stDataFrame"] div[data-testid="stDataFrameResizable"],
    [data-testid="stDataFrameGlideDataEditor"] {
        font-size: 0.78rem;
    }
    div[data-testid="stDataFrame"] [role="gridcell"],
    div[data-testid="stDataFrame"] th {
        padding: 4px 8px !important;
    }

    [data-testid="stSidebar"] {
        background: var(--bg-elevated);
        border-right: 1px solid var(--border);
    }
    [data-testid="stSidebar"] .stMarkdown p {
        color: var(--muted);
    }

    hr {
        border-color: var(--border);
        opacity: 0.6;
    }

    @media (max-width: 768px) {
        .block-container {
            padding-left: 0.55rem !important;
            padding-right: 0.55rem !important;
        }
        .stTabs [data-baseweb="tab"] {
            height: 34px;
            padding: 4px 8px;
            font-size: 0.75rem;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.25rem !important;
        }
        div.stButton > button {
            width: 100%;
        }
    }
</style>
        """
    )


st.set_page_config(
    page_title=f"BettingHUD [{_bettinghud_environment_label()}] - Tennis",
    page_icon="🎾",
    layout="wide",
)

_inject_quant_terminal_theme()

if not HEADLESS_APP:
    from scripts.web_auth import require_web_login

    if require_web_login() is None:
        st.stop()

# Feedback immédiat : load_engines peut bloquer l’UI (fond #0B0C10 = écran noir sans ce bandeau).
_init_status = None
try:
    _init_status = st.status("Chargement du dashboard…", expanded=False)
    _init_status.write("Modèle ML + stats (SQLite)…")
except Exception:
    _init_status = None

# Incrémenter si l’API de TennisStatsEngine / moteurs change (invalide le cache Streamlit).
_ENGINES_CACHE_VERSION = 22  # invalidate: inactivity days from live DB; resolve_match_surface


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

if _init_status is not None:
    try:
        _init_status.update(label="Prêt.", state="complete")
    except Exception:
        pass

# Mode d'affichage compact mobile (piloté manuellement pour Streamlit webview/smartphone).
if HEADLESS_APP:
    MOBILE_COMPACT = False
else:
    MOBILE_COMPACT = bool(st.session_state.get("mobile_compact_mode", False))

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


def _match_snapshot_quality_flags(match: dict) -> tuple[bool, str | None, float | None]:
    """(unreliable, data_alert, book_gap_pp) — seul le garde-fou rang/proba bloque l'UI."""
    from scripts.match_rank_quality import book_gap_pp_from_match

    unreliable = _prediction_contradicts_rank_points(match)
    alert = "rang_vs_proba" if unreliable else None
    return unreliable, alert, book_gap_pp_from_match(match)


def _enrich_form_record(form: dict | None) -> dict:
    """Complète wins/losses pour l'affichage W-L (snapshots TE anciens ou cache partiel)."""
    f = dict(form or {})
    try:
        matches = int(f.get("matches") or 0)
    except (TypeError, ValueError):
        matches = 0
    if matches <= 0:
        f.setdefault("wins", 0)
        f.setdefault("losses", 0)
        return f
    wins = f.get("wins")
    losses = f.get("losses")
    try:
        if wins is not None and losses is not None:
            w = int(wins)
            lo = int(losses)
            f["wins"] = max(0, w)
            f["losses"] = max(0, lo)
            return f
    except (TypeError, ValueError):
        pass
    if wins is not None:
        try:
            w = max(0, min(matches, int(wins)))
            f["wins"] = w
            f["losses"] = max(0, matches - w)
            return f
        except (TypeError, ValueError):
            pass
    try:
        wp = float(f.get("win_pct", 50))
        w = int(round(matches * wp / 100.0))
        w = max(0, min(matches, w))
    except (TypeError, ValueError):
        w = 0
    f["wins"] = w
    f["losses"] = max(0, matches - w)
    return f


def _merge_live_profile(stats, profile, tour_hint=None, ref_date: str | None = None):
    """Met à jour forme / fatigue à partir du profil TennisExplorer scrapé.

    Si le profil est absent (erreur réseau, URL manquante), retombée sur la base
    ATP TennisMyLife (`tour_hint='ATP'`) ou la base WTA Sackmann (`tour_hint='WTA'`).
    La fenêtre temporelle est mesurée par rapport à la **dernière date présente dans
    le jeu de données du tour** (et non aux N jours calendaires courants) : utile
    en intersaison, mais les libellés UI « 14 j / 3 mois » deviennent une approximation.

    Rang / points issus de TML ou Sackmann (`stats_source` officiel) ne sont pas écrasés
    par l’estimation depuis TennisExplorer.
    """
    pid = stats.get("_pid")
    if not profile:
        return (
            _enrich_form_record(
                stats_engine.get_recent_form(pid, tour_hint=tour_hint, ref_date=ref_date)
            ),
            stats_engine.get_recent_fatigue(
                pid, tour_hint=tour_hint, ref_date=ref_date
            ),
        )
    form_matches = int(profile.get("form_matches") or 0)
    if form_matches <= 0:
        form = _enrich_form_record(
            stats_engine.get_recent_form(pid, tour_hint=tour_hint, ref_date=ref_date)
        )
    else:
        form_wins = int(profile.get("form_wins") or 0)
        form = _enrich_form_record(
            {
                "win_pct": profile["win_pct"],
                "matches": form_matches,
                "wins": form_wins,
                "losses": max(0, form_matches - form_wins),
            }
        )
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


# Si l’historique TML des signaux avancés n’a pas de match dans les N derniers jours,
# on affine les jours repos avec la dernière date de match lue sur TennisExplorer.
_TE_INACTIVITY_BRIDGE_DAYS = 60


def _te_days_since_last_match(profile: dict | None, ref_dt_iso: str | None) -> int | None:
    """Jours entre ref_dt_iso et dernier match listé sur le profil TE ; None si inconnu."""
    if not profile or not ref_dt_iso:
        return None
    raw = profile.get("te_last_match_date_iso")
    if not raw:
        return None
    try:
        ref = pd.Timestamp(str(ref_dt_iso)[:10]).normalize()
        last = pd.Timestamp(str(raw)[:10]).normalize()
    except Exception:
        return None
    if last > ref:
        return None
    return int(max(0, (ref - last).days))


def _blend_inactivity_days_with_te(
    tml_days: object,
    profile: dict | None,
    ref_dt_iso: str | None,
) -> tuple[float, bool]:
    """(jours pour le modèle, True si TE a raccourci l’inactivité vs TML ≥ 60 j)."""
    try:
        td = float(tml_days)
    except Exception:
        td = 7.0
    td = max(0.0, td)
    if td < _TE_INACTIVITY_BRIDGE_DAYS:
        return td, False
    te_d = _te_days_since_last_match(profile, ref_dt_iso)
    if te_d is None:
        return td, False
    new_d = float(min(td, te_d))
    return new_d, new_d + 1e-6 < td


def _rank_stats_source_key(stats: dict | None) -> str | None:
    """Code `stats_source` utilisable pour filtrer l'affichage live (None = exclu)."""
    if not stats:
        return None
    s = str(stats.get("stats_source") or "").strip().lower()
    if not s or s == "no_ranking_source":
        return None
    return s


def _match_homogeneous_rank_source(match: dict) -> bool:
    """Les deux joueurs ont la même origine rang/points et des données en base."""
    k1 = _rank_stats_source_key(match.get("p1_stats"))
    k2 = _rank_stats_source_key(match.get("p2_stats"))
    return bool(k1 and k2 and k1 == k2)


def _rank_source_quality_label(match: dict) -> str:
    k1 = _rank_stats_source_key(match.get("p1_stats"))
    k2 = _rank_stats_source_key(match.get("p2_stats"))
    if k1 and k2 and k1 == k2:
        return "homogeneous"
    if k1 and k2:
        return "mixed_sources"
    return "missing_source"


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


# Pastille « Réf. date » : > 1 mois sur stats_reference_date (rang/points ATP ou WTA).
_STALE_REF_DATE_DAYS = 30
# Pastille « données anciennes » : > 2 mois depuis le dernier signal match / inactivité.
_STALE_PLAYER_DATA_DAYS = 60


def _player_stats(match: dict, player_side: int) -> dict:
    return (match.get("p1_stats") if int(player_side) == 1 else match.get("p2_stats")) or {}


def _player_ref_date_age_days(match: dict, player_side: int) -> int | None:
    """Âge en jours de ``stats_reference_date`` par rapport à la date du match (ou aujourd'hui)."""
    ref = _player_stats(match, player_side).get("stats_reference_date")
    if not ref:
        return None
    anchor = match.get("date") or datetime.now().strftime("%Y-%m-%d")
    try:
        d_ref = pd.Timestamp(str(ref)[:10]).normalize()
        d_anchor = pd.Timestamp(str(anchor)[:10]).normalize()
        return int(max(0, (d_anchor - d_ref).days))
    except Exception:
        return None


def _player_ref_date_stale(match: dict, player_side: int) -> bool:
    days = _player_ref_date_age_days(match, player_side)
    return days is not None and days > _STALE_REF_DATE_DAYS


def _ref_date_stale_badge_html(match: dict, player_side: int) -> str:
    days = _player_ref_date_age_days(match, player_side)
    ref = str(_player_stats(match, player_side).get("stats_reference_date") or "")[:10]
    circuit = _player_circuit(match, player_side)
    circuit_lbl = circuit if circuit in ("ATP", "WTA") else "ATP/WTA"
    src = _human_stats_source_plain(_player_stats(match, player_side).get("stats_source"))
    tip_days = f"~{days} j" if days is not None else f"&gt; {_STALE_REF_DATE_DAYS} j"
    title = (
        f"Réf. date {ref or '—'} ({circuit_lbl}) : {src} — "
        f"données rang/points de plus d'un mois ({tip_days}) par rapport au match."
    )
    return f'<span class="stale-data-badge" title="{title}">⚠</span>'


def _player_days_since_last_data(match: dict, player_side: int) -> int | None:
    """Jours depuis le dernier match connu (snapshot ML) ou réf. rang/points en base."""
    fs = match.get("feature_snapshot") or {}
    side = int(player_side)
    pre_key = f"p{side}_days_inactivity_pre_te"
    pre_raw = fs.get(pre_key)
    if pre_raw is not None:
        try:
            return int(max(0, float(pre_raw)))
        except (TypeError, ValueError):
            pass
    key = f"p{side}_days_since_last_match"
    raw = fs.get(key)
    if raw is not None:
        try:
            return int(max(0, float(raw)))
        except (TypeError, ValueError):
            pass
    ref_days = _player_ref_date_age_days(match, player_side)
    if ref_days is not None:
        return ref_days
    stats = _player_stats(match, player_side)
    ref = stats.get("stats_reference_date")
    if not ref:
        return None
    anchor = match.get("date") or datetime.now().strftime("%Y-%m-%d")
    try:
        d_ref = pd.Timestamp(str(ref)[:10]).normalize()
        d_anchor = pd.Timestamp(str(anchor)[:10]).normalize()
        return int(max(0, (d_anchor - d_ref).days))
    except Exception:
        return None


def _player_data_stale(match: dict, player_side: int) -> bool:
    days = _player_days_since_last_data(match, player_side)
    return days is not None and days > _STALE_PLAYER_DATA_DAYS


def _stale_data_badge_html(days: int | None) -> str:
    tip_days = f"~{days} j" if days is not None else f"&gt; {_STALE_PLAYER_DATA_DAYS} j"
    return (
        f'<span class="stale-data-badge" title="Peu de données récentes en base historique ({tip_days})">⚠</span>'
    )


def _match_circuit(match: dict) -> str:
    """ATP ou WTA pour libellés conflit historique / TE."""
    t = str(match.get("tour") or match.get("category") or "").strip().upper()
    return t if t in ("ATP", "WTA") else ""


def _player_circuit(match: dict, player_side: int) -> str:
    c = _match_circuit(match)
    if c:
        return c
    stats = (match.get("p1_stats") if int(player_side) == 1 else match.get("p2_stats")) or {}
    src = str(stats.get("stats_source") or "").lower()
    if src == "matches_recent":
        return "ATP"
    if src in ("wta_matches", "rankings_wta_current"):
        return "WTA"
    return "UNK"


def _hist_table_label(circuit: str) -> str:
    c = str(circuit or "").upper()
    if c == "ATP":
        return "table ATP (TennisMyLife · matches_recent)"
    if c == "WTA":
        return "table WTA (Sackmann · wta_matches)"
    return "historique officiel"


def _player_hist_te_data_conflict(match: dict, player_side: int) -> dict | None:
    """Conflit historique ATP/WTA (longue absence en base) vs activité récente sur profil TE."""
    fs = match.get("feature_snapshot") or {}
    side = int(player_side)
    if not fs.get(f"p{side}_te_inactivity_blend_used"):
        return None
    try:
        pre_d = float(fs.get(f"p{side}_days_inactivity_pre_te") or 0)
        eff_d = float(fs.get(f"p{side}_days_since_last_match") or 0)
    except (TypeError, ValueError):
        return None
    if pre_d < float(_TE_INACTIVITY_BRIDGE_DAYS) or eff_d + 1e-6 >= pre_d:
        return None
    circuit = _player_circuit(match, side)
    return {
        "pre_days": int(pre_d),
        "eff_days": int(eff_d),
        "circuit": circuit,
        "table_label": _hist_table_label(circuit),
    }


def _hist_te_conflict_badge_html(info: dict) -> str:
    pre_d = int(info["pre_days"])
    eff_d = int(info["eff_days"])
    circuit = str(info.get("circuit") or "UNK").upper()
    badge_lbl = f"{circuit}/TE" if circuit in ("ATP", "WTA") else "Base/TE"
    table_lbl = str(info.get("table_label") or _hist_table_label(circuit))
    title = (
        f"Conflit {badge_lbl} : le dernier match en {table_lbl} est ancien "
        f"(~{pre_d} j) mais le profil Tennis Explorer indique une activité plus récente "
        f"(~{eff_d} j utilisés par le modèle). "
        "Match TE possiblement hors base officielle (qualifs, ITF…) — probabilité peu fiable."
    )
    return (
        f'<span class="hist-te-conflict-badge" title="{title}">⚠ {badge_lbl}</span>'
    )


def _player_data_conflict_caption(player_label: str, match: dict, player_side: int) -> str | None:
    info = _player_hist_te_data_conflict(match, player_side)
    if not info:
        return None
    circuit = str(info.get("circuit") or "UNK").upper()
    badge_lbl = f"{circuit}/TE" if circuit in ("ATP", "WTA") else "Base/TE"
    return (
        f"**{player_label}** — pastille **⚠ {badge_lbl}** : dernier match **{info['table_label']}** il y a "
        f"**~{info['pre_days']} j**, mais le profil **Tennis Explorer** indique une activité plus récente ; "
        f"le modèle utilise **~{info['eff_days']} j** d'inactivité. "
        "Analyse à prendre avec prudence."
    )


def _player_display_html(name: str, match: dict, player_side: int) -> str:
    html = name
    conflict = _player_hist_te_data_conflict(match, player_side)
    if conflict:
        html += f" {_hist_te_conflict_badge_html(conflict)}"
    if _player_ref_date_stale(match, player_side):
        html += f" {_ref_date_stale_badge_html(match, player_side)}"
    elif _player_data_stale(match, player_side):
        html += f" {_stale_data_badge_html(_player_days_since_last_data(match, player_side))}"
    return html


def _player_display_plain(name: str, match: dict, player_side: int) -> str:
    out = name
    conflict = _player_hist_te_data_conflict(match, player_side)
    if conflict:
        c = str(conflict.get("circuit") or "Base").upper()
        out += f" ⚠{c}/TE({conflict['pre_days']}→{conflict['eff_days']}j)"
    elif _player_ref_date_stale(match, player_side):
        d = _player_ref_date_age_days(match, player_side)
        ref = str(_player_stats(match, player_side).get("stats_reference_date") or "")[:10]
        out += f" ⚠réf.{ref}({d}j)" if d is not None else " ⚠réf.>1mois"
    elif _player_data_stale(match, player_side):
        days = _player_days_since_last_data(match, player_side)
        out += f" ⚠ ({days}j)" if days is not None else " ⚠"
    return out


def _match_has_hist_te_conflict(match: dict, circuit: str | None = None) -> bool:
    want = str(circuit or "").strip().upper() or None
    for side in (1, 2):
        info = _player_hist_te_data_conflict(match, side)
        if info is None:
            continue
        if want is None:
            return True
        if str(info.get("circuit") or "").upper() == want:
            return True
    return False


# Rétrocompat (anciens imports / tests internes)
_player_wta_te_data_conflict = _player_hist_te_data_conflict
_match_has_wta_te_conflict = _match_has_hist_te_conflict


def _match_has_data_alert(match: dict) -> bool:
    return (
        _match_has_hist_te_conflict(match)
        or _player_ref_date_stale(match, 1)
        or _player_ref_date_stale(match, 2)
        or _player_data_stale(match, 1)
        or _player_data_stale(match, 2)
    )


def _match_data_reliability_bundle(match: dict) -> tuple[int, list[str]]:
    from scripts.match_rank_quality import match_data_reliability_score

    return match_data_reliability_score(
        match,
        hist_te_conflict=_match_has_hist_te_conflict(match),
        ref_date_stale_sides=(
            _player_ref_date_stale(match, 1),
            _player_ref_date_stale(match, 2),
        ),
        data_stale_sides=(
            _player_data_stale(match, 1),
            _player_data_stale(match, 2),
        ),
    )


_LIVE_DATA_ALERT_FILTER_OPTIONS = [
    "Tous les matchs",
    "Masquer conflits Base/TE (ATP & WTA)",
    "Uniquement conflits Base/TE",
    "Masquer conflits ATP/TE",
    "Masquer conflits WTA/TE",
    "Uniquement conflits ATP/TE",
    "Uniquement conflits WTA/TE",
    "Masquer toutes les alertes",
    "Uniquement avec alertes",
]


def _apply_data_alert_filter(matches: list, choice: str) -> list:
    if choice in ("Masquer conflits Base/TE (ATP & WTA)", "Masquer conflits WTA/TE"):
        if choice.endswith("WTA/TE"):
            return [m for m in matches if not _match_has_hist_te_conflict(m, "WTA")]
        return [m for m in matches if not _match_has_hist_te_conflict(m)]
    if choice == "Masquer conflits ATP/TE":
        return [m for m in matches if not _match_has_hist_te_conflict(m, "ATP")]
    if choice in ("Uniquement conflits Base/TE", "Uniquement conflits WTA/TE"):
        if choice.endswith("WTA/TE"):
            return [m for m in matches if _match_has_hist_te_conflict(m, "WTA")]
        return [m for m in matches if _match_has_hist_te_conflict(m)]
    if choice == "Uniquement conflits ATP/TE":
        return [m for m in matches if _match_has_hist_te_conflict(m, "ATP")]
    if choice == "Masquer toutes les alertes":
        return [m for m in matches if not _match_has_data_alert(m)]
    if choice == "Uniquement avec alertes":
        return [m for m in matches if _match_has_data_alert(m)]
    return matches


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
        p1_match_elo = fs.get("p1_match_elo")
        p2_match_elo = fs.get("p2_match_elo")
        p1_surface_match_elo = fs.get("p1_surface_match_elo")
        p2_surface_match_elo = fs.get("p2_surface_match_elo")
        surface_speed = fs.get("surface_speed")
        p1_svc_elo = fs.get("p1_service_elo")
        p2_svc_elo = fs.get("p2_service_elo")
        p1_ret_elo = fs.get("p1_return_elo")
        p2_ret_elo = fs.get("p2_return_elo")
        p1_global_tag = fs.get("p1_global_elo_tag")
        p2_global_tag = fs.get("p2_global_elo_tag")
        p1_match_tag = fs.get("p1_match_elo_tag")
        p2_match_tag = fs.get("p2_match_elo_tag")
        p1_micro_tag = fs.get("p1_micro_elo_tag")
        p2_micro_tag = fs.get("p2_micro_elo_tag")
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
        p1_match_elo = fs.get("p2_match_elo")
        p2_match_elo = fs.get("p1_match_elo")
        p1_surface_match_elo = fs.get("p2_surface_match_elo")
        p2_surface_match_elo = fs.get("p1_surface_match_elo")
        surface_speed = fs.get("surface_speed")
        p1_svc_elo = fs.get("p2_service_elo")
        p2_svc_elo = fs.get("p1_service_elo")
        p1_ret_elo = fs.get("p2_return_elo")
        p2_ret_elo = fs.get("p1_return_elo")
        p1_global_tag = fs.get("p2_global_elo_tag")
        p2_global_tag = fs.get("p1_global_elo_tag")
        p1_match_tag = fs.get("p2_match_elo_tag")
        p2_match_tag = fs.get("p1_match_elo_tag")
        p1_micro_tag = fs.get("p2_micro_elo_tag")
        p2_micro_tag = fs.get("p1_micro_elo_tag")

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

    def _fmt_elo_col(v, tag, *, kind):
        """Append a short hint when the rating is missing from the bundle (défaut) or imputed."""
        s = _fmt_f(v)
        if s == "—":
            return s
        t = (tag or "").strip()
        if kind == "global":
            if t == "default":
                return f"{s} (défaut ML)"
            if t == "micro_avg":
                return f"{s} (estim. micro)"
            return s
        if t == "default":
            return f"{s} (défaut ML)"
        return s

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
        _fmt_elo_col(p1_global_elo, p1_global_tag, kind="global"),
        _fmt_elo_col(p2_global_elo, p2_global_tag, kind="global"),
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
        _fmt_elo_col(p1_surface_elo, p1_global_tag, kind="global"),
        _fmt_elo_col(p2_surface_elo, p2_global_tag, kind="global"),
        _fmt_diff(
            p1_surface_elo, p2_surface_elo, p1_label, p2_label,
            fmt="{:+.0f}", warning=elo_surface_alert,
        ),
    ])

    rows.append([
        "🏆 Elo match réel",
        _fmt_elo_col(p1_match_elo, p1_match_tag, kind="global"),
        _fmt_elo_col(p2_match_elo, p2_match_tag, kind="global"),
        _fmt_diff(p1_match_elo, p2_match_elo, p1_label, p2_label, fmt="{:+.0f}"),
    ])
    rows.append([
        f"🏆 Elo match {surface}",
        _fmt_elo_col(p1_surface_match_elo, p1_match_tag, kind="global"),
        _fmt_elo_col(p2_surface_match_elo, p2_match_tag, kind="global"),
        _fmt_diff(
            p1_surface_match_elo, p2_surface_match_elo, p1_label, p2_label, fmt="{:+.0f}"
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
        _fmt_elo_col(p1_svc_elo, p1_micro_tag, kind="micro"),
        _fmt_elo_col(p2_svc_elo, p2_micro_tag, kind="micro"),
        _fmt_diff(p1_svc_elo, p2_svc_elo, p1_label, p2_label, fmt="{:+.0f}"),
    ])
    rows.append([
        return_label,
        _fmt_elo_col(p1_ret_elo, p1_micro_tag, kind="micro"),
        _fmt_elo_col(p2_ret_elo, p2_micro_tag, kind="micro"),
        _fmt_diff(p1_ret_elo, p2_ret_elo, p1_label, p2_label, fmt="{:+.0f}"),
    ])

    # Forme 90j (win_pct + W-L)
    p1_form = _enrich_form_record(p1_form)
    p2_form = _enrich_form_record(p2_form)

    def _fmt_form(form):
        f = _enrich_form_record(form)
        try:
            matches = int(f.get("matches") or 0)
        except (TypeError, ValueError):
            matches = 0
        if matches <= 0:
            return "—"
        wp = f.get("win_pct")
        if wp is None:
            return "—"
        w = int(f.get("wins") or 0)
        l = int(f.get("losses") or 0)
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


def _match_brier_segment_key(match: dict) -> str:
    cached = match.get("brier_segment_key")
    if cached:
        return str(cached)
    return resolve_match_brier_segment_key(
        ml_model,
        tour=match.get("tour") or match.get("category"),
        surface=match.get("surface"),
        tournament=match.get("tournament"),
        tourney_level=match.get("tourney_level"),
    )


def _match_segment_brier(match: dict) -> float:
    return resolve_segment_brier_score(ml_model, _match_brier_segment_key(match))


def _vb_priority_sort_key(vb: dict) -> float:
    return float((vb.get("val") or {}).get("priority_score") or 0.0)


def _vb_sharpe_per_brier_sort_key(vb: dict) -> float:
    return float((vb.get("val") or {}).get("sharpe_per_brier") or 0.0)


def _vb_ev_sort_key(vb: dict) -> float:
    return float((vb.get("val") or {}).get("value_pct") or 0.0)


def _vb_match_fav_proba(vb: dict) -> float:
    met = _match_favorite_model_metrics(vb.get("match") or {})
    return float(met["fav_p"]) if met else 0.0


def _vb_fav_proba_sort_key(vb: dict) -> float:
    return _vb_match_fav_proba(vb)


def _vb_is_model_favorite_side(vb: dict) -> bool:
    met = _match_favorite_model_metrics(vb.get("match") or {})
    if not met:
        return False
    return int(vb.get("player") or 0) == int(met.get("fav_side") or 0)


def _circuit_badge_html(tour: str) -> str:
    """Badge compact ATP / WTA pour cartes match."""
    t = str(tour or "").upper().strip()
    if "ATP" in t:
        cls = "badge-circuit badge-circuit-atp"
        lbl = "ATP"
    elif "WTA" in t:
        cls = "badge-circuit badge-circuit-wta"
        lbl = "WTA"
    else:
        cls = "badge-circuit badge-circuit-other"
        lbl = t[:8] if t else "—"
    return f'<span class="{cls}">{lbl}</span>'


def _segment_chip_badge_html(seg_key: str, *, premium: bool = False) -> str:
    """Badge segment type [WTA_Clay_G] — fond translucide."""
    key = str(seg_key or "").strip() or "—"
    cls = "badge-segment badge-segment-premium" if premium else "badge-segment badge-segment-std"
    return f'<span class="{cls}">{key}</span>'


def _ev_chip_class(ev_pct: float) -> str:
    if ev_pct >= 15.0:
        return "ev-chip-strong"
    if ev_pct >= 5.0:
        return "ev-chip-ok"
    if ev_pct >= 0.0:
        return "ev-chip-weak"
    return "ev-chip-neg"


def _ev_verdict_label(ev_book_pct: float, edge_pp: float) -> tuple[str, str]:
    """(texte verdict, classe CSS) pour le panneau EV."""
    if ev_book_pct >= 15.0 and edge_pp >= 3.0:
        return "Value forte vs book", "ev-verdict-strong"
    if ev_book_pct >= 5.0:
        return "Value modérée vs book", "ev-verdict-ok"
    if ev_book_pct >= 0.0:
        return "Marge faible — prudence", "ev-verdict-weak"
    return "Pas de value vs book", "ev-verdict-neg"


def _ev_comparison_panel_html(
    *,
    odd_book: float,
    odd_true: float,
    custom_odd: float,
    ev_book_pct: float,
    ev_custom_pct: float,
) -> str:
    """Panneau 3 colonnes : book / fair modèle / votre cote + EV et écart proba."""
    try:
        ob = float(odd_book)
        ot = float(odd_true)
        oc = float(custom_odd)
        ev_b = float(ev_book_pct)
        ev_c = float(ev_custom_pct)
    except (TypeError, ValueError):
        return ""
    if ob <= 1.0 or ot <= 1.0 or oc <= 1.0:
        return ""
    p_book = 100.0 / ob
    p_model = 100.0 / ot
    p_custom = 100.0 / oc
    edge_pp = p_model - p_book
    verdict, verdict_cls = _ev_verdict_label(ev_b, edge_pp)
    tier = "ev-tier-strong" if ev_b >= 15.0 else ("ev-tier-ok" if ev_b >= 5.0 else "")
    delta_odd = oc - ob
    delta_hint = ""
    if abs(delta_odd) > 0.02:
        better = "mieux que le book" if delta_odd > 0 else "moins bon que le book"
        delta_hint = f" · votre cote {abs(delta_odd):.2f} pt {better}"
    return f"""
<div class="ev-compare-panel {tier}">
  <div class="ev-verdict {verdict_cls}">{verdict}</div>
  <div class="ev-compare-grid">
    <div class="ev-compare-col">
      <span class="ev-compare-label">Bookmaker</span>
      <span class="ev-compare-odd">{ob:.2f}</span>
      <span class="ev-compare-proba">{p_book:.1f}% implicite</span>
      <span class="ev-chip {_ev_chip_class(ev_b)}">EV {ev_b:+.1f}%</span>
    </div>
    <div class="ev-compare-col ev-compare-col-fair">
      <span class="ev-compare-label">Juste (modèle)</span>
      <span class="ev-compare-odd">{ot:.2f}</span>
      <span class="ev-compare-proba">{p_model:.1f}% proba</span>
      <span class="ev-compare-proba">écart {edge_pp:+.1f} pp</span>
    </div>
    <div class="ev-compare-col ev-compare-col-user">
      <span class="ev-compare-label">Votre cote</span>
      <span class="ev-compare-odd">{oc:.2f}</span>
      <span class="ev-compare-proba">{p_custom:.1f}% implicite</span>
      <span class="ev-chip {_ev_chip_class(ev_c)}">EV {ev_c:+.1f}%</span>
    </div>
  </div>
  <div class="ev-compare-footer">
    Modèle <strong>{p_model:.1f}%</strong> vs book <strong>{p_book:.1f}%</strong>
    · Δ cote book→vous <strong>{delta_odd:+.2f}</strong>{delta_hint}
  </div>
</div>
"""


def _segment_brier_badge_html(segment_brier: float, seg_key: str = "") -> str:
    b = float(segment_brier)
    premium = is_premium_segment(b)
    cls = "brier-badge-premium" if premium else "brier-badge-std"
    tier = "Premium" if premium else "Standard"
    key_lbl = f" ({seg_key})" if seg_key else ""
    return (
        f'<span class="{cls} quant-num" title="Brier segment{key_lbl} (test backtest) : {b:.4f} — {tier} '
        f'(seuil premium &lt; {PREMIUM_SEGMENT_BRIER_MAX:.2f})">Brier {b:.3f}</span>'
    )


_LIVE_VB_SORT_OPTIONS = [
    "Composite (priorité)",
    "Sharpe seul",
    "EV brute",
]


def _infobulle_value_context_df(val: dict, odd_book: float, odd_true: float, match: dict) -> pd.DataFrame:
    edge_pp = (1 / odd_true * 100) - (1 / odd_book * 100)
    rows = [
        ["EV détectée", f"+{float(val.get('value_pct', 0)):.1f} %"],
        ["Rapport de Sharpe (unitaire)", f"{float(val.get('sharpe_ratio', 0)):.3f}"],
        [
            "Sharpe / Brier segment",
            f"{float(val.get('sharpe_per_brier', 0)):.3f}",
        ],
        [
            "Score priorité composite",
            f"{float(val.get('priority_score', 0)):.4f}",
        ],
        [
            "Brier segment (test)",
            f"{float(val.get('segment_brier', 0)):.4f}",
        ],
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
    for side, pname, ps, loaded in (
        (1, match["player1"], match.get("p1_stats") or {}, match.get("p1_profile_loaded")),
        (2, match["player2"], match.get("p2_stats") or {}, match.get("p2_profile_loaded")),
    ):
        ref = ps.get("stats_reference_date") or "—"
        conflict = _player_hist_te_data_conflict(match, side)
        if conflict:
            c = str(conflict.get("circuit") or "Base").upper()
            lbl = f"{c}/TE" if c in ("ATP", "WTA") else "Base/TE"
            alert = (
                f"⚠ {lbl} : {conflict.get('table_label', 'historique')} ~{conflict['pre_days']} j, "
                f"TE → modèle ~{conflict['eff_days']} j"
            )
        elif _player_ref_date_stale(match, side):
            d = _player_ref_date_age_days(match, side)
            c = _player_circuit(match, side)
            alert = (
                f"⚠ Réf. date >1 mois ({c or 'ATP/WTA'} · {ref}"
                f"{f', ~{d} j' if d is not None else ''})"
            )
        elif _player_data_stale(match, side):
            d = _player_days_since_last_data(match, side)
            alert = f"⚠ Inactivité >2 mois (~{d} j)" if d is not None else "⚠ Inactivité >2 mois"
        else:
            alert = "—"
        rows.append(
            [
                pname,
                _human_stats_source_plain(ps.get("stats_source")),
                str(ref),
                _fmt_te_sync_human(ps, bool(loaded)),
                "oui" if loaded else "non",
                alert,
            ]
        )
    return pd.DataFrame(
        rows,
        columns=["Joueur", "Source rang/points", "Réf. date", "MAJ profil TE", "Page TE OK", "Alerte"],
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
    f_self = _enrich_form_record(f_self)
    f_opp = _enrich_form_record(f_opp)

    def _form_infobulle_line(label: str, f: dict) -> str:
        try:
            n = int(f.get("matches") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return f"{label}: —"
        wp = float(f.get("win_pct", 50))
        w = int(f.get("wins") or 0)
        lo = int(f.get("losses") or 0)
        return f"{label}: {wp:.0f}% ({w}-{lo} / {n} m.)"

    form_s = f"{_form_infobulle_line(player_name, f_self)} · {_form_infobulle_line(opp_name, f_opp)}"
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
    if fs.get("p1_te_inactivity_blend_used") or fs.get("p2_te_inactivity_blend_used"):
        bits = []
        for side, plab in ((1, "P1"), (2, "P2")):
            if not fs.get(f"p{side}_te_inactivity_blend_used"):
                continue
            cinfo = _player_hist_te_data_conflict(match, side)
            if cinfo:
                bits.append(
                    f"{plab} : ⚠ {cinfo.get('circuit', '?')}/TE — "
                    f"{cinfo.get('table_label', 'historique')} ~{cinfo['pre_days']} j → "
                    f"modèle ~{cinfo['eff_days']} j (TE plus récent)"
                )
            else:
                d0 = fs.get(f"p{side}_days_inactivity_pre_te")
                d1 = fs.get(f"p{side}_days_since_last_match")
                try:
                    bits.append(
                        f"{plab} : inactivité historique ~{float(d0):.0f} j → "
                        f"{float(d1):.0f} j (pont TE)"
                    )
                except Exception:
                    bits.append(f"{plab} : pont inactivité TE appliqué")
        if bits:
            rows.append(
                [
                    "Pont inactivité TE (ATP/WTA)",
                    "Actif",
                    " ; ".join(bits),
                ]
            )
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
    from scripts.surface_speed import resolve_tournament_surface

    return resolve_tournament_surface(tournament)


def _resolve_match_surface(row: dict) -> str:
    from scripts.surface_speed import resolve_tournament_surface

    raw = str((row or {}).get("surface") or "").strip().title()
    if raw in ("Hard", "Clay", "Grass", "Carpet"):
        return raw
    return resolve_tournament_surface(
        (row or {}).get("tournament"),
        tournament_url=(row or {}).get("tournament_url"),
    )


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


@st.cache_data(ttl=900)
def _load_player_feature_store(path: str, file_mtime: float):
    if not path or not os.path.isfile(path):
        return {}
    try:
        if FEATURE_STORE_MAX_AGE_SEC > 0 and (time.time() - float(file_mtime or 0.0)) > FEATURE_STORE_MAX_AGE_SEC:
            return {}
        payload = joblib.load(path)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _feature_store_payload() -> dict:
    try:
        mtime = float(os.path.getmtime(FEATURE_STORE_PATH))
    except OSError:
        return {}
    return _load_player_feature_store(FEATURE_STORE_PATH, mtime)


def _feature_store_keys(name: object, tour: object, player_id: object = None) -> list[str]:
    t = str(tour or "").strip().upper()
    if t not in ("ATP", "WTA"):
        t = "ATP"
    out: list[str] = []
    raw_pid = str(player_id or "").strip()
    if raw_pid:
        out.append(raw_pid if raw_pid.startswith(("ATP::", "WTA::")) else f"{t}::{raw_pid}")
    for raw in (name, to_lastname_initial(str(name or ""))):
        nk = _name_key(str(raw or ""))
        if nk:
            out.append(f"{t}::{nk}")
    return list(dict.fromkeys(out))


def _lookup_player_feature_state(name: object, tour: object, player_id: object = None) -> dict | None:
    payload = _feature_store_payload()
    players = payload.get("players") if isinstance(payload, dict) else None
    if not isinstance(players, dict):
        return None
    for key in _feature_store_keys(name, tour, player_id):
        hit = players.get(key)
        if isinstance(hit, dict):
            return dict(hit)
    return None


def _store_match_quality(state: dict | None) -> dict | None:
    if not state:
        return None
    return {
        "wins_last7d": int(state.get("wins_last7d") or 0),
        "three_setters_last14d": int(state.get("three_setters_last14d") or 0),
        "last_round_reached": int(state.get("last_round_reached") or 0),
    }


def _store_speed_profile(state: dict | None) -> dict | None:
    if not state:
        return None
    return {
        "speed_affinity": float(state.get("speed_affinity") or 0.0),
        "speed_performance_delta": float(state.get("speed_performance_delta") or 0.0),
        "samples": int(state.get("speed_samples") or 0),
    }


def _store_adv_signals(
    p1_state: dict | None,
    p2_state: dict | None,
) -> dict | None:
    if not p1_state or not p2_state:
        return None
    p1_style = str(p1_state.get("style_cluster") or "all_court")
    p2_style = str(p2_state.get("style_cluster") or "all_court")
    style_matrix = {
        ("counterpuncher", "big_server"): 0.58,
        ("big_server", "counterpuncher"): 0.42,
        ("aggressive_baseliner", "counterpuncher"): 0.54,
        ("counterpuncher", "aggressive_baseliner"): 0.46,
        ("serve_volley", "aggressive_baseliner"): 0.55,
        ("aggressive_baseliner", "serve_volley"): 0.45,
        ("all_court", "all_court"): 0.50,
    }
    base_style = float(style_matrix.get((p1_style, p2_style), 0.50))
    form_diff = float(p1_state.get("form20") or 0.5) - float(p2_state.get("form20") or 0.5)
    style_adv = max(0.0, min(1.0, base_style + form_diff * 0.20))
    return {
        "style_advantage_score": style_adv,
        "p1_clutch_index": float(p1_state.get("clutch52") or 0.5),
        "p2_clutch_index": float(p2_state.get("clutch52") or 0.5),
        "p1_days": int(p1_state.get("days_since_last_match") or 7),
        "p2_days": int(p2_state.get("days_since_last_match") or 7),
        "p1_first_srv_win10": float(p1_state.get("first_srv_win10") or 0.68),
        "p2_first_srv_win10": float(p2_state.get("first_srv_win10") or 0.68),
        "p1_bp_conv10": float(p1_state.get("bp_conv10") or 0.38),
        "p2_bp_conv10": float(p2_state.get("bp_conv10") or 0.38),
        "p1_dominance_ratio": float(p1_state.get("dominance_ratio") or 1.0),
        "p2_dominance_ratio": float(p2_state.get("dominance_ratio") or 1.0),
        "p1_tac_ace": float(p1_state.get("tac_ace") or 0.08),
        "p1_tac_f1_pct": float(p1_state.get("tac_f1_pct") or 0.62),
        "p1_tac_bp_saved_pct": float(p1_state.get("tac_bp_saved_pct") or 0.58),
        "p1_tac_hold_pct": float(p1_state.get("tac_hold_pct") or 0.75),
        "p2_tac_ace": float(p2_state.get("tac_ace") or 0.08),
        "p2_tac_f1_pct": float(p2_state.get("tac_f1_pct") or 0.62),
        "p2_tac_bp_saved_pct": float(p2_state.get("tac_bp_saved_pct") or 0.58),
        "p2_tac_hold_pct": float(p2_state.get("tac_hold_pct") or 0.75),
        "p1_travel_penalty_index": 0.0,
        "p2_travel_penalty_index": 0.0,
        "p1_clutch52": float(p1_state.get("clutch52") or 0.5),
        "p2_clutch52": float(p2_state.get("clutch52") or 0.5),
        "p1_minutes_played_last7d": float(p1_state.get("minutes_played_last7d") or 0.0),
        "p2_minutes_played_last7d": float(p2_state.get("minutes_played_last7d") or 0.0),
        "p1_tb_win_pct_52w": float(p1_state.get("tb_win_pct_52w") or 0.5),
        "p2_tb_win_pct_52w": float(p2_state.get("tb_win_pct_52w") or 0.5),
    }


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
    "tourney_date, tourney_name, surface, winner_name, loser_name, score, "
    "w_ace, w_svpt, w_1stIn, w_1stWon, w_2ndWon, w_SvGms, w_bpSaved, w_bpFaced, "
    "l_ace, l_svpt, l_1stIn, l_1stWon, l_2ndWon, l_SvGms, l_bpSaved, l_bpFaced"
)


@functools.lru_cache(maxsize=8)
def _load_signal_matches_cached(tour: str = "ATP") -> pd.DataFrame:
    """Charge les matchs récents (>=2023) — cache process (threads / enrichissement)."""
    t = (tour or "ATP").strip().upper()
    if t in _SIGNAL_MATCHES_MEM:
        return _SIGNAL_MATCHES_MEM[t]
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
    _SIGNAL_PLAYER_KEYS_MEM[t] = set(df["w_key"].dropna().astype(str)) | set(df["l_key"].dropna().astype(str))
    _SIGNAL_MATCHES_MEM[t] = df
    return df


def _signal_rows_for_player(df: pd.DataFrame, tour: str, player_key: str | None) -> pd.DataFrame:
    """Rows historiques d'un joueur, cachés pour éviter le scan DataFrame par match."""
    if df is None or df.empty or not player_key:
        return pd.DataFrame()
    t = (tour or "ATP").strip().upper()
    by_player = _SIGNAL_ROWS_BY_PLAYER_MEM.setdefault(t, {})
    hit = by_player.get(player_key)
    if hit is not None:
        return hit
    rows = df[(df["w_key"] == player_key) | (df["l_key"] == player_key)].copy().sort_values("tourney_date")
    by_player[player_key] = rows
    return rows


@st.cache_data(ttl=900)
def _load_signal_matches(tour: str = "ATP"):
    """Wrapper Streamlit UI — même données que `_load_signal_matches_cached`."""
    return _load_signal_matches_cached(tour)


@functools.lru_cache(maxsize=8192)
def _defending_ratio_cached(
    db_path: str,
    player_id: object,
    tourney_name: object,
    match_date: object,
    current_points: float,
    tour: str,
) -> float:
    """Cache process pour `defending_ratio_live` (évite N× lecture SQL année entière)."""
    return float(
        TennisMLModel.defending_ratio_live(
            db_path,
            player_id,
            tourney_name,
            match_date,
            current_points,
            tour,
        )
    )


def _neutral_live_adv_signals() -> dict:
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
        "p1_tac_ace": 0.08,
        "p1_tac_f1_pct": 0.62,
        "p1_tac_bp_saved_pct": 0.58,
        "p1_tac_hold_pct": 0.75,
        "p2_tac_ace": 0.08,
        "p2_tac_f1_pct": 0.62,
        "p2_tac_bp_saved_pct": 0.58,
        "p2_tac_hold_pct": 0.75,
        "p1_travel_penalty_index": 0.0,
        "p2_travel_penalty_index": 0.0,
        "p1_clutch52": 0.5,
        "p2_clutch52": 0.5,
    }


@st.cache_data(ttl=900)
def _compute_live_advanced_signals(
    p1_name: str,
    p2_name: str,
    surface: str,
    tour_hint: str = "ATP",
    tournament_name: str = "",
    ref_dt_iso: Optional[str] = None,
):
    _mem_key = (
        str(p1_name or ""),
        str(p2_name or ""),
        str(surface or ""),
        str(tour_hint or "ATP"),
        str(tournament_name or ""),
        str(ref_dt_iso or ""),
    )
    _hit = _ADV_SIGNALS_MEM_CACHE.get(_mem_key)
    if _hit is not None:
        return dict(_hit)
    p1_store = _lookup_player_feature_state(p1_name, tour_hint)
    p2_store = _lookup_player_feature_state(p2_name, tour_hint)
    store_signals = _store_adv_signals(p1_store, p2_store)
    df = _load_signal_matches_cached(tour_hint)
    p1k, p2k = _name_key(p1_name), _name_key(p2_name)

    def _ref_ts_from_df():
        if df is None or df.empty:
            return None
        max_date = df["tourney_date"].max()
        try:
            return (
                pd.Timestamp(ref_dt_iso).normalize()
                if ref_dt_iso
                else pd.Timestamp(max_date).normalize()
            )
        except Exception:
            return pd.Timestamp(max_date).normalize()

    def _days(rows, ref_ts):
        from scripts.stats_engine import get_days_since_last_match

        if ref_ts is None:
            return 7
        return get_days_since_last_match(rows, ref_ts)

    if store_signals is not None:
        ref_ts = _ref_ts_from_df()
        store_signals = dict(store_signals)
        if ref_ts is not None and p1k and p2k:
            p1_rows = _signal_rows_for_player(df, tour_hint, p1k)
            p2_rows = _signal_rows_for_player(df, tour_hint, p2k)
            store_signals["p1_days"] = _days(p1_rows, ref_ts)
            store_signals["p2_days"] = _days(p2_rows, ref_ts)
        else:
            store_signals["p1_days"] = 7
            store_signals["p2_days"] = 7
        _ADV_SIGNALS_MEM_CACHE[_mem_key] = dict(store_signals)
        return store_signals
    if df is None or df.empty:
        return _neutral_live_adv_signals()
    if not p1k or not p2k:
        return _neutral_live_adv_signals()
    known_keys = _SIGNAL_PLAYER_KEYS_MEM.get((tour_hint or "ATP").strip().upper()) or set()
    if p1k not in known_keys and p2k not in known_keys:
        return _neutral_live_adv_signals()
    max_date = df["tourney_date"].max()
    p1_rows = _signal_rows_for_player(df, tour_hint, p1k)
    p2_rows = _signal_rows_for_player(df, tour_hint, p2k)
    # Même référence temporelle que tactical/travel : date du match (CSV prematch) si dispo,
    # sinon dernier jour connu dans l’historique chargé (évite d’utiliser seul max_date DB).
    ref_ts = _ref_ts_from_df()
    if ref_ts is None:
        return _neutral_live_adv_signals()

    def _days(rows):
        from scripts.stats_engine import get_days_since_last_match

        return get_days_since_last_match(rows, ref_ts)

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

    a1, f1_pct, bp1_sv, ho1 = tactical_vector_52weeks(p1_rows, p1k, ref_ts)
    a2, f2_pct, bp2_sv, ho2 = tactical_vector_52weeks(p2_rows, p2k, ref_ts)
    c521 = clutch_score_52weeks(p1_rows, p1k, ref_ts)
    c522 = clutch_score_52weeks(p2_rows, p2k, ref_ts)
    tr1 = travel_fatigue_index_from_history(p1_rows, p1k, tournament_name, ref_ts)
    tr2 = travel_fatigue_index_from_history(p2_rows, p2k, tournament_name, ref_ts)

    _out = {
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
        "p1_tac_ace": float(a1),
        "p1_tac_f1_pct": float(f1_pct),
        "p1_tac_bp_saved_pct": float(bp1_sv),
        "p1_tac_hold_pct": float(ho1),
        "p2_tac_ace": float(a2),
        "p2_tac_f1_pct": float(f2_pct),
        "p2_tac_bp_saved_pct": float(bp2_sv),
        "p2_tac_hold_pct": float(ho2),
        "p1_travel_penalty_index": float(tr1),
        "p2_travel_penalty_index": float(tr2),
        "p1_clutch52": float(c521),
        "p2_clutch52": float(c522),
    }
    _ADV_SIGNALS_MEM_CACHE[_mem_key] = dict(_out)
    return _out


def _prematch_csv_signature():
    """Chemin + mtime du CSV prematch le plus récent (mtime = contenu réellement mis à jour)."""
    data_dir = os.path.join("data", "scraped")
    files = glob.glob(os.path.join(data_dir, "*.csv"))
    if not files:
        return "", 0.0
    mt_pairs: list[tuple[float, str]] = []
    for fp in files:
        try:
            mt_pairs.append((float(os.path.getmtime(fp)), fp))
        except OSError:
            continue
    if not mt_pairs:
        return "", 0.0
    mtime, latest_file = max(mt_pairs, key=lambda x: x[0])
    return latest_file, mtime


def _ml_model_mtime() -> float:
    """mtime du bundle XGBoost. Sert de clé d'invalidation pour les caches Streamlit
    qui dépendent des prédictions du modèle (sinon `get_latest_scraped_data` peut
    rendre des prédictions obsolètes après un retraining)."""
    for name in ("xgb_model_tml_v47.pkl", "xgb_model_tml_v45.pkl", "xgb_model_tml_v4.pkl", "xgb_model_tml_v1.pkl"):
        path = os.path.join("models", name)
        try:
            return float(os.path.getmtime(path))
        except OSError:
            continue
    return 0.0


# Auto-refresh du fichier prematch (scrape Tennis Explorer en arrière-plan).
# - BETTINGHUD_PREMATCH_AUTO_REFRESH : active/désactive (défaut: True)
# - BETTINGHUD_PREMATCH_TTL_MIN      : âge maximal du dernier CSV avant re-scrape (défaut: 30 min)
# - BETTINGHUD_PREMATCH_LOCK_TTL_SEC : durée max d’un lock (anti-blocage si crash) (défaut: 600s)
PREMATCH_AUTO_REFRESH = _env_flag("BETTINGHUD_PREMATCH_AUTO_REFRESH", True)
PREMATCH_TTL_MIN = max(1, int(os.getenv("BETTINGHUD_PREMATCH_TTL_MIN", "20")))
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
    mt = _sanitize_mtime_float(csv_mtime)
    age_min = (time.time() - mt) / 60.0 if mt > 0 else float("inf")
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


def _match_circuit(match: dict) -> str:
    """ATP ou WTA pour filtre UI (tour ML prioritaire, sinon category du scrape)."""
    t = str(match.get("tour") or match.get("category") or "").strip().upper()
    return t if t in ("ATP", "WTA") else ""


_LIVE_MINOR_TOURNAMENT_TOKENS_ALL = (
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
_LIVE_MINOR_TOURNAMENT_TOKENS_NO_CHALLENGER = tuple(
    tok for tok in _LIVE_MINOR_TOURNAMENT_TOKENS_ALL if tok != "challenger"
)


def _is_atp_wta_circuit_match(
    category,
    tournament_name,
    *,
    include_challengers: bool = False,
) -> bool:
    """ATP/WTA individuels ; optionnellement les Challengers (ITF/UTR/futures toujours exclus)."""
    c = str(category or "").strip().upper()
    if include_challengers and c == "CHALLENGER":
        t = str(tournament_name or "").lower()
        return "itf" not in t and "utr" not in t
    if c not in {"ATP", "WTA"}:
        return False
    t = str(tournament_name or "").lower()
    tokens = (
        _LIVE_MINOR_TOURNAMENT_TOKENS_NO_CHALLENGER
        if include_challengers
        else _LIVE_MINOR_TOURNAMENT_TOKENS_ALL
    )
    return not any(tok in t for tok in tokens)


def _is_major_atp_wta(category, tournament_name):
    """
    Conserve uniquement les gros tournois ATP/WTA.
    Exclut explicitement Challenger/ITF et circuits mineurs assimilés.
    """
    return _is_atp_wta_circuit_match(
        category, tournament_name, include_challengers=False
    )


def _row_prematch_id(row) -> str:
    pmid = row.get("id") if hasattr(row, "get") else None
    if pmid is None:
        pmid = row.get("prematch_id") if hasattr(row, "get") else None
    if pmid is None or (isinstance(pmid, float) and pd.isna(pmid)):
        return ""
    return str(pmid).strip()


def _match_odds_lookup_key(
    p1: str, p2: str, tournament: str, prematch_id: str | None = None
) -> tuple[str, str, str, str]:
    return (
        str(p1 or "").strip().lower(),
        str(p2 or "").strip().lower(),
        str(tournament or "").strip().lower(),
        str(prematch_id or "").strip().lower(),
    )


def _existing_prediction_matches_row(
    existing: dict, row, p1_id: object, p2_id: object
) -> bool:
    row_pmid = _row_prematch_id(row)
    ex_pmid = str(existing.get("prematch_id") or "").strip()
    if row_pmid and ex_pmid and row_pmid != ex_pmid:
        return False
    if row_pmid and not ex_pmid:
        return False
    if str(existing.get("p1_player_id") or "") != str(p1_id or ""):
        return False
    if str(existing.get("p2_player_id") or "") != str(p2_id or ""):
        return False
    return True


def _player_stats_fingerprint(stats: dict | None, player_id: object = None) -> tuple:
    st = stats or {}
    return (
        str(player_id or ""),
        int(st.get("rank") or 100),
        float(st.get("pts") or 0),
        str(st.get("stats_reference_date") or ""),
        str(st.get("stats_source") or ""),
        str(st.get("hand") or "U"),
    )


def _prediction_contradicts_rank_points(match: dict) -> bool:
    """Proba stockée incompatible avec le classement affiché (snapshot enrichi obsolète)."""
    try:
        p1_odd = float(match.get("true_odd_p1") or 0.0)
        if p1_odd <= 1.0:
            return False
        p1_prob = 1.0 / p1_odd
        ps1 = match.get("p1_stats") if isinstance(match.get("p1_stats"), dict) else {}
        ps2 = match.get("p2_stats") if isinstance(match.get("p2_stats"), dict) else {}
        r1 = int(ps1.get("rank") or 0)
        r2 = int(ps2.get("rank") or 0)
        if r1 <= 0 or r2 <= 0:
            return False
        gap = 30
        if r1 > r2 + gap and p1_prob > 0.55:
            return True
        if r2 > r1 + gap and p1_prob < 0.45:
            return True
        if r1 > r2 and p1_prob > 0.70 and (r1 - r2) >= 25:
            return True
        if r2 > r1 and p1_prob < 0.30 and (r2 - r1) >= 25:
            return True
    except Exception:
        pass
    return False


def _match_needs_full_repredict(
    existing: dict,
    p1_stats: dict,
    p2_stats: dict,
    p1_id: object,
    p2_id: object,
) -> bool:
    """True si le match full doit être recalculé (stats joueurs ou identité changées)."""
    if str(existing.get("snapshot_tier") or "") != "full":
        return True
    if _prediction_contradicts_rank_points(existing):
        return True
    if str(existing.get("p1_player_id") or "") != str(p1_id or ""):
        return True
    if str(existing.get("p2_player_id") or "") != str(p2_id or ""):
        return True
    try:
        if float(existing.get("model_mtime_at_predict") or 0.0) != float(
            _ml_model_mtime()
        ):
            return True
    except (TypeError, ValueError):
        return True
    if not LIVE_DELTA_ENRICH:
        return False
    old_p1 = existing.get("p1_stats") if isinstance(existing.get("p1_stats"), dict) else {}
    old_p2 = existing.get("p2_stats") if isinstance(existing.get("p2_stats"), dict) else {}
    old_pid1 = existing.get("p1_player_id")
    old_pid2 = existing.get("p2_player_id")
    if _player_stats_fingerprint(old_p1, old_pid1) != _player_stats_fingerprint(p1_stats, p1_id):
        return True
    if _player_stats_fingerprint(old_p2, old_pid2) != _player_stats_fingerprint(p2_stats, p2_id):
        return True
    return False


def _filter_df_upcoming_or_recent_started_for_live(df: pd.DataFrame) -> pd.DataFrame:
    """Garde les matchs non commencés ou démarrés depuis peu (grâce après l'heure prévue du CSV).

    Utilise la date de ligne + heure ``HH:MM`` en fuseau **Europe/Paris** (aligné calendrier live).
    Les lignes ``Demain …`` restent incluses. Si parsing date/heure échoue, on conserve la ligne.
    """
    if df is None or df.empty or "time" not in df.columns:
        return df
    grace_min = int(LIVE_STARTED_GRACE_MINUTES)
    now_paris = datetime.now(ZoneInfo("Europe/Paris"))
    grace = timedelta(minutes=grace_min)

    def _keep_row(row: pd.Series) -> bool:
        ts = str(row.get("time") or "").strip()
        if not ts:
            return True
        if ts.startswith("Demain"):
            return True
        try:
            d = pd.Timestamp(str(row.get("date") or "")[:10])
            if pd.isna(d):
                return True
            match_date = d.date()
        except Exception:
            return True
        try:
            sched_time = datetime.strptime(ts, "%H:%M").time()
        except Exception:
            return True
        scheduled = datetime.combine(match_date, sched_time, tzinfo=ZoneInfo("Europe/Paris"))
        if scheduled >= now_paris:
            return True
        if grace_min <= 0:
            return False
        return (now_paris - scheduled) <= grace

    mask = df.apply(_keep_row, axis=1)
    return df.loc[mask].copy()


def _load_prematch_df_for_live(csv_path: str) -> pd.DataFrame:
    """Mêmes filtres que le build live (ATP/WTA + Challengers, J+0/J+1, à venir ou récemment commencés)."""
    if not csv_path or not os.path.isfile(csv_path):
        return pd.DataFrame()
    df = pd.read_csv(csv_path)
    df = _filter_df_exclude_doubles_prematch(df)
    if df.empty:
        return df
    # Inclure les Challengers dans le snapshot ; le Live Tracker les masque par défaut (toggle UI).
    df = _filter_df_atp_wta_circuit_vectorized(df, include_challengers=True)
    if df.empty:
        return df
    if LIVE_ONLY_TODAY_TOMORROW:
        df = _filter_df_today_tomorrow_only(df)
        if df.empty:
            return df
    max_live_build = int(
        os.getenv("BETTINGHUD_MAX_LIVE_MATCHES_BUILD", "24" if FAST_LIVE_MODE else "0")
    )

    df = _filter_df_upcoming_or_recent_started_for_live(df)
    if df.empty:
        return df
    if max_live_build > 0 and len(df) > max_live_build:
        df = _cap_live_build_prioritize_demain(df, max_live_build)
    return df


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


def _filter_df_atp_wta_circuit_vectorized(
    df: pd.DataFrame,
    *,
    include_challengers: bool = False,
) -> pd.DataFrame:
    """Équivalent vectorisé à _is_atp_wta_circuit_match."""
    if df is None or df.empty:
        return df
    c = df.get("category", pd.Series("", index=df.index)).astype(str).str.strip().str.upper()
    ok_cat = c.isin(["ATP", "WTA", "CHALLENGER"]) if include_challengers else c.isin(["ATP", "WTA"])
    t = df.get("tournament", pd.Series("", index=df.index)).astype(str).str.lower().fillna("")
    minor = pd.Series(False, index=df.index)
    minor_tokens = (
        _LIVE_MINOR_TOURNAMENT_TOKENS_NO_CHALLENGER
        if include_challengers
        else _LIVE_MINOR_TOURNAMENT_TOKENS_ALL
    )
    for tok in minor_tokens:
        minor = minor | t.str.contains(tok, case=False, na=False, regex=False)
    itf_cat = c.isin(["ITF"]) if include_challengers else pd.Series(False, index=df.index)
    itf_name = t.str.contains("itf", case=False, na=False) | t.str.contains(
        "utr", case=False, na=False
    )
    return df.loc[ok_cat & ~minor & ~itf_cat & ~itf_name].copy()


def _filter_df_major_atp_wta_vectorized(df: pd.DataFrame) -> pd.DataFrame:
    """Équivalent à _is_major_atp_wta sur chaque ligne, sans apply (pandas vectorisé)."""
    return _filter_df_atp_wta_circuit_vectorized(df, include_challengers=False)


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


_LIVE_PLAYER_CACHE_TTL_SEC = int(
    os.getenv("BETTINGHUD_LIVE_PLAYER_CACHE_TTL_SEC", str(24 * 3600))
)
_LIVE_PLAYER_FEATURES_CACHE_TTL_SEC = int(
    os.getenv("BETTINGHUD_LIVE_PLAYER_FEATURES_CACHE_TTL_SEC", str(24 * 3600))
)
_BETTINGHUD_DB_PATH = "data/bettinghud.db"
_DASHBOARD_UI_HEARTBEAT_PATH = os.path.join("data", "cache", ".dashboard_ui_heartbeat")
_LIVE_CACHE_DB_INVALIDATION_FLAG = os.path.join(
    "data", "cache", ".live_cache_db_invalidation_v1.done"
)


def _bettinghud_db_mtime() -> float:
    try:
        return float(os.path.getmtime(_BETTINGHUD_DB_PATH))
    except OSError:
        return 0.0


def _live_player_cache_row_stale(updated_ts: float, ttl_sec: float, *, now: float | None = None) -> bool:
    """True si l'entrée est expirée (TTL) ou si la base a été modifiée après l'écriture du cache."""
    ts = float(updated_ts or 0.0)
    if ts <= 0:
        return True
    t_now = float(now if now is not None else time.time())
    if (t_now - ts) > float(ttl_sec):
        return True
    db_m = _bettinghud_db_mtime()
    return db_m > 0 and db_m > (ts + 0.5)


def _purge_all_live_player_caches() -> None:
    try:
        conn = sqlite3.connect(_BETTINGHUD_DB_PATH)
        try:
            _ensure_live_player_cache_schema(conn)
            _ensure_live_player_features_cache_schema(conn)
            conn.execute("DELETE FROM live_player_cache")
            conn.execute("DELETE FROM live_player_features_cache")
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _purge_live_player_caches_older_than_db() -> int:
    """Supprime les entrées écrites avant le dernier mtime de bettinghud.db."""
    db_m = _bettinghud_db_mtime()
    if db_m <= 0:
        return 0
    n = 0
    try:
        conn = sqlite3.connect(_BETTINGHUD_DB_PATH)
        try:
            _ensure_live_player_cache_schema(conn)
            _ensure_live_player_features_cache_schema(conn)
            cur1 = conn.execute(
                "DELETE FROM live_player_cache WHERE updated_ts < ?", (db_m,)
            )
            cur2 = conn.execute(
                "DELETE FROM live_player_features_cache WHERE updated_ts < ?", (db_m,)
            )
            conn.commit()
            n = int(cur1.rowcount or 0) + int(cur2.rowcount or 0)
        finally:
            conn.close()
    except Exception:
        return 0
    return n


def _maybe_migrate_live_cache_db_invalidation() -> None:
    """Une fois : vide caches joueur + snapshot obsolète (post-ingest Sackmann ITF)."""
    if os.path.isfile(_LIVE_CACHE_DB_INVALIDATION_FLAG):
        return
    _purge_all_live_player_caches()
    try:
        if os.path.isfile(SNAPSHOT_PATH):
            os.remove(SNAPSHOT_PATH)
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(_LIVE_CACHE_DB_INVALIDATION_FLAG), exist_ok=True)
        with open(_LIVE_CACHE_DB_INVALIDATION_FLAG, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _touch_dashboard_ui_heartbeat() -> None:
    try:
        os.makedirs(os.path.dirname(_DASHBOARD_UI_HEARTBEAT_PATH), exist_ok=True)
        with open(_DASHBOARD_UI_HEARTBEAT_PATH, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _dashboard_ui_recently_active(within_sec: float | None = None) -> bool:
    window = float(within_sec if within_sec is not None else LIVE_DAEMON_DEFER_UI_SEC)
    try:
        age = time.time() - os.path.getmtime(_DASHBOARD_UI_HEARTBEAT_PATH)
        return age >= 0 and age < window
    except OSError:
        return False


def _live_signature_dict_compat(
    csv_path: str,
    csv_mtime: float,
    profile_cache_schema: int,
    model_mtime: float,
    engines_cache_version: int,
) -> dict:
    """Signature snapshot (csv + modèle ; pas de mtime DB)."""
    sig = live_signature_dict(
        str(csv_path or ""),
        float(csv_mtime or 0.0),
        int(profile_cache_schema),
        float(model_mtime or 0.0),
        int(engines_cache_version),
    )
    sig.pop("db_mtime", None)
    return sig


def _set_current_thread_background_priority() -> None:
    """Réduit la priorité CPU du thread daemon (Windows / Unix)."""
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            h_thread = kernel32.GetCurrentThread()
            kernel32.SetThreadPriority(h_thread, -1)  # THREAD_PRIORITY_BELOW_NORMAL
        except Exception:
            pass
    else:
        try:
            os.nice(10)
        except Exception:
            pass


def _ensure_live_player_cache_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_player_cache (
            cache_key TEXT PRIMARY KEY,
            updated_ts REAL NOT NULL,
            pid TEXT,
            stats_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_player_cache_updated_ts ON live_player_cache(updated_ts)")
    conn.commit()


def _load_live_player_cache(cache_keys: list[str], ttl_sec: int) -> dict:
    if not cache_keys:
        return {}
    now = time.time()
    out = {}
    try:
        conn = sqlite3.connect("data/bettinghud.db")
        try:
            _ensure_live_player_cache_schema(conn)
            q_marks = ",".join(["?"] * len(cache_keys))
            rows = conn.execute(
                f"SELECT cache_key, updated_ts, pid, stats_json FROM live_player_cache WHERE cache_key IN ({q_marks})",
                cache_keys,
            ).fetchall()
            for ck, ts, pid, sj in rows:
                if _live_player_cache_row_stale(float(ts or 0.0), ttl_sec, now=now):
                    continue
                try:
                    stats = json.loads(sj) if sj else {}
                except Exception:
                    stats = {}
                out[str(ck)] = {"pid": pid, "stats": stats}
        finally:
            conn.close()
    except Exception:
        return {}
    return out


def _upsert_live_player_cache(entries: dict) -> None:
    if not entries:
        return
    now = time.time()
    try:
        conn = sqlite3.connect("data/bettinghud.db")
        try:
            _ensure_live_player_cache_schema(conn)
            for ck, payload in entries.items():
                pid = payload.get("pid")
                stats = payload.get("stats") or {}
                conn.execute(
                    """
                    INSERT INTO live_player_cache(cache_key, updated_ts, pid, stats_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        updated_ts=excluded.updated_ts,
                        pid=excluded.pid,
                        stats_json=excluded.stats_json
                    """,
                    (str(ck), now, None if pid is None else str(pid), json.dumps(stats, ensure_ascii=True)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _ensure_live_player_features_cache_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_player_features_cache (
            cache_key TEXT PRIMARY KEY,
            updated_ts REAL NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_player_features_cache_updated_ts "
        "ON live_player_features_cache(updated_ts)"
    )
    conn.commit()


def _load_live_player_features_cache(cache_keys: list[str], ttl_sec: int) -> dict:
    if not cache_keys:
        return {}
    now = time.time()
    out = {}
    try:
        conn = sqlite3.connect("data/bettinghud.db")
        try:
            _ensure_live_player_features_cache_schema(conn)
            q_marks = ",".join(["?"] * len(cache_keys))
            rows = conn.execute(
                f"SELECT cache_key, updated_ts, payload_json FROM live_player_features_cache "
                f"WHERE cache_key IN ({q_marks})",
                cache_keys,
            ).fetchall()
            for ck, ts, pj in rows:
                if _live_player_cache_row_stale(float(ts or 0.0), ttl_sec, now=now):
                    continue
                try:
                    payload = json.loads(pj) if pj else {}
                except Exception:
                    payload = {}
                out[str(ck)] = payload
        finally:
            conn.close()
    except Exception:
        return {}
    return out


def _upsert_live_player_features_cache(entries: dict) -> None:
    if not entries:
        return
    now = time.time()
    try:
        conn = sqlite3.connect("data/bettinghud.db")
        try:
            _ensure_live_player_features_cache_schema(conn)
            for ck, payload in entries.items():
                conn.execute(
                    """
                    INSERT INTO live_player_features_cache(cache_key, updated_ts, payload_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        updated_ts=excluded.updated_ts,
                        payload_json=excluded.payload_json
                    """,
                    (str(ck), now, json.dumps(payload or {}, ensure_ascii=True)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _identity_live_cache_key(name: str, url: str | None, tour: str | None) -> str:
    return "||".join([str(name or ""), str(url or ""), str(tour or "")])


def _features_live_cache_key(
    name: str,
    tour: str | None,
    pid,
    url: str | None,
    exclude_tourney_id: str = "",
) -> str:
    return "||".join(
        [
            str(name or ""),
            str(tour or ""),
            str(pid or ""),
            str(url or ""),
            str(PROFILE_CACHE_SCHEMA),
            str(exclude_tourney_id or ""),
        ]
    )


def _delete_live_player_cache_keys(cache_keys: list[str]) -> None:
    if not cache_keys:
        return
    keys = [str(k) for k in cache_keys if k]
    if not keys:
        return
    try:
        conn = sqlite3.connect("data/bettinghud.db")
        try:
            _ensure_live_player_cache_schema(conn)
            _ensure_live_player_features_cache_schema(conn)
            q = ",".join(["?"] * len(keys))
            conn.execute(f"DELETE FROM live_player_cache WHERE cache_key IN ({q})", keys)
            conn.execute(f"DELETE FROM live_player_features_cache WHERE cache_key IN ({q})", keys)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _purge_te_profile_disk_cache(player_url: str | None) -> None:
    if not player_url:
        return
    try:
        path = profile_scraper._get_cache_path(player_url)
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _clear_stats_engine_player_cache(pid, tour_hint: str | None) -> None:
    th = str(tour_hint or "").strip().upper()
    pk = _norm_pid_key(pid) or ""
    for t in (th, "ATP", "WTA", ""):
        stats_engine._stats_result_cache.pop((pk, t), None)


def _match_live_key(match: dict) -> str:
    mid = str(match.get("prematch_id") or "").strip()
    if mid:
        return f"id:{mid}"
    return (
        f"{match.get('player1')}|{match.get('player2')}|"
        f"{match.get('date')}|{match.get('time')}"
    )


def _apply_live_match_refresh_to_session(refreshed: dict) -> None:
    key = _match_live_key(refreshed)
    cache = list(st.session_state.get("_live_matches_cache") or [])
    st.session_state["_live_matches_cache"] = [
        refreshed if _match_live_key(m) == key else m for m in cache
    ]


def _replace_live_match_in_snapshot(refreshed: dict) -> bool:
    """Remplace un match dans le snapshot disque courant et le sauvegarde."""
    key = _match_live_key(refreshed)
    sig = _current_live_signature_dict()
    matches = load_live_snapshot(sig, max_age_sec=LIVE_SNAPSHOT_TTL_SEC)
    if matches is None:
        matches = load_live_snapshot_by_model(
            _live_model_signature_dict(),
            max_age_sec=LIVE_SNAPSHOT_TTL_SEC,
        )
    if not matches:
        return False
    out: list[dict] = []
    replaced = False
    for m in matches:
        if isinstance(m, dict) and _match_live_key(m) == key:
            out.append(dict(refreshed))
            replaced = True
        else:
            out.append(dict(m) if isinstance(m, dict) else m)
    if not replaced:
        out.append(dict(refreshed))
    save_live_snapshot(out, sig)
    invalidate_live_snapshot_ram_cache()
    return True


def _force_full_live_match_refresh(match: dict) -> bool:
    """Refresh complet d'un match + persistance snapshot disque."""
    refreshed = _force_refresh_live_match(match)
    refreshed["snapshot_tier"] = "full"
    refreshed["rank_source_quality"] = _rank_source_quality_label(refreshed)
    refreshed["rank_source_homogeneous"] = _match_homogeneous_rank_source(refreshed)
    ok_disk = _replace_live_match_in_snapshot(refreshed)
    _apply_live_match_refresh_to_session(refreshed)
    return ok_disk


def _delete_features_cache_for_player_name(name: str) -> None:
    if not name:
        return
    prefix = f"{str(name).strip()}||"
    try:
        conn = sqlite3.connect("data/bettinghud.db")
        try:
            _ensure_live_player_features_cache_schema(conn)
            conn.execute(
                "DELETE FROM live_player_features_cache WHERE cache_key LIKE ?",
                (prefix + "%",),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _purge_match_player_caches(match: dict) -> None:
    """Invalidate caches TE / SQLite / stats_engine for both players of a live match."""
    p1_name = str(match.get("player1") or "").strip()
    p2_name = str(match.get("player2") or "").strip()
    tour = str(match.get("tour") or match.get("category") or "").strip().upper()
    if tour not in ("ATP", "WTA"):
        tour = _match_circuit(match) or "ATP"
    p1_url = match.get("p1_url")
    p2_url = match.get("p2_url")
    _purge_te_profile_disk_cache(p1_url)
    _purge_te_profile_disk_cache(p2_url)
    id_keys = [
        _identity_live_cache_key(p1_name, p1_url, tour),
        _identity_live_cache_key(p2_name, p2_url, tour),
    ]
    _delete_live_player_cache_keys(id_keys)
    _delete_features_cache_for_player_name(p1_name)
    _delete_features_cache_for_player_name(p2_name)


def _lookup_prematch_urls(p1_name: str, p2_name: str) -> tuple[str | None, str | None]:
    """Retrouve les URLs TE depuis le dernier CSV prematch si absentes du match en cache."""
    csv_path, _ = _prematch_csv_signature()
    if not csv_path or not os.path.isfile(csv_path):
        return None, None
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None, None
    if df.empty:
        return None, None
    p1k = str(p1_name or "").strip().lower()
    p2k = str(p2_name or "").strip().lower()
    for _, row in df.iterrows():
        r1 = str(row.get("player1") or "").strip().lower()
        r2 = str(row.get("player2") or "").strip().lower()
        if {r1, r2} == {p1k, p2k}:
            u1 = row.get("p1_url")
            u2 = row.get("p2_url")
            return (
                str(u1).strip() if pd.notna(u1) and str(u1).strip() else None,
                str(u2).strip() if pd.notna(u2) and str(u2).strip() else None,
            )
    return None, None


def _force_refresh_live_match(match: dict) -> dict:
    """Re-scrape profils TE, re-résout stats identité et recalcule la prédiction ML pour un match."""
    out = dict(match)
    p1_name = str(out.get("player1") or "").strip()
    p2_name = str(out.get("player2") or "").strip()
    if not p1_name or not p2_name:
        return out

    if not out.get("p1_url") or not out.get("p2_url"):
        u1, u2 = _lookup_prematch_urls(p1_name, p2_name)
        if u1:
            out["p1_url"] = u1
        if u2:
            out["p2_url"] = u2

    tour = str(out.get("tour") or out.get("category") or "").strip().upper()
    if tour not in ("ATP", "WTA"):
        tour = _match_circuit(out) or "ATP"
    p1_url = out.get("p1_url")
    p2_url = out.get("p2_url")

    _purge_match_player_caches(out)
    try:
        _compute_live_advanced_signals.clear()
    except Exception:
        pass

    profiles: dict = {}
    if ENABLE_PROFILE_SCRAPE:
        for url in (p1_url, p2_url):
            if url:
                profiles[url] = profile_scraper.scrape_profile(url, force_refresh=True)

    def _resolve(name: str, url: str | None):
        meta = stats_engine.get_player_id_meta(
            name,
            source_name="flashscore",
            source_url=url,
            tour_hint=tour,
        )
        pid = meta.get("player_id")
        _clear_stats_engine_player_cache(pid, tour)
        st_out = dict(stats_engine.get_player_stats(pid, name, tour_hint=tour))
        st_out["_pid"] = pid
        prof = profiles.get(url) if url else None
        _ref_live = None
        try:
            if out.get("date") is not None:
                _ref_live = str(pd.Timestamp(out.get("date")).date())
        except Exception:
            _ref_live = str(out.get("date") or "")[:10] or None
        frm, fat = _merge_live_profile(
            st_out, prof, tour_hint=tour, ref_date=_ref_live
        )
        st_out.pop("_pid", None)
        store_state = _lookup_player_feature_state(name, tour, pid)
        mq = _store_match_quality(store_state)
        sp = _store_speed_profile(store_state)
        bp = float((store_state or {}).get("bp_resilience") or 0.5) if store_state else None
        if mq is None:
            try:
                mq = stats_engine.get_recent_match_quality(
                    pid, tour_hint=tour, ref_date=_ref_live
                )
            except Exception:
                mq = {"wins_last7d": 0, "three_setters_last14d": 0, "last_round_reached": 0}
        if sp is None:
            try:
                sp = stats_engine.get_speed_profile(pid, tour_hint=tour)
            except Exception:
                sp = {"speed_affinity": 0.0, "speed_performance_delta": 0.0, "samples": 0}
        if bp is None:
            try:
                bp = float(stats_engine.get_bp_resilience(pid, tour_hint=tour))
            except Exception:
                bp = 0.5
        return pid, st_out, frm, fat, mq, sp, bp, bool(prof)

    p1_id, p1_stats, p1_form, p1_fatigue, p1_mq, p1_sp, p1_bp, p1_prof_ok = _resolve(
        p1_name, p1_url
    )
    p2_id, p2_stats, p2_form, p2_fatigue, p2_mq, p2_sp, p2_bp, p2_prof_ok = _resolve(
        p2_name, p2_url
    )

    surface = out.get("surface") or _resolve_match_surface(out)
    _mdate = out.get("date")
    _ref_iso = None
    try:
        if _mdate is not None:
            _ref_iso = str(pd.Timestamp(_mdate).date())
    except Exception:
        _ref_iso = None

    adv_signals = (
        _neutral_live_adv_signals()
        if not ENABLE_ADV_SIGNALS
        else _compute_live_advanced_signals(
            p1_name,
            p2_name,
            surface,
            tour_hint=tour,
            tournament_name=str(out.get("tournament") or ""),
            ref_dt_iso=_ref_iso,
        )
    )

    p1_prof_pred = profiles.get(p1_url) if p1_url else None
    p2_prof_pred = profiles.get(p2_url) if p2_url else None
    try:
        p1_days_tml_sig = float(adv_signals.get("p1_days") or 7.0)
    except Exception:
        p1_days_tml_sig = 7.0
    try:
        p2_days_tml_sig = float(adv_signals.get("p2_days") or 7.0)
    except Exception:
        p2_days_tml_sig = 7.0
    p1_days_eff, p1_te_inact = _blend_inactivity_days_with_te(
        adv_signals.get("p1_days"), p1_prof_pred, _ref_iso
    )
    p2_days_eff, p2_te_inact = _blend_inactivity_days_with_te(
        adv_signals.get("p2_days"), p2_prof_pred, _ref_iso
    )

    r1_def, r2_def = 0.0, 0.0
    if _mdate:
        try:
            r1_def = _defending_ratio_cached(
                ml_model.db_path,
                p1_id,
                out.get("tournament"),
                _mdate,
                float(p1_stats.get("pts") or 0),
                tour,
            )
            r2_def = _defending_ratio_cached(
                ml_model.db_path,
                p2_id,
                out.get("tournament"),
                _mdate,
                float(p2_stats.get("pts") or 0),
                tour,
            )
        except Exception:
            r1_def, r2_def = 0.0, 0.0

    if FAST_LIVE_MODE:
        h2h = {"p1_wins": 0, "p2_wins": 0}
    else:
        h2h = stats_engine.get_h2h(p1_id, p2_id, p1_name, p2_name, tour_hint=tour)

    wx_temp_c, wx_humidity = None, None
    try:
        wx_conn = sqlite3.connect(ml_model.db_path)
        from scripts.weather_open_meteo import ensure_weather_schema, weather_for_tournament_day
        from scripts.surface_speed import infer_outdoor as _infer_outdoor_wx

        ensure_weather_schema(wx_conn)
        if _mdate and _infer_outdoor_wx(None, out.get("tournament")):
            d_wx = pd.Timestamp(_mdate).date()
            wx_temp_c, wx_humidity = weather_for_tournament_day(
                out.get("tournament"),
                d_wx,
                conn=wx_conn,
                prefer_forecast_if_today=True,
            )
        wx_conn.close()
    except Exception:
        wx_temp_c, wx_humidity = None, None

    try:
        preds = ml_model.predict_match(
            surface=surface,
            p1_name=p1_name,
            p2_name=p2_name,
            p1_rank=p1_stats["rank"],
            p2_rank=p2_stats["rank"],
            p1_age=p1_stats["age"],
            p2_age=p2_stats["age"],
            p1_ht=p1_stats["ht"],
            p2_ht=p2_stats["ht"],
            p1_pts=p1_stats["pts"],
            p2_pts=p2_stats["pts"],
            p1_id=p1_id,
            p2_id=p2_id,
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
            p1_days_since_last_match=p1_days_eff,
            p2_days_since_last_match=p2_days_eff,
            p1_te_inactivity_blend_used=p1_te_inact,
            p2_te_inactivity_blend_used=p2_te_inact,
            p1_days_inactivity_pre_te=p1_days_tml_sig,
            p2_days_inactivity_pre_te=p2_days_tml_sig,
            p1_wins_last7d=p1_mq.get("wins_last7d", 0),
            p2_wins_last7d=p2_mq.get("wins_last7d", 0),
            p1_three_setters_last14d=p1_mq.get("three_setters_last14d", 0),
            p2_three_setters_last14d=p2_mq.get("three_setters_last14d", 0),
            p1_last_round_reached=p1_mq.get("last_round_reached", 0),
            p2_last_round_reached=p2_mq.get("last_round_reached", 0),
            p1_speed_affinity=p1_sp.get("speed_affinity", 0.0),
            p2_speed_affinity=p2_sp.get("speed_affinity", 0.0),
            p1_speed_performance_delta=p1_sp.get("speed_performance_delta", 0.0),
            p2_speed_performance_delta=p2_sp.get("speed_performance_delta", 0.0),
            p1_bp_resilience=p1_bp,
            p2_bp_resilience=p2_bp,
            tournament_name=str(out.get("tournament") or ""),
            tour=tour,
            match_date=_mdate,
            p1_points_def_ratio=r1_def,
            p2_points_def_ratio=r2_def,
            humidity_pct=wx_humidity,
            temp_c=wx_temp_c,
            p1_tac_ace=adv_signals.get("p1_tac_ace"),
            p1_tac_f1_pct=adv_signals.get("p1_tac_f1_pct"),
            p1_tac_bp_saved_pct=adv_signals.get("p1_tac_bp_saved_pct"),
            p1_tac_hold_pct=adv_signals.get("p1_tac_hold_pct"),
            p2_tac_ace=adv_signals.get("p2_tac_ace"),
            p2_tac_f1_pct=adv_signals.get("p2_tac_f1_pct"),
            p2_tac_bp_saved_pct=adv_signals.get("p2_tac_bp_saved_pct"),
            p2_tac_hold_pct=adv_signals.get("p2_tac_hold_pct"),
            p1_travel_penalty_index=adv_signals.get("p1_travel_penalty_index"),
            p2_travel_penalty_index=adv_signals.get("p2_travel_penalty_index"),
            p1_clutch52=adv_signals.get("p1_clutch52"),
            p2_clutch52=adv_signals.get("p2_clutch52"),
            p1_minutes_played_last7d=adv_signals.get("p1_minutes_played_last7d"),
            p2_minutes_played_last7d=adv_signals.get("p2_minutes_played_last7d"),
            p1_tb_win_pct_52w=adv_signals.get("p1_tb_win_pct_52w"),
            p2_tb_win_pct_52w=adv_signals.get("p2_tb_win_pct_52w"),
        )
        out["true_odd_p1"] = preds["p1_true_odd"]
        out["true_odd_p2"] = preds["p2_true_odd"]
        out["confidence"] = preds.get("confidence")
        out["calibration_used"] = preds.get("calibration_used", "Globale")
        out["feature_snapshot"] = preds.get("feature_snapshot", {}) or {}
        out["top_features"] = preds.get("top_features", []) or []
        out["segment_calibration_key"] = str(preds.get("segment_calibration_key", "") or "")
    except Exception:
        pass

    out["p1_stats"] = p1_stats
    out["p2_stats"] = p2_stats
    out["p1_form"] = _enrich_form_record(p1_form)
    out["p2_form"] = _enrich_form_record(p2_form)
    out["p1_fatigue"] = p1_fatigue
    out["p2_fatigue"] = p2_fatigue
    out["p1_match_quality"] = p1_mq
    out["p2_match_quality"] = p2_mq
    out["h2h"] = h2h
    out["p1_profile_loaded"] = p1_prof_ok
    out["p2_profile_loaded"] = p2_prof_ok
    out["brier_segment_key"] = resolve_match_brier_segment_key(
        ml_model,
        tour=tour,
        surface=surface,
        tournament=out.get("tournament"),
    )
    return out


def _build_live_matches_core(
    csv_path: str,
    _prematch_mtime: float,
    _profile_cache_schema: int,
    _model_mtime: float = 0.0,
    _engines_cache_version: int = 0,
    *,
    identity_workers: int | None = None,
    build_mode: str = "full",
    base_matches: list | None = None,
):
    """Cache invalidation keys:
    - _prematch_mtime    : nouveau scrape de cotes
    - _profile_cache_schema : nouveau schéma profils
    - _model_mtime       : nouveau modèle ML entraîné (mtime du fichier .pkl)
    - _engines_cache_version : bump manuel après changement d'API stats/ML
    - build_mode ``preview`` : affichage rapide (stats cache + ML, sans scrape TE ni signaux lourds)
    - build_mode ``enrich`` : repart du snapshot existant ; ne refait pas les lignes déjà ``full``
    """
    _bm = str(build_mode or "full").strip().lower()
    _enrich_build = _bm == "enrich"
    _fast_build = (not _enrich_build) and (
        _bm == "preview"
        or (LIVE_BUILD_FAST_PREVIEW and _bm not in ("full", "enrich"))
    )
    base_by_key: dict[tuple[str, str, str, str], dict] = {}
    if _enrich_build and base_matches:
        for _bm0 in base_matches:
            if not isinstance(_bm0, dict):
                continue
            _bk = _match_odds_lookup_key(
                _bm0.get("player1"),
                _bm0.get("player2"),
                _bm0.get("tournament"),
                _bm0.get("prematch_id"),
            )
            base_by_key[_bk] = dict(_bm0)
        if PERF_LOG_LIVE_BUILD:
            print(
                f"[live-build] enrich: {len(base_by_key)} matchs en base",
                flush=True,
            )
    t0 = time.perf_counter()
    t_last = t0

    def _mark(step: str):
        nonlocal t_last
        try:
            advance_live_build_progress(step)
        except Exception:
            pass
        if PERF_LOG_LIVE_BUILD:
            now = time.perf_counter()
            print(
                f"[live-build] {step}: +{(now - t_last):.2f}s (total {(now - t0):.2f}s)",
                flush=True,
            )
            t_last = now

    if not csv_path or not os.path.isfile(csv_path):
        return []
    df = _load_prematch_df_for_live(csv_path)
    _mark("read_csv")
    _mark("filter_doubles")
    _mark("filter_major")
    _mark("filter_today_tomorrow")
    _mark("filter_upcoming")
    if df.empty:
        return []
    
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
    # Garde-fou perf (journée) : limite les scrapes réseau, pas le cache disque.
    # Pipeline matin (BETTINGHUD_MORNING_BUILD=1) : aucune limite.
    _morning_build = _env_flag("BETTINGHUD_MORNING_BUILD", False)
    max_profile_fetch = max(
        0 if _fast_build else 1,
        min(1000, int(os.getenv("BETTINGHUD_MAX_PROFILE_FETCH", "0" if _fast_build else "100"))),
    )
    profile_fetch_workers = max(1, min(8, int(os.getenv("BETTINGHUD_PROFILE_SCRAPE_WORKERS", "6"))))
    profiles_by_url = {}
    if ENABLE_PROFILE_SCRAPE and not _fast_build and (_morning_build or max_profile_fetch > 0):
        targets_scrape: list[str] = []
        for u in urls_to_fetch:
            cached_prof = profile_scraper._load_from_cache(
                u, max_age_hours=PROFILE_CACHE_MAX_AGE_HOURS
            )
            if cached_prof:
                profiles_by_url[u] = cached_prof
            else:
                targets_scrape.append(u)
        if not _morning_build and len(targets_scrape) > max_profile_fetch:
            targets_scrape = targets_scrape[:max_profile_fetch]

        def _scrape_u(url: str):
            return url, profile_scraper.scrape_profile(url)

        if len(targets_scrape) == 1:
            u = targets_scrape[0]
            prof = profile_scraper.scrape_profile(u)
            if prof:
                profiles_by_url[u] = prof
        elif len(targets_scrape) > 1:
            with ThreadPoolExecutor(max_workers=profile_fetch_workers) as pool:
                futs = [pool.submit(_scrape_u, u) for u in targets_scrape]
                for fut in as_completed(futs):
                    try:
                        u, prof = fut.result()
                        if prof:
                            profiles_by_url[u] = prof
                    except Exception:
                        pass
        if PERF_LOG_LIVE_BUILD and urls_to_fetch:
            print(
                f"[live-build] profils TE: {len(profiles_by_url)} chargés / "
                f"{len(urls_to_fetch)} URLs ({len(targets_scrape)} scrape réseau)",
                flush=True,
            )
    _mark("profile_scrape")

    # tourney_id connu du CSV (Flashscore / enrichissement) pour exclure l'épreuve en cours du signal last_round.
    ref_date_by_player: dict[str, str] = {}
    for _, row in df.iterrows():
        _md_row = row.get("date") or (
            str(row["scraped_at"])[:10] if row.get("scraped_at") is not None else None
        )
        if _md_row:
            try:
                _md_iso = str(pd.Timestamp(_md_row).date())
            except Exception:
                _md_iso = str(_md_row)[:10]
            for _pn in (str(row.get("player1") or "").strip(), str(row.get("player2") or "").strip()):
                if _pn:
                    prev = ref_date_by_player.get(_pn)
                    if prev is None or _md_iso > prev:
                        ref_date_by_player[_pn] = _md_iso

    exclude_tourney_id_by_player = {}
    if "tourney_id" in df.columns:
        for _, row in df.iterrows():
            tid_raw = row.get("tourney_id")
            if tid_raw is None:
                continue
            try:
                if isinstance(tid_raw, float) and pd.isna(tid_raw):
                    continue
            except Exception:
                pass
            p1n = str(row.get("player1") or "").strip()
            p2n = str(row.get("player2") or "").strip()
            for pname in (p1n, p2n):
                if pname and pname not in exclude_tourney_id_by_player:
                    exclude_tourney_id_by_player[pname] = tid_raw

    # Pré-calcul identité/stats par joueur (évite recalcul coûteux à chaque ligne)
    unique_players = pd.concat([df["player1"], df["player2"]]).dropna().astype(str).str.strip().unique().tolist()
    player_url_map = {}
    tour_by_player = {}
    n_rows_total = len(df)
    for row_i, (_, row) in enumerate(df.iterrows(), start=1):
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
    cache_hits = 0
    if FAST_LIVE_MODE:
        # Mode ultra-rapide: on évite les résolutions identité/stats (coût principal CPU).
        for pname in unique_players:
            pid_by_name[pname] = None
            stats_by_name[pname] = {
                "rank": 100,
                "age": 25,
                "ht": 185,
                "pts": 1000,
                "hand": "U",
                "stats_source": "fast_default",
                "stats_reference_date": None,
            }
    elif _fast_build:
        # Preview : cache joueur uniquement (pas de résolution SQLite lourde par joueur).
        cache_key_by_player = {}
        cache_keys = []
        for pname in unique_players:
            ck = "||".join(
                [
                    str(pname),
                    str(player_url_map.get(pname) or ""),
                    str(tour_by_player.get(pname) or ""),
                ]
            )
            cache_keys.append(ck)
            cache_key_by_player[pname] = ck
        persistent_cache = _load_live_player_cache(cache_keys, _LIVE_PLAYER_CACHE_TTL_SEC)
        for pname in unique_players:
            cached = persistent_cache.get(cache_key_by_player.get(pname, ""))
            if cached is not None:
                pid_by_name[pname] = cached.get("pid")
                stats_by_name[pname] = dict(cached.get("stats") or _DEFAULT_LIVE_PLAYER_STATS)
                cache_hits += 1
            else:
                pid_by_name[pname] = None
                stats_by_name[pname] = dict(_DEFAULT_LIVE_PLAYER_STATS)
    else:
        cache_keys = []
        cache_key_by_player = {}
        for pname in unique_players:
            ck = "||".join(
                [
                    str(pname),
                    str(player_url_map.get(pname) or ""),
                    str(tour_by_player.get(pname) or ""),
                ]
            )
            cache_keys.append(ck)
            cache_key_by_player[pname] = ck
        persistent_cache = _load_live_player_cache(cache_keys, _LIVE_PLAYER_CACHE_TTL_SEC)
        cache_updates = {}

        def _resolve_one_player(pname: str):
            cache_key = cache_key_by_player.get(pname, "")
            cached = persistent_cache.get(cache_key)
            if cached is not None:
                return pname, cached.get("pid"), dict(cached.get("stats") or {}), True, cache_key
            th = tour_by_player.get(pname)
            meta = stats_engine.get_player_id_meta(
                pname,
                source_name="flashscore",
                source_url=player_url_map.get(pname),
                tour_hint=th,
            )
            pid = meta.get("player_id")
            st_out = stats_engine.get_player_stats(pid, pname, tour_hint=th)
            return pname, pid, st_out, False, cache_key

        _iw = (
            int(identity_workers)
            if identity_workers is not None
            else IDENTITY_RESOLVE_WORKERS
        )
        workers = min(max(1, _iw), max(1, len(unique_players)))
        if workers <= 1 or len(unique_players) <= 2:
            for pname in unique_players:
                pname2, pid, st_out, from_cache, cache_key = _resolve_one_player(pname)
                pid_by_name[pname2] = pid
                stats_by_name[pname2] = st_out
                if from_cache:
                    cache_hits += 1
                else:
                    cache_updates[cache_key] = {"pid": pid, "stats": dict(st_out)}
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_resolve_one_player, pn): pn for pn in unique_players}
                for fut in as_completed(futures):
                    pname2, pid, st_out, from_cache, cache_key = fut.result()
                    pid_by_name[pname2] = pid
                    stats_by_name[pname2] = st_out
                    if from_cache:
                        cache_hits += 1
                    else:
                        cache_updates[cache_key] = {"pid": pid, "stats": dict(st_out)}
        _upsert_live_player_cache(cache_updates)
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
    feature_cache_hits = 0
    feature_cache_key_by_player = {}
    try:
        _feature_store_mtime_key = str(round(float(os.path.getmtime(FEATURE_STORE_PATH)), 3))
    except OSError:
        _feature_store_mtime_key = "missing"
    for pname in unique_players:
        feature_cache_key_by_player[pname] = "||".join(
            [
                str(pname),
                str(tour_by_player.get(pname) or ""),
                str(pid_by_name.get(pname) or ""),
                str(player_url_map.get(pname) or ""),
                str(_profile_cache_schema),
                str(exclude_tourney_id_by_player.get(pname) or ""),
                str(ref_date_by_player.get(pname) or ""),
                _feature_store_mtime_key,
            ]
        )
    if _fast_build:
        for pname in unique_players:
            prepared_stats_by_name[pname] = dict(
                stats_by_name.get(pname) or _DEFAULT_LIVE_PLAYER_STATS
            )
            form_by_name[pname] = dict(_NEUTRAL_LIVE_FORM)
            fatigue_by_name[pname] = dict(_NEUTRAL_LIVE_FATIGUE)
            match_quality_by_name[pname] = dict(_NEUTRAL_LIVE_MATCH_QUALITY)
            speed_profile_by_name[pname] = dict(_NEUTRAL_LIVE_SPEED_PROFILE)
            bp_resilience_by_name[pname] = 0.5
    else:
        feature_cache = _load_live_player_features_cache(
            list(feature_cache_key_by_player.values()),
            _LIVE_PLAYER_FEATURES_CACHE_TTL_SEC,
        )
        feature_cache_updates = {}

        def _prepare_one_player_features(pname: str):
            _fkey = feature_cache_key_by_player.get(pname, "")
            _cached_feat = feature_cache.get(_fkey)
            if _cached_feat is not None:
                return (
                    pname,
                    dict(
                        _cached_feat.get("prepared_stats")
                        or {
                            **_DEFAULT_LIVE_PLAYER_STATS,
                            "stats_source": "cache_default",
                        }
                    ),
                    dict(_cached_feat.get("form") or _NEUTRAL_LIVE_FORM),
                    dict(_cached_feat.get("fatigue") or _NEUTRAL_LIVE_FATIGUE),
                    dict(_cached_feat.get("match_quality") or _NEUTRAL_LIVE_MATCH_QUALITY),
                    dict(_cached_feat.get("speed_profile") or _NEUTRAL_LIVE_SPEED_PROFILE),
                    float(_cached_feat.get("bp_resilience", 0.5) or 0.5),
                    True,
                    None,
                )
            base = dict(
                stats_by_name.get(pname) or {
                    **_DEFAULT_LIVE_PLAYER_STATS,
                    "stats_source": "no_ranking_source",
                }
            )
            base["_pid"] = pid_by_name.get(pname)
            prof = (
                profiles_by_url.get(player_url_map.get(pname))
                if ENABLE_PROFILE_SCRAPE
                else None
            )
            frm, fat = _merge_live_profile(
                base,
                prof,
                tour_hint=tour_by_player.get(pname),
                ref_date=ref_date_by_player.get(pname),
            )
            base.pop("_pid", None)
            store_state = _lookup_player_feature_state(
                pname, tour_by_player.get(pname), pid_by_name.get(pname)
            )
            mq = _store_match_quality(store_state)
            sp = _store_speed_profile(store_state)
            bp = float((store_state or {}).get("bp_resilience") or 0.5) if store_state else None
            if mq is None:
                try:
                    mq = stats_engine.get_recent_match_quality(
                        pid_by_name.get(pname),
                        tour_hint=tour_by_player.get(pname),
                        exclude_tourney_id=exclude_tourney_id_by_player.get(pname),
                        ref_date=ref_date_by_player.get(pname),
                    )
                except Exception:
                    mq = dict(_NEUTRAL_LIVE_MATCH_QUALITY)
            if sp is None:
                try:
                    sp = stats_engine.get_speed_profile(
                        pid_by_name.get(pname), tour_hint=tour_by_player.get(pname)
                    )
                except Exception:
                    sp = dict(_NEUTRAL_LIVE_SPEED_PROFILE)
            if bp is None:
                try:
                    bp = float(
                        stats_engine.get_bp_resilience(
                            pid_by_name.get(pname), tour_hint=tour_by_player.get(pname)
                        )
                    )
                except Exception:
                    bp = 0.5
            cache_upd = {
                "prepared_stats": dict(base),
                "form": dict(frm),
                "fatigue": dict(fat),
                "match_quality": dict(mq),
                "speed_profile": dict(sp),
                "bp_resilience": bp,
            }
            return pname, base, frm, fat, mq, sp, bp, False, (_fkey, cache_upd)

        _feat_workers = max(
            1,
            min(
                12,
                int(os.getenv("BETTINGHUD_PLAYER_FEATURES_WORKERS", "8")),
                len(unique_players),
            ),
        )
        if _feat_workers <= 1 or len(unique_players) <= 2:
            for pname in unique_players:
                (
                    pname2,
                    prep,
                    frm,
                    fat,
                    mq,
                    sp,
                    bp,
                    from_cache,
                    cache_upd,
                ) = _prepare_one_player_features(pname)
                prepared_stats_by_name[pname2] = prep
                form_by_name[pname2] = _enrich_form_record(frm)
                fatigue_by_name[pname2] = fat
                match_quality_by_name[pname2] = mq
                speed_profile_by_name[pname2] = sp
                bp_resilience_by_name[pname2] = bp
                if from_cache:
                    feature_cache_hits += 1
                elif cache_upd:
                    feature_cache_updates[cache_upd[0]] = cache_upd[1]
        else:
            with ThreadPoolExecutor(max_workers=_feat_workers) as pool:
                futs = [
                    pool.submit(_prepare_one_player_features, pn) for pn in unique_players
                ]
                for fut in as_completed(futs):
                    (
                        pname2,
                        prep,
                        frm,
                        fat,
                        mq,
                        sp,
                        bp,
                        from_cache,
                        cache_upd,
                    ) = fut.result()
                    prepared_stats_by_name[pname2] = prep
                    form_by_name[pname2] = _enrich_form_record(frm)
                    fatigue_by_name[pname2] = fat
                    match_quality_by_name[pname2] = mq
                    speed_profile_by_name[pname2] = sp
                    bp_resilience_by_name[pname2] = bp
                    if from_cache:
                        feature_cache_hits += 1
                    elif cache_upd:
                        feature_cache_updates[cache_upd[0]] = cache_upd[1]
        if feature_cache_updates:
            _upsert_live_player_features_cache(feature_cache_updates)
    _mark("prepare_player_forms")
    if PERF_LOG_LIVE_BUILD:
        print(
            f"[live-build] player_features cache hits: {feature_cache_hits}/{len(unique_players)}",
            flush=True,
        )

    h2h_cache = {}
    defending_cache: dict[tuple, float] = {}
    wx_conn = None
    weather_for_tournament_day = None
    _infer_outdoor_wx = None
    if not _fast_build and not FULL_BUILD_SKIP_WEATHER:
        try:
            wx_conn = sqlite3.connect(ml_model.db_path)
            from scripts.weather_open_meteo import ensure_weather_schema, weather_for_tournament_day
            from scripts.surface_speed import infer_outdoor as _infer_outdoor_wx

            ensure_weather_schema(wx_conn)
        except Exception:
            if wx_conn is not None:
                try:
                    wx_conn.close()
                except Exception:
                    pass
            wx_conn = None
            weather_for_tournament_day = None
            _infer_outdoor_wx = None

    if not _fast_build and not FULL_BUILD_SKIP_DEFENDING:
        def_keys: set[tuple] = set()
        for _, row in df.iterrows():
            _mdate = row.get("date") or (
                str(row["scraped_at"])[:10] if row.get("scraped_at") is not None else None
            )
            if not _mdate:
                continue
            match_tour = tour_by_player.get(str(row.get("player1") or "").strip()) or tour_by_player.get(
                str(row.get("player2") or "").strip()
            )
            for pname in (str(row.get("player1") or "").strip(), str(row.get("player2") or "").strip()):
                pid = pid_by_name.get(pname)
                stt = prepared_stats_by_name.get(pname) or stats_by_name.get(pname) or {}
                def_keys.add(
                    (
                        str(pid or ""),
                        str(row.get("tournament") or ""),
                        str(_mdate)[:10],
                        float(stt.get("pts") or 0),
                        str(match_tour or "ATP"),
                    )
                )

        def _def_one(k: tuple) -> tuple[tuple, float]:
            pid_s, tname, mdate, pts, mtour = k
            return k, _defending_ratio_cached(
                ml_model.db_path,
                pid_s or None,
                tname,
                mdate,
                pts,
                mtour,
            )

        _def_workers = min(8, max(1, len(def_keys)))
        if _def_workers <= 1 or len(def_keys) <= 2:
            for k in def_keys:
                defending_cache[k] = _def_one(k)[1]
        else:
            with ThreadPoolExecutor(max_workers=_def_workers) as pool:
                for k, val in pool.map(_def_one, def_keys):
                    defending_cache[k] = val

    adv_signals_by_match: dict[tuple[str, str, str], dict] = {}
    if not _fast_build and ENABLE_ADV_SIGNALS and not FULL_BUILD_SKIP_ADV_SIGNALS:
        adv_jobs: dict[tuple[str, str, str], tuple[str, str, str, str, str, str | None]] = {}
        for _, row in df.iterrows():
            p1n = str(row.get("player1") or "").strip()
            p2n = str(row.get("player2") or "").strip()
            if not p1n or not p2n:
                continue
            surface0 = _resolve_match_surface(row)
            mtour = tour_by_player.get(p1n) or tour_by_player.get(p2n) or "ATP"
            mdate0 = row.get("date") or (
                str(row["scraped_at"])[:10] if row.get("scraped_at") is not None else None
            )
            try:
                ref0 = str(pd.Timestamp(mdate0).date()) if mdate0 is not None else None
            except Exception:
                ref0 = None
            k = _match_odds_lookup_key(
                p1n, p2n, row.get("tournament"), _row_prematch_id(row)
            )
            adv_jobs[k] = (
                p1n,
                p2n,
                surface0,
                str(mtour or "ATP"),
                str(row.get("tournament", "") or ""),
                ref0,
            )

        def _adv_one(item: tuple[tuple[str, str, str, str], tuple[str, str, str, str, str, str | None]]):
            k, args = item
            try:
                return k, _compute_live_advanced_signals(*args)
            except Exception:
                return k, _neutral_live_adv_signals()

        adv_workers = max(1, min(8, int(os.getenv("BETTINGHUD_ADV_SIGNAL_WORKERS", "6")), len(adv_jobs) or 1))
        if adv_jobs:
            if adv_workers <= 1 or len(adv_jobs) <= 2:
                for item in adv_jobs.items():
                    k, val = _adv_one(item)
                    adv_signals_by_match[k] = val
            else:
                with ThreadPoolExecutor(max_workers=adv_workers) as pool:
                    for k, val in pool.map(_adv_one, adv_jobs.items()):
                        adv_signals_by_match[k] = val
            if PERF_LOG_LIVE_BUILD:
                print(
                    f"[live-build] adv_signals precompute: {len(adv_signals_by_match)} matchs ({adv_workers} workers)",
                    flush=True,
                )

    matches = []
    _enrich_kept_full = 0
    _enrich_delta_odds_only = 0
    n_rows_total = len(df)
    for row_i, (_, row) in enumerate(df.iterrows(), start=1):
        p1_name = str(row["player1"]).strip()
        p2_name = str(row["player2"]).strip()
        _snap_key = _match_odds_lookup_key(
            p1_name, p2_name, row.get("tournament"), _row_prematch_id(row)
        )

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

        if _enrich_build and _snap_key in base_by_key:
            _existing = base_by_key.pop(_snap_key)
            if (
                _existing_prediction_matches_row(_existing, row, p1_id, p2_id)
                and not _match_needs_full_repredict(
                    _existing, p1_stats, p2_stats, p1_id, p2_id
                )
            ):
                _em = dict(_existing)
                if isinstance(_em.get("feature_snapshot"), dict):
                    _em["feature_snapshot"] = copy.deepcopy(_em["feature_snapshot"])
                try:
                    _o1 = float(row.get("odd_p1") or 0.0)
                    _o2 = float(row.get("odd_p2") or 0.0)
                    if _o1 > 1.0 and _o2 > 1.0:
                        _em["odd_p1"] = _o1
                        _em["odd_p2"] = _o2
                except (TypeError, ValueError):
                    pass
                matches.append(_em)
                _enrich_delta_odds_only += 1
                continue

        surface = _resolve_match_surface(row)
        
        true_odd_p1 = 2.0
        true_odd_p2 = 2.0
        confidence = None
        calibration_used = "Globale"
        feature_snapshot = {}
        top_features = []
        
        # Advanced Stats
        match_tour = tour_by_player.get(p1_name) or tour_by_player.get(p2_name)
        segment_calibration_key = ""
        _mdate = row.get("date") or (
            str(row["scraped_at"])[:10] if row.get("scraped_at") is not None else None
        )
        r1_def, r2_def = 0.0, 0.0
        if _mdate and not _fast_build and not FULL_BUILD_SKIP_DEFENDING:
            try:
                _md = str(_mdate)[:10]
                k1 = (
                    str(p1_id or ""),
                    str(row.get("tournament") or ""),
                    _md,
                    float(p1_stats.get("pts") or 0),
                    str(match_tour or "ATP"),
                )
                k2 = (
                    str(p2_id or ""),
                    str(row.get("tournament") or ""),
                    _md,
                    float(p2_stats.get("pts") or 0),
                    str(match_tour or "ATP"),
                )
                r1_def = float(defending_cache.get(k1, 0.0))
                r2_def = float(defending_cache.get(k2, 0.0))
            except Exception:
                r1_def, r2_def = 0.0, 0.0
        h2h_key = (str(p1_id), str(p2_id), p1_name, p2_name, match_tour)
        if FAST_LIVE_MODE or _fast_build:
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
        p1_url = player_url_map.get(p1_name)
        p2_url = player_url_map.get(p2_name)

        wx_temp_c, wx_humidity = None, None
        if (
            wx_conn is not None
            and weather_for_tournament_day is not None
            and _infer_outdoor_wx is not None
            and _mdate
            and not FULL_BUILD_SKIP_WEATHER
        ):
            try:
                if _infer_outdoor_wx(row.get("indoor"), row.get("tournament")):
                    d_wx = pd.Timestamp(_mdate).date()
                    wx_key = (str(row.get("tournament") or ""), d_wx)
                    if wx_key in _WEATHER_DAY_CACHE:
                        wx_temp_c, wx_humidity = _WEATHER_DAY_CACHE[wx_key]
                    else:
                        wx_temp_c, wx_humidity = weather_for_tournament_day(
                            row.get("tournament"),
                            d_wx,
                            conn=wx_conn,
                            prefer_forecast_if_today=True,
                        )
                        _WEATHER_DAY_CACHE[wx_key] = (wx_temp_c, wx_humidity)
            except Exception:
                wx_temp_c, wx_humidity = None, None

        try:
            _ref_iso = None
            try:
                if _mdate is not None:
                    _ref_iso = str(pd.Timestamp(_mdate).date())
            except Exception:
                _ref_iso = None
            adv_signals = (
                _neutral_live_adv_signals()
                if (not ENABLE_ADV_SIGNALS or _fast_build or FULL_BUILD_SKIP_ADV_SIGNALS)
                else adv_signals_by_match.get(_snap_key)
                or _compute_live_advanced_signals(
                    p1_name,
                    p2_name,
                    surface,
                    tour_hint=match_tour or "ATP",
                    tournament_name=str(row.get("tournament", "") or ""),
                    ref_dt_iso=_ref_iso,
                )
            )
            p1_prof_pred = profiles_by_url.get(p1_url) if p1_url else None
            p2_prof_pred = profiles_by_url.get(p2_url) if p2_url else None
            try:
                p1_days_tml_sig = float(adv_signals.get("p1_days") or 7.0)
            except Exception:
                p1_days_tml_sig = 7.0
            try:
                p2_days_tml_sig = float(adv_signals.get("p2_days") or 7.0)
            except Exception:
                p2_days_tml_sig = 7.0
            p1_days_eff, p1_te_inact = _blend_inactivity_days_with_te(
                adv_signals.get("p1_days"), p1_prof_pred, _ref_iso
            )
            p2_days_eff, p2_te_inact = _blend_inactivity_days_with_te(
                adv_signals.get("p2_days"), p2_prof_pred, _ref_iso
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
                p1_days_since_last_match=p1_days_eff,
                p2_days_since_last_match=p2_days_eff,
                p1_te_inactivity_blend_used=p1_te_inact,
                p2_te_inactivity_blend_used=p2_te_inact,
                p1_days_inactivity_pre_te=p1_days_tml_sig,
                p2_days_inactivity_pre_te=p2_days_tml_sig,
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
                match_date=_mdate,
                p1_points_def_ratio=r1_def,
                p2_points_def_ratio=r2_def,
                humidity_pct=wx_humidity,
                temp_c=wx_temp_c,
                p1_tac_ace=adv_signals.get("p1_tac_ace"),
                p1_tac_f1_pct=adv_signals.get("p1_tac_f1_pct"),
                p1_tac_bp_saved_pct=adv_signals.get("p1_tac_bp_saved_pct"),
                p1_tac_hold_pct=adv_signals.get("p1_tac_hold_pct"),
                p2_tac_ace=adv_signals.get("p2_tac_ace"),
                p2_tac_f1_pct=adv_signals.get("p2_tac_f1_pct"),
                p2_tac_bp_saved_pct=adv_signals.get("p2_tac_bp_saved_pct"),
                p2_tac_hold_pct=adv_signals.get("p2_tac_hold_pct"),
                p1_travel_penalty_index=adv_signals.get("p1_travel_penalty_index"),
                p2_travel_penalty_index=adv_signals.get("p2_travel_penalty_index"),
                p1_clutch52=adv_signals.get("p1_clutch52"),
                p2_clutch52=adv_signals.get("p2_clutch52"),
                p1_minutes_played_last7d=adv_signals.get("p1_minutes_played_last7d"),
                p2_minutes_played_last7d=adv_signals.get("p2_minutes_played_last7d"),
                p1_tb_win_pct_52w=adv_signals.get("p1_tb_win_pct_52w"),
                p2_tb_win_pct_52w=adv_signals.get("p2_tb_win_pct_52w"),
            )
            true_odd_p1 = preds['p1_true_odd']
            true_odd_p2 = preds['p2_true_odd']
            confidence = preds.get("confidence", None)
            calibration_used = preds.get("calibration_used", "Globale")
            feature_snapshot = preds.get("feature_snapshot", {}) or {}
            top_features = preds.get("top_features", []) or []
            segment_calibration_key = str(preds.get("segment_calibration_key", "") or "")
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

        p1_profile_loaded = bool(profiles_by_url.get(p1_url)) if p1_url else False
        p2_profile_loaded = bool(profiles_by_url.get(p2_url)) if p2_url else False

        _ev_unreliable, _ev_alert, _ev_max_gap = _match_snapshot_quality_flags(
            {
                "odd_p1": odd_p1,
                "odd_p2": odd_p2,
                "true_odd_p1": true_odd_p1,
                "true_odd_p2": true_odd_p2,
                "p1_stats": p1_stats,
                "p2_stats": p2_stats,
            }
        )

        matches.append({
            "prematch_id": prematch_id,
            "date": row.get('date', row['scraped_at'][:10]),
            "tournament": row.get('tournament', 'Tournoi en cours'),
            "tournament_url": row.get("tournament_url"),
            "tourney_winner_points": row.get("tourney_winner_points"),
            "category": row.get('category', 'ATP/WTA'),
            "tour": match_tour or "ATP",
            "time": row['time'],
            "player1": row['player1'],
            "player2": row['player2'],
            "p1_player_id": p1_id,
            "p2_player_id": p2_id,
            "p1_url": p1_url,
            "p2_url": p2_url,
            "surface": surface,
            "odd_p1": odd_p1,
            "odd_p2": odd_p2,
            "true_odd_p1": true_odd_p1,
            "true_odd_p2": true_odd_p2,
            "confidence": confidence,
            "calibration_used": calibration_used,
            "feature_snapshot": copy.deepcopy(feature_snapshot) if feature_snapshot else {},
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
            "rank_source_quality": _rank_source_quality_label({
                "p1_stats": p1_stats,
                "p2_stats": p2_stats,
            }),
            "rank_source_homogeneous": _match_homogeneous_rank_source({
                "p1_stats": p1_stats,
                "p2_stats": p2_stats,
            }),
            "segment_calibration_key": segment_calibration_key,
            "brier_segment_key": resolve_match_brier_segment_key(
                ml_model,
                tour=match_tour,
                surface=surface,
                tournament=row.get("tournament"),
            ),
            "snapshot_tier": "preview" if _fast_build else "full",
            "model_mtime_at_predict": float(_model_mtime or _ml_model_mtime()),
            "unreliable": _ev_unreliable,
            "data_alert": _ev_alert,
            "book_gap_pp": _ev_max_gap,
        })
        if PERF_LOG_LIVE_BUILD and (row_i % 25 == 0 or row_i == n_rows_total):
            print(
                f"[live-build] rows_build+predict progress: {row_i}/{n_rows_total}",
                flush=True,
            )
    if _enrich_build and base_by_key:
        for _left in base_by_key.values():
            if str(_left.get("snapshot_tier") or "") == "full":
                matches.append(dict(_left))
    if PERF_LOG_LIVE_BUILD and (_enrich_delta_odds_only or _enrich_kept_full):
        print(
            f"[live-build] enrich delta: {_enrich_delta_odds_only} lignes "
            f"(cotes seules) · {_enrich_kept_full} reprises hors CSV",
            flush=True,
        )
    _mark("rows_build+predict")
    if wx_conn is not None:
        try:
            wx_conn.close()
        except Exception:
            pass
    if PERF_LOG_LIVE_BUILD:
        n_mixed = sum(1 for m in matches if not _match_homogeneous_rank_source(m))
        if n_mixed:
            print(
                f"[live-build] sources rang/points mixtes conservées : {n_mixed}/{len(matches)}",
                flush=True,
            )
    _mark("finalize")
    for _m in matches:
        if isinstance(_m, dict):
            _rs, _rf = _match_data_reliability_bundle(_m)
            _m["data_reliability_score"] = _rs
            _m["data_reliability_flags"] = _rf
    try:
        complete_live_build_progress(len(matches))
    except Exception:
        pass
    return matches


@st.cache_data(ttl=300)
def get_latest_scraped_data(
    csv_path: str,
    _prematch_mtime: float,
    _profile_cache_schema: int,
    _model_mtime: float = 0.0,
    _engines_cache_version: int = 0,
):
    try:
        init_live_build_progress()
        matches = _build_live_matches_core(
            csv_path,
            _prematch_mtime,
            _profile_cache_schema,
            _model_mtime,
            _engines_cache_version,
            build_mode="full",
        )
    finally:
        try:
            if not snapshot_build_in_progress():
                clear_live_build_progress()
        except Exception:
            pass
    try:
        save_live_snapshot(
            matches,
            _live_signature_dict_compat(
                csv_path,
                _prematch_mtime,
                _profile_cache_schema,
                _model_mtime,
                _engines_cache_version,
            ),
        )
        invalidate_live_snapshot_ram_cache()
    except Exception:
        pass
    return matches


def _live_snapshot_signature_from_paths(csv_path: str, csv_mtime_s: float) -> dict:
    return _live_signature_dict_compat(
        csv_path,
        csv_mtime_s,
        PROFILE_CACHE_SCHEMA,
        _ml_model_mtime(),
        _ENGINES_CACHE_VERSION,
    )


def _live_model_signature_dict() -> dict:
    return model_signature_dict(
        PROFILE_CACHE_SCHEMA,
        _ml_model_mtime(),
        _ENGINES_CACHE_VERSION,
    )


def _patch_live_matches_odds_from_csv(matches: list, csv_path: str) -> tuple[list, int, int]:
    """Met à jour odd_p1/odd_p2 depuis le CSV prematch (sans recalcul ML)."""
    df = _load_prematch_df_for_live(csv_path)
    if df.empty:
        return matches, 0, 0
    lookup: dict[tuple[str, str, str, str], tuple[float, float]] = {}
    for _, row in df.iterrows():
        try:
            o1 = float(row.get("odd_p1") or 0.0)
            o2 = float(row.get("odd_p2") or 0.0)
        except (TypeError, ValueError):
            continue
        if o1 <= 1.0 or o2 <= 1.0:
            continue
        k = _match_odds_lookup_key(
            row.get("player1"),
            row.get("player2"),
            row.get("tournament"),
            _row_prematch_id(row),
        )
        lookup[k] = (o1, o2)
    snap_keys: set[tuple[str, str, str, str]] = set()
    out: list = []
    n_patched = 0
    for m in matches:
        row = dict(m)
        k = _match_odds_lookup_key(
            row.get("player1"),
            row.get("player2"),
            row.get("tournament"),
            row.get("prematch_id"),
        )
        snap_keys.add(k)
        odds = lookup.get(k)
        if odds:
            row["odd_p1"], row["odd_p2"] = odds
            n_patched += 1
        out.append(row)
    n_new = sum(1 for k in lookup if k not in snap_keys)
    return out, n_patched, n_new


def _try_refresh_snapshot_odds_only(csv_path: str, csv_mtime_s: float) -> bool:
    """Rafraîchit les cotes du snapshot si le modèle n'a pas changé (quelques secondes)."""
    if not csv_path or not os.path.isfile(csv_path):
        return False
    model_sig = _live_model_signature_dict()
    matches = load_live_snapshot_by_model(
        model_sig, max_age_sec=LIVE_SNAPSHOT_TTL_SEC
    )
    if not matches:
        return False
    meta = snapshot_meta() or {}
    stored_mtime = float((meta.get("signature") or {}).get("csv_mtime") or 0.0)
    if csv_mtime_s <= stored_mtime + 0.5:
        return True
    patched, n_upd, n_new = _patch_live_matches_odds_from_csv(matches, csv_path)
    sig = _live_snapshot_signature_from_paths(csv_path, csv_mtime_s)
    save_live_snapshot(patched, sig)
    invalidate_live_snapshot_ram_cache()
    print(
        f"[live-odds-patch] {n_upd} cotes MAJ · {n_new} match(s) CSV absents du snapshot",
        flush=True,
    )
    return True


def _try_promote_nextday_snapshot(csv_path: str, csv_mtime_s: float) -> bool:
    """Promote le J+1 full préchargé la veille en snapshot du jour."""
    target_date = datetime.now().date().isoformat()
    matches = load_nextday_snapshot_for_date(
        _live_model_signature_dict(),
        target_date,
        max_age_sec=3 * 24 * 3600,
    )
    if not matches:
        return False
    patched, n_upd, n_new = _patch_live_matches_odds_from_csv(matches, csv_path)
    sig = _live_snapshot_signature_from_paths(csv_path, csv_mtime_s)
    save_live_snapshot(patched, sig)
    invalidate_live_snapshot_ram_cache()
    print(
        f"[live-nextday-promote] {len(patched)} matchs préchargés promus · "
        f"{n_upd} cotes MAJ · {n_new} nouveau(x) match(s) CSV",
        flush=True,
    )
    _schedule_full_snapshot_upgrade(label="live-nextday-full-upgrade")
    return True


def _snapshot_has_preview_only(matches: list) -> bool:
    if not matches:
        return False
    return any(str(m.get("snapshot_tier") or "full") == "preview" for m in matches)


def _try_build_live_snapshot_if_missing(
    *,
    identity_workers: int | None = LIVE_DAEMON_IDENTITY_WORKERS,
    label: str = "live-snapshot",
    force_full: bool = False,
) -> bool:
    """Construit le snapshot disque (matchs du jour + demain) s'il est absent pour la signature courante."""
    if snapshot_build_in_progress():
        return False
    csv_path, csv_mtime = _prematch_csv_signature()
    if not csv_path or not os.path.isfile(csv_path):
        return False
    csv_mtime_s = _sanitize_mtime_float(csv_mtime)
    sig = _live_snapshot_signature_from_paths(csv_path, csv_mtime_s)
    existing = load_live_snapshot(sig, max_age_sec=LIVE_SNAPSHOT_TTL_SEC)
    if existing is None and not force_full:
        if _try_promote_nextday_snapshot(csv_path, csv_mtime_s):
            return True
        # Nouveau CSV : conserver le dernier full archivé, patcher ses cotes, puis
        # enrichir en arrière-plan. Surtout ne pas remplacer le full par une preview.
        if _try_refresh_snapshot_odds_only(csv_path, csv_mtime_s):
            _schedule_full_snapshot_upgrade(
                identity_workers=identity_workers,
                label="live-snapshot-full-upgrade",
            )
            return True
    if existing is not None and not force_full:
        if (
            LIVE_TWO_PHASE_SNAPSHOT
            and _snapshot_has_preview_only(existing)
            and label != "live-snapshot-full-upgrade"
        ):
            _schedule_full_snapshot_upgrade(
                identity_workers=identity_workers,
                label="live-snapshot-full-upgrade",
            )
        return True
    if existing is not None and force_full and not _snapshot_has_preview_only(existing):
        return True
    if not acquire_snapshot_build_lock():
        return False
    try:
        if force_full:
            _ADV_SIGNALS_MEM_CACHE.clear()
            _WEATHER_DAY_CACHE.clear()
        init_live_build_progress()
        if LIVE_TWO_PHASE_SNAPSHOT and not force_full:
            print(f"[{label}] phase preview (rapide)…", flush=True)
            matches = _build_live_matches_core(
                csv_path,
                csv_mtime_s,
                PROFILE_CACHE_SCHEMA,
                _ml_model_mtime(),
                _ENGINES_CACHE_VERSION,
                identity_workers=identity_workers,
                build_mode="preview",
            )
            save_live_snapshot(matches, sig)
            invalidate_live_snapshot_ram_cache()
            print(f"[{label}] preview OK ({len(matches)} matchs)", flush=True)
            complete_live_build_progress(len(matches))
            if SYNC_FULL_AFTER_PREVIEW:
                release_snapshot_build_lock()
                return _try_build_live_snapshot_if_missing(
                    identity_workers=identity_workers,
                    label=f"{label}-full-sync",
                    force_full=True,
                )
            _schedule_full_snapshot_upgrade(
                identity_workers=identity_workers,
                label="live-snapshot-full-upgrade",
            )
            return True
        _base_enrich = None
        _build_mode = "full"
        if LIVE_INCREMENTAL_ENRICH:
            _base_enrich = load_live_snapshot_by_model(
                _live_model_signature_dict(),
                max_age_sec=LIVE_SNAPSHOT_TTL_SEC,
            )
            if _base_enrich:
                _build_mode = "enrich"
                print(
                    f"[{label}] enrichissement incrémental "
                    f"({len(_base_enrich)} matchs en base)…",
                    flush=True,
                )
        matches = _build_live_matches_core(
            csv_path,
            csv_mtime_s,
            PROFILE_CACHE_SCHEMA,
            _ml_model_mtime(),
            _ENGINES_CACHE_VERSION,
            identity_workers=identity_workers,
            build_mode=_build_mode,
            base_matches=_base_enrich,
        )
        save_live_snapshot(matches, sig)
        invalidate_live_snapshot_ram_cache()
        complete_live_build_progress(len(matches))
        print(
            f"[{label}] snapshot OK ({len(matches)} matchs, mode={_build_mode})",
            flush=True,
        )
        return True
    except Exception as exc:
        print(f"[{label}] {exc}", flush=True)
        try:
            clear_live_build_progress()
        except Exception:
            pass
        return False
    finally:
        release_snapshot_build_lock()


def _schedule_full_snapshot_upgrade(
    *,
    identity_workers: int | None = LIVE_DAEMON_IDENTITY_WORKERS,
    label: str = "live-snapshot-full-upgrade",
) -> None:
    """Enrichissement complet reporté tant que l'utilisateur consulte le Live Tracker."""

    def _run_when_ui_idle() -> None:
        while _dashboard_ui_recently_active(LIVE_DAEMON_DEFER_UI_SEC):
            time.sleep(15)
        _try_build_live_snapshot_if_missing(
            identity_workers=identity_workers,
            label=label,
            force_full=True,
        )

    threading.Thread(
        target=_run_when_ui_idle,
        daemon=True,
        name=label,
    ).start()


def _kick_live_snapshot_build_async(*, label: str = "live-projection-kick") -> None:
    """Lance un build snapshot en thread si absent (non bloquant pour l'UI)."""
    if snapshot_build_in_progress():
        return
    sig = _current_live_signature_dict()
    if load_live_snapshot(sig, max_age_sec=LIVE_SNAPSHOT_TTL_SEC) is not None:
        return
    threading.Thread(
        target=_try_build_live_snapshot_if_missing,
        kwargs={"label": label},
        daemon=True,
        name=label,
    ).start()


@st.cache_resource
def start_live_projection_warmup():
    """Précharge la projection du jour dès le démarrage Streamlit (tous onglets)."""
    if not ENABLE_LIVE_PROJECTION_WARMUP:
        return None

    def _warmup():
        _set_current_thread_background_priority()
        time.sleep(LIVE_PROJECTION_WARMUP_DELAY_SEC)
        _try_build_live_snapshot_if_missing(label="live-projection-warmup")

    threading.Thread(target=_warmup, daemon=True, name="live-projection-warmup").start()
    return True


def ensure_te_profiles_for_prematch_csv(
    csv_path: str | None = None,
    *,
    force_refresh: bool = False,
    label: str = "te-profile-ensure",
) -> dict[str, int]:
    """Pré-scrape tous les profils TE manquants pour le CSV live filtré (pipeline matin)."""
    out = {"urls": 0, "cached": 0, "scraped": 0, "failed": 0}
    if not ENABLE_PROFILE_SCRAPE:
        return out
    path = csv_path
    if not path:
        path, _ = _prematch_csv_signature()
    if not path or not os.path.isfile(path):
        return out
    df = _load_prematch_df_for_live(path)
    if df.empty:
        return out
    urls: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        for u in (row.get("p1_url"), row.get("p2_url")):
            if pd.isna(u):
                continue
            x = str(u).strip()
            if x and x not in seen:
                seen.add(x)
                urls.append(x)
    out["urls"] = len(urls)
    if not urls:
        return out
    workers = max(1, min(8, int(os.getenv("BETTINGHUD_PROFILE_SCRAPE_WORKERS", "6"))))
    missing: list[str] = []
    for url in urls:
        if force_refresh:
            missing.append(url)
            continue
        if profile_scraper._load_from_cache(url, max_age_hours=PROFILE_CACHE_MAX_AGE_HOURS):
            out["cached"] += 1
        else:
            missing.append(url)

    def _scrape_one(url: str) -> tuple[str, bool]:
        try:
            prof = profile_scraper.scrape_profile(url, force_refresh=force_refresh)
            return url, bool(prof)
        except Exception:
            return url, False

    def _run_batch(batch: list[str]) -> list[str]:
        failed: list[str] = []
        if not batch:
            return failed
        if len(batch) == 1:
            _, ok = _scrape_one(batch[0])
            if ok:
                out["scraped"] += 1
            else:
                failed.append(batch[0])
            return failed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_scrape_one, u) for u in batch]
            for fut in as_completed(futs):
                try:
                    url, ok = fut.result()
                    if ok:
                        out["scraped"] += 1
                    else:
                        failed.append(url)
                except Exception:
                    pass
        return failed

    failed = _run_batch(missing)
    if failed:
        time.sleep(2.0)
        failed = _run_batch(failed)
    out["failed"] = len(failed)
    if urls:
        print(
            f"[{label}] profils TE: {out['cached']} cache / {out['scraped']} scrape / "
            f"{out['failed']} échec(s) sur {out['urls']} URLs",
            flush=True,
        )
    return out


def _count_te_profiles_complete(matches: list) -> tuple[int, int]:
    if not matches:
        return 0, 0
    complete = sum(
        1 for m in matches if m.get("p1_profile_loaded") and m.get("p2_profile_loaded")
    )
    return complete, len(matches)


def _background_prewarm_profiles_from_csv(csv_path: str, batch_size: int = 12) -> None:
    """Préchauffe les profils TE manquants ou expirés (lot limité par cycle daemon)."""
    if not ENABLE_PROFILE_SCRAPE or not csv_path or not os.path.isfile(csv_path):
        return
    try:
        df = pd.read_csv(csv_path)
        df = _filter_df_major_atp_wta_vectorized(_filter_df_exclude_doubles_prematch(df))
    except Exception:
        return
    if df.empty:
        return
    urls: list[str] = []
    seen: set[str] = set()
    for _, row in df.iterrows():
        for u in (row.get("p1_url"), row.get("p2_url")):
            if pd.isna(u):
                continue
            x = str(u).strip()
            if x and x not in seen:
                seen.add(x)
                urls.append(x)
    done = 0
    for url in urls:
        if done >= int(batch_size):
            break
        if profile_scraper._load_from_cache(url, max_age_hours=PROFILE_CACHE_MAX_AGE_HOURS):
            continue
        try:
            profile_scraper.scrape_profile(url)
            done += 1
        except Exception:
            pass


@st.cache_resource
def start_live_data_daemon():
    """Prematch + profils TE + snapshot ML en tâche de fond (invisible, cache 24 h)."""
    if not ENABLE_LIVE_DATA_DAEMON:
        return None

    def _loop():
        _set_current_thread_background_priority()
        time.sleep(LIVE_DATA_DAEMON_INITIAL_DELAY_SEC)
        while True:
            deferred = False
            try:
                ui_active = _dashboard_ui_recently_active()
                csv_path, csv_mtime = _prematch_csv_signature()
                csv_mtime_s = _sanitize_mtime_float(csv_mtime)
                if PREMATCH_AUTO_REFRESH:
                    age_min = (
                        (time.time() - csv_mtime_s) / 60.0 if csv_mtime_s > 0 else float("inf")
                    )
                    if (not csv_path) or age_min >= float(PREMATCH_TTL_MIN):
                        _start_prematch_scraper_subprocess()
                if csv_path and os.path.isfile(csv_path) and not _prematch_scrape_in_progress():
                    if ui_active:
                        prewarm_batch = LIVE_DAEMON_PROFILE_PREWARM_BATCH
                        prewarm_rounds = 1
                    else:
                        _night_hours = os.getenv(
                            "BETTINGHUD_NIGHT_PREWARM_HOURS", "1,2,3,4,5,6"
                        )
                        try:
                            _night_set = {
                                int(x.strip())
                                for x in _night_hours.split(",")
                                if x.strip()
                            }
                        except ValueError:
                            _night_set = {1, 2, 3, 4, 5, 6}
                        if datetime.now().hour in _night_set:
                            prewarm_batch = NIGHT_PROFILE_PREWARM_BATCH
                            prewarm_rounds = DAEMON_PREWARM_ROUNDS_IDLE
                        else:
                            prewarm_batch = max(12, LIVE_DAEMON_PROFILE_PREWARM_BATCH)
                            prewarm_rounds = 2
                    if prewarm_batch > 0:
                        for _ in range(prewarm_rounds):
                            _background_prewarm_profiles_from_csv(
                                csv_path, batch_size=prewarm_batch
                            )
                    csv_path, csv_mtime = _prematch_csv_signature()
                    csv_mtime_s = _sanitize_mtime_float(csv_mtime)
                    sig = _live_snapshot_signature_from_paths(csv_path, csv_mtime_s)
                    cached = load_live_snapshot(sig, max_age_sec=LIVE_SNAPSHOT_TTL_SEC)
                    meta = snapshot_meta() or {}
                    stored_csv_mtime = float(
                        (meta.get("signature") or {}).get("csv_mtime") or 0.0
                    )
                    csv_newer = csv_mtime_s > stored_csv_mtime + 0.5
                    if cached is None:
                        if _try_refresh_snapshot_odds_only(csv_path, csv_mtime_s):
                            _schedule_full_snapshot_upgrade(
                                label="live-data-daemon-full-upgrade"
                            )
                        else:
                            _try_build_live_snapshot_if_missing(label="live-data-daemon")
                    elif csv_newer:
                        if _try_refresh_snapshot_odds_only(csv_path, csv_mtime_s):
                            _schedule_full_snapshot_upgrade(
                                label="live-data-daemon-full-upgrade"
                            )
                    elif _snapshot_has_preview_only(cached) and not ui_active:
                        _try_build_live_snapshot_if_missing(
                            label="live-data-daemon-full",
                            force_full=True,
                        )
                    elif ui_active:
                        # Snapshot déjà prêt : on évite un rebuild lourd pendant la navigation Live.
                        deferred = True
                    try:
                        from scripts.daily_top_proba_store import run_daily_top_proba_daemon_pass

                        run_daily_top_proba_daemon_pass(capture_source="live_data_daemon")
                    except Exception:
                        pass
            except Exception as exc:
                print(f"[live-data-daemon] {exc}", flush=True)
            sleep_sec = (
                min(120, LIVE_DATA_DAEMON_INTERVAL_SEC)
                if deferred
                else LIVE_DATA_DAEMON_INTERVAL_SEC
            )
            time.sleep(sleep_sec)

    threading.Thread(target=_loop, daemon=True, name="live-data-daemon").start()
    return True


if not HEADLESS_APP:
    st.title("🎾 BettingHUD - Value Bets Tracker")
    st.markdown("---")

from scripts.bets_db import (
    APP_KELLY_TRACKER_SOURCES,
    compute_live_tracker_bankroll_eur,
    get_data_freshness_snapshot,
    init_all as _init_bets_db,
    normalize_schedule_date,
    read_algo_opportunity_dates,
    read_algo_opportunity_report,
    save_bet_enriched as _save_bet_enriched,
    set_live_tracker_manual_adjust_eur,
    set_live_tracker_start_br,
    upsert_algo_opportunities,
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


def _sanitize_mtime_float(ts) -> float:
    """mtime utilisable en arithmétique (évite NaN / négatif pour le cache Streamlit)."""
    try:
        t = float(ts or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(t) or t < 0:
        return 0.0
    return t


def _safe_strftime_mtime(ts, fmt: str) -> str:
    """Affiche un mtime local. Sous Windows, ``fromtimestamp`` peut lever ``OSError: [Errno 22]``."""
    if ts is None:
        return "—"
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return "—"
    if not np.isfinite(t) or t < 0:
        return "—"
    try:
        return datetime.fromtimestamp(t).strftime(fmt)
    except (OSError, OverflowError, ValueError):
        return "—"


def _format_mtime_local(ts) -> str:
    return _safe_strftime_mtime(ts, "%Y-%m-%d %H:%M (fichier)")


_PARIS_TZ = ZoneInfo("Europe/Paris")


def _format_built_at_paris(built_at: float) -> str:
    """Horodatage snapshot live en heure de Paris (built_at = epoch UTC)."""
    try:
        dt = datetime.fromtimestamp(float(built_at), tz=timezone.utc).astimezone(_PARIS_TZ)
        return dt.strftime("%d/%m %H:%M") + " (Paris)"
    except (OSError, OverflowError, ValueError, TypeError):
        return "—"


def _format_relative_age_sec(age_sec: float | None) -> str:
    """Libellé court « il y a X min » pour un âge en secondes."""
    if age_sec is None:
        return "—"
    try:
        age = max(0.0, float(age_sec))
    except (TypeError, ValueError):
        return "—"
    if age < 90:
        return f"il y a {int(age)} s"
    if age < 3600:
        return f"il y a {int(age // 60)} min"
    if age < 86400:
        h = int(age // 3600)
        m = int((age % 3600) // 60)
        return f"il y a {h} h {m} min" if m else f"il y a {h} h"
    d = int(age // 86400)
    h = int((age % 86400) // 3600)
    return f"il y a {d} j {h} h" if h else f"il y a {d} j"


def _file_age_sec(path: str) -> float | None:
    try:
        return max(0.0, time.time() - float(os.path.getmtime(path)))
    except OSError:
        return None


def _latest_glob_mtime(patterns: list[str]) -> tuple[str | None, float | None]:
    best_path: str | None = None
    best_mtime = 0.0
    for pattern in patterns:
        for fp in glob.glob(pattern):
            try:
                mt = float(os.path.getmtime(fp))
            except OSError:
                continue
            if mt >= best_mtime:
                best_mtime = mt
                best_path = fp
    if best_path is None:
        return None, None
    return best_path, max(0.0, time.time() - best_mtime)


def _morning_pipeline_status() -> dict:
    """Dernier journal pipeline matin (détail + cron wrapper)."""
    detail_path, detail_age = _latest_glob_mtime(
        [os.path.join("data", "cache", "logs", "morning_pipeline_*.log")]
    )
    cron_path = os.path.join("data", "logs", "morning_pipeline_cron.log")
    cron_age = _file_age_sec(cron_path) if os.path.isfile(cron_path) else None
    path = detail_path
    age = detail_age
    if path is None and os.path.isfile(cron_path):
        path = cron_path
        age = cron_age
    elif path and cron_age is not None and detail_age is not None and cron_age < detail_age:
        # Cron plus récent que le log détaillé (ex. échec avant création du log horodaté)
        path = cron_path
        age = cron_age
    outcome = "unknown"
    summary = "—"
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-4000:]
        except OSError:
            tail = ""
        low = tail.lower()
        if "pipeline terminé" in low or "pipeline termine" in low:
            outcome = "ok"
            summary = "Dernière exécution OK"
        elif "erreur" in low or "échec" in low or "echec" in low:
            outcome = "error"
            summary = "Erreur détectée dans le journal"
        else:
            outcome = "warn"
            summary = "Journal sans confirmation de succès"
    return {
        "path": path,
        "age_sec": age,
        "outcome": outcome,
        "summary": summary,
    }


def _count_open_bets() -> int:
    db_path = os.path.join("data", "bettinghud.db")
    if not os.path.isfile(db_path):
        return 0
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) FROM user_bets
                WHERE COALESCE(TRIM(status), '') = 'En cours'
                """
            ).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _collect_system_status() -> dict:
    """Indicateurs read-only pour le bandeau Paramètres (diagnostic ops)."""
    now = time.time()
    csv_path, csv_mtime = _prematch_csv_signature()
    csv_age = (now - float(csv_mtime)) if csv_path and csv_mtime else None

    meta = snapshot_meta() or {}
    snap_n = int(meta.get("n_matches") or 0)
    built_at = meta.get("built_at")
    try:
        snap_age = (now - float(built_at)) if built_at else None
    except (TypeError, ValueError):
        snap_age = None
    snap_file_age = _file_age_sec(SNAPSHOT_PATH) if os.path.isfile(SNAPSHOT_PATH) else None

    from scripts.portfolio_sync_lock import daemon_recently_active

    hb_path = os.path.join("data", "cache", ".portfolio_results_daemon.heartbeat")
    hb_age = _file_age_sec(hb_path) if os.path.isfile(hb_path) else None
    daemon_ok = daemon_recently_active()

    morning = _morning_pipeline_status()
    open_bets = _count_open_bets()
    building = snapshot_build_in_progress()

    def _level(age: float | None, *, ok_h: float, warn_h: float, present: bool = True) -> str:
        if not present:
            return "error"
        if age is None:
            return "warn"
        if age <= ok_h * 3600:
            return "ok"
        if age <= warn_h * 3600:
            return "warn"
        return "error"

    prematch_level = _level(csv_age, ok_h=0.5, warn_h=3.0, present=bool(csv_path))
    snap_level = "error" if snap_n <= 0 and not building else _level(
        snap_age, ok_h=6.0, warn_h=24.0, present=snap_n > 0 or building
    )
    if building and snap_n <= 0:
        snap_level = "warn"

    if daemon_ok:
        daemon_level = "ok"
    elif hb_age is None:
        daemon_level = "warn"
    elif hb_age <= 660:
        daemon_level = "ok"
    elif hb_age <= 3600:
        daemon_level = "warn"
    else:
        daemon_level = "error"

    morning_level = morning.get("outcome") or "warn"
    if morning_level == "unknown":
        morning_level = "warn"

    issues: list[str] = []
    if prematch_level == "error":
        issues.append("CSV prematch absent ou trop ancien")
    elif prematch_level == "warn":
        issues.append("CSV prematch > 30 min")
    if snap_level == "error":
        issues.append("snapshot live absent ou obsolète")
    elif snap_level == "warn":
        if building:
            issues.append("build snapshot en cours")
        else:
            issues.append("snapshot live > 6 h")
    if daemon_level == "error":
        issues.append("daemon résultats inactif")
    elif daemon_level == "warn":
        issues.append("daemon résultats non détecté")
    if morning_level == "error":
        issues.append("dernier pipeline matin en échec")
    elif morning_level == "warn" and morning.get("path"):
        issues.append("pipeline matin sans confirmation OK")

    levels = [prematch_level, snap_level, daemon_level, morning_level]
    if "error" in levels:
        overall = "error"
    elif "warn" in levels:
        overall = "warn"
    else:
        overall = "ok"

    return {
        "overall": overall,
        "issues": issues,
        "prematch": {
            "level": prematch_level,
            "path": csv_path,
            "age_sec": csv_age,
            "label": os.path.basename(csv_path) if csv_path else "—",
        },
        "snapshot": {
            "level": snap_level,
            "n_matches": snap_n,
            "age_sec": snap_age,
            "built_paris": _format_built_at_paris(built_at) if built_at else "—",
            "file_age_sec": snap_file_age,
            "building": building,
        },
        "daemon": {
            "level": daemon_level,
            "active": daemon_ok,
            "age_sec": hb_age,
            "path": hb_path,
        },
        "morning": morning,
        "morning_level": morning_level,
        "open_bets": open_bets,
        "environment": _bettinghud_environment_label(),
    }


def _render_system_status_banner() -> None:
    """Bandeau diagnostic ops (onglet Paramètres) — lecture seule."""
    status = _collect_system_status()
    overall = status.get("overall") or "warn"
    st.subheader("État système")
    if overall == "ok":
        st.success(
            "Prêt à jouer — cotes, snapshot et services récents. "
            "Consulte le détail ci-dessous si un onglet live est vide (filtre EV possible)."
        )
    elif overall == "error":
        st.error(
            "Attention — au moins un composant critique est absent ou trop ancien : "
            + " · ".join(status.get("issues") or ["voir indicateurs"])
        )
    else:
        st.warning(
            "Partiellement prêt — vérifie les indicateurs orange avant de miser. "
            + (
                " · ".join(status.get("issues") or [])
                if status.get("issues")
                else "Certaines sources sont vieilles ou le daemon ne répond pas."
            )
        )

    prem = status["prematch"]
    snap = status["snapshot"]
    daemon = status["daemon"]
    morning = status["morning"]

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric(
            "CSV prematch",
            _format_relative_age_sec(prem.get("age_sec")),
            help="Dernier fichier `data/scraped/prematch_odds_*.csv` (mtime disque).",
        )
        st.caption(prem.get("label") or "—")
    with c2:
        snap_label = (
            f"{snap.get('n_matches', 0)} matchs"
            if int(snap.get("n_matches") or 0) > 0
            else ("build…" if snap.get("building") else "—")
        )
        st.metric(
            "Snapshot live",
            snap_label,
            help="Meta `live_matches_snapshot` — matchs projetés jour/demain.",
        )
        st.caption(
            f"{snap.get('built_paris', '—')} · {_format_relative_age_sec(snap.get('age_sec'))}"
        )
    with c3:
        st.metric(
            "Daemon résultats",
            "actif" if daemon.get("active") else "inactif",
            help="Heartbeat `data/cache/.portfolio_results_daemon.heartbeat` (< 11 min).",
        )
        st.caption(_format_relative_age_sec(daemon.get("age_sec")))
    with c4:
        st.metric(
            "Pipeline matin",
            _format_relative_age_sec(morning.get("age_sec")),
            help="Dernier `data/cache/logs/morning_pipeline_*.log` ou cron wrapper.",
        )
        st.caption(str(morning.get("summary") or "—"))
    with c5:
        st.metric(
            "Paris en cours",
            str(status.get("open_bets", 0)),
            help="Lignes `user_bets` avec statut « En cours ».",
        )
        st.caption("Portefeuille")

    with st.expander("Détail fichiers & commandes", expanded=False):
        st.markdown(
            f"- **Environnement** : `{status.get('environment')}`\n"
            f"- **CSV** : `{prem.get('path') or '—'}`\n"
            f"- **Snapshot** : `{SNAPSHOT_PATH}` · fichier {_format_relative_age_sec(snap.get('file_age_sec'))}\n"
            f"- **Daemon HB** : `{daemon.get('path')}`\n"
            f"- **Pipeline** : `{morning.get('path') or '—'}`\n"
        )
        st.caption(
            "Audit picks Paris/Telegram : `py -3 scripts/audit_daily_picks_parity.py` · "
            "Rebuild snapshot : `py -3 scripts/rebuild_live_projection.py` · "
            "Pipeline matin : `py -3 scripts/morning_live_pipeline.py`"
        )


def init_db():
    """Run all bets-DB migrations (idempotent)."""
    _init_bets_db('data/bettinghud.db')

init_db()
_maybe_migrate_live_cache_db_invalidation()
_purge_live_player_caches_older_than_db()


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
    segment_key=None,
    match_date=None,
    p_model=None,
    ev_at_bet=None,
    bookmaker_source=None,
    notes=None,
    tracker_source=None,
) -> int:
    """Persist a bet with optional decision-context fields. Falls back to a
    minimal insert when called with positional arguments only (legacy path)."""
    _tg_uid = None
    try:
        from scripts.web_auth import get_session_user

        _wu = get_session_user()
        if _wu and _wu.get("telegram_user_id"):
            _tg_uid = str(_wu["telegram_user_id"]).strip()
    except Exception:
        pass
    bet_id = _save_bet_enriched(
        match_name=match_name,
        bet_on=bet_on,
        odds=float(odds),
        stake=float(stake),
        tour=tour,
        surface=surface,
        tournament=tournament,
        match_id=match_id,
        segment_key=segment_key,
        match_date=match_date,
        p_model=p_model,
        ev_at_bet=ev_at_bet,
        bookmaker_source=bookmaker_source,
        notes=notes,
        tracker_source=tracker_source,
        telegram_user_id=_tg_uid,
    )
    _clear_portfolio_runtime_caches()
    st.toast("✅ Pari enregistré avec succès dans votre portefeuille !")
    return int(bet_id or 0)

def update_bet_status(bet_id, status, profit):
    conn = sqlite3.connect('data/bettinghud.db')
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE user_bets
        SET status = ?, profit = ?, settled_ts = ?, result_source = COALESCE(result_source, ?)
        WHERE id = ?
    ''', (
        status,
        profit,
        datetime.utcnow().isoformat(timespec="seconds") if status != "En cours" else None,
        "manual",
        bet_id,
    ))
    conn.commit()
    conn.close()
    _clear_portfolio_runtime_caches()


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


def _user_bets_state_signature() -> tuple:
    """Small DB fingerprint so UI caches notice updates made by subprocesses."""
    conn = sqlite3.connect("data/bettinghud.db")
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(MAX(id), 0),
                COALESCE(SUM(CASE WHEN status = 'En cours' THEN stake ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status != 'En cours' THEN profit ELSE 0 END), 0),
                COALESCE(MAX(COALESCE(settled_ts, placed_ts, date)), '')
            FROM user_bets
            """
        ).fetchone()
        return tuple(row or ())
    except Exception:
        return ()
    finally:
        conn.close()


@st.cache_data(ttl=20)
def _cached_existing_bets_index(user_bets_sig=None):
    return get_existing_bets_index()


@st.cache_data(ttl=15)
def _cached_live_tracker_bankroll_snapshot(user_bets_sig=None):
    return live_tracker_bankroll_snapshot()


def _clear_portfolio_runtime_caches() -> None:
    """Invalidate Streamlit caches derived from `user_bets` after DB writes."""
    for fn in (_cached_existing_bets_index, _cached_live_tracker_bankroll_snapshot):
        try:
            fn.clear()
        except Exception:
            pass
    try:
        st.session_state.pop("_tracking_cached", None)
    except Exception:
        pass


def _live_opportunity_key(detected_date: str, match: dict, bet_on: str) -> str:
    mid = str(match.get("prematch_id") or "").strip()
    # Les IDs `te_...` viennent de snapshots TennisExplorer reconstruits et ne sont
    # pas stables entre deux rebuilds. Pour le report journalier, une opportunité
    # est le triplet date + match + joueur parié.
    if mid and not mid.lower().startswith("te_"):
        return f"{detected_date}|id:{mid}|{str(bet_on).strip().lower()}"
    p1, p2 = _match_display_players(match)
    match_name = f"{p1} vs {p2}"
    return f"{detected_date}|{match_name.strip().lower()}|{str(bet_on).strip().lower()}"


def _match_display_players(match: dict) -> tuple[str, str]:
    p1 = str(match.get("player1") or match.get("p1") or "").strip()
    p2 = str(match.get("player2") or match.get("p2") or "").strip()
    return p1, p2


def _load_live_matches_for_algo_sync() -> list:
    """Charge le snapshot live courant (sans dépendre du cache session Live Tracker)."""
    matches = load_live_snapshot(
        _current_live_signature_dict(),
        max_age_sec=LIVE_SNAPSHOT_TTL_SEC,
    )
    if matches is None:
        matches = load_live_snapshot_by_model(
            _live_model_signature_dict(),
            max_age_sec=LIVE_SNAPSHOT_TTL_SEC,
        )
    return [dict(m) for m in (matches or []) if isinstance(m, dict)]


def _filter_matches_for_algo_report(matches: list) -> list:
    """Même base que le Live Tracker : cotes valides + rang/points sur les deux joueurs."""
    out = []
    for m in matches:
        try:
            if float(m.get("odd_p1") or 0.0) <= 1.0 or float(m.get("odd_p2") or 0.0) <= 1.0:
                continue
        except (TypeError, ValueError):
            continue
        if not _match_has_rank_points_source(m):
            continue
        from scripts.match_rank_quality import passes_data_reliability_filter

        if not passes_data_reliability_filter(m):
            continue
        out.append(m)
    return out


def _collect_value_bets_from_matches(
    matches: list,
    *,
    ev_threshold_pct: float = DEFAULT_LIVE_EV_THRESHOLD_PCT,
) -> list[dict]:
    detector = ValueDetector(min_value_threshold=float(ev_threshold_pct) / 100.0)
    value_bets: list[dict] = []
    from scripts.match_rank_quality import passes_data_reliability_filter

    for idx, match in enumerate(matches):
        if not passes_data_reliability_filter(match):
            continue
        _seg_brier = _match_segment_brier(match)
        p1_val = enrich_value_metrics(
            detector.detect_value(
                match["odd_p1"], match["true_odd_p1"], confidence=match.get("confidence")
            ),
            segment_brier=_seg_brier,
        )
        p2_val = enrich_value_metrics(
            detector.detect_value(
                match["odd_p2"], match["true_odd_p2"], confidence=match.get("confidence")
            ),
            segment_brier=_seg_brier,
        )
        if p1_val["is_value"]:
            value_bets.append({"match": match, "player": 1, "val": p1_val, "idx": idx})
        if p2_val["is_value"]:
            value_bets.append({"match": match, "player": 2, "val": p2_val, "idx": idx})
    return value_bets


def sync_algo_report_from_snapshot(
    *,
    ev_threshold_pct: float = DEFAULT_LIVE_EV_THRESHOLD_PCT,
    force: bool = False,
) -> int:
    """Synchronise le report journalier depuis le snapshot live (headless)."""
    meta = snapshot_meta() or {}
    built_at = float(meta.get("built_at") or 0.0)
    matches = _filter_matches_for_algo_report(_load_live_matches_for_algo_sync())
    if not matches:
        return 0
    value_bets = _collect_value_bets_from_matches(matches, ev_threshold_pct=ev_threshold_pct)
    n = _persist_live_value_opportunities(value_bets)
    try:
        from scripts.daily_top_proba_store import sync_daily_top_proba_from_snapshot

        today_matches = [m for m in matches if _is_today_calendar_match(m)]
        sync_daily_top_proba_from_snapshot(
            today_matches,
            snapshot_meta=meta,
            capture_source="live_snapshot",
        )
    except Exception:
        pass
    return int(n)


def _sync_algo_report_from_snapshot_ui(*, force: bool = False) -> int:
    """Sync report algo avec anti-spam (snapshot inchangé + délai court)."""
    meta = snapshot_meta() or {}
    built_at = float(meta.get("built_at") or 0.0)
    last_built = float(st.session_state.get("_algo_report_sync_built_at") or 0.0)
    last_ts = float(st.session_state.get("_algo_report_sync_ts") or 0.0)
    if (
        not force
        and built_at > 0
        and built_at <= last_built
        and (time.time() - last_ts) < 120.0
    ):
        return int(st.session_state.get("_algo_report_sync_n") or 0)
    ev_pct = float(
        st.session_state.get("live_ev_threshold") or DEFAULT_LIVE_EV_THRESHOLD_PCT
    )
    n = sync_algo_report_from_snapshot(ev_threshold_pct=ev_pct, force=True)
    st.session_state["_algo_report_sync_built_at"] = built_at
    st.session_state["_algo_report_sync_ts"] = time.time()
    st.session_state["_algo_report_sync_n"] = n
    return n


def _persist_live_value_opportunities(value_bets: list[dict]) -> int:
    if not value_bets:
        return 0
    detected_dt = datetime.now(ZoneInfo("Europe/Paris"))
    detected_date = detected_dt.date().isoformat()
    detected_ts = detected_dt.isoformat(timespec="seconds")
    rows = []
    for vb in value_bets:
        match = vb.get("match") or {}
        val = vb.get("val") or {}
        side = int(vb.get("player") or 0)
        if side not in (1, 2):
            continue
        p1_name, p2_name = _match_display_players(match)
        bet_on = p1_name if side == 1 else p2_name
        odd_book = match.get("odd_p1") if side == 1 else match.get("odd_p2")
        true_odd = match.get("true_odd_p1") if side == 1 else match.get("true_odd_p2")
        try:
            p_model = 1.0 / float(true_odd) if float(true_odd) > 0 else None
        except Exception:
            p_model = None
        try:
            p_implicit = 1.0 / float(odd_book) if float(odd_book) > 0 else None
        except Exception:
            p_implicit = None
        match_name = f"{p1_name} vs {p2_name}"
        from scripts.match_rank_quality import reliability_fields_from_match

        rows.append(
            {
                "opportunity_key": _live_opportunity_key(detected_date, match, str(bet_on or "")),
                "detected_date": detected_date,
                "detected_ts": detected_ts,
                "match_date": match.get("date"),
                "match_id": str(match.get("prematch_id") or "").strip() or None,
                "match_name": match_name,
                "player1": p1_name or None,
                "player2": p2_name or None,
                "bet_on": bet_on,
                "side": side,
                "tour": str(match.get("tour") or match.get("category") or "").upper() or None,
                "surface": match.get("surface"),
                "tournament": match.get("tournament"),
                "odd_book": odd_book,
                "true_odd": true_odd,
                "p_model": p_model,
                "p_implicit": p_implicit,
                "ev": float(val.get("value_pct") or 0.0) / 100.0,
                "confidence": match.get("confidence"),
                "segment_key": _match_brier_segment_key(match) or None,
                "segment_brier": _match_segment_brier(match),
                "sharpe_ratio": val.get("sharpe_ratio"),
                "sharpe_per_brier": val.get("sharpe_per_brier"),
                "priority_score": val.get("priority_score"),
                "snapshot_tier": match.get("snapshot_tier"),
                **reliability_fields_from_match(match),
            }
        )
    try:
        return int(upsert_algo_opportunities(rows))
    except Exception:
        return 0


def _match_calendar_date(m: dict):
    try:
        d = pd.Timestamp(str(m.get("date") or "")[:10])
        if pd.isna(d):
            return None
        return d.date()
    except Exception:
        return None


def _is_today_calendar_match(m: dict) -> bool:
    d = _match_calendar_date(m)
    if d is not None:
        return d == datetime.now(_PARIS_TZ).date()
    return not str(m.get("time") or "").startswith("Demain")


def _sanitize_stale_demain_time_label(m: dict) -> dict:
    """Retire « Demain » si la date calendrier est déjà aujourd'hui (libellé scrape obsolète)."""
    from scripts.daily_top_proba_store import sanitize_stale_demain_time_label

    return sanitize_stale_demain_time_label(m)


def _sanitize_live_matches_list(matches: list) -> list:
    return [_sanitize_stale_demain_time_label(m) for m in matches if isinstance(m, dict)]


# Chart « Top probas jour » — voir docs/CHART_TOP_PROBAS_JOUR.md
_TOP_PROBAS_CHART_MODEL_COLOR = "#294c86"
_TOP_PROBAS_CHART_BOOK_COLOR = "#f0d78f"
_TOP_PROBAS_CHART_REF_LINES = (50.0, 70.0, 80.0)
_TOP_PROBAS_CHART_TOUR_COLORS = {"ATP": "#00B0FF", "WTA": "#c75b9a", "—": "#8A8D98", "": "#8A8D98"}
_TOP_PROBAS_GAP_WARN_PP = 25.0
TOP_PROBAS_DISPLAY_LIMIT = 15
FAVORITE_EV_BAND_MIN_FRAC = 0.15
FAVORITE_EV_BAND_MAX_FRAC = 1.0
FAVORITE_EV_BAND_TOGGLE_KEY = "favorite_ev_band_filter"
FAVORITE_EV_BAND_TOGGLE_KEY_LIVE = "favorite_ev_band_filter_live"
FAVORITE_EV_BAND_TOGGLE_KEY_TOPPROBAS = "favorite_ev_band_filter_topprobas"


def _favorite_ev_band_is_active() -> bool:
    return bool(st.session_state.get(FAVORITE_EV_BAND_TOGGLE_KEY, False))


def _favorite_ev_band_params() -> tuple[float | None, float | None]:
    """EV min/max (fraction) si le toggle partagé est actif."""
    if _favorite_ev_band_is_active():
        return FAVORITE_EV_BAND_MIN_FRAC, FAVORITE_EV_BAND_MAX_FRAC
    return None, None


def _render_favorite_ev_band_toggle(
    *,
    widget_key: str,
    label: str | None = None,
    help_suffix: str = "",
) -> bool:
    """Toggle partagé Live Tracker + Top probas (clé widget unique, état canonique partagé)."""
    canonical = FAVORITE_EV_BAND_TOGGLE_KEY
    if canonical not in st.session_state:
        st.session_state[canonical] = False
    if widget_key not in st.session_state:
        st.session_state[widget_key] = bool(st.session_state[canonical])
    elif bool(st.session_state[widget_key]) != bool(st.session_state[canonical]):
        st.session_state[widget_key] = bool(st.session_state[canonical])

    toggle_label = label or (
        f"Top {TOP_PROBAS_DISPLAY_LIMIT} · EV favori +15 % à +100 %"
    )
    help_txt = (
        "Filtre l'EV du **favori modèle** : EV = p_fav × cote_fav − 1 "
        f"(bande +{FAVORITE_EV_BAND_MIN_FRAC * 100:.0f} % à +{FAVORITE_EV_BAND_MAX_FRAC * 100:.0f} %)."
    )
    if help_suffix:
        help_txt += f" {help_suffix}"

    def _sync_canonical() -> None:
        st.session_state[canonical] = bool(st.session_state.get(widget_key, False))

    st.toggle(
        toggle_label,
        key=widget_key,
        on_change=_sync_canonical,
        help=help_txt,
    )
    st.session_state[canonical] = bool(st.session_state.get(widget_key, False))
    return _favorite_ev_band_is_active()


def _match_favorite_model_metrics(m: dict) -> dict | None:
    """Métriques favori modèle (proba + EV) pour un match snapshot."""
    try:
        if float(m.get("odd_p1") or 0.0) <= 1.0 or float(m.get("odd_p2") or 0.0) <= 1.0:
            return None
    except (TypeError, ValueError):
        return None
    fs = m.get("feature_snapshot") or {}
    try:
        p1 = float(fs.get("capped_p1_prob") or 0.5)
    except (TypeError, ValueError):
        p1 = 0.5
    fav_side = 1 if p1 >= 0.5 else 2
    fav_p = max(p1, 1.0 - p1)
    fav = str(m.get("player1") if fav_side == 1 else m.get("player2") or "")
    und = str(m.get("player2") if fav_side == 1 else m.get("player1") or "")
    of = m.get("odd_p1") if fav_side == 1 else m.get("odd_p2")
    ou = m.get("odd_p2") if fav_side == 1 else m.get("odd_p1")
    try:
        cotes = f"{float(of):.2f} / {float(ou):.2f}"
        book_implied = 100.0 / float(of) if of else None
        ev_fav_frac = fav_p * float(of) - 1.0
    except (TypeError, ValueError):
        cotes = "—"
        book_implied = None
        ev_fav_frac = None
    gap = m.get("book_gap_pp")
    if gap is None:
        from scripts.match_rank_quality import book_gap_pp_from_favorite

        try:
            of_f = float(of) if of is not None else None
        except (TypeError, ValueError):
            of_f = None
        if of_f is not None:
            gap = book_gap_pp_from_favorite(fav_p, of_f)
    try:
        gap_f = float(gap) if gap is not None else None
        gap_s = f"{gap_f:.1f}" if gap_f is not None else "—"
    except (TypeError, ValueError):
        gap_f = None
        gap_s = "—"
    return {
        "fav_p": fav_p,
        "p1": p1,
        "fav_side": fav_side,
        "odd_fav": float(of) if of is not None else None,
        "odd_und": float(ou) if ou is not None else None,
        "ev_fav_frac": ev_fav_frac,
        "ev_fav_pct": (float(ev_fav_frac) * 100.0) if ev_fav_frac is not None else None,
        "fav": fav,
        "und": und,
        "tour": str(m.get("tour") or "").upper(),
        "tournament": str(m.get("tournament") or "")[:40],
        "cotes": cotes,
        "gap_pp": gap_f,
        "gap_s": gap_s,
        "book_implied_pct": book_implied,
        "favori_label": fav.split("(")[0].strip(),
    }


def _passes_favorite_ev_band(
    metrics: dict | None,
    *,
    ev_min_frac: float | None,
    ev_max_frac: float | None,
) -> bool:
    if metrics is None:
        return False
    ev_fav_frac = metrics.get("ev_fav_frac")
    if ev_fav_frac is None:
        return False
    if ev_min_frac is not None and float(ev_fav_frac) < float(ev_min_frac):
        return False
    if ev_max_frac is not None and float(ev_fav_frac) > float(ev_max_frac):
        return False
    return True


def _compute_favorite_ev_funnel_stats(
    matches: list[dict],
    *,
    today_only: bool = True,
    ev_min_frac: float | None = None,
    ev_max_frac: float | None = None,
) -> dict:
    """Compteurs entonnoir : pool → jour → cotes/probas → bande EV."""
    stats = {
        "total": len(matches),
        "today": 0,
        "with_metrics": 0,
        "ev_in_band": 0,
        "best_ev_pct": None,
    }
    for m in matches:
        if today_only and not _is_today_calendar_match(m):
            continue
        stats["today"] += 1
        met = _match_favorite_model_metrics(m)
        if not met:
            continue
        stats["with_metrics"] += 1
        ev_pct = met.get("ev_fav_pct")
        if ev_pct is not None:
            try:
                ev_f = float(ev_pct)
                if stats["best_ev_pct"] is None or ev_f > stats["best_ev_pct"]:
                    stats["best_ev_pct"] = ev_f
            except (TypeError, ValueError):
                pass
        if _passes_favorite_ev_band(met, ev_min_frac=ev_min_frac, ev_max_frac=ev_max_frac):
            stats["ev_in_band"] += 1
    return stats


def _format_favorite_ev_funnel_caption(
    stats: dict,
    *,
    ev_min_frac: float | None = None,
    ev_max_frac: float | None = None,
    top_n: int | None = None,
) -> str:
    """Ligne diagnostic entonnoir EV (empty states)."""
    parts = [
        f"**{stats.get('total', 0)}** pool",
        f"**{stats.get('today', 0)}** jour",
        f"**{stats.get('with_metrics', 0)}** cotes/probas",
    ]
    if ev_min_frac is not None or ev_max_frac is not None:
        lo = int((ev_min_frac or 0) * 100)
        hi = int((ev_max_frac or 1) * 100)
        parts.append(f"**{stats.get('ev_in_band', 0)}** EV +{lo} % → +{hi} %")
    if top_n is not None:
        parts.append(f"Top **{top_n}**")
    line = " → ".join(parts)
    best = stats.get("best_ev_pct")
    if best is not None and int(stats.get("ev_in_band") or 0) == 0:
        line += f" · meilleure EV favori : **{float(best):+.1f} %**"
    return line


def _filter_matches_favorite_ev_band(
    matches: list[dict],
    *,
    ev_min_frac: float | None,
    ev_max_frac: float | None,
) -> list[dict]:
    if ev_min_frac is None and ev_max_frac is None:
        return list(matches)
    out: list[dict] = []
    for m in matches:
        if _passes_favorite_ev_band(_match_favorite_model_metrics(m), ev_min_frac=ev_min_frac, ev_max_frac=ev_max_frac):
            out.append(m)
    return out


def _collect_top_model_prob_rows(
    matches: list[dict],
    *,
    limit: int = TOP_PROBAS_DISPLAY_LIMIT,
    ev_min_frac: float | None = None,
    ev_max_frac: float | None = None,
    today_only: bool = True,
) -> list[dict]:
    """Lignes numériques top N (favori = max(capped_p1_prob, 1-p1)), tri proba décroissante."""
    rows: list[dict] = []
    from scripts.match_rank_quality import passes_data_reliability_filter

    for m in matches:
        if today_only and not _is_today_calendar_match(m):
            continue
        if not passes_data_reliability_filter(m):
            continue
        met = _match_favorite_model_metrics(m)
        if not _passes_favorite_ev_band(met, ev_min_frac=ev_min_frac, ev_max_frac=ev_max_frac):
            continue
        rows.append(dict(met))
    rows.sort(key=lambda r: -float(r["fav_p"]))
    return rows[: max(0, int(limit))]


def _build_top_model_probs_df(
    matches: list[dict],
    *,
    limit: int = TOP_PROBAS_DISPLAY_LIMIT,
    ev_min_frac: float | None = None,
    ev_max_frac: float | None = None,
    today_only: bool = True,
) -> pd.DataFrame:
    """Top N matchs du jour — tableau affichage (voir docs/CHART_TOP_PROBAS_JOUR.md)."""
    rows = _collect_top_model_prob_rows(
        matches,
        limit=limit,
        ev_min_frac=ev_min_frac,
        ev_max_frac=ev_max_frac,
        today_only=today_only,
    )
    if not rows:
        return pd.DataFrame(
            columns=[
                "Proba fav",
                "P1 %",
                "Tour",
                "Favori modèle",
                "Adversaire",
                "Tournoi",
                "Cotes F / U",
                "EV favori",
                "Gap book (pp)",
            ]
        )
    display = [
        {
            "Proba fav": f"{r['fav_p'] * 100:.1f} %",
            "P1 %": f"{r['p1'] * 100:.1f} %",
            "Tour": r["tour"],
            "Favori modèle": r["fav"],
            "Adversaire": r["und"],
            "Tournoi": r["tournament"],
            "Cotes F / U": r["cotes"],
            "EV favori": (
                f"{float(r['ev_fav_pct']):+.1f} %"
                if r.get("ev_fav_pct") is not None
                else "—"
            ),
            "Gap book (pp)": r["gap_s"],
        }
        for r in rows
    ]
    out = pd.DataFrame(display)
    out.insert(0, "#", range(1, len(out) + 1))
    return out


def _build_top_probas_day_chart(rows: list[dict]) -> alt.Chart | None:
    """Graphique Altair horizontal : proba modèle (barres) vs marché (ticks). Spec : CHART_TOP_PROBAS_JOUR.md."""
    if not rows:
        return None
    chart_rows = []
    for i, r in enumerate(rows, start=1):
        book_pct = r.get("book_implied_pct")
        chart_rows.append(
            {
                "rang": i,
                "favori_label": r["favori_label"],
                "favori": r["fav"],
                "adversaire": r["und"],
                "tour": r["tour"] or "—",
                "proba_modele_pct": round(float(r["fav_p"]) * 100.0, 2),
                "ev_fav_pct": round(float(r["ev_fav_pct"]), 2) if r.get("ev_fav_pct") is not None else None,
                "proba_book_pct": round(float(book_pct), 2) if book_pct is not None else None,
                "gap_pp": r.get("gap_pp"),
                "tournoi": r["tournament"],
            }
        )
    df = pd.DataFrame(chart_rows)
    # Ordre explicite axe Y : proba croissante → la plus forte en haut (évite tri alpha Altair).
    df = df.sort_values("proba_modele_pct", ascending=True)
    label_order = df["favori_label"].astype(str).tolist()

    y_enc = alt.Y(
        "favori_label:N",
        sort=label_order,
        scale=alt.Scale(domain=label_order),
        title="Favori modèle",
        axis=alt.Axis(labelLimit=200),
    )
    x_enc = alt.X(
        "proba_modele_pct:Q",
        title="Probabilité (%)",
        scale=alt.Scale(domain=[0, 100]),
    )
    color_enc = alt.Color(
        "tour:N",
        scale=alt.Scale(
            domain=list(_TOP_PROBAS_CHART_TOUR_COLORS.keys()),
            range=list(_TOP_PROBAS_CHART_TOUR_COLORS.values()),
        ),
        legend=alt.Legend(title="Circuit"),
    )
    tooltip = [
        alt.Tooltip("rang:O", title="Rang"),
        alt.Tooltip("favori:N", title="Favori modèle"),
        alt.Tooltip("adversaire:N", title="Adversaire"),
        alt.Tooltip("tour:N", title="Circuit"),
        alt.Tooltip("tournoi:N", title="Tournoi"),
        alt.Tooltip("proba_modele_pct:Q", title="Proba modèle (%)", format=".1f"),
        alt.Tooltip("ev_fav_pct:Q", title="EV favori (%)", format=".1f"),
        alt.Tooltip("proba_book_pct:Q", title="Proba book implicite (%)", format=".1f"),
        alt.Tooltip("gap_pp:Q", title="Gap book (pp)", format=".1f"),
    ]

    bars = (
        alt.Chart(df)
        .mark_bar(cornerRadiusEnd=3)
        .encode(x=x_enc, y=y_enc, color=color_enc, tooltip=tooltip)
    )

    ref_df = pd.DataFrame({"pct": list(_TOP_PROBAS_CHART_REF_LINES)})
    ref_lines = (
        alt.Chart(ref_df)
        .mark_rule(color="#5a6478", strokeDash=[4, 4], opacity=0.55)
        .encode(x=alt.X("pct:Q"))
    )

    layers: list[alt.Chart] = [ref_lines, bars]
    df_book = df.dropna(subset=["proba_book_pct"])
    if not df_book.empty:
        book_ticks = (
            alt.Chart(df_book)
            .mark_tick(color=_TOP_PROBAS_CHART_BOOK_COLOR, thickness=3, size=14)
            .encode(
                x=alt.X("proba_book_pct:Q"),
                y=y_enc,
                tooltip=tooltip,
            )
        )
        layers.append(book_ticks)

    height = max(360, 26 * len(df) + 48)
    return (
        alt.layer(*layers)
        .resolve_scale(color="independent")
        .properties(
            height=height,
            title="Top probas (favori modèle) — barres = proba · trait jaune = book",
        )
        .configure_axis(gridColor="#2a3142", labelColor="#e0e4ef", titleColor="#e0e4ef")
        .configure_view(strokeWidth=0)
    )


def _style_top_model_probs_df(df: pd.DataFrame):
    """Surbrillance colonne « Favori modèle » ; gap book ≥ 25 pp en orange."""
    fav_col = "Favori modèle"
    gap_col = "Gap book (pp)"

    def _row_style(row: pd.Series) -> list[str]:
        styles = [""] * len(row)
        if fav_col in row.index:
            styles[row.index.get_loc(fav_col)] = (
                "background-color: rgba(0, 176, 255, 0.18); color: #FFFFFF; font-weight: 700; "
                "border: 1px solid rgba(0, 176, 255, 0.4)"
            )
        if gap_col in row.index:
            try:
                g = float(str(row[gap_col]).replace("—", "").strip())
                if g >= 25.0:
                    styles[row.index.get_loc(gap_col)] = "color: #ffb74d; font-weight: 600"
            except (TypeError, ValueError):
                pass
        return styles

    try:
        return df.style.apply(_row_style, axis=1)
    except Exception:
        return df


def _filter_matches_top_probas_scope(
    matches: list[dict],
    *,
    include_challengers: bool,
) -> list[dict]:
    """Défaut = main draw 250+ ; option = même périmètre que Live Tracker (+ Challengers)."""
    from scripts.tournament_tier import is_major_tournament_match

    out: list[dict] = []
    for m in matches:
        if include_challengers:
            if _is_atp_wta_circuit_match(
                m.get("category"),
                m.get("tournament"),
                include_challengers=True,
            ):
                out.append(m)
        elif is_major_tournament_match(m):
            out.append(m)
    return out


def _render_top_model_probs_panel(
    matches: list[dict],
    *,
    today_only: bool = True,
    chart_key_prefix: str = "top_probas",
    pool_label: str | None = None,
) -> None:
    """Graphique + tableau top N (partagé onglet Top probas et Live Tracker)."""
    ev_min_frac, ev_max_frac = _favorite_ev_band_params()
    n_pool = sum(1 for m in matches if (not today_only) or _is_today_calendar_match(m))
    if pool_label:
        st.caption(pool_label)
    df_top = _build_top_model_probs_df(
        matches,
        limit=TOP_PROBAS_DISPLAY_LIMIT,
        ev_min_frac=ev_min_frac,
        ev_max_frac=ev_max_frac,
        today_only=today_only,
    )
    if df_top.empty:
        _funnel = _compute_favorite_ev_funnel_stats(
            matches,
            today_only=today_only,
            ev_min_frac=ev_min_frac,
            ev_max_frac=ev_max_frac,
        )
        st.info(
            "Aucune ligne à afficher dans le top probas."
        )
        st.caption(_format_favorite_ev_funnel_caption(
            _funnel,
            ev_min_frac=ev_min_frac,
            ev_max_frac=ev_max_frac,
            top_n=TOP_PROBAS_DISPLAY_LIMIT,
        ))
        if ev_min_frac is not None and int(_funnel.get("with_metrics") or 0) > 0:
            st.caption(
                "Le filtre EV favori est actif — désactivez-le ci-dessus pour voir "
                "toutes les probas du jour."
            )
        elif int(_funnel.get("today") or 0) == 0 and int(_funnel.get("total") or 0) > 0:
            st.caption(
                f"**{n_pool}** match(s) dans le périmètre calendrier, "
                "mais aucun daté aujourd'hui (Europe/Paris)."
            )
        return
    _rows = _collect_top_model_prob_rows(
        matches,
        limit=TOP_PROBAS_DISPLAY_LIMIT,
        ev_min_frac=ev_min_frac,
        ev_max_frac=ev_max_frac,
        today_only=today_only,
    )
    st.caption(f"**{len(df_top)}** lignes affichées sur **{n_pool}** match(s) dans le périmètre.")

    _snap_meta = snapshot_meta()
    _chart = _build_top_probas_day_chart(_rows)
    if _chart is not None:
        _chart_key = (
            f"{chart_key_prefix}_{(_snap_meta or {}).get('built_at', 'na')}_{len(_rows)}_"
            f"{'evband' if _favorite_ev_band_is_active() else 'all'}"
        )
        st.altair_chart(_chart, use_container_width=True, key=_chart_key)
        st.caption(
            "Graphique défini dans `docs/CHART_TOP_PROBAS_JOUR.md` · "
            "barres = **proba favori modèle** (`capped_p1_prob`) · trait jaune = **proba implicite book** "
            f"(1/cote favori) · pointillés = {', '.join(f'{int(x)} %' for x in _TOP_PROBAS_CHART_REF_LINES)}."
        )

    st.dataframe(
        _style_top_model_probs_df(df_top),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "Recalculé à chaque rafraîchissement depuis le snapshot live. "
        f"Un **gap book** ≥ {int(_TOP_PROBAS_GAP_WARN_PP)} pp signale un écart fort modèle vs marché (informatif)."
    )


def _render_top_model_probs_tab() -> None:
    """Onglet « Top probas jour » — tableau top 15 depuis le snapshot live."""
    st.header(f"📈 Meilleures probas modèle (top {TOP_PROBAS_DISPLAY_LIMIT})")
    _today_paris = datetime.now(_PARIS_TZ).date()
    st.caption(
        f"Matchs du **{_today_paris.isoformat()}** (fuseau Europe/Paris) · "
        f"proba = `capped_p1_prob` du snapshot · favori modèle en **surbrillance**."
    )
    _snap_meta = snapshot_meta()
    if _snap_meta and int(_snap_meta.get("n_matches") or 0) > 0:
        st.caption(
            f"Source : snapshot disque · **{_snap_meta['n_matches']}** matchs · "
            f"construit {_format_built_at_paris(_snap_meta['built_at'])}."
        )
    elif snapshot_build_in_progress():
        st.info("Snapshot en cours de construction — le tableau se remplira à la fin du build.")
    else:
        st.warning("Aucun snapshot — lancez `py -3 scripts/rebuild_live_projection.py` ou le pipeline matin.")

    matches = list(st.session_state.get("_live_matches_cache") or [])
    if not matches:
        matches = list(_hydrate_live_matches_from_disk() or [])

    st.divider()
    _col_ev, _col_chal = st.columns([3, 1])
    with _col_ev:
        _render_favorite_ev_band_toggle(
            widget_key=FAVORITE_EV_BAND_TOGGLE_KEY_TOPPROBAS,
            label=(
                f"Top {TOP_PROBAS_DISPLAY_LIMIT} · EV favori +15 % à +100 % "
                f"(tri proba favori ↓)"
            ),
        )
    with _col_chal:
        _include_challengers = st.checkbox(
            "Inclure les Challengers",
            value=False,
            key="topprobas_include_challengers",
            help=(
                "Par défaut : uniquement les tournois **main draw ATP/WTA 250+** "
                "(aligné Paris du jour). Cochez pour inclure aussi les Challengers "
                "(ITF, UTR et futures restent exclus)."
            ),
        )
    matches = _filter_matches_top_probas_scope(
        matches, include_challengers=_include_challengers
    )
    if _include_challengers:
        st.caption(
            "Challengers ATP/WTA inclus. ITF, UTR et tournois « futures » restent exclus."
        )
    else:
        st.caption(
            "Périmètre : **main draw ATP/WTA 250+** uniquement "
            "(Challengers, ITF, UTR exclus — comme Paris du jour)."
        )
    _render_top_model_probs_panel(matches, today_only=True, chart_key_prefix="top_probas_tab")


_LIVE_TRACKER_TAB_INDEX = 2  # 0=Paris du jour, 1=Portefeuille, 2=Live Tracker


def _focus_streamlit_tab_index(index: int) -> None:
    """Active un onglet Streamlit par index (hack DOM, best-effort)."""
    import streamlit.components.v1 as components

    components.html(
        f"""
        <script>
        (function () {{
          const doc = window.parent.document;
          const tabs = doc.querySelectorAll('[data-baseweb="tab-list"] button[role="tab"]');
          if (tabs && tabs[{int(index)}]) tabs[{int(index)}].click();
        }})();
        </script>
        """,
        height=0,
    )


def _apply_pending_live_tracker_link_from_paris_du_jour() -> None:
    """Applique les filtres Live Tracker avant instanciation des widgets (depuis Paris du jour)."""
    player = st.session_state.pop("_pending_live_player_name_filter", None)
    if player is not None:
        st.session_state["live_player_name_filter"] = str(player).split("(")[0].strip()
    circuit = st.session_state.pop("_pending_live_circuit_filter", None)
    if circuit is not None:
        c = str(circuit).upper()
        st.session_state["live_circuit_filter"] = c if c in {"ATP", "WTA"} else "Tous"
    if st.session_state.pop("_pending_live_tracker_notice", False):
        st.session_state["_live_tracker_prefill_notice"] = True
    if st.session_state.pop("_pending_live_goto_tracker", False):
        st.session_state["_live_tracker_focus_tab"] = True


def _collect_top_favorite_action_cards(
    matches: list[dict],
    *,
    limit: int = 5,
) -> list[dict]:
    """Top favoris modèle du jour (EV favori 15–100 %) triés par proba décroissante."""
    ev_min_frac = FAVORITE_EV_BAND_MIN_FRAC
    ev_max_frac = FAVORITE_EV_BAND_MAX_FRAC
    cards: list[dict] = []
    for m in matches:
        if not _is_today_calendar_match(m):
            continue
        met = _match_favorite_model_metrics(m)
        if not met:
            continue
        if not _passes_favorite_ev_band(met, ev_min_frac=ev_min_frac, ev_max_frac=ev_max_frac):
            continue
        cards.append({"match": m, "metrics": met})
    cards.sort(key=lambda x: -float((x.get("metrics") or {}).get("fav_p") or 0.0))
    return cards[: max(0, int(limit))]


def _render_top5_proba_action_tab() -> None:
    """Onglet épuré : top 5 probas favori avec saisie cote réelle + mise Kelly/Brier."""
    if st.session_state.pop("_live_link_success_toast", False):
        st.toast("Filtre appliqué — onglet Live Tracker ouvert.", icon="↪")
    st.header("🎯 Top 5 proba · Action rapide")
    st.caption(
        "Top 5 favoris modèle du jour · **EV favori +15 % à +100 %** · "
        "cote modifiable, mise reco Kelly/Brier, enregistrement direct portefeuille."
    )
    _today_paris = datetime.now(_PARIS_TZ).date().isoformat()
    st.caption(
        f"Périmètre : matchs du **{_today_paris}** (Europe/Paris) · "
        f"tri **proba favori modèle** ↓ · EV = p_fav × cote_fav − 1."
    )

    matches = _load_today_tracked_matches_for_inplay()
    _funnel = _compute_favorite_ev_funnel_stats(
        matches,
        today_only=True,
        ev_min_frac=FAVORITE_EV_BAND_MIN_FRAC,
        ev_max_frac=FAVORITE_EV_BAND_MAX_FRAC,
    )
    if not matches:
        st.info(
            "Aucun match du jour dans le pool Paris du jour "
            "(snapshot + cotes + rang + ATP/WTA majeur)."
        )
        st.caption(_format_favorite_ev_funnel_caption(_funnel, top_n=5))
        st.caption(
            "Utilisez **Actualiser le Live Tracker** si le snapshot est vide, "
            "ou vérifiez qu'il reste des matchs ATP/WTA du jour."
        )
        return

    cards = _collect_top_favorite_action_cards(matches, limit=5)
    if not cards:
        st.caption(_format_favorite_ev_funnel_caption(
            _funnel,
            ev_min_frac=FAVORITE_EV_BAND_MIN_FRAC,
            ev_max_frac=FAVORITE_EV_BAND_MAX_FRAC,
            top_n=5,
        ))
        if int(_funnel.get("with_metrics") or 0) > 0:
            _ev_band = (
                f"+{int(FAVORITE_EV_BAND_MIN_FRAC * 100)} % à "
                f"+{int(FAVORITE_EV_BAND_MAX_FRAC * 100)} %"
            )
            st.warning(
                f"Aucun pick Top 5 — bande EV {_ev_band} : "
                f"**0** / **{_funnel['with_metrics']}** match(s) éligible(s). "
                "Consultez **Top probas jour** (toggle EV off) ou attendez de meilleures cotes."
            )
        else:
            st.info("Pool du jour OK mais cotes ou probas modèle manquantes sur tous les matchs.")
        return

    _bets_sig = _user_bets_state_signature()
    bets_index = _cached_existing_bets_index(_bets_sig)
    br_snap = _cached_live_tracker_bankroll_snapshot(_bets_sig)
    br_avail = max(0.0, float(br_snap.get("available_eur") or 0.0))
    st.caption(
        f"Bankroll Kelly (Live Tracker + Paris du jour + in-play) : **{br_avail:.2f} €** "
        f"— les mises enregistrées ici déduisent cette BR."
    )

    for i, card in enumerate(cards, start=1):
        match = dict(card.get("match") or {})
        met = dict(card.get("metrics") or {})
        fav_side = int(met.get("fav_side") or 0)
        if fav_side not in (1, 2):
            continue
        player_name = str(match.get("player1") if fav_side == 1 else match.get("player2") or "")
        opp_name = str(match.get("player2") if fav_side == 1 else match.get("player1") or "")
        match_name = f"{match.get('player1', '')} vs {match.get('player2', '')}"
        stake_already = float(bets_index.get((match_name, player_name), 0.0))
        _tour = str(match.get("tour") or match.get("category") or "").upper()
        _mid = str(match.get("prematch_id") or match.get("match_id") or f"row{i}")
        widget_uid = f"top5proba_{_mid}_{i}_p{fav_side}"
        try:
            odd_book = float(met.get("odd_fav") or 0.0)
        except (TypeError, ValueError):
            odd_book = 0.0
        if odd_book <= 1.0:
            try:
                odd_book = float(match.get("odd_p1") if fav_side == 1 else match.get("odd_p2") or 0.0)
            except (TypeError, ValueError):
                odd_book = 0.0
        odd_true = (
            float(match.get("true_odd_p1") or 0.0)
            if fav_side == 1
            else float(match.get("true_odd_p2") or 0.0)
        )
        p_fav = float(met.get("fav_p") or 0.0)
        ev_fav_pct = met.get("ev_fav_pct")
        _seg_key = _match_brier_segment_key(match)
        _seg_brier = _match_segment_brier(match)

        with st.container(border=True):
            if stake_already > 0:
                st.markdown(
                    (
                        "<div style='background:rgba(46,160,67,0.18);border:1px solid rgba(46,160,67,0.45);"
                        "padding:8px 10px;border-radius:8px;margin-bottom:8px;'>"
                        f"✅ Pari déjà enregistré : <b>{stake_already:.2f} €</b> sur <b>{player_name}</b>"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )

            st.markdown(f"### {i}. {player_name} vs {opp_name}")
            st.caption(
                f"**{match.get('tournament', '—')}** · {_tour or '—'} · "
                f"{match.get('time', '—')} · Segment {_seg_key} ({_seg_brier:.3f})"
            )
            _m1, _m2, _m3, _m4 = st.columns(4)
            _m1.metric("Proba favori", f"{p_fav * 100:.1f}%")
            _m2.metric("Cote book fav", f"{odd_book:.2f}" if odd_book > 1.0 else "—")
            _m3.metric(
                "EV favori",
                f"{float(ev_fav_pct):+.1f}%" if ev_fav_pct is not None else "—",
            )
            _m4.metric("Gap book (pp)", met.get("gap_s") or "—")

            _odd_key = f"top5proba_custom_odd_{widget_uid}"
            _default_odd = odd_book if odd_book > 1.0 else 1.01
            custom_odd = st.number_input(
                "Cote réelle",
                min_value=1.01,
                max_value=100.0,
                value=float(_default_odd),
                step=0.05,
                key=_odd_key,
                help=(
                    "Préremplie avec la cote détectée dans le snapshot live. "
                    "La reco Kelly/Brier et la mise préremplie utilisent cette cote."
                ),
            )

            reco_frac, reco_eur, reco_pct = _kelly_reco_from_live_proba(
                br_avail,
                live_proba=p_fav,
                custom_odd=float(custom_odd),
                match=match,
            )
            _cap_pct = int(round(KELLY_RECO_BANKROLL_CAP_FRAC * 100.0))
            _k_label = "1/2" if float(KELLY_RECO_ADAPTIVE_BASE_FRAC) >= 0.5 else "1/4"
            st.caption(
                f"Mise reco Kelly {_k_label} x Brier (cap {_cap_pct}% BR) : "
                f"**{reco_eur:.2f} €** ({reco_pct:.2f}% BR dispo)"
            )

            _max_stake = max(0.01, float(br_avail))
            default_stake = max(
                0.01,
                min(round(reco_eur if reco_eur > 0 else 0.5, 2), _max_stake),
            )
            _stake_key = (
                f"top5proba_stake_{widget_uid}|"
                f"odd{round(float(custom_odd),4)}|"
                f"br{round(float(br_avail),2)}"
            )
            stake_eur = st.number_input(
                "Mise (€)",
                min_value=0.01,
                max_value=_max_stake,
                value=float(default_stake),
                step=0.5,
                key=_stake_key,
                help=(
                    "Préremplie avec la reco Kelly/Brier calculée sur la cote réelle "
                    "et la bankroll disponible. Se met à jour quand l'une des deux change."
                ),
            )

            _c1, _c2 = st.columns([2, 2])
            with _c1:
                _avail_ok = br_avail >= float(stake_eur) - 1e-6
                if st.button(
                    f"Parier {float(stake_eur):.2f} € sur {player_name}",
                    key=f"top5proba_bet_{widget_uid}",
                    type="primary",
                    disabled=(not _avail_ok or br_avail < 0.01),
                ):
                    if not _avail_ok:
                        st.error("Mise supérieure à la bankroll disponible.")
                    else:
                        save_bet(
                            match_name,
                            player_name,
                            float(custom_odd),
                            float(stake_eur),
                            tour=_tour or None,
                            surface=match.get("surface"),
                            tournament=match.get("tournament"),
                            match_id=_mid or None,
                            segment_key=_seg_key or None,
                            match_date=match.get("date"),
                            p_model=p_fav,
                            ev_at_bet=(float(ev_fav_pct) / 100.0) if ev_fav_pct is not None else None,
                            bookmaker_source=(
                                "custom" if abs(float(custom_odd) - float(odd_book)) > 1e-3 else "book_avg"
                            ),
                            notes="top5_proba_action_tab",
                            tracker_source="top5_proba_action",
                        )
                        st.rerun()
            with _c2:
                if st.button("↪ Ouvrir ce match dans Live Tracker", key=f"top5proba_link_{widget_uid}"):
                    st.session_state["_pending_live_player_name_filter"] = (
                        player_name.split("(")[0].strip()
                    )
                    st.session_state["_pending_live_circuit_filter"] = (
                        _tour if _tour in {"ATP", "WTA"} else "Tous"
                    )
                    st.session_state["_pending_live_tracker_notice"] = True
                    st.session_state["_pending_live_goto_tracker"] = True
                    st.session_state["_live_link_success_toast"] = True
                    st.rerun()


def _load_today_tracked_matches_for_inplay() -> list[dict]:
    """Matchs du snapshot Live Tracker limités au jour courant et aux garde-fous qualité."""
    matches = list(_hydrate_live_matches_from_disk() or [])
    out: list[dict] = []
    for m in matches:
        try:
            if float(m.get("odd_p1") or 0.0) <= 1.0 or float(m.get("odd_p2") or 0.0) <= 1.0:
                continue
        except (TypeError, ValueError):
            continue
        if not _match_has_rank_points_source(m):
            continue
        from scripts.tournament_tier import is_major_tournament_match

        if not is_major_tournament_match(m):
            continue
        if not _is_today_calendar_match(m):
            continue
        out.append(m)
    return out


def _prematch_proba_p1(match: dict) -> float:
    try:
        ot = float(match.get("true_odd_p1") or 0.0)
        if ot > 1.0:
            return min(1.0, max(0.0, 1.0 / ot))
    except (TypeError, ValueError):
        pass
    return 0.5


def _kelly_reco_from_live_proba(
    br_avail: float,
    *,
    live_proba: float,
    custom_odd: float,
    match: dict,
) -> tuple[float, float, float]:
    """Retourne (reco_frac, reco_eur, reco_pct) pour une proba live et cote saisie."""
    try:
        p_model_side = min(1.0, max(0.0, float(live_proba)))
        b_side = max(0.01, float(custom_odd) - 1.0)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0
    kelly_full = max(0.0, (b_side * p_model_side - (1.0 - p_model_side)) / b_side)
    kelly_partial = float(KELLY_RECO_ADAPTIVE_BASE_FRAC) * kelly_full
    _brier_s = _match_segment_brier(match)
    _kelly_adj = max(0.0, 1.0 - (_brier_s / 0.25))
    reco_frac = max(0.0, min(kelly_partial * _kelly_adj, KELLY_RECO_BANKROLL_CAP_FRAC))
    reco_eur = float(br_avail) * reco_frac
    reco_pct = reco_frac * 100.0
    return reco_frac, reco_eur, reco_pct


def _inplay_match_uid(match: dict, idx: int = 0) -> str:
    return str(match.get("prematch_id") or match.get("id") or f"row_{idx}")


def _compute_inplay_opportunity(
    match: dict,
    *,
    sets_p1: int,
    sets_p2: int,
    game_p1: int,
    game_p2: int,
    status_first_set_in_progress: bool,
    best_of: int,
    live_odd_p1: float,
    live_odd_p2: float,
) -> dict | None:
    """Calcule EV live P1/P2 si le match est encore dans le 1er set ; sinon None."""
    score_state = build_manual_score_state(
        sets_p1=sets_p1,
        sets_p2=sets_p2,
        game_p1=game_p1,
        game_p2=game_p2,
        status_first_set_in_progress=status_first_set_in_progress,
        best_of=best_of,
    )
    if not is_first_set_in_progress(score_state, best_of=best_of):
        return None

    prior_p1 = _prematch_proba_p1(match)
    try:
        live_p1 = calculate_live_probability(
            prior_p1,
            (int(sets_p1), int(sets_p2)),
            best_of=int(best_of),
            live_odd=float(live_odd_p1) if float(live_odd_p1) > 1.0 else None,
        )
    except ValueError:
        return None

    live_p2_proba = 1.0 - float(live_p1.live_proba)
    ev_p2 = None
    if float(live_odd_p2) > 1.0:
        ev_p2 = live_p2_proba * float(live_odd_p2) - 1.0

    return {
        "score_state": score_state,
        "prior_p1": prior_p1,
        "prior_p2": 1.0 - prior_p1,
        "live_p1": live_p1,
        "live_p2_proba": live_p2_proba,
        "ev_p1": live_p1.ev_live,
        "ev_p2": ev_p2,
    }


def _render_inplay_bet_side(
    match: dict,
    *,
    side: int,
    br_avail: float,
    br_stake_sig: str,
    bets_index: dict,
    match_unreliable: bool,
    live_proba: float,
    prior_proba: float,
    ev_live_frac: float | None,
    live_odd: float,
    inplay_ctx: dict,
) -> None:
    p_num = int(side)
    player_name = str(match.get("player1") if p_num == 1 else match.get("player2") or "")
    opp_name = str(match.get("player2") if p_num == 1 else match.get("player1") or "")
    odd_book = float(match.get("odd_p1") if p_num == 1 else match.get("odd_p2") or 0.0)
    match_name = f"{match.get('player1', '')} vs {match.get('player2', '')}"
    mid = str(inplay_ctx.get("match_uid") or "")
    widget_uid = f"inplay_{mid}_p{p_num}"
    stake_already = float(bets_index.get((match_name, player_name), 0.0))
    ps = match.get("p1_stats") if p_num == 1 else match.get("p2_stats") or {}
    rank = (ps or {}).get("rank", "—")

    st.markdown(f"**{player_name}** · rang #{rank}")
    st.caption(f"vs {opp_name}")
    if stake_already > 0:
        st.info(f"Déjà parié : {stake_already:.2f} €")
    if match_unreliable:
        st.error("Signal ML non fiable sur ce match (voir alerte).", icon="🚫")

    odd_true = float(match.get("true_odd_p1") if p_num == 1 else match.get("true_odd_p2") or 0.0)
    if odd_true <= 1.0:
        odd_true = 1.0 / max(float(live_proba), 0.01)
    try:
        _vb = ValueDetector(min_value_threshold=0.0).detect_value(
            float(odd_book), float(odd_true), confidence=None
        )
        ev_book_pct = float(_vb.get("value_pct") or 0.0)
    except Exception:
        ev_book_pct = 0.0
    ev_custom_pct = live_ev_pct(ev_live_frac) if ev_live_frac is not None else 0.0
    _ev_panel = _ev_comparison_panel_html(
        odd_book=float(odd_book),
        odd_true=float(odd_true),
        custom_odd=float(live_odd),
        ev_book_pct=ev_book_pct,
        ev_custom_pct=ev_custom_pct,
    )
    if _ev_panel:
        st.markdown(_ev_panel, unsafe_allow_html=True)
    st.caption(
        f"Prior V47 : **{prior_proba * 100:.1f}%** · Proba live : **{live_proba * 100:.1f}%**"
    )

    reco_frac, reco_eur, reco_pct = _kelly_reco_from_live_proba(
        br_avail,
        live_proba=float(live_proba),
        custom_odd=float(live_odd),
        match=match,
    )
    st.caption(
        f"Mise reco Kelly 1/2 × Brier : **{reco_eur:.2f} €** ({reco_pct:.2f}% BR dispo)"
    )

    default_stake = max(
        0.01,
        round(min(reco_eur if reco_eur > 0 else 0.5, max(float(br_avail), 0.01)), 2),
    )
    max_stake = max(0.01, float(br_avail))
    stake_key = (
        f"inplay_stake_{widget_uid}_{br_stake_sig}|"
        f"odd{round(float(live_odd), 4)}|p{round(float(live_proba), 4)}"
    )
    stake_eur = st.number_input(
        "Mise (€)",
        min_value=0.01,
        max_value=max_stake,
        value=min(float(default_stake), max_stake),
        step=0.5,
        key=stake_key,
        disabled=match_unreliable or stake_already > 0,
    )
    avail_ok = br_avail >= float(stake_eur) - 1e-6
    btn_disabled = match_unreliable or stake_already > 0 or not avail_ok or br_avail < 0.01
    if st.button(
        f"Parier {float(stake_eur):.2f} €",
        key=f"inplay_bet_{widget_uid}",
        type="primary",
        disabled=btn_disabled,
    ):
        if not avail_ok:
            st.error("Mise supérieure à la bankroll disponible.")
        else:
            note = (
                f"inplay_v2 prior={prior_proba:.3f} live={live_proba:.3f} "
                f"sets={inplay_ctx.get('sets_p1')}-{inplay_ctx.get('sets_p2')} "
                f"game={inplay_ctx.get('game_p1')}-{inplay_ctx.get('game_p2')} "
                f"bo={inplay_ctx.get('best_of')} first_set={int(bool(inplay_ctx.get('first_set_live')))}"
            )
            save_bet(
                match_name,
                player_name,
                float(live_odd),
                float(stake_eur),
                tour=str(match.get("tour") or match.get("category") or "").upper() or None,
                surface=match.get("surface"),
                tournament=match.get("tournament"),
                match_id=str(mid) if mid else None,
                segment_key=_match_brier_segment_key(match) or None,
                match_date=match.get("date"),
                p_model=float(live_proba),
                ev_at_bet=float(ev_live_frac) if ev_live_frac is not None else None,
                bookmaker_source="live_manual",
                notes=note,
                tracker_source="live_inplay_manual",
            )
            _clear_portfolio_runtime_caches()
            st.success(f"Pari live enregistré sur {player_name}.")
            st.rerun()


def _render_live_inplay_tab() -> None:
    st.header("⚡ Pari Live In-Play")
    st.caption(
        "Paris affichés uniquement pour les matchs **en cours dans le 1er set**. "
        "La **proba live** utilise le prior V47 et la cote live saisie ; "
        f"seules les opportunités avec **EV live > {DEFAULT_LIVE_EV_THRESHOLD_PCT:.0f} %** sont proposées."
    )

    matches = _load_today_tracked_matches_for_inplay()
    if not matches:
        st.info(
            "Aucun match du jour dans le snapshot. Ouvre le Live Tracker ou lance "
            "**Actualiser le Live Tracker**, puis reviens ici."
        )
        return

    _bets_sig = _user_bets_state_signature()
    br_snap = _cached_live_tracker_bankroll_snapshot(_bets_sig)
    br_avail = max(0.0, float(br_snap["available_eur"]))
    br_stake_sig = (
        f"{round(br_avail, 2)}|{round(float(br_snap.get('committed_open_eur', 0)), 2)}|"
        f"{round(float(br_snap.get('manual_adjust_eur', 0.0)), 2)}"
    )

    filt_c1, filt_c2, filt_c3 = st.columns(3)
    with filt_c1:
        inplay_q = st.text_input("Joueur", "", key="inplay_player_q", placeholder="ex. Sinner")
    with filt_c2:
        tourneys = ["Tous"] + sorted({str(m.get("tournament") or "") for m in matches if m.get("tournament")})
        inplay_tourney = st.selectbox("Tournoi", tourneys, key="inplay_tourney_filter")
    with filt_c3:
        inplay_circuit = st.selectbox("Circuit", ["Tous", "ATP", "WTA"], key="inplay_circuit_filter")

    bets_index = _cached_existing_bets_index(_bets_sig)

    q_l = str(inplay_q or "").strip().lower()
    filtered = matches
    if q_l:
        filtered = [
            m
            for m in filtered
            if q_l in str(m.get("player1", "")).lower() or q_l in str(m.get("player2", "")).lower()
        ]
    if inplay_tourney != "Tous":
        filtered = [m for m in filtered if m.get("tournament") == inplay_tourney]
    if inplay_circuit != "Tous":
        filtered = [m for m in filtered if _match_circuit(m) == inplay_circuit]

    if not filtered:
        st.info("Aucun match ne correspond aux filtres.")
        return

    st.markdown("#### Configuration live (saisie manuelle)")
    st.caption(
        "Renseigne le score en sets/jeux, confirme que le match est **en cours dans le 1er set**, "
        "le format BO3/BO5 et les cotes live. "
        f"Les cartes de pari n'apparaissent que si la fenêtre est valide et l'EV live dépasse {DEFAULT_LIVE_EV_THRESHOLD_PCT:.0f} %."
    )

    configured_rows: list[dict] = []
    for idx, match in enumerate(filtered):
        mid = _inplay_match_uid(match, idx)
        match_name = f"{match.get('player1', '')} vs {match.get('player2', '')}"
        with st.expander(f"Configurer · {match_name}", expanded=False):
            cfg1, cfg2, cfg3, cfg4, cfg5, cfg6, cfg7 = st.columns([1, 1, 1, 1, 1.2, 1.2, 1.4])
            with cfg1:
                sets_p1 = st.number_input(
                    "Sets P1",
                    min_value=0,
                    max_value=3,
                    value=int(st.session_state.get(f"inplay_{mid}_sets_p1", 0)),
                    step=1,
                    key=f"inplay_{mid}_sets_p1",
                )
            with cfg2:
                sets_p2 = st.number_input(
                    "Sets P2",
                    min_value=0,
                    max_value=3,
                    value=int(st.session_state.get(f"inplay_{mid}_sets_p2", 0)),
                    step=1,
                    key=f"inplay_{mid}_sets_p2",
                )
            with cfg3:
                game_p1 = st.number_input(
                    "Jeux P1",
                    min_value=0,
                    max_value=7,
                    value=int(st.session_state.get(f"inplay_{mid}_game_p1", 0)),
                    step=1,
                    key=f"inplay_{mid}_game_p1",
                )
            with cfg4:
                game_p2 = st.number_input(
                    "Jeux P2",
                    min_value=0,
                    max_value=7,
                    value=int(st.session_state.get(f"inplay_{mid}_game_p2", 0)),
                    step=1,
                    key=f"inplay_{mid}_game_p2",
                )
            with cfg5:
                best_of = st.selectbox(
                    "Format",
                    options=[3, 5],
                    index=0 if int(st.session_state.get(f"inplay_{mid}_bo", 3)) == 3 else 1,
                    key=f"inplay_{mid}_bo",
                    format_func=lambda x: f"BO{int(x)}",
                )
            with cfg6:
                first_set_live = st.checkbox(
                    "1er set en cours",
                    value=bool(st.session_state.get(f"inplay_{mid}_first_set_live", False)),
                    key=f"inplay_{mid}_first_set_live",
                )
            with cfg7:
                default_odd_p1 = float(match.get("odd_p1") or 1.01)
                default_odd_p2 = float(match.get("odd_p2") or 1.01)
                live_odd_p1 = st.number_input(
                    "Cote live P1",
                    min_value=1.01,
                    max_value=100.0,
                    value=float(st.session_state.get(f"inplay_{mid}_odd_p1", default_odd_p1)),
                    step=0.05,
                    key=f"inplay_{mid}_odd_p1",
                )
                live_odd_p2 = st.number_input(
                    "Cote live P2",
                    min_value=1.01,
                    max_value=100.0,
                    value=float(st.session_state.get(f"inplay_{mid}_odd_p2", default_odd_p2)),
                    step=0.05,
                    key=f"inplay_{mid}_odd_p2",
                )

            opp = _compute_inplay_opportunity(
                match,
                sets_p1=int(sets_p1),
                sets_p2=int(sets_p2),
                game_p1=int(game_p1),
                game_p2=int(game_p2),
                status_first_set_in_progress=bool(first_set_live),
                best_of=int(best_of),
                live_odd_p1=float(live_odd_p1),
                live_odd_p2=float(live_odd_p2),
            )
            if opp is None:
                st.warning("Fenêtre invalide : le match n'est pas dans le 1er set en cours.")
            else:
                ev1 = float(opp.get("ev_p1") or -1.0)
                ev2 = float(opp.get("ev_p2") or -1.0)
                st.caption(
                    f"Prior P1 {opp['prior_p1']*100:.1f}% → Live P1 {opp['live_p1'].live_proba*100:.1f}% · "
                    f"EV P1 {ev1*100:+.1f}% · EV P2 {ev2*100:+.1f}%"
                )
                configured_rows.append(
                    {
                        "match": match,
                        "match_uid": mid,
                        "opp": opp,
                        "sets_p1": int(sets_p1),
                        "sets_p2": int(sets_p2),
                        "game_p1": int(game_p1),
                        "game_p2": int(game_p2),
                        "best_of": int(best_of),
                        "first_set_live": bool(first_set_live),
                        "live_odd_p1": float(live_odd_p1),
                        "live_odd_p2": float(live_odd_p2),
                    }
                )

    qualified = []
    for row in configured_rows:
        opp = row["opp"]
        ev1 = opp.get("ev_p1")
        ev2 = opp.get("ev_p2")
        if (ev1 is not None and float(ev1) > INPLAY_EV_MIN_FRAC) or (
            ev2 is not None and float(ev2) > INPLAY_EV_MIN_FRAC
        ):
            qualified.append(row)

    if MOBILE_COMPACT:
        c1, c2, c3 = st.columns(3)
        c1.metric("Surveillés", len(filtered))
        c2.metric("Opportunités", len(qualified))
        c3.metric("BR dispo", f"{br_avail:.2f} €")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Matchs du jour", len(filtered))
        m2.metric(f"Opportunités EV>{DEFAULT_LIVE_EV_THRESHOLD_PCT:.0f}%", len(qualified))
        m3.metric("BR disponible", f"{br_avail:.2f} €")
        m4.metric("Engagé", f"{float(br_snap.get('committed_open_eur', 0)):.2f} €")

    st.markdown("#### Opportunités In-Play qualifiées")
    if not qualified:
        st.info(
            f"Aucune opportunité live > {DEFAULT_LIVE_EV_THRESHOLD_PCT:.0f} % pour l'instant. "
            "Configure un match ci-dessus en **1er set en cours** avec les cotes observées."
        )
        return

    for row in qualified:
        match = row["match"]
        opp = row["opp"]
        unreliable = _prediction_contradicts_rank_points(match)
        inplay_ctx = {
            "match_uid": row["match_uid"],
            "sets_p1": row["sets_p1"],
            "sets_p2": row["sets_p2"],
            "game_p1": row["game_p1"],
            "game_p2": row["game_p2"],
            "best_of": row["best_of"],
            "first_set_live": row["first_set_live"],
        }
        live_p1: LiveProbabilityResult = opp["live_p1"]
        score_label = f"{row['sets_p1']}-{row['sets_p2']} (jeu {row['game_p1']}-{row['game_p2']})"
        with st.container(border=True):
            st.markdown(f"### {match.get('player1', '')} vs {match.get('player2', '')}")
            st.caption(
                f"{match.get('tournament', '')} ({match.get('category', '')}) · "
                f"BO{row['best_of']} · Score **{score_label}** · 1er set en cours"
            )
            if unreliable:
                _ps1c = match.get("p1_stats") or {}
                _ps2c = match.get("p2_stats") or {}
                st.error(
                    "Probabilité modèle incohérente avec le classement affiché "
                    f"(#{_ps1c.get('rank', '—')} vs #{_ps2c.get('rank', '—')}). "
                    "Paris désactivés sur ce match.",
                    icon="🚫",
                )

            show_p1 = opp.get("ev_p1") is not None and float(opp["ev_p1"]) > INPLAY_EV_MIN_FRAC
            show_p2 = opp.get("ev_p2") is not None and float(opp["ev_p2"]) > INPLAY_EV_MIN_FRAC
            cols = st.columns(2 if show_p1 and show_p2 else 1)
            col_idx = 0
            if show_p1:
                with cols[col_idx]:
                    _render_inplay_bet_side(
                        match,
                        side=1,
                        br_avail=br_avail,
                        br_stake_sig=br_stake_sig,
                        bets_index=bets_index,
                        match_unreliable=unreliable,
                        live_proba=float(live_p1.live_proba),
                        prior_proba=float(opp["prior_p1"]),
                        ev_live_frac=opp.get("ev_p1"),
                        live_odd=float(row["live_odd_p1"]),
                        inplay_ctx=inplay_ctx,
                    )
                col_idx += 1
            if show_p2:
                with cols[col_idx if show_p1 and show_p2 else 0]:
                    _render_inplay_bet_side(
                        match,
                        side=2,
                        br_avail=br_avail,
                        br_stake_sig=br_stake_sig,
                        bets_index=bets_index,
                        match_unreliable=unreliable,
                        live_proba=float(opp["live_p2_proba"]),
                        prior_proba=float(opp["prior_p2"]),
                        ev_live_frac=opp.get("ev_p2"),
                        live_odd=float(row["live_odd_p2"]),
                        inplay_ctx=inplay_ctx,
                    )


def _render_daily_algo_opportunity_report() -> None:
    st.markdown("### 📆 Report journalier algo")
    st.caption(
        "Historique durable des opportunités détectées par le Live Tracker. "
        "La performance théorique simule ta règle Live : tri composite, Kelly 1/2, cap 15%, indexé Brier ; "
        "la performance réelle reprend uniquement les paris effectivement placés."
    )
    _sync_cols = st.columns([1.4, 3.6]) if not MOBILE_COMPACT else st.columns([1, 1])
    with _sync_cols[0]:
        if st.button(
            "🔄 Sync snapshot live",
            key="algo_report_force_sync",
            help="Met à jour les opportunités du jour depuis le snapshot live (cotes, EV, priorité).",
        ):
            try:
                _n_sync = _sync_algo_report_from_snapshot_ui(force=True)
                st.session_state["_algo_report_sync_toast"] = _n_sync
                st.rerun()
            except Exception as exc:
                st.error(f"Sync impossible : {exc}")
    with _sync_cols[1]:
        try:
            _n_auto = _sync_algo_report_from_snapshot_ui(force=False)
            _built = _format_built_at_paris((snapshot_meta() or {}).get("built_at"))
            st.caption(
                f"Dernière sync auto : **{_n_auto}** opportunité(s) indexée(s) · snapshot {_built}."
            )
        except Exception:
            pass
    _sync_toast_n = st.session_state.pop("_algo_report_sync_toast", None)
    if _sync_toast_n is not None:
        st.success(f"Report synchronisé : {int(_sync_toast_n)} opportunité(s) du snapshot live.")
    try:
        dates = read_algo_opportunity_dates()
    except Exception as exc:
        st.warning(f"Report opportunités indisponible : {exc}")
        return
    if not dates:
        st.info(
            "Aucune opportunité historisée pour l'instant. "
            "L'historique démarre dès que le Live Tracker détecte des value bets."
        )
        return

    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    mode_options = ["Journée précise", "7 derniers jours", "30 derniers jours", "Mois courant", "Toutes les dates"]
    rcols = st.columns([1.2, 1.6, 2.2])
    with rcols[0]:
        report_mode = st.selectbox("Période", mode_options, key="algo_report_period")
    with rcols[1]:
        selected_date = st.selectbox(
            "Journée",
            options=dates,
            index=0,
            key="algo_report_day",
            disabled=report_mode != "Journée précise",
        )
    if report_mode == "Journée précise":
        start_date = end_date = selected_date
    elif report_mode == "7 derniers jours":
        start_date = (today - timedelta(days=6)).isoformat()
        end_date = today.isoformat()
    elif report_mode == "30 derniers jours":
        start_date = (today - timedelta(days=29)).isoformat()
        end_date = today.isoformat()
    elif report_mode == "Mois courant":
        start_date = today.replace(day=1).isoformat()
        end_date = today.isoformat()
    else:
        start_date = end_date = None
    with rcols[2]:
        if start_date and end_date:
            st.caption(f"Période analysée : `{start_date}` → `{end_date}`")
        else:
            st.caption(f"Période analysée : toutes les dates ({len(dates)} journée(s)).")

    try:
        report = read_algo_opportunity_report(start_date=start_date, end_date=end_date)
    except Exception as exc:
        st.warning(f"Calcul report opportunités impossible : {exc}")
        return
    rows = report.get("rows") or []
    metrics = report.get("metrics") or {}
    if not rows:
        st.info("Aucune opportunité sur cette période.")
        return

    if MOBILE_COMPACT:
        m1, m2 = st.columns(2)
        m3, m4 = st.columns(2)
        m5, m6 = st.columns(2)
        metric_targets = [m1, m2, m3, m4, m5, m6]
    else:
        metric_targets = st.columns(6)
    metric_targets[0].metric("Détectées", int(metrics.get("n_detected", 0)))
    metric_targets[1].metric("Résolues", int(metrics.get("n_resolved", 0)))
    metric_targets[2].metric("Hit-rate théorique", f"{float(metrics.get('hit_rate_pct', 0.0)):.1f}%")
    metric_targets[3].metric("ROI théorique", f"{float(metrics.get('theoretical_roi_pct', 0.0)):+.1f}%")
    metric_targets[4].metric("Paris réels", int(metrics.get("n_real_bets", 0)))
    metric_targets[5].metric("ROI réel", f"{float(metrics.get('real_roi_pct', 0.0)):+.1f}%")
    st.caption(
        f"BR théorique : **{float(metrics.get('theoretical_start_bankroll', 0.0)):.2f} € → "
        f"{float(metrics.get('theoretical_end_bankroll', 0.0)):.2f} €** "
        f"({float(metrics.get('theoretical_growth_pct', 0.0)):+.1f}%) · "
        f"Profit théorique Kelly : **{float(metrics.get('theoretical_profit_u', 0.0)):+.2f} €** · "
        f"Volume théorique : **{float(metrics.get('theoretical_stake_u', 0.0)):.2f} €** · "
        f"Mise théo moyenne : **{float(metrics.get('avg_theoretical_stake_pct', 0.0)):.2f}% BR** · "
        f"Profit réel : **{float(metrics.get('real_profit', 0.0)):+.2f} €** · "
        f"EV moyenne : **{float(metrics.get('avg_ev_pct', 0.0)):+.1f}%** · "
        f"EV réelle moyenne : **{float(metrics.get('avg_real_ev_pct', 0.0)):+.1f}%** · "
        f"Priorité moyenne : **{float(metrics.get('avg_priority', 0.0)):.4f}**."
    )

    df_report = pd.DataFrame(rows)
    display_cols = [
        "detected_date",
        "match_date",
        "match_name",
        "bet_on",
        "tournament",
        "tour",
        "odd_book",
        "p_model",
        "ev",
        "priority_score",
        "status",
        "theoretical_bankroll_start",
        "theoretical_stake_frac",
        "theoretical_stake_used_frac",
        "theoretical_stake_used_eur",
        "theoretical_profit_eur",
        "theoretical_bankroll_end",
        "linked_bet_id",
        "real_odd",
        "real_stake",
        "real_profit",
    ]
    display_cols = [c for c in display_cols if c in df_report.columns]
    df_display = df_report[display_cols].copy()
    rename = {
        "detected_date": "Détection",
        "match_date": "Match",
        "match_name": "Affiche",
        "bet_on": "Sélection",
        "tournament": "Tournoi",
        "tour": "Circuit",
        "odd_book": "Cote",
        "p_model": "Proba modèle",
        "ev": "EV",
        "priority_score": "Priorité",
        "status": "Statut",
        "theoretical_bankroll_start": "BR matin théo",
        "theoretical_stake_frac": "Mise reco (%BR)",
        "theoretical_stake_used_frac": "Mise ordre composite (%BR)",
        "theoretical_stake_used_eur": "Mise théo (€)",
        "theoretical_profit_eur": "P/L théo (€)",
        "theoretical_bankroll_end": "BR soir théo",
        "linked_bet_id": "Pari réel",
        "real_odd": "Cote réelle",
        "real_stake": "Mise réelle",
        "real_profit": "P/L réel",
    }
    df_display = df_display.rename(columns=rename)
    if "Cote réelle" in df_display.columns and "Proba modèle" in df_display.columns:
        _p = pd.to_numeric(df_display["Proba modèle"], errors="coerce")
        _odd = pd.to_numeric(df_display["Cote réelle"], errors="coerce")
        df_display["EV réelle"] = (_p * _odd - 1.0) * 100.0
    for stake_col in ("Mise reco (%BR)", "Mise ordre composite (%BR)"):
        if stake_col in df_display.columns:
            df_display[stake_col] = pd.to_numeric(
                df_display[stake_col], errors="coerce"
            ) * 100.0
    for col in ("Proba modèle", "EV"):
        if col in df_display.columns:
            df_display[col] = pd.to_numeric(df_display[col], errors="coerce") * 100.0
    st.dataframe(df_display.head(300), use_container_width=True, hide_index=True)
    if len(df_display) > 300:
        st.caption(f"Affichage limité aux 300 premières lignes sur {len(df_display)} opportunités.")


def _compute_clv_alert(df_bets: pd.DataFrame) -> tuple[float | None, str | None]:
    """Alert message based on last 20 CLV values (independent from match result)."""
    if df_bets is None or df_bets.empty or "clv_score" not in df_bets.columns:
        return None, None
    clv = pd.to_numeric(df_bets["clv_score"], errors="coerce").dropna()
    if clv.empty:
        return None, None
    last20 = clv.tail(20)
    if last20.empty:
        return None, None
    m = float(last20.mean())
    if m < 0.0:
        return m, "Alerte : Le modèle perd de la valeur face au marché. Vérifier la calibration."
    if m > 0.05:
        return m, "Edge Validé : Le modèle bat l'efficience du marché."
    return m, None


@st.cache_data(ttl=900)
def compute_model_diagnostics(year_start: int, year_end: int, max_matches: int, seed: int):
    """Diagnostics honnêtes (no-leak, match_date, cotes réelles si dispo)."""
    from scripts.honest_model_diagnostics import run_honest_model_diagnostics

    return run_honest_model_diagnostics(
        year_start,
        year_end,
        max_matches=int(max_matches),
        seed=int(seed),
        ev_threshold_frac=DEFAULT_LIVE_EV_THRESHOLD_FRAC,
    )


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
    seg_brier = _match_segment_brier(match)

    markets = []

    # 1) Total jeux OVER 22.5
    over_games_prob = _clip01(0.41 + 0.34 * closeness + 0.12 * fatigue_factor + 0.08 * inact_factor)
    over_games_book_prob = _clip01(over_games_prob - (0.045 + 0.02 * max(0.0, closeness - 0.4)))
    over_games_true_odd = 1.0 / over_games_prob
    over_games_book_odd = 1.0 / over_games_book_prob
    over_val = enrich_value_metrics(
        detector.detect_value(over_games_book_odd, over_games_true_odd, confidence=confidence),
        segment_brier=seg_brier,
    )
    if over_val.get("is_value"):
        markets.append({
            "market": "Total jeux",
            "selection": "Over 22.5 jeux",
            "book_odd": over_games_book_odd,
            "true_odd": over_games_true_odd,
            "value_pct": float(over_val.get("value_pct", 0.0)),
            "sharpe_ratio": float(over_val.get("sharpe_ratio", 0.0)),
            "sharpe_per_brier": float(over_val.get("sharpe_per_brier", 0.0)),
            "priority_score": float(over_val.get("priority_score", 0.0)),
            "segment_brier": float(seg_brier),
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
    tb_val = enrich_value_metrics(
        detector.detect_value(tiebreak_book_odd, tiebreak_true_odd, confidence=confidence),
        segment_brier=seg_brier,
    )
    if tb_val.get("is_value"):
        markets.append({
            "market": "Tie-break",
            "selection": "Tie-break dans le match: OUI",
            "book_odd": tiebreak_book_odd,
            "true_odd": tiebreak_true_odd,
            "value_pct": float(tb_val.get("value_pct", 0.0)),
            "sharpe_ratio": float(tb_val.get("sharpe_ratio", 0.0)),
            "sharpe_per_brier": float(tb_val.get("sharpe_per_brier", 0.0)),
            "priority_score": float(tb_val.get("priority_score", 0.0)),
            "segment_brier": float(seg_brier),
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
    sets_val = enrich_value_metrics(
        detector.detect_value(three_sets_book_odd, three_sets_true_odd, confidence=confidence),
        segment_brier=seg_brier,
    )
    if sets_val.get("is_value"):
        markets.append({
            "market": "Nombre de sets",
            "selection": "Over 2.5 sets",
            "book_odd": three_sets_book_odd,
            "true_odd": three_sets_true_odd,
            "value_pct": float(sets_val.get("value_pct", 0.0)),
            "sharpe_ratio": float(sets_val.get("sharpe_ratio", 0.0)),
            "sharpe_per_brier": float(sets_val.get("sharpe_per_brier", 0.0)),
            "priority_score": float(sets_val.get("priority_score", 0.0)),
            "segment_brier": float(seg_brier),
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
                # Attendre la fin d'un rebuild snapshot ; ne pas bloquer sur signature CSV
                # (un nouveau scrape prematch invalide le snapshot sans empêcher la sync DB).
                if snapshot_build_in_progress():
                    time.sleep(60)
                    continue
                run_kw: dict = {
                    "args": [sys.executable, sync_script],
                    "cwd": project_root,
                    "stdin": subprocess.DEVNULL,
                }
                if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                    run_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[assignment]
                subprocess.run(**run_kw)
                invalidate_live_snapshot_ram_cache()
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


if not HEADLESS_APP:
    try:
        if not os.path.isfile(_DB_INDEXES_FLAG):
            from scripts.db_indexes import ensure_bettinghud_query_indexes

            if ensure_bettinghud_query_indexes(ml_model.db_path):
                os.makedirs(os.path.dirname(_DB_INDEXES_FLAG), exist_ok=True)
                with open(_DB_INDEXES_FLAG, "w", encoding="utf-8") as _f:
                    _f.write(str(time.time()))
    except Exception:
        pass
    start_auto_tours_db_sync()
    start_weekly_ml_train()
    start_live_data_daemon()
    start_live_projection_warmup()


def _current_live_signature_tuple() -> tuple:
    csv_path, csv_mtime = _prematch_csv_signature()
    csv_mtime_s = _sanitize_mtime_float(csv_mtime)
    return (
        str(csv_path or ""),
        float(csv_mtime_s),
        int(PROFILE_CACHE_SCHEMA),
        float(_ml_model_mtime()),
        int(_ENGINES_CACHE_VERSION),
    )


def _current_live_signature_dict() -> dict:
    t = _current_live_signature_tuple()
    return _live_signature_dict_compat(t[0], t[1], t[2], t[3], t[4])


def _hydrate_live_matches_from_disk() -> list:
    """Applique le snapshot disque (TTL 24 h) au session_state si la signature courante correspond."""
    sig_t = _current_live_signature_tuple()
    sig_d = _current_live_signature_dict()
    snap = load_live_snapshot(sig_d, max_age_sec=LIVE_SNAPSHOT_TTL_SEC)
    # Nouveau scrape prematch (mtime CSV) : le snapshot disque reste valide tant que le modèle
    # n'a pas changé — même repli que _load_live_matches_for_algo_sync.
    if snap is None:
        snap = load_live_snapshot_by_model(
            _live_model_signature_dict(),
            max_age_sec=LIVE_SNAPSHOT_TTL_SEC,
        )
    if snap is not None:
        snap = _sanitize_live_matches_list(snap)
        st.session_state["_live_matches_sig"] = sig_t
        st.session_state["_live_matches_cache"] = list(snap)
        return list(snap)
    if st.session_state.get("_live_matches_sig") == sig_t:
        return list(st.session_state.get("_live_matches_cache") or [])
    return []


def _force_live_tracker_refresh_sync() -> list:
    """Rebuild complet (snapshot + session) — bouton manuel Live Tracker."""
    get_latest_scraped_data.clear()
    clear_live_build_progress()
    st.session_state.pop("_live_matches_cache", None)
    st.session_state.pop("_live_matches_sig", None)
    try:
        if os.path.isfile(SNAPSHOT_PATH):
            os.remove(SNAPSHOT_PATH)
        invalidate_live_snapshot_ram_cache()
    except OSError:
        pass
    csv_path, csv_mtime = _prematch_csv_signature()
    csv_mtime_s = _sanitize_mtime_float(csv_mtime)
    matches = get_latest_scraped_data(
        csv_path,
        csv_mtime_s,
        PROFILE_CACHE_SCHEMA,
        _ml_model_mtime(),
        _ENGINES_CACHE_VERSION,
    )
    st.session_state["_live_matches_sig"] = _current_live_signature_tuple()
    st.session_state["_live_matches_cache"] = matches
    return matches


def _render_live_background_build_status() -> None:
    """Barre d'avancement pendant le build snapshot (daemon ou premier chargement)."""
    if not snapshot_build_in_progress():
        return
    prog = read_live_build_progress() or {}
    pct = float(prog.get("pct") or 0.02)
    label = str(prog.get("label") or "Préparation…")
    step_i = int(prog.get("step_index") or -1)
    total = int(prog.get("total_steps") or 1)
    pct_i = int(round(max(0.0, min(1.0, pct)) * 100))
    step_disp = max(1, step_i + 1)
    started = float(prog.get("started_at") or time.time())
    elapsed = max(0, int(time.time() - started))
    st.info(
        "Construction des analyses en arrière-plan (premier chargement). "
        f"**{pct_i} %** — étape **{step_disp}/{total}** : {label} "
        f"_(écoulé {elapsed // 60} min {elapsed % 60:02d} s)_ · "
        "mise à jour automatique toutes les quelques secondes."
    )
    st.progress(pct, text=f"{label} ({pct_i} %)")


if not HEADLESS_APP:
    # --- UI Streamlit (onglets, sidebar) ---
    # Précharge session depuis le snapshot disque (accessible avant d'ouvrir Live Tracker).
    _hydrate_live_matches_from_disk()
    if (
        ENABLE_LIVE_PROJECTION_WARMUP
        and not snapshot_build_in_progress()
        and not st.session_state.get("_live_snapshot_async_kicked")
    ):
        if load_live_snapshot(_current_live_signature_dict(), max_age_sec=LIVE_SNAPSHOT_TTL_SEC) is None:
            st.session_state["_live_snapshot_async_kicked"] = True
            _kick_live_snapshot_build_async(label="live-projection-page-load")
    
    _snap_preload_meta = snapshot_meta()
    _preload_status_caption = None
    if snapshot_build_in_progress():
        _preload_status_caption = "⏳ Projection du jour en préparation en arrière-plan…"
    elif _snap_preload_meta and int(_snap_preload_meta.get("n_matches") or 0) > 0:
        _built_pre = _format_built_at_paris(_snap_preload_meta["built_at"])
        _preload_status_caption = (
            f"✓ **{_snap_preload_meta['n_matches']}** matchs préchargés · snapshot {_built_pre}"
        )
    
    st.markdown(
        """
        <style>
        /* Surbrillance visuelle de l'onglet "Paris du jour" (1er onglet). */
        div[data-baseweb="tab-list"] > button[role="tab"]:first-child {
            background: rgba(249, 115, 22, 0.12) !important;
            color: var(--text) !important;
            font-weight: 700 !important;
            border-radius: 10px !important;
            border: 1px solid rgba(249, 115, 22, 0.28) !important;
            box-shadow: 0 0 0 1px rgba(249, 115, 22, 0.10) inset;
        }
        div[data-baseweb="tab-list"] > button[role="tab"]:first-child[aria-selected="true"] {
            background: rgba(249, 115, 22, 0.18) !important;
            color: var(--text) !important;
            box-shadow: 0 0 0 2px rgba(249, 115, 22, 0.18) inset;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    from scripts.web_auth import render_account_banner

    render_account_banner()

    tab_top5_action, tab_portfolio, tab_live, tab_top_probs, tab_backtest, tab_diag, tab_tracking, tab_settings = st.tabs(
        [
            "🗓️ Paris du jour",
            "💼 Mon Portefeuille",
            "🎯 Live Tracker",
            "📈 Top probas jour",
            "📊 Backtest Kelly (CSV)",
            "🧪 Diagnostics Modèle",
            "📡 Tracking modèle (réel)",
            "⚙️ Paramètres",
        ]
    )

    _apply_pending_live_tracker_link_from_paris_du_jour()
    if st.session_state.pop("_live_tracker_focus_tab", False):
        _focus_streamlit_tab_index(_LIVE_TRACKER_TAB_INDEX)
    
    with tab_live:
        _touch_dashboard_ui_heartbeat()
        if FAST_LIVE_MODE:
            st.info("Mode rapide actif: profils live et signaux avancés sont allégés pour privilégier la réactivité.")
    
        _lt_hdr_btn, _lt_hdr_meta = st.columns([1.15, 3.85])
        with _lt_hdr_btn:
            if st.button(
                "🔄 Actualiser le Live Tracker",
                key="live_tracker_force_refresh",
                help="Reconstruit les analyses (aujourd'hui + demain) et recharge l'affichage.",
            ):
                if snapshot_build_in_progress():
                    st.warning("Une construction est déjà en cours en arrière-plan. Patientez quelques instants.")
                else:
                    with st.spinner("Reconstruction du Live Tracker…"):
                        _force_live_tracker_refresh_sync()
                st.rerun()
        with _lt_hdr_meta:
            _snap_meta_hdr = snapshot_meta()
            if _snap_meta_hdr:
                _built_hdr = _format_built_at_paris(_snap_meta_hdr["built_at"])
                _maj_hint = (
                    f"MAJ auto toutes les {LIVE_TRACKER_IDLE_REFRESH_SEC} s"
                    if LIVE_TRACKER_AUTOREFRESH_WHEN_LOADED
                    else "pas de MAJ auto (bouton Actualiser)"
                )
                st.caption(
                    f"Cache live **24 h** · **{_snap_meta_hdr['n_matches']}** matchs "
                    f"(snapshot {_built_hdr}) · {_maj_hint}."
                )
            else:
                st.caption(
                    f"Jour courant + demain · cache snapshot **24 h** · "
                    f"MAJ auto toutes les {LIVE_BUILD_POLL_SEC} s tant que les données se préparent."
                )
    
        # Hydrater avant le calcul du polling (évite refresh agressif alors que le snapshot existe).
        real_matches = list(st.session_state.get("_live_matches_cache") or [])
        if not real_matches:
            real_matches = _hydrate_live_matches_from_disk()
        _n_snapshot_raw = len(real_matches)
        _lt_has_data = bool(real_matches)
        _build_active = snapshot_build_in_progress()
        if _lt_has_data and _build_active:
            _poll_ms = LIVE_ENRICH_POLL_SEC * 1000
        elif _lt_has_data:
            _poll_ms = LIVE_TRACKER_IDLE_REFRESH_SEC * 1000
        elif _build_active:
            _poll_ms = LIVE_BUILD_POLL_SEC * 1000
        else:
            _poll_ms = LIVE_TRACKER_AUTO_REFRESH_SEC * 1000
        if (not _lt_has_data) or LIVE_TRACKER_AUTOREFRESH_WHEN_LOADED:
            st_autorefresh(interval=_poll_ms, key="live_tracker_autorefresh")
    
        _csv_path, _csv_mtime = _prematch_csv_signature()
        _csv_mtime_s = _sanitize_mtime_float(_csv_mtime)
        if _maybe_auto_refresh_prematch(_csv_path, _csv_mtime_s):
            st.toast("🔄 Mise à jour des cotes en cours…", icon="⏳")
        elif _csv_path and _try_refresh_snapshot_odds_only(_csv_path, _csv_mtime_s):
            _hydrate_live_matches_from_disk()
    
        if _build_active and not _lt_has_data:
            _render_live_background_build_status()
        if not real_matches:
            if not _build_active:
                st.info(
                    "La projection du jour se prépare en arrière-plan (démarrée à l'ouverture du dashboard). "
                    f"Cette page se met à jour toutes les {LIVE_BUILD_POLL_SEC} s."
                )
            if not st.session_state.get("_live_tab_build_kicked"):
                st.session_state["_live_tab_build_kicked"] = True
                _kick_live_snapshot_build_async(label="live-tab-fallback")
        elif not _build_active:
            clear_live_build_progress()

        if real_matches and _snapshot_has_preview_only(real_matches):
            st.warning(
                "Affichage **rapide** (cotes + ML). L'enrichissement complet continue "
                "en arrière-plan quand vous quittez cet onglet — utilisez "
                "**Actualiser le Live Tracker** pour charger la version enrichie."
            )
        
        # Filtrer les matchs sans cotes valides (si on n'a pas pu les simuler non plus)
        real_matches = [m for m in real_matches if m['odd_p1'] > 1.0 and m['odd_p2'] > 1.0]
        _n_with_odds = len(real_matches)
        _rank_exclude_counts = count_matches_excluded_by_reason(real_matches)
        real_matches = [m for m in real_matches if _match_has_rank_points_source(m)]
        _n_with_rank = len(real_matches)
        _n_rank_data_excluded = sum(
            _rank_exclude_counts.get(k, 0)
            for k in ("missing_rank_source", "tennisexplorer_estimate")
        )
        _n_stale_rank_excluded = int(_rank_exclude_counts.get("stale_rank_stats", 0))
    
        # Filtrer les matchs trop anciens. Même règle que le build snapshot :
        # on conserve les matchs démarrés récemment pour l'In-Play.
        _now_live_paris = datetime.now(ZoneInfo("Europe/Paris"))
        _started_grace = timedelta(minutes=int(LIVE_STARTED_GRACE_MINUTES))

        def is_future_or_recent_started_match(m: dict) -> bool:
            time_str = str(m.get("time") or "").strip()
            if not time_str:
                return True
            if time_str.startswith("Demain"):
                return True
            try:
                match_time = datetime.strptime(time_str, "%H:%M").time()
            except ValueError:
                return True  # Au cas où c'est un autre format (ex: Inconnu / --:--)
            try:
                d = pd.Timestamp(str(m.get("date") or "")[:10])
                if pd.isna(d):
                    return True
                scheduled = datetime.combine(
                    d.date(),
                    match_time,
                    tzinfo=ZoneInfo("Europe/Paris"),
                )
            except Exception:
                return True
            if scheduled >= _now_live_paris:
                return True
            if int(LIVE_STARTED_GRACE_MINUTES) <= 0:
                return False
            return (_now_live_paris - scheduled) <= _started_grace

        real_matches = [m for m in real_matches if is_future_or_recent_started_match(m)]
        _n_after_time = len(real_matches)
    
        st.markdown("---")
        
        _circuit_options = ["Tous", "ATP", "WTA"]
        if st.session_state.pop("_live_tracker_prefill_notice", False):
            st.info("Pré-filtre Live Tracker appliqué depuis « Paris du jour ».")

        if MOBILE_COMPACT:
            day_filter = st.radio("📅 Filtrer par jour :", ["Aujourd'hui", "Demain", "Tous"], horizontal=True)
            circuit_filter = st.selectbox(
                "🎾 Circuit",
                _circuit_options,
                key="live_circuit_filter",
                help="ATP ou WTA uniquement (simples individuels).",
            )
            include_challengers = st.checkbox(
                "Inclure les Challengers",
                key="live_include_challengers",
                help=(
                    "Affiche aussi les tournois ATP/WTA Challenger. "
                    "ITF, UTR et tournois « futures » restent masqués. "
                    "Un rebuild du snapshot peut être nécessaire après activation."
                ),
            )
        else:
            col_filter_day, col_filter_circuit, col_filter_chal, col_filter_tourney = st.columns(4)
            with col_filter_day:
                day_filter = st.radio("📅 Filtrer par jour :", ["Aujourd'hui", "Demain", "Tous"], horizontal=True)
            with col_filter_circuit:
                circuit_filter = st.selectbox(
                    "🎾 Circuit",
                    _circuit_options,
                    key="live_circuit_filter",
                    help="ATP ou WTA uniquement.",
                )
            with col_filter_chal:
                include_challengers = st.checkbox(
                    "Inclure les Challengers",
                    key="live_include_challengers",
                    help=(
                        "Affiche aussi les tournois ATP/WTA Challenger. "
                        "ITF, UTR et tournois « futures » restent masqués."
                    ),
                )
    
        _today_live_date = datetime.now().date()
        _tomorrow_live_date = _today_live_date + timedelta(days=1)

        def _match_live_date(m: dict):
            try:
                d = pd.Timestamp(str(m.get("date") or "")[:10])
                if pd.isna(d):
                    return None
                return d.date()
            except Exception:
                return None

        def _is_today_match(m: dict) -> bool:
            d = _match_live_date(m)
            if d is not None:
                return d == _today_live_date
            return not str(m.get("time") or "").startswith("Demain")

        def _is_tomorrow_match(m: dict) -> bool:
            d = _match_live_date(m)
            if d is not None:
                return d == _tomorrow_live_date
            return str(m.get("time") or "").startswith("Demain")

        if day_filter == "Aujourd'hui":
            st.caption(
                "Filtre basé sur la **date réelle du match**. Les libellés d'heure "
                "hérités du scrape précédent (ex. `Demain 10:30`) ne déplacent plus "
                "un match d'aujourd'hui dans l'onglet Demain."
            )
        
        # 1. Filtre par jour
        if day_filter == "Aujourd'hui":
            filtered_matches = [m for m in real_matches if _is_today_match(m)]
        elif day_filter == "Demain":
            filtered_matches = [m for m in real_matches if _is_tomorrow_match(m)]
        else:
            filtered_matches = real_matches
    
        # Garde-fou UI: gros tournois ATP/WTA par défaut ; Challengers si toggle actif.
        filtered_matches = [
            m
            for m in filtered_matches
            if _is_atp_wta_circuit_match(
                m.get("category"),
                m.get("tournament"),
                include_challengers=include_challengers,
            )
        ]
        _n_day_major = len(filtered_matches)
        if include_challengers:
            st.caption(
                "Challengers ATP/WTA inclus. ITF, UTR et tournois « futures » restent exclus."
            )
        if _n_rank_data_excluded:
            st.caption(
                f"{_n_rank_data_excluded} match(s) masqué(s) : pas de données rang/points "
                "en base pour au moins un joueur, ou **rang Tennis Explorer estimé uniquement**."
            )
        if _n_stale_rank_excluded:
            _stale_months = max(1, round(STALE_RANK_STATS_MAX_DAYS / 30))
            st.caption(
                f"{_n_stale_rank_excluded} match(s) masqué(s) : données rang/points TML/WTA "
                f"de plus de **{_stale_months} mois** (`stats_reference_date` > "
                f"{STALE_RANK_STATS_MAX_DAYS} jours) sur au moins un joueur."
            )
    
        _n_mixed_rank_src = sum(
            1 for m in filtered_matches if not _match_homogeneous_rank_source(m)
        )
        if _n_mixed_rank_src:
            st.caption(
                f"**{len(filtered_matches)}** match(s) affiché(s), dont {_n_mixed_rank_src} "
                "avec sources rang/points mixtes ou partielles (badge qualité sur la carte)."
            )
    
        if circuit_filter != "Tous":
            filtered_matches = [
                m for m in filtered_matches
                if _match_circuit(m) == circuit_filter
            ]
    
        # Extraire les tournois uniques basés sur le filtre de jour sélectionné
        tournaments = ["Tous"] + sorted(list(set([m['tournament'] for m in filtered_matches])))
        if MOBILE_COMPACT:
            tourney_filter = st.selectbox("🏆 Filtrer par tournoi :", tournaments)
        else:
            with col_filter_tourney:
                tourney_filter = st.selectbox("🏆 Filtrer par tournoi :", tournaments)
            
        # 2. Filtre par tournoi
        if tourney_filter != "Tous":
            filtered_matches = [m for m in filtered_matches if m['tournament'] == tourney_filter]
    
        if MOBILE_COMPACT:
            _player_q = st.text_input(
                "Rechercher un joueur",
                key="live_player_name_filter",
                placeholder="ex. Zverev",
                help="Filtre table + opportunités sur un joueur (partie du nom, sans tenir compte de la casse).",
            )
            data_alert_filter = st.selectbox(
                "Alertes données",
                _LIVE_DATA_ALERT_FILTER_OPTIONS,
                key="live_data_alert_filter",
                help=(
                    "Conflit Base/TE : écart entre l'historique ATP (TennisMyLife) ou WTA (Sackmann) "
                    "et le profil Tennis Explorer. Filtres ATP/TE et WTA/TE disponibles."
                ),
            )
        else:
            _pf1, _pf2 = st.columns([1, 1])
            with _pf1:
                _player_q = st.text_input(
                    "Rechercher un joueur",
                    key="live_player_name_filter",
                    placeholder="ex. Zverev",
                    help="Filtre table + opportunités sur un joueur (partie du nom, sans tenir compte de la casse).",
                )
            with _pf2:
                data_alert_filter = st.selectbox(
                    "Alertes données",
                    _LIVE_DATA_ALERT_FILTER_OPTIONS,
                    key="live_data_alert_filter",
                    help=(
                        "Conflit ATP/TE ou WTA/TE (rouge), Réf. date >1 mois ou inactivité >2 mois (ambre)."
                    ),
                )
        _pq_l = str(_player_q or "").strip().lower()
        if _pq_l:
            filtered_matches = [
                m
                for m in filtered_matches
                if _pq_l in str(m.get("player1", "")).lower()
                or _pq_l in str(m.get("player2", "")).lower()
            ]
    
        filtered_matches = _apply_data_alert_filter(filtered_matches, data_alert_filter)
        if data_alert_filter != "Tous les matchs":
            st.caption(
                f"Filtre alertes actif : **{data_alert_filter}** — "
                f"**{len(filtered_matches)}** match(s) affiché(s)."
            )
    
        if MOBILE_COMPACT:
            premium_segments_only = st.toggle(
                "Segments bien calibrés (Brier < 0,18)",
                value=False,
                key="live_premium_segments_filter",
                help=(
                    "N'affiche que les matchs dont le Brier segment du modèle V47 est "
                    f"strictement inférieur à {PREMIUM_SEGMENT_BRIER_MAX:.2f}."
                ),
            )
        else:
            _prem_col, _prem_sp = st.columns([1, 2])
            with _prem_col:
                premium_segments_only = st.toggle(
                    "Segments bien calibrés (Brier < 0,18)",
                    value=False,
                    key="live_premium_segments_filter",
                    help=(
                        "Filtre premium : segments les mieux calibrés historiquement "
                        f"(Brier test &lt; {PREMIUM_SEGMENT_BRIER_MAX:.2f})."
                    ),
                )
        if premium_segments_only:
            filtered_matches = [
                m for m in filtered_matches
                if is_premium_segment(_match_segment_brier(m))
            ]
            st.caption(
                f"Filtre **segments bien calibrés** actif — "
                f"**{len(filtered_matches)}** match(s) (Brier &lt; {PREMIUM_SEGMENT_BRIER_MAX:.2f})."
            )

        _render_favorite_ev_band_toggle(
            widget_key=FAVORITE_EV_BAND_TOGGLE_KEY_LIVE,
            label=(
                f"Top {TOP_PROBAS_DISPLAY_LIMIT} · EV favori +15 % à +100 % "
                f"(tri tuiles par proba favori ↓)"
            ),
            help_suffix=(
                f"Filtre les matchs EV favori, puis affiche jusqu'à {TOP_PROBAS_DISPLAY_LIMIT} "
                "tuiles value bets triées comme l'onglet Top probas (proba favori modèle décroissante)."
            ),
        )
        _ev_min_frac, _ev_max_frac = _favorite_ev_band_params()
        _n_before_ev: int | None = None
        if _ev_min_frac is not None or _ev_max_frac is not None:
            _n_before_ev = len(filtered_matches)
            filtered_matches = _filter_matches_favorite_ev_band(
                filtered_matches,
                ev_min_frac=_ev_min_frac,
                ev_max_frac=_ev_max_frac,
            )
            st.caption(
                f"Filtre **top {TOP_PROBAS_DISPLAY_LIMIT}** · EV favori +15 % à +100 % actif — "
                f"**{len(filtered_matches)}** / {_n_before_ev} match(s) · "
                f"tuiles triées par **proba favori** ↓."
            )

        if filtered_matches:
            st.caption(f"Live Tracker : **{len(filtered_matches)}** match(s) disponibles après filtres.")
        else:
            st.warning("Aucun match ne passe les filtres actuels.")
            st.caption(
                f"Entonnoir snapshot : **{_n_snapshot_raw}** disque → "
                f"**{_n_with_odds}** cotes → **{_n_with_rank}** rang/points → "
                f"**{_n_after_time}** horaire → **{_n_day_major}** "
                f"{day_filter.lower()} + ATP/WTA → **0** après filtres UI."
            )
            if _n_before_ev is not None and _n_before_ev > 0:
                st.caption(
                    f"Filtre EV actif : **0** / **{_n_before_ev}** match(s) dans la bande "
                    f"+{int((_ev_min_frac or 0) * 100)} % → +{int((_ev_max_frac or 1) * 100)} %."
                )
            st.caption(
                "Essayez **Tous** (jour / circuit / tournoi), désactivez **Segments bien calibrés** "
                "et **EV favori**, remettez **Alertes données** sur **Tous les matchs**."
            )
    
        if (
            float(st.session_state.get("live_ev_threshold") or 0) < DEFAULT_LIVE_EV_THRESHOLD_PCT
            and not st.session_state.get("_ev_threshold_migrated_15")
        ):
            st.session_state["live_ev_threshold"] = DEFAULT_LIVE_EV_THRESHOLD_PCT
            st.session_state["_ev_threshold_migrated_15"] = True

        if MOBILE_COMPACT:
            live_ev_threshold = st.slider(
                "Seuil EV minimum (Live, %)",
                min_value=1.0,
                max_value=25.0,
                value=DEFAULT_LIVE_EV_THRESHOLD_PCT,
                step=0.5,
                key="live_ev_threshold",
            )
            live_conf_filter = st.selectbox(
                "Filtre confiance IA",
                ["Toutes", "Moyenne+", "Forte+", "Très forte"],
                key="live_conf_filter",
            )
        else:
            live_col1, live_col2 = st.columns(2)
            with live_col1:
                live_ev_threshold = st.slider(
                    "Seuil EV minimum (Live, %)",
                    min_value=1.0,
                    max_value=25.0,
                    value=DEFAULT_LIVE_EV_THRESHOLD_PCT,
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
            _bets_sig_live = _user_bets_state_signature()
            _cfg_snap = _cached_live_tracker_bankroll_snapshot(_bets_sig_live)
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
                    _clear_portfolio_runtime_caches()
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
                    _clear_portfolio_runtime_caches()
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
                    _clear_portfolio_runtime_caches()
                    st.success("Correction remise à zéro.")
                    st.rerun()
    
        br_snap = _cached_live_tracker_bankroll_snapshot(_bets_sig_live)
        br_avail = max(0.0, float(br_snap["available_eur"]))
        br_committed = float(br_snap["committed_open_eur"])
        br_equity = float(br_snap["equity_eur"])
        if MOBILE_COMPACT:
            _mbr1, _mbr2 = st.columns(2)
            _mbr1.metric("BR réf. (€)", f"{br_snap['start_eur']:.2f}")
            _mbr2.metric("BR dispo (€)", f"{br_avail:.2f}")
            _mbr3, _mbr4 = st.columns(2)
            _mbr3.metric("Engagé (€)", f"{br_committed:.2f}")
            _mbr4.metric("Capital total (€)", f"{br_equity:.2f}")
        else:
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
        _capt += " **Mode BR : tous les paris (hypothèse Kelly 1/2 adaptatif globale).**"
        st.caption(_capt)
    
        # Streamlit mémorise les `number_input` par clé : si la BR change (nouveau pari, résultat…),
        # on change artificiellement la clé pour réappliquer la reco Kelly sur la mise pré-remplie.
        _live_br_stake_sig = (
            f"{round(br_avail, 2)}|{round(br_committed, 2)}|"
            f"{round(float(br_snap.get('manual_adjust_eur', 0.0)), 2)}"
        )
    
        detector = ValueDetector(min_value_threshold=float(live_ev_threshold) / 100.0)
        value_bets = _collect_value_bets_from_matches(
            filtered_matches,
            ev_threshold_pct=float(live_ev_threshold),
        )
        # Top KPIs
        if MOBILE_COMPACT:
            st.metric("🎾 Matchs scannés", len(filtered_matches))
            st.metric("🚨 Value Bets trouvés", len(value_bets))
            if filtered_matches:
                te_full = sum(
                    1 for m in filtered_matches if m.get("p1_profile_loaded") and m.get("p2_profile_loaded")
                )
                st.metric("📡 Profils TE complets", f"{te_full}/{len(filtered_matches)} matchs")
            else:
                st.metric("📡 Profils TE complets", "—")
        else:
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
            fresh = _safe_strftime_mtime(_csv_mtime_s, "%d/%m %H:%M")
            mt = _csv_mtime_s
            age_min = max(0.0, (time.time() - mt) / 60.0) if mt > 0 else float("inf")
            if not np.isfinite(age_min):
                age_str = "—"
            elif age_min < 60:
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
        _persisted_opps_n = _persist_live_value_opportunities(value_bets)

        _ev_band_sort = _favorite_ev_band_is_active()
        if _ev_band_sort:
            value_bets = [vb for vb in value_bets if _vb_is_model_favorite_side(vb)]
            value_bets.sort(key=_vb_fav_proba_sort_key, reverse=True)
            value_bets = value_bets[:TOP_PROBAS_DISPLAY_LIMIT]
            st.caption(
                f"Tri **top {TOP_PROBAS_DISPLAY_LIMIT}** : côté favori modèle, "
                f"proba décroissante (max **{TOP_PROBAS_DISPLAY_LIMIT}** tuiles)."
            )
        else:
            # Options de tri (défaut : score composite Sharpe/Brier × qualité segment)
            if MOBILE_COMPACT:
                sort_option = st.selectbox(
                    "Trier par :",
                    _LIVE_VB_SORT_OPTIONS,
                    index=0,
                    key="live_vb_sort",
                )
            else:
                col_sort1, col_sort2 = st.columns([1, 3])
                with col_sort1:
                    sort_option = st.selectbox(
                        "Trier par :",
                        _LIVE_VB_SORT_OPTIONS,
                        index=0,
                        key="live_vb_sort",
                    )
                with col_sort2:
                    st.caption(
                        "**Composite** : `(Sharpe unitaire / Brier segment) × (1 − Brier/0,25)` — "
                        "met en avant les values sur segments V47 les mieux calibrés."
                    )

            if sort_option == "Composite (priorité)":
                value_bets.sort(key=_vb_priority_sort_key, reverse=True)
            elif sort_option == "Sharpe seul":
                value_bets.sort(key=_vb_sharpe_per_brier_sort_key, reverse=True)
            elif sort_option == "EV brute":
                value_bets.sort(key=_vb_ev_sort_key, reverse=True)
    
        bets_index = _cached_existing_bets_index(_bets_sig_live)
    
        # Affichage des alertes de Value Bets (paginé — gros gain perf Streamlit)
        st.header(f"🔥 Top Opportunités ({day_filter})")
        if _persisted_opps_n:
            st.caption(f"Journal opportunités : {_persisted_opps_n} opportunité(s) suivie(s) pour l'historique.")
        _vb_filt_key = "|".join(
            [
                str(day_filter),
                str(circuit_filter),
                str(data_alert_filter),
                str(premium_segments_only),
                str(_favorite_ev_band_is_active()),
                str(live_ev_threshold),
                str(live_conf_filter),
            ]
        )
        if st.session_state.get("_live_vb_filt_key") != _vb_filt_key:
            st.session_state["_live_vb_filt_key"] = _vb_filt_key
            st.session_state["live_vb_page"] = 0

        if len(value_bets) == 0:
            st.info("Aucun Value Bet détecté pour le moment. Essayez de changer de filtre ou attendez de nouveaux matchs.")
        else:
            _vb_total = len(value_bets)
            _vb_pages = max(1, (_vb_total + LIVE_VB_DISPLAY_LIMIT - 1) // LIVE_VB_DISPLAY_LIMIT)
            _vb_page = int(st.session_state.get("live_vb_page") or 0)
            _vb_page = max(0, min(_vb_page, _vb_pages - 1))
            st.session_state["live_vb_page"] = _vb_page
            if _vb_pages > 1:
                _vb_nav1, _vb_nav2, _vb_nav3 = st.columns([1, 2, 1])
                with _vb_nav1:
                    if st.button("◀ Préc.", key="live_vb_prev", disabled=_vb_page <= 0):
                        st.session_state["live_vb_page"] = _vb_page - 1
                        st.rerun()
                with _vb_nav2:
                    st.caption(
                        f"Opportunités **{_vb_page * LIVE_VB_DISPLAY_LIMIT + 1}–"
                        f"{min((_vb_page + 1) * LIVE_VB_DISPLAY_LIMIT, _vb_total)}** "
                        f"sur **{_vb_total}** (max {LIVE_VB_DISPLAY_LIMIT} par page)"
                    )
                with _vb_nav3:
                    if st.button("Suiv. ▶", key="live_vb_next", disabled=_vb_page >= _vb_pages - 1):
                        st.session_state["live_vb_page"] = _vb_page + 1
                        st.rerun()
            _vb_slice = value_bets[
                _vb_page * LIVE_VB_DISPLAY_LIMIT : (_vb_page + 1) * LIVE_VB_DISPLAY_LIMIT
            ]
            for vb in _vb_slice:
                match = vb["match"]
                idx = vb["idx"]
                val = vb["val"]
                p_num = vb["player"]
                
                player_name = match['player1'] if p_num == 1 else match['player2']
                opp_name = match['player2'] if p_num == 1 else match['player1']
                _p_side = int(p_num)
                _o_side = 2 if _p_side == 1 else 1
                player_name_html = _player_display_html(player_name, match, _p_side)
                opp_name_html = _player_display_html(opp_name, match, _o_side)
                odd_book = match['odd_p1'] if p_num == 1 else match['odd_p2']
                odd_true = match['true_odd_p1'] if p_num == 1 else match['true_odd_p2']
                match_name = f"{match['player1']} vs {match['player2']}"
                stake_already = float(bets_index.get((match_name, player_name), 0.0))
                mid = match.get("prematch_id") or f"row_{idx}"
                widget_uid = f"{mid}_vb{idx}_p{p_num}"
    
                _seg_key_card = _match_brier_segment_key(match)
                _seg_brier_card = _match_segment_brier(match)
                _vb_premium = is_premium_segment(float(_seg_brier_card))
                with st.container(border=True):
                    if _vb_premium:
                        st.markdown(
                            '<span class="vb-card-premium-marker" aria-hidden="true"></span>',
                            unsafe_allow_html=True,
                        )
                    st.markdown(
                        f"### 🎾 {player_name_html} "
                        f"<span class='vb-opp-muted' style='font-size:0.95rem;color:#8A8D98;'>vs {opp_name_html}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f'<div class="vb-card-meta">'
                        f"{_circuit_badge_html(match.get('tour') or match.get('category'))}"
                        f"{_segment_chip_badge_html(_seg_key_card, premium=_vb_premium)}"
                        f"{_segment_brier_badge_html(_seg_brier_card, _seg_key_card)}"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"**{match['tournament']}** ({match['category']}) · **{match['time']}**"
                    )
                    _ps1, _ps2 = match.get("p1_stats") or {}, match.get("p2_stats") or {}
                    st.caption(_player_provenance_caption(match["player1"], _ps1, bool(match.get("p1_profile_loaded"))))
                    st.caption(_player_provenance_caption(match["player2"], _ps2, bool(match.get("p2_profile_loaded"))))
                    if not _match_homogeneous_rank_source(match):
                        st.warning(
                            "Sources rang/points mixtes ou partielles : prédiction conservée, "
                            "mais à lire avec prudence.",
                            icon="⚠️",
                        )
                    if _prediction_contradicts_rank_points(match):
                        _ps1c = match.get("p1_stats") or {}
                        _ps2c = match.get("p2_stats") or {}
                        st.error(
                            "Probabilité modèle **incohérente** avec le classement affiché "
                            f"(#{_ps1c.get('rank', '—')} vs #{_ps2c.get('rank', '—')}, "
                            f"cote book très déséquilibrée). "
                            "Ne pas suivre la value sur ce match — signal ML non fiable ici.",
                            icon="🚫",
                        )
                    _rf1, _rf2 = st.columns([3, 1])
                    with _rf2:
                        if st.button(
                            "🔄 Forcer full match",
                            key=f"refresh_players_{mid}_vb{idx}",
                            help=(
                                "Re-scrape les profils Tennis Explorer, recharge rang/points/stats, "
                                "recalcule la prédiction et persiste ce match en snapshot disque."
                            ),
                        ):
                            with st.spinner("Actualisation complète du match…"):
                                _ok_disk = _force_full_live_match_refresh(match)
                            st.toast(
                                "Match full actualisé et sauvegardé." if _ok_disk else "Match full actualisé en session (snapshot introuvable).",
                                icon="✅" if _ok_disk else "⚠️",
                            )
                            st.rerun()
                    for _cap_side, _cap_label in ((1, match["player1"]), (2, match["player2"])):
                        _conf_cap = _player_data_conflict_caption(_cap_label, match, _cap_side)
                        if _conf_cap:
                            st.markdown(
                                f'<p style="margin:0.35rem 0 0.5rem 0;font-size:0.88rem;color:#ff9aaa;">{_conf_cap}</p>',
                                unsafe_allow_html=True,
                            )
                    if stake_already > 0:
                        st.markdown(
                            f"<span class='bet-badge'>✅ Déjà parié : {stake_already:.2f} € sur {player_name}</span>",
                            unsafe_allow_html=True,
                        )
    
                    _odd_key = f"custom_odd_{widget_uid}"
                    _default_odd_live = float(odd_book) if odd_book >= 1.01 else 1.01
                    if MOBILE_COMPACT:
                        custom_odd = st.number_input(
                            "Cote réelle",
                            min_value=1.01,
                            max_value=100.0,
                            value=_default_odd_live,
                            step=0.05,
                            key=_odd_key,
                            help="Kelly, EV reco et pré-remplissage de la mise utilisent cette cote.",
                        )
                        _book_vs = (
                            f"Réf. book agrégé : **{odd_book}** · "
                            if abs(float(custom_odd) - float(odd_book)) > 1e-3
                            else ""
                        )
                        st.caption(
                            f"{_book_vs}"
                            "L’espérance (EV) ci-contre suppose que vous passez votre pari à la cote saisie."
                        )
                    else:
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
                    _ev_panel = _ev_comparison_panel_html(
                        odd_book=float(odd_book),
                        odd_true=float(odd_true),
                        custom_odd=float(custom_odd),
                        ev_book_pct=float(val.get("value_pct") or 0.0),
                        ev_custom_pct=ev_live_pct,
                    )
                    if _ev_panel:
                        st.markdown(_ev_panel, unsafe_allow_html=True)
    
                    if MOBILE_COMPACT:
                        c1 = st.container()
                        c2 = st.container()
                        c3 = st.container()
                    else:
                        c1, c2, c3 = st.columns([1.5, 2, 1.5])
                    with c1:
                        st.caption(
                            f"Sharpe **{float(val.get('sharpe_ratio', 0)):.3f}** · "
                            f"Sharpe/Brier **{float(val.get('sharpe_per_brier', 0)):.3f}** · "
                            f"Priorité **{float(val.get('priority_score', 0)):.4f}**"
                        )
                        # Kelly partiel (défaut 1/2) sur la **cote saisie**, plafond BR dispo → KELLY_RECO_BANKROLL_CAP_FRAC.
                        p_model_side = min(1.0, max(0.0, 1.0 / float(odd_true))) if odd_true and odd_true > 0 else 0.0
                        b_side = max(0.01, float(custom_odd) - 1.0)
                        kelly_full = max(0.0, (b_side * p_model_side - (1.0 - p_model_side)) / b_side)
                        kelly_partial = float(KELLY_RECO_ADAPTIVE_BASE_FRAC) * kelly_full
                        _seg_k = _match_brier_segment_key(match)
                        _brier_s = _match_segment_brier(match)
                        _kelly_adj = max(0.0, 1.0 - (_brier_s / 0.25))
                        reco_frac = max(0.0, min(kelly_partial * _kelly_adj, KELLY_RECO_BANKROLL_CAP_FRAC))
                        reco_pct = reco_frac * 100.0
                        reco_eur = br_avail * reco_frac
                        _cap_pct = int(round(KELLY_RECO_BANKROLL_CAP_FRAC * 100.0))
                        _k_label = "1/2" if float(KELLY_RECO_ADAPTIVE_BASE_FRAC) >= 0.5 else "1/4"
                        st.markdown(
                            f"**Mise reco (Kelly {_k_label} × Brier-adaptatif, cap {_cap_pct} % BR)** : "
                            f"<span class='quant-num'>{reco_eur:.2f} €</span>",
                            unsafe_allow_html=True,
                        )
                        st.caption(
                            f"{reco_pct:.2f}% de la BR restante (**{br_avail:.2f} €**). "
                            f"Brier segment **{_seg_k}**≈{_brier_s:.3f}, facteur {_kelly_adj:.2f}."
                        )
                        
                        with st.popover("ℹ️ Pourquoi cette value ?"):
                            p1_stats = match.get("p1_stats", {})
                            p2_stats = match.get("p2_stats", {})
                            _fs = match.get("feature_snapshot") or {}
                            _card_is_p1 = int(p_num) == 1
                            _style_left = _fs.get("human_p1_style", "—") if _card_is_p1 else _fs.get("human_p2_style", "—")
                            _style_right = _fs.get("human_p2_style", "—") if _card_is_p1 else _fs.get("human_p1_style", "—")
                            _jet_left = bool(_fs.get("p1_jetlag_alert")) if _card_is_p1 else bool(_fs.get("p2_jetlag_alert"))
                            _jet_right = bool(_fs.get("p2_jetlag_alert")) if _card_is_p1 else bool(_fs.get("p1_jetlag_alert"))
                            _cl_left = float(_fs.get("p1_clutch52", 0.5) or 0.5) if _card_is_p1 else float(_fs.get("p2_clutch52", 0.5) or 0.5)
                            _cl_right = float(_fs.get("p2_clutch52", 0.5) or 0.5) if _card_is_p1 else float(_fs.get("p1_clutch52", 0.5) or 0.5)
                            _smb_raw = _fs.get("style_matchup_bias")
                            _smb_view = (float(_smb_raw) if _smb_raw is not None else None)
                            if _smb_view is not None and not _card_is_p1:
                                _smb_view = -_smb_view
                            _hf_rows = []
                            _hf_rows.append(
                                [
                                    "Style (joueur vs adversaire)",
                                    f"{_style_left} vs {_style_right}",
                                ]
                            )
                            _jet = []
                            if _jet_left:
                                _jet.append("Joueur")
                            if _jet_right:
                                _jet.append("Adversaire")
                            _hf_rows.append(["Jetlag alert", ", ".join(_jet) if _jet else "Aucune alerte"])
                            try:
                                _hf_rows.append(
                                    [
                                        "Clutch 52s (joueur/adversaire)",
                                        f"{_cl_left*100:.0f}% / {_cl_right*100:.0f}%",
                                    ]
                                )
                            except Exception:
                                _hf_rows.append(["Clutch 52s (joueur/adversaire)", "—"])
                            _hf_rows.append(
                                ["Style matchup bias (joueur)", f"{float(_smb_view):+.3f}" if _smb_view is not None else "—"]
                            )
    
                            st.markdown("#### Human Factors (résumé)")
                            st.dataframe(
                                pd.DataFrame(_hf_rows, columns=["Signal", "Valeur"]),
                                hide_index=True,
                                use_container_width=True,
                                height=165,
                            )
    
                            comp_rows = _build_comparison_rows(
                                match,
                                _player_display_plain(player_name, match, _p_side),
                                _player_display_plain(opp_name, match, _o_side),
                                p_num,
                                _infobulle_hand_label,
                            )
                            st.markdown(_infobulle_quick_summary(
                                player_name, float(odd_book), float(odd_true), val, match
                            ))
                            with st.expander(
                                "📊 Comparatif modèle · forme · H2H · signaux & micro-stats",
                                expanded=False,
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
                            f"stake_eur_{widget_uid}_{_live_br_stake_sig}|odd{round(custom_odd, 4)}"
                        )
                        stake_eur = st.number_input(
                            "Mise (€)",
                            min_value=0.01,
                            max_value=_max_stake,
                            value=min(float(default_stake), _max_stake),
                            step=0.5,
                            key=_stake_key,
                            help=(
                                "Pré-rempli avec la reco Kelly (1/2 adaptatif Brier, cap "
                                f"{int(round(KELLY_RECO_BANKROLL_CAP_FRAC * 100.0))} %) sur la BR **disponible**, "
                                "**selon votre cote réelle**. Remis à jour quand la dispo ou cette cote changent."
                            ),
                        )
                        st.caption(
                            "S’aligne sur la reco quand la BR disponible ou la « Cote réelle » changent."
                        )
    
                    with c3:
                        st.markdown("<br><br>", unsafe_allow_html=True)
                        _avail_ok = br_avail >= float(stake_eur) - 1e-6
                        if st.button(
                            f"Parier {float(stake_eur):.2f} € sur {player_name}",
                            key=f"bet_{widget_uid}",
                            type="primary",
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
                                    segment_key=_match_brier_segment_key(match) or None,
                                    match_date=match.get("date"),
                                    p_model=(1.0 / float(odd_true)) if odd_true and odd_true > 0 else None,
                                    ev_at_bet=(ev_live_pct / 100.0),
                                    bookmaker_source=(
                                        "custom" if abs(float(custom_odd) - float(odd_book)) > 1e-3 else "book_avg"
                                    ),
                                    notes="paliers_live_kelly_1_4",
                                    tracker_source="live_tracker",
                                )
                                st.rerun()
    
        # Affichage de tous les matchs trackés (tableau chargé à la demande)
        with st.expander("📋 Voir tous les matchs trackés (Détails)"):
            st.caption(
                "Actualisation ciblée : re-scrape TE + stats base + prédiction pour un seul match "
                "(sans recharger tout le Live)."
            )
            _load_all_tbl = st.checkbox(
                "Charger le tableau complet",
                value=False,
                key="live_load_all_matches_table",
                help="Évite de recalculer toutes les lignes à chaque rafraîchissement.",
            )
            if _load_all_tbl:
                for _ri, _rm in enumerate(filtered_matches):
                    _rc1, _rc2 = st.columns([5, 1])
                    with _rc1:
                        st.markdown(
                            f"**{_rm['player1']}** vs **{_rm['player2']}** — {_rm.get('time', '')} "
                            f"({_rm.get('tournament', '')})"
                        )
                    with _rc2:
                        if st.button("🔄 Full", key=f"refresh_players_exp_{_ri}", help="Forcer full match + sauvegarde snapshot"):
                            with st.spinner("Actualisation…"):
                                _ok_disk = _force_full_live_match_refresh(_rm)
                            st.toast(
                                "Match full sauvegardé." if _ok_disk else "Match actualisé en session.",
                                icon="✅" if _ok_disk else "⚠️",
                            )
                            st.rerun()
            df_display = []
            if not _load_all_tbl:
                st.info(f"Cochez « Charger le tableau complet » pour afficher les **{len(filtered_matches)}** matchs.")
            for m in (filtered_matches if _load_all_tbl else []):
                p1_val = detector.detect_value(m["odd_p1"], m["true_odd_p1"], confidence=m.get("confidence"))
                p2_val = detector.detect_value(m["odd_p2"], m["true_odd_p2"], confidence=m.get("confidence"))
                
                df_display.append({
                    "Catégorie": m.get("category", "ATP/WTA"),
                    "Tournoi": m["tournament"],
                    "Statut": m["time"],
                    "Joueur 1": _player_display_plain(m["player1"], m, 1),
                    "Rang P1": m["p1_stats"].get("rank"),
                    "Fatigue P1": m["p1_fatigue"].get("minutes_played", 0),
                    "Cote P1": m["odd_p1"],
                    "True P1": round(m["true_odd_p1"], 2),
                    "EV P1 (%)": f"{p1_val['value_pct']:.1f}%",
                    "Joueur 2": _player_display_plain(m["player2"], m, 2),
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
    
    with tab_top_probs:
        _touch_dashboard_ui_heartbeat()
        _render_top_model_probs_tab()
    
    with tab_top5_action:
        _touch_dashboard_ui_heartbeat()
        _render_top5_proba_action_tab()

    if False:
        _render_live_inplay_tab()
    
    with tab_backtest:
        st.header("📈 Backtest (no-leak) — projection Kelly (ATP / WTA)")
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
        k_scope = st.checkbox(
            "Limiter aux tournois pariables (ATP/WTA + niveaux sélectionnés)",
            value=True,
            key="kcsv_scope_bettable",
        )
        if k_scope:
            k_scope_cols = st.columns([2, 3])
            with k_scope_cols[0]:
                k_levels = st.multiselect(
                    "Niveaux tournoi inclus",
                    options=["G", "M", "A", "F", "O", "D", "T"],
                    default=["G", "M", "A"],
                    key="kcsv_levels",
                    help="G=Grand Chelem, M=Masters/WTA1000, A=ATP/WTA 250/500 (mapping tennis-data/TML).",
                )
            with k_scope_cols[1]:
                k_extra_events = st.text_input(
                    "Compétitions extra (mots-clés, séparés par virgules)",
                    value="olympics,davis cup,billie jean king cup,united cup,atp finals,wta finals,laver cup",
                    key="kcsv_extra_events",
                    help="Appliqué seulement si la colonne `tournament` existe dans le CSV.",
                )
        k_bt_circuit = st.radio(
            "Circuit (colonne `tour` du CSV)",
            options=["ATP + WTA", "ATP uniquement", "WTA uniquement"],
            index=0,
            horizontal=True,
            key="kcsv_bt_circuit",
            help=(
                "ATP + WTA : même logique qu’avant avec « tournois pariables » (deux circuits). "
                "ATP ou WTA seul : restreint les lignes du CSV — utile même sans filtre niveaux si la colonne `tour` est présente."
            ),
        )
        _bt_year_opts = list_backtest_years_with_valid_csv(_PROJECT_ROOT)
        if "kcsv_years" in st.session_state and _bt_year_opts:
            _clean_years = [int(y) for y in st.session_state["kcsv_years"] if int(y) in _bt_year_opts]
            if _clean_years != list(st.session_state["kcsv_years"]):
                st.session_state["kcsv_years"] = _clean_years
        k_row1 = st.columns([1, 1, 1])
        with k_row1[0]:
            if not _bt_year_opts:
                st.warning(
                    "Aucun CSV backtest valide dans `data/` (fichier `backtest_<année>_bets*.csv` "
                    "avec colonnes p_model, odd, won, date et au moins une ligne). "
                    "Génère-en avec `python scripts/backtest_2026.py --year <année> --ev-min 0 "
                    "--out data/backtest_<année>_bets.csv`."
                )
                k_years = []
            else:
                _default_bt_years = (
                    [2025] if 2025 in _bt_year_opts else [_bt_year_opts[-1]]
                )
                k_years = st.multiselect(
                    "Année(s) cible(s)",
                    options=_bt_year_opts,
                    default=_default_bt_years,
                    key="kcsv_years",
                    help="Uniquement les années pour lesquelles un CSV backtest valide est présent dans `data/`.",
                )
            if not k_years and _bt_year_opts:
                st.caption("Sélectionne au moins une année dans la liste.")
            _years_selected = sorted({int(y) for y in (k_years or (_bt_year_opts[-1:] if _bt_year_opts else []))})
        with k_row1[1]:
            k_ev_min = st.number_input(
                "EV min (%)",
                min_value=0.0,
                max_value=100.0,
                value=DEFAULT_LIVE_EV_THRESHOLD_PCT,
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
                options=[
                    "Kelly 1/2 adaptatif (Brier)",
                    "Kelly 1/4 adaptatif (Brier)",
                    "Kelly ¼",
                    "Kelly ½",
                    "Kelly plein",
                    "% fixe de la BR",
                ],
                index=0,
                key="kcsv_kmult",
                help="Mode adaptatif : fraction Kelly (1/2 ou 1/4) × max(0, 1 - Brier_segment/0.25), comme le live (défaut 1/2).",
            )
            k_frac_map = {"Kelly ¼": 0.25, "Kelly ½": 0.5, "Kelly plein": 1.0}
            k_use_pct_br = k_frac_label == "% fixe de la BR"
            k_use_adapt = "adaptatif" in k_frac_label.lower()
            if k_frac_label.startswith("Kelly 1/2 adaptatif"):
                k_adapt_base = 0.5
            elif k_frac_label.startswith("Kelly 1/4 adaptatif"):
                k_adapt_base = 0.25
            else:
                k_adapt_base = 0.25  # non utilisé si mode non adaptatif
            k_mult = k_adapt_base if k_use_adapt else float(k_frac_map.get(k_frac_label, 0.25))
        with k_row2[1]:
            k_cap = st.slider(
                "Mise max (% BR du matin / pari)",
                min_value=0.5,
                max_value=50.0,
                value=10.0,
                step=0.5,
                key="kcsv_cap_pct",
            )
        with k_row2[2]:
            if k_use_pct_br:
                k_fixed_stake_pct = st.number_input(
                    "Mise fixe (% BR du matin / pari)",
                    min_value=0.1,
                    max_value=100.0,
                    value=2.0,
                    step=0.1,
                    key="kcsv_fixed_stake_pct",
                    help="Utilisé uniquement si l'option `% fixe de la BR` est sélectionnée.",
                )
            else:
                st.markdown("")
                k_fixed_stake_pct = 0.0
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
                help="Relatif à la racine du projet ; sinon recherche `backtest_<année>_bets*.csv` pour chaque année sélectionnée.",
            )
    
        def _csv_path_pick_bt() -> str | None:
            if k_csv_ov.strip():
                p = os.path.join(_PROJECT_ROOT, k_csv_ov.strip().replace("/", os.sep))
                return p if os.path.isfile(p) else None
            if not _years_selected:
                return None
            return resolve_backtest_csv(_PROJECT_ROOT, int(_years_selected[0]))
    
        def _csv_paths_pick_bt(years: list[int]) -> tuple[list[tuple[int, str]], list[int]]:
            if k_csv_ov.strip():
                p = os.path.join(_PROJECT_ROOT, k_csv_ov.strip().replace("/", os.sep))
                if not os.path.isfile(p):
                    return [], years
                return [(int(y), p) for y in years], []
            pairs: list[tuple[int, str]] = []
            missing: list[int] = []
            for y in years:
                p = resolve_backtest_csv(_PROJECT_ROOT, int(y))
                if p and os.path.isfile(p):
                    pairs.append((int(y), p))
                else:
                    missing.append(int(y))
            return pairs, missing
    
        k_tournaments = []
        k_use_tourney_filter = False
        _csv_preview_pairs, _csv_preview_missing = _csv_paths_pick_bt(_years_selected)
        for _, _csv_preview_path in _csv_preview_pairs:
            try:
                _df_header = pd.read_csv(_csv_preview_path, nrows=0)
                _tour_col = next(
                    (c for c in ("tournament", "tourney_name", "tournament_name") if c in _df_header.columns),
                    None,
                )
                if _tour_col is None:
                    continue
                _df_tournaments = pd.read_csv(_csv_preview_path, usecols=[_tour_col])
                _ser_t = _df_tournaments[_tour_col].dropna().astype(str).str.strip()
                k_tournaments.extend([x for x in _ser_t.unique().tolist() if x])
            except Exception:
                continue
        if k_tournaments:
            k_tournaments = sorted(set(k_tournaments))
        if k_tournaments:
            k_use_tourney_filter = st.checkbox(
                "Filtrer par tournoi(s) précis",
                value=False,
                key="kcsv_use_tournament_filter",
            )
        if k_use_tourney_filter:
            k_tourney_search = st.text_input(
                "Recherche tournoi",
                value="",
                key="kcsv_tournament_search",
                help="Ex: Roland Garros (tolère 'Rolland Garros').",
            )
            _search_norm = str(k_tourney_search or "").strip().lower()
            _search_alias = {
                "rolland garros": "roland garros",
                "roland garros": "roland garros",
                "rg": "roland garros",
            }
            _search_norm = _search_alias.get(_search_norm, _search_norm)
            if _search_norm:
                _opts = [t for t in k_tournaments if _search_norm in str(t).lower()]
            else:
                _opts = k_tournaments
            k_tournaments_selected = st.multiselect(
                "Tournoi(x) inclus",
                options=_opts,
                default=[],
                key="kcsv_tournament_filter",
                help="Si vide, aucun filtre tournoi n'est appliqué.",
            )
            if _search_norm in {"roland garros", "rg"} and not k_tournaments_selected:
                _rg_auto = [t for t in _opts if "roland garros" in str(t).lower()]
                if _rg_auto:
                    st.caption(f"Suggestion: {', '.join(_rg_auto[:3])}")
        else:
            k_tournaments_selected = []
        k_gs_choice = st.selectbox(
            "Grand Chelem ciblé",
            options=["Tous", "Australian Open", "Roland Garros", "Wimbledon", "US Open"],
            index=0,
            key="kcsv_gs_filter",
            help="Permet de filtrer rapidement un Grand Chelem en particulier.",
        )
    
        st.markdown("### Lancer la projection")
        if st.button("Calculer projection Kelly intra-jour", key="kcsv_run"):
            if not k_no_leak_ok:
                st.error(
                    "Coche la confirmation **no-leak** pour lancer, ou régénère un CSV avec "
                    "`python scripts/backtest_2026.py --year <année> --out data/backtest_<année>_bets.csv`."
                )
            else:
                _run_bt = False
                if not _years_selected:
                    st.error(
                        "Sélectionne au moins une année parmi les CSV backtest valides listés ci-dessus."
                    )
                else:
                    _path_pairs, _missing_years = _csv_paths_pick_bt(_years_selected)
                    if not _path_pairs:
                        st.error(
                            "Fichier introuvable. Exemple : "
                            "`python scripts/backtest_2026.py --year <année> --ev-min … --out data/backtest_<année>_bets.csv`."
                        )
                        if _missing_years:
                            st.caption(f"Années sans CSV détecté: {', '.join(map(str, _missing_years))}")
                    else:
                        _run_bt = True
                if _run_bt:
                    try:
                        kw_f = dict()
                        if k_fev:
                            kw_f["ev_min_pct"] = float(k_ev_min)
                        _circ_bt = str(st.session_state.get("kcsv_bt_circuit", "ATP + WTA"))
                        if _circ_bt == "ATP uniquement":
                            kw_f["allowed_tours"] = ["ATP"]
                        elif _circ_bt == "WTA uniquement":
                            kw_f["allowed_tours"] = ["WTA"]
                        elif k_scope:
                            kw_f["allowed_tours"] = ["ATP", "WTA"]
                        if k_scope:
                            kw_f["allowed_tourney_levels"] = list(k_levels)
                            kw_f["extra_tournament_tokens"] = [
                                t.strip() for t in str(k_extra_events).split(",") if t.strip()
                            ]
                        _frames_bt = []
                        for _yy, _pp in _path_pairs:
                            _frames_bt.append(load_and_filter_bets_csv(_pp, year=int(_yy), **kw_f))
                        df_bt = pd.concat(_frames_bt, ignore_index=True) if _frames_bt else pd.DataFrame()
                        if not df_bt.empty:
                            df_bt["date"] = pd.to_datetime(df_bt["date"], errors="coerce")
                            df_bt = df_bt.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
                        _tour_col = next(
                            (c for c in ("tournament", "tourney_name", "tournament_name") if c in df_bt.columns),
                            None,
                        )
                        if k_use_tourney_filter and k_tournaments_selected and _tour_col is not None:
                            _sel = {str(x).strip() for x in k_tournaments_selected if str(x).strip()}
                            if _sel:
                                _tour_series = df_bt[_tour_col].astype(str).str.strip()
                                df_bt = df_bt[_tour_series.isin(_sel)].reset_index(drop=True)
                        elif k_use_tourney_filter and k_tournaments_selected and _tour_col is None:
                            st.error(
                                "Le CSV backtest ne contient pas de colonne tournoi (`tournament`/`tourney_name`). "
                                "Régénère le CSV pour activer le filtre tournoi."
                            )
                            df_bt = pd.DataFrame()
                        if k_gs_choice != "Tous" and _tour_col is not None:
                            _tser = (
                                df_bt[_tour_col]
                                .astype(str)
                                .str.lower()
                                .str.replace("-", " ", regex=False)
                                .str.replace(".", " ", regex=False)
                                .str.replace(r"\s+", " ", regex=True)
                                .str.strip()
                            )
                            _gs_alias = {
                                "Australian Open": ["australian open", "ao"],
                                "Roland Garros": ["roland garros", "rolland garros", "french open", "rolandgarros"],
                                "Wimbledon": ["wimbledon"],
                                "US Open": ["us open", "u.s. open"],
                            }
                            _tokens = _gs_alias.get(k_gs_choice, [])
                            _keep = pd.Series(False, index=df_bt.index)
                            for _tok in _tokens:
                                _keep = _keep | _tser.str.contains(_tok, na=False, regex=False)
                            df_bt = df_bt[_keep].reset_index(drop=True)
                        elif k_gs_choice != "Tous" and _tour_col is None:
                            st.error(
                                "Filtre Grand Chelem indisponible: colonne tournoi absente du CSV "
                                "(`tournament`/`tourney_name`). Régénère le CSV backtest."
                            )
                            df_bt = pd.DataFrame()
                        if df_bt.empty:
                            st.warning("Aucun pari après filtres année / EV.")
                        else:
                            _ = resolve_segment_brier_score(ml_model, "")
                            _seg_brier = getattr(ml_model, "segment_brier_scores", {}) or {}
                            _glob_brier = float(getattr(ml_model, "global_test_brier", 0.12))
                            res = simulate_sequential_intraday(
                                df_bt,
                                bankroll_start=float(k_br0),
                                kelly_multiplier=k_mult,
                                max_stake_pct=float(k_cap),
                                daily_stake_budget_pct=float(k_day_budget),
                                use_fixed_stake_pct=bool(k_use_pct_br),
                                fixed_stake_pct=float(k_fixed_stake_pct),
                                use_adaptive_kelly_quarter=bool(k_use_adapt),
                                adaptive_kelly_base_fraction=float(k_adapt_base),
                                segment_brier_scores=_seg_brier,
                                global_brier_score=_glob_brier,
                                return_history=True,
                            )
                            st.success(
                                f"**{len(df_bt)}** paris · années {min(_years_selected)}-{max(_years_selected)} "
                                f"({len(_years_selected)} an(s))."
                            )
                            _used_csvs = sorted(
                                {
                                    os.path.relpath(_pp, _PROJECT_ROOT)
                                    for _, _pp in _path_pairs
                                }
                            )
                            st.caption(f"CSV utilisés: {', '.join(_used_csvs)}")
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
    
    if False:  # Onglet alternatif retiré à la demande utilisateur.
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
            max_value=25.0,
            value=DEFAULT_LIVE_EV_THRESHOLD_PCT,
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
    
        alt_rows.sort(
            key=lambda x: float(x["market"].get("priority_score") or x["market"].get("sharpe_ratio") or 0.0),
            reverse=True,
        )
    
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
                        if st.button(
                            f"Parier {stake_alt} U (annexe)",
                            key=f"alt_btn_{i}",
                            type="primary",
                        ):
                            save_bet(
                                match_name,
                                bet_label,
                                float(mk["book_odd"]),
                                float(stake_alt),
                                tour=str(m.get("tour") or m.get("category") or "").upper() or None,
                                surface=m.get("surface"),
                                tournament=m.get("tournament"),
                                match_id=str(m.get("prematch_id")) if m.get("prematch_id") else None,
                                segment_key=_match_brier_segment_key(m) or None,
                                match_date=m.get("date"),
                                p_model=(1.0 / float(mk["true_odd"])) if mk.get("true_odd") else None,
                                ev_at_bet=float(mk.get("value_pct", 0.0)) / 100.0,
                                bookmaker_source="book_avg",
                                notes=f"market={mk.get('selection')}",
                            )
    
    with tab_portfolio:
        st.header("💼 Mon Portefeuille de Paris")
        st.markdown("Suivez vos performances, filtrez vos paris et visualisez rapidement votre risque/rendement.")
    
        # CLV sync (best effort, throttled): refresh closing odds from latest prematch snapshot.
        _last_clv_sync = float(st.session_state.get("portfolio_last_clv_sync_ts", 0.0) or 0.0)
        if time.time() - _last_clv_sync >= 600.0:
            try:
                from scripts.sync_tml_recent import update_closing_odds
    
                _n_clv = int(update_closing_odds(db_path="data/bettinghud.db"))
                st.session_state["portfolio_last_clv_sync_ts"] = time.time()
                if _n_clv > 0:
                    st.caption(f"CLV sync: {_n_clv} pari(s) mis à jour.")
            except Exception:
                pass
        if MOBILE_COMPACT:
            if st.button("🔁 Forcer MAJ CLV", key="portfolio_force_clv_update"):
                try:
                    from scripts.sync_tml_recent import update_closing_odds
    
                    _n_clv_manual = int(update_closing_odds(db_path="data/bettinghud.db"))
                    st.success(f"CLV mise à jour : {_n_clv_manual} pari(s).")
                    st.session_state["portfolio_last_clv_sync_ts"] = time.time()
                    st.rerun()
                except Exception as _e:
                    st.error(f"Échec MAJ CLV: {_e}")
            st.caption("Met à jour `closing_odd`/`clv_score` depuis le dernier snapshot prematch.")
        else:
            _clv_btn_col1, _clv_btn_col2 = st.columns([1, 4])
            with _clv_btn_col1:
                if st.button("🔁 Forcer MAJ CLV", key="portfolio_force_clv_update"):
                    try:
                        from scripts.sync_tml_recent import update_closing_odds
    
                        _n_clv_manual = int(update_closing_odds(db_path="data/bettinghud.db"))
                        st.success(f"CLV mise à jour : {_n_clv_manual} pari(s).")
                        st.session_state["portfolio_last_clv_sync_ts"] = time.time()
                        st.rerun()
                    except Exception as _e:
                        st.error(f"Échec MAJ CLV: {_e}")
            with _clv_btn_col2:
                st.caption("Met à jour `closing_odd`/`clv_score` depuis le dernier snapshot prematch.")
    
        conn = sqlite3.connect('data/bettinghud.db')
        df_bets = pd.read_sql("SELECT * FROM user_bets ORDER BY id ASC", conn)
        conn.close()
        # Masqué à la demande utilisateur : section "📆 Report journalier algo".
        # _render_daily_algo_opportunity_report()
        # st.markdown("---")
    
        # Auto-update des résultats (opt-in via env) avec anti-spam.
        # Exécution en arrière-plan pour ne pas bloquer le rendu.
        auto_now = time.time()
        last_auto = st.session_state.get("portfolio_last_auto_results_ts", 0.0)
        auto_interval_sec = PORTFOLIO_AUTO_RESULTS_INTERVAL_SEC
        has_pending = not df_bets.empty and (df_bets["status"] == "En cours").any()
        if ENABLE_PORTFOLIO_AUTO_RESULTS and has_pending:
            # Désactivé pour garantir une expérience stable dans l'onglet Backtest
            # (les reruns globaux de streamlit_autorefresh impactent tous les onglets).
            pass
        _daemon_handles_results = False
        try:
            from scripts.portfolio_sync_lock import daemon_recently_active

            _daemon_handles_results = daemon_recently_active(
                max_age_sec=float(PORTFOLIO_AUTO_RESULTS_INTERVAL_SEC) + 120.0
            )
        except Exception:
            pass
        if (
            ENABLE_PORTFOLIO_AUTO_RESULTS
            and has_pending
            and (auto_now - float(last_auto) >= auto_interval_sec)
            and not _daemon_handles_results
        ):
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
        elif has_pending and _daemon_handles_results:
            st.caption("Auto-résultats : daemon portefeuille actif (toutes les 10 min).")
        elif has_pending and not ENABLE_PORTFOLIO_AUTO_RESULTS:
            st.caption("Auto-résultats au chargement désactivé (mode performance). Utilise le bouton de MAJ manuelle.")
        
        if df_bets.empty:
            st.info("Vous n'avez pas encore enregistré de paris. Allez dans l'onglet 'Live Tracker' pour trouver de la Value !")
        else:
            # Base temporelle : jour du match (match_date) si disponible, sinon jour d'enregistrement
            if "match_date" in df_bets.columns:
                _sched = df_bets["match_date"].map(
                    lambda x: normalize_schedule_date(x)
                    if pd.notna(x) and str(x).strip() != ""
                    else None
                )
                df_bets["date_dt"] = pd.to_datetime(_sched.fillna(df_bets["date"]), errors="coerce")
            else:
                df_bets["date_dt"] = pd.to_datetime(df_bets["date"], errors="coerce")
            if df_bets["date_dt"].isna().all():
                df_bets["date_dt"] = pd.Timestamp.today().normalize()
            else:
                df_bets["date_dt"] = df_bets["date_dt"].fillna(pd.Timestamp.today().normalize())
            df_bets = df_bets.sort_values(["date_dt", "id"]).reset_index(drop=True)
    
            if "tracker_source" in df_bets.columns:
                _ts_pf = df_bets["tracker_source"].fillna("").astype(str)
                df_live_pf = df_bets[
                    _ts_pf.isin(APP_KELLY_TRACKER_SOURCES) | (_ts_pf == "")
                ].copy()
            else:
                df_live_pf = df_bets.iloc[0:0].copy()
    
            if not df_live_pf.empty:
                snap_pf = live_tracker_bankroll_snapshot()
                lt_open = df_live_pf[df_live_pf["status"] == "En cours"]
                lt_closed = df_live_pf[df_live_pf["status"] != "En cours"]
                _n_top5 = int(
                    (df_live_pf["tracker_source"].fillna("") == "top5_proba_action").sum()
                )
                st.markdown("### 📟 Bankroll Kelly (app)")
                st.caption(
                    "BR et P/L sur les paris enregistrés via Live Tracker, Paris du jour "
                    f"ou in-play ({len(df_live_pf)} lignes dont {_n_top5} Paris du jour). "
                    "La BR disponible se met à jour quand les paris passent Gagné / Perdu / Annulé. "
                    "Les métriques « Paris totaux » ci-dessous incluent tous les paris du tableau."
                )
                if MOBILE_COMPACT:
                    _lx1, _lx2 = st.columns(2)
                    _lx1.metric("Paris Kelly (app)", len(df_live_pf))
                    _lx2.metric("En cours", len(lt_open))
                    _lx3, _lx4 = st.columns(2)
                    _lx3.metric("BR dispo (€)", f"{snap_pf['available_eur']:.2f}")
                    _lx4.metric("Engagé (€)", f"{snap_pf['committed_open_eur']:.2f}")
                    st.metric("Capital total (€)", f"{snap_pf['equity_eur']:.2f}")
                else:
                    lx1, lx2, lx3, lx4, lx5 = st.columns(5)
                    lx1.metric("Paris Kelly (app)", len(df_live_pf))
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
                if MOBILE_COMPACT:
                    _ly1, _ly2 = st.columns(2)
                    _ly1.metric("Profit net (clos, €)", f"{pl_sub:.2f}")
                    _ly2.metric("ROI clôturé", f"{roi_lt:.1f}%")
                    st.metric("Winrate clos", f"{wr_lt:.1f}%")
                else:
                    ly1, ly2, ly3 = st.columns(3)
                    ly1.metric("Profit net (clos, €)", f"{pl_sub:.2f}")
                    ly2.metric("ROI sur mises clôturées", f"{roi_lt:.1f}%")
                    ly3.metric("Winrate clos", f"{wr_lt:.1f}%")
    
            # --- CLV analytics (independent from match result) ---
            if "clv_score" in df_bets.columns:
                df_clv = df_bets.copy()
                df_clv["clv_score"] = pd.to_numeric(df_clv["clv_score"], errors="coerce")
                df_clv["closing_odd"] = pd.to_numeric(df_clv.get("closing_odd"), errors="coerce")
                df_clv = df_clv[df_clv["clv_score"].notna()].copy()
                if not df_clv.empty:
                    st.markdown("### 📉 Closing Line Value (CLV)")
                    if MOBILE_COMPACT:
                        c_k1, c_k2 = st.columns(2)
                        c_k3, c_k4 = st.columns(2)
                    else:
                        c_k1, c_k2, c_k3, c_k4 = st.columns(4)
                    clv_mean = float(df_clv["clv_score"].mean())
                    clv_med = float(df_clv["clv_score"].median())
                    clv_cov = float(len(df_clv) / max(1, len(df_bets)) * 100.0)
                    c_k1.metric("CLV Moyenne", f"{clv_mean*100:+.2f}%")
                    c_k2.metric("CLV Médiane", f"{clv_med*100:+.2f}%")
                    c_k3.metric("Paris avec closing", f"{len(df_clv)}/{len(df_bets)}")
                    c_k4.metric("Couverture CLV", f"{clv_cov:.1f}%")
    
                    plot = df_clv.sort_values(["date_dt", "id"]).copy()
                    plot["cum_clv_pct"] = (plot["clv_score"].cumsum() * 100.0)
                    plot["cum_profit"] = plot["profit"].cumsum()
                    plot_df = plot.set_index("date_dt")[["cum_clv_pct", "cum_profit"]]
                    st.caption("CLV cumulée (en points de %) vs profit cumulé réel")
                    st.line_chart(plot_df, use_container_width=True)
    
                    if MOBILE_COMPACT:
                        seg1 = st.container()
                        seg2 = st.container()
                    else:
                        seg1, seg2 = st.columns(2)
                    with seg1:
                        st.caption("CLV par circuit (ATP vs WTA)")
                        clv_tour = df_clv.assign(
                            tour_key=df_clv.get("tour").fillna("N/A").replace("", "N/A")
                        ).groupby("tour_key", as_index=False).agg(
                            clv_mean=("clv_score", "mean"),
                            n=("clv_score", "count"),
                        )
                        if not clv_tour.empty:
                            clv_tour.columns = ["Circuit", "CLV moyenne", "n"]
                            clv_tour["CLV moyenne"] = clv_tour["CLV moyenne"] * 100.0
                            st.dataframe(clv_tour, use_container_width=True, hide_index=True)
                    with seg2:
                        st.caption("CLV par segment (Clay_G, Hard_M, etc.)")
                        seg_col = "segment_key" if "segment_key" in df_clv.columns else None
                        if seg_col is None:
                            st.info("Aucun segment stocké pour ces paris.")
                        else:
                            dseg = df_clv.copy()
                            dseg["segment_key"] = dseg["segment_key"].fillna("").astype(str)
                            dseg = dseg[dseg["segment_key"].str.strip() != ""]
                            if dseg.empty:
                                st.info("Aucun segment stocké pour ces paris.")
                            else:
                                clv_seg = dseg.groupby("segment_key", as_index=False).agg(
                                    clv_mean=("clv_score", "mean"),
                                    n=("clv_score", "count"),
                                )
                                clv_seg.columns = ["Segment", "CLV moyenne", "n"]
                                clv_seg["CLV moyenne"] = clv_seg["CLV moyenne"] * 100.0
                                st.dataframe(
                                    clv_seg.sort_values("n", ascending=False),
                                    use_container_width=True,
                                    hide_index=True,
                                )
                else:
                    st.caption("CLV: pas encore de closing odds disponibles pour les paris enregistrés.")
    
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
            
            if MOBILE_COMPACT:
                _k1, _k2 = st.columns(2)
                _k1.metric("Paris", len(df_bets))
                _k2.metric("En cours", nb_en_cours)
                _k3, _k4 = st.columns(2)
                _k3.metric("Gagnés", nb_gagnes)
                _k4.metric("Perdus", nb_perdus)
                _k5, _k6 = st.columns(2)
                _k5.metric("Annulés", nb_annule, help="Walkover / abandon : mise remboursée")
                _k6.metric("Winrate", f"{winrate_closed:.1f}%")
                st.metric("Profit net", f"{total_profit:.2f} U", f"{roi:.1f}% ROI")
                c1 = st.container()
                c2 = st.container()
            else:
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
                                _clear_portfolio_runtime_caches()
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
                            _clear_portfolio_runtime_caches()
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
                        log_mtime = _safe_strftime_mtime(os.path.getmtime(log_path), "%H:%M")
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
                
            _hist_cols = ["date", "match_name", "bet_on", "odds", "stake", "status", "profit"]
            if "match_date" in df_view.columns:
                _hist_cols.insert(1, "match_date")
            _hist_df = df_view[_hist_cols].copy()
            if "match_date" in _hist_df.columns:
                _hist_df = _hist_df.rename(columns={"date": "Pari (jour)", "match_date": "Match (jour)"})
            else:
                _hist_df = _hist_df.rename(columns={"date": "Pari (jour)"})
            if MOBILE_COMPACT:
                _compact_cols = [c for c in ["Match (jour)", "Pari (jour)", "bet_on", "odds", "stake", "status", "profit"] if c in _hist_df.columns]
                _compact = _hist_df[_compact_cols].copy()
                _compact = _compact.rename(
                    columns={
                        "bet_on": "Sélection",
                        "odds": "Cote",
                        "stake": "Mise",
                        "status": "Statut",
                        "profit": "P/L",
                    }
                )
                st.dataframe(
                    _compact.style.map(color_status, subset=["Statut"] if "Statut" in _compact.columns else []),
                    use_container_width=True,
                )
                with st.expander("Voir historique complet", expanded=False):
                    st.dataframe(
                        _hist_df.style.map(color_status, subset=["status"]),
                        use_container_width=True,
                    )
            else:
                st.dataframe(
                    _hist_df.style.map(color_status, subset=["status"]),
                    use_container_width=True,
                )
    
    with tab_diag:
        st.header("🧪 Diagnostics Modèle")
        try:
            _conn_alert = sqlite3.connect("data/bettinghud.db")
            _df_alert = pd.read_sql(
                "SELECT id, date, clv_score FROM user_bets WHERE clv_score IS NOT NULL ORDER BY id ASC",
                _conn_alert,
            )
            _conn_alert.close()
            _clv20_mean, _clv_msg = _compute_clv_alert(_df_alert)
            if _clv_msg:
                if _clv20_mean is not None and _clv20_mean < 0.0:
                    st.warning(f"⚠️ {_clv_msg} (CLV20={_clv20_mean*100:+.2f}%)")
                elif _clv20_mean is not None and _clv20_mean > 0.05:
                    st.success(f"✅ {_clv_msg} (CLV20={_clv20_mean*100:+.2f}%)")
        except Exception:
            pass
        st.markdown(
            "Évaluation **honnête** sur historique ATP (A/M/G) : modèle ré-entraîné **sans fuite** "
            f"(données &lt; 1ʳᵉ date de l'échantillon), inférence au **`match_date`**, "
            f"seuil EV **{DEFAULT_LIVE_EV_THRESHOLD_PCT:.0f} %**. "
            "Ce n'est **pas** ton portefeuille live — voir **Tracking modèle** pour les paris réels."
        )
        st.info(
            "Limites : pas de WTA/Challenger, pas de Kelly/plafond journalier, "
            "cotes tennis-data quand disponibles sinon synthétique (rang/points). "
            "Le premier run peut prendre plusieurs minutes (ré-entraînement XGBoost)."
        )
    
        c1, c2, c3 = st.columns(3)
        with c1:
            diag_years = st.slider("Période diagnostics", min_value=2010, max_value=2026, value=(2018, 2026), step=1, key="diag_years")
        with c2:
            diag_n = st.number_input("Taille échantillon", min_value=500, max_value=50000, value=5000, step=500, key="diag_n")
        with c3:
            diag_seed = st.number_input("Seed diagnostics", min_value=1, max_value=999999, value=42, step=1, key="diag_seed")
    
        if st.button("Lancer diagnostics (mode honnête)"):
            with st.spinner(
                "Ré-entraînement no-leak + scoring (peut prendre 2–8 min selon l'échantillon)…"
            ):
                diag = compute_model_diagnostics(diag_years[0], diag_years[1], int(diag_n), int(diag_seed))
    
            if not diag:
                st.warning("Aucune donnée disponible pour ces paramètres.")
            elif diag.get("ok") is False:
                st.warning(diag.get("reason") or "Échec du diagnostic.")
            else:
                st.caption(
                    f"Train no-leak : ≤ `{diag.get('train_cutoff', '—')}` "
                    f"({int(diag.get('train_rows', 0)):,} matchs) · "
                    f"Cotes réelles sur {float(diag.get('pct_real_odds', 0)):.0f} % des matchs évalués "
                    f"(tennis-data dans `data/raw/tennis_data/`)."
                )
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("Matchs évalués", diag["n_matches"])
                m2.metric("Accuracy", f"{diag['accuracy']*100:.1f}%")
                m3.metric("Brier", f"{diag['brier']:.4f}")
                m4.metric("Confiance moyenne", f"{diag['mean_confidence']*100:.1f}%")
                m5.metric(
                    "ROI value simulé (1u)",
                    f"{diag['total_roi']:.1f}%",
                    f"{diag['total_bets']} paris · EV≥{diag.get('ev_threshold_pct', 15):.0f}%",
                )
                if diag.get("roi_real_odds_pct") is not None or diag.get("roi_synthetic_odds_pct") is not None:
                    r1, r2 = st.columns(2)
                    if diag.get("roi_real_odds_pct") is not None:
                        r1.metric(
                            "ROI (cotes tennis-data)",
                            f"{diag['roi_real_odds_pct']:.1f}%",
                            f"{diag.get('n_bets_real_odds', 0)} paris value",
                        )
                    if diag.get("roi_synthetic_odds_pct") is not None:
                        r2.metric(
                            "ROI (cotes synthétiques)",
                            f"{diag['roi_synthetic_odds_pct']:.1f}%",
                            f"{diag.get('n_bets_synthetic_odds', 0)} paris value",
                        )
    
                st.markdown("---")
                st.subheader("Calibration modèle (proba vs résultat observé)")
                calib = diag["calibration"].copy()
                if not calib.empty:
                    chart = calib[["mean_prob", "observed_win_rate"]].set_index("mean_prob")
                    st.line_chart(chart, use_container_width=True)
                    st.dataframe(calib, use_container_width=True)
                else:
                    st.info("Pas assez de points pour la calibration.")
    
                st.subheader("ROI simulé par bucket de confiance (≠ portefeuille réel)")
                roi_conf = diag["roi_by_conf"].copy()
                if not roi_conf.empty:
                    roi_plot = roi_conf.copy()
                    roi_plot["conf_bin"] = roi_plot["conf_bin"].astype(str)
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
    
    with tab_tracking:
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
    
            if MOBILE_COMPACT:
                m1, m2 = st.columns(2)
                m1.metric("Paris clos", g["n"])
                m2.metric(
                    "Hit-rate réel",
                    f"{g['hit']*100:.1f}%",
                    f"prév. {g['expected_hit']*100:.1f}%",
                )
                m3, m4 = st.columns(2)
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
                m5, m6 = st.columns(2)
                m5.metric(
                    "vs baseline ROI",
                    f"{(g['realised_roi'] - g['baseline_roi'])*100:+.1f} pp",
                )
                m6.metric(
                    "vs baseline hit",
                    f"{(g['hit'] - g['baseline_hit'])*100:+.1f} pp",
                )
            else:
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
                if MOBILE_COMPACT:
                    st.dataframe(disp[["proba moyenne", "observé", "n"]], use_container_width=True, hide_index=True)
                    with st.expander("Détails calibration", expanded=False):
                        st.dataframe(disp, use_container_width=True, hide_index=True)
                else:
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
                if MOBILE_COMPACT:
                    st.dataframe(disp_ev[["EV bucket", "n", "ROI réel"]], use_container_width=True, hide_index=True)
                    with st.expander("Détails ROI par bucket EV", expanded=False):
                        st.dataframe(disp_ev, use_container_width=True, hide_index=True)
                else:
                    st.dataframe(disp_ev, use_container_width=True, hide_index=True)
            else:
                st.info("Aucun pari clos avec EV stockée. Place quelques paris depuis la nouvelle UI.")
    
            if MOBILE_COMPACT:
                c1 = st.container()
                c2 = st.container()
            else:
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
                    if MOBILE_COMPACT:
                        st.dataframe(disp_tour[["Tour", "n", "ROI %"]], use_container_width=True, hide_index=True)
                        with st.expander("Détails par tour", expanded=False):
                            st.dataframe(disp_tour, use_container_width=True, hide_index=True)
                    else:
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
                    if MOBILE_COMPACT:
                        st.dataframe(disp_s[["Surface", "n", "ROI %"]], use_container_width=True, hide_index=True)
                        with st.expander("Détails par surface", expanded=False):
                            st.dataframe(disp_s, use_container_width=True, hide_index=True)
                    else:
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
                if MOBILE_COMPACT:
                    _rc = [c for c in ["run_ts", "match_name", "action", "new_status"] if c in recon_df.columns]
                    st.dataframe(recon_df[_rc], use_container_width=True, hide_index=True)
                    with st.expander("Voir log réconciliation complet", expanded=False):
                        st.dataframe(recon_df, use_container_width=True, hide_index=True)
                else:
                    st.dataframe(recon_df, use_container_width=True, hide_index=True)
    
    if False:
        st.header("🧠 Human Factors")
        st.caption(
            "Styles tactiques (KMeans v4.5), voyage/jetlag inféré depuis l’historique des matchs, "
            "et score clutch 52 semaines. Le bundle `xgb_model_tml_v45.pkl` doit être entraîné pour les étiquettes de style ML."
        )
        hf_ctrl1, hf_ctrl2, hf_ctrl3 = st.columns([1, 1, 2])
        with hf_ctrl1:
            hf_enable = st.checkbox("Charger la vue détaillée", value=False, key="hf_enable_detailed")
        with hf_ctrl2:
            hf_page_size = st.selectbox("Lignes/page", [10, 20, 30, 60], index=1, key="hf_page_size")
        with hf_ctrl3:
            st.caption("Astuce perf: laisse décoché si tu n’analyses pas l’onglet maintenant.")
        if not hf_enable:
            st.info("Vue détaillée désactivée pour accélérer le chargement global. Coche l’option pour afficher les cartes HF.")
        else:
            _csv_x, _mtime_x = _prematch_csv_signature()
            hf_list = get_latest_scraped_data(
                _csv_x,
                _mtime_x,
                PROFILE_CACHE_SCHEMA,
                _ml_model_mtime(),
                _ENGINES_CACHE_VERSION,
            )
            hf_list = [
                z
                for z in hf_list
                if float(z.get("odd_p1") or 0) > 1.0 and float(z.get("odd_p2") or 0) > 1.0
            ]
            if not hf_list:
                st.info("Aucun match live à afficher. Vérifie le scraper Flashscore puis « Rafraîchir les données ».")
                hf_page_rows = []
                hf_page_start = 0
            else:
                hf_total = len(hf_list)
                hf_pages = max(1, int(np.ceil(hf_total / float(hf_page_size))))
                hf_page = st.number_input(
                    "Page HF",
                    min_value=1,
                    max_value=hf_pages,
                    value=1,
                    step=1,
                    key="hf_page_idx",
                )
                hf_page_start = int((hf_page - 1) * hf_page_size)
                hf_page_rows = hf_list[hf_page_start: hf_page_start + int(hf_page_size)]
                st.caption(
                    f"Affichage {hf_page_start + 1}-{hf_page_start + len(hf_page_rows)} / {hf_total} matchs."
                )
            for _i_hf, zm in enumerate(hf_page_rows, start=hf_page_start):
                fs = zm.get("feature_snapshot") or {}
                st.markdown(f"**{zm.get('player1', '')}** vs **{zm.get('player2', '')}** — `{zm.get('tournament', '')}`")
                hc1, hc2, hc3 = st.columns(3)
                with hc1:
                    st.markdown(f"Style **P1** : `{fs.get('human_p1_style', '—')}`")
                    if fs.get("p1_jetlag_alert"):
                        st.markdown("⚠️ **Jetlag Alert** — P1")
                with hc2:
                    st.markdown(f"Style **P2** : `{fs.get('human_p2_style', '—')}`")
                    if fs.get("p2_jetlag_alert"):
                        st.markdown("⚠️ **Jetlag Alert** — P2")
                with hc3:
                    smb = fs.get("style_matchup_bias")
                    if smb is not None:
                        st.caption(f"Style matchup bias (ML) : {float(smb):+.3f}")
                p1_style = str(fs.get("human_p1_style") or "—")
                p2_style = str(fs.get("human_p2_style") or "—")
                wr_p1 = fs.get("style_surface_winrate_p1")
                n_duels = int(fs.get("style_surface_winrate_samples", 0) or 0)
                smb_val = float(fs.get("style_matchup_bias", 0.0) or 0.0)
                slow_flag = bool(fs.get("style_is_slow_surface", False))
                if wr_p1 is None:
                    wr_p1 = float(np.clip(0.5 + smb_val, 0.0, 1.0))
                wr_p1 = float(np.clip(float(wr_p1), 0.0, 1.0))
                _style_emoji = {
                    "Big Server": "🚀",
                    "Counter-Puncher": "🛡️",
                    "Aggressive Baseliner": "⚡",
                    "Aggressive": "⚡",
                    "Tactical / Slicer": "🎯",
                    "Tactical/Slicer": "🎯",
                }
                st.caption(
                    f"P1: {p1_style} {_style_emoji.get(p1_style, '')} | "
                    f"P2: {p2_style} {_style_emoji.get(p2_style, '')}"
                )
                st.markdown(
                    f"**Analyse Tactique :** Le style **{p1_style}** gagne statistiquement **{wr_p1*100:.1f}%** "
                    f"de ses duels contre **{p2_style}** sur cette surface."
                )
                if float(fs.get("style_drift_detected", 0.0) or 0.0) > 0.5:
                    st.warning("⚠️ Évolution de style détectée : Profil tactique en transition.")
                if smb_val >= 0.04:
                    expl = "Avantage tactique net pour P1."
                elif smb_val <= -0.04:
                    expl = "Matchup défavorable pour P1 sur cette configuration."
                else:
                    expl = "Matchup tactique relativement équilibré."
                if slow_flag and "Big Server" in p1_style and "Counter-Puncher" in p2_style:
                    expl += " Surface lente: le serveur est davantage neutralisé."
                st.caption(expl)
                st.slider(
                    "Mismatch Tactique ↔ Avantage Tactique",
                    min_value=0,
                    max_value=100,
                    value=int(round(np.clip(50.0 + smb_val * 300.0, 0.0, 100.0))),
                    disabled=True,
                    key=f"hf_battle_slider_{_i_hf}",
                    help=f"Jauge basée sur le biais tactique final (n={n_duels}).",
                )
                _labels = ["Big Server", "Aggressive Baseliner", "Tactical / Slicer", "Counter-Puncher"]
                _mix1 = fs.get("p1_style_mix") or [0.25, 0.25, 0.25, 0.25]
                _mix2 = fs.get("p2_style_mix") or [0.25, 0.25, 0.25, 0.25]
                st.caption("Proximité styles (distribution KMeans)")
                sm1, sm2 = st.columns(2)
                with sm1:
                    st.markdown(f"**{zm.get('player1', 'P1')}**")
                    for lb, vv in zip(_labels, _mix1):
                        st.progress(float(np.clip(vv, 0.0, 1.0)), text=f"{lb}: {float(vv)*100:.0f}%")
                with sm2:
                    st.markdown(f"**{zm.get('player2', 'P2')}**")
                    for lb, vv in zip(_labels, _mix2):
                        st.progress(float(np.clip(vv, 0.0, 1.0)), text=f"{lb}: {float(vv)*100:.0f}%")
                g1, g2 = st.columns(2)
                with g1:
                    c1 = float(fs.get("p1_clutch52", 0.5) or 0.5)
                    st.metric("Clutch P1 (52 sem.)", f"{c1:.0%}")
                    st.progress(min(1.0, max(0.0, c1)))
                with g2:
                    c2 = float(fs.get("p2_clutch52", 0.5) or 0.5)
                    st.metric("Clutch P2 (52 sem.)", f"{c2:.0%}")
                    st.progress(min(1.0, max(0.0, c2)))
                st.divider()
    
    # Bouton de rafraîchissement
    if st.button("🔄 Rafraîchir les données"):
        get_latest_scraped_data.clear()
        try:
            if os.path.isfile(SNAPSHOT_PATH):
                os.remove(SNAPSHOT_PATH)
        except OSError:
            pass
        st.session_state.pop("_live_matches_sig", None)
        st.session_state.pop("_live_matches_cache", None)
        st.rerun()
    
    with tab_settings:
        st.header("⚙️ Paramètres")
        from scripts.web_auth import render_account_settings

        render_account_settings()
        if _bettinghud_environment() == "prod":
            st.warning(
                "Environnement **PROD** (serveur dédié) — paris et données de référence. "
                "Développer et tester d’abord en **PREPROD** (PC local)."
            )
        else:
            st.info(
                "Environnement **PREPROD** (poste local) — version de test avant déploiement sur le serveur **PROD**."
            )
        _render_system_status_banner()
        if _preload_status_caption:
            st.caption(_preload_status_caption)
        mobile_compact = st.toggle(
            "📱 Mode mobile compact",
            value=bool(st.session_state.get("mobile_compact_mode", False)),
            key="mobile_compact_mode",
            help="Empile les blocs principaux pour faciliter l'usage smartphone.",
        )
        if bool(mobile_compact) != bool(MOBILE_COMPACT):
            st.rerun()
        st.caption(
            "Stats live par tour : **ATP → TennisMyLife** (`matches_recent`), "
            "**WTA → Tennis Abstract / Sackmann** (`wta_matches` + `rankings_wta_current`)."
        )
        if not FAST_LIVE_MODE:
            st.caption(
                "Mode qualité : stats réelles + mapping (plus de fast_default). "
                "Après sync TML / backfill, « Rafraîchir les données » invalide le cache."
            )
        if LIVE_ONLY_TODAY_TOMORROW:
            st.caption(
                "Live limité à aujourd’hui + demain (charge réduite). "
                "Tout afficher : BETTINGHUD_LIVE_ONLY_TODAY_TOMORROW=false."
            )
        if ENABLE_LIVE_DATA_DAEMON:
            st.caption(
                f"Projection du jour préchargée ~{LIVE_PROJECTION_WARMUP_DELAY_SEC} s après ouverture "
                f"(BETTINGHUD_LIVE_PROJECTION_WARMUP). Daemon toutes les "
                f"{LIVE_DATA_DAEMON_INTERVAL_SEC // 60} min (rebuild reporté si Live actif depuis moins de "
                f"{LIVE_DAEMON_DEFER_UI_SEC} s et snapshot déjà prêt). Live Tracker : MAJ auto "
                f"{LIVE_TRACKER_AUTO_REFRESH_SEC} s."
            )
        st.slider(
            "Seuil minimal de Value (EV %)",
            min_value=1.0,
            max_value=25.0,
            value=DEFAULT_LIVE_EV_THRESHOLD_PCT,
            step=0.5,
            key="sidebar_ev_threshold",
        )
        st.selectbox("Filtre Surface", ["Toutes", "Hard", "Clay", "Grass"])

        st.subheader("Automatisation & Scraper")
        st.caption(
            "Mise à jour base ATP/WTA en arrière-plan (déf. toutes les 24h après 2min). "
            "Sans dépôt git dans `data/raw/tennis_wta`, les CSV WTA sont téléchargés depuis GitHub. "
            "Log : `data/logs/tours_auto_sync.log`. Désactiver : `BETTINGHUD_AUTO_SYNC_TOURS=0`."
        )
        st.caption(
            "Réentraînement ML automatique : défaut **1× / semaine** (après 2h de délai). "
            "Désactiver : `BETTINGHUD_AUTO_ML_TRAIN_WEEKLY=0`. "
            "Intervalle (s) : `BETTINGHUD_AUTO_ML_TRAIN_INTERVAL_SEC` (déf. 604800 = 7 jours). "
            "Délai avant 1er run : `BETTINGHUD_AUTO_ML_TRAIN_INITIAL_DELAY_SEC` (déf. 7200)."
        )
        _fresh = get_data_freshness_snapshot()
        with st.expander("Fraîcheur ATP/WTA & modèle ML", expanded=False):
            st.markdown(
                f"**Dernière sync DB** (ATP+WTA, pipeline OK) : `{_format_meta_utc(_fresh.get('last_tours_sync_iso'))}`"
            )
            st.markdown(
                f"**Dernier entraînement ML** (`update_model_tml`) : `{_format_meta_utc(_fresh.get('last_ml_train_iso'))}`"
            )
            st.markdown(
                f"**Fichier modèle** (`xgb_model_tml_v45.pkl`, `v4` ou `v1`) : `{_format_mtime_local(_fresh.get('model_bundle_mtime'))}`"
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

        auto_mode = st.checkbox("Mode Auto (Scrape 10m / Refresh 1m)", value=False)
        if auto_mode:
            start_background_scraper()
            st.success("Mode Auto activé (scraper en arrière-plan).")
            st.caption("Auto-refresh visuel désactivé pour éviter les reruns automatiques du Backtest.")
        else:
            st.info("Mode manuel actif")
            if st.button("🚀 Lancer le Scraper manuellement"):
                with st.spinner("Scraping en cours..."):
                    scraper = FlashscoreScraper()
                    asyncio.run(scraper.get_today_matches_and_odds())
                get_latest_scraped_data.clear()
                st.success("Scraping terminé !")
                st.rerun()
            if st.button("🧠 MAJ TML + Réentraîner modèle"):
                with st.spinner("Sync TennisMyLife + entraînement modèle en cours..."):
                    subprocess.run([sys.executable, "scripts/update_model_tml.py"], check=False)
                st.success("Mise à jour modèle terminée.")
                st.rerun()
