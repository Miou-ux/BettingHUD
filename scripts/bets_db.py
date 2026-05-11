"""Centralised SQLite helpers for the user-bets portfolio.

Responsibilities:
- `ensure_user_bets_schema`: idempotent ALTER TABLE migration so we never lose
  legacy data while progressively adding richer columns (palier 3.1).
- `ensure_match_results_cache`: cache of resolved matches scraped from
  Tennis Explorer / Sackmann so the same date isn't re-scraped repeatedly.
- `save_bet_enriched`: drop-in replacement for the legacy `save_bet` that
  records the model decision context at the moment a bet is placed.
- `read_cached_results` / `write_cached_results`: minimal cache API for the
  scraper.

The migration is safe to call on every dashboard reload — it only ALTERs
columns that don't yet exist.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from typing import Iterable, Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

DB_PATH_DEFAULT = os.path.join("data", "bettinghud.db")


# ---------------------------------------------------------------------------
# user_bets — schema migration
# ---------------------------------------------------------------------------

# columns we want to guarantee on user_bets. (col_name, sql_type, default_sql)
_USER_BETS_TARGET_COLUMNS: tuple[tuple[str, str, Optional[str]], ...] = (
    ("match_date", "TEXT", None),
    ("tour", "TEXT", None),
    ("surface", "TEXT", None),
    ("tournament", "TEXT", None),
    ("match_id", "TEXT", None),
    ("segment_key", "TEXT", None),
    ("p_model", "REAL", None),
    ("p_implicit", "REAL", None),
    ("ev_at_bet", "REAL", None),
    ("closing_odd", "REAL", None),
    ("clv_score", "REAL", None),
    ("clv_updated_ts", "TEXT", None),
    ("bookmaker_source", "TEXT", None),
    ("placed_ts", "TEXT", None),
    ("settled_ts", "TEXT", None),
    ("score_final", "TEXT", None),
    ("winner_resolved", "TEXT", None),
    ("result_source", "TEXT", None),
    ("notes", "TEXT", None),
    ("tracker_source", "TEXT", None),
)

LIVE_TRACKER_META_START_BR = "live_br_start_eur"
LIVE_TRACKER_DEFAULT_START_BR = "55"
LIVE_TRACKER_META_MANUAL_ADJUST = "live_br_manual_adjust_eur"
META_LAST_TOURS_SYNC_TS = "last_tours_sync_ts"
META_LAST_ML_TRAIN_TS = "last_ml_train_ts"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalize_schedule_date(raw: Optional[object]) -> Optional[str]:
    """Return ``YYYY-MM-DD`` from prematch/date cell, or ``None``.

    Handles ISO strings and pandas-compatible datetimes embedded in strings.
    """
    if raw is None:
        return None
    if hasattr(raw, "strftime"):
        try:
            return raw.strftime("%Y-%m-%d")  # type: ignore[union-attr]
        except Exception:
            pass
    s = str(raw).strip()
    if not s:
        return None
    if " " in s:
        s = s.split()[0]
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None
ML_MODEL_BUNDLE_REL = os.path.join("models", "xgb_model_tml_v45.pkl")


def get_ml_bundle_abspath() -> str:
    return os.path.join(_REPO_ROOT, ML_MODEL_BUNDLE_REL.replace("/", os.sep))


def get_ml_bundle_mtime() -> Optional[float]:
    """mtime du fichier bundle ML sur disque (None si absent)."""
    p = get_ml_bundle_abspath()
    try:
        return float(os.path.getmtime(p)) if os.path.isfile(p) else None
    except OSError:
        return None


def _format_atp_date_for_display(raw) -> str:
    """Affiche la date TML (YYYYMMDD ou texte) en AAAA-MM-JJ."""
    if raw is None:
        return "—"
    s = str(raw).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _format_wta_date_for_display(raw) -> str:
    """Affiche tourney_date WTA (datetime SQLite / ISO) en AAAA-MM-JJ."""
    if raw is None:
        return "—"
    s = str(raw).strip()
    if " " in s:
        s = s.split()[0]
    return s[:10] if len(s) >= 10 else s


def _fetch_last_ml_source_matches(
    conn: sqlite3.Connection,
) -> tuple[Optional[dict], Optional[dict]]:
    """Derniers matchs ATP (TML) et WTA (Sackmann) avec les mêmes filtres année que ml_model.prepare_data."""
    last_atp: Optional[dict] = None
    last_wta: Optional[dict] = None
    try:
        row = conn.execute(
            """
            SELECT tourney_date, tourney_name, winner_name, loser_name
            FROM matches_recent
            WHERE source='tennismylife'
              AND CAST(substr(tourney_date,1,4) AS INTEGER) >= 2010
            ORDER BY tourney_date DESC
            LIMIT 1
            """
        ).fetchone()
        if row:
            last_atp = {
                "date": _format_atp_date_for_display(row[0]),
                "tourney_name": row[1] or "—",
                "winner_name": row[2] or "—",
                "loser_name": row[3] or "—",
            }
    except sqlite3.Error:
        pass
    try:
        row = conn.execute(
            """
            SELECT tourney_date, tourney_name, winner_name, loser_name
            FROM wta_matches
            WHERE CAST(substr(CAST(tourney_date AS TEXT),1,4) AS INTEGER) >= 2010
            ORDER BY tourney_date DESC
            LIMIT 1
            """
        ).fetchone()
        if row:
            last_wta = {
                "date": _format_wta_date_for_display(row[0]),
                "tourney_name": row[1] or "—",
                "winner_name": row[2] or "—",
                "loser_name": row[3] or "—",
            }
    except sqlite3.Error:
        pass
    return last_atp, last_wta


def get_data_freshness_snapshot(db_path: str = DB_PATH_DEFAULT) -> dict:
    """Horodatages derniers sync ATP+WTA et dernier entraînement ML (+ mtime fichier modèle)."""
    conn = sqlite3.connect(db_path)
    try:
        ensure_bets_meta(conn)
        tours = get_meta(conn, META_LAST_TOURS_SYNC_TS)
        ml_tr = get_meta(conn, META_LAST_ML_TRAIN_TS)
        last_atp, last_wta = _fetch_last_ml_source_matches(conn)
    finally:
        conn.close()
    mt = get_ml_bundle_mtime()
    return {
        "last_tours_sync_iso": tours,
        "last_ml_train_iso": ml_tr,
        "model_bundle_mtime": mt,
        "model_bundle_path": get_ml_bundle_abspath(),
        "last_atp_match": last_atp,
        "last_wta_match": last_wta,
    }


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def ensure_user_bets_schema(conn: sqlite3.Connection) -> list[str]:
    """Ensure user_bets has all the enriched columns. Idempotent.

    Returns the list of columns that were added in this call (empty if up to date).
    """
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            match_name TEXT,
            bet_on TEXT,
            odds REAL,
            stake REAL,
            status TEXT DEFAULT 'En cours',
            profit REAL DEFAULT 0.0
        )
        """
    )
    added: list[str] = []
    for col, sql_type, default in _USER_BETS_TARGET_COLUMNS:
        have = _existing_columns(conn, "user_bets")
        if col in have:
            continue
        try:
            if default is None:
                cur.execute(f"ALTER TABLE user_bets ADD COLUMN {col} {sql_type}")
            else:
                cur.execute(
                    f"ALTER TABLE user_bets ADD COLUMN {col} {sql_type} DEFAULT {default}"
                )
            added.append(col)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column name" not in msg and "duplicate column" not in msg:
                raise
            # Already present (migration race / stale PRAGMA cache)
    if added:
        conn.commit()
    return added


