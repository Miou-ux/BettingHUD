"""Shared helpers for WTA Sackmann delta pipeline (preprod)."""
from __future__ import annotations

import re
from datetime import datetime
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


def fill_ranks_if_missing(
    row: dict[str, Any],
    *,
    tourney_date: int,
    rank_history: dict[int, list[tuple[int, float, float]]],
    current_rankings: dict[int, tuple[float, float]] | None = None,
) -> int:
    """Complète winner/loser rank (+ points) depuis l'historique ou rankings courants. Retourne nb champs remplis."""
    filled = 0
    cur = current_rankings or {}
    for side, pid_col, rank_col, pts_col in (
        ("winner", "winner_id", "winner_rank", "winner_rank_points"),
        ("loser", "loser_id", "loser_rank", "loser_rank_points"),
    ):
        if _rank_val_ok(row.get(rank_col)):
            continue
        try:
            pid = int(float(row.get(pid_col)))
        except (TypeError, ValueError):
            continue
        rank, pts = lookup_rank_at(rank_history, pid, tourney_date)
        if rank is None and pid in cur:
            rank, pts = cur[pid]
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
