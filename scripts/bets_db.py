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
_WAL_ENABLED: set[str] = set()


def resolve_db_path(db_path: str = DB_PATH_DEFAULT) -> str:
    if os.path.isabs(db_path):
        return db_path
    root = os.path.dirname(_SCRIPTS_DIR)
    return os.path.join(root, db_path)


def open_db(db_path: str = DB_PATH_DEFAULT, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Connexion SQLite avec WAL + busy_timeout (lectures concurrentes)."""
    path = resolve_db_path(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    if path not in _WAL_ENABLED:
        conn.execute("PRAGMA journal_mode=WAL")
        _WAL_ENABLED.add(path)
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return conn


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
    ("telegram_user_id", "TEXT", None),
    ("web_username", "TEXT", None),
)

LIVE_TRACKER_META_START_BR = "live_br_start_eur"
LIVE_TRACKER_DEFAULT_START_BR = "100"
LEGACY_DEFAULT_START_BR = 55.0
LIVE_TRACKER_META_MANUAL_ADJUST = "live_br_manual_adjust_eur"
TELEGRAM_DEFAULT_START_BR = os.getenv("TELEGRAM_DEFAULT_START_BR", LIVE_TRACKER_DEFAULT_START_BR)

# Sources enregistrées via l'app (Kelly / portefeuille unifié).
APP_KELLY_TRACKER_SOURCES: tuple[str, ...] = (
    "live_tracker",
    "top5_proba_action",
    "live_inplay_manual",
    "1day1pick_web",
)
META_LAST_TOURS_SYNC_TS = "last_tours_sync_ts"
META_LAST_TML_SYNC_TS = "last_tml_sync_ts"
META_LAST_SACKMANN_SYNC_TS = "last_sackmann_sync_ts"
META_LAST_ML_TRAIN_TS = "last_ml_train_ts"
ALGO_OPP_KELLY_BASE_FRAC = 0.5
ALGO_OPP_KELLY_MAX_STAKE_FRAC = 0.15
ALGO_OPP_BRIER_CAP = 0.25

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
ML_MODEL_BUNDLE_REL = os.path.join("models", "xgb_model_tml_v47.pkl")


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
    conn = open_db(db_path)
    try:
        ensure_bets_meta(conn)
        tours = get_meta(conn, META_LAST_TOURS_SYNC_TS)
        tml = get_meta(conn, META_LAST_TML_SYNC_TS)
        sack = get_meta(conn, META_LAST_SACKMANN_SYNC_TS)
        ml_tr = get_meta(conn, META_LAST_ML_TRAIN_TS)
        last_atp, last_wta = _fetch_last_ml_source_matches(conn)
    finally:
        conn.close()
    mt = get_ml_bundle_mtime()
    return {
        "last_tours_sync_iso": tours,
        "last_tml_sync_iso": tml or tours,
        "last_sackmann_sync_iso": sack or tours,
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


def _telegram_br_meta_key(telegram_user_id: str, kind: str) -> str:
    uid = str(telegram_user_id or "").strip()
    if not uid:
        raise ValueError("telegram_user_id requis")
    if kind == "start":
        return f"telegram_br_start_{uid}"
    if kind == "adjust":
        return f"telegram_br_manual_adj_{uid}"
    if kind == "custom":
        return f"telegram_br_start_custom_{uid}"
    raise ValueError(f"meta kind inconnu: {kind}")


def is_telegram_user_start_br_custom(
    conn: sqlite3.Connection,
    telegram_user_id: str,
) -> bool:
    ensure_bets_meta(conn)
    return get_meta(conn, _telegram_br_meta_key(telegram_user_id, "custom")) == "1"


def mark_telegram_user_start_br_custom(
    conn: sqlite3.Connection,
    telegram_user_id: str,
) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, _telegram_br_meta_key(telegram_user_id, "custom"), "1")


def ensure_telegram_user_start_br(
    conn: sqlite3.Connection,
    telegram_user_id: str,
    *,
    default_start: Optional[float] = None,
) -> None:
    ensure_bets_meta(conn)
    key = _telegram_br_meta_key(telegram_user_id, "start")
    if get_meta(conn, key) is None:
        d = (
            TELEGRAM_DEFAULT_START_BR
            if default_start is None
            else str(float(default_start))
        )
        set_meta(conn, key, d)


def get_telegram_user_start_br(conn: sqlite3.Connection, telegram_user_id: str) -> float:
    ensure_telegram_user_start_br(conn, telegram_user_id)
    v = get_meta(conn, _telegram_br_meta_key(telegram_user_id, "start"))
    try:
        return float(v) if v is not None else float(TELEGRAM_DEFAULT_START_BR)
    except ValueError:
        return float(TELEGRAM_DEFAULT_START_BR)


def set_telegram_user_start_br(
    conn: sqlite3.Connection,
    telegram_user_id: str,
    value: float,
    *,
    user_custom: bool = False,
) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, _telegram_br_meta_key(telegram_user_id, "start"), str(float(value)))
    if user_custom:
        mark_telegram_user_start_br_custom(conn, telegram_user_id)


def get_telegram_user_manual_adjust_eur(
    conn: sqlite3.Connection,
    telegram_user_id: str,
) -> float:
    ensure_bets_meta(conn)
    v = get_meta(conn, _telegram_br_meta_key(telegram_user_id, "adjust"))
    if v is None or str(v).strip() == "":
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def set_telegram_user_manual_adjust_eur(
    conn: sqlite3.Connection,
    telegram_user_id: str,
    value: float,
) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, _telegram_br_meta_key(telegram_user_id, "adjust"), str(float(value)))


def link_unassigned_app_bets_to_telegram_user(
    conn: sqlite3.Connection,
    telegram_user_id: str,
) -> int:
    """Rattache à un user Telegram les paris app (Kelly) encore sans ``telegram_user_id``."""
    ensure_user_bets_schema(conn)
    uid = str(telegram_user_id).strip()
    src_ph = ",".join("?" * len(APP_KELLY_TRACKER_SOURCES))
    cur = conn.execute(
        f"""
        UPDATE user_bets
        SET telegram_user_id = ?
        WHERE (telegram_user_id IS NULL OR TRIM(telegram_user_id) = '')
          AND (
            tracker_source IN ({src_ph})
            OR COALESCE(TRIM(tracker_source), '') = ''
          )
        """,
        (uid, *APP_KELLY_TRACKER_SOURCES),
    )
    conn.commit()
    return int(cur.rowcount or 0)


def copy_app_kelly_meta_to_telegram_user(
    conn: sqlite3.Connection,
    telegram_user_id: str,
) -> dict:
    """Recopie capital de départ + ajustement manuel Kelly app → méta Telegram user."""
    ensure_bets_meta(conn)
    uid = str(telegram_user_id).strip()
    start = get_live_tracker_start_br(conn)
    adj = get_live_tracker_manual_adjust_eur(conn)
    set_telegram_user_start_br(conn, uid, start)
    set_telegram_user_manual_adjust_eur(conn, uid, adj)
    return {"start_eur": float(start), "manual_adjust_eur": float(adj)}


def compute_telegram_user_bankroll_eur(
    conn: sqlite3.Connection,
    telegram_user_id: str,
) -> dict:
    """Bankroll Kelly d'un utilisateur (tous ses paris : app + Telegram)."""
    ensure_user_bets_schema(conn)
    uid = str(telegram_user_id).strip()
    ensure_telegram_user_start_br(conn, uid)
    start = get_telegram_user_start_br(conn, uid)
    cur = conn.execute(
        """
        SELECT COALESCE(SUM(stake), 0)
        FROM user_bets
        WHERE telegram_user_id = ?
          AND COALESCE(TRIM(status), '') = 'En cours'
        """,
        (uid,),
    )
    open_stakes = float(cur.fetchone()[0] or 0.0)
    cur2 = conn.execute(
        """
        SELECT COALESCE(SUM(profit), 0)
        FROM user_bets
        WHERE telegram_user_id = ?
          AND COALESCE(TRIM(status), '') != 'En cours'
        """,
        (uid,),
    )
    settled_profit = float(cur2.fetchone()[0] or 0.0)
    manual_adj = get_telegram_user_manual_adjust_eur(conn, uid)
    avail_raw = float(start) + settled_profit - open_stakes
    avail = avail_raw + manual_adj
    return {
        "telegram_user_id": uid,
        "start_eur": float(start),
        "available_eur": float(avail),
        "available_raw_eur": float(avail_raw),
        "manual_adjust_eur": float(manual_adj),
        "committed_open_eur": float(open_stakes),
        "equity_eur": float(avail + open_stakes),
        "settled_profit_eur": float(settled_profit),
    }


def _web_br_meta_key(web_username: str, kind: str) -> str:
    uname = str(web_username or "").strip().lower()
    if not uname:
        raise ValueError("web_username requis")
    if kind == "start":
        return f"web_br_start_{uname}"
    if kind == "adjust":
        return f"web_br_manual_adj_{uname}"
    if kind == "custom":
        return f"web_br_start_custom_{uname}"
    raise ValueError(f"meta kind inconnu: {kind}")


def is_web_user_start_br_custom(conn: sqlite3.Connection, web_username: str) -> bool:
    ensure_bets_meta(conn)
    return get_meta(conn, _web_br_meta_key(web_username, "custom")) == "1"


def mark_web_user_start_br_custom(conn: sqlite3.Connection, web_username: str) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, _web_br_meta_key(web_username, "custom"), "1")