# ---------------------------------------------------------------------------
# match_results — cache table for resolved matches
# ---------------------------------------------------------------------------


def ensure_bets_meta(conn: sqlite3.Connection) -> None:
    """Create a tiny key/value meta table used to track e.g. the last
    reconciliation timestamp."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bets_meta (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.execute("SELECT value FROM bets_meta WHERE key = ?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO bets_meta(key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, value, datetime.utcnow().isoformat(timespec="seconds")),
    )
    conn.commit()


def ensure_live_tracker_start_br(
    conn: sqlite3.Connection,
    *,
    default_start: Optional[float] = None,
) -> None:
    """If not set yet, initialise live-tracker starting bankroll in meta."""
    ensure_bets_meta(conn)
    if get_meta(conn, LIVE_TRACKER_META_START_BR) is None:
        d = (
            LIVE_TRACKER_DEFAULT_START_BR
            if default_start is None
            else str(float(default_start))
        )
        set_meta(conn, LIVE_TRACKER_META_START_BR, d)


def get_live_tracker_start_br(conn: sqlite3.Connection) -> float:
    ensure_bets_meta(conn)
    v = get_meta(conn, LIVE_TRACKER_META_START_BR)
    if v is None:
        return float(LIVE_TRACKER_DEFAULT_START_BR)
    try:
        return float(v)
    except ValueError:
        return float(LIVE_TRACKER_DEFAULT_START_BR)


def set_live_tracker_start_br(conn: sqlite3.Connection, value: float) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, LIVE_TRACKER_META_START_BR, str(float(value)))


def get_live_tracker_manual_adjust_eur(conn: sqlite3.Connection) -> float:
    ensure_bets_meta(conn)
    v = get_meta(conn, LIVE_TRACKER_META_MANUAL_ADJUST)
    if v is None or str(v).strip() == "":
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def set_live_tracker_manual_adjust_eur(conn: sqlite3.Connection, value: float) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, LIVE_TRACKER_META_MANUAL_ADJUST, str(float(value)))


def compute_live_tracker_bankroll_eur(conn: sqlite3.Connection) -> dict:
    """Available bankroll for Live Tracker staking.

    Ledger: start + sum(settled profits) - sum(open stakes).

    Settled stakes are reflected in profits (lost => negative profit).

    Pending bets imply stake temporarily unavailable.
    """
    ensure_user_bets_schema(conn)
    start = get_live_tracker_start_br(conn)
    cur = conn.execute(
        """
        SELECT COALESCE(SUM(stake), 0)
        FROM user_bets
        WHERE tracker_source = 'live_tracker'
          AND COALESCE(TRIM(status), '') = 'En cours'
        """
    )
    open_stakes = float(cur.fetchone()[0] or 0.0)
    cur2 = conn.execute(
        """
        SELECT COALESCE(SUM(profit), 0)
        FROM user_bets
        WHERE tracker_source = 'live_tracker'
          AND COALESCE(TRIM(status), '') != 'En cours'
        """
    )
    settled_profit = float(cur2.fetchone()[0] or 0.0)

    manual_adj = get_live_tracker_manual_adjust_eur(conn)
    avail_raw = float(start) + settled_profit - open_stakes
    avail = avail_raw + manual_adj
    committed = open_stakes
    equity = avail + committed
    return {
        "start_eur": float(start),
        "available_eur": float(avail),
        "available_raw_eur": float(avail_raw),
        "manual_adjust_eur": float(manual_adj),
        "committed_open_eur": float(committed),
        "equity_eur": float(equity),
        "settled_profit_eur": float(settled_profit),
    }


def ensure_reconciliation_log(conn: sqlite3.Connection) -> None:
    """Append-only log of every reconciliation pass and any divergences found."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts TEXT NOT NULL,
            bet_id INTEGER,
            match_name TEXT,
            old_status TEXT,
            new_status TEXT,
            old_winner TEXT,
            new_winner TEXT,
            sources_agreement TEXT,
            action TEXT,
            notes TEXT
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_reco_run_ts ON reconciliation_log(run_ts)"
    )
    conn.commit()


