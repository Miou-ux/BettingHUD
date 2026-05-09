"""Inspection du modèle v3.2 (weighted Elo) sur quelques matchs clés.

Reproduit le pipeline minimal du dashboard pour des matchs précis afin
de pouvoir valider que :
    - le blending surface-Elo fonctionne (alpha proche de 1 pour les
      spécialistes, plus bas pour les jeunes)
    - le time decay est appliqué quand pertinent
    - les caps cap_surface_specialist ne sont plus déclenchés
    - les probabilités sont raisonnables
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


def _print_match(label: str, surface: str, tour: str, p1: dict, p2: dict, tournament: str, level: str | None = None) -> None:
    print("=" * 88)
    print(f"  {label}")
    print(f"  Tournoi: {tournament} ({tour}, {surface}{', ' + level if level else ''})")
    print("=" * 88)

    ml = TennisMLModel()
    stats = TennisStatsEngine()

    def _resolve_meta(name: str, hint: str):
        meta = stats.get_player_id_meta(name, tour_hint=hint)
        return meta.get("player_id"), meta.get("source", "unresolved")

    p1_id, p1_src = _resolve_meta(p1["name"], tour)
    p2_id, p2_src = _resolve_meta(p2["name"], tour)
    print(f"  P1 {p1['name']}: id={p1_id} ({p1_src})")
    print(f"  P2 {p2['name']}: id={p2_id} ({p2_src})")

    pred = ml.predict_match(
        surface=surface,
        p1_rank=p1["rank"],
        p2_rank=p2["rank"],
        p1_age=p1["age"],
        p2_age=p2["age"],
        p1_ht=p1.get("ht", 185),
        p2_ht=p2.get("ht", 185),
        p1_pts=p1.get("pts", 0),
        p2_pts=p2.get("pts", 0),
        p1_id=p1_id,
        p2_id=p2_id,
        p1_name=p1["name"],
        p2_name=p2["name"],
        p1_hand=p1.get("hand", "R"),
        p2_hand=p2.get("hand", "R"),
        tournament_name=tournament,
        tournament_level=level,
        tour=tour,
    )

    snap = pred["feature_snapshot"]
    print(f"\n  Prédiction P1: {pred['p1_win_prob']*100:5.2f}%  →  cote {pred['p1_true_odd']:.2f}")
    print(f"  Prédiction P2: {pred['p2_win_prob']*100:5.2f}%  →  cote {pred['p2_true_odd']:.2f}")
    print(f"  Calibration : {pred['calibration_used']}")
    print(f"  Caps actifs : {snap.get('caps_applied') or 'aucun'}")
    print(f"  Raw p1_prob : {snap.get('raw_p1_prob', 0)*100:.2f}%  →  capped {snap.get('capped_p1_prob', 0)*100:.2f}%")
    print()
    print(f"  Elo global   : P1={snap['p1_global_elo']:6.1f}  | P2={snap['p2_global_elo']:6.1f}  | diff={snap['p1_global_elo']-snap['p2_global_elo']:+6.1f}")
    print(f"  Elo surface (effective): P1={snap['p1_surface_elo']:6.1f} | P2={snap['p2_surface_elo']:6.1f} | diff={snap['p1_surface_elo']-snap['p2_surface_elo']:+6.1f}")
    print(f"  Elo surface (raw)      : P1={snap['p1_surface_elo_raw']:6.1f} | P2={snap['p2_surface_elo_raw']:6.1f}")
    print(f"  Surface match counts   : P1={snap['p1_surface_match_count']:4d}    | P2={snap['p2_surface_match_count']:4d}")
    print(f"  Blend alpha            : P1={snap['p1_surface_blend_alpha']:.2%}  | P2={snap['p2_surface_blend_alpha']:.2%}")
    print(f"  Last seen              : P1={snap['p1_last_seen']} | P2={snap['p2_last_seen']}")
    print()
    print(f"  Top features (locales): ")
    for tf in pred.get("top_features", []):
        print(f"    - {tf['feature']:<32s}  value={tf['value']:+8.3f}  score={tf['score']:.4f}")
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--match", type=str, default=None,
                        help="Optional single match name like 'Sonego L. vs Buse I.'")
    args = parser.parse_args()

    if args.match and "Sonego" in args.match:
        _print_match(
            "Sonego L. vs Buse I. (Rome 1000)",
            surface="Clay",
            tour="ATP",
            p1={"name": "Sonego L.", "rank": 66, "age": 31, "ht": 191, "pts": 810, "hand": "R"},
            p2={"name": "Buse I.", "rank": 60, "age": 22, "ht": 183, "pts": 864, "hand": "R"},
            tournament="Rome",
            level="M",
        )
        sys.exit(0)

    if args.match and "Garin" in args.match:
        _print_match(
            "Garin C. vs Cerundolo J. (Rome 1000)",
            surface="Clay",
            tour="ATP",
            p1={"name": "Garin C.", "rank": 109, "age": 30, "ht": 184, "pts": 599, "hand": "R"},
            p2={"name": "Cerundolo J.", "rank": 69, "age": 25, "ht": 175, "pts": 803, "hand": "R"},
            tournament="Rome",
            level="M",
        )
        sys.exit(0)

    # Match 1: Tsitsipas S. vs Machac T. (Rome ATP, Clay) — cas qui posait problème
    _print_match(
        "Tsitsipas S. vs Machac T. (Rome 1000)",
        surface="Clay",
        tour="ATP",
        p1={"name": "Tsitsipas S.", "rank": 26, "age": 27, "ht": 193, "pts": 1310, "hand": "R"},
        p2={"name": "Machac T.", "rank": 30, "age": 25, "ht": 188, "pts": 1180, "hand": "R"},
        tournament="Rome",
        level="M",
    )

    # Match 2: Maria T. vs Cirstea S. (Rome WTA, Clay) — cas du "value mathématique"
    _print_match(
        "Maria T. vs Cirstea S. (Rome WTA 1000)",
        surface="Clay",
        tour="WTA",
        p1={"name": "Maria T.", "rank": 86, "age": 37, "ht": 175, "pts": 760, "hand": "R"},
        p2={"name": "Cirstea S.", "rank": 26, "age": 35, "ht": 178, "pts": 1340, "hand": "R"},
        tournament="Rome WTA",
        level="M",
    )

    # Match 3: Sabalenka A. vs Krejcikova B. (Rome WTA, Clay)
    _print_match(
        "Sabalenka A. vs Krejcikova B. (Rome WTA 1000)",
        surface="Clay",
        tour="WTA",
        p1={"name": "Sabalenka A.", "rank": 1, "age": 27, "ht": 182, "pts": 9706, "hand": "R"},
        p2={"name": "Krejcikova B.", "rank": 16, "age": 30, "ht": 178, "pts": 2390, "hand": "R"},
        tournament="Rome WTA",
        level="M",
    )

    # Match 4: Sinner J. vs Alcaraz C. (hypothétique, pour vérifier l'ATP haut niveau)
    _print_match(
        "Sinner J. vs Alcaraz C. (Rome 1000, hypothétique)",
        surface="Clay",
        tour="ATP",
        p1={"name": "Sinner J.", "rank": 1, "age": 24, "ht": 188, "pts": 11500, "hand": "R"},
        p2={"name": "Alcaraz C.", "rank": 2, "age": 22, "ht": 183, "pts": 11300, "hand": "R"},
        tournament="Rome",
        level="M",
    )
