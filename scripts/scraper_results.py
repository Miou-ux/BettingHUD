"""Resolve user bets to Gagne / Perdu / Annule using Tennis Explorer (primary)
and Sackmann GitHub CSVs (fallback).

Improvements vs v1:
- Lookback window: 7 days (was 1-2). Late-rescheduled matches are picked up.
- Persistent log file: data/logs/scraper_results.log (rotating).
- Cache of resolved matches in `match_results` table; days fully in the past
  are never re-scraped.
- Retries with exponential backoff on Playwright via tenacity.
- Robust matching: unidecode + rapidfuzz on surnames; tolerates accents,
  hyphenated names, "(seed)/(Q)/(WC)" markers.
- New status `Annule` for retired / walkover / cancelled matches (refunds stake).
- Sackmann fallback runs automatically when TE fails or hasn't resolved a bet
  within ~24h.
- Subprocess monitoring: stdout/stderr captured to file so silent failures
  become visible.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Iterable

from playwright.async_api import async_playwright
from rapidfuzz import fuzz
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from unidecode import unidecode

from scripts.bets_db import (
    DB_PATH_DEFAULT,
    correct_retirement_voids_user_bets,
    ensure_match_results_cache,
    ensure_user_bets_schema,
    normalize_schedule_date,
    read_cached_results,
    settle_bet,
    write_cached_results,
)
from scripts.sackmann_results import load_recent_results as load_sackmann_results

# ---------------------------------------------------------------------------
# Logging setup (rotating file + console)
# ---------------------------------------------------------------------------

LOG_DIR = os.path.join("data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "scraper_results.log")


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("scraper_results")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    os.makedirs(LOG_DIR, exist_ok=True)
    fh = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    fh.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


LOGGER = _setup_logger()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_DAYS_DEFAULT = 7
NAME_FUZZY_THRESHOLD = 88  # 0..100 — surname fuzzy ratio required for a match
NEARBY_DAY_OFFSETS = (-2, -1, 1, 2, 3)  # handle longer postponements
# Upper inclusive date for TE/Sackmann cache load + fuzzy lookup: Tennis Explorer often
# files late EU matches on the "next calendar day" while bets stay on the booked date.
LOOKUP_UPPER_EXTRA_DAYS = max(NEARBY_DAY_OFFSETS)  # 3 — keep aligned with NEARBY forward span
RETIRED_TOKENS = ("ret.", "ret ", "retired", "abandon", " abd")
WALKOVER_TOKENS = ("w.o.", "wo.", "walkover", " w/o", " w.o", "default")
CANCELLED_TOKENS = ("cancel", "annul")


# ---------------------------------------------------------------------------
# Name canonicalisation
# ---------------------------------------------------------------------------


def _strip_seeds(s: str) -> str:
    """Remove (Q), (WC), (3), (LL), (PR)... markers."""
    return re.sub(r"\s*\([^)]*\)", " ", s).strip()


def _normalize_name(s: str) -> str:
    s = _strip_seeds(s)
    s = unidecode(str(s)).lower()
    s = re.sub(r"[^a-z0-9\s\.\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _split_name(s: str) -> tuple[str, str]:
    """Return (surname_canonical, first_fragment).

    Detection heuristic:
    - Tennis Explorer / bookmakers format: "Surname I." or "Surname Ka." or
      "Auger-Aliassime F." -> trailing token is the (multi-letter) initial.
    - Sackmann format: "Felix Auger-Aliassime" / "Karolina Pliskova" ->
      leading token is the first name. Multi-word surnames join with '-'.

    The first_fragment we keep is the FULL initial-or-first-name token so we
    can do prefix matching ("ka" prefix of "karolina"), which disambiguates
    Pliskova Ka. from Pliskova Kr.
    """
    n = _normalize_name(s).replace(".", "")
    tokens = [t for t in n.split(" ") if t]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0], ""
    last = tokens[-1]
    # TE convention: trailing token is the initial (1-3 chars), the rest is the surname
    if len(last) <= 3:
        surname_parts = tokens[:-1]
        first = last
    else:
        # "First [Middle] Last" -> first token is the first name, the rest is surname
        surname_parts = tokens[1:]
        first = tokens[0]
    surname = "-".join(surname_parts)
    return surname, first


def _surname_token(s: str) -> str:
    return _split_name(s)[0]


def _initial(s: str) -> str:
    """Return the (potentially multi-char) initial fragment for ``s``."""
    return _split_name(s)[1]


def _first_fragment_match(a: str, b: str) -> bool:
    """Two first-name fragments match if one is a prefix of the other.

    Examples:
    - 'ka' vs 'karolina' -> True
    - 'kr' vs 'karolina' -> False
    - 'f'  vs 'felix'    -> True
    - ''   vs anything   -> True (one side missing, don't block)
    """
    if not a or not b:
        return True
    if len(a) <= len(b):
        return b.startswith(a)
    return a.startswith(b)


def canonical_player(s: str) -> str:
    """Canonical form used as cache key: 'surname f' (lowercase, ASCII).

    The single initial is enough for cache-key uniqueness given how rare
    same-surname/same-initial collisions are. The full first-fragment is only
    used by ``names_match`` for fine-grained discrimination (Pliskova Ka./Kr.).
    """
    sur, first = _split_name(s)
    ini = first[:1] if first else ""
    if sur and ini:
        return f"{sur} {ini}".strip()
    return sur or _normalize_name(s).replace(" ", "_")


def names_match(a: str, b: str, *, threshold: int = NAME_FUZZY_THRESHOLD) -> bool:
    """Return True if a and b plausibly refer to the same player.

    Strategy:
    1. Compare surnames exactly (after canonicalisation): if equal AND the
       first-name fragments are compatible (prefix match), accept.
    2. Fall back to fuzzy ratio on the surname token alone (handles typos /
       transliteration drift like "krejcikova" vs "krejickova").
    3. Initials must always be compatible; same surname with mismatched
       initials (Pliskova Ka. vs Kr.) is rejected.
    """
    if not a or not b:
        return False
    sa, fa = _split_name(a)
    sb, fb = _split_name(b)
    if not sa or not sb:
        return False
    if not _first_fragment_match(fa, fb):
        return False
    if sa == sb:
        return True
    score = fuzz.ratio(sa, sb)
    return score >= threshold


# ---------------------------------------------------------------------------
# Score parsing
# ---------------------------------------------------------------------------


def _classify_score(score: str) -> tuple[bool, bool]:
    """Return (retired, walkover) flags from a score string."""
    if not score:
        return False, False
    s = score.lower()
    retired = any(tok in s for tok in RETIRED_TOKENS)
    walkover = any(tok in s for tok in WALKOVER_TOKENS) or any(
        tok in s for tok in CANCELLED_TOKENS
    )
    return retired, walkover


def _parse_te_sets_won(res_text: str) -> tuple[int | None, bool]:
    """Parse TE td.result: '2', '1 ret.' -> (sets_won, retirement_marker)."""
    if not res_text:
        return None, False
    s = res_text.strip().lower()
    has_ret = any(tok in s for tok in RETIRED_TOKENS)
    m = re.search(r"(\d+)", s)
    n = int(m.group(1)) if m else None
    return n, has_ret


def _is_set_complete(g1: int, g2: int) -> bool:
    """Rough tennis set completion (6+ with 2-game margin, or 7-5 / 7-6)."""
    hi, lo = max(g1, g2), min(g1, g2)
    if hi < 6:
        return False
    if hi == 6 and lo <= 4:
        return True
    if hi == 7 and lo in (5, 6):
        return True
    return hi >= 6 and hi - lo >= 2


def _te_last_set_game_scores(full_score: str) -> tuple[int, int] | None:
    """TE layout: p1 games per set | p2 games per set (e.g. '6 6 | 4 4' → last set 6-4)."""
    parts = [p.strip() for p in (full_score or "").split("|") if p.strip()]
    if len(parts) != 2:
        return None
    t1, t2 = parts[0].split(), parts[1].split()
    if not t1 or not t2:
        return None
    try:
        return int(t1[-1]), int(t2[-1])
    except ValueError:
        return None


def _last_te_set_incomplete(full_score: str) -> bool:
    last = _te_last_set_game_scores(full_score)
    if not last:
        return False
    g1, g2 = last
    return (g1 > 0 or g2 > 0) and not _is_set_complete(g1, g2)


def _winner_from_incomplete_last_set(
    full_score: str, p1_name: str, p2_name: str
) -> str | None:
    """When a match ends mid-set, the player ahead in the last set wins."""
    last = _te_last_set_game_scores(full_score)
    if not last:
        return None
    g1, g2 = last
    if g1 == g2:
        return None
    return p1_name if g1 > g2 else p2_name


def match_result_effective_retired(
    *,
    retired: bool,
    walkover: bool,
    score: str | None,
) -> bool:
    """Ne pas faire confiance au flag DB seul — valider via le score TE."""
    if walkover:
        return False
    sc = score or ""
    explicit, _ = _classify_score(sc)
    if explicit:
        return True
    return score_suggests_retirement(sc)


def score_suggests_retirement(score: str) -> bool:
    """Infer retirement from score when TE omits 'ret.' (e.g. '6 3 | 2 4')."""
    if not score:
        return False
    retired, walkover = _classify_score(score)
    if walkover:
        return False
    if retired:
        return True
    return _last_te_set_incomplete(score)


def _infer_te_retirement(
    *,
    p1_res: str,
    p2_res: str,
    full_score: str,
    sets_p1: int | None,
    sets_p2: int | None,
) -> bool:
    """TE often omits 'ret.' in score cells — infer from result columns / partial sets."""
    retired, walkover = _classify_score(full_score)
    if walkover:
        return False
    if retired:
        return True
    _, ret1 = _parse_te_sets_won(p1_res)
    _, ret2 = _parse_te_sets_won(p2_res)
    if ret1 or ret2:
        return True
    if _classify_score(f"{p1_res} {p2_res}")[0]:
        return True
    if (
        sets_p1 is not None
        and sets_p2 is not None
        and sets_p1 == sets_p2
        and sets_p1 > 0
    ):
        return _last_te_set_incomplete(full_score)
    return _last_te_set_incomplete(full_score)


# ---------------------------------------------------------------------------
# Tennis Explorer scraper (with retries)
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
async def _scrape_te_day(page, target_date: str) -> list[dict]:
    """Scrape Tennis Explorer for a single ISO date (YYYY-MM-DD).

    Returns list of {p1_name, p2_name, p1_score, p2_score, winner_name,
    score_text, retired, walkover}.
    """
    date_obj = datetime.strptime(target_date, "%Y-%m-%d")
    url = (
        f"https://www.tennisexplorer.com/results/?type=all&"
        f"year={date_obj.year}&month={date_obj.strftime('%m')}&day={date_obj.strftime('%d')}"
    )
    LOGGER.info("Tennis Explorer fetch %s -> %s", target_date, url)
    await page.goto(url, timeout=60000)
    await page.wait_for_timeout(1500)

    rows = await page.query_selector_all("table.result tbody tr")
    out: list[dict] = []
    current: dict | None = None
    for row in rows:
        class_name = await row.get_attribute("class") or ""
        if not ("one" in class_name or "two" in class_name):
            current = None
            continue
        cells = await row.query_selector_all("td")
        if not cells:
            continue
        name_el = await row.query_selector("td.t-name a")
        name = (await name_el.inner_text()).strip() if name_el else ""
        res_el = await row.query_selector("td.result")
        res = (await res_el.inner_text()).strip() if res_el else ""
        # the score text (sets) lives in subsequent td.score cells
        score_cells = await row.query_selector_all("td.score")
        sets_text = []
        for sc in score_cells:
            sets_text.append((await sc.inner_text()).strip())
        score_str = " ".join([s for s in sets_text if s])

        if "bott" in class_name:
            current = {
                "p1_name": name,
                "p1_score": res,
                "p1_sets": score_str,
            }
        else:
            if current and name:
                p2_name = name
                p1 = current["p1_name"]
                p1_res = current["p1_score"]
                p2_res = res
                s1, ret1 = _parse_te_sets_won(p1_res)
                s2, ret2 = _parse_te_sets_won(p2_res)
                full_score = f"{current.get('p1_sets', '')} | {score_str}".strip(" |")
                _, walkover = _classify_score(full_score)
                retired = (
                    False
                    if walkover
                    else _infer_te_retirement(
                        p1_res=p1_res,
                        p2_res=p2_res,
                        full_score=full_score,
                        sets_p1=s1,
                        sets_p2=s2,
                    )
                )
                if s1 is not None and s2 is not None and s1 != s2:
                    winner_name = p1 if s1 > s2 else p2_name
                elif retired:
                    winner_name = _winner_from_incomplete_last_set(full_score, p1, p2_name)
                    if not winner_name:
                        if ret1 and not ret2:
                            winner_name = p2_name
                        elif ret2 and not ret1:
                            winner_name = p1
                else:
                    winner_name = None
                out.append(
                    {
                        "p1_name": p1,
                        "p2_name": p2_name,
                        "winner_name": winner_name,
                        "score_text": full_score,
                        "retired": retired,
                        "walkover": walkover,
                    }
                )
                current = None
    LOGGER.info("Tennis Explorer %s -> %d matches parsed", target_date, len(out))
    return out


async def _scrape_te_dates(dates: list[str]) -> dict[str, list[dict]]:
    """Scrape multiple dates in a single Playwright session."""
    out: dict[str, list[dict]] = {}
    if not dates:
        return out
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        try:
            for d in dates:
                try:
                    out[d] = await _scrape_te_day(page, d)
                except Exception as exc:
                    LOGGER.error("TE scrape failed for %s after retries: %s", d, exc)
                    out[d] = []
        finally:
            await browser.close()
    return out


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def _is_day_complete(d_iso: str) -> bool:
    """A day is considered complete (cache permanent) once it's strictly in the past."""
    try:
        dt = datetime.strptime(d_iso, "%Y-%m-%d").date()
    except Exception:
        return False
    return dt < datetime.now().date()


def _store_te_results_in_cache(
    conn: sqlite3.Connection, results_by_date: dict[str, list[dict]]
) -> int:
    # Les pages TE affichent aussi des matchs pas encore terminés. Les anciennes
    # entrées vides (winner NULL + score vide) ne doivent pas masquer un résultat
    # plus récent ou donner un faux "hit" au portefeuille.
    for d in results_by_date.keys():
        conn.execute(
            """
            DELETE FROM match_results
            WHERE match_date = ?
              AND source = 'tennisexplorer'
              AND winner_canonical IS NULL
              AND COALESCE(score, '') = ''
            """,
            (d,),
        )
    rows = []
    for d, matches in results_by_date.items():
        for m in matches:
            if not m.get("winner_name") and not str(m.get("score_text") or "").strip():
                continue
            p1c = canonical_player(m["p1_name"])
            p2c = canonical_player(m["p2_name"])
            wc = (
                canonical_player(m["winner_name"])
                if m.get("winner_name")
                else None
            )
            # store both directions so a lookup with reversed names also hits
            rows.append(
                {
                    "match_date": d,
                    "p1_canonical": p1c,
                    "p2_canonical": p2c,
                    "winner_canonical": wc,
                    "score": m.get("score_text"),
                    "retired": m.get("retired", False),
                    "walkover": m.get("walkover", False),
                    "source": "tennisexplorer",
                }
            )
            rows.append(
                {
                    "match_date": d,
                    "p1_canonical": p2c,
                    "p2_canonical": p1c,
                    "winner_canonical": wc,
                    "score": m.get("score_text"),
                    "retired": m.get("retired", False),
                    "walkover": m.get("walkover", False),
                    "source": "tennisexplorer",
                }
            )
    return write_cached_results(conn, rows)


def _store_sackmann_results_in_cache(
    conn: sqlite3.Connection, sackmann_rows: list[dict]
) -> int:
    rows = []
    for m in sackmann_rows:
        p1c = canonical_player(m["winner_name"])
        p2c = canonical_player(m["loser_name"])
        score = m.get("score") or ""
        retired, walkover = _classify_score(score)
        rows.append(
            {
                "match_date": m["match_date"],
                "p1_canonical": p1c,
                "p2_canonical": p2c,
                "winner_canonical": p1c,
                "score": score,
                "retired": retired,
                "walkover": walkover,
                "tour": m.get("tour"),
                "source": "sackmann",
            }
        )
        rows.append(
            {
                "match_date": m["match_date"],
                "p1_canonical": p2c,
                "p2_canonical": p1c,
                "winner_canonical": p1c,
                "score": score,
                "retired": retired,
                "walkover": walkover,
                "tour": m.get("tour"),
                "source": "sackmann",
            }
        )
    return write_cached_results(conn, rows)


def _lookup_in_cache(
    cache: dict[str, dict[str, dict]],
    *,
    bet_date: str,
    bet_p1: str,
    bet_p2: str,
    nearby_dates: list[str],
) -> dict | None:
    """Look up a match across the cache. Try exact date first then nearby
    dates (matches can be rescheduled)."""
    p1c = canonical_player(bet_p1)
    p2c = canonical_player(bet_p2)
    keys = [f"{p1c}||{p2c}", f"{p2c}||{p1c}"]
    candidate_dates = [bet_date] + [d for d in nearby_dates if d != bet_date]
    unresolved: dict | None = None
    # exact key first, but prefer a resolved/walkover row over an empty placeholder
    for d in candidate_dates:
        bucket = cache.get(d) or {}
        for k in keys:
            if k in bucket:
                hit = bucket[k]
                if hit.get("winner_canonical") or hit.get("walkover"):
                    return hit
                unresolved = unresolved or hit
    # fuzzy fallback: scan all entries on those dates, same resolved-first rule
    for d in candidate_dates:
        bucket = cache.get(d) or {}
        for k, v in bucket.items():
            try:
                kp1, kp2 = k.split("||", 1)
            except ValueError:
                continue
            ok_a = names_match(p1c, kp1) and names_match(p2c, kp2)
            ok_b = names_match(p1c, kp2) and names_match(p2c, kp1)
            if ok_a or ok_b:
                if v.get("winner_canonical") or v.get("walkover"):
                    return v
                unresolved = unresolved or v
    return unresolved


# ---------------------------------------------------------------------------
# Main resolution flow
# ---------------------------------------------------------------------------


class ResultsScraper:
    def __init__(self, db_path: str = DB_PATH_DEFAULT, window_days: int = WINDOW_DAYS_DEFAULT):
        self.db_path = db_path
        self.window_days = int(window_days)

    # legacy-compatible API
    async def update_pending_bets(self, fast_mode: bool = True):
        """Resolve pending bets. Returns count of newly settled bets.

        fast_mode kept for backward compatibility but is now a no-op:
        the new implementation always uses the configured 7-day window with
        cache + Sackmann fallback.
        """
        return await self._run()

    async def _run(self) -> int:
        conn = sqlite3.connect(self.db_path)
        updated = 0
        try:
            ensure_user_bets_schema(conn)
            ensure_match_results_cache(conn)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, date, match_name, bet_on, odds, stake, match_date
                FROM user_bets WHERE status = 'En cours'
                """
            )
            pending = cur.fetchall()
            if not pending:
                LOGGER.info("No pending bets to resolve")
            else:
                today = datetime.now().date()
                cutoff = today - timedelta(days=self.window_days)
                lookup_upper = today + timedelta(days=LOOKUP_UPPER_EXTRA_DAYS)
                target_dates: set[str] = set()
                for row in pending:
                    placement_date = row[1]
                    raw_sched = row[6]
                    resolve_date = normalize_schedule_date(raw_sched) or placement_date
                    try:
                        d = datetime.strptime(str(resolve_date), "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if d < cutoff or d > today:
                        continue
                    target_dates.add(d.isoformat())
                    for off in NEARBY_DAY_OFFSETS:
                        adj = d + timedelta(days=off)
                        if cutoff <= adj <= lookup_upper:
                            target_dates.add(adj.isoformat())

                if not target_dates:
                    LOGGER.info(
                        "Pending bets all outside window (%dd) — nothing to do",
                        self.window_days,
                    )
                else:
                    # 1) load cache
                    cache = read_cached_results(conn, target_dates)

                    # 2) determine which dates we still need to scrape on Tennis Explorer
                    dates_needing_te: set[str] = set()
                    pending_dates: set[str] = set()
                    for row in pending:
                        placement_date = row[1]
                        raw_sched = row[6]
                        eff = normalize_schedule_date(raw_sched) or placement_date
                        if isinstance(eff, str) and eff in target_dates:
                            pending_dates.add(eff)
                    for d_iso in sorted(target_dates):
                        bucket = cache.get(d_iso) or {}
                        try:
                            d_obj = datetime.strptime(d_iso, "%Y-%m-%d").date()
                        except ValueError:
                            continue
                        # Tennis Explorer buckets can be +1/+2 calendar days vs the booked date;
                        # keep refreshing future buckets in expanded range until they become past days.
                        if d_obj > today:
                            dates_needing_te.add(d_iso)
                            continue
                        # always re-scrape today (live data); skip past days that already
                        # have a tennisexplorer entry in cache
                        if d_iso == today.isoformat():
                            dates_needing_te.add(d_iso)
                            continue
                        # If there are still pending bets on that date, force a fresh
                        # TE pass to avoid stale cache locking unresolved bets forever.
                        if d_iso in pending_dates:
                            dates_needing_te.add(d_iso)
                            continue
                        has_te_entry = any(
                            v.get("source") == "tennisexplorer" for v in bucket.values()
                        )
                        if not has_te_entry:
                            dates_needing_te.add(d_iso)

                    dates_te_list = sorted(dates_needing_te)
                    te_results: dict[str, list[dict]] = {}
                    if dates_te_list:
                        try:
                            te_results = await _scrape_te_dates(dates_te_list)
                            n_te = _store_te_results_in_cache(conn, te_results)
                            LOGGER.info(
                                "Tennis Explorer: cached %d entries across %d dates",
                                n_te,
                                len(dates_te_list),
                            )
                        except Exception as exc:
                            LOGGER.error("Tennis Explorer scraping failed entirely: %s", exc)

                    # 3) Sackmann fallback (always run for window — adds redundancy &
                    #    catches WTA mismatches). Lightweight: cached CSV per tour/year.
                    try:
                        sackmann_rows = load_sackmann_results(target_dates)
                        if sackmann_rows:
                            n_sk = _store_sackmann_results_in_cache(conn, sackmann_rows)
                            LOGGER.info("Sackmann: cached %d entries", n_sk)
                    except Exception as exc:
                        LOGGER.warning("Sackmann fallback failed: %s", exc)

                    # 4) Re-read cache (now potentially enriched)
                    cache = read_cached_results(conn, target_dates)

                    # 5) Resolve pending bets
                    for bet_id, placement_date, match_name, bet_on, odds, stake, raw_sched in pending:
                        resolve_date = normalize_schedule_date(raw_sched) or placement_date
                        try:
                            bd = datetime.strptime(str(resolve_date), "%Y-%m-%d").date()
                        except Exception:
                            continue
                        if bd < cutoff or bd > today:
                            continue

                        parts = (match_name or "").split(" vs ")
                        if len(parts) != 2:
                            LOGGER.warning(
                                "Bet %s has invalid match_name format: %r", bet_id, match_name
                            )
                            continue
                        p1_raw, p2_raw = parts[0], parts[1]
                        # search NEARBY_DAY_OFFSETS on [cutoff, lookup_upper] for rescheduling / TE buckets
                        nearby = [
                            (bd + timedelta(days=k)).isoformat()
                            for k in NEARBY_DAY_OFFSETS
                            if cutoff <= bd + timedelta(days=k) <= lookup_upper
                        ]
                        hit = _lookup_in_cache(
                            cache,
                            bet_date=str(resolve_date),
                            bet_p1=p1_raw,
                            bet_p2=p2_raw,
                            nearby_dates=nearby,
                        )
                        if not hit:
                            continue

                        # Walkover or cancellation -> stake refunded
                        if hit.get("walkover"):
                            settle_bet(
                                conn,
                                bet_id=bet_id,
                                status="Annulé",
                                profit=0.0,
                                score_final=hit.get("score"),
                                result_source=hit.get("source"),
                            )
                            LOGGER.info(
                                "Bet %s annulé (walkover) source=%s", bet_id, hit.get("source")
                            )
                            updated += 1
                            continue

                        winner = hit.get("winner_canonical")
                        if not winner:
                            # match in cache but no winner yet (live or unknown)
                            continue

                        bet_on_canon = canonical_player(bet_on)
                        won = names_match(bet_on_canon, winner)
                        score_text = hit.get("score") or ""
                        retired_eff = match_result_effective_retired(
                            retired=bool(hit.get("retired")),
                            walkover=bool(hit.get("walkover")),
                            score=score_text,
                        )
                        if retired_eff and not won:
                            settle_bet(
                                conn,
                                bet_id=bet_id,
                                status="Annulé",
                                profit=0.0,
                                winner_resolved=winner,
                                score_final=hit.get("score"),
                                result_source=hit.get("source"),
                            )
                            LOGGER.info(
                                "Bet %s annulé (retired) source=%s",
                                bet_id,
                                hit.get("source"),
                            )
                            updated += 1
                            continue
                        if won:
                            profit = float(odds - 1.0) * float(stake)
                            settle_bet(
                                conn,
                                bet_id=bet_id,
                                status="Gagné",
                                profit=profit,
                                winner_resolved=winner,
                                score_final=hit.get("score"),
                                result_source=hit.get("source"),
                            )
                        else:
                            profit = -float(stake)
                            settle_bet(
                                conn,
                                bet_id=bet_id,
                                status="Perdu",
                                profit=profit,
                                winner_resolved=winner,
                                score_final=hit.get("score"),
                                result_source=hit.get("source"),
                            )
                        LOGGER.info(
                            "Bet %s settled %s (winner=%s, source=%s, retired=%s)",
                            bet_id,
                            "Gagné" if won else "Perdu",
                            winner,
                            hit.get("source"),
                            hit.get("retired"),
                        )
                        updated += 1

                    LOGGER.info("Resolution pass done — %d bets settled", updated)

            n_void = correct_retirement_voids_user_bets(conn)
            if n_void:
                LOGGER.info("Retirement void correction: %d user_bets fixed", n_void)
            return updated
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def main() -> int:
    from scripts.portfolio_sync_lock import acquire_scrape_lock, release_scrape_lock, scrape_in_progress

    if scrape_in_progress():
        LOGGER.info("CLI run skipped: another results scrape is in progress")
        return 2
    if not acquire_scrape_lock():
        LOGGER.info("CLI run skipped: scrape lock unavailable")
        return 2
    scraper = ResultsScraper()
    try:
        n = asyncio.run(scraper.update_pending_bets())
        LOGGER.info("CLI run finished: %d bets settled", n)
        try:
            import sqlite3

            from scripts.bets_db import (
                ensure_algo_opportunities_schema,
                ensure_user_bets_schema,
                sync_algo_opportunities_from_bets,
                sync_algo_opportunities_from_results,
            )

            conn = sqlite3.connect(scraper.db_path)
            try:
                ensure_user_bets_schema(conn)
                ensure_algo_opportunities_schema(conn)
                sync_algo_opportunities_from_bets(conn)
                sync_algo_opportunities_from_results(conn)
            finally:
                conn.close()
        except Exception as sync_exc:
            LOGGER.warning("Algo report sync after CLI run ignored: %s", sync_exc)
        return 0
    except Exception as exc:
        LOGGER.exception("CLI run crashed: %s", exc)
        return 1
    finally:
        release_scrape_lock()


if __name__ == "__main__":
    raise SystemExit(main())
