import streamlit as st
import pandas as pd
import numpy as np
import inspect
import sys
import os
import glob
import asyncio
import sqlite3
import threading
import time
import subprocess
import re
from datetime import datetime
from streamlit_autorefresh import st_autorefresh

# Ajouter le répertoire parent au path pour les imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.value_detector import ValueDetector
from scripts.ml_model import TennisMLModel
from scripts.stats_engine import TennisStatsEngine
from scripts.scraper_prematch import FlashscoreScraper
from scripts.simulate_day import simulate_day
from scripts.scraper_results import ResultsScraper
import scripts.scraper_profiles as scraper_profiles
from scripts.scraper_profiles import ProfileScraper
from scripts.data_quality import run_data_quality_checks
from scripts.model_monitor import compute_monthly_diagnostics, compute_feature_drift

PROFILE_CACHE_SCHEMA = getattr(scraper_profiles, "PROFILE_CACHE_VERSION", 1)

st.set_page_config(page_title="BettingHUD - Tennis", page_icon="🎾", layout="wide")

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

@st.cache_resource
def load_engines():
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

ml_model, stats_engine, profile_scraper = load_engines()

@st.cache_resource
def start_background_scraper():
    def scrape_loop():
        scraper = FlashscoreScraper()
        while True:
            try:
                # 1. Scrape les matchs et les cotes
                matches = asyncio.run(scraper.get_today_matches_and_odds())
                
                # 2. Pré-charger les profils de tous les joueurs trouvés (pour ne pas bloquer l'UI)
                if matches:
                    from scripts.scraper_profiles import ProfileScraper
                    ps = ProfileScraper()
                    urls_to_scrape = set()
                    for m in matches:
                        # Ignorer les tournois mineurs pour économiser les requêtes
                        if m.get('category') not in ['ATP', 'WTA']: 
                            continue
                            
                        if m.get('p1_url'): urls_to_scrape.add(m['p1_url'])
                        if m.get('p2_url'): urls_to_scrape.add(m['p2_url'])
                    
                    print(f"Pré-chargement en arrière-plan de {len(urls_to_scrape)} profils...")
                    for i, url in enumerate(urls_to_scrape):
                        ps.scrape_profile(url)
                        if i % 10 == 0:
                            print(f"Progression profils : {i}/{len(urls_to_scrape)}")
                
            except Exception as e:
                print("Erreur du scraper en arrière-plan:", e)
            
            # Attendre 60 minutes avant de refaire un grand tour complet
            time.sleep(3600)
            
    t = threading.Thread(target=scrape_loop, daemon=True)
    t.start()
    return t

def _pts_from_rank(rank):
    """Estimation des points ATP/WTA à partir du classement (profils TennisExplorer sans points bruts)."""
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


def _merge_live_profile(stats, profile):
    """Met à jour forme / fatigue à partir du profil TennisExplorer scrapé.

    Si le profil est absent (erreur réseau, URL manquante), retombée sur la base ATP
    locale : fenêtre temporelle = derniers N jours avant la **dernière date du jeu de données**,
    pas les N derniers jours calendaires — les libellés UI « 14 j / 3 mois » peuvent alors être trompeurs.
    """
    if not profile:
        return (
            stats_engine.get_recent_form(stats.get("_pid")),
            stats_engine.get_recent_fatigue(stats.get("_pid")),
        )
    form = {"win_pct": profile["win_pct"], "matches": profile["form_matches"]}
    fatigue = {"minutes_played": profile["fatigue_minutes"], "matches": profile["fatigue_matches"]}
    if profile.get("rank") not in (None, 100):
        stats["rank"] = profile["rank"]
        stats["pts"] = _pts_from_rank(profile["rank"])
    if profile.get("age") not in (None, 25):
        stats["age"] = profile["age"]
    if profile.get("hand") not in (None, "U"):
        stats["hand"] = profile["hand"]
    return form, fatigue


def _profile_sources_caption(match: dict) -> str:
    def tag(ok: bool) -> str:
        return "TennisExplorer" if ok else "base ATP (fallback)"

    return (
        f"**Profils récents** — {match['player1']}: {tag(match.get('p1_profile_loaded'))} · "
        f"{match['player2']}: {tag(match.get('p2_profile_loaded'))}"
    )


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


@st.cache_data(ttl=1800)
def _load_signal_matches():
    conn = sqlite3.connect("data/bettinghud.db")
    q = """
        SELECT tourney_date, surface, winner_name, loser_name, score,
               w_ace, w_svpt, w_1stWon, w_2ndWon, w_SvGms, w_bpSaved, w_bpFaced,
               l_ace, l_svpt, l_1stWon, l_2ndWon, l_SvGms, l_bpSaved, l_bpFaced
        FROM matches_recent
        WHERE source='tennismylife' AND CAST(substr(tourney_date,1,4) AS INTEGER) >= 2023
    """
    df = pd.read_sql(q, conn)
    conn.close()
    df["tourney_date"] = pd.to_datetime(df["tourney_date"], errors="coerce")
    df = df.dropna(subset=["tourney_date"])
    df["w_key"] = df["winner_name"].map(_name_key)
    df["l_key"] = df["loser_name"].map(_name_key)
    return df


