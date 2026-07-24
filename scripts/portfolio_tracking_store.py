"""Suivi portfolio Top5 / 1D1P — ledger journalier reconstructible (BR théorique 100 €)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from scripts.backtest_pack12_global_2026 import MAX_STAKE_PCT
from scripts.backtest_staking_sim import kelly_full_fraction
from scripts.kelly_policy import KELLY_BASE_FRAC
from scripts.published_picks_store import MODE_1D1P, MODE_TOP5, NO_PICK_KEY

DEFAULT_BANKROLL_EUR = 100.0
MODES = (MODE_TOP5, MODE_1D1P)


def ensure_portfolio_tracking_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_tracking_config (
            mode TEXT PRIMARY KEY,
            start_date TEXT NOT NULL,
            bankroll_start_eur REAL NOT NULL DEFAULT 100.0,
            updated_ts TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS portfolio_daily_bets (
            calendar_date TEXT NOT NULL,
            mode TEXT NOT NULL,
            bet_rank INTEGER NOT NULL,
            pick_key TEXT,
            fav_player TEXT,
            match_name TEXT,
            player1 TEXT,
            player2 TEXT,
            tour TEXT,
            tournament TEXT,
            match_date TEXT,
            surface TEXT,
            p_model_fav REAL,
            ev_fav_pct REAL,
            odd_fav REAL,
            data_reliability_score INTEGER,
            segment_brier REAL,
            status TEXT DEFAULT 'En cours',
            score_final TEXT,
            winner_resolved TEXT,
            stake_frac REAL,
            stake_eur REAL,
            profit_eur REAL,
            bankroll_before_eur REAL,
            bankroll_after_eur REAL,
            published_ts TEXT,
            publish_source TEXT,
            settled_ts TEXT,
            payload_json TEXT,
            PRIMARY KEY (calendar_date, mode, bet_rank)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_portfolio_daily_bets_mode_date "
        "ON portfolio_daily_bets(mode, calendar_date)"
    )
    conn.commit()


def get_tracking_config(conn: sqlite3.Connection, mode: str) -> dict[str, Any] | None:
    ensure_portfolio_tracking_schema(conn)
    row = conn.execute(
        "SELECT mode, start_date, bankroll_start_eur, updated_ts "
        "FROM portfolio_tracking_config WHERE mode = ?",
        (mode,),
    ).fetchone()
    if not row:
        return None
    return {
        "mode": str(row[0]),
        "start_date": str(row[1])[:10],
        "bankroll_start_eur": float(row[2] or DEFAULT_BANKROLL_EUR),
        "updated_ts": row[3],
    }


def has_portfolio_tracking(conn: sqlite3.Connection, mode: str) -> bool:
    return get_tracking_config(conn, mode) is not None


def init_portfolio_tracking(
    conn: sqlite3.Connection,
    *,
    mode: str,
    start_date: str,
    bankroll_start_eur: float = DEFAULT_BANKROLL_EUR,
    clear_ledger: bool = True,
) -> None:
    """(Re)démarre le suivi pour un mode à partir de ``start_date``."""
    ensure_portfolio_tracking_schema(conn)
    cal = str(start_date)[:10]
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO portfolio_tracking_config (mode, start_date, bankroll_start_eur, updated_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(mode) DO UPDATE SET
            start_date = excluded.start_date,
            bankroll_start_eur = excluded.bankroll_start_eur,
            updated_ts = excluded.updated_ts
        """,
        (mode, cal, float(bankroll_start_eur), now),
    )
    if clear_ledger:
        conn.execute(
            "DELETE FROM portfolio_daily_bets WHERE mode = ? AND calendar_date >= ?",
            (mode, cal),
        )
    conn.commit()


