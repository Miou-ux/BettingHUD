"""Weekly reconciliation pass — cross-check settled bets against all three
result sources (Tennis Explorer, Sackmann GitHub, tennis-data.co.uk Excel).

Runs on the last `--window-days` days (default 7). For each settled bet:
- Look up the resolved match in TE cache, Sackmann cache, and tennis-data.
- If at least 2 sources agree on a winner that disagrees with the recorded
  status, the bet is **re-settled** with `result_source='reconciliation'`
  and the divergence is logged.
- Pure cache hits (no fresh scraping) for TE; tennis-data and Sackmann CSVs
  re-downloaded automatically when stale.

Schedule:
- CLI: ``python -m scripts.reconcile_bets`` — safe to run anytime, idempotent.
- Auto-trigger: from the dashboard if more than `RECONCILE_INTERVAL_DAYS`
  have elapsed since the last successful run.

Logs:
- Free-form messages -> ``data/logs/scraper_results.log`` (shared logger).
- Structured rows  -> ``reconciliation_log`` table for in-app display.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, Optional

from scripts.bets_db import (
    DB_PATH_DEFAULT,
    ensure_bets_meta,
    ensure_match_results_cache,
    ensure_reconciliation_log,
    ensure_user_bets_schema,
    get_meta,
    read_cached_results,
    set_meta,
    settle_bet,
    write_cached_results,
)
from scripts.sackmann_results import load_recent_results as load_sackmann
import scripts.scraper_results as _scraper_results
from scripts.scraper_results import (
    ResultsScraper,
    _classify_score,
    canonical_player,
    names_match,
)
from scripts.tennis_data_results import load_recent_results as load_tennis_data

# Do not `from scripts.scraper_results import LOGGER` — some environments hit a
# partially initialised module or older snapshots without LOGGER. Resolve lazily.
def _resolve_scraper_logger() -> logging.Logger:
    lg = getattr(_scraper_results, "LOGGER", None)
    if lg is not None:
        return lg
    setup = getattr(_scraper_results, "_setup_logger", None)
    if callable(setup):
        return setup()
    return logging.getLogger("scraper_results")


LOGGER = _resolve_scraper_logger()

RECONCILE_INTERVAL_DAYS = 7  # auto-trigger threshold from dashboard
LAST_RECON_KEY = "last_reconciliation_ts"


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------


def _store_external_in_cache(
    conn: sqlite3.Connection, rows: list[dict], *, source: str
) -> int:
    """Cache external rows (Sackmann / tennis-data). Each row already has
    winner_name/loser_name as raw strings; we canonicalize here."""
    out = []
    for r in rows:
        wn = canonical_player(r["winner_name"])
        ln = canonical_player(r["loser_name"])
        score = r.get("score") or ""
        retired, walkover = _classify_score(score)
        for a, b in ((wn, ln), (ln, wn)):
            out.append(
                {
                    "match_date": r["match_date"],
                    "p1_canonical": a,
                    "p2_canonical": b,
                    "winner_canonical": wn,
                    "score": score,
                    "retired": retired,
                    "walkover": walkover,
                    "tour": r.get("tour"),
                    "source": source,
                }
            )
    return write_cached_results(conn, out)


def _lookup_one_source(
    cache_for_dates: dict[str, dict[str, dict]],
    *,
    bet_p1: str,
    bet_p2: str,
    bet_date: str,
    nearby_dates: list[str],
    source: str,
) -> Optional[dict]:
    """Look up the result for a bet within a particular source's cache slice.

    cache_for_dates: cache as returned by read_cached_results, but pre-filtered
    so each bucket only contains entries from `source`.
    """
    p1c = canonical_player(bet_p1)
    p2c = canonical_player(bet_p2)
    keys = [f"{p1c}||{p2c}", f"{p2c}||{p1c}"]
    candidate_dates = [bet_date] + [d for d in nearby_dates if d != bet_date]
    for d in candidate_dates:
        bucket = cache_for_dates.get(d) or {}
        for k in keys:
            if k in bucket:
                return bucket[k]
    # fuzzy fallback
    for d in candidate_dates:
        bucket = cache_for_dates.get(d) or {}
        for k, v in bucket.items():
            try:
                kp1, kp2 = k.split("||", 1)
            except ValueError:
                continue
            if (
                names_match(p1c, kp1) and names_match(p2c, kp2)
            ) or (names_match(p1c, kp2) and names_match(p2c, kp1)):
                return v
    return None


def _split_cache_by_source(
    cache: dict[str, dict[str, dict]],
) -> dict[str, dict[str, dict[str, dict]]]:
    """Split the unified cache into one bucket per source."""
    out: dict[str, dict[str, dict[str, dict]]] = {}
    for d, bucket in cache.items():
        for k, v in bucket.items():
            src = v.get("source") or "unknown"
            out.setdefault(src, {}).setdefault(d, {})[k] = v
    return out


# ---------------------------------------------------------------------------
# Main reconciliation
# ---------------------------------------------------------------------------


def _outcome_from_hit(hit: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (winner_canonical, marker) from a cache hit. marker is one of
    'walkover' / 'retired' / None.
    """
    if hit.get("walkover"):
        return None, "walkover"
    return hit.get("winner_canonical"), ("retired" if hit.get("retired") else None)


