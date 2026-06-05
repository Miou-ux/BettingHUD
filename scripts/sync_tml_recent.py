import json
import math
import os
import glob
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib.request import urlopen

import pandas as pd

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_scripts_dir)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
from surface_speed import (  # noqa: E402
    effective_surface_speed_cpi,
    infer_outdoor,
    lookup_surface_speed,
)
from value_detector import ValueDetector  # noqa: E402
# Villes / fuseaux pour le module « fatigue de voyage » : `scripts.tournament_geo.TOURNAMENT_GPS`
# (matching sur `tourney_name`, pas d’API).


def ensure_surface_speed_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches_recent)").fetchall()}
    if "surface_speed" not in cols:
        conn.execute("ALTER TABLE matches_recent ADD COLUMN surface_speed REAL")
        conn.commit()


def ensure_table(conn):
    # Source unique TML: on repart d'une table propre pour éviter les schémas historiques.
    conn.execute("DROP TABLE IF EXISTS matches_recent")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches_recent (
            tourney_id TEXT,
            tourney_name TEXT,
            surface TEXT,
            draw_size TEXT,
            tourney_level TEXT,
            indoor TEXT,
            tourney_date TEXT,
            match_num INTEGER,
            winner_id INTEGER,
            winner_seed REAL,
            winner_entry TEXT,
            winner_name TEXT,
            winner_hand TEXT,
            winner_ht REAL,
            winner_ioc TEXT,
            winner_age REAL,
            winner_rank REAL,
            winner_rank_points REAL,
            loser_id INTEGER,
            loser_seed REAL,
            loser_entry TEXT,
            loser_name TEXT,
            loser_hand TEXT,
            loser_ht REAL,
            loser_ioc TEXT,
            loser_age REAL,
            loser_rank REAL,
            loser_rank_points REAL,
            score TEXT,
            best_of INTEGER,
            round TEXT,
            minutes REAL,
            w_ace REAL,
            w_df REAL,
            w_svpt REAL,
            w_1stIn REAL,
            w_1stWon REAL,
            w_2ndWon REAL,
            w_SvGms REAL,
            w_bpSaved REAL,
            w_bpFaced REAL,
            l_ace REAL,
            l_df REAL,
            l_svpt REAL,
            l_1stIn REAL,
            l_1stWon REAL,
            l_2ndWon REAL,
            l_SvGms REAL,
            l_bpSaved REAL,
            l_bpFaced REAL,
            source TEXT,
            source_updated_at TEXT,
            surface_speed REAL,
            UNIQUE(tourney_id, match_num, winner_id, loser_id)
        )
        """
    )
    conn.commit()


def fetch_year_csv(url: str) -> pd.DataFrame:
    df = pd.read_csv(url)
    df["source"] = "tennismylife"
    df["source_updated_at"] = datetime.utcnow().isoformat()
    return df


def fetch_available_files():
    payload = json.loads(urlopen("https://stats.tennismylife.org/api/data-files", timeout=30).read().decode("utf-8"))
    files = payload.get("files", [])
    # keep yearly files only: 2026.csv, 2025.csv, ...
    out = []
    for f in files:
        name = f.get("name", "")
        if name.endswith(".csv") and name[:-4].isdigit():
            out.append(f)
    return out


def _norm_name(s: object) -> str:
    t = str(s or "").lower().strip()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _resolve_data_path(rel: str) -> str:
    """Chemin absolu : cwd d'abord, puis racine du dépôt (évite cwd=scripts/ sous Streamlit)."""
    if os.path.isabs(rel):
        return rel
    cand = os.path.abspath(rel)
    if os.path.isfile(cand) or os.path.isdir(cand):
        return cand
    alt = os.path.abspath(os.path.join(_repo_root, rel))
    return alt


def _safe_mtime(path: str) -> float:
    try:
        return float(os.path.getmtime(path))
    except OSError:
        return 0.0


