"""
Engine de stats joueurs avec source par tour explicite :

  - ATP -> TennisMyLife (table `matches_recent` filtrée `source='tennismylife'`).
           Player IDs : codes alphanumériques ATP officiels (ex. 'B0BI', 'R485').

  - WTA -> Jeff Sackmann (`wta_matches` + `rankings_wta_current` + `players_wta`
           si présente, sinon noms extraits des matchs).
           Player IDs : entiers Sackmann (ex. 214544 = Sabalenka).

Aucun fallback Sackmann sur l'ATP : si un joueur ATP n'est pas dans TML, il
retombe sur les valeurs par défaut. Aucun fallback TML sur la WTA non plus.

Le tour est déterminé par `tour_hint='ATP'|'WTA'`. Si le hint est absent, on
essaie d'inférer (lookup nom ATP d'abord, sinon WTA).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from scripts.player_identity import canonical_name, to_lastname_initial

def _canonical_player_index_key(name: str) -> str:
    """Clé alignée sur les index ATP/WTA (`_build_*_indexes`).

    Les flux peuvent envoyer « Prénom Nom » (Flashscore) ; `to_lastname_initial`
    ramène au même libellé que les noms Sackmann pour le match WTA/ATP.
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    return canonical_name(to_lastname_initial(raw)) or canonical_name(raw)
from scripts.surface_speed import lookup_surface_speed


def _to_int_or_none(x) -> Optional[int]:
    if x is None or x is pd.NA:
        return None
    try:
        if isinstance(x, float) and pd.isna(x):
            return None
    except Exception:
        pass
    try:
        s = str(x).strip()
        if s.upper().startswith("WTA::"):
            s = s.split("::", 1)[1]
        elif s.upper().startswith("ATP::"):
            s = s.split("::", 1)[1]
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _norm_pid_key(pid) -> Optional[str]:
    """Normalise un player_id pour usage en clé : str sans espace."""
    if pid is None:
        return None
    s = str(pid).strip()
    if not s or s.lower() == "nan":
        return None
    if s.upper().startswith("ATP::") or s.upper().startswith("WTA::"):
        s = s.split("::", 1)[1].strip()
    if not s:
        return None
    return s


def _normalize_date_display(val: Any) -> Optional[str]:
    """Retourne une date `YYYY-MM-DD` pour affichage, ou None si inconnu."""
    if val is None:
        return None
    try:
        if isinstance(val, float) and pd.isna(val):
            return None
    except Exception:
        pass
    try:
        if pd.isna(val):  # type: ignore[arg-type]
            return None
    except Exception:
        pass
    s = str(val).strip()
    if not s or s.lower() in {"nan", "nat", "<na>"}:
        return None
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d")
    ts = pd.to_datetime(val, errors="coerce")
    try:
        if ts is pd.NaT or pd.isna(ts):
            pass
        else:
            return str(ts.date())
    except Exception:
        pass
    ds = "".join(ch for ch in s if ch.isdigit())
    if len(ds) >= 8:
        ds8 = ds[:8]
        y, mo, d = ds8[:4], ds8[4:6], ds8[6:8]
        if 1900 <= int(y) <= 2100:
            try:
                return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            except Exception:
                return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


def _stats_reference_date_from_row(row: Any) -> Optional[str]:
    """Date du dernier match (colonne tourney_date) pour contextualiser rang/points."""
    if row is None:
        return None
    try:
        if hasattr(row, "index") and "tourney_date" in getattr(row, "index", []) and hasattr(row, "get"):
            td = row["tourney_date"]
        elif hasattr(row, "get"):
            td = row.get("tourney_date")
        elif hasattr(row, "__getitem__"):
            td = row["tourney_date"]
        else:
            return None
    except Exception:
        return None
    return _normalize_date_display(td)


_STATS_CACHE_SHAPE_VER = 2


def _strip_stats_cache_meta(out: Dict[str, Any]) -> Dict[str, Any]:
    z = dict(out)
    z.pop("_stats_cache_shape", None)
    return z


