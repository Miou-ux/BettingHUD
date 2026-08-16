"""Messages ops Telegram lisibles (humains) — échecs cron, QC, digest pipeline."""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")

# Codes QC → (titre court, explication)
QC_HUMAN: dict[str, tuple[str, str]] = {
    "wta_c1_duplicates": (
        "Matchs WTA en double",
        "Le même match a été enregistré plusieurs fois (souvent tennis-data + Flashscore).",
    ),
    "wta_d1_ranks": (
        "Rangs WTA manquants",
        "Trop de matchs WTA récents n'ont pas de classement officiel.",
    ),
    "wta_sqlite_ranks_post_cutoff": (
        "Rangs WTA incomplets en base",
        "Après import SQLite, la couverture des rangs WTA est trop basse.",
    ),
    "atp_stale": (
        "Historique ATP trop ancien",
        "Les derniers matchs ATP en base datent de plus de 5 jours.",
    ),
    "wta_stale": (
        "Historique WTA trop ancien",
        "Les derniers matchs WTA en base datent de plus de 5 jours.",
    ),
    "feature_store_old": (
        "Feature store périmé",
        "Le cache de features joueurs n'a pas été reconstruit récemment.",
    ),
    "feature_store_missing": (
        "Feature store absent",
        "Le fichier player_feature_store.joblib est introuvable.",
    ),
    "tours_sync_meta_stale": (
        "Horodatage sync incohérent",
        "La date « dernier sync » affichée n'a pas été mise à jour aujourd'hui.",
    ),
}

# Jobs cron → libellé français
JOB_HUMAN: dict[str, str] = {
    "sync tours 03:30": "Sync données tennis (03:30)",
    "morning build 02:00": "Préparation du jour (02:00)",
    "preflight morning 04:40": "Contrôle avant publication (04:40)",
    "morning publish 05:00": "Publication des picks (05:00)",
    "daily admin digest 06:30": "Digest admin (06:30)",
}

_SCRIPT_FAIL_RE = re.compile(
    r"(?P<script>[\w_]+\.py)\s+(?:a\s+)?(?:echoue|échoué|rapporte\s+erreur)",
    re.IGNORECASE,
)
_BLOCK_RE = re.compile(r"BLOCK\s+(\w+):\s*(.+)", re.IGNORECASE)
_WARN_RE = re.compile(r"WARN\s+(\w+):\s*(.+)", re.IGNORECASE)
_FAIL_LINE_RE = re.compile(r"^\s*FAIL\s+(.+)", re.IGNORECASE)


def _job_label(job: str) -> str:
    key = job.strip().lower()
    for pattern, label in JOB_HUMAN.items():
        if pattern in key:
            return label
    return job.strip()


def _now_label() -> str:
    return datetime.now(PARIS).strftime("%d/%m/%Y %H:%M")


def _freshness_block() -> list[str]:
    try:
        from scripts.bets_db import DB_PATH_DEFAULT, get_data_freshness_snapshot

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dbp = os.path.join(root, DB_PATH_DEFAULT)
        fresh = get_data_freshness_snapshot(dbp)
    except Exception:
        return []

    lines = ["<b>État des données</b>"]
    atp = fresh.get("last_atp_match") or {}
    wta = fresh.get("last_wta_match") or {}
    if atp.get("date"):
        lines.append(f"• ATP : {atp.get('tourney_name', '—')} ({atp.get('date')})")
    if wta.get("date"):
        lines.append(f"• WTA : {wta.get('tourney_name', '—')} ({wta.get('date')})")
    sync = fresh.get("last_tours_sync_iso") or fresh.get("last_sackmann_sync_iso")
    if sync:
        lines.append(f"• Dernière sync : {str(sync)[:16].replace('T', ' ')}")
    return lines


