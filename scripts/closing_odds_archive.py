"""Archive des cotes closing TE (derniere cote pre-coup d'envoi par match).

- Chaque scrape prematch met a jour un etat JSON (``data/cache/closing_odds_state.json``).
- Le daemon portfolio declenche une passe nocturne (defaut 04:00 Europe/Paris) :
  scrape TE hier + aujourd'hui + demain, fige les matchs du jour J-1, ecrit
  ``data/scraped/closing_odds/closing_odds_YYYY-MM-DD.csv`` + JSONL.

Variables d'environnement:
  BETTINGHUD_CLOSING_ODDS_ARCHIVE     (1/0, defaut 1)
  BETTINGHUD_CLOSING_ODDS_NIGHT_HOUR  (0-23, defaut 4)
  BETTINGHUD_CLOSING_ODDS_TZ          (defaut Europe/Paris)
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STATE_PATH = os.path.join(ROOT, "data", "cache", "closing_odds_state.json")
ARCHIVE_DIR = os.path.join(ROOT, "data", "scraped", "closing_odds")
JSONL_PATH = os.path.join(ARCHIVE_DIR, "closing_odds.jsonl")
LAST_RUN_PATH = os.path.join(ROOT, "data", "cache", ".closing_odds_archive_last_date")
NIGHT_LOCK_PATH = os.path.join(ROOT, "data", "scraped", ".closing_odds_nightly.lock")


def _env_flag(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _archive_tz() -> ZoneInfo:
    name = str(os.getenv("BETTINGHUD_CLOSING_ODDS_TZ", "Europe/Paris")).strip() or "Europe/Paris"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Paris")


def _norm_name(name: str) -> str:
    from scripts.sync_tml_recent import _norm_name as _n

    return _n(name)


def _match_key(match_date: str, tournament: str, player1: str, player2: str) -> str:
    a = _norm_name(player1)
    b = _norm_name(player2)
    t = str(tournament or "").strip().lower()[:120]
    return f"{match_date}|{t}|{a}|{b}"


def parse_match_calendar_date(row: dict) -> str:
    """Date calendrier du match (YYYY-MM-DD) depuis ligne scrape TE."""
    raw = str(row.get("date") or "").strip()[:10]
    if raw and re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        base = datetime.strptime(raw, "%Y-%m-%d").date()
    else:
        scraped = str(row.get("scraped_at") or "")
        try:
            base = pd.Timestamp(scraped).date()
        except Exception:
            base = datetime.now(_archive_tz()).date()
    ts = str(row.get("time") or "").strip()
    if ts.startswith("Demain"):
        base = base + timedelta(days=1)
    return base.isoformat()


def parse_match_start_local(row: dict) -> datetime | None:
    """Heure prevue du match en fuseau archive (Europe/Paris par defaut)."""
    tz = _archive_tz()
    cal = parse_match_calendar_date(row)
    try:
        match_date = datetime.strptime(cal, "%Y-%m-%d").date()
    except ValueError:
        return None
    ts = str(row.get("time") or "").strip()
    if ts.startswith("Demain"):
        ts = ts.replace("Demain", "", 1).strip()
    if not ts:
        return None
    try:
        sched_time = datetime.strptime(ts, "%H:%M").time()
    except ValueError:
        return None
    return datetime.combine(match_date, sched_time, tzinfo=tz)


def _load_state() -> dict[str, Any]:
    if not os.path.isfile(STATE_PATH):
        return {"version": 1, "matches": {}}
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"version": 1, "matches": {}}
        data.setdefault("matches", {})
        return data
    except Exception:
        return {"version": 1, "matches": {}}


def _save_state(state: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def ingest_match_rows(
    rows: list[dict],
    *,
    source: str = "prematch",
    scraped_at: str | None = None,
) -> int:
    """Met a jour l'etat closing depuis des lignes scrape (liste de dicts TE)."""
    if not rows:
        return 0
    state = _load_state()
    bucket: dict[str, Any] = state["matches"]
    now = datetime.now(_archive_tz())
    now_iso = scraped_at or now.isoformat()
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        p1 = str(row.get("player1") or "").strip()
        p2 = str(row.get("player2") or "").strip()
        if not p1 or not p2:
            continue
        try:
            o1 = float(row.get("odd_p1") or 0)
            o2 = float(row.get("odd_p2") or 0)
        except (TypeError, ValueError):
            continue
        if o1 <= 1.0 or o2 <= 1.0:
            continue
        match_date = parse_match_calendar_date(row)
        key = _match_key(match_date, str(row.get("tournament") or ""), p1, p2)
        start_local = parse_match_start_local(row)
        frozen = False
        if start_local is not None and now >= start_local - timedelta(minutes=3):
            frozen = True
        prev = bucket.get(key) or {}
        if prev.get("frozen") and frozen:
            # Deja fige : ne pas ecraser les cotes closing
            entry = dict(prev)
            entry["last_seen_at"] = now_iso
            entry["last_source"] = source
        elif frozen and not prev.get("frozen"):
            entry = {
                **prev,
                "match_date": match_date,
                "tournament": str(row.get("tournament") or ""),
                "tournament_url": str(row.get("tournament_url") or ""),
                "category": str(row.get("category") or ""),
                "time": str(row.get("time") or ""),
                "player1": p1,
                "player2": p2,
                "p1_url": str(row.get("p1_url") or ""),
                "p2_url": str(row.get("p2_url") or ""),
                "odd_p1": o1,
                "odd_p2": o2,
                "closing_odd_p1": float(prev.get("odd_p1") or o1),
                "closing_odd_p2": float(prev.get("odd_p2") or o2),
                "frozen": True,
                "frozen_at": now_iso,
                "last_seen_at": now_iso,
                "last_source": source,
            }
        else:
            entry = {
                "match_date": match_date,
                "tournament": str(row.get("tournament") or ""),
                "tournament_url": str(row.get("tournament_url") or ""),
                "category": str(row.get("category") or ""),
                "time": str(row.get("time") or ""),
                "player1": p1,
                "player2": p2,
                "p1_url": str(row.get("p1_url") or ""),
                "p2_url": str(row.get("p2_url") or ""),
                "odd_p1": o1,
                "odd_p2": o2,
                "closing_odd_p1": o1,
                "closing_odd_p2": o2,
                "frozen": bool(prev.get("frozen")),
                "frozen_at": prev.get("frozen_at"),
                "last_seen_at": now_iso,
                "last_source": source,
            }
        bucket[key] = entry
        n += 1
    state["matches"] = bucket
    _save_state(state)
    return n


