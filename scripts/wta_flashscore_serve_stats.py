"""Flashscore WTA serve-stats fetch + Sackmann mapping (preprod delta).

Probe summary (2026-06-17):
- Tennis Explorer WTA match-detail: no serve-stat tables (H2H only).
- Flashscore ``global.flashscore.ninja`` feeds ``f_2_{offset}_3_en_1`` (discovery)
  and ``df_st_1_{match_id}`` (stats) work with ``x-fsign: SW9D1eZo``.
- tennisdata.app / SofaScore: bot-gate or no bulk match stats without auth.
- tennisabstract.com: no per-match integer serve totals for automation.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timezone
from typing import Callable

import requests
from rapidfuzz import fuzz

from scripts.wta_sackmann_common import norm_name_key

FS_FEED_BASE = "https://global.flashscore.ninja/2/x/feed"
FS_PLAYER_BASE = "https://www.flashscore.com/player"
FS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/121.0.0.0 Safari/537.36",
    "x-fsign": "SW9D1eZo",
}

_STAT_MAP = {
    "aces": "ace",
    "double faults": "df",
    "1st serve percentage": "first_in_pct",
    "1st serve points won": "first_won",
    "2nd serve points won": "second_won",
    "break points saved": "bp_saved",
    "service games won": "sv_gms",
    "service points won": "sv_won_total",
}


def _int_from_cell(text: str) -> int | None:
    text = str(text or "").strip()
    m = re.match(r"^(\d+)", text)
    return int(m.group(1)) if m else None


def _pct_from_cell(text: str) -> float | None:
    m = re.search(r"(\d+)%", str(text or ""))
    return float(m.group(1)) / 100.0 if m else None


def _won_pair(text: str) -> tuple[int | None, int | None]:
    m = re.search(r"\((\d+)/(\d+)\)", str(text or ""))
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _bp_pair(text: str) -> tuple[int | None, int | None]:
    m = re.match(r"^(\d+)/(\d+)$", str(text or "").strip())
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _game_date_int(game: dict) -> int | None:
    try:
        ts = int(game.get("AD") or 0)
        if ts <= 0:
            return None
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        return int(d.strftime("%Y%m%d"))
    except (TypeError, ValueError, OSError):
        return None


def parse_fs_games(feed_text: str) -> list[dict]:
    games: list[dict] = []
    for seg in feed_text.split("~AA"):
        if not seg:
            continue
        parts = seg.split("¬")
        g: dict = {}
        if parts and parts[0].startswith("÷"):
            g["AA"] = parts[0][1:]
        for part in parts:
            if "÷" in part:
                k, v = part.split("÷", 1)
                if k:
                    g[k] = v
        if g.get("AE") or g.get("AF"):
            games.append(g)
    return games


def _name_score(winner: str, loser: str, home: str, away: str) -> float:
    """Pair score; each player must match their FS counterpart (not just one surname)."""
    try:
        from scripts.wta_name_aliases import canonicalize_wta_display_name

        winner = canonicalize_wta_display_name(winner)
        loser = canonicalize_wta_display_name(loser)
    except Exception:
        pass
    wk, lk = norm_name_key(winner), norm_name_key(loser)
    hk, ak = norm_name_key(home), norm_name_key(away)

    def _orient(hw: str, aw: str) -> float:
        wp = fuzz.partial_ratio(wk, hw)
        lp = fuzz.partial_ratio(lk, aw)
        if wp < 70 or lp < 70:
            return 0.0
        blend = (wp + lp) / 2.0
        tok = fuzz.token_set_ratio(f"{wk} {lk}", f"{hw} {aw}")
        return float((blend + tok) / 2.0)

    return max(_orient(hk, ak), _orient(ak, hk))


def _index_player_slug(game: dict, slugs: dict[str, tuple[str, str]]) -> None:
    for name_k, slug_k, id_k in (
        ("AE", "WU", "PX"),
        ("AF", "WV", "PY"),
    ):
        name = str(game.get(name_k) or "").strip()
        slug = str(game.get(slug_k) or "").strip()
        pid = str(game.get(id_k) or "").strip()
        if not name or not slug or not pid or "/" in name:
            continue
        nk = norm_name_key(name)
        slugs[nk] = (slug, pid)


class FlashscoreIndex:
  """Caches daily WTA feeds + optional player-page history."""

  def __init__(self, session: requests.Session | None = None) -> None:
      self.session = session or requests.Session()
      self.games: dict[str, dict] = {}
      self.slugs: dict[str, tuple[str, str]] = {}
      self._loaded_offsets: set[int] = set()
      self._loaded_players: set[str] = set()

  def load_offsets(
      self,
      offsets: range | list[int],
      *,
      delay_s: float = 0.35,
  ) -> None:
      for off in offsets:
          if off in self._loaded_offsets:
              continue
          url = f"{FS_FEED_BASE}/f_2_{off}_3_en_1"
          try:
              r = self.session.get(url, headers=FS_HEADERS, timeout=30)
              r.raise_for_status()
              for g in parse_fs_games(r.text if len(r.text) > 10 else ""):
                  mid = g.get("AA")
                  if mid:
                      self.games[mid] = g
                  _index_player_slug(g, self.slugs)
          except Exception:
              pass
          self._loaded_offsets.add(off)
          time.sleep(delay_s)

  def warm_default(self, *, today: date | None = None, delay_s: float = 0.35) -> None:
      today = today or date.today()
      # Rolling window: ~3 months of WTA singles results.
      self.load_offsets(range(-90, 8), delay_s=delay_s)

  def _player_history(self, slug: str, pid: str) -> list[dict]:
      key = f"{slug}:{pid}"
      if key in self._loaded_players:
          return []
      self._loaded_players.add(key)
      url = f"{FS_PLAYER_BASE}/{slug}/{pid}/"
      try:
          r = self.session.get(url, headers=FS_HEADERS, timeout=30)
          r.raise_for_status()
          games = parse_fs_games(r.text)
          for g in games:
              mid = g.get("AA")
              if mid:
                  self.games[mid] = g
              _index_player_slug(g, self.slugs)
          return games
      except Exception:
          return []

  def _rank_game(self, g: dict, winner: str, loser: str, tourney_date: int) -> float:
      home = str(g.get("AE") or "")
      away = str(g.get("AF") or "")
      if not home or not away or "/" in home or "/" in away:
          return -1.0
      ns = _name_score(winner, loser, home, away)
      if ns < 72:
          return -1.0
      gd = _game_date_int(g)
      date_bonus = 0.0
      if gd is not None:
          diff = abs(gd - tourney_date)
          if diff == 0:
              date_bonus = 20.0
          elif diff <= 2:
              date_bonus = 8.0
          else:
              date_bonus = -min(diff / 7.0, 14.0)
      return ns + date_bonus

  def _best_game(self, winner: str, loser: str, tourney_date: int) -> dict | None:
      best: dict | None = None
      best_rank = -1.0
      for g in self.games.values():
          rank = self._rank_game(g, winner, loser, tourney_date)
          if rank > best_rank:
              best_rank = rank
              best = g
      return best if best_rank >= 70 else None

  def _warm_players(self, winner: str, loser: str) -> None:
      for name in (winner, loser):
          nk = norm_name_key(name)
          slug_pid = self.slugs.get(nk)
          if not slug_pid:
              continue
          self._player_history(slug_pid[0], slug_pid[1])

  def find_game(self, winner: str, loser: str, tourney_date: int) -> dict | None:
      best = self._best_game(winner, loser, tourney_date)
      if best is not None:
          gd = _game_date_int(best)
          if gd == tourney_date:
              return best

      self._warm_players(winner, loser)
      return self._best_game(winner, loser, tourney_date)


def parse_df_st_match_service(feed_text: str) -> dict[str, tuple[str, str]]:
    tokens = feed_text.split("¬")
    in_match = False
    in_service = False
    in_games = False
    out: dict[str, tuple[str, str]] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "SE÷Match":
            in_match = True
            in_service = False
            in_games = False
        elif tok.startswith("~SE÷") and in_match:
            break
        elif in_match and tok.startswith("~SF÷"):
            grp = tok[4:]
            in_service = grp == "Service"
            in_games = grp == "Games"
        elif in_match and (in_service or in_games) and tok.startswith("~SG÷"):
            label = tok[4:]
            home = away = ""
            if i + 1 < len(tokens) and tokens[i + 1].startswith("SH÷"):
                home = tokens[i + 1][3:]
                i += 1
            if i + 1 < len(tokens) and tokens[i + 1].startswith("SI÷"):
                away = tokens[i + 1][3:]
                i += 1
            out[label.lower()] = (home, away)
        i += 1
    return out


def fetch_match_service_stats(
    match_id: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, tuple[str, str]] | None:
    sess = session or requests.Session()
    url = f"{FS_FEED_BASE}/df_st_1_{match_id}"
    r = sess.get(url, headers=FS_HEADERS, timeout=30)
    if r.status_code != 200 or len(r.text) < 80:
        return None
    stats = parse_df_st_match_service(r.text)
    return stats or None


def _pack_side(raw: dict[str, str], prefix: str) -> dict:
    ace = _int_from_cell(raw.get("ace", ""))
    df = _int_from_cell(raw.get("df", ""))
    sv_gms = _int_from_cell(raw.get("sv_gms", ""))
    first_won_n, first_total = _won_pair(raw.get("first_won", ""))
    second_won_n, second_total = _won_pair(raw.get("second_won", ""))
    bp_saved, bp_faced = _bp_pair(raw.get("bp_saved", ""))
    first_in_pct = _pct_from_cell(raw.get("first_in_pct", ""))

    first_won = first_won_n
    second_won = second_won_n
    if first_total and second_total:
        svpt = first_total + second_total
    elif first_won is not None and second_won is not None:
        svpt = first_won + second_won
    else:
        svpt = _int_from_cell(raw.get("sv_won_total", ""))

    first_in = None
    if first_total:
        first_in = first_total
    elif svpt and first_in_pct is not None:
        first_in = max(1, int(round(svpt * first_in_pct)))

    return {
        f"{prefix}_ace": ace,
        f"{prefix}_df": df,
        f"{prefix}_svpt": svpt,
        f"{prefix}_1stIn": first_in,
        f"{prefix}_1stWon": first_won,
        f"{prefix}_2ndWon": second_won,
        f"{prefix}_SvGms": sv_gms,
        f"{prefix}_bpSaved": bp_saved,
        f"{prefix}_bpFaced": bp_faced,
    }


def _raw_from_fs(service: dict[str, tuple[str, str]], home: bool) -> dict[str, str]:
    idx = 0 if home else 1
    raw: dict[str, str] = {}
    for frag, key in _STAT_MAP.items():
        for label, pair in service.items():
            if frag in label:
                raw[key] = pair[idx]
                break
    sg = raw.get("sv_gms", "")
    m = re.search(r"\((\d+)/(\d+)\)", sg)
    if m:
        raw["sv_gms"] = m.group(2)
    return raw


def map_service_to_sackmann(
    service: dict[str, tuple[str, str]],
    *,
    winner_name: str,
    loser_name: str,
    home_name: str,
    away_name: str,
) -> dict | None:
    wk = norm_name_key(winner_name)
    hk, ak = norm_name_key(home_name), norm_name_key(away_name)
    if fuzz.partial_ratio(wk, hk) >= fuzz.partial_ratio(wk, ak):
        w_raw = _raw_from_fs(service, home=True)
        l_raw = _raw_from_fs(service, home=False)
    else:
        w_raw = _raw_from_fs(service, home=False)
        l_raw = _raw_from_fs(service, home=True)

    stats: dict = {}
    stats.update(_pack_side(w_raw, "w"))
    stats.update(_pack_side(l_raw, "l"))
    if stats.get("w_svpt") is None or stats.get("l_svpt") is None:
        return None
    return stats


def enrich_batch(
    rows: list[dict],
    *,
    delay_s: float = 1.75,
    feed_delay_s: float = 0.35,
    on_progress: Callable[[int, int, dict], None] | None = None,
) -> tuple[dict[tuple, dict], dict]:
    session = requests.Session()
    index = FlashscoreIndex(session)
    index.warm_default(delay_s=feed_delay_s)

    updates: dict[tuple, dict] = {}
    failed = 0
    enriched = 0

    for i, row in enumerate(rows):
        td = int(row["tourney_date"])
        w, l = row["winner_name"], row["loser_name"]
        game = index.find_game(w, l, td)
        stats = None
        if game and game.get("AA"):
            service = fetch_match_service_stats(str(game["AA"]), session=session)
            if service:
                stats = map_service_to_sackmann(
                    service,
                    winner_name=w,
                    loser_name=l,
                    home_name=str(game.get("AE") or ""),
                    away_name=str(game.get("AF") or ""),
                )
        if not stats:
            index._warm_players(w, l)
            game = index.find_game(w, l, td)
            if game and game.get("AA"):
                service = fetch_match_service_stats(str(game["AA"]), session=session)
                if service:
                    stats = map_service_to_sackmann(
                        service,
                        winner_name=w,
                        loser_name=l,
                        home_name=str(game.get("AE") or ""),
                        away_name=str(game.get("AF") or ""),
                    )
        if stats:
            updates[row["dedup_key"]] = stats
            enriched += 1
        else:
            failed += 1
        if on_progress:
            on_progress(i + 1, len(rows), row)
        if i + 1 < len(rows):
            time.sleep(delay_s)

    return updates, {"enriched": enriched, "failed": failed, "updated_keys": len(updates)}


def enrich_match_serve_stats(
    winner: str,
    loser: str,
    tourney_date: int,
    *,
    index: FlashscoreIndex | None = None,
) -> dict | None:
    """Single-match helper (loads index if not provided)."""
    idx = index or FlashscoreIndex()
    if index is None:
        idx.warm_default()
    game = idx.find_game(winner, loser, tourney_date)
    if not game or not game.get("AA"):
        return None
    service = fetch_match_service_stats(str(game["AA"]), session=idx.session)
    if not service:
        return None
    return map_service_to_sackmann(
        service,
        winner_name=winner,
        loser_name=loser,
        home_name=str(game.get("AE") or ""),
        away_name=str(game.get("AF") or ""),
    )
