"""Shared helpers for WTA Sackmann delta pipeline (preprod)."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

SACKMANN_COLUMNS: tuple[str, ...] = (
    "tourney_id",
    "tourney_name",
    "surface",
    "draw_size",
    "tourney_level",
    "tourney_date",
    "match_num",
    "winner_id",
    "winner_seed",
    "winner_entry",
    "winner_name",
    "winner_hand",
    "winner_ht",
    "winner_ioc",
    "winner_age",
    "loser_id",
    "loser_seed",
    "loser_entry",
    "loser_name",
    "loser_hand",
    "loser_ht",
    "loser_ioc",
    "loser_age",
    "score",
    "best_of",
    "round",
    "minutes",
    "w_ace",
    "w_df",
    "w_svpt",
    "w_1stIn",
    "w_1stWon",
    "w_2ndWon",
    "w_SvGms",
    "w_bpSaved",
    "w_bpFaced",
    "l_ace",
    "l_df",
    "l_svpt",
    "l_1stIn",
    "l_1stWon",
    "l_2ndWon",
    "l_SvGms",
    "l_bpSaved",
    "l_bpFaced",
    "winner_rank",
    "winner_rank_points",
    "loser_rank",
    "loser_rank_points",
)

DEFAULT_CUTOFF = 20260526

# ITF / W15 : pas de rang WTA officiel pour la plupart des joueuses — QC aligné sur main_delta (D1).
WTA_ITF_TOURNEY_LEVELS = frozenset({"15", "I"})

MIN_PLAYER_AGE = 15.0
MAX_PLAYER_AGE = 45.0

SERVE_COLS = (
    "w_ace",
    "w_df",
    "w_svpt",
    "w_1stIn",
    "w_1stWon",
    "w_2ndWon",
    "w_SvGms",
    "w_bpSaved",
    "w_bpFaced",
    "l_ace",
    "l_df",
    "l_svpt",
    "l_1stIn",
    "l_1stWon",
    "l_2ndWon",
    "l_SvGms",
    "l_bpSaved",
    "l_bpFaced",
)


def empty_row() -> dict[str, Any]:
    return {c: pd.NA for c in SACKMANN_COLUMNS}


def norm_name_key(name: object) -> str:
    s = str(name or "").strip().lower()
    s = re.sub(r"[^a-z0-9 .-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_name_to_player_id(socle: pd.DataFrame) -> dict[str, int]:
    """Prefer lowest numeric id per normalized name (Sackmann id over synthetic)."""
    from scripts.player_identity import canonical_name, to_lastname_initial

    def _nk(name: object) -> str:
        raw = str(name or "").strip()
        if not raw:
            return ""
        return canonical_name(to_lastname_initial(raw)) or norm_name_key(raw)

    out: dict[str, int] = {}
    if socle is None or socle.empty:
        return out
    for _, row in socle.iterrows():
        for name_col, id_col in (("winner_name", "winner_id"), ("loser_name", "loser_id")):
            nk = _nk(row.get(name_col))
            if not nk:
                continue
            try:
                pid = int(float(row.get(id_col)))
            except (TypeError, ValueError):
                continue
            prev = out.get(nk)
            if prev is None or pid < prev:
                out[nk] = pid
    return out


def resolve_player_id(name: str, name_to_id: dict[str, int], player_ids: set[int]) -> int:
    from scripts.player_identity import canonical_name, to_lastname_initial

    raw = str(name or "").strip()
    nk = canonical_name(to_lastname_initial(raw)) or norm_name_key(raw)
    if nk in name_to_id:
        return int(name_to_id[nk])
    return next_synthetic_player_id(player_ids)


def dedup_key(row: dict | pd.Series) -> tuple:
    td = row.get("tourney_date")
    if isinstance(td, pd.Timestamp):
        td_i = int(td.strftime("%Y%m%d"))
    else:
        td_i = int(str(td).replace("-", "")[:8])
    return (
        td_i,
        str(row.get("tourney_name", "") or "").strip(),
        str(row.get("winner_name", "") or "").strip(),
        str(row.get("loser_name", "") or "").strip(),
    )


def is_qual_itf_tier(tier: object, tourney_name: object = None) -> bool:
    """True si la ligne doit aller dans ``wta_matches_qual_itf_*`` (ITF / qual)."""
    t = str(tier or "").upper().strip()
    tn = str(tourney_name or "").upper()
    if "ITF" in t or t.startswith("Q") or "QUAL" in t:
        return True
    if "ITF" in tn:
        return True
    return False


def is_qual_itf_route(
    category: object,
    tier: object,
    tourney_name: object = None,
    tourney_level: object = None,
) -> bool:
    """Routage qual/ITF (Flashscore bridge, prematch TE category ITF)."""
    if str(category or "").upper() == "ITF":
        return True
    return is_qual_itf_tier(tier, tourney_name)


def tier_to_level(tier: object) -> str:
    t = str(tier or "").upper().replace(" ", "")
    if "GRANDSLAM" in t or t == "GS":
        return "G"
    if "1000" in t:
        return "PM"
    if "500" in t:
        return "P"
    if "250" in t:
        return "I"
    if "125" in t:
        return "I"
    if t.startswith("ITF") or t in {"15", "25", "35", "50", "60", "75", "100"}:
        return t.replace("ITF", "") or "15"
    return "I"


def round_to_sackmann(round_raw: object) -> str:
    r = str(round_raw or "").strip().lower()
    mapping = {
        "1st round": "R64",
        "2nd round": "R32",
        "3rd round": "R16",
        "round of 16": "R16",
        "quarterfinals": "QF",
        "quarter-finals": "QF",
        "semifinals": "SF",
        "semi-finals": "SF",
        "the final": "F",
        "final": "F",
        "round robin": "RR",
    }
    for k, v in mapping.items():
        if k in r:
            return v
    if r.startswith("q"):
        return "Q" + r[-1] if r[-1:].isdigit() else "Q"
    return str(round_raw or "R32")[:8]


def surface_norm(s: object) -> str:
    x = str(s or "Hard").strip().title()
    if x not in {"Hard", "Clay", "Grass", "Carpet"}:
        return "Hard"
    return x


def build_score(row: pd.Series) -> str:
    parts = []
    for i in range(1, 6):
        wk, lk = f"W{i}", f"L{i}"
        if wk in row.index and pd.notna(row.get(wk)) and pd.notna(row.get(lk)):
            parts.append(f"{int(row[wk])}-{int(row[lk])}")
    return " ".join(parts) if parts else str(row.get("Comment", "") or "")


def tourney_id_from(name: str, year: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", norm_name_key(name))[:24] or "tourney"
    return f"{year}-{slug}"


def parse_yyyymmdd(val: object) -> int | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, pd.Timestamp):
        return int(val.strftime("%Y%m%d"))
    s = str(val).strip()
    if len(s) >= 10 and s[4] == "-":
        return int(s[:4] + s[5:7] + s[8:10])
    if s.isdigit() and len(s) == 8:
        return int(s)
    try:
        return int(pd.to_datetime(val).strftime("%Y%m%d"))
    except Exception:
        return None


def clamp_player_age(age: float) -> float:
    return round(max(MIN_PLAYER_AGE, min(MAX_PLAYER_AGE, float(age))), 2)


def _age_is_plausible(age: object) -> bool:
    try:
        a = float(age)
    except (TypeError, ValueError):
        return False
    return MIN_PLAYER_AGE <= a <= MAX_PLAYER_AGE


def build_age_lookup(matches_df: pd.DataFrame) -> dict[str, tuple[int, float]]:
    """name_key -> (last_tourney_date_int, age_at_that_date)."""
    out: dict[str, tuple[int, float]] = {}
    if matches_df.empty:
        return out
    df = matches_df.copy()
    df["_td"] = df["tourney_date"].apply(parse_yyyymmdd)
    df = df.dropna(subset=["_td"])
    for _, row in df.iterrows():
        td = int(row["_td"])
        for name_col, age_col in (("winner_name", "winner_age"), ("loser_name", "loser_age")):
            nk = norm_name_key(row.get(name_col))
            age = row.get(age_col)
            if not nk or pd.isna(age):
                continue
            try:
                age_f = float(age)
            except (TypeError, ValueError):
                continue
            if not _age_is_plausible(age_f):
                continue
            prev = out.get(nk)
            if prev is None or td >= prev[0]:
                out[nk] = (td, age_f)
    return out


def estimate_age(name: str, tourney_date: int, lookup: dict[str, tuple[int, float]], default: float = 25.0) -> float:
    nk = norm_name_key(name)
    rec = lookup.get(nk)
    if not rec:
        return clamp_player_age(default)
    last_td, last_age = rec
    days = max(0, tourney_date - last_td)
    return clamp_player_age(last_age + days / 365.25)


def next_synthetic_player_id(existing: set[int]) -> int:
    base = 920000
    while base in existing:
        base += 1
    existing.add(base)
    return base


def build_player_profile_lookup(
    matches_df: pd.DataFrame,
) -> tuple[dict[str, int], dict[int, dict[str, Any]]]:
    """name_key -> Sackmann player_id ; player_id -> dernier profil connu (hand/ht/ioc/age)."""
    name_to_id = build_name_to_player_id(matches_df)
    profiles: dict[int, dict[str, Any]] = {}
    if matches_df.empty:
        return name_to_id, profiles

    for _, row in matches_df.iterrows():
        td = parse_yyyymmdd(row.get("tourney_date"))
        if td is None:
            continue
        for name_col, id_col, hand_col, ht_col, ioc_col, age_col in (
            ("winner_name", "winner_id", "winner_hand", "winner_ht", "winner_ioc", "winner_age"),
            ("loser_name", "loser_id", "loser_hand", "loser_ht", "loser_ioc", "loser_age"),
        ):
            try:
                pid = int(float(row.get(id_col)))
            except (TypeError, ValueError):
                continue
            if pid >= 900000:
                continue
            prev = profiles.get(pid)
            if prev is not None and int(prev.get("last_tourney_date") or 0) > td:
                continue
            prof: dict[str, Any] = {
                "player_id": pid,
                "name": str(row.get(name_col) or "").strip(),
                "last_tourney_date": td,
            }
            for src, dst in (
                (hand_col, "hand"),
                (ht_col, "ht"),
                (ioc_col, "ioc"),
                (age_col, "age"),
            ):
                val = row.get(src)
                if val is not None and not (isinstance(val, float) and pd.isna(val)):
                    s = str(val).strip()
                    if s:
                        if dst == "age" and not _age_is_plausible(val):
                            continue
                        prof[dst] = val if dst == "age" else s
            if prev:
                for k in ("hand", "ht", "ioc", "age", "name"):
                    if k not in prof and k in prev:
                        prof[k] = prev[k]
            profiles[pid] = prof
    return name_to_id, profiles


def apply_player_enrichment(
    row: dict[str, Any],
    *,
    tourney_date: int,
    name_to_id: dict[str, int],
    profiles: dict[int, dict[str, Any]],
    age_lookup: dict[str, tuple[int, float]],
    player_ids: set[int],
) -> None:
    """Remplit winner/loser id, âges, hand, ht, ioc sur une ligne Sackmann (in-place)."""
    winner = str(row.get("winner_name") or "").strip()
    loser = str(row.get("loser_name") or "").strip()
    wid = resolve_player_id(winner, name_to_id, player_ids)
    lid = resolve_player_id(loser, name_to_id, player_ids)
    row["winner_id"] = wid
    row["loser_id"] = lid

    for side, name, pid in (("winner", winner, wid), ("loser", loser, lid)):
        prof = profiles.get(pid) if pid < 900000 else None
        age = estimate_age(name, tourney_date, age_lookup)
        if prof and prof.get("age") is not None and prof.get("last_tourney_date"):
            last_td = int(prof["last_tourney_date"])
            days = max(0, tourney_date - last_td)
            age = clamp_player_age(float(prof["age"]) + days / 365.25)
        row[f"{side}_age"] = age
        if prof:
            if prof.get("hand"):
                row[f"{side}_hand"] = prof["hand"]
            if prof.get("ht") is not None and not pd.isna(prof.get("ht")):
                try:
                    row[f"{side}_ht"] = int(float(prof["ht"]))
                except (TypeError, ValueError):
                    row[f"{side}_ht"] = prof["ht"]
            if prof.get("ioc"):
                row[f"{side}_ioc"] = prof["ioc"]


def _rank_val_ok(val: object) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    s = str(val).strip()
    return bool(s) and s.lower() not in {"nan", "none"}


_TE_HASH_SUFFIX_RE = re.compile(r"\s[a-f0-9]{5}$", re.I)


def te_name_key_safe(key: object) -> bool:
    """False pour clés TE internes type ``oliveira 7b451`` (non utilisables comme alias)."""
    k = str(key or "").strip()
    if not k or len(k) < 3:
        return False
    if _TE_HASH_SUFFIX_RE.search(k):
        return False
    return any(ch.isalpha() for ch in k)


def name_last_first_initial(nk: str) -> tuple[str, str]:
    """Déduit (nom de famille, initiale) depuis une clé ``norm_name_key``."""
    parts = str(nk or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    if len(parts[-1].rstrip(".")) <= 2:
        last = " ".join(parts[:-1])
        fi = parts[-1].rstrip(".")[:1]
        return last, fi
    last = parts[-1]
    fi = parts[0][:1] if parts else ""
    return last, fi


def build_wta_players_last_index(work_dir: Path | str) -> dict[tuple[str, str], int]:
    """(last_norm, first_initial) -> player_id depuis ``wta_players.csv``."""
    path = Path(work_dir) / "wta_players.csv"
    out: dict[tuple[str, str], int] = {}
    if not path.is_file():
        return out
    try:
        pdf = pd.read_csv(path, low_memory=False)
    except Exception:
        return out
    for _, row in pdf.iterrows():
        try:
            pid = int(float(row.get("player_id")))
        except (TypeError, ValueError):
            continue
        ln = norm_name_key(row.get("last_name"))
        fn = str(row.get("first_name") or "").strip()
        if not ln:
            continue
        fi = fn[:1].lower() if fn else ""
        for key in ((ln, fi), (ln, "")):
            out.setdefault(key, pid)
    return out


def lookup_te_rank_fuzzy(
    raw_name: object,
    *,
    te_rank_by_name: dict[str, tuple[float, float]],
    te_rank_by_pid: dict[int, tuple[float, float]] | None = None,
    name_to_id: dict[str, int] | None = None,
    wta_players_last_index: dict[tuple[str, str], int] | None = None,
) -> tuple[float, float] | None:
    """Rang TE via clé exacte sûre, index joueuses WTA, ou match nom+initiale."""
    nk = norm_name_key(raw_name)
    if not nk:
        return None
    if nk in te_rank_by_name and te_name_key_safe(nk):
        return te_rank_by_name[nk]

    te_pid = te_rank_by_pid or {}
    id_map = name_to_id or {}
    try:
        from scripts.wta_name_aliases import canonical_wta_name_key

        cnk = canonical_wta_name_key(raw_name)
    except Exception:
        cnk = nk
    pid = id_map.get(cnk) or id_map.get(nk)
    if pid is not None and int(pid) in te_pid:
        return te_pid[int(pid)]

    last, fi = name_last_first_initial(nk)
    if last and wta_players_last_index:
        for key in ((last, fi), (last, "")):
            p = wta_players_last_index.get(key)
            if p is not None and int(p) in te_pid:
                return te_pid[int(p)]

    best: tuple[float, float] | None = None
    best_len = 999
    for pk, val in te_rank_by_name.items():
        if not te_name_key_safe(pk):
            continue
        pp = pk.split()
        if not pp or pp[0] != last:
            continue
        if fi and len(pp) > 1 and not pp[1].startswith(fi):
            continue
        if len(pp) < best_len:
            best = val
            best_len = len(pp)
    return best


def build_rank_history(matches_df: pd.DataFrame) -> dict[int, list[tuple[int, float, float]]]:
    """Historique (tourney_date, rank, rank_points) par player_id, trié par date."""
    out: dict[int, list[tuple[int, float, float]]] = {}
    if matches_df.empty:
        return out
    for _, row in matches_df.iterrows():
        td = parse_yyyymmdd(row.get("tourney_date"))
        if td is None:
            continue
        for pid_col, rank_col, pts_col in (
            ("winner_id", "winner_rank", "winner_rank_points"),
            ("loser_id", "loser_rank", "loser_rank_points"),
        ):
            try:
                pid = int(float(row.get(pid_col)))
            except (TypeError, ValueError):
                continue
            if pid >= 900000:
                continue
            rank = row.get(rank_col)
            pts = row.get(pts_col)
            if not _rank_val_ok(rank):
                continue
            try:
                rank_f = float(rank)
                pts_f = float(pts) if _rank_val_ok(pts) else float("nan")
            except (TypeError, ValueError):
                continue
            out.setdefault(pid, []).append((td, rank_f, pts_f))
    for pid in out:
        out[pid].sort(key=lambda x: x[0])
    return out


def lookup_rank_at(
    history: dict[int, list[tuple[int, float, float]]],
    pid: int,
    tourney_date: int,
) -> tuple[float | None, float | None]:
    recs = history.get(pid)
    if not recs:
        return None, None
    rank, pts = None, None
    for td, r, p in recs:
        if td > tourney_date:
            break
        rank, pts = r, p if not (isinstance(p, float) and pd.isna(p)) else None
    return rank, pts


def _days_between_yyyymmdd(a: int, b: int) -> int:
    from datetime import date

    def _to_d(x: int) -> date:
        return date(x // 10000, (x // 100) % 100, x % 100)

    return abs((_to_d(b) - _to_d(a)).days)


def lookup_rank_forward(
    history: dict[int, list[tuple[int, float, float]]],
    pid: int,
    tourney_date: int,
    *,
    max_days: int = 60,
) -> tuple[float | None, float | None]:
    """Premier rang observé dans les ``max_days`` après la date du match (débuts / trou historique)."""
    recs = history.get(pid)
    if not recs:
        return None, None
    for td, r, p in recs:
        if td < tourney_date:
            continue
        if _days_between_yyyymmdd(tourney_date, td) > max_days:
            break
        return r, p if not (isinstance(p, float) and pd.isna(p)) else None
    return None, None


def resolve_player_id_for_rank(
    row: dict[str, Any] | pd.Series,
    side: str,
    name_to_id: dict[str, int] | None,
) -> int | None:
    """Résout un player_id réel (< 900000) depuis l'ID ligne ou le nom."""
    pid_col = f"{side}_id"
    name_col = f"{side}_name"
    try:
        pid = int(float(row.get(pid_col)))
        if pid < 900000:
            return pid
    except (TypeError, ValueError):
        pass
    if not name_to_id:
        return None
    try:
        from scripts.wta_name_aliases import canonical_wta_name_key

        nk = canonical_wta_name_key(row.get(name_col))
    except Exception:
        nk = norm_name_key(row.get(name_col))
    if not nk:
        return None
    return name_to_id.get(nk)


