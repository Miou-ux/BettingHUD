"""Clés et résolution 1 Day 1 Pick — évite collision avec Top5 ``date|tour|rank``."""
from __future__ import annotations

import sqlite3
from typing import Any

OD1P_KEY_SUFFIX = "1D1P"


def od1p_post_pick_key(calendar_date: str) -> str:
    """Clé journal posts 1D1P (une par jour, indépendante du rank Top5)."""
    return f"{str(calendar_date)[:10]}|{OD1P_KEY_SUFFIX}"


def is_od1p_post_pick_key(pick_key: str | None) -> bool:
    return str(pick_key or "").endswith(f"|{OD1P_KEY_SUFFIX}")


def _norm_label(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _match_labels(row: dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    mn = str(row.get("match_name") or "").strip()
    if mn:
        labels.add(_norm_label(mn))
    p1, p2 = str(row.get("player1") or "").strip(), str(row.get("player2") or "").strip()
    if p1 and p2:
        labels.add(_norm_label(f"{p1} vs {p2}"))
        labels.add(_norm_label(f"{p2} vs {p1}"))
    fav, dog = str(row.get("fav_player") or "").strip(), str(row.get("underdog_player") or "").strip()
    if fav and dog:
        labels.add(_norm_label(f"{fav} vs {dog}"))
        labels.add(_norm_label(f"{dog} vs {fav}"))
    return {x for x in labels if x}


def resolve_od1p_pick_for_result(
    conn: sqlite3.Connection,
    *,
    calendar_date: str,
    message_preview: str | None = None,
) -> dict[str, Any] | None:
    """Retrouve le match 1D1P publié via aperçu (pas via pick_key Top5)."""
    cal = str(calendar_date)[:10]
    preview = (message_preview or "").strip()
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM daily_top_proba_picks WHERE calendar_date = ?",
            (cal,),
        ).fetchall()
    ]
    if not rows:
        return None
    if not preview or preview == "no_pick":
        return None

    np = _norm_label(preview)
    for row in rows:
        if np in _match_labels(row):
            return row
    return None


def fetch_od1p_pending_results(
    conn: sqlite3.Connection,
    *,
    posts_table: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Posts ``daily_pick`` sans ``result`` → (post_pick_key, pick_résolu)."""
    if posts_table not in ("discord_1d1p_posts", "telegram_1d1p_posts"):
        raise ValueError(f"unsupported posts_table: {posts_table}")

    rows = conn.execute(
        f"""
        SELECT d.calendar_date, d.pick_key, d.message_preview
        FROM {posts_table} d
        WHERE d.post_type = 'daily_pick'
          AND d.pick_key IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM {posts_table} r
            WHERE r.post_type = 'result' AND r.pick_key = d.pick_key
          )
        ORDER BY d.calendar_date ASC
        """
    ).fetchall()

    out: list[tuple[str, dict[str, Any]]] = []
    for cal, post_pick_key, preview in rows:
        pick = resolve_od1p_pick_for_result(conn, calendar_date=str(cal), message_preview=preview)
        if not pick:
            continue
        if str(pick.get("status") or "En cours") == "En cours":
            continue
        out.append((str(post_pick_key), pick))
    return out
