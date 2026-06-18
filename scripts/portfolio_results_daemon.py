#!/usr/bin/env python3
"""Daemon : résolution automatique des paris « En cours » (TE + Sackmann).

Boucle par défaut toutes les 10 minutes. Un verrou fichier évite les scrapes
Playwright concurrents (CLI, UI Streamlit, autre daemon).

En parallèle (sans verrou scrape) : capture headless du top 15 probas ATP/WTA
journalier depuis le snapshot live + sync résultats sur `daily_top_proba_picks`.

Usage:
  py -3 -m scripts.portfolio_results_daemon
  py -3 -m scripts.portfolio_results_daemon --once

Variables d'environnement:
  BETTINGHUD_PORTFOLIO_DAEMON_INTERVAL_SEC  (défaut 600)
  BETTINGHUD_PORTFOLIO_SCRAPE_LOCK_MAX_SEC    (défaut 1200)
  BETTINGHUD_DAILY_TOP_PROBA_DAEMON_INTERVAL_SEC  (défaut 600)
  BETTINGHUD_DAILY_TOP_PROBA_JSONL_INTERVAL_SEC   (défaut 3600)
  BETTINGHUD_CLOSING_ODDS_ARCHIVE                 (défaut 1)
  BETTINGHUD_CLOSING_ODDS_NIGHT_HOUR              (défaut 4, Europe/Paris)
  BETTINGHUD_CLOSING_ODDS_TZ                      (défaut Europe/Paris)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import time
from logging.handlers import RotatingFileHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

DEFAULT_INTERVAL_SEC = 600
DEFAULT_LOCK_MAX_SEC = 1200

LOG_DIR = os.path.join(ROOT, "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "portfolio_results_daemon.log")


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("portfolio_results_daemon")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


LOGGER = _setup_logger()


def _count_pending(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM user_bets WHERE status = 'En cours'"
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def _run_daily_top_proba_pass(db_path: str) -> None:
    from scripts.daily_top_proba_store import run_daily_top_proba_daemon_pass

    result = run_daily_top_proba_daemon_pass(
        db_path=db_path,
        capture_source="portfolio_results_daemon",
    )
    if result.get("skipped_capture"):
        LOGGER.info(
            "Top probas journalier : capture ignorée (%s) — %d résultat(s) sync",
            result.get("reason") or "skip",
            int(result.get("n_results") or 0),
        )
    else:
        LOGGER.info(
            "Top probas journalier : %d pick(s), JSONL=%s, snapshot=%.0f",
            int(result.get("n_picks") or 0),
            bool(result.get("append_jsonl")),
            float(result.get("snapshot_built_at") or 0.0),
        )


def _maybe_run_closing_odds_archive() -> None:
    """Une passe nocturne : scrape TE + CSV closing de la veille."""
    try:
        from scripts.closing_odds_archive import (
            run_nightly_closing_archive,
            should_run_nightly_archive_now,
        )

        if not should_run_nightly_archive_now():
            return
        LOGGER.info("Archive cotes closing (passe nocturne TE)…")
        stats = run_nightly_closing_archive(logger=LOGGER)
        if stats.get("skipped"):
            LOGGER.info("Closing odds ignoré : %s", stats.get("reason") or "skip")
        else:
            LOGGER.info(
                "Closing odds : date=%s ingere=%s archive=%s fichier=%s",
                stats.get("archive_date"),
                stats.get("ingested"),
                stats.get("archived"),
                stats.get("path") or "-",
            )
    except Exception as exc:
        LOGGER.warning("Archive closing odds ignorée : %s", exc)


def _maybe_od1p_results() -> None:
    try:
        from scripts.od1p_publish import publish_1d1p_results

        out = publish_1d1p_results()
        tg_n = int((out.get("telegram") or {}).get("n_posted") or 0)
        dc_n = int((out.get("discord") or {}).get("n_posted") or 0)
        if tg_n:
            LOGGER.info("Telegram 1D1P : %d résultat(s) publié(s)", tg_n)
        if dc_n:
            LOGGER.info("Discord 1D1P : %d résultat(s) publié(s)", dc_n)
        board = (out.get("discord") or {}).get("performance_board")
        if board and board.get("ok"):
            LOGGER.info("Discord 1D1P track record : %s", board.get("action", "ok"))
    except Exception as exc:
        LOGGER.info("1D1P résultats ignoré : %s", exc)


def _sync_algo_report(db_path: str) -> None:
    from scripts.bets_db import (
        correct_retirement_voids_user_bets,
        ensure_algo_opportunities_schema,
        ensure_daily_top_proba_schema,
        ensure_user_bets_schema,
        sync_algo_opportunities_from_bets,
        sync_algo_opportunities_from_results,
        sync_daily_top_proba_from_results,
    )

    conn = sqlite3.connect(db_path)
    try:
        ensure_user_bets_schema(conn)
        ensure_algo_opportunities_schema(conn)
        ensure_daily_top_proba_schema(conn)
        n_bets = sync_algo_opportunities_from_bets(conn)
        n_res = sync_algo_opportunities_from_results(conn)
        n_top = sync_daily_top_proba_from_results(conn)
        n_void = correct_retirement_voids_user_bets(conn)
        LOGGER.info(
            "Algo report sync: %d liens paris, %d statuts résolus, %d top-proba résolus, %d voids user_bets",
            n_bets,
            n_res,
            n_top,
            n_void,
        )
    finally:
        conn.close()


def run_pass(*, db_path: str, lock_max_sec: float) -> int:
    """One resolution pass. Returns 0=ok/skipped idle, 2=skipped lock, 1=error."""
    from scripts.portfolio_sync_lock import (
        acquire_scrape_lock,
        release_scrape_lock,
        scrape_in_progress,
        touch_daemon_heartbeat,
    )
    from scripts.scraper_results import ResultsScraper

    touch_daemon_heartbeat()

    _maybe_run_closing_odds_archive()

    try:
        _run_daily_top_proba_pass(db_path)
    except Exception as exc:
        LOGGER.warning("Top probas journalier ignoré : %s", exc)

    pending = _count_pending(db_path)
    if pending == 0:
        LOGGER.info("Aucun pari « En cours » — scrape ignoré")
        try:
            _sync_algo_report(db_path)
        except Exception as exc:
            LOGGER.warning("Sync report algo ignorée : %s", exc)
        _maybe_od1p_results()
        return 0

    if scrape_in_progress(max_lock_sec=lock_max_sec):
        LOGGER.info("Scrape résultats déjà en cours — passe ignorée")
        return 2

    if not acquire_scrape_lock():
        LOGGER.info("Verrou scrape indisponible — passe ignorée")
        return 2

    try:
        LOGGER.info("Début passe (%d pari(s) en cours)…", pending)
        t0 = time.time()
        scraper = ResultsScraper(db_path=db_path)
        n = asyncio.run(scraper.update_pending_bets())
        elapsed = time.time() - t0
        LOGGER.info("Passe terminée : %d pari(s) liquidé(s) en %.0f s", n, elapsed)
        try:
            _sync_algo_report(db_path)
        except Exception as exc:
            LOGGER.warning("Sync report algo ignorée : %s", exc)
        _maybe_od1p_results()
        return 0
    except Exception as exc:
        LOGGER.exception("Passe en échec : %s", exc)
        return 1
    finally:
        release_scrape_lock()


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Daemon sync portefeuille (résultats TE)")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Une seule passe puis quitter (utile pour tests ou tâche planifiée)",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("BETTINGHUD_DB_PATH", "data/bettinghud.db"),
        help="Chemin SQLite (défaut data/bettinghud.db)",
    )
    args = parser.parse_args()

    interval_sec = max(
        60,
        int(os.getenv("BETTINGHUD_PORTFOLIO_DAEMON_INTERVAL_SEC", str(DEFAULT_INTERVAL_SEC))),
    )
    lock_max_sec = max(
        interval_sec,
        int(os.getenv("BETTINGHUD_PORTFOLIO_SCRAPE_LOCK_MAX_SEC", str(DEFAULT_LOCK_MAX_SEC))),
    )
    db_path = os.path.join(ROOT, args.db) if not os.path.isabs(args.db) else args.db

    LOGGER.info(
        "Portfolio results daemon — intervalle %d s, lock max %d s, db=%s",
        interval_sec,
        lock_max_sec,
        db_path,
    )

    if args.once:
        return run_pass(db_path=db_path, lock_max_sec=lock_max_sec)

    while True:
        code = run_pass(db_path=db_path, lock_max_sec=lock_max_sec)
        if code == 1:
            LOGGER.warning("Dernière passe en erreur — nouvelle tentative dans %d s", interval_sec)
        time.sleep(interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())