def _stake_for_pick(
    *,
    bankroll_liquid: float,
    bankroll_day_start: float,
    day_deployed: float,
    p_model: float,
    odd: float,
    segment_brier: float,
    global_brier: float,
    kelly_frac: float,
) -> tuple[float, float]:
    """Retourne (stake_frac_applied, stake_eur)."""
    cap_frac = float(MAX_STAKE_PCT) / 100.0
    kf_raw = kelly_full_fraction(float(p_model), float(odd))
    brier_seg = float(segment_brier if segment_brier is not None else global_brier)
    kelly_adj = max(0.0, 1.0 - (brier_seg / 0.25))
    stake_frac = max(0.0, (float(kelly_frac) * kf_raw) * kelly_adj)
    raw = bankroll_liquid * stake_frac
    cap_amt = bankroll_liquid * cap_frac
    if raw > cap_amt + 1e-9:
        raw = cap_amt
    deploy_left = bankroll_day_start - day_deployed
    stake = min(raw, bankroll_liquid, deploy_left)
    applied_frac = (stake / bankroll_day_start) if bankroll_day_start > 0 else 0.0
    return applied_frac, max(0.0, stake)


def _profit_for_status(status: str, odd: float, stake: float) -> float:
    st = str(status or "").strip().lower()
    if "annul" in st:
        return 0.0
    if "gagn" in st:
        return stake * (float(odd) - 1.0)
    if "perdu" in st:
        return -stake
    return 0.0


def _is_settled_status(status: str) -> bool:
    st = str(status or "").strip().lower()
    return "gagn" in st or "perdu" in st or "annul" in st