def ensure_match_results_cache(conn: sqlite3.Connection) -> None:
    """Create the cache table for resolved matches if missing."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS match_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_date TEXT NOT NULL,
            p1_canonical TEXT NOT NULL,
            p2_canonical TEXT NOT NULL,
            winner_canonical TEXT,
            score TEXT,
            retired INTEGER DEFAULT 0,
            walkover INTEGER DEFAULT 0,
            tour TEXT,
            source TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            UNIQUE(match_date, p1_canonical, p2_canonical, source)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_match_results_date ON match_results(match_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_match_results_pair ON match_results(p1_canonical, p2_canonical)"
    )
    conn.commit()


def read_cached_results(
    conn: sqlite3.Connection, dates: Iterable[str]
) -> dict[str, dict[str, dict]]:
    """Return cached results keyed by match_date -> {(p1_can, p2_can): row_dict}.

    Each value dict contains: winner, score, retired, walkover, source, scraped_at.
    """
    out: dict[str, dict[str, dict]] = {}
    dates_list = [d for d in dates if d]
    if not dates_list:
        return out
    placeholders = ",".join("?" * len(dates_list))
    cur = conn.execute(
        f"""
        SELECT match_date, p1_canonical, p2_canonical, winner_canonical,
               score, retired, walkover, source, scraped_at, tour
        FROM match_results
        WHERE match_date IN ({placeholders})
        """,
        dates_list,
    )
    for (
        d,
        p1c,
        p2c,
        winner,
        score,
        retired,
        walkover,
        source,
        scraped_at,
        tour,
    ) in cur.fetchall():
        bucket = out.setdefault(d, {})
        key = f"{p1c}||{p2c}"
        bucket[key] = {
            "winner_canonical": winner,
            "score": score,
            "retired": bool(retired),
            "walkover": bool(walkover),
            "source": source,
            "scraped_at": scraped_at,
            "tour": tour,
        }
    return out


def write_cached_results(
    conn: sqlite3.Connection,
    rows: Iterable[dict],
) -> int:
    """Bulk insert resolved matches into the cache. Each row dict must have:
    match_date, p1_canonical, p2_canonical, winner_canonical, source.
    Optional: score, retired, walkover, tour.
    Uses INSERT OR REPLACE on UNIQUE(match_date, p1c, p2c, source) so the cache
    remains source-of-truth per source.
    """
    cur = conn.cursor()
    n = 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    for r in rows:
        try:
            cur.execute(
                """
                INSERT OR REPLACE INTO match_results
                (match_date, p1_canonical, p2_canonical, winner_canonical,
                 score, retired, walkover, tour, source, scraped_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    r["match_date"],
                    r["p1_canonical"],
                    r["p2_canonical"],
                    r.get("winner_canonical"),
                    r.get("score"),
                    1 if r.get("retired") else 0,
                    1 if r.get("walkover") else 0,
                    r.get("tour"),
                    r["source"],
                    r.get("scraped_at", now_iso),
                ),
            )
            n += 1
        except Exception:
            continue
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Saving an enriched bet
# ---------------------------------------------------------------------------


