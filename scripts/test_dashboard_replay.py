"""Replay exactement le pipeline du dashboard pour un match donné.

Charge stats_engine + ml_model, calcule form/fatigue/h2h/match_quality
comme le dashboard, puis appelle predict_match avec TOUS les paramètres.

Aide à debugger un écart entre le test simple et la prédiction du dashboard.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from scripts.ml_model import TennisMLModel  # noqa: E402
from scripts.stats_engine import TennisStatsEngine  # noqa: E402


def replay(p1_name: str, p2_name: str, *, surface: str, tour: str, tournament: str, level: str | None,
           p1_rank: float, p2_rank: float, p1_age: float, p2_age: float,
           p1_ht: float, p2_ht: float, p1_pts: float, p2_pts: float,
           p1_hand: str = "R", p2_hand: str = "R") -> None:

    stats = TennisStatsEngine()
    ml = TennisMLModel()

    p1_meta = stats.get_player_id_meta(p1_name, tour_hint=tour)
    p2_meta = stats.get_player_id_meta(p2_name, tour_hint=tour)
    p1_id = p1_meta.get("player_id")
    p2_id = p2_meta.get("player_id")

    print("=" * 88)
    print(f"  DASHBOARD REPLAY — {p1_name} vs {p2_name} ({tournament}, {tour}, {surface})")
    print("=" * 88)
    print(f"  P1 {p1_name}: id={p1_id}  meta={p1_meta}")
    print(f"  P2 {p2_name}: id={p2_id}  meta={p2_meta}")

    # Stats classiques (form, fatigue, h2h)
    p1_form = stats.get_recent_form(p1_id, tour_hint=tour) if p1_id else {"win_pct": 50}
    p2_form = stats.get_recent_form(p2_id, tour_hint=tour) if p2_id else {"win_pct": 50}
    p1_fatigue = stats.get_recent_fatigue(p1_id, tour_hint=tour) if p1_id else {"minutes_played": 0, "matches": 0}
    p2_fatigue = stats.get_recent_fatigue(p2_id, tour_hint=tour) if p2_id else {"minutes_played": 0, "matches": 0}
    h2h = stats.get_h2h(p1_id, p2_id, tour_hint=tour) if p1_id and p2_id else {"p1_wins": 0, "p2_wins": 0}

    # Match quality (causal features)
    p1_mq = stats.get_recent_match_quality(p1_id, tour_hint=tour) if p1_id else {}
    p2_mq = stats.get_recent_match_quality(p2_id, tour_hint=tour) if p2_id else {}

    print(f"\n  P1 form90: {p1_form}")
    print(f"  P2 form90: {p2_form}")
    print(f"  P1 fatigue14: {p1_fatigue}")
    print(f"  P2 fatigue14: {p2_fatigue}")
    print(f"  H2H: {h2h}")
    print(f"  P1 match_quality: {p1_mq}")
    print(f"  P2 match_quality: {p2_mq}")

    pred = ml.predict_match(
        surface=surface,
        p1_rank=p1_rank, p2_rank=p2_rank,
        p1_age=p1_age, p2_age=p2_age,
        p1_ht=p1_ht, p2_ht=p2_ht,
        p1_pts=p1_pts, p2_pts=p2_pts,
        p1_id=p1_id, p2_id=p2_id,
        p1_name=p1_name, p2_name=p2_name,
        p1_form_win_pct_90=p1_form.get("win_pct", 50),
        p2_form_win_pct_90=p2_form.get("win_pct", 50),
        p1_fatigue_minutes_14=p1_fatigue.get("minutes_played", 0),
        p2_fatigue_minutes_14=p2_fatigue.get("minutes_played", 0),
        p1_fatigue_matches_14=p1_fatigue.get("matches", 0),
        p2_fatigue_matches_14=p2_fatigue.get("matches", 0),
        p1_hand=p1_hand, p2_hand=p2_hand,
        h2h_p1_wins=h2h.get("p1_wins", 0),
        h2h_p2_wins=h2h.get("p2_wins", 0),
        p1_wins_last7d=p1_mq.get("wins_last7d", 0),
        p2_wins_last7d=p2_mq.get("wins_last7d", 0),
        p1_three_setters_last14d=p1_mq.get("three_setters_last14d", 0),
        p2_three_setters_last14d=p2_mq.get("three_setters_last14d", 0),
        p1_last_round_reached=p1_mq.get("last_round_reached", 0),
        p2_last_round_reached=p2_mq.get("last_round_reached", 0),
        tournament_name=tournament,
        tournament_level=level,
        tour=tour,
    )
    snap = pred["feature_snapshot"]
    print(f"\n  Prédiction P1 ({p1_name}): {pred['p1_win_prob']*100:5.2f}%  →  cote {pred['p1_true_odd']:.2f}")
    print(f"  Prédiction P2 ({p2_name}): {pred['p2_win_prob']*100:5.2f}%  →  cote {pred['p2_true_odd']:.2f}")
    print(f"  Calibration : {pred['calibration_used']}")
    print(f"  Caps actifs : {snap.get('caps_applied') or 'aucun'}")
    print(f"  Raw → Capped: {snap.get('raw_p1_prob', 0)*100:.2f}% → {snap.get('capped_p1_prob', 0)*100:.2f}%")
    print()
    print(f"  Elo global   : P1={snap['p1_global_elo']:6.1f}  | P2={snap['p2_global_elo']:6.1f}  | diff={snap['p1_global_elo']-snap['p2_global_elo']:+6.1f}")
    print(f"  Surface eff. : P1={snap['p1_surface_elo']:6.1f}  | P2={snap['p2_surface_elo']:6.1f}  | diff={snap['p1_surface_elo']-snap['p2_surface_elo']:+6.1f}")
    print(f"  Surface raw  : P1={snap['p1_surface_elo_raw']:6.1f}  | P2={snap['p2_surface_elo_raw']:6.1f}")
    print(f"  Match counts : P1={snap['p1_surface_match_count']:4d}     | P2={snap['p2_surface_match_count']:4d}")
    print(f"  Blend alpha  : P1={snap['p1_surface_blend_alpha']:.2%}  | P2={snap['p2_surface_blend_alpha']:.2%}")
    print(f"  Last seen    : P1={snap['p1_last_seen']} | P2={snap['p2_last_seen']}")
    print()
    print(f"  Top features (locales):")
    for tf in pred.get("top_features", []):
        print(f"    - {tf['feature']:<32s}  value={tf['value']:+10.3f}  score={tf['score']:.4f}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--match", default="garin")
    args = parser.parse_args()

    if "garin_no_level" in args.match.lower():
        replay(
            "Garin C.", "Cerundolo J.",
            surface="Clay", tour="ATP", tournament="Rome", level=None,
            p1_rank=109, p2_rank=69,
            p1_age=30, p2_age=25,
            p1_ht=184, p2_ht=175,
            p1_pts=599, p2_pts=803,
        )
    elif "giron" in args.match.lower():
        replay(
            "Giron M.", "Cilic M.",
            surface="Clay", tour="ATP", tournament="Rome", level="M",
            p1_rank=70, p2_rank=48,
            p1_age=32.65, p2_age=37.54,
            p1_ht=180, p2_ht=198,
            p1_pts=790, p2_pts=950,
            p1_hand="R", p2_hand="R",
        )
    else:
        replay(
            "Garin C.", "Cerundolo J.",
            surface="Clay", tour="ATP", tournament="Rome", level="M",
            p1_rank=109, p2_rank=69,
            p1_age=30, p2_age=25,
            p1_ht=184, p2_ht=175,
            p1_pts=599, p2_pts=803,
        )
