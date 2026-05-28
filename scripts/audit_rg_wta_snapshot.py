#!/usr/bin/env python3
"""Audit French Open WTA matches in live snapshot."""
from __future__ import annotations

import json
import os
import sys

import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _load_matches():
    for path in (
        os.path.join(ROOT, "data", "cache", "live_matches_snapshot.full.joblib"),
        os.path.join(ROOT, "data", "cache", "live_matches_snapshot.joblib"),
    ):
        if os.path.isfile(path):
            data = joblib.load(path)
            if isinstance(data, dict):
                return data.get("matches") or list(data.values()), path
            return data, path
    return [], None


def _is_rg_wta(m: dict) -> bool:
    t = str(m.get("tournament") or "").lower()
    tour = str(m.get("tour") or m.get("category") or "").upper()
    if tour not in ("WTA", "W"):
        return False
    return any(x in t for x in ("roland", "garros", "french open", "paris"))


def _implied(odd) -> float | None:
    try:
        o = float(odd)
        return 1.0 / o if o > 1.0 else None
    except (TypeError, ValueError):
        return None


def _model_p1_prob(m: dict) -> float:
    fs = m.get("feature_snapshot") or {}
    for key in ("capped_p1_prob", "raw_p1_prob", "p1_win_prob"):
        v = fs.get(key) if key != "p1_win_prob" else m.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.5


def _audit(m: dict, idx: int) -> dict:
    p1 = m.get("player1", "?")
    p2 = m.get("player2", "?")
    fs = m.get("feature_snapshot") or {}
    p = _model_p1_prob(m)
    o1 = float(m.get("odd_p1") or 0)
    o2 = float(m.get("odd_p2") or 0)
    imp1, imp2 = _implied(o1), _implied(o2)
    flags: list[str] = []

    if p <= 0.08 or p >= 0.92:
        flags.append("proba_extrême (>92% ou <8%)")
    if imp1 is not None and abs(p - imp1) > 0.22:
        flags.append(f"écart modèle/book P1 {abs(p-imp1)*100:.0f} pp")
    if imp2 is not None and abs((1.0 - p) - imp2) > 0.22:
        flags.append(f"écart modèle/book P2 {abs((1-p)-imp2)*100:.0f} pp")

    r1, r2 = m.get("p1_rank"), m.get("p2_rank")
    try:
        r1f, r2f = float(r1), float(r2)
        if r1f + 5 < r2f and p > 0.62:
            flags.append("favori rang P1 mais proba très haute (vérifier)")
        if r2f + 5 < r1f and p < 0.38:
            flags.append("favori rang P2 mais proba P1 basse (vérifier)")
    except (TypeError, ValueError):
        pass

    conf = m.get("confidence")
    if conf is not None:
        try:
            if float(conf) < 0.25:
                flags.append("confiance IA faible")
        except (TypeError, ValueError):
            pass

    if not m.get("p1_profile_loaded") or not m.get("p2_profile_loaded"):
        flags.append("profil TE incomplet")

    if m.get("data_alert") or m.get("unreliable"):
        flags.append(f"alerte données: {m.get('data_alert') or m.get('unreliable')}")

    val_p1 = m.get("is_value_p1") or m.get("value_p1")
    val_p2 = m.get("is_value_p2") or m.get("value_p2")
    ev_p1 = m.get("value_pct_p1")
    ev_p2 = m.get("value_pct_p2")

    return {
        "n": idx,
        "date": m.get("date"),
        "match": f"{p1} vs {p2}",
        "tournament": m.get("tournament"),
        "tour": m.get("tour") or m.get("category"),
        "surface": m.get("surface"),
        "p1_win_prob_pct": round(p * 100, 1),
        "raw_p1_prob_pct": round(float(fs.get("raw_p1_prob") or p) * 100, 1) if fs.get("raw_p1_prob") is not None else None,
        "caps_applied": fs.get("caps_applied"),
        "p1_global_elo": fs.get("p1_global_elo"),
        "p2_global_elo": fs.get("p2_global_elo"),
        "odd_p1": o1,
        "odd_p2": o2,
        "book_implied_p1_pct": round(imp1 * 100, 1) if imp1 else None,
        "book_implied_p2_pct": round(imp2 * 100, 1) if imp2 else None,
        "rank_p1": r1,
        "rank_p2": r2,
        "confidence": conf,
        "segment_brier": m.get("segment_brier"),
        "value_p1": val_p1,
        "value_p2": val_p2,
        "ev_p1_pct": ev_p1,
        "ev_p2_pct": ev_p2,
        "flags": flags if flags else ["OK — rien d'anormal détecté"],
    }


def main() -> int:
    matches, path = _load_matches()
    rg = [m for m in matches if _is_rg_wta(m)]
    rg.sort(key=lambda m: (str(m.get("date") or ""), str(m.get("player1") or "")))
    print(f"Snapshot: {path}")
    print(f"Total matchs: {len(matches)} | Roland-Garros WTA: {len(rg)}")
    sample = rg[:5]
    if not sample:
        print("Aucun match Roland-Garros WTA dans le snapshot.")
        return 1
    audits = [_audit(m, i + 1) for i, m in enumerate(sample)]
    print(json.dumps(audits, indent=2, ensure_ascii=False))
    n_warn = sum(1 for a in audits if a["flags"] != ["OK — rien d'anormal détecté"])
    print(f"\nRésumé: {len(audits)} matchs passés, {n_warn} avec signal(s) à surveiller.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
