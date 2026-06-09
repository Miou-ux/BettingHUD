"""Formatage des tweets CourtAlphaX (texte + lien track record UTM)."""
from __future__ import annotations

from scripts.courtalphax_config import COURTALPHAX_START_BR_EUR, courtalpha_track_url
from scripts.x_client import X_MAX_CHARS
DISCLAIMER = "Info — pas un conseil financier. BR virtuelle."

_BRAND_HASHTAGS = ("#CourtAlpha", "#ValueBetting")

_TOURNAMENT_HASHTAGS: tuple[tuple[str, str], ...] = (
    ("roland-garros", "#RolandGarros"),
    ("roland garros", "#RolandGarros"),
    ("french open", "#RolandGarros"),
    ("wimbledon", "#Wimbledon"),
    ("us open", "#USOpen"),
    ("australian open", "#AusOpen"),
    ("indian wells", "#IndianWells"),
    ("miami open", "#MiamiOpen"),
    ("miami", "#MiamiOpen"),
    ("monte-carlo", "#MonteCarlo"),
    ("monte carlo", "#MonteCarlo"),
    ("madrid", "#MadridOpen"),
    ("rome", "#ItalianOpen"),
    ("internazionali", "#ItalianOpen"),
    ("cincinnati", "#CincyTennis"),
    ("shanghai", "#ShanghaiMasters"),
    ("paris masters", "#ParisMasters"),
    ("rogers cup", "#CanadaOpen"),
    ("canadian open", "#CanadaOpen"),
    ("queens", "#QueensTennis"),
    ("halle", "#Halle"),
    ("barcelona", "#BarcelonaOpen"),
)


def _proba_pct(pick: dict) -> float:
    try:
        if pick.get("p_model_pct") is not None:
            return float(pick["p_model_pct"])
    except (TypeError, ValueError):
        pass
    return float(pick.get("p_model_fav") or 0.0) * 100.0


def _pick_player_names(row: dict) -> tuple[str, str]:
    """Noms parié / adversaire (clés Telegram ou Paris du jour)."""
    bet_on = str(row.get("bet_on") or row.get("fav_player") or "").strip()
    opp = str(row.get("opponent") or row.get("underdog_player") or "").strip()
    if not bet_on or not opp:
        p1 = str(row.get("player1") or "").strip()
        p2 = str(row.get("player2") or "").strip()
        if p1 and p2:
            if not bet_on:
                bet_on = p1 if row.get("fav_side") in (1, "1", None) else p2
            if not opp:
                opp = p2 if bet_on == p1 else p1
    return bet_on or "?", opp or "?"


def _bet_on_display(bet: dict) -> str:
    """Joueur parié pour tweets résultat (DB parfois sans bet_on)."""
    name = str(bet.get("bet_on") or bet.get("fav_player") or "").strip()
    if name:
        return name
    mn = str(bet.get("match_name") or "").strip()
    if " vs " in mn:
        return mn.split(" vs ", 1)[0].strip()
    return "?"