def load_current_rankings_map(
    work_dir: Path | str,
    db_path: str | None = None,
) -> dict[int, tuple[float, float]]:
    """player_id -> (rank, points) depuis wta_rankings_current.csv et optionnellement SQLite."""
    out: dict[int, tuple[float, float]] = {}
    work = Path(work_dir)
    path = work / "wta_rankings_current.csv"
    if path.is_file():
        try:
            rk = pd.read_csv(path, low_memory=False)
            id_col = "player_id" if "player_id" in rk.columns else ("player" if "player" in rk.columns else None)
            rank_col = "rank" if "rank" in rk.columns else ("ranking" if "ranking" in rk.columns else None)
            pts_col = "points" if "points" in rk.columns else None
            if id_col and rank_col:
                for _, row in rk.iterrows():
                    try:
                        pid = int(float(row[id_col]))
                        rank = float(row[rank_col])
                    except (TypeError, ValueError):
                        continue
                    if rank <= 0 or rank >= 1500:
                        continue
                    pts = float("nan")
                    if pts_col and pd.notna(row.get(pts_col)):
                        try:
                            pts = float(row[pts_col])
                        except (TypeError, ValueError):
                            pass
                    out[pid] = (rank, pts)
        except Exception:
            pass

    if db_path and os.path.isfile(db_path):
        try:
            import sqlite3

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT player_id, ranking, points FROM rankings_wta_current "
                    "WHERE ranking IS NOT NULL AND ranking > 0 AND ranking < 1500"
                ).fetchall()
                for pid, rank, pts in rows:
                    try:
                        pid_i = int(pid)
                        rank_f = float(rank)
                        pts_f = float(pts) if pts is not None else float("nan")
                    except (TypeError, ValueError):
                        continue
                    prev = out.get(pid_i)
                    if prev is None or rank_f < prev[0]:
                        out[pid_i] = (rank_f, pts_f)
            except Exception:
                pass
            finally:
                conn.close()
        except Exception:
            pass
    return out