def _latest_prematch_csv_path(scraped_dir: str = "data/scraped") -> str | None:
    scraped_abs = _resolve_data_path(scraped_dir)
    files = glob.glob(os.path.join(scraped_abs, "prematch_odds_*.csv"))
    if not files:
        return None
    files = sorted(files, key=_safe_mtime, reverse=True)
    return files[0]


def update_closing_odds(
    db_path: str = "data/bettinghud.db",
    scraped_dir: str = "data/scraped",
) -> int:
    """
    Met à jour `closing_odd` + `clv_score` pour les paris dont le match a démarré/est terminé.

    Source closing (priorite) :
      1. Archives journalieres ``data/scraped/closing_odds/closing_odds_*.csv``
      2. Dernier snapshot prematch ``prematch_odds_*.csv``
    """
    idx_mid: dict[str, tuple[float, float]] = {}
    idx_name: dict[str, tuple[float, float]] = {}
    try:
        from scripts.closing_odds_archive import load_closing_odds_index

        idx_name.update(load_closing_odds_index())
    except Exception:
        pass

    csv_path = _latest_prematch_csv_path(scraped_dir)
    if csv_path:
        try:
            try:
                df = pd.read_csv(csv_path)
            except OSError as oe:
                if getattr(oe, "errno", None) == 22 or "Invalid argument" in str(oe):
                    df = pd.read_csv(
                        csv_path, engine="python", encoding="utf-8", encoding_errors="replace"
                    )
                else:
                    raise
        except Exception as e:
            print(f"[clv] warn: cannot read prematch {csv_path} ({e})", flush=True)
            df = pd.DataFrame()
        if not df.empty:
            for _, r in df.iterrows():
                p1 = _norm_name(r.get("player1"))
                p2 = _norm_name(r.get("player2"))
                if not p1 or not p2:
                    continue
                o1 = pd.to_numeric(r.get("odd_p1"), errors="coerce")
                o2 = pd.to_numeric(r.get("odd_p2"), errors="coerce")
                if pd.isna(o1) or pd.isna(o2):
                    continue
                o1f, o2f = float(o1), float(o2)
                if o1f <= 1.0 or o2f <= 1.0:
                    continue
                key = "||".join(sorted([p1, p2]))
                idx_name.setdefault(key, (o1f, o2f))
                mid = str(r.get("prematch_id") or r.get("id") or "").strip()
                if mid:
                    idx_mid[mid] = (o1f, o2f)

    if not idx_name:
        print("[clv] skip: no closing/prematch odds index", flush=True)
        return 0

    db_abs = _resolve_data_path(db_path)
    conn = sqlite3.connect(db_abs)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    today_iso = datetime.now(timezone.utc).date().isoformat()
    n_upd = 0
    try:
        # started/finished: match_date <= today OR status déjà clos
        bets = conn.execute(
            """
            SELECT id, match_name, bet_on, odds, status, match_id, match_date, closing_odd
            FROM user_bets
            WHERE (closing_odd IS NULL OR closing_odd <= 1.0 OR clv_score IS NULL)
              AND (
                    COALESCE(TRIM(status), '') != 'En cours'
                 OR (match_date IS NOT NULL AND substr(match_date,1,10) <= ?)
              )
            """,
            (today_iso,),
        ).fetchall()
        for bet_id, match_name, bet_on, odd_taken, _status, match_id, _match_date, _clo in bets:
            close = None
            mkey = str(match_id or "").strip()
            if mkey and mkey in idx_mid:
                o1f, o2f = idx_mid[mkey]
            else:
                parts = str(match_name or "").split(" vs ")
                if len(parts) != 2:
                    continue
                p1 = _norm_name(parts[0])
                p2 = _norm_name(parts[1])
                if not p1 or not p2:
                    continue
                pair_key = "||".join(sorted([p1, p2]))
                if pair_key not in idx_name:
                    continue
                o1f, o2f = idx_name[pair_key]
            # side mapping: bet_on equals player1 name => odd_p1 else odd_p2
            if _norm_name(bet_on) == _norm_name(str(match_name).split(" vs ")[0] if " vs " in str(match_name) else ""):
                close = o1f
            elif _norm_name(bet_on) == _norm_name(str(match_name).split(" vs ")[1] if " vs " in str(match_name) else ""):
                close = o2f
            else:
                # alt markets or unmatched labels: skip CLV assignment
                continue
            clv = ValueDetector.calculate_clv_score(odd_taken, close)
            clv_db = None if clv is None else float(clv)
            if clv_db is not None and not math.isfinite(clv_db):
                clv_db = None
            conn.execute(
                """
                UPDATE user_bets
                SET closing_odd = ?, clv_score = ?, clv_updated_ts = ?
                WHERE id = ?
                """,
                (float(close), clv_db, now_iso, int(bet_id)),
            )
            n_upd += 1
        if n_upd:
            conn.commit()
    finally:
        conn.close()
    print(f"[clv] updated {n_upd} bet(s) from {os.path.basename(csv_path)}", flush=True)
    return n_upd