def _humanize_block(code: str, detail: str) -> str:
    title, explain = QC_HUMAN.get(code, (code.replace("_", " ").title(), detail))
    # Extraire un nombre si présent (ex. "307 doublon(s)")
    m = re.search(r"(\d+)", detail)
    count = f" ({m.group(1)})" if m else ""
    line = f"• <b>{title}</b>{count}"
    if explain and explain != detail:
        line += f"\n  <i>{explain}</i>"
    elif detail and detail not in title:
        line += f"\n  <i>{detail}</i>"
    return line


def parse_log_problems(log_text: str, *, max_items: int = 6) -> list[str]:
    """Extrait des problèmes lisibles depuis la fin d'un log cron."""
    if not log_text.strip():
        return ["• Le job s'est arrêté sans message explicite."]

    seen: set[str] = set()
    bullets: list[str] = []

    for line in reversed(log_text.strip().splitlines()):
        m = _BLOCK_RE.search(line)
        if m:
            key = f"block:{m.group(1)}"
            if key not in seen:
                seen.add(key)
                bullets.append(_humanize_block(m.group(1), m.group(2).strip()))
        wm = _WARN_RE.search(line)
        if wm and len(bullets) < max_items:
            key = f"warn:{wm.group(1)}"
            if key not in seen:
                seen.add(key)
                bullets.append(f"• ⚠️ {QC_HUMAN.get(wm.group(1), (wm.group(1), ''))[0]} : {wm.group(2).strip()}")
        sm = _SCRIPT_FAIL_RE.search(line)
        if sm and len(bullets) < max_items:
            script = sm.group("script")
            key = f"script:{script}"
            if key not in seen:
                seen.add(key)
                labels = {
                    "enrich_wta_delta_metadata.py": "Enrichissement métadonnées WTA",
                    "sync_wta_delta.py": "Import nouveaux matchs WTA",
                    "sync_tml_recent.py": "Mise à jour ATP (TennisMyLife)",
                    "pipeline_quality.py": "Import SQLite + index",
                    "build_feature_store.py": "Reconstruction feature store",
                    "preflight_morning_chain.py": "Contrôle pré-publication",
                }
                label = labels.get(script, script)
                bullets.append(f"• <b>{label}</b> en échec")
        fm = _FAIL_LINE_RE.match(line)
        if fm and len(bullets) < max_items:
            msg = fm.group(1).strip()
            key = f"fail:{msg[:40]}"
            if key not in seen:
                seen.add(key)
                bullets.append(f"• {msg}")
        if len(bullets) >= max_items:
            break

    bullets.reverse()
    if not bullets:
        tail = [ln.strip() for ln in log_text.strip().splitlines() if ln.strip()][-3:]
        bullets = [f"• {ln[:200]}" for ln in tail]
    return bullets[:max_items]


def format_cron_failure(job: str, exit_code: int, log_text: str) -> tuple[str, str]:
    """Sujet + corps HTML pour échec cron."""
    label = _job_label(job)
    problems = parse_log_problems(log_text)
    subject = f"🔴 {label} — problème (code {exit_code})"

    parts = [
        f"<b>{subject}</b>",
        f"<i>{_now_label()}</i>",
        "",
        "<b>Ce qui bloque</b>",
        *problems,
    ]

    fresh = _freshness_block()
    if fresh:
        parts.extend(["", *fresh])

    parts.extend(
        [
            "",
            "<b>Impact</b>",
            "• Les picks du matin peuvent être retardés ou basés sur des données incomplètes.",
            "",
            "<b>Que faire</b>",
            "• Consulter le log sur le serveur (data/logs/)",
            "• Si doublons WTA : scripts/fix_wta_data_cleanup.py",
        ]
    )
    return subject, "\n".join(parts)