def save_bet_enriched(
    *,
    match_name: str,
    bet_on: str,
    odds: float,
    stake: float,
    db_path: str = DB_PATH_DEFAULT,
    match_date: Optional[str] = None,
    tour: Optional[str] = None,
    surface: Optional[str] = None,
    tournament: Optional[str] = None,
    match_id: Optional[str] = None,
    segment_key: Optional[str] = None,
    p_model: Optional[float] = None,
    p_implicit: Optional[float] = None,
    ev_at_bet: Optional[float] = None,
    bookmaker_source: Optional[str] = None,
    notes: Optional[str] = None,
    tracker_source: Optional[str] = None,
) -> int:
    """Insert a bet with full decision context. Returns the new bet id.

    ``match_date`` (YYYY-MM-DD): scheduled match day from the fixture. The ``date``
    column stays as SQLite ``date('now')`` (day the ticket was recorded). Result
    resolution uses ``match_date`` when present, otherwise ``date``.
    """
    conn = sqlite3.connect(db_path)
    try:
        ensure_user_bets_schema(conn)
        ensure_bets_meta(conn)
        ensure_live_tracker_start_br(conn)
        if p_implicit is None and odds and odds > 0:
            try:
                p_implicit = 1.0 / float(odds)
            except Exception:
                p_implicit = None
        sched = normalize_schedule_date(match_date)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_bets (
                date, match_name, bet_on, odds, stake, status, profit,
                match_date,
                tour, surface, tournament, match_id,
                segment_key,
                p_model, p_implicit, ev_at_bet,
                bookmaker_source, placed_ts, notes, tracker_source
            ) VALUES (date('now'), ?, ?, ?, ?, 'En cours', 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_name,
                bet_on,
                float(odds),
                float(stake),
                sched,
                tour,
                surface,
                tournament,
                match_id,
                segment_key,
                None if p_model is None else float(p_model),
                None if p_implicit is None else float(p_implicit),
                None if ev_at_bet is None else float(ev_at_bet),
                bookmaker_source,
                datetime.utcnow().isoformat(timespec="seconds"),
                notes,
                tracker_source,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Settling a bet
# ---------------------------------------------------------------------------


def settle_bet(
    conn: sqlite3.Connection,
    *,
    bet_id: int,
    status: str,
    profit: float,
    winner_resolved: Optional[str] = None,
    score_final: Optional[str] = None,
    result_source: Optional[str] = None,
) -> None:
    """Update a bet to its resolved state. Status must be one of:
    'Gagné', 'Perdu', 'Annulé'.
    For 'Annulé' the stake is refunded so profit should be 0.0.
    """
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE user_bets
        SET status = ?, profit = ?, settled_ts = ?,
            winner_resolved = COALESCE(?, winner_resolved),
            score_final = COALESCE(?, score_final),
            result_source = COALESCE(?, result_source)
        WHERE id = ?
        """,
        (
            status,
            float(profit),
            datetime.utcnow().isoformat(timespec="seconds"),
            winner_resolved,
            score_final,
            result_source,
            int(bet_id),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Bootstrapping helper
# ---------------------------------------------------------------------------


def init_all(db_path: str = DB_PATH_DEFAULT) -> dict:
    """Run all migrations once. Returns a small summary dict."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        added_cols = ensure_user_bets_schema(conn)
        ensure_match_results_cache(conn)
        ensure_bets_meta(conn)
        ensure_live_tracker_start_br(conn)
        ensure_reconciliation_log(conn)
        try:
            from weather_open_meteo import ensure_weather_schema

            ensure_weather_schema(conn)
        except Exception:
            pass
        return {"user_bets_added_columns": added_cols}
    finally:
        conn.close()


if __name__ == "__main__":
    print(init_all())