def _ev_pct(pick: dict) -> float:
    try:
        if pick.get("ev_fav_pct") is not None:
            return float(pick["ev_fav_pct"])
        if pick.get("ev_pct") is not None:
            return float(pick["ev_pct"])
    except (TypeError, ValueError):
        pass
    try:
        return float(pick.get("ev_fav") or 0.0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def _tour_hashtag(tour: str | None) -> str | None:
    u = str(tour or "").strip().upper()
    if u == "ATP":
        return "#ATPTour"
    if u == "WTA":
        return "#WTATour"
    return None


def _tournament_hashtag(tournament: str | None) -> str | None:
    t = str(tournament or "").strip().lower()
    if not t:
        return None
    for needle, tag in _TOURNAMENT_HASHTAGS:
        if needle in t:
            return tag
    return None


def _hashtag_tags(
    *,
    tour: str | None = None,
    tournament: str | None = None,
    no_pick: bool = False,
    weekly: bool = False,
) -> list[str]:
    tags = list(_BRAND_HASHTAGS)
    if weekly or no_pick:
        tags.append("#Tennis")
        return tags
    th = _tour_hashtag(tour)
    tourn = _tournament_hashtag(tournament)
    if tourn:
        tags.append(tourn)
    if th:
        tags.append(th)
    return tags


def _embed_track_url(text: str, *, campaign: str = "daily", limit: int = X_MAX_CHARS) -> str:
    """Ajoute le lien CourtAlpha en fin de tweet (tronque le corps si nécessaire)."""
    url = courtalpha_track_url(campaign=campaign)
    suffix = f"\n{url}"
    if len(text) + len(suffix) <= limit:
        return text + suffix
    room = limit - len(suffix) - 1
    if room < 40:
        return (text[: limit - 4] + "…") if len(text) > limit else text
    trimmed = text[:room].rstrip()
    if "\n" in trimmed:
        trimmed = trimmed.rsplit("\n", 1)[0].rstrip()
    return trimmed + "…" + suffix


def _embed_hashtags(
    body: str,
    tags: list[str],
    *,
    limit: int = X_MAX_CHARS,
    url_campaign: str | None = "daily",
) -> str:
    """Insère les hashtags avant le disclaimer ; retire les tags optionnels si > 280 car."""
    main, disc = body, DISCLAIMER
    if body.endswith(DISCLAIMER):
        main = body[: -len(DISCLAIMER)].rstrip()

    kept = list(tags)
    url = courtalpha_track_url(campaign=url_campaign) if url_campaign else ""
    url_suffix = f"\n{url}" if url else ""
    while kept:
        text = f"{main}\n\n{' '.join(kept)}\n{disc}{url_suffix}"
        if len(text) <= limit:
            return text
        if len(kept) <= len(_BRAND_HASHTAGS):
            break
        kept.pop()
    fallback = f"{main}\n{disc}"
    if url_campaign:
        return _embed_track_url(fallback, campaign=url_campaign, limit=limit)
    if len(fallback) <= limit:
        return fallback
    return fallback[: limit - 1].rstrip() + "…"


def format_daily_pick_tweet(
    pick: dict,
    *,
    pick_number: int,
    stake_eur: float,
    stake_pct: float,
    bankroll_eur: float,
    calendar_date: str,
) -> str:
    bet_on, opp = _pick_player_names(pick)
    odd = float(pick.get("odd_fav") or pick.get("odd_book") or 0.0)
    p_pct = _proba_pct(pick)
    ev_pct = _ev_pct(pick)
    tour = str(pick.get("tour") or "").strip()
    tourn = str(pick.get("tournament") or "").strip()
    surface = str(pick.get("surface") or "").strip()
    when = str(pick.get("match_time") or "").strip()

    meta = " · ".join(x for x in (tourn, surface, tour, when) if x)

    lines = [
        f"🎾 CourtAlpha — pick #{pick_number} ({calendar_date})",
        "",
        f"{bet_on} vs {opp}",
        f"Proba {p_pct:.0f}% · EV {ev_pct:+.0f}% · @{odd:.2f}",
        f"Mise Kelly : {stake_eur:.2f} € ({stake_pct:.1f}% BR)",
    ]
    if meta:
        lines.append(meta)
    lines.extend([
        "",
        f"BR virtuelle : {bankroll_eur:.2f} € (start {COURTALPHAX_START_BR_EUR:.0f} €)",
        DISCLAIMER,
    ])
    body = "\n".join(lines)
    tags = _hashtag_tags(tour=tour, tournament=tourn)
    return _embed_hashtags(body, tags)


def format_no_pick_tweet(
    *,
    calendar_date: str,
    bankroll_eur: float,
    pool_size: int = 0,
) -> str:
    lines = [
        f"🎾 CourtAlpha — {calendar_date}",
        "",
        "Pas de pick value aujourd'hui.",
        "Filtres : proba > 60 % · EV 15–100 % · main draw ATP/WTA.",
    ]
    if pool_size > 0:
        lines.append(f"{pool_size} match(s) scannés · jour sans mise.")
    lines.extend([
        "",
        f"BR virtuelle : {bankroll_eur:.2f} € (start {COURTALPHAX_START_BR_EUR:.0f} €)",
        DISCLAIMER,
    ])
    body = "\n".join(lines)
    return _embed_hashtags(body, _hashtag_tags(no_pick=True))


def format_result_tweet(
    bet: dict,
    *,
    bankroll_eur: float,
    start_br_eur: float,
    wins: int,
    losses: int,
    roi_pct: float | None,
) -> str:
    status = str(bet.get("status") or "").strip()
    won = status == "Gagné"
    icon = "✅" if won else "❌"
    label = "Gagné" if won else "Perdu"
    bet_on = _bet_on_display(bet)
    odd = float(bet.get("odds") or 0.0)
    stake = float(bet.get("stake") or 0.0)
    profit = float(bet.get("profit") or 0.0)
    pl_sign = "+" if profit >= 0 else ""

    growth = 0.0
    if start_br_eur > 0:
        growth = 100.0 * (bankroll_eur - start_br_eur) / start_br_eur

    roi_part = f" · ROI {roi_pct:+.1f}%" if roi_pct is not None else ""
    record = f"{wins}W-{losses}L" if (wins + losses) > 0 else "—"

    lines = [
        f"{icon} {label} — {bet_on} @{odd:.2f}",
        f"P/L : {pl_sign}{profit:.2f} € · mise {stake:.2f} €",
        "",
        f"📊 BR CourtAlphaX : {bankroll_eur:.2f} € ({growth:+.1f}%)",
        f"Record : {record}{roi_part}",
        DISCLAIMER,
    ]
    body = "\n".join(lines)
    tags = _hashtag_tags(
        tour=str(bet.get("tour") or ""),
        tournament=str(bet.get("tournament") or ""),
    )
    return _embed_hashtags(body, tags, url_campaign="result")


def _week_label_fr(week_start: str, week_end: str) -> str:
    from datetime import datetime

    d0 = datetime.strptime(week_start, "%Y-%m-%d")
    d1 = datetime.strptime(week_end, "%Y-%m-%d")
    mo = (
        "jan.",
        "fév.",
        "mar.",
        "avr.",
        "mai",
        "juin",
        "juil.",
        "aoû.",
        "sep.",
        "oct.",
        "nov.",
        "déc.",
    )
    if d0.month == d1.month:
        return f"{d0.day}–{d1.day} {mo[d1.month - 1]}"
    return f"{d0.day} {mo[d0.month - 1]} – {d1.day} {mo[d1.month - 1]}"


def _bet_line_compact(bet: dict) -> str:
    st = str(bet.get("status") or "").strip()
    icon = "✅" if st == "Gagné" else "❌" if st == "Perdu" else "⏳"
    bet_on = _bet_on_display(bet)[:18]
    odd = float(bet.get("odds") or 0.0)
    sched = str(bet.get("sched") or "")[-5:]
    if st in ("Gagné", "Perdu"):
        profit = float(bet.get("profit") or 0.0)
        sign = "+" if profit >= 0 else ""
        return f"{icon} {sched} {bet_on} @{odd:.2f} {sign}{profit:.2f}€"
    return f"{icon} {sched} {bet_on} @{odd:.2f} · en cours"


def format_weekly_recap_tweet(
    *,
    week_start: str,
    week_end: str,
    bets: list[dict],
    no_pick_dates: list[str],
    bankroll_eur: float,
    start_br_eur: float,
) -> str:
    label = _week_label_fr(week_start, week_end)
    wins = sum(1 for b in bets if str(b.get("status") or "") == "Gagné")
    losses = sum(1 for b in bets if str(b.get("status") or "") == "Perdu")
    pending = sum(1 for b in bets if str(b.get("status") or "").strip() in ("", "En cours"))
    settled_pl = sum(
        float(b.get("profit") or 0.0)
        for b in bets
        if str(b.get("status") or "") in ("Gagné", "Perdu")
    )
    staked = sum(float(b.get("stake") or 0.0) for b in bets)
    roi_pct = (100.0 * settled_pl / staked) if staked > 0 else None
    growth = 100.0 * (bankroll_eur - start_br_eur) / start_br_eur if start_br_eur > 0 else 0.0
    pl_sign = "+" if settled_pl >= 0 else ""

    lines = [f"📊 CourtAlpha — récap semaine {label}", ""]
    if not bets and not no_pick_dates:
        lines.append("Semaine calme · aucun brief publié.")
    elif not bets:
        lines.append(f"{len(no_pick_dates)} jour(s) sans pick value.")
    else:
        pend = f"-{pending}⏳" if pending else ""
        lines.append(f"Paris : {len(bets)} · {wins}W-{losses}L{pend}")
        if settled_pl or wins or losses:
            roi_s = f" · ROI {roi_pct:+.0f}%" if roi_pct is not None else ""
            lines.append(f"P/L semaine : {pl_sign}{settled_pl:.2f} €{roi_s}")
        if no_pick_dates:
            lines.append(f"Jours sans pick : {len(no_pick_dates)}")
        lines.append("")
        for bet in bets:
            lines.append(_bet_line_compact(bet))

    lines.extend([
        "",
        f"BR virtuelle : {bankroll_eur:.2f} € ({growth:+.1f}%)",
        DISCLAIMER,
    ])
    body = "\n".join(lines)
    text = _embed_hashtags(body, _hashtag_tags(weekly=True), limit=X_MAX_CHARS, url_campaign="weekly")
    if len(text) <= X_MAX_CHARS:
        return text

    compact = [f"📊 CourtAlpha — récap semaine {label}", ""]
    if not bets and not no_pick_dates:
        compact.append("Semaine calme · aucun brief.")
    elif not bets:
        compact.append(f"{len(no_pick_dates)} jour(s) sans pick value.")
    else:
        compact.append(
            f"Paris {len(bets)} · {wins}W-{losses}L"
            + (f" · {pending} en cours" if pending else "")
        )
        if wins or losses:
            roi_s = f" · ROI {roi_pct:+.0f}%" if roi_pct is not None else ""
            compact.append(f"P/L : {pl_sign}{settled_pl:.2f} €{roi_s}")
        if no_pick_dates:
            compact.append(f"Sans pick : {len(no_pick_dates)} j")
    compact.extend(["", f"BR : {bankroll_eur:.2f} € ({growth:+.1f}%)", DISCLAIMER])
    return _embed_hashtags("\n".join(compact), _hashtag_tags(weekly=True), url_campaign="weekly")