def format_qc_alert_human(report: Any, *, level: str = "fail") -> tuple[str, str]:
    """Format alerte QC post-sync (hors wrapper cron)."""
    blocking = getattr(report, "blocking", None) or (report.get("blocking") if isinstance(report, dict) else [])
    warnings = getattr(report, "warnings", None) or (report.get("warnings") if isinstance(report, dict) else [])

    if level == "fail":
        subject = "🔴 Contrôle données — échec"
        issues = blocking
    else:
        subject = "⚠️ Contrôle données — attention"
        issues = warnings

    parts = [f"<b>{subject}</b>", f"<i>{_now_label()}</i>", ""]
    if not issues:
        parts.append("• Détail technique indisponible.")
    else:
        parts.append("<b>Détails</b>")
        for issue in issues[:8]:
            if hasattr(issue, "code"):
                code, msg = issue.code, issue.message
            else:
                code = str(issue.get("code") or "issue")
                msg = str(issue.get("message") or "")
            parts.append(_humanize_block(code, msg))

    fresh = _freshness_block()
    if fresh:
        parts.extend(["", *fresh])
    return subject, "\n".join(parts)


def format_pipeline_digest_human() -> str:
    """Section pipeline pour digest admin 06:30."""
    from scripts.bets_db import DB_PATH_DEFAULT, get_data_freshness_snapshot
    from scripts.morning_chain_state import get_step, step_ok_today

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fresh = get_data_freshness_snapshot(os.path.join(root, DB_PATH_DEFAULT))
    tours_ok = step_ok_today("tours_sync")
    qc_ok = step_ok_today("qc_post_sync")

    icon_tours = "✅" if tours_ok else "🔴"
    icon_qc = "✅" if qc_ok else ("🔴" if not qc_ok and get_step("qc_post_sync") else "🟡")

    lines = ["<b>📦 Données tennis</b>"]
    sync = fresh.get("last_tours_sync_iso") or fresh.get("last_sackmann_sync_iso") or "—"
    lines.append(f"{icon_tours} Sync nocturne : {str(sync)[:16].replace('T', ' ')}")

    atp = fresh.get("last_atp_match") or {}
    wta = fresh.get("last_wta_match") or {}
    if atp.get("date"):
        lines.append(f"   ATP → {atp.get('tourney_name', '—')} ({atp.get('date')})")
    if wta.get("date"):
        lines.append(f"   WTA → {wta.get('tourney_name', '—')} ({wta.get('date')})")

    if qc_ok:
        lines.append(f"{icon_qc} Contrôles qualité : OK")
    elif get_step("qc_post_sync"):
        qc = get_step("qc_post_sync")
        detail = qc.get("detail") or {}
        blocking = detail.get("blocking") if isinstance(detail, dict) else []
        if blocking:
            first = blocking[0]
            code = first.get("code", "?") if isinstance(first, dict) else "?"
            title = QC_HUMAN.get(code, (code, ""))[0]
            lines.append(f"{icon_qc} Contrôles qualité : {title}")
        else:
            lines.append(f"{icon_qc} Contrôles qualité : à vérifier")
    else:
        lines.append(f"{icon_qc} Contrôles qualité : pas encore exécutés")

    ml = fresh.get("last_ml_train_iso")
    if ml:
        lines.append(f"🤖 Dernier entraînement ML : {str(ml)[:10]}")
    return "\n".join(lines)


def format_calibration_drift_alert(alerts: list[str]) -> tuple[str, str]:
    """Alerte ops si calibration ML en dérive."""
    subject = "⚠️ Modèle — calibration en dérive"
    parts = [
        f"<b>{subject}</b>",
        f"<i>{_now_label()}</i>",
        "",
        "Les picks récents s'écartent des performances attendues à l'entraînement :",
    ]
    for a in alerts[:5]:
        parts.append(f"• {a}")
    parts.extend(
        [
            "",
            "<i>Le betting continue ; surveiller la série sur 7–30 jours.</i>",
        ]
    )
    return subject, "\n".join(parts)


def format_simple_ops(subject_short: str, body_lines: list[str], *, icon: str = "🔴") -> tuple[str, str]:
    """Alerte générique (build, backup, watchdog…)."""
    subject = f"{icon} {subject_short}"
    parts = [f"<b>{subject}</b>", f"<i>{_now_label()}</i>", ""] + body_lines
    return subject, "\n".join(parts)
