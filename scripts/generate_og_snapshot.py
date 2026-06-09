#!/usr/bin/env python3
"""Génère og-1-day-1-pick.png (stats) pour partages sociaux / Open Graph."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

OUT_REL = Path("CourtAlpha/frontend/public/og-1-day-1-pick.png")


def _courtalpha_public_dir() -> Path:
    raw = os.getenv("COURTALPHA_ROOT", "").strip()
    if raw:
        return Path(raw) / "frontend" / "public"
    local = Path(ROOT).parent / "CourtAlpha" / "frontend" / "public"
    if local.is_dir():
        return local
    return Path("/opt/courtalpha/frontend/public")


def generate_og_image(*, db_path: str, out_path: Path) -> dict:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("pip install pillow") from exc

    ca = _courtalpha_public_dir().parents[1]
    if str(ca) not in sys.path:
        sys.path.insert(0, str(ca))
    from api.services.one_day_one_pick import build_one_day_one_pick_replay

    replay = build_one_day_one_pick_replay(db_path=db_path)
    summary = replay.get("summary") or {}
    hit = float(summary.get("hit_pct") or 0)
    br = float(summary.get("bankroll_final_eur") or 100)
    n = int(summary.get("n_picks") or 0)
    pl = float(summary.get("net_profit_eur") or 0)

    w, h = 1200, 630
    img = Image.new("RGB", (w, h), color=(12, 18, 24))
    draw = ImageDraw.Draw(img)
    try:
        font_l = ImageFont.truetype("arial.ttf", 52)
        font_m = ImageFont.truetype("arial.ttf", 36)
        font_s = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font_l = font_m = font_s = ImageFont.load_default()

    draw.text((60, 50), "CourtAlpha — 1 Day 1 Pick", fill=(0, 212, 170), font=font_l)
    draw.text((60, 140), "Track record public tennis ATP/WTA", fill=(200, 210, 220), font=font_m)
    stats = f"Picks {n}  ·  Hit {hit:.1f}%  ·  BR {br:.0f} €  ·  P/L {pl:+.0f} €"
    draw.text((60, 220), stats, fill=(255, 255, 255), font=font_m)
    draw.text((60, 320), "courtalpha.tech/1-day-1-pick", fill=(120, 140, 160), font=font_s)
    draw.text((60, 520), "Info statistique — pas un conseil financier · 18+", fill=(100, 110, 120), font=font_s)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)
    return {"ok": True, "path": str(out_path), "n_picks": n, "hit_pct": hit}


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ROOT, ".env"))
    except ImportError:
        pass

    from scripts.bets_db import DB_PATH_DEFAULT

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="Chemin PNG de sortie")
    args = ap.parse_args()
    db_path = os.path.join(ROOT, DB_PATH_DEFAULT) if not os.path.isabs(DB_PATH_DEFAULT) else DB_PATH_DEFAULT
    out = Path(args.out) if args.out else _courtalpha_public_dir() / "og-1-day-1-pick.png"
    result = generate_og_image(db_path=db_path, out_path=out)
    print(result)


if __name__ == "__main__":
    main()