def _compute_live_advanced_signals(p1_name: str, p2_name: str, surface: str):
    df = _load_signal_matches()
    p1k, p2k = _name_key(p1_name), _name_key(p2_name)
    if not p1k or not p2k:
        return {"style_advantage_score": 0.5, "p1_clutch_index": 0.5, "p2_clutch_index": 0.5, "p1_days": 7, "p2_days": 7}
    max_date = df["tourney_date"].max()
    p1_rows = df[(df["w_key"] == p1k) | (df["l_key"] == p1k)].copy().sort_values("tourney_date")
    p2_rows = df[(df["w_key"] == p2k) | (df["l_key"] == p2k)].copy().sort_values("tourney_date")

    def _days(rows):
        return 7 if rows.empty else int(max(0, (max_date - rows["tourney_date"].max()).days))

    def _player_cluster(rows):
        if rows.empty:
            return "all_court"
        r = rows.tail(20)
        ace_rates, serve_wins, break_rates = [], [], []
        for x in r.itertuples(index=False):
            is_w = x.w_key == (x.w_key if x.w_key in (p1k, p2k) else x.w_key)
            # player perspective by membership
            if x.w_key in (p1k, p2k):
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

    p2_cluster = _player_cluster(p2_rows)
    # Approximation: style_adv = 0.5 + écart de forme plafonné
    p1_form = 0.5 if p1_rows.empty else float((p1_rows["w_key"] == p1k).tail(20).mean())
    p2_form = 0.5 if p2_rows.empty else float((p2_rows["w_key"] == p2k).tail(20).mean())
    style_adv = max(0.0, min(1.0, 0.5 + (p1_form - p2_form) * 0.35))

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

    return {
        "style_advantage_score": style_adv if p2_cluster else 0.5,
        "p1_clutch_index": _clutch(p1_rows, p1k),
        "p2_clutch_index": _clutch(p2_rows, p2k),
        "p1_days": _days(p1_rows),
        "p2_days": _days(p2_rows),
    }


def _prematch_csv_signature():
    """Chemin + mtime du CSV prematch le plus récent (mtime = contenu réellement mis à jour)."""
    data_dir = os.path.join("data", "scraped")
    files = glob.glob(os.path.join(data_dir, "*.csv"))
    if not files:
        return "", 0.0
    latest_file = max(files, key=os.path.getmtime)
    return latest_file, os.path.getmtime(latest_file)


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


@st.cache_data(ttl=300)
def get_latest_scraped_data(csv_path: str, _prematch_mtime: float, _profile_cache_schema: int):
    """_prematch_mtime et _profile_cache_schema : clés d'invalidation (nouveau CSV ou nouveau schéma profils)."""
    if not csv_path or not os.path.isfile(csv_path):
        return []
    df = pd.read_csv(csv_path)
    
    # Filtrer uniquement gros tournois ATP/WTA (exclut Challenger/ITF/mineurs).
    df = df[df.apply(lambda r: _is_major_atp_wta(r.get("category"), r.get("tournament")), axis=1)]
    
    # Une seule lecture par joueur (URL unique), pas une par ligne de match
    urls_to_fetch = set()
    for _, row in df.iterrows():
        u1, u2 = row.get("p1_url"), row.get("p2_url")
        if pd.notna(u1) and isinstance(u1, str) and u1.strip():
            urls_to_fetch.add(u1.strip())
        if pd.notna(u2) and isinstance(u2, str) and u2.strip():
            urls_to_fetch.add(u2.strip())
    profiles_by_url = {url: profile_scraper.scrape_profile(url) for url in urls_to_fetch}
    
    matches = []
    for _, row in df.iterrows():
        # Obtenir les IDs et stats
        p1_id = stats_engine.get_player_id(row['player1'])
        p2_id = stats_engine.get_player_id(row['player2'])
        
        p1_stats = stats_engine.get_player_stats(p1_id) if p1_id else {"rank": 100, "age": 25, "ht": 185, "pts": 1000}
        p2_stats = stats_engine.get_player_stats(p2_id) if p2_id else {"rank": 100, "age": 25, "ht": 185, "pts": 1000}
        p1_stats["_pid"] = p1_id
        p2_stats["_pid"] = p2_id
        
        surface = _infer_surface(row.get("tournament", ""))
        
        true_odd_p1 = 2.0
        true_odd_p2 = 2.0
        confidence = None
        calibration_used = "Globale"
        feature_snapshot = {}
        top_features = []
        
        # Advanced Stats
        h2h = stats_engine.get_h2h(p1_id, p2_id)
        
        p1_url = row.get("p1_url")
        p2_url = row.get("p2_url")
        if pd.notna(p1_url) and isinstance(p1_url, str):
            p1_url = p1_url.strip()
        else:
            p1_url = None
        if pd.notna(p2_url) and isinstance(p2_url, str):
            p2_url = p2_url.strip()
        else:
            p2_url = None
        
        p1_profile = profiles_by_url.get(p1_url) if p1_url else None
        p2_profile = profiles_by_url.get(p2_url) if p2_url else None
        
        p1_form, p1_fatigue = _merge_live_profile(p1_stats, p1_profile)
        p2_form, p2_fatigue = _merge_live_profile(p2_stats, p2_profile)
        
        p1_stats.pop("_pid", None)
        p2_stats.pop("_pid", None)
        
        try:
            adv_signals = _compute_live_advanced_signals(row['player1'], row['player2'], surface)
            preds = ml_model.predict_match(
                surface=surface,
                p1_name=row['player1'],
                p2_name=row['player2'],
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
                p1_days_since_last_match=adv_signals.get("p1_days"),
                p2_days_since_last_match=adv_signals.get("p2_days"),
            )
            true_odd_p1 = preds['p1_true_odd']
            true_odd_p2 = preds['p2_true_odd']
            confidence = preds.get("confidence", None)
            calibration_used = preds.get("calibration_used", "Globale")
            feature_snapshot = preds.get("feature_snapshot", {}) or {}
            top_features = preds.get("top_features", []) or []
        except:
            pass

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

        matches.append({
            "prematch_id": prematch_id,
            "date": row.get('date', row['scraped_at'][:10]),
            "tournament": row.get('tournament', 'Tournoi en cours'),
            "category": row.get('category', 'ATP/WTA'),
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
            "p1_profile_loaded": p1_profile is not None,
            "p2_profile_loaded": p2_profile is not None,
        })
    return matches

