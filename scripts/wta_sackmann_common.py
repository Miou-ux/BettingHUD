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
            prev = out.get(nk)
            if prev is None or td >= prev[0]:
                out[nk] = (td, age_f)
    return out


def estimate_age(name: str, tourney_date: int, lookup: dict[str, tuple[int, float]], default: float = 25.0) -> float:
    nk = norm_name_key(name)
    rec = lookup.get(nk)
    if not rec:
        return default
    last_td, last_age = rec
    days = max(0, tourney_date - last_td)
    return round(last_age + days / 365.25, 2)


def next_synthetic_player_id(existing: set[int]) -> int:
    base = 920000
    while base in existing:
        base += 1
    existing.add(base)
    return base
