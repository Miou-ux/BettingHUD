import os
import sqlite3
from collections import deque
import re

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import accuracy_score, brier_score_loss, classification_report
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier


class TennisMLModel:
    def __init__(self, db_path="data/bettinghud.db"):
        self.db_path = db_path
        self.model = None
        self.model_clay = None
        self.model_segments = {}
        self.player_elo = {}
        self.player_surface_elo = {}
        self.player_name_elo = {}
        self.player_name_surface_elo = {}
        self.features = [
            "surface_encoded",
            "tournament_level_encoded",
            "rank_diff",
            "age_diff",
            "ht_diff",
            "points_diff",
            "elo_diff",
            "surface_elo_diff",
            "days_since_last_match_diff",
            "workload7_minutes_diff",
            "workload7_sets_diff",
            "momentum5_diff",
            "form90_surface_diff",
            "second_srv_ratio3_diff",
            "hold_surface_diff",
            "break_surface_diff",
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
        ]
        self.model_path = "models/xgb_model_tml_v1.pkl"
        self.feature_plot_path = "models/feature_importance_tml_v1.png"

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
    def _infer_style_cluster(ace_rate, serve_win_rate, break_rate):
        # heuristique simple et robuste à partir des stats historiques
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
        m = {"A": 1.0, "M": 2.0, "G": 3.0}
        return m.get(str(level or "A"), 1.0)

    def _build_elo_features(self, matches_df, base_elo=1500.0):
        ratings = {}
        ratings_surface = {}
        ratings_name = {}
        ratings_surface_name = {}
        w_pre, l_pre, w_s_pre, l_s_pre = [], [], [], []

        for row in matches_df.itertuples(index=False):
            wid = self._pid_key(row.winner_id)
            lid = self._pid_key(row.loser_id)
            wname = self._name_key(getattr(row, "winner_name", None))
            lname = self._name_key(getattr(row, "loser_name", None))
            if not wid or not lid:
                # fallback via canonical names if IDs are absent
                if wname and lname:
                    surface = str(row.surface)
                    rw = ratings_name.get(wname, base_elo)
                    rl = ratings_name.get(lname, base_elo)
                    sw = ratings_surface_name.get((wname, surface), base_elo)
                    sl = ratings_surface_name.get((lname, surface), base_elo)

                    w_pre.append(rw)
                    l_pre.append(rl)
                    w_s_pre.append(sw)
                    l_s_pre.append(sl)

                    level = str(getattr(row, "tourney_level", "A"))
                    k = 36.0 if level == "G" else 30.0 if level == "M" else 24.0 if level == "A" else 20.0
                    ks = 32.0 if level in ("G", "M") else 22.0

                    ew = self._expected_score(rw, rl)
                    ratings_name[wname] = rw + k * (1.0 - ew)
                    ratings_name[lname] = rl + k * (0.0 - (1.0 - ew))

                    esw = self._expected_score(sw, sl)
                    ratings_surface_name[(wname, surface)] = sw + ks * (1.0 - esw)
                    ratings_surface_name[(lname, surface)] = sl + ks * (0.0 - (1.0 - esw))
                    continue
                else:
                    w_pre.append(base_elo)
                    l_pre.append(base_elo)
                    w_s_pre.append(base_elo)
                    l_s_pre.append(base_elo)
                    continue
            surface = str(row.surface)

            rw = ratings.get(wid, base_elo)
            rl = ratings.get(lid, base_elo)
            sw = ratings_surface.get((wid, surface), base_elo)
            sl = ratings_surface.get((lid, surface), base_elo)

            w_pre.append(rw)
            l_pre.append(rl)
            w_s_pre.append(sw)
            l_s_pre.append(sl)

            level = str(getattr(row, "tourney_level", "A"))
            k = 36.0 if level == "G" else 30.0 if level == "M" else 24.0 if level == "A" else 20.0
            ks = 32.0 if level in ("G", "M") else 22.0

            ew = self._expected_score(rw, rl)
            ratings[wid] = rw + k * (1.0 - ew)
            ratings[lid] = rl + k * (0.0 - (1.0 - ew))

            esw = self._expected_score(sw, sl)
            ratings_surface[(wid, surface)] = sw + ks * (1.0 - esw)
            ratings_surface[(lid, surface)] = sl + ks * (0.0 - (1.0 - esw))

        self.player_elo = ratings
        self.player_surface_elo = ratings_surface
        self.player_name_elo = ratings_name
        self.player_name_surface_elo = ratings_surface_name
        return pd.Series(w_pre), pd.Series(l_pre), pd.Series(w_s_pre), pd.Series(l_s_pre)

    def _build_temporal_features(self, df):
        # per-player rolling history: (date, won, minutes, sets, second_srv_ratio)
        hist = {}
        # per-player/per-surface rolling history: (date, won, hold_rate, break_rate, surf_elo_pre)
        hist_surface = {}
        # per-player rolling style signals: (date, ace_rate, serve_win_rate, break_rate)
        hist_style = {}
        # per-player rolling clutch signals: (date, bp_saved_ratio, bp_conv_ratio, tb_won, tb_played)
        hist_clutch = {}
        last_date = {}

        # H2H directional stats
        pair_wins = {}
        pair_matches = {}
        # per-player performance vs opponent style cluster
        style_vs_wins = {}
        style_vs_matches = {}

        w_days_rest, l_days_rest = [], []
        w_work_mins7, l_work_mins7 = [], []
        w_work_sets7, l_work_sets7 = [], []
        w_momentum5, l_momentum5 = [], []
        w_form90_surface, l_form90_surface = [], []
        w_ssr3, l_ssr3 = [], []
        w_hold_surface, l_hold_surface = [], []
        w_break_surface, l_break_surface = [], []
        w_elo_surface_recent, l_elo_surface_recent = [], []
        w_h2h_ratio, l_h2h_ratio = [], []
        w_h2h_sig, l_h2h_sig = [], []
        w_style_adv, l_style_adv = [], []
        w_clutch_idx, l_clutch_idx = [], []
        w_inact_decay, l_inact_decay = [], []
        w_home, l_home = [], []
        altitude_list, indoor_list = [], []

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
                w_work_mins7.append(0.0); l_work_mins7.append(0.0)
                w_work_sets7.append(0.0); l_work_sets7.append(0.0)
                w_momentum5.append(0.5); l_momentum5.append(0.5)
                w_form90_surface.append(0.5); l_form90_surface.append(0.5)
                w_ssr3.append(0.5); l_ssr3.append(0.5)
                w_hold_surface.append(0.75); l_hold_surface.append(0.75)
                w_break_surface.append(0.20); l_break_surface.append(0.20)
                w_elo_surface_recent.append(0.0); l_elo_surface_recent.append(0.0)
                w_h2h_ratio.append(0.5); l_h2h_ratio.append(0.5)
                w_h2h_sig.append(0.0); l_h2h_sig.append(0.0)
                w_style_adv.append(0.5); l_style_adv.append(0.5)
                w_clutch_idx.append(0.5); l_clutch_idx.append(0.5)
                w_inact_decay.append(1.0); l_inact_decay.append(1.0)
                w_home.append(0.0); l_home.append(0.0)
                altitude_list.append(0.0); indoor_list.append(0.0)
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

            # rolling history helpers
            for pid, is_winner, out_work_mins, out_work_sets, out_mom, out_ssr in (
                (wid, True, w_work_mins7, w_work_sets7, w_momentum5, w_ssr3),
                (lid, False, l_work_mins7, l_work_sets7, l_momentum5, l_ssr3),
            ):
                dq = hist.get(pid)
                if dq is None:
                    dq = deque()
                    hist[pid] = dq

                # workload 7d
                cutoff = dt - td7
                work_items = [x for x in dq if x[0] >= cutoff]
                out_work_mins.append(float(sum(x[2] for x in work_items)))
                out_work_sets.append(float(sum(x[3] for x in work_items)))

                # momentum 5 latest (weighted recent higher)
                last5 = list(dq)[-5:]
                if not last5:
                    out_mom.append(0.5)
                else:
                    # weights ascending by recency: oldest->newest
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

            hist[wid].append((dt, True, mins, sets, w_ssr_current))
            hist[lid].append((dt, False, mins, sets, l_ssr_current))
            hist_surface[(wid, surface)].append((dt, True, w_hold, w_break, float(row.winner_surf_elo_pre)))
            hist_surface[(lid, surface)].append((dt, False, l_hold, l_break, float(row.loser_surf_elo_pre)))

            # update style rolling stats
            w_ace_rate = self._safe_ratio(row.w_ace, row.w_svpt, default=0.06)
            l_ace_rate = self._safe_ratio(row.l_ace, row.l_svpt, default=0.06)
            w_serve_win = self._safe_ratio((float(row.w_1stWon) if pd.notna(row.w_1stWon) else 0.0) + (float(row.w_2ndWon) if pd.notna(row.w_2ndWon) else 0.0), row.w_svpt, default=0.60)
            l_serve_win = self._safe_ratio((float(row.l_1stWon) if pd.notna(row.l_1stWon) else 0.0) + (float(row.l_2ndWon) if pd.notna(row.l_2ndWon) else 0.0), row.l_svpt, default=0.60)
            hist_style.setdefault(wid, deque()).append((dt, w_ace_rate, w_serve_win, w_break))
            hist_style.setdefault(lid, deque()).append((dt, l_ace_rate, l_serve_win, l_break))

            # update clutch rolling stats
            w_bp_saved = self._safe_ratio(row.w_bpSaved, row.w_bpFaced, default=np.nan)
            l_bp_saved = self._safe_ratio(row.l_bpSaved, row.l_bpFaced, default=np.nan)
            w_bp_conv = self._safe_ratio(loser_breaks_suffered, row.l_bpFaced, default=np.nan)
            l_bp_conv = self._safe_ratio(winner_breaks_suffered, row.w_bpFaced, default=np.nan)
            hist_clutch.setdefault(wid, deque()).append((dt, w_bp_saved, w_bp_conv, w_tb_won, tb_played))
            hist_clutch.setdefault(lid, deque()).append((dt, l_bp_saved, l_bp_conv, l_tb_won, tb_played))

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
            "winner_work_mins7": pd.Series(w_work_mins7),
            "loser_work_mins7": pd.Series(l_work_mins7),
            "winner_work_sets7": pd.Series(w_work_sets7),
            "loser_work_sets7": pd.Series(l_work_sets7),
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
            "winner_inactivity_decay": pd.Series(w_inact_decay),
            "loser_inactivity_decay": pd.Series(l_inact_decay),
            "winner_home": pd.Series(w_home),
            "loser_home": pd.Series(l_home),
            "altitude": pd.Series(altitude_list),
            "indoor": pd.Series(indoor_list),
        }

    def prepare_data(self):
        print("Chargement et préparation des données...")
        conn = sqlite3.connect(self.db_path)
        try:
            df = pd.read_sql(
                "SELECT * FROM matches_recent "
                "WHERE source='tennismylife' AND CAST(substr(tourney_date,1,4) AS INTEGER) >= 2010",
                conn,
            )
            print(f"TennisMyLife rows: {len(df)}")
        except Exception as e:
            conn.close()
            raise RuntimeError(f"Table matches_recent indisponible. Lance d'abord sync_tml_recent.py ({e})")
        conn.close()
        if df.empty:
            raise RuntimeError("Aucune donnée TML disponible pour l'entraînement.")

        df = df.dropna(subset=["winner_rank", "loser_rank", "winner_age", "loser_age", "surface"])
        df["tourney_date"] = pd.to_datetime(df["tourney_date"], errors="coerce")
        df = df.dropna(subset=["tourney_date"]).sort_values("tourney_date").reset_index(drop=True)

        # Elo features (pre-match, no leakage)
        w_elo, l_elo, w_s_elo, l_s_elo = self._build_elo_features(df)
        df["winner_elo_pre"] = w_elo
        df["loser_elo_pre"] = l_elo
        df["winner_surf_elo_pre"] = w_s_elo
        df["loser_surf_elo_pre"] = l_s_elo

        temporal = self._build_temporal_features(df)
        for k, v in temporal.items():
            df[k] = v

        # Create oriented binary dataset (winner as p1 + loser as p1)
        df1 = pd.DataFrame()
        df1["surface"] = df["surface"]
        df1["tourney_date"] = df["tourney_date"]
        df1["tournament_level_encoded"] = df["tourney_level"].fillna("A").map(self._encode_tourney_level)
        df1["rank_diff"] = df["winner_rank"] - df["loser_rank"]
        df1["age_diff"] = df["winner_age"] - df["loser_age"]
        df1["ht_diff"] = df["winner_ht"] - df["loser_ht"]
        df1["points_diff"] = df["winner_rank_points"] - df["loser_rank_points"]
        df1["elo_diff"] = df["winner_elo_pre"] - df["loser_elo_pre"]
        df1["surface_elo_diff"] = df["winner_surf_elo_pre"] - df["loser_surf_elo_pre"]
        df1["days_since_last_match_diff"] = df["winner_days_rest"] - df["loser_days_rest"]
        df1["workload7_minutes_diff"] = df["winner_work_mins7"] - df["loser_work_mins7"]
        df1["workload7_sets_diff"] = df["winner_work_sets7"] - df["loser_work_sets7"]
        df1["momentum5_diff"] = df["winner_momentum5"] - df["loser_momentum5"]
        df1["form90_surface_diff"] = df["winner_form90_surface"] - df["loser_form90_surface"]
        df1["second_srv_ratio3_diff"] = df["winner_ssr3"] - df["loser_ssr3"]
        df1["hold_surface_diff"] = df["winner_hold_surface"] - df["loser_hold_surface"]
        df1["break_surface_diff"] = df["winner_break_surface"] - df["loser_break_surface"]
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
        df1["target"] = 1

        df2 = pd.DataFrame()
        df2["surface"] = df["surface"]
        df2["tourney_date"] = df["tourney_date"]
        df2["tournament_level_encoded"] = df["tourney_level"].fillna("A").map(self._encode_tourney_level)
        df2["rank_diff"] = df["loser_rank"] - df["winner_rank"]
        df2["age_diff"] = df["loser_age"] - df["winner_age"]
        df2["ht_diff"] = df["loser_ht"] - df["winner_ht"]
        df2["points_diff"] = df["loser_rank_points"] - df["winner_rank_points"]
        df2["elo_diff"] = df["loser_elo_pre"] - df["winner_elo_pre"]
        df2["surface_elo_diff"] = df["loser_surf_elo_pre"] - df["winner_surf_elo_pre"]
        df2["days_since_last_match_diff"] = df["loser_days_rest"] - df["winner_days_rest"]
        df2["workload7_minutes_diff"] = df["loser_work_mins7"] - df["winner_work_mins7"]
        df2["workload7_sets_diff"] = df["loser_work_sets7"] - df["winner_work_sets7"]
        df2["momentum5_diff"] = df["loser_momentum5"] - df["winner_momentum5"]
        df2["form90_surface_diff"] = df["loser_form90_surface"] - df["winner_form90_surface"]
        df2["second_srv_ratio3_diff"] = df["loser_ssr3"] - df["winner_ssr3"]
        df2["hold_surface_diff"] = df["loser_hold_surface"] - df["winner_hold_surface"]
        df2["break_surface_diff"] = df["loser_break_surface"] - df["winner_break_surface"]
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

        print("Entraînement XGBoost...")
        base_model = XGBClassifier(
            n_estimators=500,
            max_depth=6,
            learning_rate=0.03,
            subsample=0.9,
            colsample_bytree=0.9,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
            n_jobs=4,
        )
        base_model.fit(X_train, y_train)

        print("Calibration des probabilités (sigmoid, TimeSeriesSplit=3)...")
        tscv = TimeSeriesSplit(n_splits=3)
        calibrated = CalibratedClassifierCV(base_model, method="sigmoid", cv=tscv)
        calibrated.fit(X_train, y_train)
        self.model = calibrated

        # Calibration dédiée Clay pour limiter les biais cross-surface en prédiction sur terre.
        self.model_clay = None
        clay_mask = X_train["surface_encoded"] == 1
        if clay_mask.sum() > 500:
            X_train_clay = X_train[clay_mask]
            y_train_clay = y_train[clay_mask]
            if len(np.unique(y_train_clay)) >= 2:
                print("Calibration spécifique Clay (sigmoid, TimeSeriesSplit=3)...")
                tscv_clay = TimeSeriesSplit(n_splits=3)
                calibrated_clay = CalibratedClassifierCV(base_model, method="sigmoid", cv=tscv_clay)
                calibrated_clay.fit(X_train_clay, y_train_clay)
                self.model_clay = calibrated_clay

        # Calibration segmentée surface x niveau
        self.model_segments = {}
        seg_defs = [
            ("Hard_G", 0.0, 3.0),
            ("Hard_M", 0.0, 2.0),
            ("Clay_G", 1.0, 3.0),
            ("Clay_M", 1.0, 2.0),
            ("Clay_A", 1.0, 1.0),
        ]
        for seg_name, surf_code, lvl_code in seg_defs:
            seg_mask = (X_train["surface_encoded"] == surf_code) & (X_train["tournament_level_encoded"] == lvl_code)
            if seg_mask.sum() < 600:
                continue
            y_seg = y_train[seg_mask]
            if len(np.unique(y_seg)) < 2:
                continue
            seg_model = CalibratedClassifierCV(base_model, method="sigmoid", cv=TimeSeriesSplit(n_splits=3))
            seg_model.fit(X_train[seg_mask], y_seg)
            self.model_segments[seg_name] = seg_model

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
        plt.title("Feature Importance - XGBoost Tennis v3")
        plt.xlabel("Importance")
        plt.tight_layout()
        plt.savefig(self.feature_plot_path, dpi=140)
        plt.close()
        print(f"Graphique feature importance sauvegardé: {self.feature_plot_path}")

        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        bundle = {
            "model": self.model,
            "model_clay": self.model_clay,
            "model_segments": self.model_segments,
            "player_elo": self.player_elo,
            "player_surface_elo": self.player_surface_elo,
            "player_name_elo": self.player_name_elo,
            "player_name_surface_elo": self.player_name_surface_elo,
            "features": self.features,
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
            self.player_elo = loaded.get("player_elo", {})
            self.player_surface_elo = loaded.get("player_surface_elo", {})
            self.player_name_elo = loaded.get("player_name_elo", {})
            self.player_name_surface_elo = loaded.get("player_name_surface_elo", {})
            self.features = loaded.get("features", self.features)
        else:
            self.model = loaded
            self.model_clay = None

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
        p1_workload7_minutes=None,
        p2_workload7_minutes=None,
        p1_workload7_sets=None,
        p2_workload7_sets=None,
        p1_second_srv_ratio3=None,
        p2_second_srv_ratio3=None,
        p1_form_surface_win_pct_90=None,
        p2_form_surface_win_pct_90=None,
        p1_hold_surface=None,
        p2_hold_surface=None,
        p1_break_surface=None,
        p2_break_surface=None,
        p1_elo_surface_recent=None,
        p2_elo_surface_recent=None,
        p1_style_advantage_score=None,
        p1_clutch_index=None,
        p2_clutch_index=None,
        tournament_level=None,
    ):
        self._load_bundle_if_needed()

        surface_map = {"Hard": 0, "Clay": 1, "Grass": 2, "Carpet": 3}
        surface_encoded = surface_map.get(surface, 0)

        base_elo = 1500.0
        p1k = self._pid_key(p1_id)
        p2k = self._pid_key(p2_id)
        p1n = self._name_key(p1_name)
        p2n = self._name_key(p2_name)
        if p1k:
            p1_global_elo = self.player_elo.get(p1k, base_elo)
            p1_surface_elo = self.player_surface_elo.get((p1k, surface), p1_global_elo)
        elif p1n:
            p1_global_elo = self.player_name_elo.get(p1n, base_elo)
            p1_surface_elo = self.player_name_surface_elo.get((p1n, surface), p1_global_elo)
        else:
            p1_global_elo = base_elo
            p1_surface_elo = base_elo
        if p2k:
            p2_global_elo = self.player_elo.get(p2k, base_elo)
            p2_surface_elo = self.player_surface_elo.get((p2k, surface), p2_global_elo)
        elif p2n:
            p2_global_elo = self.player_name_elo.get(p2n, base_elo)
            p2_surface_elo = self.player_name_surface_elo.get((p2n, surface), p2_global_elo)
        else:
            p2_global_elo = base_elo
            p2_surface_elo = base_elo

        p1_form = 0.5 if p1_form_win_pct_90 is None else float(p1_form_win_pct_90) / 100.0
        p2_form = 0.5 if p2_form_win_pct_90 is None else float(p2_form_win_pct_90) / 100.0

        p1_fat_minutes14 = 0.0 if p1_fatigue_minutes_14 is None else float(p1_fatigue_minutes_14)
        p2_fat_minutes14 = 0.0 if p2_fatigue_minutes_14 is None else float(p2_fatigue_minutes_14)

        p1_fat_matches14 = 0.0 if p1_fatigue_matches_14 is None else float(p1_fatigue_matches_14)
        p2_fat_matches14 = 0.0 if p2_fatigue_matches_14 is None else float(p2_fatigue_matches_14)

        # Live fallback: approx workload7 from fatigue14 if explicit workload absent
        p1_work7_minutes = p1_fat_minutes14 * 0.5 if p1_workload7_minutes is None else float(p1_workload7_minutes)
        p2_work7_minutes = p2_fat_minutes14 * 0.5 if p2_workload7_minutes is None else float(p2_workload7_minutes)
        p1_work7_sets = p1_fat_matches14 * 2.0 if p1_workload7_sets is None else float(p1_workload7_sets)
        p2_work7_sets = p2_fat_matches14 * 2.0 if p2_workload7_sets is None else float(p2_workload7_sets)

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

        # garde-fou: si Elo+H2H peu informatifs, limiter la domination fatigue/workload
        weak_elo_h2h = (
            total_h2h == 0
            and abs(p1_global_elo - p2_global_elo) < 35
            and abs(p1_surface_elo - p2_surface_elo) < 45
        )
        if weak_elo_h2h:
            p1_work7_minutes = float(np.clip(p1_work7_minutes, 0.0, 420.0))
            p2_work7_minutes = float(np.clip(p2_work7_minutes, 0.0, 420.0))
            p1_work7_sets = float(np.clip(p1_work7_sets, 0.0, 12.0))
            p2_work7_sets = float(np.clip(p2_work7_sets, 0.0, 12.0))

        X_new = pd.DataFrame([
            {
                "surface_encoded": surface_encoded,
                "tournament_level_encoded": self._encode_tourney_level(tournament_level or self._infer_tourney_level_from_name(tournament_name)),
                "rank_diff": float(p1_rank) - float(p2_rank),
                "age_diff": float(p1_age) - float(p2_age),
                "ht_diff": float(p1_ht) - float(p2_ht),
                "points_diff": float(p1_pts) - float(p2_pts),
                "elo_diff": p1_global_elo - p2_global_elo,
                "surface_elo_diff": p1_surface_elo - p2_surface_elo,
                "days_since_last_match_diff": p1_days - p2_days,
                "workload7_minutes_diff": p1_work7_minutes - p2_work7_minutes,
                "workload7_sets_diff": p1_work7_sets - p2_work7_sets,
                "momentum5_diff": p1_form - p2_form,
                "form90_surface_diff": p1_form_s - p2_form_s,
                "second_srv_ratio3_diff": p1_ssr3 - p2_ssr3,
                "hold_surface_diff": p1_hold_s - p2_hold_s,
                "break_surface_diff": p1_break_s - p2_break_s,
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
            }
        ])

        # Calibration segmentée (surface x niveau) prioritaire, puis Clay, puis globale.
        using_clay_calibration = (surface == "Clay" and self.model_clay is not None)
        lvl = tournament_level or self._infer_tourney_level_from_name(tournament_name)
        seg_key = f"{surface}_{lvl}"
        model_for_pred = self.model_segments.get(seg_key)
        calibration_used = f"Segment:{seg_key}" if model_for_pred is not None else ("Clay" if using_clay_calibration else "Globale")
        if model_for_pred is None:
            model_for_pred = self.model_clay if using_clay_calibration else self.model
        probs = model_for_pred.predict_proba(X_new[self.features])[0]
        p1_prob = float(probs[1])
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

        # garde-fou supplémentaire quand workload extrême sans support Elo/H2H
        workload_gap = abs((p1_work7_minutes - p2_work7_minutes)) + 15.0 * abs((p1_work7_sets - p2_work7_sets))
        if weak_elo_h2h and workload_gap > 220:
            p1_prob = max(0.24, min(0.76, p1_prob))
            caps_applied.append("cap_workload_guardrail")

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
                "weak_elo_h2h_guardrail": 1.0 if weak_elo_h2h else 0.0,
                "raw_p1_prob": raw_p1_prob,
                "capped_p1_prob": p1_prob,
                "caps_applied": caps_applied,
                "p1_days_since_last_match": p1_days,
                "p2_days_since_last_match": p2_days,
            },
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