st.title("🎾 BettingHUD - Value Bets Tracker")
st.markdown("---")

def init_db():
    conn = sqlite3.connect('data/bettinghud.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS user_bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        date TEXT, 
        match_name TEXT, 
        bet_on TEXT, 
        odds REAL, 
        stake REAL, 
        status TEXT DEFAULT 'En cours', 
        profit REAL DEFAULT 0.0)''')
    conn.commit()
    conn.close()

init_db()

def save_bet(match_name, bet_on, odds, stake):
    conn = sqlite3.connect('data/bettinghud.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO user_bets (date, match_name, bet_on, odds, stake, status, profit)
        VALUES (date('now'), ?, ?, ?, ?, 'En cours', 0.0)
    ''', (match_name, bet_on, odds, stake))
    conn.commit()
    conn.close()
    st.toast("✅ Pari enregistré avec succès dans votre portefeuille !")

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


@st.cache_data(ttl=900)
def get_backtest_tournaments(year_start: int, year_end: int):
    y0, y1 = sorted((int(year_start), int(year_end)))
    dmin = f"{y0}0101"
    dmax = f"{y1}1231"
    conn = sqlite3.connect("data/bettinghud.db")
    query = """
        SELECT tourney_name, COUNT(*) AS n_matches
        FROM matches_recent
        WHERE source = 'tennismylife'
          AND CAST(tourney_date AS TEXT) BETWEEN ? AND ?
        GROUP BY tourney_name
        HAVING COUNT(*) > 0
        ORDER BY tourney_name ASC
    """
    df = pd.read_sql(query, conn, params=(dmin, dmax))
    conn.close()
    return df


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


tab1, tab2, tab3, tab4, tab5 = st.tabs(["🎯 Live Tracker", "📊 Simulateur / Backtest", "💼 Mon Portefeuille", "🧪 Diagnostics Modèle", "🧩 Annexe - Paris Alternatifs"])