def sync_years(min_year=2010, max_year=None, db_path="data/bettinghud.db"):
    t_sync_all = time.perf_counter()
    conn = sqlite3.connect(db_path)
    ensure_table(conn)
    ensure_surface_speed_column(conn)
    try:
        from weather_open_meteo import ensure_weather_schema

        ensure_weather_schema(conn)
    except Exception as e:
        print(f"[sync_tml] weather schema: {e}", flush=True)

    if max_year is None:
        max_year = datetime.utcnow().year

    files = fetch_available_files()
    files = [f for f in files if min_year <= int(f["name"][:-4]) <= max_year]
    files = sorted(files, key=lambda x: int(x["name"][:-4]))
    print(
        f"[sync_tml] {len(files)} fichier(s) année(s) {min_year}-{max_year} — début",
        flush=True,
    )

    total = 0
    for f in files:
        y = int(f["name"][:-4])
        t_y = time.perf_counter()
        try:
            df = fetch_year_csv(f["url"])
            # Harmoniser colonnes si variation mineure
            table_cols = [r[1] for r in conn.execute("PRAGMA table_info(matches_recent)").fetchall()]
            for c in table_cols:
                if c not in df.columns:
                    df[c] = pd.NA
            def _row_eff_cpi(r):
                base = float(lookup_surface_speed(r.get("tourney_name"), r.get("surface")))
                outdoor = infer_outdoor(r.get("indoor"), r.get("tourney_name"))
                return float(effective_surface_speed_cpi(base, outdoor, None, None))

            df["surface_speed"] = df.apply(_row_eff_cpi, axis=1)
            df = df[table_cols]
            # replace yearly slice (idempotent reruns)
            conn.execute(
                "DELETE FROM matches_recent WHERE source='tennismylife' AND substr(tourney_date,1,4)=?",
                (str(y),),
            )
            conn.commit()
            df.to_sql("matches_recent", conn, if_exists="append", index=False)
            n = len(df)
            total += n
            dt_y = time.perf_counter() - t_y
            print(f"{y}: inserted {n} — {dt_y:.1f}s", flush=True)
        except Exception as e:
            print(f"{y}: ERR {e}", flush=True)

    conn.commit()
    conn.close()
    try:
        update_closing_odds(db_path=db_path)
    except Exception as e:
        print(f"[sync_tml] CLV update: {e}", flush=True)
    dt_sync = time.perf_counter() - t_sync_all
    print(f"TOTAL_INSERTED {total}", flush=True)
    print(f"[sync_tml] durée totale synchronisation {dt_sync:.1f}s ({dt_sync/60:.1f} min)", flush=True)


if __name__ == "__main__":
    sync_years(min_year=2010)