def _rows_for_archive_date(state: dict[str, Any], archive_date: str) -> list[dict]:
    out: list[dict] = []
    for _key, m in (state.get("matches") or {}).items():
        if str(m.get("match_date") or "") != archive_date:
            continue
        co1 = m.get("closing_odd_p1", m.get("odd_p1"))
        co2 = m.get("closing_odd_p2", m.get("odd_p2"))
        try:
            co1f = float(co1)
            co2f = float(co2)
        except (TypeError, ValueError):
            continue
        if co1f <= 1.0 or co2f <= 1.0:
            continue
        out.append(
            {
                "match_date": archive_date,
                "tournament": m.get("tournament"),
                "tournament_url": m.get("tournament_url"),
                "category": m.get("category"),
                "time": m.get("time"),
                "player1": m.get("player1"),
                "player2": m.get("player2"),
                "p1_url": m.get("p1_url"),
                "p2_url": m.get("p2_url"),
                "closing_odd_p1": co1f,
                "closing_odd_p2": co2f,
                "frozen_at": m.get("frozen_at") or m.get("last_seen_at"),
                "archived_at": datetime.now(_archive_tz()).isoformat(),
            }
        )
    return out


def finalize_archive_day(archive_date: str) -> tuple[str | None, int]:
    """Ecrit le CSV journalier + append JSONL pour ``archive_date`` (YYYY-MM-DD)."""
    state = _load_state()
    rows = _rows_for_archive_date(state, archive_date)
    if not rows:
        return None, 0
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    out_path = os.path.join(ARCHIVE_DIR, f"closing_odds_{archive_date}.csv")
    df = pd.DataFrame(rows)
    df.to_csv(out_path, index=False)
    try:
        with open(JSONL_PATH, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return out_path, len(rows)


def _acquire_nightly_lock() -> bool:
    if os.path.exists(NIGHT_LOCK_PATH):
        try:
            age = time.time() - os.path.getmtime(NIGHT_LOCK_PATH)
            if age < 1800:
                return False
            os.remove(NIGHT_LOCK_PATH)
        except OSError:
            return False
    try:
        os.makedirs(os.path.dirname(NIGHT_LOCK_PATH), exist_ok=True)
        with open(NIGHT_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        return True
    except OSError:
        return False


def _release_nightly_lock() -> None:
    try:
        if os.path.exists(NIGHT_LOCK_PATH):
            os.remove(NIGHT_LOCK_PATH)
    except OSError:
        pass


def _prematch_or_portfolio_scrape_busy() -> bool:
    from scripts.portfolio_sync_lock import scrape_in_progress

    prematch_lock = os.path.join(ROOT, "data", "scraped", ".prematch_scrape.lock")
    if scrape_in_progress():
        return True
    if os.path.exists(prematch_lock):
        try:
            if time.time() - os.path.getmtime(prematch_lock) < 900:
                return True
        except OSError:
            pass
    return False


async def _scrape_offsets(offsets: list[int]) -> list[dict]:
    from scripts.scraper_prematch import FlashscoreScraper

    scraper = FlashscoreScraper()
    parts: list[dict] = []
    for off in offsets:
        batch = await scraper.get_matches_and_odds(day_offset=int(off))
        if batch:
            parts.extend(batch)
    return parts


def run_nightly_closing_archive(*, logger=None) -> dict[str, Any]:
    """Scrape TE + fige la veille + ecrit CSV closing. Retourne stats."""
    log = logger.info if logger else print

    if not _acquire_nightly_lock():
        return {"skipped": True, "reason": "lock"}
    try:
        if _prematch_or_portfolio_scrape_busy():
            return {"skipped": True, "reason": "scrape_busy"}

        tz = _archive_tz()
        today = datetime.now(tz).date()
        yesterday = today - timedelta(days=1)
        archive_date = yesterday.isoformat()

        log("Closing odds : scrape TE offsets -1, 0, +1 …")
        rows = asyncio.run(_scrape_offsets([-1, 0, 1]))
        n_ingest = ingest_match_rows(rows, source="nightly", scraped_at=datetime.now(tz).isoformat())
        log("Closing odds : %d lignes ingerees depuis TE", n_ingest)

        # Figer explicitement tous les matchs de la veille encore ouverts
        state = _load_state()
        now_iso = datetime.now(tz).isoformat()
        for _key, m in list((state.get("matches") or {}).items()):
            if str(m.get("match_date") or "") != archive_date:
                continue
            if not m.get("frozen"):
                m["frozen"] = True
                m["frozen_at"] = m.get("frozen_at") or now_iso
                m["closing_odd_p1"] = float(m.get("odd_p1") or m.get("closing_odd_p1") or 0)
                m["closing_odd_p2"] = float(m.get("odd_p2") or m.get("closing_odd_p2") or 0)
        state["matches"] = state.get("matches") or {}
        _save_state(state)

        path, n_out = finalize_archive_day(archive_date)
        if path:
            log("Closing odds : archive %s (%d matchs)", path, n_out)
        else:
            log("Closing odds : aucun match a archiver pour %s", archive_date)

        try:
            with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
                f.write(today.isoformat())
        except OSError:
            pass

        return {
            "skipped": False,
            "archive_date": archive_date,
            "ingested": n_ingest,
            "archived": n_out,
            "path": path,
        }
    finally:
        _release_nightly_lock()


def should_run_nightly_archive_now() -> bool:
    """True une fois par jour calendaire pendant l'heure configuree."""
    if not _env_flag("BETTINGHUD_CLOSING_ODDS_ARCHIVE", True):
        return False
    try:
        hour_target = int(os.getenv("BETTINGHUD_CLOSING_ODDS_NIGHT_HOUR", "4"))
    except ValueError:
        hour_target = 4
    hour_target = max(0, min(23, hour_target))
    tz = _archive_tz()
    now = datetime.now(tz)
    if now.hour != hour_target:
        return False
    today_s = now.date().isoformat()
    if os.path.isfile(LAST_RUN_PATH):
        try:
            with open(LAST_RUN_PATH, encoding="utf-8") as f:
                if f.read().strip() == today_s:
                    return False
        except OSError:
            pass
    return True


def list_closing_archive_csvs() -> list[str]:
    if not os.path.isdir(ARCHIVE_DIR):
        return []
    files = glob.glob(os.path.join(ARCHIVE_DIR, "closing_odds_*.csv"))
    return sorted(files, key=os.path.getmtime, reverse=True)


def load_closing_odds_index() -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    """Index nom (||) et optionnel id depuis tous les CSV closing + etat non archive."""
    idx_name: dict[str, tuple[float, float]] = {}
    for path in list_closing_archive_csvs():
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        for _, r in df.iterrows():
            p1 = _norm_name(r.get("player1"))
            p2 = _norm_name(r.get("player2"))
            if not p1 or not p2:
                continue
            try:
                o1 = float(r.get("closing_odd_p1"))
                o2 = float(r.get("closing_odd_p2"))
            except (TypeError, ValueError):
                continue
            if o1 <= 1.0 or o2 <= 1.0:
                continue
            idx_name["||".join(sorted([p1, p2]))] = (o1, o2)
    state = _load_state()
    for m in (state.get("matches") or {}).values():
        if m.get("frozen"):
            p1 = _norm_name(m.get("player1"))
            p2 = _norm_name(m.get("player2"))
            if not p1 or not p2:
                continue
            try:
                o1 = float(m.get("closing_odd_p1"))
                o2 = float(m.get("closing_odd_p2"))
            except (TypeError, ValueError):
                continue
            if o1 > 1.0 and o2 > 1.0:
                idx_name["||".join(sorted([p1, p2]))] = (o1, o2)
    return idx_name


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive cotes closing TE")
    parser.add_argument("--once", action="store_true", help="Passe nocturne immediate")
    parser.add_argument(
        "--finalize-date",
        type=str,
        default=None,
        help="Re-ecrit le CSV pour une date YYYY-MM-DD depuis l'etat",
    )
    args = parser.parse_args()
    if args.finalize_date:
        path, n = finalize_archive_day(str(args.finalize_date)[:10])
        print(f"{path or 'vide'} ({n} matchs)")
        return 0
    if args.once or should_run_nightly_archive_now():
        stats = run_nightly_closing_archive()
        print(stats)
        return 0
    print("Rien a faire (hors fenetre horaire). Utilisez --once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