def sync_portfolio_from_published(
    conn: sqlite3.Connection,
    *,
    mode: str,
    calendar_date: str,
) -> int:
    """Copie la publication TG du jour dans le ledger portfolio."""
    from scripts.bets_db import ensure_daily_top_proba_schema
    from scripts.published_picks_store import ensure_published_picks_schema

    cfg = get_tracking_config(conn, mode)
    if not cfg:
        return 0
    cal = str(calendar_date)[:10]
    if cal < cfg["start_date"]:
        return 0

    ensure_published_picks_schema(conn)
    ensure_daily_top_proba_schema(conn)
    ensure_portfolio_tracking_schema(conn)

    pub_rows = conn.execute(
        """
        SELECT * FROM daily_published_picks
        WHERE calendar_date = ? AND mode = ?
        ORDER BY publish_rank ASC
        """,
        (cal, mode),
    ).fetchall()
    if not pub_rows:
        return 0

    conn.execute(
        "DELETE FROM portfolio_daily_bets WHERE calendar_date = ? AND mode = ?",
        (cal, mode),
    )
    n = 0
    for pub in pub_rows:
        d = dict(pub)
        pk = str(d.get("pick_key") or "")
        rank = int(d.get("publish_rank") or 0)
        if rank <= 0 or pk == NO_PICK_KEY:
            continue
        merged: dict[str, Any] = dict(d)
        full = conn.execute(
            "SELECT * FROM daily_top_proba_picks WHERE pick_key = ?",
            (pk,),
        ).fetchone()
        if full:
            merged.update({k: full[k] for k in full.keys() if full[k] is not None})
        conn.execute(
            """
            INSERT INTO portfolio_daily_bets (
                calendar_date, mode, bet_rank, pick_key, fav_player, match_name,
                player1, player2, tour, tournament, match_date, surface,
                p_model_fav, ev_fav_pct, odd_fav, data_reliability_score, segment_brier,
                status, score_final, winner_resolved,
                published_ts, publish_source, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cal,
                mode,
                rank,
                pk,
                merged.get("fav_player"),
                merged.get("match_name"),
                merged.get("player1"),
                merged.get("player2"),
                merged.get("tour"),
                merged.get("tournament"),
                merged.get("match_date") or cal,
                merged.get("surface"),
                merged.get("p_model_fav"),
                merged.get("ev_fav_pct"),
                merged.get("odd_fav"),
                merged.get("data_reliability_score"),
                merged.get("segment_brier"),
                merged.get("status") or "En cours",
                merged.get("score_final"),
                merged.get("winner_resolved"),
                d.get("published_ts"),
                d.get("publish_source"),
                d.get("payload_json"),
            ),
        )
        n += 1
    conn.commit()
    return n


def sync_portfolio_settlement(conn: sqlite3.Connection, *, mode: str | None = None) -> int:
    """Met à jour statut/score ledger depuis daily_top_proba_picks."""
    ensure_portfolio_tracking_schema(conn)
    modes = (mode,) if mode else MODES
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    n = 0
    for m in modes:
        cfg = get_tracking_config(conn, m)
        if not cfg:
            continue
        rows = conn.execute(
            """
            SELECT calendar_date, bet_rank, pick_key, status
            FROM portfolio_daily_bets
            WHERE mode = ? AND calendar_date >= ?
            """,
            (m, cfg["start_date"]),
        ).fetchall()
        for cal, rank, pk, old_status in rows:
            if not pk:
                continue
            full = conn.execute(
                "SELECT status, score_final, winner_resolved, segment_brier FROM daily_top_proba_picks WHERE pick_key = ?",
                (pk,),
            ).fetchone()
            if not full:
                continue
            status, score, winner, seg_brier = full
            if not status or status == old_status:
                if seg_brier and not _is_settled_status(str(old_status or "")):
                    conn.execute(
                        "UPDATE portfolio_daily_bets SET segment_brier = ? WHERE calendar_date = ? AND mode = ? AND bet_rank = ?",
                        (seg_brier, cal, m, rank),
                    )
                continue
            settled_ts = now if _is_settled_status(str(status)) else None
            conn.execute(
                """
                UPDATE portfolio_daily_bets
                SET status = ?, score_final = ?, winner_resolved = ?,
                    segment_brier = COALESCE(?, segment_brier),
                    settled_ts = COALESCE(?, settled_ts)
                WHERE calendar_date = ? AND mode = ? AND bet_rank = ?
                """,
                (status, score, winner, seg_brier, settled_ts, cal, m, rank),
            )
            n += 1
    conn.commit()
    return n


def recompute_portfolio_ledger(
    conn: sqlite3.Connection,
    mode: str,
    *,
    ml: Any | None = None,
    kelly_frac: float | None = None,
) -> int:
    """Recalcule stakes / PnL / bankroll séquentielle sur le ledger."""
    cfg = get_tracking_config(conn, mode)
    if not cfg:
        return 0
    ensure_portfolio_tracking_schema(conn)

    if ml is None:
        from scripts.live_replay_engine import load_ml

        ml = load_ml()
    seg_map = getattr(ml, "segment_brier_scores", {}) or {}
    glob_b = float(getattr(ml, "global_test_brier", 0.1741))
    kf = float(KELLY_BASE_FRAC if kelly_frac is None else kelly_frac)

    rows = conn.execute(
        """
        SELECT * FROM portfolio_daily_bets
        WHERE mode = ? AND calendar_date >= ?
        ORDER BY calendar_date ASC, bet_rank ASC
        """,
        (mode, cfg["start_date"]),
    ).fetchall()
    if not rows:
        return 0

    br = float(cfg["bankroll_start_eur"])
    by_day_liquid: dict[str, float] = {}
    by_day_b0: dict[str, float] = {}
    by_day_deploy: dict[str, float] = {}
    n = 0

    for row in rows:
        d = dict(row)
        cal = str(d["calendar_date"])[:10]
        status = str(d.get("status") or "En cours")
        odd = float(d.get("odd_fav") or 0)
        p = float(d.get("p_model_fav") or 0)
        seg_key = d.get("segment_brier")
        if seg_key is None:
            try:
                from scripts.ml_model import resolve_match_brier_segment_key

                seg_key = seg_map.get(
                    str(resolve_match_brier_segment_key(d)),
                    glob_b,
                )
            except Exception:
                seg_key = glob_b
        seg_b = float(seg_key or glob_b)

        stake_frac = 0.0
        stake_eur = 0.0
        profit_eur = 0.0
        bankroll_before = br

        if _is_settled_status(status) and "annul" not in status.lower():
            if cal not in by_day_b0:
                by_day_b0[cal] = br
                by_day_liquid[cal] = br
                by_day_deploy[cal] = 0.0
            stake_frac, stake_eur = _stake_for_pick(
                bankroll_liquid=by_day_liquid[cal],
                bankroll_day_start=by_day_b0[cal],
                day_deployed=by_day_deploy[cal],
                p_model=p,
                odd=odd,
                segment_brier=seg_b,
                global_brier=glob_b,
                kelly_frac=kf,
            )
            profit_eur = _profit_for_status(status, odd, stake_eur)
            if stake_eur > 0:
                by_day_liquid[cal] -= stake_eur
                by_day_deploy[cal] += stake_eur
            br += profit_eur
        elif _is_settled_status(status):
            profit_eur = 0.0

        bankroll_after = br
        conn.execute(
            """
            UPDATE portfolio_daily_bets
            SET stake_frac = ?, stake_eur = ?, profit_eur = ?,
                bankroll_before_eur = ?, bankroll_after_eur = ?,
                segment_brier = ?
            WHERE calendar_date = ? AND mode = ? AND bet_rank = ?
            """,
            (
                round(stake_frac, 6),
                round(stake_eur, 2),
                round(profit_eur, 2) if _is_settled_status(status) else None,
                round(bankroll_before, 2),
                round(bankroll_after, 2),
                seg_b,
                cal,
                mode,
                int(d["bet_rank"]),
            ),
        )
        n += 1
    conn.commit()
    return n


def backfill_portfolio_from_published(
    conn: sqlite3.Connection,
    mode: str,
    *,
    from_date: str | None = None,
) -> int:
    """Importe les publications existantes depuis ``start_date``."""
    cfg = get_tracking_config(conn, mode)
    if not cfg:
        return 0
    start = from_date or cfg["start_date"]
    rows = conn.execute(
        """
        SELECT DISTINCT calendar_date FROM daily_published_picks
        WHERE mode = ? AND calendar_date >= ?
        ORDER BY calendar_date
        """,
        (mode, start),
    ).fetchall()
    n = 0
    for (cal,) in rows:
        n += sync_portfolio_from_published(conn, mode=mode, calendar_date=str(cal))
    if n:
        sync_portfolio_settlement(conn, mode=mode)
        recompute_portfolio_ledger(conn, mode)
    return n


def portfolio_bet_to_pick_row(row: dict[str, Any]) -> dict[str, Any]:
    """Convertit une ligne ledger en pick replay CourtAlpha."""
    out = dict(row)
    out["rank"] = int(row.get("bet_rank") or row.get("rank") or 1)
    out["calendar_date"] = str(row.get("calendar_date") or "")[:10]
    out["pool_source"] = "portfolio_ledger"
    out["theoretical_stake_frac"] = row.get("stake_frac")
    out["replay_net_profit_eur"] = row.get("profit_eur")
    st = str(row.get("status") or "En cours")
    out["void"] = "annul" in st.lower()
    out["settled"] = _is_settled_status(st)
    out["won"] = "gagn" in st.lower()
    out["lost"] = "perdu" in st.lower()
    return out


def load_portfolio_replay_picks(
    conn: sqlite3.Connection,
    mode: str,
    *,
    exclude_date: str | None = None,
) -> list[dict[str, Any]]:
    cfg = get_tracking_config(conn, mode)
    if not cfg:
        return []
    ensure_portfolio_tracking_schema(conn)
    q = """
        SELECT * FROM portfolio_daily_bets
        WHERE mode = ? AND calendar_date >= ?
    """
    params: list[Any] = [mode, cfg["start_date"]]
    if exclude_date:
        q += " AND calendar_date < ?"
        params.append(str(exclude_date)[:10])
    q += " ORDER BY calendar_date ASC, bet_rank ASC"
    rows = conn.execute(q, params).fetchall()
    return [portfolio_bet_to_pick_row(dict(r)) for r in rows]


def build_portfolio_curve_and_summary(
    conn: sqlite3.Connection,
    mode: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Courbe + summary depuis le ledger (sans re-simulation)."""
    cfg = get_tracking_config(conn, mode)
    if not cfg:
        return [], {}
    picks = load_portfolio_replay_picks(conn, mode)
    bankroll_start = float(cfg["bankroll_start_eur"])
    br = bankroll_start
    peak = br
    max_dd = 0.0
    total_staked = 0.0
    n_won = n_lost = n_void = n_open = n_settled = 0
    curve: list[dict[str, Any]] = []
    by_day: dict[str, list[dict[str, Any]]] = {}
    for p in picks:
        by_day.setdefault(str(p["calendar_date"]), []).append(p)

    n_cum = 0
    for cal in sorted(by_day.keys()):
        day_rows = by_day[cal]
        day_profit = 0.0
        day_stake = 0.0
        day_settled = True
        for row in day_rows:
            n_cum += 1
            st = str(row.get("status") or "").lower()
            if "gagn" in st:
                n_won += 1
                n_settled += 1
            elif "perdu" in st:
                n_lost += 1
                n_settled += 1
            elif "annul" in st:
                n_void += 1
                n_settled += 1
            else:
                n_open += 1
                day_settled = False
            rep = row.get("profit_eur")
            stk = row.get("stake_eur")
            if rep is not None:
                day_profit += float(rep)
            if stk is not None and "annul" not in st:
                day_stake += float(stk)
                total_staked += float(stk)
        last = day_rows[-1]
        if last.get("bankroll_after_eur") is not None and day_settled:
            br = float(last["bankroll_after_eur"])
        elif day_settled:
            br += day_profit
        peak = max(peak, br)
        dd = ((peak - br) / peak * 100.0) if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        curve.append(
            {
                "date": cal,
                "bankroll": round(br, 2),
                "daily_profit_eur": round(day_profit, 2),
                "daily_stake_eur": round(day_stake, 2),
                "n_picks_cum": n_cum,
                "pnl_cum_eur": round(br - bankroll_start, 2),
                "drawdown_pct": round(dd, 2),
                "settled": day_settled,
            }
        )

    net = br - bankroll_start
    n_decided = n_won + n_lost
    summary = {
        "n_picks": len(picks),
        "n_settled": n_settled,
        "n_open": n_open,
        "n_won": n_won,
        "n_lost": n_lost,
        "n_void": n_void,
        "hit_pct": round((n_won / n_decided * 100.0) if n_decided > 0 else 0.0, 1),
        "bankroll_start_eur": round(bankroll_start, 2),
        "bankroll_final_eur": round(br, 2),
        "net_profit_eur": round(net, 2),
        "growth_pct": round((net / bankroll_start * 100.0) if bankroll_start > 0 else 0.0, 1),
        "total_staked_eur": round(total_staked, 2),
        "roi_on_staked_pct": round((net / total_staked * 100.0) if total_staked > 0 else 0.0, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "kelly_base_frac": KELLY_BASE_FRAC,
        "replay_mode": "portfolio_ledger",
        "tracking_start_date": cfg["start_date"],
    }
    return curve, summary


def _pick_reconcile_key(row: dict[str, Any]) -> tuple[str, int, str]:
    cal = str(row.get("calendar_date") or "")[:10]
    rank = int(row.get("bet_rank") or row.get("rank") or 0)
    name = str(row.get("match_name") or row.get("fav_player") or "").strip().lower()
    return cal, rank, name


def reconcile_portfolio_ledger(
    conn: sqlite3.Connection,
    mode: str,
    *,
    ml: Any | None = None,
    tol_eur: float = 0.02,
) -> dict[str, Any]:
    """Compare ledger ``portfolio_daily_bets`` vs ``kelly_replay_metrics`` frais."""
    cfg = get_tracking_config(conn, mode)
    if not cfg:
        return {"mode": mode, "ok": True, "skipped": True, "reason": "no_tracking_config"}

    if ml is None:
        from scripts.live_replay_engine import load_ml

        ml = load_ml()

    ledger_rows = conn.execute(
        """
        SELECT * FROM portfolio_daily_bets
        WHERE mode = ? AND calendar_date >= ?
        ORDER BY calendar_date ASC, bet_rank ASC
        """,
        (mode, cfg["start_date"]),
    ).fetchall()
    ledger_picks = [portfolio_bet_to_pick_row(dict(r)) for r in ledger_rows]

    _, ledger_summary = build_portfolio_curve_and_summary(conn, mode)

    sim_picks = []
    for p in ledger_picks:
        row = dict(p)
        row.pop("stake_frac", None)
        row.pop("stake_eur", None)
        row.pop("profit_eur", None)
        row.pop("bankroll_before_eur", None)
        row.pop("bankroll_after_eur", None)
        row.pop("replay_net_profit_eur", None)
        row.pop("theoretical_stake_frac", None)
        sim_picks.append(row)

    from scripts.live_replay_engine import kelly_replay_metrics

    enriched, _, sim_summary = kelly_replay_metrics(
        sim_picks,
        ml,
        bankroll_start=float(cfg["bankroll_start_eur"]),
    )

    sim_by_key = {_pick_reconcile_key(p): p for p in enriched}
    mismatches: list[dict[str, Any]] = []

    for led in ledger_picks:
        key = _pick_reconcile_key(led)
        sim = sim_by_key.get(key)
        if sim is None:
            mismatches.append(
                {
                    "key": key,
                    "field": "missing_in_sim",
                    "ledger": led.get("profit_eur"),
                    "sim": None,
                }
            )
            continue

        st = str(led.get("status") or "")
        settled = _is_settled_status(st)
        led_profit = led.get("profit_eur")
        sim_profit = sim.get("replay_net_profit_eur")

        if settled:
            if led_profit is None and sim_profit is not None:
                mismatches.append(
                    {
                        "key": key,
                        "field": "profit_eur",
                        "ledger": led_profit,
                        "sim": sim_profit,
                        "delta": None,
                    }
                )
            elif led_profit is not None and sim_profit is None:
                mismatches.append(
                    {
                        "key": key,
                        "field": "profit_eur",
                        "ledger": led_profit,
                        "sim": sim_profit,
                        "delta": None,
                    }
                )
            elif led_profit is not None and sim_profit is not None:
                delta = abs(float(led_profit) - float(sim_profit))
                if delta > tol_eur:
                    mismatches.append(
                        {
                            "key": key,
                            "field": "profit_eur",
                            "ledger": round(float(led_profit), 2),
                            "sim": round(float(sim_profit), 2),
                            "delta": round(delta, 2),
                        }
                    )

    summary_diff: dict[str, Any] = {}
    for field in ("bankroll_final_eur", "net_profit_eur", "total_staked_eur"):
        lv = float(ledger_summary.get(field) or 0)
        sv = float(sim_summary.get(field) or 0)
        if abs(lv - sv) > tol_eur:
            summary_diff[field] = {
                "ledger": round(lv, 2),
                "sim": round(sv, 2),
                "delta": round(abs(lv - sv), 2),
            }

    ok = not mismatches and not summary_diff
    return {
        "mode": mode,
        "ok": ok,
        "n_picks": len(ledger_picks),
        "n_mismatches": len(mismatches),
        "mismatches": mismatches,
        "summary_ledger": ledger_summary,
        "summary_sim": sim_summary,
        "summary_diff": summary_diff,
        "tracking_start_date": cfg["start_date"],
        "bankroll_start_eur": cfg["bankroll_start_eur"],
    }


def refresh_portfolio_tracking(conn: sqlite3.Connection, *, mode: str | None = None) -> None:
    """Sync settlement + recompute — appelé par daemon / publish."""
    sync_portfolio_settlement(conn, mode=mode)
    for m in ((mode,) if mode else MODES):
        if get_tracking_config(conn, m):
            recompute_portfolio_ledger(conn, m)


def on_published_picks_saved(
    conn: sqlite3.Connection,
    *,
    mode: str,
    calendar_date: str,
) -> None:
    """Hook après ``save_published_picks``."""
    if not get_tracking_config(conn, mode):
        return
    sync_portfolio_from_published(conn, mode=mode, calendar_date=calendar_date)
    refresh_portfolio_tracking(conn, mode=mode)