def set_web_user_start_br(
    conn: sqlite3.Connection,
    web_username: str,
    value: float,
    *,
    user_custom: bool = False,
) -> None:
    ensure_bets_meta(conn)
    uname = str(web_username or "").strip().lower()
    set_meta(conn, _web_br_meta_key(uname, "start"), str(float(value)))
    if user_custom:
        mark_web_user_start_br_custom(conn, uname)


def ensure_web_user_start_br(
    conn: sqlite3.Connection,
    web_username: str,
    *,
    default_start: Optional[float] = None,
) -> None:
    ensure_bets_meta(conn)
    key = _web_br_meta_key(web_username, "start")
    if get_meta(conn, key) is None:
        d = (
            TELEGRAM_DEFAULT_START_BR
            if default_start is None
            else str(float(default_start))
        )
        set_meta(conn, key, d)


def get_web_user_start_br(conn: sqlite3.Connection, web_username: str) -> float:
    ensure_web_user_start_br(conn, web_username)
    v = get_meta(conn, _web_br_meta_key(web_username, "start"))
    try:
        return float(v) if v is not None else float(TELEGRAM_DEFAULT_START_BR)
    except ValueError:
        return float(TELEGRAM_DEFAULT_START_BR)


def migrate_legacy_default_start_br(
    conn: sqlite3.Connection,
    *,
    old_default: float = LEGACY_DEFAULT_START_BR,
    new_default: float | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Passe les BR auto-initialisées (ex. 55 €) à la nouvelle valeur par défaut.

    Ignore les comptes dont le capital de départ a été personnalisé (/brset) ou
    qui ont un ajustement manuel (dépôt/retrait).
    """
    target = float(new_default if new_default is not None else TELEGRAM_DEFAULT_START_BR)
    stats = {"live_tracker": 0, "telegram": 0, "web": 0}

    ensure_bets_meta(conn)
    live_start = get_meta(conn, LIVE_TRACKER_META_START_BR)
    live_adj = get_live_tracker_manual_adjust_eur(conn)
    if live_start is not None:
        try:
            if abs(float(live_start) - old_default) < 0.01 and abs(live_adj) <= 1e-9:
                stats["live_tracker"] = 1
                if not dry_run:
                    set_meta(conn, LIVE_TRACKER_META_START_BR, str(target))
        except ValueError:
            pass

    for key, value in conn.execute(
        "SELECT key, value FROM bets_meta WHERE key LIKE 'telegram_br_start_%'"
    ).fetchall():
        if str(key).endswith("_custom"):
            continue
        uid = str(key)[len("telegram_br_start_") :]
        if is_telegram_user_start_br_custom(conn, uid):
            continue
        if abs(get_telegram_user_manual_adjust_eur(conn, uid)) > 1e-9:
            continue
        try:
            if abs(float(value) - old_default) < 0.01:
                stats["telegram"] += 1
                if not dry_run:
                    set_meta(conn, key, str(target))
        except ValueError:
            pass

    for key, value in conn.execute(
        "SELECT key, value FROM bets_meta WHERE key LIKE 'web_br_start_%'"
    ).fetchall():
        if str(key).endswith("_custom"):
            continue
        uname = str(key)[len("web_br_start_") :]
        if is_web_user_start_br_custom(conn, uname):
            continue
        if abs(get_web_user_manual_adjust_eur(conn, uname)) > 1e-9:
            continue
        try:
            if abs(float(value) - old_default) < 0.01:
                stats["web"] += 1
                if not dry_run:
                    set_meta(conn, key, str(target))
        except ValueError:
            pass

    if not dry_run:
        conn.commit()
    return stats


def get_web_user_manual_adjust_eur(conn: sqlite3.Connection, web_username: str) -> float:
    ensure_bets_meta(conn)
    v = get_meta(conn, _web_br_meta_key(web_username, "adjust"))
    if v is None or str(v).strip() == "":
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def set_web_user_manual_adjust_eur(
    conn: sqlite3.Connection,
    web_username: str,
    value: float,
) -> None:
    ensure_bets_meta(conn)
    set_meta(conn, _web_br_meta_key(web_username, "adjust"), str(float(value)))


def compute_web_user_bankroll_eur(conn: sqlite3.Connection, web_username: str) -> dict:
    """Bankroll Kelly d'un compte web CourtAlpha (sans Telegram lié)."""
    ensure_user_bets_schema(conn)
    uname = str(web_username or "").strip().lower()
    ensure_web_user_start_br(conn, uname)
    start = get_web_user_start_br(conn, uname)
    cur = conn.execute(
        """
        SELECT COALESCE(SUM(stake), 0)
        FROM user_bets
        WHERE LOWER(COALESCE(web_username, '')) = ?
          AND COALESCE(TRIM(status), '') = 'En cours'
        """,
        (uname,),
    )
    open_stakes = float(cur.fetchone()[0] or 0.0)
    cur2 = conn.execute(
        """
        SELECT COALESCE(SUM(profit), 0)
        FROM user_bets
        WHERE LOWER(COALESCE(web_username, '')) = ?
          AND COALESCE(TRIM(status), '') != 'En cours'
        """,
        (uname,),
    )
    settled_profit = float(cur2.fetchone()[0] or 0.0)
    manual_adj = get_web_user_manual_adjust_eur(conn, uname)
    avail_raw = float(start) + settled_profit - open_stakes
    avail = avail_raw + manual_adj
    return {
        "web_username": uname,
        "start_eur": float(start),
        "available_eur": float(avail),
        "available_raw_eur": float(avail_raw),
        "manual_adjust_eur": float(manual_adj),
        "committed_open_eur": float(open_stakes),
        "equity_eur": float(avail + open_stakes),
        "settled_profit_eur": float(settled_profit),
    }


_TRACKER_SOURCE_LABELS: dict[str, str] = {
    "telegram_bet": "Telegram",
    "live_tracker": "Live Tracker",
    "top5_proba_action": "Paris du jour",
    "live_inplay_manual": "In-play manuel",
    "1day1pick_web": "1 Day 1 Pick",
}


def _tracker_source_label(raw: object) -> str:
    key = str(raw or "").strip() or "_legacy"
    if key == "_legacy":
        return "App (legacy)"
    return _TRACKER_SOURCE_LABELS.get(key, key)


def compute_telegram_user_br_advanced_stats(
    conn: sqlite3.Connection,
    telegram_user_id: str,
) -> dict:
    """Statistiques portefeuille détaillées pour un utilisateur Telegram."""
    from datetime import date, timedelta

    ensure_user_bets_schema(conn)
    uid = str(telegram_user_id).strip()
    snap = compute_telegram_user_bankroll_eur(conn, uid)

    cur = conn.execute(
        """
        SELECT COALESCE(TRIM(status), '') AS st, COUNT(*), COALESCE(SUM(stake), 0), COALESCE(SUM(profit), 0)
        FROM user_bets
        WHERE telegram_user_id = ?
        GROUP BY COALESCE(TRIM(status), '')
        """,
        (uid,),
    )
    by_status: dict[str, dict[str, float]] = {}
    for row in cur.fetchall():
        st = str(row[0] or "").strip() or "En cours"
        by_status[st] = {
            "count": int(row[1] or 0),
            "stake_eur": float(row[2] or 0.0),
            "profit_eur": float(row[3] or 0.0),
        }

    open_info = by_status.get("En cours", {"count": 0, "stake_eur": 0.0, "profit_eur": 0.0})
    wins = int(by_status.get("Gagné", {}).get("count") or 0)
    losses = int(by_status.get("Perdu", {}).get("count") or 0)
    voids = int(by_status.get("Annulé", {}).get("count") or 0)
    settled_n = wins + losses + voids
    win_rate_pct = (100.0 * wins / (wins + losses)) if (wins + losses) > 0 else None

    settled_stake = 0.0
    settled_profit = 0.0
    for st, info in by_status.items():
        if st == "En cours":
            continue
        settled_stake += float(info.get("stake_eur") or 0.0)
        settled_profit += float(info.get("profit_eur") or 0.0)
    roi_pct = (100.0 * settled_profit / settled_stake) if settled_stake > 0 else None

    cur_src = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(tracker_source), ''), '_legacy') AS src,
               COUNT(*) AS n,
               COALESCE(SUM(CASE WHEN COALESCE(TRIM(status), '') != 'En cours' THEN profit ELSE 0 END), 0) AS pl,
               COALESCE(SUM(CASE WHEN COALESCE(TRIM(status), '') = 'En cours' THEN stake ELSE 0 END), 0) AS open_stake
        FROM user_bets
        WHERE telegram_user_id = ?
        GROUP BY src
        ORDER BY n DESC
        """,
        (uid,),
    )
    by_source = [
        {
            "source": str(r[0]),
            "label": _tracker_source_label(r[0]),
            "count": int(r[1] or 0),
            "settled_profit_eur": float(r[2] or 0.0),
            "open_stake_eur": float(r[3] or 0.0),
        }
        for r in cur_src.fetchall()
    ]

    since = (date.today() - timedelta(days=6)).isoformat()
    cur_days = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(match_date), ''), NULLIF(TRIM(date), '')) AS d,
               COUNT(*) AS n,
               COALESCE(SUM(profit), 0) AS pl,
               COALESCE(SUM(stake), 0) AS st
        FROM user_bets
        WHERE telegram_user_id = ?
          AND COALESCE(TRIM(status), '') NOT IN ('', 'En cours')
          AND COALESCE(NULLIF(TRIM(match_date), ''), NULLIF(TRIM(date), '')) >= ?
        GROUP BY d
        HAVING d IS NOT NULL
        ORDER BY d DESC
        """,
        (uid, since),
    )
    last_7_days = [
        {
            "date": str(r[0]),
            "count": int(r[1] or 0),
            "profit_eur": float(r[2] or 0.0),
            "stake_eur": float(r[3] or 0.0),
        }
        for r in cur_days.fetchall()
    ]

    cur_open = conn.execute(
        """
        SELECT match_name, bet_on, odds, stake
        FROM user_bets
        WHERE telegram_user_id = ?
          AND COALESCE(TRIM(status), '') = 'En cours'
        ORDER BY stake DESC
        LIMIT 5
        """,
        (uid,),
    )
    open_bets = [
        {
            "match_name": str(r[0] or ""),
            "bet_on": str(r[1] or ""),
            "odds": float(r[2] or 0.0),
            "stake_eur": float(r[3] or 0.0),
        }
        for r in cur_open.fetchall()
    ]

    cur_form = conn.execute(
        """
        SELECT COALESCE(TRIM(status), '')
        FROM user_bets
        WHERE telegram_user_id = ?
          AND COALESCE(TRIM(status), '') IN ('Gagné', 'Perdu')
        ORDER BY COALESCE(NULLIF(TRIM(settled_ts), ''), NULLIF(TRIM(placed_ts), ''), date) DESC,
                 id DESC
        LIMIT 10
        """,
        (uid,),
    )
    recent_form = [str(r[0]) for r in cur_form.fetchall()]

    cur_avg = conn.execute(
        """
        SELECT COALESCE(TRIM(status), ''), AVG(odds)
        FROM user_bets
        WHERE telegram_user_id = ?
          AND COALESCE(TRIM(status), '') IN ('Gagné', 'Perdu')
        GROUP BY COALESCE(TRIM(status), '')
        """,
        (uid,),
    )
    avg_odds: dict[str, float] = {}
    for st, avg in cur_avg.fetchall():
        if avg is not None:
            avg_odds[str(st)] = float(avg)

    equity = float(snap.get("equity_eur") or 0.0)
    committed = float(snap.get("committed_open_eur") or 0.0)
    exposure_pct = (100.0 * committed / equity) if equity > 1e-6 else None

    pl_total = float(snap.get("settled_profit_eur") or 0.0)
    start = float(snap.get("start_eur") or 0.0)
    growth_pct = (100.0 * pl_total / start) if start > 1e-6 else None

    return {
        **snap,
        "by_status": by_status,
        "settled_count": settled_n,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "win_rate_pct": win_rate_pct,
        "settled_stake_eur": settled_stake,
        "settled_profit_eur_detail": settled_profit,
        "roi_pct": roi_pct,
        "by_source": by_source,
        "last_7_days": last_7_days,
        "open_bets": open_bets,
        "open_count": int(open_info.get("count") or 0),
        "recent_form": recent_form,
        "avg_odds_won": avg_odds.get("Gagné"),
        "avg_odds_lost": avg_odds.get("Perdu"),
        "exposure_pct": exposure_pct,
        "growth_on_start_pct": growth_pct,
    }