async def reconcile(
    db_path: str = DB_PATH_DEFAULT,
    window_days: int = RECONCILE_INTERVAL_DAYS,
    *,
    refresh_te: bool = True,
) -> dict:
    """Run the reconciliation pass.

    Args:
        db_path: SQLite database path.
        window_days: How many days back to reconcile.
        refresh_te: If True, run the standard TE scraper first to ensure the
            TE cache is up to date for the window.

    Returns a summary dict with counts.
    """
    # Step 1 — make sure TE has the latest data for the window
    if refresh_te:
        try:
            await ResultsScraper(db_path=db_path, window_days=window_days)._run()
        except Exception as exc:
            LOGGER.warning("TE refresh during reconciliation failed: %s", exc)

    conn = sqlite3.connect(db_path)
    try:
        ensure_user_bets_schema(conn)
        ensure_match_results_cache(conn)
        ensure_bets_meta(conn)
        ensure_reconciliation_log(conn)

        today = datetime.now().date()
        cutoff = today - timedelta(days=int(window_days))
        target_dates = [
            (cutoff + timedelta(days=i)).isoformat()
            for i in range(window_days + 1)
        ]

        # Step 2 — refresh Sackmann + tennis-data CSV caches
        try:
            sk_rows = load_sackmann(target_dates, lookback_days=window_days + 7)
            n_sk = _store_external_in_cache(conn, sk_rows, source="sackmann")
            LOGGER.info("Reconciliation: Sackmann cached %d rows", n_sk)
        except Exception as exc:
            LOGGER.warning("Reconciliation: Sackmann fetch failed: %s", exc)

        try:
            td_rows = load_tennis_data(target_dates, lookback_days=window_days + 7)
            n_td = _store_external_in_cache(conn, td_rows, source="tennis-data")
            LOGGER.info("Reconciliation: tennis-data cached %d rows", n_td)
        except Exception as exc:
            LOGGER.warning("Reconciliation: tennis-data fetch failed: %s", exc)

        # Step 3 — read all sources for the window
        cache = read_cached_results(conn, target_dates)
        per_source = _split_cache_by_source(cache)

        # Step 4 — pick all bets in window, even already-settled ones
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, date, match_name, bet_on, odds, stake, status,
                   winner_resolved, profit, result_source
            FROM user_bets
            WHERE date >= ? AND date <= ?
            ORDER BY date ASC, id ASC
            """,
            (cutoff.isoformat(), today.isoformat()),
        )
        bets = cur.fetchall()

        run_ts = datetime.utcnow().isoformat(timespec="seconds")
        n_checked = n_resettled = n_logged = n_pending_resolved = n_enriched = 0

        for (
            bet_id,
            date,
            match_name,
            bet_on,
            odds,
            stake,
            status,
            winner_resolved,
            profit,
            result_source,
        ) in bets:
            n_checked += 1
            parts = (match_name or "").split(" vs ")
            if len(parts) != 2:
                continue
            p1_raw, p2_raw = parts[0], parts[1]
            try:
                bd = datetime.strptime(date, "%Y-%m-%d").date()
            except Exception:
                continue
            nearby = [
                (bd + timedelta(days=k)).isoformat()
                for k in (-1, 1)
                if cutoff <= bd + timedelta(days=k) <= today
            ]

            # Look up each source independently
            verdicts: dict[str, dict] = {}
            for src_name in ("tennisexplorer", "sackmann", "tennis-data"):
                src_cache = per_source.get(src_name, {})
                hit = _lookup_one_source(
                    src_cache,
                    bet_p1=p1_raw,
                    bet_p2=p2_raw,
                    bet_date=date,
                    nearby_dates=nearby,
                    source=src_name,
                )
                if hit:
                    w, marker = _outcome_from_hit(hit)
                    verdicts[src_name] = {
                        "winner": w,
                        "marker": marker,
                        "score": hit.get("score"),
                    }

            if not verdicts:
                continue  # nothing new to compare

            # Aggregate verdict — majority vote (≥ 2 of 3) wins, ties handled
            # by trust order: tennis-data > sackmann > tennisexplorer.
            walk_count = sum(1 for v in verdicts.values() if v["marker"] == "walkover")
            winner_votes: dict[str, int] = {}
            for v in verdicts.values():
                if v["winner"]:
                    winner_votes[v["winner"]] = winner_votes.get(v["winner"], 0) + 1

            consensus_winner: Optional[str] = None
            consensus_marker: Optional[str] = None
            if walk_count >= 2 or (walk_count == 1 and len(verdicts) == 1):
                consensus_marker = "walkover"
            elif winner_votes:
                # majority vote
                top = max(winner_votes.values())
                tied = [w for w, n in winner_votes.items() if n == top]
                if len(tied) == 1:
                    consensus_winner = tied[0]
                else:
                    # tie → trust order
                    for src in ("tennis-data", "sackmann", "tennisexplorer"):
                        v = verdicts.get(src)
                        if v and v["winner"] in tied:
                            consensus_winner = v["winner"]
                            break

            if consensus_winner is None and consensus_marker is None:
                continue

            sources_agreement = ",".join(
                f"{s}:{verdicts[s]['winner'] or verdicts[s]['marker'] or '?'}"
                for s in verdicts
            )

            # Decide what the bet should look like NOW
            if consensus_marker == "walkover":
                desired_status = "Annulé"
                desired_winner = None
                desired_profit = 0.0
            else:
                bet_canon = canonical_player(bet_on)
                won = names_match(bet_canon, consensus_winner) if consensus_winner else False
                desired_status = "Gagné" if won else "Perdu"
                desired_winner = consensus_winner
                desired_profit = (
                    float(odds - 1.0) * float(stake) if won else -float(stake)
                )

            # Compare with current state
            score_text = next(
                (v["score"] for v in verdicts.values() if v.get("score")), None
            )
            if status == "En cours":
                # Newly resolved by reconciliation
                settle_bet(
                    conn,
                    bet_id=bet_id,
                    status=desired_status,
                    profit=desired_profit,
                    winner_resolved=desired_winner,
                    score_final=score_text,
                    result_source="reconciliation:" + sources_agreement,
                )
                conn.execute(
                    """
                    INSERT INTO reconciliation_log
                    (run_ts, bet_id, match_name, old_status, new_status,
                     old_winner, new_winner, sources_agreement, action, notes)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_ts,
                        bet_id,
                        match_name,
                        status,
                        desired_status,
                        winner_resolved,
                        desired_winner,
                        sources_agreement,
                        "resolve_pending",
                        score_text,
                    ),
                )
                n_pending_resolved += 1
                LOGGER.info(
                    "Reconciliation: bet #%s pending → %s (sources: %s)",
                    bet_id,
                    desired_status,
                    sources_agreement,
                )
            elif desired_status != status:
                # Existing bet disagrees with consensus → real flip
                settle_bet(
                    conn,
                    bet_id=bet_id,
                    status=desired_status,
                    profit=desired_profit,
                    winner_resolved=desired_winner,
                    score_final=score_text,
                    result_source="reconciliation:" + sources_agreement,
                )
                conn.execute(
                    """
                    INSERT INTO reconciliation_log
                    (run_ts, bet_id, match_name, old_status, new_status,
                     old_winner, new_winner, sources_agreement, action, notes)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_ts,
                        bet_id,
                        match_name,
                        status,
                        desired_status,
                        winner_resolved,
                        desired_winner,
                        sources_agreement,
                        "flip_status",
                        score_text,
                    ),
                )
                n_resettled += 1
                LOGGER.warning(
                    "Reconciliation FLIP: bet #%s %s -> %s (was winner=%s, now %s; sources: %s)",
                    bet_id,
                    status,
                    desired_status,
                    winner_resolved,
                    desired_winner,
                    sources_agreement,
                )
            elif desired_winner and not (winner_resolved or "").strip():
                # Status already correct but winner_resolved was empty (legacy
                # bets pre-migration) → silent enrichment, no financial change.
                conn.execute(
                    """
                    UPDATE user_bets
                    SET winner_resolved = ?, score_final = COALESCE(score_final, ?),
                        result_source = COALESCE(result_source, ?)
                    WHERE id = ?
                    """,
                    (
                        desired_winner,
                        score_text,
                        "reconciliation:" + sources_agreement,
                        bet_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO reconciliation_log
                    (run_ts, bet_id, match_name, old_status, new_status,
                     old_winner, new_winner, sources_agreement, action, notes)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_ts,
                        bet_id,
                        match_name,
                        status,
                        status,
                        winner_resolved,
                        desired_winner,
                        sources_agreement,
                        "enrich_winner",
                        score_text,
                    ),
                )
                n_enriched += 1
                LOGGER.info(
                    "Reconciliation ENRICH: bet #%s winner_resolved %s -> %s",
                    bet_id,
                    winner_resolved,
                    desired_winner,
                )
            elif desired_winner and desired_winner != winner_resolved:
                # Genuine winner-string disagreement on a settled bet — log only
                conn.execute(
                    """
                    INSERT INTO reconciliation_log
                    (run_ts, bet_id, match_name, old_status, new_status,
                     old_winner, new_winner, sources_agreement, action, notes)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_ts,
                        bet_id,
                        match_name,
                        status,
                        status,
                        winner_resolved,
                        desired_winner,
                        sources_agreement,
                        "confirm_winner",
                        score_text,
                    ),
                )
                n_logged += 1

        conn.commit()
        set_meta(conn, LAST_RECON_KEY, run_ts)
        summary = {
            "checked": n_checked,
            "pending_resolved": n_pending_resolved,
            "flipped": n_resettled,
            "enriched": n_enriched,
            "confirmed_winner_diff": n_logged,
            "run_ts": run_ts,
        }
        LOGGER.info("Reconciliation summary: %s", summary)
        return summary
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers used by the dashboard
# ---------------------------------------------------------------------------


def days_since_last_reconciliation(db_path: str = DB_PATH_DEFAULT) -> Optional[float]:
    """Return the number of days since the last successful reconciliation, or
    None if it has never run."""
    conn = sqlite3.connect(db_path)
    try:
        ensure_bets_meta(conn)
        ts = get_meta(conn, LAST_RECON_KEY)
    finally:
        conn.close()
    if not ts:
        return None
    try:
        last = datetime.fromisoformat(ts)
    except Exception:
        return None
    delta = datetime.utcnow() - last
    return delta.total_seconds() / 86400.0


def is_reconciliation_due(
    db_path: str = DB_PATH_DEFAULT, threshold_days: float = RECONCILE_INTERVAL_DAYS
) -> bool:
    d = days_since_last_reconciliation(db_path)
    return d is None or d >= threshold_days


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=RECONCILE_INTERVAL_DAYS)
    parser.add_argument("--no-te-refresh", action="store_true")
    args = parser.parse_args()

    try:
        summary = asyncio.run(
            reconcile(window_days=args.window_days, refresh_te=not args.no_te_refresh)
        )
        LOGGER.info("CLI reconciliation finished: %s", summary)
        return 0
    except Exception as exc:
        LOGGER.exception("CLI reconciliation crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
