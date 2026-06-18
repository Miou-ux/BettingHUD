#!/usr/bin/env python3
"""Contrôles automatiques checklist delta WTA (sections A/B/C/D/G).

Usage :
  python scripts/check_wta_delta_acceptance.py
  python scripts/check_wta_delta_acceptance.py --raw-dir /opt/bettinghud/data/raw/tennis_wta
  python scripts/check_wta_delta_acceptance.py --db-path data/bettinghud.db --run-ingest-smoke
  python scripts/check_wta_delta_acceptance.py --json

Voir scripts/_wta_delta_acceptance.md pour les seuils et go/no-go.
Pour le postulat « sans perte Brier » : scripts/_wta_delta_brier_plan.md et --brier-gate (défaut).
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd

def _resolve_root() -> Path:
    here = Path(__file__).resolve().parents[1]
    if (here / "scripts" / "ingest_sackmann_wta.py").is_file():
        return here
    cwd = Path.cwd()
    if (cwd / "scripts" / "ingest_sackmann_wta.py").is_file():
        return cwd
    return here


ROOT = _resolve_root()
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger(__name__)

Status = Literal["PASS", "WARN", "FAIL", "SKIP", "N/A"]

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

VALID_SURFACES = frozenset({"Hard", "Clay", "Grass", "Carpet"})
VALID_ROUNDS = frozenset(
    {
        "R128",
        "R64",
        "R32",
        "R16",
        "QF",
        "SF",
        "F",
        "RR",
        "BR",
        "Q",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "R1",
        "R2",
        "R3",
        "ER",
        "Unknown",
    }
)

_WTA_CSV_YEAR_RE = re.compile(r"(\d{4})\.csv$", re.IGNORECASE)
DEFAULT_CUTOFF = os.getenv("BETTINGHUD_WTA_SACKMANN_CUTOFF", "20260526")
DEFAULT_MIN_YEAR = int(os.getenv("BETTINGHUD_WTA_SACKMANN_MIN_YEAR", "2010"))


@dataclass
class CheckResult:
    code: str
    label: str
    status: Status
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)


def _csv_file_year(path: str) -> int | None:
    m = _WTA_CSV_YEAR_RE.search(os.path.basename(path))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def list_wta_match_csv_files(raw_dir: str, min_year: int) -> list[str]:
    files = sorted(glob.glob(os.path.join(raw_dir, "wta_matches_*.csv")))
    out: list[str] = []
    for f in files:
        if "doubles" in os.path.basename(f).lower():
            continue
        y = _csv_file_year(f)
        if y is not None and y < int(min_year):
            continue
        out.append(f)
    return out


def load_match_frames(raw_dir: str, min_year: int) -> tuple[pd.DataFrame, list[str]]:
    files = list_wta_match_csv_files(raw_dir, min_year)
    if not files:
        raise FileNotFoundError(f"Aucun wta_matches_*.csv dans {raw_dir}")
    dfs: list[pd.DataFrame] = []
    for p in files:
        try:
            dfs.append(pd.read_csv(p, low_memory=False))
        except Exception as exc:
            LOGGER.warning("Skip %s: %s", p, exc)
    if not dfs:
        raise RuntimeError("Impossible de lire les CSV WTA.")
    out = pd.concat(dfs, ignore_index=True)
    out["tourney_date"] = pd.to_datetime(out["tourney_date"], format="%Y%m%d", errors="coerce")
    out = out.dropna(subset=["tourney_date"])
    out = out.loc[out["tourney_date"].dt.year >= int(min_year)].reset_index(drop=True)
    return out, files


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _match_key_row(row: pd.Series) -> tuple:
    td = row["tourney_date"]
    if isinstance(td, pd.Timestamp):
        td_int = int(td.strftime("%Y%m%d"))
    else:
        td_int = int(td)
    return (
        td_int,
        str(row.get("tourney_name", "") or "").strip(),
        str(row.get("winner_name", "") or "").strip(),
        str(row.get("loser_name", "") or "").strip(),
    )


def _status_band(value: float, *, pass_ge: float | None = None, warn_ge: float | None = None,
                 pass_le: float | None = None, warn_le: float | None = None,
                 higher_is_better: bool = True) -> Status:
    if higher_is_better:
        if pass_ge is not None and value >= pass_ge:
            return "PASS"
        if warn_ge is not None and value >= warn_ge:
            return "WARN"
        return "FAIL"
    if pass_le is not None and value <= pass_le:
        return "PASS"
    if warn_le is not None and value <= warn_le:
        return "WARN"
    return "FAIL"


def _days_since_max(df: pd.DataFrame, mask: pd.Series | None = None) -> int | None:
    sub = df if mask is None else df.loc[mask]
    if sub.empty:
        return None
    mx = sub["tourney_date"].max()
    if pd.isna(mx):
        return None
    return (date.today() - mx.date()).days


def detect_cutoff_date(raw_dir: str, explicit: str | None) -> int:
    if explicit:
        return int(explicit)
    env = os.getenv("BETTINGHUD_WTA_SACKMANN_CUTOFF", "").strip()
    if env:
        return int(env)
    # Dernier fetch documenté dans logs ou max date archive — défaut conservateur
    return int(DEFAULT_CUTOFF)


def check_a1_backup(raw_dir: Path, backup_root: Path) -> CheckResult:
    if not backup_root.is_dir():
        return CheckResult(
            "A1",
            "Backup archive < 24 h",
            "FAIL",
            f"dossier backup absent ({backup_root})",
        )
    archives = sorted(backup_root.glob("wta_sackmann_*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not archives:
        return CheckResult("A1", "Backup archive < 24 h", "FAIL", "aucune archive wta_sackmann_*.tar.gz")
    latest = archives[0]
    age_h = (datetime.now().timestamp() - latest.stat().st_mtime) / 3600.0
    manifest = latest.with_suffix("").with_suffix(".manifest.json")
    if not manifest.exists():
        manifest = Path(str(latest) + ".manifest.json")
    detail = f"{latest.name} ({age_h:.1f} h)"
    if age_h <= 24:
        return CheckResult("A1", "Backup archive < 24 h", "PASS", detail, {"age_hours": age_h})
    if age_h <= 72:
        return CheckResult("A1", "Backup archive < 24 h", "WARN", detail, {"age_hours": age_h})
    return CheckResult("A1", "Backup archive < 24 h", "FAIL", detail, {"age_hours": age_h})


def check_a2_schema(files: list[str]) -> CheckResult:
    if not files:
        return CheckResult("A2", "Schéma 49 colonnes", "FAIL", "aucun fichier")
    bad: list[str] = []
    for p in files[:5]:
        try:
            cols = list(pd.read_csv(p, nrows=0).columns)
        except Exception as exc:
            bad.append(f"{os.path.basename(p)}: {exc}")
            continue
        if list(cols) != list(SACKMANN_COLUMNS):
            bad.append(f"{os.path.basename(p)}: {len(cols)} cols (attendu 49)")
    if bad:
        return CheckResult("A2", "Schéma 49 colonnes", "FAIL", "; ".join(bad[:3]))
    return CheckResult("A2", "Schéma 49 colonnes", "PASS", f"OK sur {min(5, len(files))} fichier(s) échantillon")


def check_b_freshness(df: pd.DataFrame, files: list[str]) -> list[CheckResult]:
    main_mask = pd.Series(False, index=df.index)
    qual_mask = pd.Series(False, index=df.index)
    for p in files:
        bn = os.path.basename(p).lower()
        if "qual_itf" in bn:
            y = _csv_file_year(p)
            if y:
                qual_mask |= df["tourney_date"].dt.year == y
        elif "doubles" not in bn:
            y = _csv_file_year(p)
            if y:
                main_mask |= df["tourney_date"].dt.year == y

    out: list[CheckResult] = []
    d_main = _days_since_max(df, main_mask)
    if d_main is None:
        out.append(CheckResult("B1", "Fraîcheur main tour", "FAIL", "aucune donnée main"))
    elif d_main <= 7:
        out.append(CheckResult("B1", "Fraîcheur main tour", "PASS", f"{d_main} j", {"days": d_main}))
    elif d_main <= 14:
        out.append(CheckResult("B1", "Fraîcheur main tour", "WARN", f"{d_main} j", {"days": d_main}))
    else:
        out.append(CheckResult("B1", "Fraîcheur main tour", "FAIL", f"{d_main} j", {"days": d_main}))

    d_qual = _days_since_max(df, qual_mask)
    if d_qual is None:
        out.append(CheckResult("B2", "Fraîcheur qual/ITF", "SKIP", "pas de qual_itf"))
    elif d_qual <= 10:
        out.append(CheckResult("B2", "Fraîcheur qual/ITF", "PASS", f"{d_qual} j", {"days": d_qual}))
    elif d_qual <= 21:
        out.append(CheckResult("B2", "Fraîcheur qual/ITF", "WARN", f"{d_qual} j", {"days": d_qual}))
    else:
        out.append(CheckResult("B2", "Fraîcheur qual/ITF", "FAIL", f"{d_qual} j", {"days": d_qual}))
    return out


def load_tennis_data_wta_matches(tennis_data_dir: str, cutoff_int: int) -> pd.DataFrame:
    cutoff_ts = pd.Timestamp(str(cutoff_int))
    years = sorted({cutoff_ts.year, date.today().year})
    frames: list[pd.DataFrame] = []
    for year in years:
        path = os.path.join(tennis_data_dir, f"{year}.xlsx")
        if not os.path.isfile(path):
            continue
        try:
            sheets = pd.read_excel(path, sheet_name=None)
        except Exception as exc:
            LOGGER.warning("read_excel %s: %s", path, exc)
            continue
        for sheet_df in sheets.values():
            if not isinstance(sheet_df, pd.DataFrame) or sheet_df.empty:
                continue
            cols = {c.lower(): c for c in sheet_df.columns}
            if "winner" not in cols or "loser" not in cols or "date" not in cols:
                continue
            tmp = sheet_df.copy()
            tmp["_date"] = pd.to_datetime(tmp[cols["date"]], errors="coerce", dayfirst=True)
            tmp = tmp[tmp["_date"] >= cutoff_ts]
            if tmp.empty:
                continue
            tier_col = cols.get("tier") or cols.get("series")
            if tier_col:
                tier = tmp[tier_col].astype(str).str.upper()
                tmp = tmp[~tier.str.contains("CHALLENGER|ITF|EXHIB", na=False, regex=True)]
            tmp["winner_name"] = tmp[cols["winner"]].astype(str).str.strip()
            tmp["loser_name"] = tmp[cols["loser"]].astype(str).str.strip()
            tmp["tourney_date"] = tmp["_date"]
            frames.append(tmp[["tourney_date", "winner_name", "loser_name"]])
    if not frames:
        return pd.DataFrame(columns=["tourney_date", "winner_name", "loser_name"])
    return pd.concat(frames, ignore_index=True)


def check_b4_tennis_data(
    df: pd.DataFrame, tennis_data_dir: str, cutoff_int: int, main_only: pd.DataFrame
) -> CheckResult:
    td = load_tennis_data_wta_matches(tennis_data_dir, cutoff_int)
    if td.empty:
        return CheckResult(
            "B4",
            "Delta vs tennis-data",
            "SKIP",
            f"aucun xlsx WTA dans {tennis_data_dir} (post-cutoff)",
        )
    delta = main_only[main_only["tourney_date"] >= pd.Timestamp(str(cutoff_int))].copy()
    if delta.empty:
        return CheckResult("B4", "Delta vs tennis-data", "SKIP", "aucun match CSV post-cutoff (main)")

    csv_keys = {
        (_norm_name(w), _norm_name(l), int(d.strftime("%Y%m%d")))
        for d, w, l in zip(delta["tourney_date"], delta["winner_name"], delta["loser_name"])
        if w and l
    }
    td_keys = {
        (_norm_name(w), _norm_name(l), int(d.strftime("%Y%m%d")))
        for d, w, l in zip(td["tourney_date"], td["winner_name"], td["loser_name"])
        if w and l
    }
    if not td_keys:
        return CheckResult("B4", "Delta vs tennis-data", "SKIP", "tennis-data vide post-cutoff")
    matched = 0
    for w, l, d in td_keys:
        if (w, l, d) in csv_keys or (l, w, d) in csv_keys:
            matched += 1
    rate = matched / len(td_keys) if td_keys else 0.0
    pct = round(rate * 100, 2)
    st = _status_band(pct, pass_ge=98.0, warn_ge=95.0)
    return CheckResult(
        "B4",
        "Delta vs tennis-data",
        st,
        f"{pct}% ({matched}/{len(td_keys)} matchs TD couverts)",
        {"coverage_pct": pct, "td_total": len(td_keys), "matched": matched},
    )


def check_c_integrity(df: pd.DataFrame, delta: pd.DataFrame, main_delta: pd.DataFrame, files: list[str]) -> list[CheckResult]:
    out: list[CheckResult] = []
    key_cols = ["tourney_date", "tourney_name", "winner_name", "loser_name"]
    dupes = int(df.duplicated(subset=key_cols).sum())
    out.append(
        CheckResult(
            "C1",
            "Doublons clé dédup",
            "PASS" if dupes == 0 else "FAIL",
            f"{dupes} doublon(s)",
            {"dupes": dupes},
        )
    )

    invalid_dates = int(df["tourney_date"].isna().sum())
    out.append(
        CheckResult(
            "C2",
            "tourney_date valide",
            "PASS" if invalid_dates == 0 else "FAIL",
            f"{invalid_dates} invalide(s)",
        )
    )

    empty_names = int(
        (df["winner_name"].astype(str).str.strip() == "").sum()
        + (df["loser_name"].astype(str).str.strip() == "").sum()
    )
    out.append(
        CheckResult(
            "C3",
            "Noms winner/loser",
            "PASS" if empty_names == 0 else "FAIL",
            f"{empty_names} vide(s)",
        )
    )

    if len(delta):
        surf = delta["surface"].astype(str).str.strip()
        filled = surf.isin(VALID_SURFACES) | (surf == "") | surf.isna()
        rate = float(filled.mean()) * 100
        st = _status_band(rate, pass_ge=99.0, warn_ge=95.0)
        out.append(CheckResult("C4", "Surface delta", st, f"{rate:.1f}% OK", {"pct": rate}))
    else:
        out.append(CheckResult("C4", "Surface delta", "SKIP", "delta vide"))

    if len(delta):
        rounds = delta["round"].astype(str).str.strip()
        ok = rounds.isin(VALID_ROUNDS) | rounds.str.match(r"^R\d+$", na=False)
        bad_rate = 1.0 - float(ok.mean())
        if bad_rate <= 0.05:
            st: Status = "PASS"
        elif bad_rate <= 0.20:
            st = "WARN"
        else:
            st = "FAIL"
        out.append(CheckResult("C5", "Round cohérent", st, f"{bad_rate*100:.1f}% incohérent"))
    else:
        out.append(CheckResult("C5", "Round cohérent", "SKIP", "delta vide"))

    if len(main_delta):
        score_ok = main_delta["score"].notna() & (main_delta["score"].astype(str).str.strip() != "")
        rate = float(score_ok.mean()) * 100
        st = _status_band(rate, pass_ge=95.0, warn_ge=90.0)
        out.append(CheckResult("C6", "Score main tour", st, f"{rate:.1f}%", {"pct": rate}))
    else:
        out.append(CheckResult("C6", "Score main tour", "SKIP", "delta main vide"))

    misplaced = 0
    for p in files:
        bn = os.path.basename(p)
        y = _csv_file_year(p)
        if y is None:
            continue
        try:
            sample = pd.read_csv(p, usecols=["tourney_date"], low_memory=False)
            years = pd.to_datetime(sample["tourney_date"], format="%Y%m%d", errors="coerce").dt.year
            wrong = int((years.notna() & (years != y)).sum())
            misplaced += wrong
        except Exception:
            continue
    out.append(
        CheckResult(
            "C7",
            "Fichier année correct",
            "PASS" if misplaced == 0 else "FAIL",
            f"{misplaced} ligne(s) hors année fichier",
            {"misplaced": misplaced},
        )
    )
    return out


def check_d_ranks(
    main_delta: pd.DataFrame,
    raw_dir: str,
    rankings_path: Path | None,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    if main_delta.empty:
        for code, label in [("D1", "Rangs main"), ("D3", "Points rang"), ("D4", "Âges"), ("D5", "Âge plausible")]:
            out.append(CheckResult(code, label, "SKIP", "delta main vide"))
        out.append(CheckResult("D2", "Écart vs rankings_wta_current", "SKIP", "delta main vide"))
        return out

    wr = main_delta["winner_rank"].notna() & (main_delta["winner_rank"].astype(str).str.strip() != "")
    lr = main_delta["loser_rank"].notna() & (main_delta["loser_rank"].astype(str).str.strip() != "")
    rank_rate = float((wr & lr).mean()) * 100
    st = _status_band(rank_rate, pass_ge=90.0, warn_ge=80.0)
    out.append(CheckResult("D1", "Rangs main delta", st, f"{rank_rate:.1f}%", {"pct": rank_rate}))

    # D2 — matchs < 30 j vs rankings file
    recent = main_delta[main_delta["tourney_date"] >= (pd.Timestamp.today() - pd.Timedelta(days=30))]
    rk_file = rankings_path or Path(raw_dir) / "wta_rankings_current.csv"
    if recent.empty or not rk_file.is_file():
        out.append(CheckResult("D2", "Écart vs rankings_wta_current", "SKIP", "pas de match < 30 j ou CSV absent"))
    else:
        try:
            rk = pd.read_csv(rk_file)
            id_col = "player_id" if "player_id" in rk.columns else "player"
            rank_col = "rank" if "rank" in rk.columns else "ranking"
            rk_map = dict(zip(rk[id_col].astype(int), rk[rank_col].astype(float)))
            deltas: list[int] = []
            for _, row in recent.iterrows():
                for pid_col, rank_col_m in (("winner_id", "winner_rank"), ("loser_id", "loser_rank")):
                    try:
                        pid = int(row[pid_col])
                        actual = float(row[rank_col_m])
                    except (TypeError, ValueError):
                        continue
                    if pid in rk_map and pd.notna(actual):
                        deltas.append(abs(int(rk_map[pid]) - int(actual)))
            if not deltas:
                out.append(CheckResult("D2", "Écart vs rankings_wta_current", "SKIP", "pas de paires comparables"))
            else:
                within3 = sum(1 for d in deltas if d <= 3) / len(deltas) * 100
                bad10 = sum(1 for d in deltas if d > 10) / len(deltas) * 100
                if within3 >= 95:
                    st2: Status = "PASS"
                elif bad10 > 5:
                    st2 = "FAIL"
                else:
                    st2 = "WARN"
                out.append(
                    CheckResult(
                        "D2",
                        "Écart vs rankings_wta_current",
                        st2,
                        f"{within3:.1f}% |Δ|≤3 ; {bad10:.1f}% |Δ|>10",
                        {"within3_pct": within3, "bad10_pct": bad10},
                    )
                )
        except Exception as exc:
            out.append(CheckResult("D2", "Écart vs rankings_wta_current", "SKIP", str(exc)))

    wp = main_delta["winner_rank_points"].notna() & (main_delta["winner_rank_points"].astype(str).str.strip() != "")
    lp = main_delta["loser_rank_points"].notna() & (main_delta["loser_rank_points"].astype(str).str.strip() != "")
    pts_rate = float((wp & lp).mean()) * 100
    st3 = _status_band(pts_rate, pass_ge=85.0, warn_ge=70.0)
    out.append(CheckResult("D3", "Points rang delta", st3, f"{pts_rate:.1f}%", {"pct": pts_rate}))

    wa = main_delta["winner_age"].notna() & (main_delta["winner_age"].astype(str).str.strip() != "")
    la = main_delta["loser_age"].notna() & (main_delta["loser_age"].astype(str).str.strip() != "")
    age_rate = float((wa & la).mean()) * 100
    st4 = _status_band(age_rate, pass_ge=80.0, warn_ge=60.0)
    out.append(CheckResult("D4", "Âges delta", st4, f"{age_rate:.1f}%", {"pct": age_rate}))

    ages = pd.concat([main_delta["winner_age"], main_delta["loser_age"]], ignore_index=True)
    ages = pd.to_numeric(ages, errors="coerce").dropna()
    if ages.empty:
        out.append(CheckResult("D5", "Âge plausible [15,45]", "SKIP", "aucun âge"))
    else:
        bad = int(((ages < 15) | (ages > 45)).sum())
        out.append(
            CheckResult(
                "D5",
                "Âge plausible [15,45]",
                "PASS" if bad == 0 else "FAIL",
                f"{bad} hors bornes / {len(ages)}",
                {"bad": bad},
            )
        )
    return out


SERVE_INT_COLS: tuple[str, ...] = (
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


def _null_rate(series: pd.Series) -> float:
    if len(series) == 0:
        return 100.0
    s = series.replace("", pd.NA)
    return float(s.isna().mean() * 100.0)


def _serve_block_complete(row: pd.Series) -> bool:
    for col in SERVE_INT_COLS:
        v = row.get(col)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return False
        if str(v).strip() == "":
            return False
    return True


def _serve_coherence_violations(df: pd.DataFrame) -> int:
    """Compte les lignes avec stats serve incohérentes (ace > svpt, etc.)."""
    if df.empty:
        return 0
    sub = df[df["w_svpt"].notna() & df["l_svpt"].notna()].copy()
    if sub.empty:
        return 0
    violations = 0
    for _, row in sub.iterrows():
        try:
            w_svpt = float(row["w_svpt"])
            l_svpt = float(row["l_svpt"])
            w_ace = float(row.get("w_ace") or 0)
            l_ace = float(row.get("l_ace") or 0)
            w_1stin = float(row.get("w_1stIn") or 0)
            l_1stin = float(row.get("l_1stIn") or 0)
            w_bps = float(row.get("w_bpSaved") or 0)
            w_bpf = float(row.get("w_bpFaced") or 0)
            l_bps = float(row.get("l_bpSaved") or 0)
            l_bpf = float(row.get("l_bpFaced") or 0)
            if w_ace > w_svpt or l_ace > l_svpt:
                violations += 1
                continue
            if w_1stin > w_svpt or l_1stin > l_svpt:
                violations += 1
                continue
            if w_bps > w_bpf or l_bps > l_bpf:
                violations += 1
                continue
        except (TypeError, ValueError):
            violations += 1
    return violations


def check_j_brier_preservation(
    df: pd.DataFrame,
    main_df: pd.DataFrame,
    main_delta: pd.DataFrame,
    cutoff_ts: pd.Timestamp,
) -> list[CheckResult]:
    """Sections J1–J4 : gate « alimenter Sackmann sans dégrader le Brier ».

    Voir scripts/_wta_delta_brier_plan.md
    """
    out: list[CheckResult] = []
    if main_delta.empty:
        for code, label in [
            ("J1", "Champs bloquants ML (delta main)"),
            ("J2", "Points rang delta main"),
            ("J3", "Stats serve delta main"),
            ("J4", "Stabilité NULL w_svpt global"),
        ]:
            out.append(CheckResult(code, label, "SKIP", "delta main vide"))
        return out

    # J1 — dropna fields in prepare_data
    j1_specs = [
        ("J1.1", "winner_rank + loser_rank", "winner_rank", "loser_rank"),
        ("J1.2", "winner_age + loser_age", "winner_age", "loser_age"),
        ("J1.3", "surface", "surface", None),
    ]
    for code, label, c1, c2 in j1_specs:
        if c2:
            ok = main_delta[c1].notna() & main_delta[c2].notna()
            ok &= main_delta[c1].astype(str).str.strip().ne("")
            ok &= main_delta[c2].astype(str).str.strip().ne("")
        else:
            ok = main_delta[c1].notna() & main_delta[c1].astype(str).str.strip().ne("")
        rate = float(ok.mean()) * 100
        st: Status = "PASS" if rate >= 100.0 else "FAIL"
        out.append(CheckResult(code, label, st, f"{rate:.1f}%", {"pct": rate}))

    td_ok = main_delta["tourney_date"].notna()
    rate_td = float(td_ok.mean()) * 100
    out.append(
        CheckResult(
            "J1.4",
            "tourney_date valide",
            "PASS" if rate_td >= 100.0 else "FAIL",
            f"{rate_td:.1f}%",
            {"pct": rate_td},
        )
    )

    # J2 — rank points
    wp = main_delta["winner_rank_points"].notna() & main_delta["winner_rank_points"].astype(str).str.strip().ne("")
    lp = main_delta["loser_rank_points"].notna() & main_delta["loser_rank_points"].astype(str).str.strip().ne("")
    pts_rate = float((wp & lp).mean()) * 100
    out.append(
        CheckResult(
            "J2.1",
            "Points rang delta main",
            _status_band(pts_rate, pass_ge=90.0, warn_ge=80.0),
            f"{pts_rate:.1f}%",
            {"pct": pts_rate},
        )
    )

    # J3 — serve stats on main delta
    w_svpt_ok = main_delta["w_svpt"].notna() & main_delta["l_svpt"].notna()
    w_svpt_ok &= main_delta["w_svpt"].astype(str).str.strip().ne("")
    w_svpt_ok &= main_delta["l_svpt"].astype(str).str.strip().ne("")
    svpt_rate = float(w_svpt_ok.mean()) * 100
    out.append(
        CheckResult(
            "J3.1",
            "w_svpt + l_svpt delta main",
            _status_band(svpt_rate, pass_ge=90.0, warn_ge=75.0),
            f"{svpt_rate:.1f}%",
            {"pct": svpt_rate},
        )
    )

    complete = main_delta.apply(_serve_block_complete, axis=1)
    block_rate = float(complete.mean()) * 100
    out.append(
        CheckResult(
            "J3.2",
            "Bloc serve complet delta main",
            _status_band(block_rate, pass_ge=85.0, warn_ge=70.0),
            f"{block_rate:.1f}%",
            {"pct": block_rate},
        )
    )

    # w_svpt >= 15 for micro-Elo reliability
    def _svpt_vol_ok(v) -> bool:
        try:
            return float(v) >= 15.0
        except (TypeError, ValueError):
            return False

    with_svpt = main_delta.loc[w_svpt_ok]
    if len(with_svpt) == 0:
        out.append(CheckResult("J3.3", "w_svpt >= 15 (volume micro-Elo)", "FAIL", "aucune ligne avec svpt"))
    else:
        vol_rate = float(with_svpt["w_svpt"].apply(_svpt_vol_ok).mean()) * 100
        out.append(
            CheckResult(
                "J3.3",
                "w_svpt >= 15 (volume micro-Elo)",
                _status_band(vol_rate, pass_ge=95.0, warn_ge=90.0),
                f"{vol_rate:.1f}%",
                {"pct": vol_rate},
            )
        )

    viol = _serve_coherence_violations(main_delta)
    out.append(
        CheckResult(
            "J3.4",
            "Cohérence stats serve",
            "PASS" if viol == 0 else "FAIL",
            f"{viol} violation(s)",
            {"violations": viol},
        )
    )

    # J4 — global null w_svpt shift vs pre-cutoff baseline
    pre = main_df[main_df["tourney_date"] < cutoff_ts]
    if pre.empty:
        out.append(CheckResult("J4.1", "Delta NULL w_svpt global vs baseline", "SKIP", "pas de baseline pre-cutoff"))
    else:
        base_null = _null_rate(pre["w_svpt"])
        all_null = _null_rate(main_df["w_svpt"])
        delta_null = _null_rate(main_delta["w_svpt"])
        shift = all_null - base_null
        st = _status_band(shift, pass_le=3.0, warn_le=5.0, higher_is_better=False)
        out.append(
            CheckResult(
                "J4.1",
                "Delta NULL w_svpt global vs baseline",
                st,
                f"baseline={base_null:.1f}% global={all_null:.1f}% delta={delta_null:.1f}% shift={shift:+.1f}pp",
                {"baseline_null_pct": base_null, "global_null_pct": all_null, "delta_null_pct": delta_null, "shift_pp": shift},
            )
        )

    return out


def check_g_pipeline(
    *,
    raw_dir: str,
    db_path: str | None,
    min_year: int,
    df: pd.DataFrame,
    run_ingest_smoke: bool,
    tennis_data_dir: str,
) -> list[CheckResult]:
    out: list[CheckResult] = []
    if not db_path:
        for code in ("G1", "G2", "G3", "G6"):
            out.append(CheckResult(code, f"Pipeline {code}", "SKIP", "pas de --db-path"))
        return out

    if run_ingest_smoke:
        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "ingest_sackmann_wta.py"),
            "--raw-dir",
            raw_dir,
            "--db-path",
            db_path,
            "--min-year",
            str(min_year),
        ]
        try:
            subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True, timeout=600)
            out.append(CheckResult("G1", "ingest_sackmann_wta", "PASS", "OK"))
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "")[:300]
            out.append(CheckResult("G1", "ingest_sackmann_wta", "FAIL", err or f"exit {exc.returncode}"))
        except Exception as exc:
            out.append(CheckResult("G1", "ingest_sackmann_wta", "FAIL", str(exc)))
    else:
        out.append(CheckResult("G1", "ingest_sackmann_wta", "SKIP", "ajouter --run-ingest-smoke"))

    if os.path.isfile(db_path):
        try:
            conn = sqlite3.connect(db_path)
            db_rows = conn.execute("SELECT COUNT(*) FROM wta_matches").fetchone()[0]
            conn.close()
            csv_rows = len(df)
            if db_rows == csv_rows:
                out.append(CheckResult("G2", "Row count SQLite vs CSV", "PASS", f"{db_rows}"))
            else:
                out.append(
                    CheckResult(
                        "G2",
                        "Row count SQLite vs CSV",
                        "FAIL",
                        f"db={db_rows} csv={csv_rows} (delta {db_rows - csv_rows:+d})",
                    )
                )
        except Exception as exc:
            out.append(CheckResult("G2", "Row count SQLite vs CSV", "SKIP", str(exc)))
    else:
        out.append(CheckResult("G2", "Row count SQLite vs CSV", "SKIP", f"db absent: {db_path}"))

    if run_ingest_smoke:
        try:
            from scripts.ml_model import TennisMLModel

            TennisMLModel().prepare_data()
            out.append(CheckResult("G3", "ml_model.prepare_data()", "PASS", "OK"))
        except Exception as exc:
            out.append(CheckResult("G3", "ml_model.prepare_data()", "FAIL", str(exc)[:400]))
    else:
        out.append(CheckResult("G3", "ml_model.prepare_data()", "SKIP", "ajouter --run-ingest-smoke"))

    # G6 — réconciliation 14 j
    try:
        from scripts.tennis_data_results import load_recent_results

        today = date.today()
        dates = [(today - timedelta(days=i)).isoformat() for i in range(14)]
        td_rows = [r for r in load_recent_results(dates) if r.get("tour") == "WTA"]
        if not td_rows:
            out.append(CheckResult("G6", "Réconciliation tennis-data 14 j", "SKIP", "aucune ligne TD"))
        elif not os.path.isfile(db_path):
            out.append(CheckResult("G6", "Réconciliation tennis-data 14 j", "SKIP", "db absent"))
        else:
            conn = sqlite3.connect(db_path)
            db = pd.read_sql_query(
                "SELECT tourney_date, winner_name, loser_name FROM wta_matches",
                conn,
            )
            conn.close()
            db["tourney_date"] = pd.to_datetime(db["tourney_date"], errors="coerce")
            db_keys = {
                (_norm_name(w), _norm_name(l), int(d.strftime("%Y%m%d")))
                for d, w, l in zip(db["tourney_date"], db["winner_name"], db["loser_name"])
                if pd.notna(d) and w and l
            }
            matched = 0
            for row in td_rows:
                d = int(row["match_date"].replace("-", ""))
                w, l = _norm_name(row["winner_name"]), _norm_name(row["loser_name"])
                if (w, l, d) in db_keys or (l, w, d) in db_keys:
                    matched += 1
            rate = matched / len(td_rows) * 100
            st = _status_band(rate, pass_ge=99.0, warn_ge=97.0)
            out.append(
                CheckResult(
                    "G6",
                    "Réconciliation tennis-data 14 j",
                    st,
                    f"{rate:.1f}% ({matched}/{len(td_rows)})",
                    {"pct": rate},
                )
            )
    except Exception as exc:
        out.append(CheckResult("G6", "Réconciliation tennis-data 14 j", "SKIP", str(exc)[:200]))

    return out


def run_checks(args: argparse.Namespace) -> dict[str, Any]:
    raw_dir = os.path.abspath(args.raw_dir)
    if not os.path.isdir(raw_dir):
        raise FileNotFoundError(f"raw-dir introuvable: {raw_dir}")

    cutoff_int = detect_cutoff_date(raw_dir, args.cutoff_date)
    df, files = load_match_frames(raw_dir, args.min_year)
    cutoff_ts = pd.Timestamp(str(cutoff_int))
    delta = df[df["tourney_date"] >= cutoff_ts].copy()

    main_files = [p for p in files if "qual_itf" not in os.path.basename(p).lower()]
    main_years = {_csv_file_year(p) for p in main_files if _csv_file_year(p)}
    main_mask = df["tourney_date"].dt.year.isin(main_years)
    main_df = df[main_mask].copy()
    main_delta = main_df[main_df["tourney_date"] >= cutoff_ts].copy()

    backup_root = Path(args.backup_dir)
    tennis_data_dir = args.tennis_data_dir
    if not os.path.isabs(tennis_data_dir):
        tennis_data_dir = os.path.join(str(ROOT), tennis_data_dir)
    tennis_data_dir = os.path.abspath(tennis_data_dir)

    results: list[CheckResult] = []
    results.append(check_a1_backup(Path(raw_dir), backup_root))
    results.append(check_a2_schema(files))
    results.extend(check_b_freshness(df, files))
    results.append(check_b4_tennis_data(df, tennis_data_dir, cutoff_int, main_df))
    results.extend(check_c_integrity(df, delta, main_delta, files))
    results.extend(check_d_ranks(main_delta, raw_dir, None))
    if args.brier_gate:
        results.extend(check_j_brier_preservation(df, main_df, main_delta, cutoff_ts))
    results.extend(
        check_g_pipeline(
            raw_dir=raw_dir,
            db_path=args.db_path,
            min_year=args.min_year,
            df=df,
            run_ingest_smoke=args.run_ingest_smoke,
            tennis_data_dir=tennis_data_dir,
        )
    )

    statuses = [r.status for r in results]
    has_fail = "FAIL" in statuses
    has_warn = "WARN" in statuses
    exit_code = 1 if has_fail else (2 if has_warn else 0)

    return {
        "raw_dir": raw_dir,
        "cutoff_date": cutoff_int,
        "rows_total": len(df),
        "rows_delta": len(delta),
        "max_tourney_date": df["tourney_date"].max().strftime("%Y-%m-%d") if len(df) else None,
        "checks": [
            {"code": r.code, "label": r.label, "status": r.status, "detail": r.detail, "metrics": r.metrics}
            for r in results
        ],
        "summary": {
            "pass": sum(1 for r in results if r.status == "PASS"),
            "warn": sum(1 for r in results if r.status == "WARN"),
            "fail": sum(1 for r in results if r.status == "FAIL"),
            "skip": sum(1 for r in results if r.status == "SKIP"),
        },
        "exit_code": exit_code,
    }


def _color_status(st: str) -> str:
    if not sys.stdout.isatty():
        return st
    colors = {"PASS": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m", "SKIP": "\033[90m", "N/A": "\033[90m"}
    reset = "\033[0m"
    return f"{colors.get(st, '')}{st}{reset}"


def print_report(report: dict[str, Any]) -> None:
    print(f"WTA delta acceptance — {report['raw_dir']}")
    print(f"  cutoff={report['cutoff_date']}  rows={report['rows_total']}  delta={report['rows_delta']}  max_date={report['max_tourney_date']}")
    print()
    print(f"{'Code':<5} {'Status':<6} {'Check':<32} Detail")
    print("-" * 90)
    for chk in report["checks"]:
        st = _color_status(chk["status"])
        print(f"{chk['code']:<5} {st:<6} {chk['label']:<32} {chk['detail']}")
    s = report["summary"]
    print()
    print(f"Résumé: PASS={s['pass']} WARN={s['warn']} FAIL={s['fail']} SKIP={s['skip']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Checklist automatique delta WTA Sackmann.")
    ap.add_argument("--raw-dir", default=os.path.join("data", "raw", "tennis_wta"))
    ap.add_argument("--db-path", default=None, help="SQLite pour G2/G6 (optionnel)")
    ap.add_argument("--cutoff-date", default=None, help="YYYYMMDD (déf. env ou 20260526)")
    ap.add_argument(
        "--tennis-data-dir",
        default=os.path.join("data", "raw", "tennis_data_wta"),
        help="Dossier xlsx tennis-data WTA",
    )
    ap.add_argument("--backup-dir", default=os.path.join("data", "backups", "wta_sackmann"))
    ap.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR)
    ap.add_argument("--run-ingest-smoke", action="store_true", help="Lance ingest + prepare_data (G1/G3)")
    ap.add_argument(
        "--brier-gate",
        action="store_true",
        help="Sections J1–J4 optionnelles (scripts/_wta_delta_brier_plan.md).",
    )
    ap.add_argument("--json", action="store_true", help="Sortie JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    try:
        report = run_checks(args)
    except FileNotFoundError as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