def build_te_cache_rank_maps(
    work_dir: Path | str | None = None,
    name_to_id: dict[str, int] | None = None,
    *,
    cache_dir: Path | str | None = None,
) -> tuple[dict[int, tuple[float, float]], dict[str, tuple[float, float]]]:
    """(by_player_id, by_norm_name) depuis ``data/cache/player_*.json`` (TE)."""
    by_pid: dict[int, tuple[float, float]] = {}
    by_name: dict[str, tuple[float, float]] = {}
    cache = Path(cache_dir or Path("data") / "cache")
    if not cache.is_dir():
        return by_pid, by_name

    name_to_id = dict(name_to_id or {})
    work = Path(work_dir) if work_dir else None
    if work and (work / "wta_players.csv").is_file():
        try:
            pdf = pd.read_csv(work / "wta_players.csv", low_memory=False)
            for _, row in pdf.iterrows():
                fn = str(row.get("first_name") or "").strip()
                ln = str(row.get("last_name") or "").strip()
                try:
                    pid = int(float(row.get("player_id")))
                except (TypeError, ValueError):
                    continue
                from scripts.player_identity import canonical_name, to_lastname_initial

                for nm in (f"{fn} {ln}".strip(), ln):
                    nk = canonical_name(to_lastname_initial(nm)) or nm.lower()
                    if nk:
                        name_to_id.setdefault(nk, pid)
        except Exception:
            pass

    for path in cache.glob("player_*.json"):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        rank = data.get("rank")
        try:
            rank_i = int(rank)
        except (TypeError, ValueError):
            continue
        if rank_i <= 0 or rank_i >= 1500:
            continue
        slug = path.stem[7:] if path.stem.startswith("player_") else path.stem
        norm_slug = re.sub(r"[^a-z0-9]+", "", slug.lower())
        pid: int | None = None
        for norm, p in name_to_id.items():
            if norm_slug and (norm_slug in norm or norm in norm_slug):
                pid = int(p)
                break
        pts = float("nan")
        if pid is not None:
            by_pid[pid] = (float(rank_i), pts)
        nk_slug = norm_name_key(slug.replace("-", " "))
        if nk_slug and te_name_key_safe(nk_slug):
            by_name[nk_slug] = (float(rank_i), pts)
    return by_pid, by_name