class TennisStatsEngine:
    def __init__(self, db_path: str = "data/bettinghud.db"):
        self.db_path = db_path

        # Sources principales :
        self.matches_atp_df: pd.DataFrame = pd.DataFrame()
        self.matches_wta_df: Optional[pd.DataFrame] = None
        self._wta_lock = threading.Lock()

        # Indexes inverse pid -> liste d'indices (triés par tourney_date croissant).
        self._atp_winner_idx: Dict[str, list] = {}
        self._atp_loser_idx: Dict[str, list] = {}
        self._wta_winner_idx: Dict[int, list] = {}
        self._wta_loser_idx: Dict[int, list] = {}

        # Résolution nom canonique -> pid par tour (utilisée par get_player_id_meta).
        self._atp_name_to_id: Dict[str, str] = {}
        self._wta_name_to_id: Dict[str, int] = {}

        # Présence de tables Sackmann WTA (cache).
        self._has_wta_table: Optional[bool] = None
        self._has_rankings_wta_current: Optional[bool] = None

        # Cache résultat get_player_stats keyed by (pid, tour_upper).
        self._stats_result_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._stats_cache_db_mtime: Optional[float] = None
        self._stats_cache_max_size = 4096

        self._load_data()
        self._build_atp_indexes()
        # WTA lazy load : on ne charge wta_matches que quand un joueur WTA est demandé.

    # ---------------------------------------------------------------------
    # Chargement des données
    # ---------------------------------------------------------------------
    def _connect(self, *, timeout: float = 60.0) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-128000")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA temp_store=MEMORY")
        except Exception:
            pass
        return conn

    def _load_data(self):
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(
                f"Base de données {self.db_path} non trouvée. "
                "Exécutez d'abord scripts/sync_tml_recent.py et scripts/ingest_sackmann_wta.py."
            )
        conn = self._connect()
        try:
            try:
                self.matches_atp_df = pd.read_sql(
                    "SELECT * FROM matches_recent WHERE source='tennismylife'",
                    conn,
                )
            except Exception:
                self.matches_atp_df = pd.DataFrame()
        finally:
            conn.close()

        if not self.matches_atp_df.empty:
            self.matches_atp_df["tourney_date"] = pd.to_datetime(
                self.matches_atp_df["tourney_date"], errors="coerce"
            )
            self.matches_atp_df = (
                self.matches_atp_df.dropna(subset=["tourney_date"]).reset_index(drop=True)
            )

    def _wta_table_exists(self) -> bool:
        if self._has_wta_table is not None:
            return bool(self._has_wta_table)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wta_matches' LIMIT 1"
            ).fetchone()
            self._has_wta_table = row is not None
        except Exception:
            self._has_wta_table = False
        finally:
            conn.close()
        return bool(self._has_wta_table)

    def _rankings_wta_current_exists(self) -> bool:
        if self._has_rankings_wta_current is not None:
            return bool(self._has_rankings_wta_current)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rankings_wta_current' LIMIT 1"
            ).fetchone()
            self._has_rankings_wta_current = row is not None
        except Exception:
            self._has_rankings_wta_current = False
        finally:
            conn.close()
        return bool(self._has_rankings_wta_current)

    def _load_wta_matches(self) -> pd.DataFrame:
        """Lazy-load `wta_matches` (Sackmann). Retourne un DataFrame éventuellement vide."""
        if self.matches_wta_df is not None:
            return self.matches_wta_df
        with self._wta_lock:
            if self.matches_wta_df is not None:
                return self.matches_wta_df
            if not self._wta_table_exists():
                self.matches_wta_df = pd.DataFrame()
                return self.matches_wta_df
            conn = self._connect()
            try:
                df = pd.read_sql("SELECT * FROM wta_matches", conn)
            except Exception:
                df = pd.DataFrame()
            finally:
                conn.close()
            if not df.empty:
                df["tourney_date"] = pd.to_datetime(df["tourney_date"], errors="coerce")
                df = df.dropna(subset=["tourney_date"]).reset_index(drop=True)
            self.matches_wta_df = df
            self._build_wta_indexes()
            return self.matches_wta_df

    # ---------------------------------------------------------------------
    # Construction des indexes
    # ---------------------------------------------------------------------
    def _build_atp_indexes(self):
        """ATP : index inverse pid (str) -> rows + canonical_name -> pid."""
        df = self.matches_atp_df
        if df is None or df.empty:
            return
        winner_idx: Dict[str, list] = {}
        loser_idx: Dict[str, list] = {}
        name_to_id: Dict[str, str] = {}

        order = df["tourney_date"].argsort(kind="mergesort")
        winner_id_arr = df["winner_id"].array
        loser_id_arr = df["loser_id"].array
        winner_name_arr = df["winner_name"].array
        loser_name_arr = df["loser_name"].array

        for pos in order:
            wid = winner_id_arr[pos]
            lid = loser_id_arr[pos]
            wn = winner_name_arr[pos]
            ln = loser_name_arr[pos]
            wid_s = _norm_pid_key(wid)
            lid_s = _norm_pid_key(lid)
            if wid_s:
                winner_idx.setdefault(wid_s, []).append(int(pos))
            if lid_s:
                loser_idx.setdefault(lid_s, []).append(int(pos))

        for pos in order[::-1]:
            wid = winner_id_arr[pos]
            lid = loser_id_arr[pos]
            wn = winner_name_arr[pos]
            ln = loser_name_arr[pos]
            wid_s = _norm_pid_key(wid)
            lid_s = _norm_pid_key(lid)
            if wid_s and wn:
                cn = canonical_name(to_lastname_initial(str(wn)))
                if cn and cn not in name_to_id:
                    name_to_id[cn] = wid_s
            if lid_s and ln:
                cn = canonical_name(to_lastname_initial(str(ln)))
                if cn and cn not in name_to_id:
                    name_to_id[cn] = lid_s

        self._atp_winner_idx = winner_idx
        self._atp_loser_idx = loser_idx
        self._atp_name_to_id = name_to_id

    def _build_wta_indexes(self):
        df = self.matches_wta_df
        if df is None or df.empty:
            return
        winner_idx: Dict[int, list] = {}
        loser_idx: Dict[int, list] = {}
        name_to_id: Dict[str, int] = {}

        order = df["tourney_date"].argsort(kind="mergesort")
        winner_id_arr = df["winner_id"].array
        loser_id_arr = df["loser_id"].array
        winner_name_arr = df["winner_name"].array
        loser_name_arr = df["loser_name"].array

        for pos in order:
            wid = _to_int_or_none(winner_id_arr[pos])
            lid = _to_int_or_none(loser_id_arr[pos])
            wn = winner_name_arr[pos]
            ln = loser_name_arr[pos]
            if wid is not None:
                winner_idx.setdefault(wid, []).append(int(pos))
            if lid is not None:
                loser_idx.setdefault(lid, []).append(int(pos))

        # Homonymes « Nom I. » : en parcourant du **plus récent** au plus ancien,
        # la première affectation gagne -> plutôt le joueur encore actif (ex. Karolina
        # vs Kristyna Pliskova, même clé `pliskova k`).
        for pos in order[::-1]:
            wid = _to_int_or_none(winner_id_arr[pos])
            lid = _to_int_or_none(loser_id_arr[pos])
            wn = winner_name_arr[pos]
            ln = loser_name_arr[pos]
            if wid is not None and wn:
                cn = canonical_name(to_lastname_initial(str(wn)))
                if cn and cn not in name_to_id:
                    name_to_id[cn] = wid
            if lid is not None and ln:
                cn = canonical_name(to_lastname_initial(str(ln)))
                if cn and cn not in name_to_id:
                    name_to_id[cn] = lid

        self._wta_winner_idx = winner_idx
        self._wta_loser_idx = loser_idx
        self._wta_name_to_id = name_to_id

    # ---------------------------------------------------------------------
    # Cache invalidation
    # ---------------------------------------------------------------------
    def _db_mtime(self) -> float:
        try:
            return os.path.getmtime(self.db_path)
        except OSError:
            return 0.0

    def _maybe_invalidate_stats_cache(self):
        cur = self._db_mtime()
        if self._stats_cache_db_mtime is None:
            self._stats_cache_db_mtime = cur
            return
        if cur != self._stats_cache_db_mtime:
            self._stats_result_cache.clear()
            self._stats_cache_db_mtime = cur

    def _cache_stats_result(self, key, value):
        if len(self._stats_result_cache) >= self._stats_cache_max_size:
            drop = max(1, self._stats_cache_max_size // 4)
            for _ in range(drop):
                try:
                    self._stats_result_cache.pop(next(iter(self._stats_result_cache)))
                except StopIteration:
                    break
        self._stats_result_cache[key] = dict(value)
        self._stats_result_cache[key]["_stats_cache_shape"] = _STATS_CACHE_SHAPE_VER

    # ---------------------------------------------------------------------
    # Résolution d'identité par tour
    # ---------------------------------------------------------------------
    def get_player_id(self, player_name, source_name="flashscore", source_url=None, tour_hint=None):
        meta = self.get_player_id_meta(
            player_name,
            source_name=source_name,
            source_url=source_url,
            tour_hint=tour_hint,
        )
        return meta.get("player_id")

    def get_player_id_meta(self, player_name, source_name="flashscore", source_url=None, tour_hint=None):
        """Renvoie {'player_id', 'method', 'confidence', 'reason', 'tour'} pour le nom donné.

        Priorité :
          1) Index par tour explicite (`tour_hint`) construit depuis matches_recent / wta_matches.
          2) Si tour_hint manquant, on tente ATP puis WTA dans cet ordre.
          3) Aucun match -> 'unresolved' (pas de fallback Sackmann ATP).

        La source ATP officielle est `matches_recent` (TennisMyLife). La WTA passe
        exclusivement par `wta_matches` (Sackmann).
        """
        cn = _canonical_player_index_key(str(player_name or ""))
        th = (tour_hint or "").strip().upper()

        if cn:
            # Préchargement WTA si on doit chercher du côté femme.
            if th == "WTA" and not self._wta_name_to_id:
                self._load_wta_matches()

            if th == "WTA":
                pid = self._wta_name_to_id.get(cn)
                if pid is not None:
                    return {
                        "player_id": f"WTA::{pid}",
                        "method": "wta_matches_name",
                        "confidence": 0.99,
                        "reason": "match_wta_canonical",
                        "tour": "WTA",
                    }
            elif th == "ATP":
                pid = self._atp_name_to_id.get(cn)
                if pid:
                    return {
                        "player_id": f"ATP::{pid}",
                        "method": "matches_recent_name",
                        "confidence": 0.99,
                        "reason": "match_atp_canonical",
                        "tour": "ATP",
                    }
            else:
                # tour_hint inconnu : on essaie ATP, puis WTA.
                pid = self._atp_name_to_id.get(cn)
                if pid:
                    return {
                        "player_id": f"ATP::{pid}",
                        "method": "matches_recent_name",
                        "confidence": 0.99,
                        "reason": "match_atp_canonical",
                        "tour": "ATP",
                    }
                if not self._wta_name_to_id:
                    self._load_wta_matches()
                pid = self._wta_name_to_id.get(cn)
                if pid is not None:
                    return {
                        "player_id": f"WTA::{pid}",
                        "method": "wta_matches_name",
                        "confidence": 0.99,
                        "reason": "match_wta_canonical",
                        "tour": "WTA",
                    }

        # Si les indexes par tour ne matchent pas, on n'a aucune source utilisable :
        # le resolver Sackmann legacy retournait des IDs incompatibles avec nos indexes.
        return {
            "player_id": None,
            "method": "unresolved",
            "confidence": 0.0,
            "reason": "no_tour_match",
            "tour": th or None,
        }

    # ---------------------------------------------------------------------
    # Dernier match d'un joueur (par tour)
    # ---------------------------------------------------------------------
    def _last_atp_match(self, pid: str):
        if not pid:
            return None, None
        idxs = self._atp_winner_idx.get(pid, []) + self._atp_loser_idx.get(pid, [])
        if not idxs:
            return None, None
        last_pos = max(idxs)
        row = self.matches_atp_df.iloc[last_pos]
        is_winner = str(row.get("winner_id") or "").strip() == pid
        return row, is_winner

    def _last_wta_match(self, pid_int: int):
        df = self._load_wta_matches()
        if df is None or df.empty:
            return None, None
        idxs = self._wta_winner_idx.get(pid_int, []) + self._wta_loser_idx.get(pid_int, [])
        if not idxs:
            return None, None
        last_pos = max(idxs)
        row = df.iloc[last_pos]
        is_winner = _to_int_or_none(row.get("winner_id")) == pid_int
        return row, is_winner

    # ---------------------------------------------------------------------
    # Stats joueur (rank, age, ht, pts, hand)
    # ---------------------------------------------------------------------
    def _stats_from_match_row(self, row, is_winner: bool, source_label: str) -> Dict[str, Any]:
        def g(field_w, field_l):
            key = field_w if is_winner else field_l
            try:
                return row[key]
            except Exception:
                if hasattr(row, "get"):
                    return row.get(key)
                return None

        rank = g("winner_rank", "loser_rank")
        age = g("winner_age", "loser_age")
        ht = g("winner_ht", "loser_ht")
        pts = g("winner_rank_points", "loser_rank_points")
        hand = g("winner_hand", "loser_hand")
        rank = 100 if rank is None or pd.isna(rank) else int(float(rank))
        age = 25 if age is None or pd.isna(age) else float(age)
        ht = 185 if ht is None or pd.isna(ht) else float(ht)
        pts = 1000 if pts is None or pd.isna(pts) else float(pts)
        hand = "U" if hand is None or pd.isna(hand) else str(hand).strip().upper()[:1] or "U"
        return {
            "rank": rank,
            "age": age,
            "ht": ht,
            "pts": pts,
            "hand": hand,
            "stats_source": source_label,
        }

    def _wta_rankings_current_meta(self, pid_int: int) -> Optional[tuple[int, float, Optional[str]]]:
        """Rank, points et date de ligne classement WTA courant (si colonne présente)."""
        if not self._rankings_wta_current_exists():
            return None
        conn = self._connect()
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(rankings_wta_current)").fetchall()}
            if "ranking_date" in cols:
                row = conn.execute(
                    "SELECT ranking, points, ranking_date FROM rankings_wta_current "
                    "WHERE player_id = ? LIMIT 1",
                    (int(pid_int),),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT ranking, points FROM rankings_wta_current "
                    "WHERE player_id = ? LIMIT 1",
                    (int(pid_int),),
                ).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            conn.close()
        if not row or row[1] is None:
            return None
        rnk, pts = row[0], row[1]
        rnk = 100 if rnk is None else int(rnk)
        pts = float(pts)
        rdate = _normalize_date_display(row[2]) if len(row) > 2 else None
        return rnk, pts, rdate

    def _wta_rankings_stats_dict(self, pid_int: int, detail: str) -> Optional[Dict[str, Any]]:
        meta = self._wta_rankings_current_meta(pid_int)
        if not meta:
            return None
        rnk, pts, rdate = meta
        return {
            "rank": rnk,
            "age": 25,
            "ht": 185,
            "pts": pts,
            "hand": "U",
            "stats_source": "rankings_wta_current",
            "stats_reference_date": rdate,
            "stats_source_detail": detail,
        }

    def get_player_stats(self, player_id, player_name=None, tour_hint=None):
        """Stats joueur (rank/age/ht/pts/hand) selon le tour.

        - tour='ATP' : recherche dans `matches_recent` (TML) uniquement.
        - tour='WTA' : recherche dans `wta_matches` puis `rankings_wta_current` (Sackmann).
        Aucun fallback croisé.

        Champs supplémentaires :
        - ``stats_reference_date`` : date `YYYY-MM-DD` (match de réf. pour rang/points,
          ou `ranking_date` du classement WTA courant si applicable).
        """
        self._maybe_invalidate_stats_cache()
        th = (tour_hint or "").strip().upper()
        pid_key = _norm_pid_key(player_id) or ""
        cache_key = (pid_key, th)
        cached = self._stats_result_cache.get(cache_key)
        if cached is not None:
            if int(cached.get("_stats_cache_shape", 0) or 0) != _STATS_CACHE_SHAPE_VER:
                self._stats_result_cache.pop(cache_key, None)
            else:
                return _strip_stats_cache_meta(cached)

        if th == "ATP":
            row, is_w = self._last_atp_match(pid_key)
            if row is not None:
                out = self._stats_from_match_row(row, bool(is_w), "matches_recent")
                out["stats_reference_date"] = _stats_reference_date_from_row(row)
                self._cache_stats_result(cache_key, out)
                return _strip_stats_cache_meta(out)

        elif th == "WTA":
            pid_int = _to_int_or_none(player_id)
            if pid_int is not None:
                row, is_w = self._last_wta_match(pid_int)
                if row is not None:
                    out = self._stats_from_match_row(row, bool(is_w), "wta_matches")
                    out["stats_reference_date"] = _stats_reference_date_from_row(row)
                    self._cache_stats_result(cache_key, out)
                    return _strip_stats_cache_meta(out)

                out = self._wta_rankings_stats_dict(
                    pid_int,
                    "Aucun match WTA récent dans `wta_matches` pour cette joueuse — "
                    "rang/points issus de `rankings_wta_current` (Sackmann).",
                )
                if out is not None:
                    self._cache_stats_result(cache_key, out)
                    return _strip_stats_cache_meta(out)

        else:
            # Pas de tour_hint : on essaie ATP puis WTA.
            row, is_w = self._last_atp_match(pid_key)
            if row is not None:
                out = self._stats_from_match_row(row, bool(is_w), "matches_recent")
                out["stats_reference_date"] = _stats_reference_date_from_row(row)
                self._cache_stats_result(cache_key, out)
                return _strip_stats_cache_meta(out)
            pid_int = _to_int_or_none(player_id)
            if pid_int is not None:
                row, is_w = self._last_wta_match(pid_int)
                if row is not None:
                    out = self._stats_from_match_row(row, bool(is_w), "wta_matches")
                    out["stats_reference_date"] = _stats_reference_date_from_row(row)
                    self._cache_stats_result(cache_key, out)
                    return _strip_stats_cache_meta(out)
                out = self._wta_rankings_stats_dict(
                    pid_int,
                    "Aucun match WTA récent — rang/points depuis `rankings_wta_current` (Sackmann).",
                )
                if out is not None:
                    self._cache_stats_result(cache_key, out)
                    return _strip_stats_cache_meta(out)

        # Aucune source disponible.
        out = {
            "rank": 100,
            "age": 25,
            "ht": 185,
            "pts": 1000,
            "hand": "U",
            "stats_source": "no_ranking_source",
            "stats_reference_date": None,
            "stats_source_detail": (
                f"Aucune donnée pour ce joueur ({th or 'tour inconnu'}). "
                "ATP -> sync TML manquante (`scripts/sync_tml_recent.py`). "
                "WTA -> ingest Sackmann manquant (`scripts/ingest_sackmann_wta.py` "
                "+ `scripts/ingest_rankings_current.py`)."
            ),
        }
        self._cache_stats_result(cache_key, out)
        return _strip_stats_cache_meta(out)

    # ---------------------------------------------------------------------
    # Forme / fatigue / service / H2H par tour
    # ---------------------------------------------------------------------
    def _player_subframe(self, pid, tour: str) -> pd.DataFrame:
        if tour == "ATP":
            pid_key = _norm_pid_key(pid)
            if not pid_key:
                return self.matches_atp_df.iloc[0:0]
            idxs = self._atp_winner_idx.get(pid_key, []) + self._atp_loser_idx.get(pid_key, [])
            if not idxs:
                return self.matches_atp_df.iloc[0:0]
            return self.matches_atp_df.iloc[sorted(set(idxs))].reset_index(drop=True)
        if tour == "WTA":
            df = self._load_wta_matches()
            if df is None or df.empty:
                return df if df is not None else pd.DataFrame()
            pid_int = _to_int_or_none(pid)
            if pid_int is None:
                return df.iloc[0:0]
            idxs = self._wta_winner_idx.get(pid_int, []) + self._wta_loser_idx.get(pid_int, [])
            if not idxs:
                return df.iloc[0:0]
            return df.iloc[sorted(set(idxs))].reset_index(drop=True)
        return pd.DataFrame()

    @staticmethod
    def _winner_mask(df: pd.DataFrame, pid, tour: str) -> pd.Series:
        if df is None or df.empty:
            return pd.Series([], dtype=bool)
        if tour == "ATP":
            pid_key = _norm_pid_key(pid)
            return df["winner_id"].astype(str).str.strip() == (pid_key or "")
        # WTA
        pid_int = _to_int_or_none(pid)
        if pid_int is None:
            return pd.Series(False, index=df.index)
        nc = pd.to_numeric(df["winner_id"], errors="coerce")
        return nc == float(pid_int)

    @staticmethod
    def _loser_mask(df: pd.DataFrame, pid, tour: str) -> pd.Series:
        if df is None or df.empty:
            return pd.Series([], dtype=bool)
        if tour == "ATP":
            pid_key = _norm_pid_key(pid)
            return df["loser_id"].astype(str).str.strip() == (pid_key or "")
        pid_int = _to_int_or_none(pid)
        if pid_int is None:
            return pd.Series(False, index=df.index)
        nc = pd.to_numeric(df["loser_id"], errors="coerce")
        return nc == float(pid_int)

    def get_recent_form(self, player_id, days: int = 90, tour_hint: Optional[str] = None):
        if not player_id:
            return {"win_pct": 50.0, "matches": 0}
        tour = (tour_hint or "").strip().upper()
        if tour not in ("ATP", "WTA"):
            tour = "ATP" if _norm_pid_key(player_id) and not str(player_id).isdigit() else "WTA"
        df = self._player_subframe(player_id, tour)
        if df is None or df.empty:
            return {"win_pct": 50.0, "matches": 0}
        max_date = df["tourney_date"].max()
        recent = df[df["tourney_date"] >= max_date - pd.Timedelta(days=days)]
        wins = int(self._winner_mask(recent, player_id, tour).sum())
        losses = int(self._loser_mask(recent, player_id, tour).sum())
        total = wins + losses
        win_pct = (wins / total * 100) if total > 0 else 50.0
        return {"win_pct": win_pct, "matches": total, "wins": wins, "losses": losses}

    def get_recent_fatigue(self, player_id, days: int = 14, tour_hint: Optional[str] = None):
        if not player_id:
            return {"minutes_played": 0, "matches": 0}
        tour = (tour_hint or "").strip().upper()
        if tour not in ("ATP", "WTA"):
            tour = "ATP" if _norm_pid_key(player_id) and not str(player_id).isdigit() else "WTA"
        df = self._player_subframe(player_id, tour)
        if df is None or df.empty:
            return {"minutes_played": 0, "matches": 0}
        max_date = df["tourney_date"].max()
        recent = df[df["tourney_date"] >= max_date - pd.Timedelta(days=days)]
        wins_df = recent[self._winner_mask(recent, player_id, tour)]
        losses_df = recent[self._loser_mask(recent, player_id, tour)]
        total_matches = len(wins_df) + len(losses_df)
        if "minutes" not in recent.columns:
            return {"minutes_played": 0, "matches": total_matches}
        total_mins = wins_df["minutes"].sum() + losses_df["minutes"].sum()
        return {
            "minutes_played": int(total_mins) if pd.notna(total_mins) else 0,
            "matches": total_matches,
        }

    def get_recent_match_quality(self, player_id, tour_hint: Optional[str] = None):
        """Compute the *causal* recent-activity signals used by the v2 ML model.

        These replace the raw "minutes/sets played in last 7d" features that fooled
        the v1 model into thinking "less time on court = top player".

        Returns dict with:
            wins_last7d (int): victories in the past 7 days. Positive correlates with form.
            three_setters_last14d (int): 3+/4+ setter matches in last 14 days. Positive = fatigue.
            last_round_reached (int): depth at most-recent tournament (1=Q1 ... 8=W).
        """
        empty = {"wins_last7d": 0, "three_setters_last14d": 0, "last_round_reached": 0}
        if not player_id:
            return empty
        tour = (tour_hint or "").strip().upper()
        if tour not in ("ATP", "WTA"):
            tour = "ATP" if _norm_pid_key(player_id) and not str(player_id).isdigit() else "WTA"
        df = self._player_subframe(player_id, tour)
        if df is None or df.empty:
            return empty

        max_date = df["tourney_date"].max()
        wmask = self._winner_mask(df, player_id, tour)

        # Wins in last 7 days
        cutoff7 = max_date - pd.Timedelta(days=7)
        recent7 = df[df["tourney_date"] >= cutoff7]
        wmask7 = self._winner_mask(recent7, player_id, tour)
        wins7 = int(wmask7.sum())

        # 3+/4+ setter matches in last 14 days
        cutoff14 = max_date - pd.Timedelta(days=14)
        recent14 = df[df["tourney_date"] >= cutoff14]
        three_count = 0
        if "score" in recent14.columns:
            for _, row in recent14.iterrows():
                score_text = row.get("score", "")
                if not isinstance(score_text, str):
                    continue
                cleaned = score_text.split("RET")[0].split("W/O")[0].split("DEF")[0]
                import re as _re
                n_sets = len(_re.findall(r"\d+\s*-\s*\d+", cleaned))
                bo = row.get("best_of", 3)
                try:
                    bo_int = int(bo) if bo is not None and not pd.isna(bo) else 3
                except Exception:
                    bo_int = 3
                if (bo_int == 3 and n_sets >= 3) or (bo_int == 5 and n_sets >= 4):
                    three_count += 1

        # Round reached at most recent tournament: take rows of the latest tourney_id
        # (or fall back to the latest match date if tourney_id missing) and pick max depth.
        round_map = {
            "Q1": 1, "Q2": 2, "Q3": 3, "RR": 4, "BR": 5, "ER": 1,
            "R128": 1, "R64": 2, "R32": 3, "R16": 4,
            "QF": 5, "SF": 6, "F": 7, "W": 8,
        }
        last_round = 0
        if "tourney_id" in df.columns and "round" in df.columns:
            latest = df.dropna(subset=["tourney_date"]).sort_values("tourney_date").tail(20)
            if not latest.empty:
                last_tid = latest["tourney_id"].iloc[-1]
                last_tourney_rows = latest[latest["tourney_id"] == last_tid]
                # If player won the title, the deepest 'round' was 'F' AND they won it
                # — promote to depth 8. Else use the deepest reached round depth.
                depths = [round_map.get(str(r).upper(), 0) for r in last_tourney_rows["round"].tolist()]
                last_round = max(depths) if depths else 0
                # Did they win a Final? (means they won the title)
                final_rows = last_tourney_rows[last_tourney_rows["round"].astype(str).str.upper() == "F"]
                if not final_rows.empty:
                    final_won = self._winner_mask(final_rows, player_id, tour).any()
                    if final_won:
                        last_round = 8

        return {
            "wins_last7d": wins7,
            "three_setters_last14d": three_count,
            "last_round_reached": last_round,
        }

    def get_speed_profile(
        self,
        player_id,
        tour_hint: Optional[str] = None,
        days: int = 365,
    ) -> Dict[str, float]:
        """Compute speed-related features for live predictions (v3.5).

        Returns dict with:
            speed_affinity (float): mean(win_rate on fast courts ≥0.75) − mean(win_rate slow ≤0.65).
                                    0.0 if not enough samples in either bucket.
            speed_performance_delta (float): Pearson correlation between surface_speed and
                                             win_flag over the last `days`. 0.0 if insufficient data.
            samples (int): number of qualifying matches.
        """
        empty = {"speed_affinity": 0.0, "speed_performance_delta": 0.0, "samples": 0}
        if not player_id:
            return empty
        tour = (tour_hint or "").strip().upper()
        if tour not in ("ATP", "WTA"):
            tour = "ATP" if _norm_pid_key(player_id) and not str(player_id).isdigit() else "WTA"
        df = self._player_subframe(player_id, tour)
        if df is None or df.empty:
            return empty
        max_date = df["tourney_date"].max()
        cutoff = max_date - pd.Timedelta(days=int(days))
        recent = df[df["tourney_date"] >= cutoff]
        if recent.empty:
            return empty

        wmask = self._winner_mask(recent, player_id, tour).reset_index(drop=True)
        recent = recent.reset_index(drop=True)
        speeds = []
        wins = []
        for i, row in recent.iterrows():
            spd = row.get("surface_speed", None)
            if spd is None or (isinstance(spd, float) and pd.isna(spd)):
                spd = lookup_surface_speed(row.get("tourney_name"), row.get("surface"))
            try:
                speeds.append(float(spd))
                wins.append(1.0 if bool(wmask.iloc[i]) else 0.0)
            except Exception:
                continue

        if len(speeds) < 5:
            return empty

        speeds_a = np.array(speeds, dtype=float)
        wins_a = np.array(wins, dtype=float)
        if np.std(speeds_a) < 1e-9 or np.std(wins_a) < 1e-9:
            corr = 0.0
        else:
            c = np.corrcoef(speeds_a, wins_a)[0, 1]
            corr = 0.0 if np.isnan(c) else float(c)

        fast = wins_a[speeds_a >= 0.75]
        slow = wins_a[speeds_a <= 0.65]
        aff = 0.0
        if len(fast) >= 2 and len(slow) >= 2:
            aff = float(np.mean(fast) - np.mean(slow))
        return {
            "speed_affinity": float(aff),
            "speed_performance_delta": float(corr),
            "samples": int(len(speeds)),
        }

    def get_bp_resilience(
        self,
        player_id,
        tour_hint: Optional[str] = None,
        days: int = 365,
    ) -> float:
        """Rolling break-point save ratio (bp_saved / bp_faced) over the past `days`.

        Used by the WTA branch (v3.5) where the ability to hold serve under pressure
        outweighs raw aces as a win predictor.
        """
        if not player_id:
            return 0.5
        tour = (tour_hint or "").strip().upper()
        if tour not in ("ATP", "WTA"):
            tour = "ATP" if _norm_pid_key(player_id) and not str(player_id).isdigit() else "WTA"
        df = self._player_subframe(player_id, tour)
        if df is None or df.empty:
            return 0.5
        max_date = df["tourney_date"].max()
        cutoff = max_date - pd.Timedelta(days=int(days))
        recent = df[df["tourney_date"] >= cutoff].reset_index(drop=True)
        if recent.empty:
            return 0.5
        wmask = self._winner_mask(recent, player_id, tour).reset_index(drop=True)
        saved = 0.0
        faced = 0.0
        for i, row in recent.iterrows():
            if bool(wmask.iloc[i]):
                s = row.get("w_bpSaved")
                f = row.get("w_bpFaced")
            else:
                s = row.get("l_bpSaved")
                f = row.get("l_bpFaced")
            try:
                if s is None or f is None or pd.isna(s) or pd.isna(f) or float(f) <= 0:
                    continue
                saved += float(s)
                faced += float(f)
            except Exception:
                continue
        if faced <= 0:
            return 0.5
        return float(np.clip(saved / faced, 0.0, 1.0))

    def get_service_hold_probability(
        self,
        player_id,
        surface: Optional[str] = None,
        pressure: bool = False,
        tour_hint: Optional[str] = None,
    ) -> float:
        if not player_id:
            return 0.5
        tour = (tour_hint or "").strip().upper()
        if tour not in ("ATP", "WTA"):
            tour = "ATP" if _norm_pid_key(player_id) and not str(player_id).isdigit() else "WTA"
        combo = self._player_subframe(player_id, tour)
        if combo is None or combo.empty or "w_SvGms" not in combo.columns:
            return 0.5
        wins = combo[self._winner_mask(combo, player_id, tour)].copy()
        losses = combo[self._loser_mask(combo, player_id, tour)].copy()
        if surface and surface in ("Hard", "Clay", "Grass", "Carpet"):
            if "surface" in wins.columns:
                wins = wins[wins["surface"] == surface]
                losses = losses[losses["surface"] == surface]
        if wins.empty and losses.empty:
            return 0.5
        wins["service_games_lost"] = wins["w_bpFaced"] - wins["w_bpSaved"]
        wins["service_games_won"] = wins["w_SvGms"] - wins["service_games_lost"]
        losses["service_games_lost"] = losses["l_bpFaced"] - losses["l_bpSaved"]
        losses["service_games_won"] = losses["l_SvGms"] - losses["service_games_lost"]
        total_games = wins["w_SvGms"].sum() + losses["l_SvGms"].sum()
        won_games = wins["service_games_won"].sum() + losses["service_games_won"].sum()
        if total_games == 0 or pd.isna(total_games):
            return 0.5
        hold_prob = won_games / total_games
        if pressure:
            total_bp = wins["w_bpFaced"].sum() + losses["l_bpFaced"].sum()
            saved_bp = wins["w_bpSaved"].sum() + losses["l_bpSaved"].sum()
            if total_bp > 0:
                hold_prob = (hold_prob + (saved_bp / total_bp)) / 2
        return float(hold_prob)

    def get_h2h(
        self,
        p1_id,
        p2_id,
        p1_name=None,
        p2_name=None,
        tour_hint: Optional[str] = None,
    ):
        """H2H par tour. Ne mélange pas ATP et WTA."""
        tour = (tour_hint or "").strip().upper()
        if tour not in ("ATP", "WTA"):
            # Inférence : si les deux pids sont des ints -> WTA ; si l'un des deux est str alphanum -> ATP.
            if all(_to_int_or_none(p) is not None for p in (p1_id, p2_id) if p):
                tour = "WTA"
            else:
                tour = "ATP"

        if tour == "ATP":
            df = self.matches_atp_df
        else:
            df = self._load_wta_matches()
        if df is None or df.empty or not p1_id or not p2_id:
            return {"p1_wins": 0, "p2_wins": 0}

        w1m = self._winner_mask(df, p1_id, tour)
        l2m = self._loser_mask(df, p2_id, tour)
        p1_wins = int((w1m & l2m).sum())
        w2m = self._winner_mask(df, p2_id, tour)
        l1m = self._loser_mask(df, p1_id, tour)
        p2_wins = int((w2m & l1m).sum())
        if p1_wins + p2_wins > 0:
            return {"p1_wins": p1_wins, "p2_wins": p2_wins}

        # Repli par noms canoniques (utile si les ids manquent ou ne matchent pas).
        if p1_name and p2_name and "winner_name" in df.columns:
            p1c = canonical_name(p1_name)
            p2c = canonical_name(p2_name)
            if p1c and p2c:
                wcan = df["winner_name"].fillna("").map(canonical_name)
                lcan = df["loser_name"].fillna("").map(canonical_name)
                p1_wins = int(((wcan == p1c) & (lcan == p2c)).sum())
                p2_wins = int(((wcan == p2c) & (lcan == p1c)).sum())
        return {"p1_wins": p1_wins, "p2_wins": p2_wins}

    # ---------------------------------------------------------------------
    # Probabilité contextuelle (jeu de service, surface, pression)
    # ---------------------------------------------------------------------
    def calculate_situational_probability(
        self,
        server_name,
        returner_name,
        surface,
        set_score,
        games_score,
        tour_hint: Optional[str] = None,
    ):
        server_id = self.get_player_id(server_name, tour_hint=tour_hint)
        returner_id = self.get_player_id(returner_name, tour_hint=tour_hint)
        if not server_id or not returner_id:
            return 0.5
        try:
            games_split = [int(g) for g in games_score.split("-")]
        except (ValueError, AttributeError):
            games_split = []
        pressure = False
        if len(games_split) == 2:
            s_games, r_games = games_split
            if r_games >= 5 and s_games < r_games:
                pressure = True
        server_hold = self.get_service_hold_probability(
            server_id, surface=surface, pressure=pressure, tour_hint=tour_hint
        )
        returner_hold = self.get_service_hold_probability(
            returner_id, surface=surface, pressure=False, tour_hint=tour_hint
        )
        return_break_prob = 1 - returner_hold
        combined = server_hold - (0.5 - (1 - return_break_prob))
        combined = max(0.01, min(0.99, combined))
        return {
            "server": server_name,
            "returner": returner_name,
            "surface": surface,
            "situation": f"{set_score} {games_score}",
            "pressure_detected": pressure,
            "raw_server_hold_prob": server_hold,
            "adjusted_hold_prob": combined,
            "true_odd": 1 / combined if combined > 0 else 0,
        }


def _safe_ratio_hf(num, den, default=np.nan):
    try:
        n = float(num)
        d = float(den)
        if d <= 0:
            return default
        return n / d
    except (TypeError, ValueError):
        return default


def _tb_wins_played_from_score(score_text: object, player_is_winner: bool) -> Tuple[float, float]:
    if not isinstance(score_text, str):
        return 0.0, 0.0
    cleaned = score_text.split("RET")[0].split("W/O")[0].split("DEF")[0]
    sets = re.findall(r"(\d+)\s*-\s*(\d+)", cleaned)
    won = played = 0.0
    for a, b in sets:
        try:
            sa, sb = int(a), int(b)
        except ValueError:
            continue
        if (sa == 7 and sb == 6) or (sa == 6 and sb == 7):
            played += 1.0
            if (player_is_winner and sa == 7) or ((not player_is_winner) and sb == 7):
                won += 1.0
    return float(won), float(played)


def tactical_vector_52weeks(rows: pd.DataFrame, name_key: str, ref_dt: pd.Timestamp, min_pts: int = 5):
    """Moyenne 52 sem glissantes (ace%, 1er%, BP sauvés%, hold jeu de service %) — lignes hors `score` simplifiées."""
    if rows is None or rows.empty or not name_key:
        return 0.08, 0.62, 0.58, 0.75
    try:
        ref = pd.Timestamp(ref_dt).normalize()
    except Exception:
        ref = pd.Timestamp.now().normalize()
    cutoff = ref - pd.Timedelta(days=365)
    r = rows[rows["tourney_date"] < ref]
    r = r[r["tourney_date"] >= cutoff]
    if len(r) < min_pts:
        return 0.08, 0.62, 0.58, 0.75

    ace_l, f1_l, bp_l, hold_l, w_l = [], [], [], [], []
    for x in r.itertuples(index=False):
        is_w = getattr(x, "w_key", None) == name_key
        try:
            dtd = pd.Timestamp(getattr(x, "tourney_date"))
            days_diff = max(0.0, float((ref - dtd).days))
            wt = float(np.exp(-days_diff / 180.0))
            if is_w:
                ace_l.append(_safe_ratio_hf(x.w_ace, x.w_svpt, 0.06))
                f1_l.append(_safe_ratio_hf(x.w_1stWon, x.w_1stIn, 0.62))
                bp_l.append(_safe_ratio_hf(x.w_bpSaved, x.w_bpFaced, np.nan))
                sv = float(x.w_SvGms) if pd.notna(getattr(x, "w_SvGms", np.nan)) else 0.0
                bpf = float(x.w_bpFaced) if pd.notna(getattr(x, "w_bpFaced", np.nan)) else 0.0
                bsv = float(x.w_bpSaved) if pd.notna(getattr(x, "w_bpSaved", np.nan)) else 0.0
                l_sv = float(x.l_SvGms) if pd.notna(getattr(x, "l_SvGms", np.nan)) else 0.0
            else:
                ace_l.append(_safe_ratio_hf(x.l_ace, x.l_svpt, 0.06))
                f1_l.append(_safe_ratio_hf(x.l_1stWon, x.l_1stIn, 0.62))
                bp_l.append(_safe_ratio_hf(x.l_bpSaved, x.l_bpFaced, np.nan))
                sv = float(x.l_SvGms) if pd.notna(getattr(x, "l_SvGms", np.nan)) else 0.0
                bpf = float(x.l_bpFaced) if pd.notna(getattr(x, "l_bpFaced", np.nan)) else 0.0
                bsv = float(x.l_bpSaved) if pd.notna(getattr(x, "l_bpSaved", np.nan)) else 0.0
                l_sv = float(x.w_SvGms) if pd.notna(getattr(x, "w_SvGms", np.nan)) else 0.0
            breaks_suf = max(0.0, bpf - bsv)
            hold = (sv - breaks_suf) / sv if sv > 0 else 0.75
            hold_l.append(max(0.0, min(1.0, hold)))
            w_l.append(wt)
        except Exception:
            continue
    if len(ace_l) < min_pts:
        return 0.08, 0.62, 0.58, 0.75

    def _m(vals, wts, d):
        vw = [
            (float(v), float(w))
            for v, w in zip(vals, wts)
            if not (isinstance(v, float) and np.isnan(v))
        ]
        if not vw:
            return d
        arr_v = np.asarray([x[0] for x in vw], dtype=float)
        arr_w = np.asarray([x[1] for x in vw], dtype=float)
        sw = float(np.sum(arr_w))
        if sw <= 1e-12:
            return float(np.mean(arr_v))
        return float(np.sum(arr_v * arr_w) / sw)

    return (
        _m(ace_l, w_l, 0.08),
        _m(f1_l, w_l, 0.62),
        _m(bp_l, w_l, 0.58),
        float(np.clip(_m(hold_l, w_l, 0.75), 0, 1)),
    )


def clutch_score_52weeks(rows: pd.DataFrame, name_key: str, ref_dt: pd.Timestamp, min_pts: int = 3):
    """(BP_Saved% + BP_Converted% + TieBreak Win%) / 3 sur fenêtre ~52 semaines."""
    if rows is None or rows.empty or not name_key:
        return 0.5
    try:
        ref = pd.Timestamp(ref_dt).normalize()
    except Exception:
        ref = pd.Timestamp.now().normalize()
    cutoff = ref - pd.Timedelta(days=365)
    r = rows[rows["tourney_date"] < ref]
    r = r[r["tourney_date"] >= cutoff]
    if len(r) < min_pts:
        return 0.5
    bp_sv, bp_cnv = [], []
    tb_w, tb_p = 0.0, 0.0
    for x in r.itertuples(index=False):
        is_w = getattr(x, "w_key", None) == name_key
        score = getattr(x, "score", "")
        if is_w:
            bp_sv.append(_safe_ratio_hf(x.w_bpSaved, x.w_bpFaced, np.nan))
            opp_b = max(0.0, float(x.l_bpFaced or 0) - float(x.l_bpSaved or 0))
            bp_cnv.append(_safe_ratio_hf(opp_b, x.l_bpFaced, np.nan))
        else:
            bp_sv.append(_safe_ratio_hf(x.l_bpSaved, x.l_bpFaced, np.nan))
            opp_b = max(0.0, float(x.w_bpFaced or 0) - float(x.w_bpSaved or 0))
            bp_cnv.append(_safe_ratio_hf(opp_b, x.w_bpFaced, np.nan))
        wn, pl = _tb_wins_played_from_score(score, is_w)
        tb_w += wn
        tb_p += pl
    m1 = float(np.nanmean([v for v in bp_sv if not pd.isna(v)])) if any(not pd.isna(v) for v in bp_sv) else 0.5
    m2 = float(np.nanmean([v for v in bp_cnv if not pd.isna(v)])) if any(not pd.isna(v) for v in bp_cnv) else 0.5
    m3 = (tb_w / tb_p) if tb_p > 0 else 0.5
    return float(np.clip((m1 + m2 + m3) / 3.0, 0.0, 1.0))


def travel_fatigue_index_from_history(
    rows: pd.DataFrame,
    name_key: str,
    current_tournament: object,
    ref_dt: pd.Timestamp,
) -> float:
    """0 ou 0.05 : malus si saut long-courrier / fuseau et repos < 4 jours (inféré sur l’historique)."""
    try:
        from scripts.tournament_geo import haversine_km, tournament_site_lon_lat_tz
    except ImportError:
        from tournament_geo import haversine_km, tournament_site_lon_lat_tz  # type: ignore
    if rows is None or rows.empty or not name_key:
        return 0.0
    try:
        ref = pd.Timestamp(ref_dt).normalize()
    except Exception:
        ref = pd.Timestamp.now().normalize()
    prev_all = rows[rows["tourney_date"] < ref]
    if prev_all.empty:
        return 0.0
    last = prev_all.iloc[-1]
    prev_name = getattr(last, "tourney_name", None) if hasattr(last, "tourney_name") else last["tourney_name"]
    ld = pd.Timestamp(last["tourney_date"])
    rest = max(0, int((ref - ld).days))
    if rest >= 4:
        return 0.0
    cur_lat, cur_lon, cur_tz = tournament_site_lon_lat_tz(current_tournament)
    plat, plon, ptz = tournament_site_lon_lat_tz(prev_name)
    dist_km = haversine_km(float(plat), float(plon), float(cur_lat), float(cur_lon))
    if dist_km > 4000.0 or abs(int(cur_tz) - int(ptz)) > 4:
        return 0.05
    return 0.0


if __name__ == "__main__":
    e = TennisStatsEngine()
    print("ATP indexed players:", len(e._atp_winner_idx) + len(e._atp_loser_idx))
    print("Sample resolves:")
    for n in ("Sabalenka A.", "Swiatek I.", "Sinner J.", "Alcaraz C.", "Sabalenka A. (1)"):
        atp = e.get_player_id_meta(n, tour_hint="ATP")
        wta = e.get_player_id_meta(n, tour_hint="WTA")
        print(f"  {n!r:25} ATP={atp.get('player_id')}/{atp.get('method')}  WTA={wta.get('player_id')}/{wta.get('method')}")
