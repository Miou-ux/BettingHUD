#!/usr/bin/env python3

"""Publie le pick safe du jour (Top5 #1) sur X + enregistre le pari CourtAlphaX.



Si aucun pick value : tweet informatif « pas de mise aujourd'hui ».



Usage :

  py -3 scripts/courtalphax_daily_pick.py --dry-run

  py -3 scripts/courtalphax_daily_pick.py



Variables (.env) :

  COURTALPHAX_X_ENABLED=1

  X_USER_ACCESS_TOKEN=…   (OAuth 2.0 user, recommandé)

  # ou OAuth 1.0a : X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET

"""

from __future__ import annotations



import argparse

import os

import sqlite3

import sys



ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.chdir(ROOT)

sys.path.insert(0, ROOT)

os.environ.setdefault("BETTINGHUD_HEADLESS", "1")



from scripts.bets_db import (  # noqa: E402

    compute_telegram_user_bankroll_eur,

    ensure_bets_meta,

    save_bet_enriched,

)

from scripts.courtalphax_config import (  # noqa: E402

    COURTALPHAX_TELEGRAM_USER_ID,

    COURTALPHAX_TRACKER_SOURCE,

)

from scripts.courtalphax_format import format_daily_pick_tweet, format_no_pick_tweet  # noqa: E402

from scripts.courtalphax_post_log import (  # noqa: E402

    count_daily_picks,

    has_daily_post_for_date,

    log_post,

)

from scripts.telegram_bet_flow import kelly_stake_for_pick  # noqa: E402

from scripts.telegram_top5_notify import (  # noqa: E402

    _load_top5_context,

    filter_telegram_display_picks,

)

from scripts.courtalphax_preflight import check_courtalphax_preflight  # noqa: E402
from scripts.x_client import post_tweet, require_prod_for_x_post, truncate_tweet, x_posting_enabled  # noqa: E402





def _existing_bet_today(conn: sqlite3.Connection, calendar_date: str) -> bool:

    row = conn.execute(

        """

        SELECT 1 FROM user_bets

        WHERE telegram_user_id = ?

          AND COALESCE(TRIM(tracker_source), '') = ?

          AND COALESCE(NULLIF(TRIM(match_date), ''), date) = ?

        LIMIT 1

        """,

        (COURTALPHAX_TELEGRAM_USER_ID, COURTALPHAX_TRACKER_SOURCE, calendar_date),

    ).fetchone()

    return row is not None





def _publish_no_pick(

    conn: sqlite3.Connection,

    *,

    cal_day: str,

    pool_n: int,

    dry_run: bool,

) -> dict:

    snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)

    avail = float(snap["available_eur"])

    tweet = truncate_tweet(

        format_no_pick_tweet(

            calendar_date=cal_day,

            bankroll_eur=avail,

            pool_size=pool_n,

        )

    )



    if dry_run:

        print(tweet)

        print(f"\n--- dry-run · pas de pick · BR {avail:.2f} € ---")

        return {

            "ok": True,

            "dry_run": True,

            "calendar_date": cal_day,

            "no_pick": True,

            "tweet": tweet,

        }



    if not x_posting_enabled():

        raise SystemExit("COURTALPHAX_X_ENABLED=1 requis pour publier (ou --dry-run).")



    posted = post_tweet(tweet, dry_run=False)

    log_post(

        conn,

        post_type="no_pick",

        calendar_date=cal_day,

        tweet_id=posted.get("tweet_id"),

        tweet_text=tweet,

    )

    return {

        "ok": True,

        "calendar_date": cal_day,

        "no_pick": True,

        "tweet_id": posted.get("tweet_id"),

    }