def fill_ranks_if_missing(
    row: dict[str, Any],
    *,
    tourney_date: int,
    rank_history: dict[int, list[tuple[int, float, float]]],
    current_rankings: dict[int, tuple[float, float]] | None = None,
    name_to_id: dict[str, int] | None = None,
    te_rank_by_pid: dict[int, tuple[float, float]] | None = None,
    te_rank_by_name: dict[str, tuple[float, float]] | None = None,
    wta_players_last_index: dict[tuple[str, str], int] | None = None,
) -> int:
    """Complète winner/loser rank (+ points) depuis historique, forward, classement ou cache TE."""
    filled = 0
    cur = current_rankings or {}
    te_pid = te_rank_by_pid or {}
    te_name = te_rank_by_name or {}
    for side, pid_col, rank_col, pts_col in (
        ("winner", "winner_id", "winner_rank", "winner_rank_points"),
        ("loser", "loser_id", "loser_rank", "loser_rank_points"),
    ):
        if _rank_val_ok(row.get(rank_col)):
            continue
        pid = resolve_player_id_for_rank(row, side, name_to_id)
        if pid is None:
            try:
                pid = int(float(row.get(pid_col)))
            except (TypeError, ValueError):
                pid = None
        rank, pts = None, None
        if pid is not None and pid < 900000:
            rank, pts = lookup_rank_at(rank_history, pid, tourney_date)
            if rank is None:
                rank, pts = lookup_rank_forward(rank_history, pid, tourney_date)
            if rank is None and pid in cur:
                rank, pts = cur[pid]
            if rank is None and pid in te_pid:
                rank, pts = te_pid[pid]
        if rank is None:
            try:
                from scripts.wta_name_aliases import canonical_wta_name_key

                nk = canonical_wta_name_key(row.get(f"{side}_name"))
            except Exception:
                nk = norm_name_key(row.get(f"{side}_name"))
            if nk and nk in te_name and te_name_key_safe(nk):
                rank, pts = te_name[nk]
        if rank is None:
            te_hit = lookup_te_rank_fuzzy(
                row.get(f"{side}_name"),
                te_rank_by_name=te_name,
                te_rank_by_pid=te_pid,
                name_to_id=name_to_id,
                wta_players_last_index=wta_players_last_index,
            )
            if te_hit is not None:
                rank, pts = te_hit
        if rank is not None:
            row[rank_col] = rank
            filled += 1
            if pts is not None and not _rank_val_ok(row.get(pts_col)):
                row[pts_col] = pts
                filled += 1
    return filled


def row_completeness_score(row: dict | pd.Series) -> int:
    """Plus haut = ligne préférée en cas de doublon dédup."""
    score = 0
    for col in SERVE_COLS:
        v = row.get(col)
        if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip():
            score += 2
    for col in ("winner_rank", "loser_rank", "winner_hand", "winner_ht", "score"):
        v = row.get(col)
        if v is not None and not (isinstance(v, float) and pd.isna(v)) and str(v).strip():
            score += 1
    try:
        if int(float(row.get("winner_id") or 0)) < 900000:
            score += 3
    except (TypeError, ValueError):
        pass
    return score


def build_wta_players_table(profiles: dict[int, dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for pid in sorted(profiles):
        p = profiles[pid]
        rows.append(
            {
                "player_id": pid,
                "name": p.get("name"),
                "hand": p.get("hand"),
                "ht": p.get("ht"),
                "ioc": p.get("ioc"),
                "last_age": p.get("age"),
                "last_match_date": p.get("last_tourney_date"),
            }
        )
    return pd.DataFrame(rows)
