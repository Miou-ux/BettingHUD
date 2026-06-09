#!/usr/bin/env python3
"""Liste tournois du jour dans le snapshot (brut vs filtre major ATP/WTA)."""
import os
import sys
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import joblib

PARIS = ZoneInfo("Europe/Paris")
today = datetime.now(PARIS).date().isoformat()


def is_major(m: dict) -> bool:
    c = str(m.get("category") or "").strip().upper()
    if c not in {"ATP", "WTA"}:
        return False
    t = str(m.get("tournament") or "").lower()
    minor = (
        "challenger", "itf", "utr", "utr pro tennis", "universal tennis",
        "futures", "future", "m15", "m25", "m35", "m50", "m60", "m80", "m100",
        "w15", "w25", "w35", "w50", "w60", "w80", "w100",
    )
    return not any(tok in t for tok in minor)


def main() -> None:
    for path in (
        "data/cache/live_matches_snapshot.full.joblib",
        "data/cache/live_matches_snapshot.joblib",
    ):
        if os.path.isfile(path):
            payload = joblib.load(path)
            break
    else:
        print("Pas de snapshot")
        return
    if isinstance(payload, dict):
        ms = list(payload.get("matches") or [])
    elif isinstance(payload, list):
        ms = list(payload)
    else:
        ms = []
    today_ms = [m for m in ms if str(m.get("date") or "")[:10] == today]

    print(f"Date Paris: {today}")
    print(f"Snapshot total: {len(ms)} | calendrier aujourd'hui: {len(today_ms)}")
    print(f"Categories (jour): {dict(Counter(str(m.get('category') or '?') for m in today_ms))}")

    print("\n--- Tous les tournois du jour (nom + category) ---")
    seen: dict[tuple[str, str], int] = {}
    for m in today_ms:
        key = (str(m.get("category") or ""), str(m.get("tournament") or ""))
        seen[key] = seen.get(key, 0) + 1
    for (c, t), n in sorted(seen.items(), key=lambda x: (-x[1], x[1])):
        flag = "MAJOR" if is_major({"category": c, "tournament": t}) else "exclu"
        print(f"  [{flag:5s}] {n:3d} matchs · [{c}] {t}")

    maj = [m for m in today_ms if is_major(m)]
    print(f"\nApres filtre major ATP/WTA (Paris du jour / Live Tracker): {len(maj)} matchs")
    print("Tournois:", ", ".join(sorted({str(m.get('tournament')) for m in maj})) or "—")

    print(f"\n--- Snapshot complet ({len(ms)} matchs, jour + demain) ---")
    seen_all: dict[tuple[str, str], int] = {}
    for m in ms:
        key = (str(m.get("category") or ""), str(m.get("tournament") or ""))
        seen_all[key] = seen_all.get(key, 0) + 1
    for (c, t), n in sorted(seen_all.items(), key=lambda x: (-x[1], x[1])):
        flag = "MAJOR" if is_major({"category": c, "tournament": t}) else "mineur/autre"
        print(f"  [{flag:12s}] {n:3d} · [{c}] {t}")


if __name__ == "__main__":
    main()