with tab1:
    _csv_path, _csv_mtime = _prematch_csv_signature()
    real_matches = get_latest_scraped_data(_csv_path, _csv_mtime, PROFILE_CACHE_SCHEMA)
    
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

    # Limiter le nombre de matchs affichés à l'écran pour éviter de surcharger (50 matchs max)
    filtered_matches = filtered_matches[:50]

    live_col1, live_col2 = st.columns(2)
    with live_col1:
        live_ev_threshold = st.slider(
            "Seuil EV minimum (Live, %)",
            min_value=1.0,
            max_value=12.0,
            value=3.0,
            step=0.5,
            key="live_ev_threshold",
        )
    with live_col2:
        live_conf_filter = st.selectbox(
            "Filtre confiance IA",
            ["Toutes", "Moyenne+", "Forte+", "Très forte"],
            key="live_conf_filter",
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
        st.caption(f"Fichier prematch : `{os.path.basename(_csv_path)}` · indexé à {fresh}")
    
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
                if stake_already > 0:
                    st.markdown(
                        f"<span class='bet-badge'>✅ Déjà parié : {stake_already:.1f} U sur {player_name}</span>",
                        unsafe_allow_html=True,
                    )
                
                c1, c2, c3 = st.columns([1.5, 2, 1.5])
                with c1:
                    st.markdown(f"<div style='margin-bottom: 10px;'><span class='ev-highlight'>+ {val['value_pct']:.1f}% EV</span></div>", unsafe_allow_html=True)
                    st.markdown(f"Cote Bookmaker: <span class='odd-highlight'>{odd_book}</span>", unsafe_allow_html=True)
                    st.markdown(f"Cote Estimée: **{odd_true:.2f}**")
                    st.markdown(f"Probabilité: **{1/odd_true*100:.1f}%**")
                    
                    with st.popover("ℹ️ Pourquoi cette value ?"):
                        p1_stats = match.get("p1_stats", {})
                        p2_stats = match.get("p2_stats", {})
                        st.markdown(_profile_sources_caption(match))
                        
                        st.markdown("### Pourquoi ce bet ?")
                        st.markdown(
                            f"- **Value mathématique**: l'IA estime **{1/odd_true*100:.1f}%** de chances "
                            f"(cote juste **{odd_true:.2f}**) alors que le bookmaker price **{odd_book:.2f}** "
                            f"(proba implicite **{1/odd_book*100:.1f}%**)."
                        )
                        edge_pp = (1 / odd_true * 100) - (1 / odd_book * 100)
                        st.markdown(f"- **Edge estimé**: **{edge_pp:+.1f} points** de probabilité.")
                        
                        st.markdown("**Facteurs clés pris en compte :**")
                        
                        # Helper for Hand
                        def get_hand_text(hand_code):
                            if hand_code == 'R': return 'Droitier'
                            if hand_code == 'L': return 'Gaucher'
                            return 'Inconnu'
                            
                        st.markdown(f"- 🏆 **Différence de Classement** : {p1_stats.get('rank', 'N/A')} vs {p2_stats.get('rank', 'N/A')}")
                        st.markdown(f"- 📈 **Points ATP/WTA** : {p1_stats.get('pts', 'N/A')} vs {p2_stats.get('pts', 'N/A')}")
                        st.markdown(f"- 🎂 **Âge** : {p1_stats.get('age', 'N/A')} vs {p2_stats.get('age', 'N/A')} ans")
                        st.markdown(f"- 📏 **Taille** : {p1_stats.get('ht', 'N/A')} vs {p2_stats.get('ht', 'N/A')} cm")
                        st.markdown(f"- ✋ **Main forte** : {get_hand_text(p1_stats.get('hand', 'U'))} vs {get_hand_text(p2_stats.get('hand', 'U'))}")
                        st.markdown(f"- 🌱 **Surface** : Le modèle est ajusté pour le jeu sur **{match.get('surface', 'Terre Battue')}**.")
                        st.markdown(f"- 🧪 **Calibration utilisée** : **{match.get('calibration_used', 'Globale')}**")
                        if match.get("confidence") is not None:
                            _lvl, _hint = _confidence_explained(match.get("confidence"))
                            st.markdown(f"- 🎯 **Confiance IA** : **{_lvl}**")
                            st.caption(_hint)
                        
                        st.markdown("---")
                        st.markdown("### Dynamique du match")
                        
                        h2h = match.get('h2h', {})
                        p1_h2h = h2h.get('p1_wins', 0)
                        p2_h2h = h2h.get('p2_wins', 0)
                        
                        p1_form = match.get('p1_form', {})
                        p2_form = match.get('p2_form', {})
                        
                        p1_fat = match.get('p1_fatigue', {})
                        p2_fat = match.get('p2_fatigue', {})
                        lineup_note = (
                            "simple messieurs"
                            if match.get("category") == "ATP"
                            else "simple dames"
                            if match.get("category") == "WTA"
                            else "simple"
                        )

                        if p_num == 1:
                            st.markdown(f"- ⚔️ **Face-à-face (H2H)** : **{player_name}** a gagné **{p1_h2h}** fois contre **{p2_h2h}** pour **{opp_name}**")
                            st.markdown(f"- 🔥 **Forme (~90 jours)** : **{player_name}** a {p1_form.get('win_pct', 50):.0f}% de victoires ({p1_form.get('matches', 0)} matchs) contre {p2_form.get('win_pct', 50):.0f}% pour **{opp_name}**")
                            st.markdown(f"- 🔋 **Fatigue (~14 jours)** : **{player_name}** ~{p1_fat.get('minutes_played', 0)} min ({p1_fat.get('matches', 0)} matchs) contre ~{p2_fat.get('minutes_played', 0)} min ({p2_fat.get('matches', 0)}) pour **{opp_name}** — _{lineup_note}, durées estimées depuis les scores_")
                        else:
                            st.markdown(f"- ⚔️ **Face-à-face (H2H)** : **{player_name}** a gagné **{p2_h2h}** fois contre **{p1_h2h}** pour **{opp_name}**")
                            st.markdown(f"- 🔥 **Forme (~90 jours)** : **{player_name}** a {p2_form.get('win_pct', 50):.0f}% de victoires ({p2_form.get('matches', 0)} matchs) contre {p1_form.get('win_pct', 50):.0f}% pour **{opp_name}**")
                            st.markdown(f"- 🔋 **Fatigue (~14 jours)** : **{player_name}** ~{p2_fat.get('minutes_played', 0)} min ({p2_fat.get('matches', 0)} matchs) contre ~{p1_fat.get('minutes_played', 0)} min ({p1_fat.get('matches', 0)}) pour **{opp_name}** — _{lineup_note}, durées estimées depuis les scores_")

                        fs = match.get("feature_snapshot", {}) or {}
                        style_lbl, style_hint = _style_advantage_label(fs.get("style_advantage_score"))
                        clutch_lbl, clutch_hint = _clutch_label(fs.get("clutch_index_diff"))
                        inact_lbl, inact_hint = _inactivity_label(
                            fs.get("inactivity_decay_weight"),
                            fs.get("p1_days_since_last_match"),
                            fs.get("p2_days_since_last_match"),
                        )

                        st.markdown("---")
                        st.markdown("### 🧠 Signaux avancés")
                        st.markdown(f"- 🧩 **Style Match-up** : **{style_lbl}**")
                        st.caption(style_hint)
                        st.markdown(f"- 🎯 **Pressure / Clutch** : **{clutch_lbl}**")
                        st.caption(clutch_hint)
                        st.markdown(f"- 🧯 **Volatilité retour blessure / inactivité** : **{inact_lbl}**")
                        st.caption(inact_hint)
                        if fs.get("raw_p1_prob") is not None and fs.get("capped_p1_prob") is not None:
                            st.markdown("### 🧪 Audit décision")
                            st.markdown(
                                f"- Proba brute modèle: **{float(fs.get('raw_p1_prob'))*100:.1f}%** "
                                f"-> proba finale: **{float(fs.get('capped_p1_prob'))*100:.1f}%**"
                            )
                            caps = fs.get("caps_applied", [])
                            st.markdown(f"- Garde-fous activés: **{', '.join(caps) if caps else 'aucun'}**")
                        tf = match.get("top_features", []) or []
                        if tf:
                            st.markdown("### 📌 Top facteurs (local)")
                            for tfi in tf:
                                st.markdown(f"- **{tfi.get('feature')}** (val={tfi.get('value'):.3f})")
                
                mid = match.get("prematch_id") or f"row_{idx}"
                with c2:
                    st.markdown("**📝 Placer un pari virtuel**")
                    col_stake, col_odd = st.columns(2)
                    with col_stake:
                        stake = st.number_input("Mise (U)", min_value=0.1, max_value=100.0, value=1.0, step=0.5, key=f"stake_{mid}_p{p_num}")
                    with col_odd:
                        default_odd = float(odd_book) if odd_book >= 1.01 else 1.01
                        custom_odd = st.number_input("Cote réelle", min_value=1.01, max_value=100.0, value=default_odd, step=0.05, key=f"custom_odd_{mid}_p{p_num}")
                
                with c3:
                    st.markdown("<br><br>", unsafe_allow_html=True)
                    if st.button(f"Parier {stake} U sur {player_name}", key=f"bet_{mid}_p{p_num}"):
                        save_bet(match_name, player_name, custom_odd, stake)

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
    st.header("📈 Backtest: Simulation Historique (échantillon paramétrable)")
    st.markdown("""
    Cette section permet de rejouer le modèle sur des données passées pour valider son efficacité. 
    L'IA ne connait pas le vainqueur à l'avance et parie uniquement lorsqu'une **Value** (EV > 5%) est détectée.
    Le backtest est exécuté en **mode anti-fuite (no-leak)**: entraînement uniquement sur les données antérieures au tournoi simulé.

    Source utilisée: **TennisMyLife (`matches_recent`)**, identique au modèle "cerveau".
    """)
    
    col1, col2, col3 = st.columns(3)
    with col2:
        year_range = st.slider("Période (années)", min_value=2010, max_value=2026, value=(2026, 2026), step=1)
    tourney_df = get_backtest_tournaments(year_range[0], year_range[1])
    tourney_options = tourney_df["tourney_name"].tolist() if not tourney_df.empty else ["(aucun tournoi)"]
    with col1:
        tourney = st.selectbox("Tournoi à simuler", options=tourney_options)
    with col3:
        max_matches_bt = st.number_input("Taille max échantillon", min_value=100, max_value=50000, value=5000, step=100)
    ev_threshold_bt = st.slider(
        "Seuil EV minimum pour placer un pari (%)",
        min_value=1.0,
        max_value=15.0,
        value=5.0,
        step=0.5,
    )
    if not tourney_df.empty:
        st.caption(f"{len(tourney_options)} tournoi(s) disponible(s) sur la période sélectionnée.")
    else:
        st.warning("Aucun tournoi trouvé sur la période sélectionnée (source TennisMyLife).")
    random_sample_bt = st.checkbox("Échantillonnage aléatoire", value=True)
    sample_seed_bt = st.number_input("Seed", min_value=1, max_value=999999, value=42, step=1)
    bank_col1, bank_col2 = st.columns(2)
    with bank_col1:
        bankroll_start_bt = st.number_input(
            "Bankroll initiale (€)",
            min_value=100.0,
            max_value=100000.0,
            value=1000.0,
            step=100.0,
        )
    with bank_col2:
        stake_bt = st.number_input(
            "Mise fixe par pari (€)",
            min_value=5.0,
            max_value=5000.0,
            value=50.0,
            step=5.0,
        )
    ex1, ex2, ex3 = st.columns(3)
    with ex1:
        drawdown_limit_bt = st.slider("Limite drawdown (%)", min_value=5.0, max_value=80.0, value=35.0, step=1.0)
    with ex2:
        slippage_bt = st.slider("Slippage (%)", min_value=0.0, max_value=8.0, value=1.0, step=0.5)
    with ex3:
        max_stake_pct_bt = st.slider("Max stake (% bankroll)", min_value=1.0, max_value=30.0, value=10.0, step=1.0)
    ex4, ex5 = st.columns(2)
    with ex4:
        market_unavail_bt = st.slider("Indispo marché (%)", min_value=0.0, max_value=40.0, value=5.0, step=1.0)
    with ex5:
        capture_delay_bt = st.slider("Capture delay (%)", min_value=0.0, max_value=40.0, value=10.0, step=1.0)
    st.caption("Chaque lancement exécute automatiquement 2 variantes: mise fixe et mise adaptative.")

    st.markdown("### 🧭 Playbook opérationnel")
    st.markdown(
        """
        - **Filtre principal** : privilégier `EV >= 8%` en ATP 250/500, et `EV >= 10%` en Masters/Grand Chelem.
        - **Confiance IA** : éviter les prises si confiance **Faible** ; taille standard si **Forte**.
        - **Risque** : mise fixe recommandée entre `2%` et `5%` de bankroll max par pari.
        - **Discipline** : ne pas forcer de pari sans value, même sur un gros match.
        - **Suivi** : juger la stratégie sur un bloc de `200+ paris`, pas sur quelques jours.
        """
    )
        
    if st.button("Lancer la Simulation", disabled=(tourney == "(aucun tournoi)")):
        with st.spinner(f"Simulation du tournoi {tourney} en cours..."):
            sim_kwargs_common = dict(
                date="",
                tournament=tourney,
                return_data=True,
                year_start=year_range[0],
                year_end=year_range[1],
                max_matches=int(max_matches_bt),
                random_sample=bool(random_sample_bt),
                random_seed=int(sample_seed_bt),
                no_leak=True,
                min_value_threshold=float(ev_threshold_bt) / 100.0,
                base_stake=float(stake_bt),
            )
            sim_params = set(inspect.signature(simulate_day).parameters.keys())
            missing_new_params = [p for p in ["confidence_staking", "base_stake", "min_value_threshold", "no_leak"] if p not in sim_params]
            if missing_new_params:
                st.error(
                    "La version chargée du simulateur est obsolète pour la comparaison fixe/adaptative. "
                    "Relance l'application (déjà fait côté serveur), puis réessaie."
                )
                stats_fixed, match_results_fixed = None, []
                stats_adapt, match_results_adapt = None, []
                stats_k10, stats_k25 = None, None
            else:
                stats_fixed, match_results_fixed = simulate_day(
                    **sim_kwargs_common,
                    confidence_staking=False,
                    staking_policy="fixed",
                    bankroll_start=float(bankroll_start_bt),
                    drawdown_limit_pct=float(drawdown_limit_bt),
                    capture_delay_prob=float(capture_delay_bt) / 100.0,
                    market_unavailable_prob=float(market_unavail_bt) / 100.0,
                    slippage_pct=float(slippage_bt) / 100.0,
                    max_stake_pct_bankroll=float(max_stake_pct_bt),
                )
                stats_adapt, match_results_adapt = simulate_day(
                    **sim_kwargs_common,
                    confidence_staking=True,
                    staking_policy="adaptive",
                    bankroll_start=float(bankroll_start_bt),
                    drawdown_limit_pct=float(drawdown_limit_bt),
                    capture_delay_prob=float(capture_delay_bt) / 100.0,
                    market_unavailable_prob=float(market_unavail_bt) / 100.0,
                    slippage_pct=float(slippage_bt) / 100.0,
                    max_stake_pct_bankroll=float(max_stake_pct_bt),
                )
                stats_k10, _ = simulate_day(
                    **sim_kwargs_common,
                    confidence_staking=False,
                    staking_policy="kelly_0_1",
                    bankroll_start=float(bankroll_start_bt),
                    drawdown_limit_pct=float(drawdown_limit_bt),
                    capture_delay_prob=float(capture_delay_bt) / 100.0,
                    market_unavailable_prob=float(market_unavail_bt) / 100.0,
                    slippage_pct=float(slippage_bt) / 100.0,
                    max_stake_pct_bankroll=float(max_stake_pct_bt),
                )
                stats_k25, _ = simulate_day(
                    **sim_kwargs_common,
                    confidence_staking=False,
                    staking_policy="kelly_0_25",
                    bankroll_start=float(bankroll_start_bt),
                    drawdown_limit_pct=float(drawdown_limit_bt),
                    capture_delay_prob=float(capture_delay_bt) / 100.0,
                    market_unavailable_prob=float(market_unavail_bt) / 100.0,
                    slippage_pct=float(slippage_bt) / 100.0,
                    max_stake_pct_bankroll=float(max_stake_pct_bt),
                )
            
        if stats_fixed is None or not match_results_fixed:
            st.warning("Aucun match trouvé pour cette date et ce tournoi.")
        else:
            st.success("Simulation terminée !")
            if stats_fixed.get("no_leak"):
                st.caption(
                    f"No-leak actif: entraînement jusqu'au {stats_fixed.get('train_cutoff')} "
                    f"({stats_fixed.get('train_rows', 0)} lignes d'entraînement)."
                )

            def _summarize(stats_obj, label):
                total_matches = int(stats_obj.get("total_matches", 0) or 0)
                correct = int(stats_obj.get("correct_predictions", 0) or 0)
                bets = int(stats_obj.get("total_bets_placed", 0) or 0)
                profit = float(stats_obj.get("net_profit", 0.0) or 0.0)
                total_staked = float(stats_obj.get("total_staked", 0.0) or 0.0)
                if total_staked <= 0 and bets > 0:
                    total_staked = float(stake_bt) * bets
                acc = (correct / total_matches * 100.0) if total_matches > 0 else 0.0
                roi_staked = (profit / total_staked * 100.0) if total_staked > 0 else 0.0
                bankroll_final = float(bankroll_start_bt) + profit
                return {
                    "Stratégie": label,
                    "Matchs": total_matches,
                    "Précision (%)": round(acc, 2),
                    "Paris": bets,
                    "Mises totales (€)": round(total_staked, 2),
                    "Profit net (€)": round(profit, 2),
                    "ROI sur mises (%)": round(roi_staked, 2),
                    "Bankroll finale (€)": round(bankroll_final, 2),
                    "Max drawdown (%)": round(float(stats_obj.get("max_drawdown_pct", 0.0) or 0.0), 2),
                }

            fixed_summary = _summarize(stats_fixed, f"Fixe ({float(stake_bt):.0f}€)")
            adapt_summary = _summarize(stats_adapt, f"Adaptative (base {float(stake_bt):.0f}€)")
            k10_summary = _summarize(stats_k10 or stats_fixed, "Kelly 0.1x")
            k25_summary = _summarize(stats_k25 or stats_fixed, "Kelly 0.25x")

            st.subheader("Comparatif des stratégies")
            comp_df = pd.DataFrame([fixed_summary, adapt_summary, k10_summary, k25_summary])
            st.dataframe(comp_df, use_container_width=True)

            cfix, cadapt = st.columns(2)
            cfix.metric("Fixe - Profit net", f"{fixed_summary['Profit net (€)']:.2f} €", f"{fixed_summary['ROI sur mises (%)']:.2f}% ROI")
            cadapt.metric("Adaptative - Profit net", f"{adapt_summary['Profit net (€)']:.2f} €", f"{adapt_summary['ROI sur mises (%)']:.2f}% ROI")
            st.caption(
                f"Execution-aware: slippage={slippage_bt:.1f}% · indispo={market_unavail_bt:.1f}% · "
                f"capture delay={capture_delay_bt:.1f}% · max stake={max_stake_pct_bt:.1f}% bankroll · DD limit={drawdown_limit_bt:.1f}%"
            )
            
            st.markdown("---")
            st.subheader("Détail des paris (mise fixe)")
            df_sim = pd.DataFrame(match_results_fixed)
            
            def color_result(val):
                if val == "✅ GAGNÉ":
                    return 'background-color: lightgreen; color: black'
                elif val == "❌ PERDU":
                    return 'background-color: lightcoral; color: black'
                return ''
                
            st.dataframe(df_sim.style.map(color_result, subset=['Résultat']), use_container_width=True)

            st.subheader("Détail des paris (mise adaptative)")
            df_sim_adapt = pd.DataFrame(match_results_adapt)
            st.dataframe(df_sim_adapt.style.map(color_result, subset=['Résultat']), use_container_width=True)

with tab5:
    st.header("🧩 Projet annexe: Marchés alternatifs (ATP/WTA)")
    st.caption("Expérimentation hors backtest: value bets sur marchés non-vainqueur (jeux, tie-break, sets).")

    _csv_path_alt, _csv_mtime_alt = _prematch_csv_signature()
    alt_matches = get_latest_scraped_data(_csv_path_alt, _csv_mtime_alt, PROFILE_CACHE_SCHEMA)
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
                        save_bet(match_name, bet_label, float(mk["book_odd"]), float(stake_alt))

with tab3:
    st.header("💼 Mon Portefeuille de Paris")
    st.markdown("Suivez vos performances, filtrez vos paris et visualisez rapidement votre risque/rendement.")
    
    conn = sqlite3.connect('data/bettinghud.db')
    df_bets = pd.read_sql("SELECT * FROM user_bets ORDER BY id ASC", conn)
    conn.close()

    # Auto-update des résultats à l'ouverture de la page (avec anti-spam)
    # Exécution en arrière-plan pour ne pas bloquer le rendu de l'onglet portefeuille.
    auto_now = time.time()
    last_auto = st.session_state.get("portfolio_last_auto_results_ts", 0.0)
    auto_interval_sec = 300  # 5 minutes
    has_pending = not df_bets.empty and (df_bets["status"] == "En cours").any()
    if has_pending and (auto_now - float(last_auto) >= auto_interval_sec):
        st.session_state["portfolio_last_auto_results_ts"] = auto_now
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import asyncio; from scripts.scraper_results import ResultsScraper; asyncio.run(ResultsScraper().update_pending_bets())",
                ],
                cwd=os.getcwd(),
            )
            st.caption("Auto-résultats lancés en arrière-plan. Rafraîchis la page dans ~1 minute.")
        except Exception as e:
            st.caption(f"Auto-résultats au chargement: erreur ({e})")
    
    if df_bets.empty:
        st.info("Vous n'avez pas encore enregistré de paris. Allez dans l'onglet 'Live Tracker' pour trouver de la Value !")
    else:
        # Calculer le profit cumulé
        df_bets['cumulative_profit'] = df_bets['profit'].cumsum()
        
        # Statistiques
        total_profit = df_bets['profit'].sum()
        total_staked = df_bets['stake'].sum()
        roi = (total_profit / total_staked * 100) if total_staked > 0 else 0
        
        # Filtrer et compter les statuts
        nb_en_cours = len(df_bets[df_bets['status'] == 'En cours'])
        nb_gagnes = len(df_bets[df_bets['status'] == 'Gagné'])
        nb_perdus = len(df_bets[df_bets['status'] == 'Perdu'])
        
        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Paris totaux", len(df_bets))
        k2.metric("En cours", nb_en_cours)
        k3.metric("Gagnés", nb_gagnes)
        k4.metric("Perdus", nb_perdus)
        k5.metric("Profit net", f"{total_profit:.2f} U", f"{roi:.1f}% ROI")

        c1, c2 = st.columns([3, 1])
        with c1:
            st.subheader("📈 Évolution du profit cumulé")
            st.line_chart(df_bets.set_index('date')['cumulative_profit'], use_container_width=True)
        with c2:
            st.subheader("⚙️ Actions")
            st.metric("Mises totales", f"{total_staked:.1f} U")
            if st.button("🔄 Mettre à jour les résultats"):
                with st.spinner("Recherche des résultats en cours..."):
                    res_scraper = ResultsScraper()
                    updated = asyncio.run(res_scraper.update_pending_bets())
                st.success(f"{updated} pari(s) mis à jour")
                st.rerun()
        
        st.markdown("---")

        # Renverser l'ordre pour afficher les plus récents en premier
        df_bets_disp = df_bets.sort_values(by="id", ascending=False)

        f1, f2, f3 = st.columns(3)
        with f1:
            status_filter = st.selectbox("Filtre statut", ["Tous", "En cours", "Gagné", "Perdu"], key="pf_status")
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

# Bouton de rafraîchissement
if st.button("🔄 Rafraîchir les données"):
    get_latest_scraped_data.clear()
    st.rerun()

st.sidebar.header("Paramètres")
st.sidebar.slider("Seuil minimal de Value (EV %)", min_value=1.0, max_value=10.0, value=3.0, step=0.5)
st.sidebar.selectbox("Filtre Surface", ["Toutes", "Hard", "Clay", "Grass"])

st.sidebar.header("Automatisation & Scraper")
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