def compute_live_tracker_bankroll_eur(conn: sqlite3.Connection) -> dict:
    """Available bankroll for Kelly staking (Live Tracker + Paris du jour + in-play).

    Ledger: start + sum(settled profits) - sum(open stakes).

    Settled stakes are reflected in profits (lost => negative profit).

    Pending bets imply stake temporarily unavailable.
    """
    ensure_user_bets_schema(conn)
    start = get_live_tracker_start_br(conn)
    src_ph = ",".join("?" * len(APP_KELLY_TRACKER_SOURCES))
    cur = conn.execute(
        f"""
        SELECT COALESCE(SUM(stake), 0)
        FROM user_bets
        WHERE tracker_source IN ({src_ph})
          AND COALESCE(TRIM(status), '') = 'En cours'
        """,
        APP_KELLY_TRACKER_SOURCES,
    )
    open_stakes = float(cur.fetchone()[0] or 0.0)
    cur2 = conn.execute(
        f"""
        SELECT COALESCE(SUM(profit), 0)
        FROM user_bets
        WHERE tracker_source IN ({src_ph})
          AND COALESCE(TRIM(status), '') != 'En cours'
        """,
        APP_KELLY_TRACKER_SOURCES,
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


def ensure_algo_opportunities_schema(conn: sqlite3.Connection) -> None:
    """Persist every live value opportunity so past days remain auditable."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS algo_opportunities (
            opportunity_key TEXT PRIMARY KEY,
            detected_date TEXT NOT NULL,
            detected_ts TEXT,
            match_date TEXT,
            match_id TEXT,
            match_name TEXT NOT NULL,
            player1 TEXT,
            player2 TEXT,
            bet_on TEXT NOT NULL,
            side INTEGER,
            tour TEXT,
            surface TEXT,
            tournament TEXT,
            odd_book REAL,
            true_odd REAL,
            p_model REAL,
            p_implicit REAL,
            ev REAL,
            confidence REAL,
            segment_key TEXT,
            segment_brier REAL,
            sharpe_ratio REAL,
            sharpe_per_brier REAL,
            priority_score REAL,
            snapshot_tier TEXT,
            status TEXT DEFAULT 'En cours',
            theoretical_stake_frac REAL DEFAULT 0.0,
            theoretical_profit REAL DEFAULT 0.0,
            result_source TEXT,
            settled_ts TEXT,
            score_final TEXT,
            linked_bet_id INTEGER,
            real_odd REAL,
            real_stake REAL,
            real_profit REAL,
            updated_at TEXT
        )
        """
    )
    have = _existing_columns(conn, "algo_opportunities")
    if "real_odd" not in have:
        cur.execute("ALTER TABLE algo_opportunities ADD COLUMN real_odd REAL")
    if "theoretical_stake_frac" not in have:
        cur.execute("ALTER TABLE algo_opportunities ADD COLUMN theoretical_stake_frac REAL DEFAULT 0.0")
    if "data_reliability_score" not in have:
        cur.execute("ALTER TABLE algo_opportunities ADD COLUMN data_reliability_score INTEGER")
    if "data_reliability_flags" not in have:
        cur.execute("ALTER TABLE algo_opportunities ADD COLUMN data_reliability_flags TEXT")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_algo_opp_detected_date ON algo_opportunities(detected_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_algo_opp_match_date ON algo_opportunities(match_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_algo_opp_status ON algo_opportunities(status)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_algo_opp_match_id ON algo_opportunities(match_id)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_algo_opp_match_name_bet ON algo_opportunities(match_name, bet_on)"
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
        ORDER BY
            CASE
                WHEN winner_canonical IS NOT NULL OR walkover = 1 THEN 2
                WHEN COALESCE(score, '') <> '' THEN 1
                ELSE 0
            END ASC,
            COALESCE(scraped_at, '') ASC
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
            from scripts.scraper_results import match_result_effective_retired

            retired_eff = match_result_effective_retired(
                retired=bool(r.get("retired")),
                walkover=bool(r.get("walkover")),
                score=r.get("score"),
            )
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
                    1 if retired_eff else 0,
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
    telegram_user_id: Optional[str] = None,
    web_username: Optional[str] = None,
) -> int:
    """Insert a bet with full decision context. Returns the new bet id.

    ``match_date`` (YYYY-MM-DD): scheduled match day from the fixture. The ``date``
    column stays as SQLite ``date('now')`` (day the ticket was recorded). Result
    resolution uses ``match_date`` when present, otherwise ``date``.
    """
    conn = open_db(db_path)
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
                bookmaker_source, placed_ts, notes, tracker_source,
                telegram_user_id, web_username
            ) VALUES (date('now'), ?, ?, ?, ?, 'En cours', 0.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(telegram_user_id).strip() if telegram_user_id else None,
                str(web_username).strip().lower() if web_username else None,
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
# Algo opportunities journal
# ---------------------------------------------------------------------------


def _none_if_blank(value):
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _float_or_none(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _algo_kelly_stake_frac(
    p_model: object,
    odd: object,
    segment_brier: object,
) -> float:
    """Live staking policy: 1/2 Kelly, Brier indexed, capped at 15% bankroll."""
    try:
        p = max(0.0, min(1.0, float(p_model)))
        o = float(odd)
    except (TypeError, ValueError):
        return 0.0
    if o <= 1.0 or p <= 0.0 or p >= 1.0:
        return 0.0
    b = max(0.01, o - 1.0)
    kelly_full = max(0.0, (b * p - (1.0 - p)) / b)
    try:
        brier = float(segment_brier)
    except (TypeError, ValueError):
        brier = 0.25
    brier_factor = max(0.0, 1.0 - (brier / ALGO_OPP_BRIER_CAP))
    stake_frac = ALGO_OPP_KELLY_BASE_FRAC * kelly_full * brier_factor
    return float(max(0.0, min(stake_frac, ALGO_OPP_KELLY_MAX_STAKE_FRAC)))


def _algo_profit_for_status(status: str, odd: object, stake_frac: object) -> float:
    try:
        stake = float(stake_frac or 0.0)
        o = float(odd or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if status == "Gagné":
        return stake * max(0.0, o - 1.0)
    if status == "Perdu":
        return -stake
    return 0.0


def _apply_composite_ordered_theoretical_staking(
    rows: list[dict],
    *,
    starting_bankroll: float = 1.0,
) -> list[dict]:
    """Apply daily liquidity and carry each closing bankroll into the next day."""
    out = [dict(r) for r in rows]
    by_day: dict[str, list[dict]] = {}
    for r in out:
        day = str(r.get("detected_date") or r.get("match_date") or "")
        by_day.setdefault(day, []).append(r)
        r["theoretical_stake_used_frac"] = 0.0
        r["theoretical_profit_ordered"] = 0.0
        r["theoretical_bankroll_start"] = None
        r["theoretical_bankroll_end"] = None
        r["theoretical_stake_used_eur"] = 0.0
        r["theoretical_profit_eur"] = 0.0
    bankroll = max(0.0, float(starting_bankroll or 0.0))
    for _day in sorted(by_day):
        day_rows = by_day[_day]
        day_start = bankroll
        liquid = day_start
        day_profit = 0.0
        ordered = sorted(
            day_rows,
            key=lambda r: (
                float(r.get("priority_score") or 0.0),
                float(r.get("ev") or 0.0),
            ),
            reverse=True,
        )
        for r in ordered:
            r["theoretical_bankroll_start"] = day_start
            if r.get("status") not in ("Gagné", "Perdu", "Annulé"):
                continue
            if r.get("status") == "Annulé":
                continue
            recommended = max(0.0, float(r.get("theoretical_stake_frac") or 0.0))
            recommended_eur = day_start * recommended
            stake_eur = min(recommended_eur, max(0.0, liquid))
            stake_used_frac = (stake_eur / day_start) if day_start > 0 else 0.0
            profit_eur = _algo_profit_for_status(
                str(r.get("status") or ""),
                r.get("odd_book"),
                stake_eur,
            )
            r["theoretical_stake_used_frac"] = stake_used_frac
            r["theoretical_stake_used_eur"] = stake_eur
            r["theoretical_profit_ordered"] = profit_eur
            r["theoretical_profit_eur"] = profit_eur
            day_profit += profit_eur
            liquid = max(0.0, liquid - stake_eur)
            if liquid <= 1e-12:
                liquid = 0.0
        bankroll = max(0.0, day_start + day_profit)
        for r in day_rows:
            r["theoretical_bankroll_start"] = day_start
            r["theoretical_bankroll_end"] = bankroll
    return out


def upsert_algo_opportunities(
    rows: Iterable[dict],
    db_path: str = DB_PATH_DEFAULT,
) -> int:
    """Insert/update detected live opportunities without losing resolved status."""
    rows_list = list(rows or [])
    if not rows_list:
        return 0
    conn = open_db(db_path)
    try:
        ensure_algo_opportunities_schema(conn)
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        cur = conn.cursor()
        n = 0
        for r in rows_list:
            key = _none_if_blank(r.get("opportunity_key"))
            match_name = _none_if_blank(r.get("match_name"))
            bet_on = _none_if_blank(r.get("bet_on"))
            detected_date = _none_if_blank(r.get("detected_date"))
            if not key or not match_name or not bet_on or not detected_date:
                continue
            stake_frac = _algo_kelly_stake_frac(
                r.get("p_model"),
                r.get("odd_book"),
                r.get("segment_brier"),
            )
            cur.execute(
                """
                INSERT INTO algo_opportunities (
                    opportunity_key, detected_date, detected_ts, match_date, match_id,
                    match_name, player1, player2, bet_on, side, tour, surface, tournament,
                    odd_book, true_odd, p_model, p_implicit, ev, confidence,
                    segment_key, segment_brier, sharpe_ratio, sharpe_per_brier,
                    priority_score, snapshot_tier, theoretical_stake_frac,
                    data_reliability_score, data_reliability_flags, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(opportunity_key) DO UPDATE SET
                    detected_ts=excluded.detected_ts,
                    match_date=COALESCE(excluded.match_date, algo_opportunities.match_date),
                    match_id=COALESCE(excluded.match_id, algo_opportunities.match_id),
                    match_name=excluded.match_name,
                    player1=excluded.player1,
                    player2=excluded.player2,
                    bet_on=excluded.bet_on,
                    side=excluded.side,
                    tour=excluded.tour,
                    surface=excluded.surface,
                    tournament=excluded.tournament,
                    odd_book=excluded.odd_book,
                    true_odd=excluded.true_odd,
                    p_model=excluded.p_model,
                    p_implicit=excluded.p_implicit,
                    ev=excluded.ev,
                    confidence=excluded.confidence,
                    segment_key=excluded.segment_key,
                    segment_brier=excluded.segment_brier,
                    sharpe_ratio=excluded.sharpe_ratio,
                    sharpe_per_brier=excluded.sharpe_per_brier,
                    priority_score=excluded.priority_score,
                    snapshot_tier=excluded.snapshot_tier,
                    theoretical_stake_frac=excluded.theoretical_stake_frac,
                    data_reliability_score=excluded.data_reliability_score,
                    data_reliability_flags=excluded.data_reliability_flags,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    detected_date,
                    _none_if_blank(r.get("detected_ts")) or now_iso,
                    normalize_schedule_date(r.get("match_date")),
                    _none_if_blank(r.get("match_id")),
                    match_name,
                    _none_if_blank(r.get("player1")),
                    _none_if_blank(r.get("player2")),
                    bet_on,
                    int(r.get("side") or 0) if r.get("side") is not None else None,
                    _none_if_blank(r.get("tour")),
                    _none_if_blank(r.get("surface")),
                    _none_if_blank(r.get("tournament")),
                    _float_or_none(r.get("odd_book")),
                    _float_or_none(r.get("true_odd")),
                    _float_or_none(r.get("p_model")),
                    _float_or_none(r.get("p_implicit")),
                    _float_or_none(r.get("ev")),
                    _float_or_none(r.get("confidence")),
                    _none_if_blank(r.get("segment_key")),
                    _float_or_none(r.get("segment_brier")),
                    _float_or_none(r.get("sharpe_ratio")),
                    _float_or_none(r.get("sharpe_per_brier")),
                    _float_or_none(r.get("priority_score")),
                    _none_if_blank(r.get("snapshot_tier")),
                    stake_frac,
                    int(r["data_reliability_score"])
                    if r.get("data_reliability_score") is not None
                    else None,
                    _none_if_blank(r.get("data_reliability_flags")),
                    now_iso,
                ),
            )
            n += 1
        conn.commit()
        dedupe_algo_opportunities(conn)
        sync_algo_opportunities_from_bets(conn)
        sync_algo_opportunities_from_results(conn)
        return n
    finally:
        conn.close()


def dedupe_algo_opportunities(conn: sqlite3.Connection) -> int:
    """Remove duplicate daily opportunities caused by unstable snapshot match IDs."""
    ensure_algo_opportunities_schema(conn)
    dup_groups = conn.execute(
        """
        SELECT detected_date, lower(trim(match_name)) AS match_key, lower(trim(bet_on)) AS bet_key
        FROM algo_opportunities
        WHERE detected_date IS NOT NULL
          AND match_name IS NOT NULL
          AND bet_on IS NOT NULL
        GROUP BY detected_date, match_key, bet_key
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    n_deleted = 0
    for detected_date, match_key, bet_key in dup_groups:
        rows = conn.execute(
            """
            SELECT rowid, opportunity_key
            FROM algo_opportunities
            WHERE detected_date = ?
              AND lower(trim(match_name)) = ?
              AND lower(trim(bet_on)) = ?
            ORDER BY
              CASE WHEN linked_bet_id IS NOT NULL THEN 1 ELSE 0 END DESC,
              CASE WHEN COALESCE(status, 'En cours') <> 'En cours' THEN 1 ELSE 0 END DESC,
              COALESCE(priority_score, 0) DESC,
              COALESCE(ev, 0) DESC,
              COALESCE(updated_at, detected_ts, '') DESC,
              rowid DESC
            """,
            (detected_date, match_key, bet_key),
        ).fetchall()
        if len(rows) <= 1:
            continue
        keep_rowid = int(rows[0][0])
        delete_rowids = [int(r[0]) for r in rows[1:] if int(r[0]) != keep_rowid]
        if not delete_rowids:
            continue
        placeholders = ",".join("?" for _ in delete_rowids)
        conn.execute(
            f"DELETE FROM algo_opportunities WHERE rowid IN ({placeholders})",
            delete_rowids,
        )
        n_deleted += len(delete_rowids)
    if n_deleted:
        conn.commit()
    return n_deleted


def sync_algo_opportunities_from_bets(conn: sqlite3.Connection) -> int:
    """Attach real portfolio bets to detected opportunities when possible."""
    ensure_algo_opportunities_schema(conn)
    ensure_user_bets_schema(conn)
    opps = conn.execute(
        """
        SELECT opportunity_key, match_id, match_date, match_name, bet_on
        FROM algo_opportunities
        """
    ).fetchall()
    n = 0
    for key, match_id, match_date, match_name, bet_on in opps:
        bet = None
        if match_id:
            bet = conn.execute(
                """
                SELECT id, odds, stake, profit
                FROM user_bets
                WHERE match_id = ? AND bet_on = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (match_id, bet_on),
            ).fetchone()
        if bet is None:
            bet = conn.execute(
                """
                SELECT id, odds, stake, profit
                FROM user_bets
                WHERE match_name = ?
                  AND bet_on = ?
                  AND COALESCE(match_date, date) = COALESCE(?, COALESCE(match_date, date))
                ORDER BY id DESC
                LIMIT 1
                """,
                (match_name, bet_on, match_date),
            ).fetchone()
        if bet is None:
            continue
        conn.execute(
            """
            UPDATE algo_opportunities
            SET linked_bet_id = ?, real_odd = ?, real_stake = ?, real_profit = ?,
                updated_at = ?
            WHERE opportunity_key = ?
            """,
            (
                int(bet[0]),
                float(bet[1] or 0.0),
                float(bet[2] or 0.0),
                float(bet[3] or 0.0),
                datetime.utcnow().isoformat(timespec="seconds"),
                key,
            ),
        )
        n += 1
    conn.commit()
    return n


def refresh_algo_opportunity_staking(conn: sqlite3.Connection) -> int:
    """Recompute theoretical stake/profit with current live staking policy."""
    ensure_algo_opportunities_schema(conn)
    rows = conn.execute(
        """
        SELECT opportunity_key, status, p_model, odd_book, segment_brier
        FROM algo_opportunities
        """
    ).fetchall()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    n = 0
    for key, status, p_model, odd_book, segment_brier in rows:
        stake_frac = _algo_kelly_stake_frac(p_model, odd_book, segment_brier)
        profit = _algo_profit_for_status(str(status or ""), odd_book, stake_frac)
        conn.execute(
            """
            UPDATE algo_opportunities
            SET theoretical_stake_frac = ?, theoretical_profit = ?, updated_at = ?
            WHERE opportunity_key = ?
            """,
            (stake_frac, profit, now_iso, key),
        )
        n += 1
    conn.commit()
    return n


def sync_algo_opportunities_from_results(conn: sqlite3.Connection) -> int:
    """Resolve theoretical Kelly outcome from the match-results cache."""
    ensure_algo_opportunities_schema(conn)
    ensure_match_results_cache(conn)
    try:
        from scripts.scraper_results import canonical_player, names_match
    except Exception:
        return 0
    rows = conn.execute(
        """
        SELECT opportunity_key, match_date, detected_date, player1, player2, bet_on,
               odd_book, p_model, segment_brier
        FROM algo_opportunities
        WHERE COALESCE(status, 'En cours') = 'En cours'
          AND match_date IS NOT NULL
          AND player1 IS NOT NULL
          AND player2 IS NOT NULL
        """
    ).fetchall()
    n = 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    for key, match_date, detected_date, p1, p2, bet_on, odd_book, p_model, segment_brier in rows:
        p1c = canonical_player(p1)
        p2c = canonical_player(p2)
        md = str(match_date or "")[:10]
        hit = _lookup_match_result_for_players(
            conn,
            md,
            p1c,
            p2c,
            calendar_date=str(detected_date or md)[:10] or None,
        )
        if not hit:
            continue
        winner, score, walkover, retired, source, _resolved_date = hit
        retired_eff = _retired_from_match_hit(
            walkover=bool(walkover), retired=bool(retired), score=score
        )
        stake_frac = _algo_kelly_stake_frac(p_model, odd_book, segment_brier)
        status, _won_flag = _resolve_bet_status_from_match_result(
            bet_on,
            winner,
            walkover=bool(walkover),
            retired=retired_eff,
        )
        if status == "En cours":
            continue
        profit = 0.0 if status == "Annulé" else _algo_profit_for_status(status, odd_book, stake_frac)
        conn.execute(
            """
            UPDATE algo_opportunities
            SET status = ?, theoretical_stake_frac = ?, theoretical_profit = ?, result_source = ?,
                score_final = ?, settled_ts = ?, updated_at = ?
            WHERE opportunity_key = ?
            """,
            (status, stake_frac, profit, source, score, now_iso, now_iso, key),
        )
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# daily_top_proba_picks — top N probas favori/jour (ATP & WTA) pour replay réel
# ---------------------------------------------------------------------------

def ensure_daily_top_proba_schema(conn: sqlite3.Connection) -> None:
    """Table canonique + index pour replay top probas journalier."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_top_proba_picks (
            pick_key TEXT PRIMARY KEY,
            calendar_date TEXT NOT NULL,
            match_date TEXT NOT NULL,
            tour TEXT NOT NULL,
            rank INTEGER NOT NULL,
            top_limit INTEGER NOT NULL DEFAULT 15,
            match_id TEXT,
            match_name TEXT NOT NULL,
            player1 TEXT,
            player2 TEXT,
            fav_side INTEGER,
            fav_player TEXT,
            underdog_player TEXT,
            p1_prob REAL,
            p_model_fav REAL,
            odd_fav REAL,
            odd_underdog REAL,
            true_odd_fav REAL,
            true_odd_underdog REAL,
            ev_fav REAL,
            ev_fav_pct REAL,
            p_implicit_fav REAL,
            book_gap_pp REAL,
            tournament TEXT,
            surface TEXT,
            match_time TEXT,
            tourney_level TEXT,
            confidence REAL,
            segment_key TEXT,
            segment_brier REAL,
            theoretical_stake_frac REAL DEFAULT 0.0,
            snapshot_built_at REAL,
            snapshot_tier TEXT,
            capture_source TEXT,
            first_captured_ts TEXT,
            last_captured_ts TEXT,
            status TEXT DEFAULT 'En cours',
            fav_won INTEGER,
            winner_resolved TEXT,
            score_final TEXT,
            result_source TEXT,
            theoretical_profit REAL DEFAULT 0.0,
            settled_ts TEXT,
            updated_at TEXT,
            UNIQUE(calendar_date, tour, rank)
        )
        """
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_top_proba_cal ON daily_top_proba_picks(calendar_date)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_top_proba_match ON daily_top_proba_picks(match_date, tour)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_top_proba_status ON daily_top_proba_picks(status)"
    )
    have = _existing_columns(conn, "daily_top_proba_picks")
    if "data_reliability_score" not in have:
        cur.execute(
            "ALTER TABLE daily_top_proba_picks ADD COLUMN data_reliability_score INTEGER"
        )
    if "data_reliability_flags" not in have:
        cur.execute(
            "ALTER TABLE daily_top_proba_picks ADD COLUMN data_reliability_flags TEXT"
        )
    conn.commit()


def upsert_daily_top_proba_picks(
    rows: Iterable[dict],
    db_path: str = DB_PATH_DEFAULT,
) -> int:
    """Upsert le top N du jour (dernière capture par rang) ; préserve first_captured_ts."""
    rows_list = list(rows or [])
    if not rows_list:
        return 0
    conn = open_db(db_path)
    try:
        ensure_daily_top_proba_schema(conn)
        now_iso = datetime.utcnow().isoformat(timespec="seconds")
        cur = conn.cursor()
        n = 0
        for r in rows_list:
            key = _none_if_blank(r.get("pick_key"))
            cal = _none_if_blank(r.get("calendar_date"))
            tour = _none_if_blank(r.get("tour"))
            rank = int(r.get("rank") or 0)
            match_name = _none_if_blank(r.get("match_name"))
            if not key or not cal or not tour or rank <= 0 or not match_name:
                continue
            existing = cur.execute(
                "SELECT first_captured_ts FROM daily_top_proba_picks WHERE pick_key = ?",
                (key,),
            ).fetchone()
            first_ts = existing[0] if existing and existing[0] else (
                _none_if_blank(r.get("first_captured_ts")) or now_iso
            )
            last_ts = _none_if_blank(r.get("last_captured_ts")) or now_iso
            cur.execute(
                """
                INSERT INTO daily_top_proba_picks (
                    pick_key, calendar_date, match_date, tour, rank, top_limit,
                    match_id, match_name, player1, player2, fav_side, fav_player, underdog_player,
                    p1_prob, p_model_fav, odd_fav, odd_underdog, true_odd_fav, true_odd_underdog,
                    ev_fav, ev_fav_pct, p_implicit_fav, book_gap_pp,
                    tournament, surface, match_time, tourney_level, confidence,
                    segment_key, segment_brier, theoretical_stake_frac,
                    snapshot_built_at, snapshot_tier, capture_source,
                    data_reliability_score, data_reliability_flags,
                    first_captured_ts, last_captured_ts, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(pick_key) DO UPDATE SET
                    match_date=excluded.match_date,
                    top_limit=excluded.top_limit,
                    match_id=excluded.match_id,
                    match_name=excluded.match_name,
                    player1=excluded.player1,
                    player2=excluded.player2,
                    fav_side=excluded.fav_side,
                    fav_player=excluded.fav_player,
                    underdog_player=excluded.underdog_player,
                    p1_prob=excluded.p1_prob,
                    p_model_fav=excluded.p_model_fav,
                    odd_fav=excluded.odd_fav,
                    odd_underdog=excluded.odd_underdog,
                    true_odd_fav=excluded.true_odd_fav,
                    true_odd_underdog=excluded.true_odd_underdog,
                    ev_fav=excluded.ev_fav,
                    ev_fav_pct=excluded.ev_fav_pct,
                    p_implicit_fav=excluded.p_implicit_fav,
                    book_gap_pp=excluded.book_gap_pp,
                    tournament=excluded.tournament,
                    surface=excluded.surface,
                    match_time=excluded.match_time,
                    tourney_level=excluded.tourney_level,
                    confidence=excluded.confidence,
                    segment_key=excluded.segment_key,
                    segment_brier=excluded.segment_brier,
                    theoretical_stake_frac=excluded.theoretical_stake_frac,
                    snapshot_built_at=excluded.snapshot_built_at,
                    snapshot_tier=excluded.snapshot_tier,
                    capture_source=excluded.capture_source,
                    data_reliability_score=excluded.data_reliability_score,
                    data_reliability_flags=excluded.data_reliability_flags,
                    last_captured_ts=excluded.last_captured_ts,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    cal,
                    normalize_schedule_date(r.get("match_date")) or cal,
                    tour,
                    rank,
                    int(r.get("top_limit") or 15),
                    _none_if_blank(r.get("match_id")),
                    match_name,
                    _none_if_blank(r.get("player1")),
                    _none_if_blank(r.get("player2")),
                    int(r.get("fav_side") or 0) if r.get("fav_side") is not None else None,
                    _none_if_blank(r.get("fav_player")),
                    _none_if_blank(r.get("underdog_player")),
                    _float_or_none(r.get("p1_prob")),
                    _float_or_none(r.get("p_model_fav")),
                    _float_or_none(r.get("odd_fav")),
                    _float_or_none(r.get("odd_underdog")),
                    _float_or_none(r.get("true_odd_fav")),
                    _float_or_none(r.get("true_odd_underdog")),
                    _float_or_none(r.get("ev_fav")),
                    _float_or_none(r.get("ev_fav_pct")),
                    _float_or_none(r.get("p_implicit_fav")),
                    _float_or_none(r.get("book_gap_pp")),
                    _none_if_blank(r.get("tournament")),
                    _none_if_blank(r.get("surface")),
                    _none_if_blank(r.get("match_time")),
                    _none_if_blank(r.get("tourney_level")),
                    _float_or_none(r.get("confidence")),
                    _none_if_blank(r.get("segment_key")),
                    _float_or_none(r.get("segment_brier")),
                    _float_or_none(r.get("theoretical_stake_frac")),
                    _float_or_none(r.get("snapshot_built_at")),
                    _none_if_blank(r.get("snapshot_tier")),
                    _none_if_blank(r.get("capture_source")),
                    int(r["data_reliability_score"])
                    if r.get("data_reliability_score") is not None
                    else None,
                    _none_if_blank(r.get("data_reliability_flags")),
                    first_ts,
                    last_ts,
                    now_iso,
                ),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def _retired_from_match_hit(
    *,
    walkover: bool,
    retired: bool,
    score: str | None,
) -> bool:
    from scripts.scraper_results import match_result_effective_retired

    return match_result_effective_retired(
        retired=bool(retired),
        walkover=bool(walkover),
        score=score,
    )


def _resolve_bet_status_from_match_result(
    bet_on: str,
    winner: str | None,
    *,
    walkover: bool,
    retired: bool,
) -> tuple[str, int | None]:
    """Walkover / retirement du pari → Annulé ; sinon Gagné/Perdu sur le vainqueur."""
    if walkover:
        return "Annulé", None
    if not winner:
        return "En cours", None
    try:
        from scripts.scraper_results import canonical_player, names_match

        won = names_match(canonical_player(bet_on), canonical_player(winner))
    except Exception:
        return "En cours", None
    if retired and not won:
        return "Annulé", None
    return ("Gagné" if won else "Perdu"), (1 if won else 0)


def _lookup_match_result_for_players(
    conn: sqlite3.Connection,
    match_date: str,
    p1c: str,
    p2c: str,
    *,
    calendar_date: str | None = None,
    date_window_days: int = 3,
) -> tuple | None:
    """Résultat TE/Sackmann : date exacte puis fenêtre autour du jour match / capture."""
    exact = conn.execute(
        """
        SELECT winner_canonical, score, walkover, retired, source, match_date
        FROM match_results
        WHERE match_date = ?
          AND (
            (p1_canonical = ? AND p2_canonical = ?)
            OR (p1_canonical = ? AND p2_canonical = ?)
          )
          AND winner_canonical IS NOT NULL
          AND TRIM(winner_canonical) != ''
        ORDER BY source = 'tennisexplorer' DESC, scraped_at DESC
        LIMIT 1
        """,
        (match_date, p1c, p2c, p2c, p1c),
    ).fetchone()
    if exact:
        return exact

    anchor = str(calendar_date or match_date or "")[:10]
    if not anchor:
        return None
    window = max(1, int(date_window_days))
    return conn.execute(
        """
        SELECT winner_canonical, score, walkover, retired, source, match_date
        FROM match_results
        WHERE (
            (p1_canonical = ? AND p2_canonical = ?)
            OR (p1_canonical = ? AND p2_canonical = ?)
          )
          AND winner_canonical IS NOT NULL
          AND TRIM(winner_canonical) != ''
          AND julianday(match_date) BETWEEN julianday(?) - ? AND julianday(?) + ?
        ORDER BY ABS(julianday(match_date) - julianday(?)) ASC,
                 source = 'tennisexplorer' DESC,
                 scraped_at DESC
        LIMIT 1
        """,
        (p1c, p2c, p2c, p1c, anchor, window, anchor, window, match_date or anchor),
    ).fetchone()


def _correct_retirement_voids_daily_top_proba(conn: sqlite3.Connection) -> int:
    """Corrige les Perdu déjà réglés quand le favori a abandonné (stake remboursé)."""
    ensure_daily_top_proba_schema(conn)
    ensure_match_results_cache(conn)
    try:
        from scripts.scraper_results import canonical_player
    except Exception:
        return 0
    rows = conn.execute(
        """
        SELECT pick_key, calendar_date, match_date, player1, player2, fav_player,
               odd_fav, p_model_fav, segment_brier, theoretical_stake_frac
        FROM daily_top_proba_picks
        WHERE status = 'Perdu'
          AND match_date IS NOT NULL
          AND player1 IS NOT NULL
          AND player2 IS NOT NULL
          AND fav_player IS NOT NULL
        """
    ).fetchall()
    n = 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    for key, calendar_date, match_date, p1, p2, fav_player, odd_fav, p_model_fav, segment_brier, stake_frac in rows:
        p1c = canonical_player(p1)
        p2c = canonical_player(p2)
        hit = _lookup_match_result_for_players(
            conn,
            str(match_date or "")[:10],
            p1c,
            p2c,
            calendar_date=str(calendar_date or "")[:10] or None,
        )
        if not hit:
            continue
        winner, score, walkover, retired, source, _resolved_date = hit
        retired_eff = _retired_from_match_hit(
            walkover=bool(walkover), retired=bool(retired), score=score
        )
        status, fav_won = _resolve_bet_status_from_match_result(
            fav_player,
            winner,
            walkover=bool(walkover),
            retired=retired_eff,
        )
        if status != "Annulé":
            continue
        stake_frac = float(stake_frac or 0.0) or _algo_kelly_stake_frac(
            p_model_fav, odd_fav, segment_brier
        )
        conn.execute(
            """
            UPDATE daily_top_proba_picks
            SET status = ?, fav_won = ?, winner_resolved = ?, score_final = ?,
                result_source = ?, theoretical_stake_frac = ?, theoretical_profit = ?,
                settled_ts = ?, updated_at = ?
            WHERE pick_key = ?
            """,
            (
                status,
                fav_won,
                winner,
                score,
                source,
                stake_frac,
                0.0,
                now_iso,
                now_iso,
                key,
            ),
        )
        n += 1
    if n:
        conn.commit()
    return n


def _correct_false_retirement_voids_daily_top_proba(conn: sqlite3.Connection) -> int:
    """Corrige les Annulé erronés (retired DB faux positif) → Gagné/Perdu."""
    ensure_daily_top_proba_schema(conn)
    ensure_match_results_cache(conn)
    try:
        from scripts.scraper_results import canonical_player
    except Exception:
        return 0
    rows = conn.execute(
        """
        SELECT pick_key, calendar_date, match_date, player1, player2, fav_player,
               odd_fav, p_model_fav, segment_brier, theoretical_stake_frac
        FROM daily_top_proba_picks
        WHERE status = 'Annulé'
          AND match_date IS NOT NULL
          AND player1 IS NOT NULL
          AND player2 IS NOT NULL
          AND fav_player IS NOT NULL
        """
    ).fetchall()
    n = 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    for key, calendar_date, match_date, p1, p2, fav_player, odd_fav, p_model_fav, segment_brier, stake_frac in rows:
        p1c = canonical_player(p1)
        p2c = canonical_player(p2)
        hit = _lookup_match_result_for_players(
            conn,
            str(match_date or "")[:10],
            p1c,
            p2c,
            calendar_date=str(calendar_date or "")[:10] or None,
        )
        if not hit:
            continue
        winner, score, walkover, retired, source, _resolved_date = hit
        retired_eff = _retired_from_match_hit(
            walkover=bool(walkover), retired=bool(retired), score=score
        )
        status, fav_won = _resolve_bet_status_from_match_result(
            fav_player,
            winner,
            walkover=bool(walkover),
            retired=retired_eff,
        )
        if status == "Annulé":
            continue
        stake_frac = float(stake_frac or 0.0) or _algo_kelly_stake_frac(
            p_model_fav, odd_fav, segment_brier
        )
        profit = _algo_profit_for_status(status, odd_fav, stake_frac)
        conn.execute(
            """
            UPDATE daily_top_proba_picks
            SET status = ?, fav_won = ?, winner_resolved = ?, score_final = ?,
                result_source = ?, theoretical_stake_frac = ?, theoretical_profit = ?,
                settled_ts = ?, updated_at = ?
            WHERE pick_key = ?
            """,
            (
                status,
                fav_won,
                winner,
                score,
                source or "false_void_correction",
                stake_frac,
                profit,
                now_iso,
                now_iso,
                key,
            ),
        )
        n += 1
    if n:
        conn.commit()
    return n


def correct_retirement_voids_user_bets(conn: sqlite3.Connection) -> int:
    """Corrige les Perdu déjà réglés quand le parié a abandonné (stake remboursé)."""
    ensure_user_bets_schema(conn)
    ensure_match_results_cache(conn)
    try:
        from scripts.scraper_results import canonical_player
    except Exception:
        return 0
    rows = conn.execute(
        """
        SELECT id, match_date, date, match_name, bet_on, score_final
        FROM user_bets
        WHERE status = 'Perdu'
          AND bet_on IS NOT NULL
          AND TRIM(bet_on) != ''
          AND match_name IS NOT NULL
          AND TRIM(match_name) != ''
        """
    ).fetchall()
    n = 0
    for bet_id, match_date, placement_date, match_name, bet_on, existing_score in rows:
        parts = (match_name or "").split(" vs ")
        if len(parts) != 2:
            continue
        p1_raw, p2_raw = parts[0].strip(), parts[1].strip()
        p1c = canonical_player(p1_raw)
        p2c = canonical_player(p2_raw)
        resolve_date = str(match_date or placement_date or "")[:10]
        if not resolve_date:
            continue
        hit = _lookup_match_result_for_players(
            conn,
            resolve_date,
            p1c,
            p2c,
            calendar_date=str(placement_date or "")[:10] or None,
        )
        if not hit:
            continue
        winner, score, walkover, retired, source, _resolved_date = hit
        # Only trust explicit retired flag — score_suggests_retirement false-positives
        # on TE multi-set formats (e.g. "6 7 6 | 4 5 4").
        score_text = score or existing_score or ""
        retired_eff = _retired_from_match_hit(
            walkover=bool(walkover), retired=bool(retired), score=score_text
        )
        status, _won = _resolve_bet_status_from_match_result(
            bet_on,
            winner,
            walkover=bool(walkover),
            retired=retired_eff,
        )
        if status != "Annulé":
            continue
        settle_bet(
            conn,
            bet_id=int(bet_id),
            status="Annulé",
            profit=0.0,
            winner_resolved=winner,
            score_final=score_text or None,
            result_source=source or "retirement_void_correction",
        )
        n += 1
    return n


def sync_daily_top_proba_from_results(conn: sqlite3.Connection) -> int:
    """Résout Gagné/Perdu sur le favori modèle via le cache match_results."""
    ensure_daily_top_proba_schema(conn)
    ensure_match_results_cache(conn)
    try:
        from scripts.scraper_results import canonical_player, names_match
    except Exception:
        return 0
    rows = conn.execute(
        """
        SELECT pick_key, calendar_date, match_date, player1, player2, fav_player, odd_fav,
               p_model_fav, segment_brier, theoretical_stake_frac
        FROM daily_top_proba_picks
        WHERE COALESCE(status, 'En cours') = 'En cours'
          AND match_date IS NOT NULL
          AND player1 IS NOT NULL
          AND player2 IS NOT NULL
          AND fav_player IS NOT NULL
        """
    ).fetchall()
    n = 0
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    for key, calendar_date, match_date, p1, p2, fav_player, odd_fav, p_model_fav, segment_brier, stake_frac in rows:
        p1c = canonical_player(p1)
        p2c = canonical_player(p2)
        hit = _lookup_match_result_for_players(
            conn,
            str(match_date or "")[:10],
            p1c,
            p2c,
            calendar_date=str(calendar_date or "")[:10] or None,
        )
        if not hit:
            continue
        winner, score, walkover, retired, source, _resolved_date = hit
        retired_eff = _retired_from_match_hit(
            walkover=bool(walkover), retired=bool(retired), score=score
        )
        stake_frac = float(stake_frac or 0.0) or _algo_kelly_stake_frac(
            p_model_fav, odd_fav, segment_brier
        )
        status, fav_won = _resolve_bet_status_from_match_result(
            fav_player,
            winner,
            walkover=bool(walkover),
            retired=retired_eff,
        )
        if status == "En cours":
            continue
        profit = 0.0 if status == "Annulé" else _algo_profit_for_status(status, odd_fav, stake_frac)
        conn.execute(
            """
            UPDATE daily_top_proba_picks
            SET status = ?, fav_won = ?, winner_resolved = ?, score_final = ?,
                result_source = ?, theoretical_stake_frac = ?, theoretical_profit = ?,
                settled_ts = ?, updated_at = ?
            WHERE pick_key = ?
            """,
            (
                status,
                fav_won,
                winner,
                score,
                source,
                stake_frac,
                profit,
                now_iso,
                now_iso,
                key,
            ),
        )
        n += 1
    conn.commit()
    n += _correct_false_retirement_voids_daily_top_proba(conn)
    n += _correct_retirement_voids_daily_top_proba(conn)
    n += correct_retirement_voids_user_bets(conn)
    return n


def read_daily_top_proba_picks(
    *,
    calendar_date: str | None = None,
    tour: str | None = None,
    db_path: str = DB_PATH_DEFAULT,
) -> list[dict]:
    """Lecture pour replay / export."""
    conn = open_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_daily_top_proba_schema(conn)
        sync_daily_top_proba_from_results(conn)
        where = []
        params: list[object] = []
        if calendar_date:
            where.append("calendar_date = ?")
            params.append(calendar_date)
        if tour:
            where.append("tour = ?")
            params.append(str(tour).upper())
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"""
            SELECT * FROM daily_top_proba_picks
            {where_sql}
            ORDER BY calendar_date DESC, tour ASC, rank ASC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def read_algo_opportunity_dates(db_path: str = DB_PATH_DEFAULT) -> list[str]:
    conn = open_db(db_path)
    try:
        ensure_algo_opportunities_schema(conn)
        return [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT detected_date FROM algo_opportunities ORDER BY detected_date DESC"
            ).fetchall()
            if r[0]
        ]
    finally:
        conn.close()


def read_algo_opportunity_report(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    db_path: str = DB_PATH_DEFAULT,
) -> dict:
    """Return raw rows and aggregate metrics for the selected detected-date range."""
    conn = open_db(db_path)
    conn.row_factory = sqlite3.Row
    try:
        ensure_algo_opportunities_schema(conn)
        dedupe_algo_opportunities(conn)
        sync_algo_opportunities_from_bets(conn)
        sync_algo_opportunities_from_results(conn)
        refresh_algo_opportunity_staking(conn)
        starting_bankroll = get_live_tracker_start_br(conn)
        where = []
        params: list[object] = []
        if start_date:
            where.append("detected_date >= ?")
            params.append(start_date)
        if end_date:
            where.append("detected_date <= ?")
            params.append(end_date)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        all_where = []
        all_params: list[object] = []
        if end_date:
            all_where.append("detected_date <= ?")
            all_params.append(end_date)
        all_where_sql = ("WHERE " + " AND ".join(all_where)) if all_where else ""
        all_rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT *
                FROM algo_opportunities
                {all_where_sql}
                ORDER BY detected_date ASC, priority_score DESC, ev DESC
                """,
                all_params,
            ).fetchall()
        ]
    finally:
        conn.close()

    all_rows = _apply_composite_ordered_theoretical_staking(
        all_rows,
        starting_bankroll=float(starting_bankroll or 0.0),
    )
    rows = []
    for r in all_rows:
        d = str(r.get("detected_date") or "")
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        rows.append(r)
    rows = sorted(
        rows,
        key=lambda r: (
            str(r.get("detected_date") or ""),
            float(r.get("priority_score") or 0.0),
            float(r.get("ev") or 0.0),
        ),
        reverse=True,
    )
    resolved = [r for r in rows if r.get("status") in ("Gagné", "Perdu", "Annulé")]
    won = [r for r in resolved if r.get("status") == "Gagné"]
    staked = [r for r in rows if r.get("linked_bet_id") is not None]
    real_resolved = [
        r for r in staked if r.get("real_profit") is not None and r.get("status") in ("Gagné", "Perdu", "Annulé")
    ]
    non_cancelled_resolved = [r for r in resolved if r.get("status") != "Annulé"]
    theo_profit = sum(float(r.get("theoretical_profit_eur") or 0.0) for r in non_cancelled_resolved)
    theo_stake = sum(float(r.get("theoretical_stake_used_eur") or 0.0) for r in non_cancelled_resolved)
    chronological_rows = sorted(rows, key=lambda r: str(r.get("detected_date") or ""))
    first_row = next((r for r in chronological_rows if r.get("theoretical_bankroll_start") is not None), None)
    last_row = next((r for r in reversed(chronological_rows) if r.get("theoretical_bankroll_end") is not None), None)
    start_br = float((first_row or {}).get("theoretical_bankroll_start") or starting_bankroll or 0.0)
    end_br = float((last_row or {}).get("theoretical_bankroll_end") or start_br)
    real_profit = sum(float(r.get("real_profit") or 0.0) for r in real_resolved)
    real_stake = sum(float(r.get("real_stake") or 0.0) for r in real_resolved)
    real_ev_rows = [
        r for r in staked
        if r.get("real_odd") is not None and r.get("p_model") is not None
    ]
    avg_real_ev = (
        sum((float(r.get("p_model") or 0.0) * float(r.get("real_odd") or 0.0) - 1.0) for r in real_ev_rows)
        / len(real_ev_rows)
        if real_ev_rows
        else 0.0
    )
    avg_ev = (
        sum(float(r.get("ev") or 0.0) for r in rows) / len(rows)
        if rows
        else 0.0
    )
    avg_priority = (
        sum(float(r.get("priority_score") or 0.0) for r in rows) / len(rows)
        if rows
        else 0.0
    )
    return {
        "rows": rows,
        "metrics": {
            "n_detected": len(rows),
            "n_resolved": len(resolved),
            "n_won": len(won),
            "n_lost": len([r for r in resolved if r.get("status") == "Perdu"]),
            "n_cancelled": len([r for r in resolved if r.get("status") == "Annulé"]),
            "hit_rate_pct": (len(won) / max(1, len(non_cancelled_resolved)) * 100.0),
            "theoretical_profit_u": theo_profit,
            "theoretical_stake_u": theo_stake,
            "theoretical_roi_pct": (theo_profit / theo_stake * 100.0) if theo_stake > 0 else 0.0,
            "theoretical_start_bankroll": start_br,
            "theoretical_end_bankroll": end_br,
            "theoretical_growth_pct": (
                ((end_br / start_br) - 1.0) * 100.0
                if start_br > 0
                else 0.0
            ),
            "avg_theoretical_stake_pct": (
                sum(float(r.get("theoretical_stake_frac") or 0.0) for r in rows) / len(rows) * 100.0
                if rows
                else 0.0
            ),
            "n_real_bets": len(staked),
            "real_profit": real_profit,
            "real_stake": real_stake,
            "real_roi_pct": (real_profit / real_stake * 100.0) if real_stake > 0 else 0.0,
            "avg_real_ev_pct": avg_real_ev * 100.0,
            "avg_ev_pct": avg_ev * 100.0,
            "avg_priority": avg_priority,
        },
    }


# ---------------------------------------------------------------------------
# Bootstrapping helper
# ---------------------------------------------------------------------------


def init_all(db_path: str = DB_PATH_DEFAULT) -> dict:
    """Run all migrations once. Returns a small summary dict."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = open_db(db_path)
    try:
        added_cols = ensure_user_bets_schema(conn)
        ensure_match_results_cache(conn)
        ensure_algo_opportunities_schema(conn)
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