def run_daily_pick(*, dry_run: bool = False, force: bool = False, skip_preflight: bool = False) -> dict:

    require_prod_for_x_post(force=force, dry_run=dry_run)

    if not skip_preflight and not force and not dry_run:
        preflight = check_courtalphax_preflight()
        if not preflight.get("ready"):
            return {
                "ok": False,
                "reason": "not_ready",
                "message": preflight.get("summary") or "Snapshot/scrape pas prêts.",
                "preflight": preflight,
            }

    picks, _meta, cal_day, pool_n, _age = _load_top5_context(

        limit=5,

        ev_min_pct=15.0,

        ev_max_pct=100.0,

    )

    picks = filter_telegram_display_picks(picks)



    conn = sqlite3.connect("data/bettinghud.db")

    try:

        ensure_bets_meta(conn)

        if not force and (has_daily_post_for_date(conn, cal_day) or _existing_bet_today(conn, cal_day)):

            return {

                "ok": False,

                "reason": "already_posted",

                "calendar_date": cal_day,

                "message": "Brief du jour déjà publié ou pari déjà enregistré.",

            }



        if not picks:

            return _publish_no_pick(conn, cal_day=cal_day, pool_n=pool_n, dry_run=dry_run)



        pick = picks[0]

        snap = compute_telegram_user_bankroll_eur(conn, COURTALPHAX_TELEGRAM_USER_ID)

        avail = float(snap["available_eur"])

        odd = float(pick.get("odd_fav") or pick.get("odd_book") or 0.0)

        if odd <= 1.0:

            return {"ok": False, "reason": "bad_odd", "calendar_date": cal_day}



        stake_eur, stake_pct = kelly_stake_for_pick(

            pick,

            custom_odd=odd,

            bankroll_avail=avail,

        )

        if stake_eur < 0.01:

            return {

                "ok": False,

                "reason": "stake_zero",

                "calendar_date": cal_day,

                "available_eur": avail,

            }



        pick_number = count_daily_picks(conn) + 1

        tweet = truncate_tweet(

            format_daily_pick_tweet(

                pick,

                pick_number=pick_number,

                stake_eur=stake_eur,

                stake_pct=stake_pct,

                bankroll_eur=avail,

                calendar_date=cal_day,

            )

        )



        if dry_run:

            print(tweet)

            print(f"\n--- dry-run · mise {stake_eur:.2f} € · BR dispo {avail:.2f} € ---")

            return {

                "ok": True,

                "dry_run": True,

                "calendar_date": cal_day,

                "stake_eur": stake_eur,

                "tweet": tweet,

            }



        if not x_posting_enabled():

            raise SystemExit("COURTALPHAX_X_ENABLED=1 requis pour publier (ou --dry-run).")



        posted = post_tweet(tweet, dry_run=False)

        bet_id = save_bet_enriched(

            match_name=str(pick.get("match_name") or f"{pick.get('bet_on')} vs {pick.get('opponent')}"),

            bet_on=str(pick.get("bet_on") or ""),

            odds=odd,

            stake=stake_eur,

            match_date=cal_day,

            tour=str(pick.get("tour") or "") or None,

            surface=str(pick.get("surface") or "") or None,

            tournament=str(pick.get("tournament") or "") or None,

            segment_key=str(pick.get("segment_key") or "") or None,

            p_model=float(pick.get("p_model_fav") or 0) or None,

            ev_at_bet=float(pick.get("ev_fav") or 0) or None,

            notes=f"CourtAlphaX pick #{pick_number}",

            tracker_source=COURTALPHAX_TRACKER_SOURCE,

            telegram_user_id=COURTALPHAX_TELEGRAM_USER_ID,

        )

        log_post(

            conn,

            post_type="daily_pick",

            calendar_date=cal_day,

            bet_id=bet_id,

            tweet_id=posted.get("tweet_id"),

            tweet_text=tweet,

        )

        return {

            "ok": True,

            "calendar_date": cal_day,

            "bet_id": bet_id,

            "tweet_id": posted.get("tweet_id"),

            "stake_eur": stake_eur,

        }

    finally:

        conn.close()





def main() -> int:

    ap = argparse.ArgumentParser(description="CourtAlphaX — pick safe du jour sur X")

    ap.add_argument("--dry-run", action="store_true", help="Aperçu sans tweet ni pari")

    ap.add_argument("--force", action="store_true", help="Publier hors PROD (déconseillé)")
    ap.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Ignorer vérif scrape/snapshot (déconseillé en prod)",
    )

    args = ap.parse_args()

    res = run_daily_pick(
        dry_run=args.dry_run,
        force=args.force,
        skip_preflight=args.skip_preflight,
    )

    if not res.get("ok"):

        print(res.get("message") or res, file=sys.stderr)

        if res.get("reason") == "already_posted":
            return 1
        if res.get("reason") == "not_ready":
            return 2
        return 0

    if not args.dry_run:

        if res.get("no_pick"):

            print(f"OK pas de pick · tweet={res.get('tweet_id')}")

        else:

            print(f"OK pick publié · bet_id={res.get('bet_id')} · tweet={res.get('tweet_id')}")

    return 0





if __name__ == "__main__":

    raise SystemExit(main())


