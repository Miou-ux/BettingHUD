"""JSON « Pourquoi cette value ? » — parité dashboard Streamlit (lazy load)."""
from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from typing import Any

_DASHBOARD_MOD: Any = None


def _get_dashboard():
    global _DASHBOARD_MOD
    if _DASHBOARD_MOD is not None:
        return _DASHBOARD_MOD
    os.environ.setdefault("BETTINGHUD_HEADLESS", "1")
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "bettinghud_dashboard_explain",
        root / "app" / "dashboard.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    _DASHBOARD_MOD = mod
    return mod


def _df_records(df) -> list[dict[str, Any]]:
    if df is None:
        return []
    try:
        if getattr(df, "empty", True):
            return []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        out.append({str(c): _json_cell(row[c]) for c in df.columns})
    return out


def _json_cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def _human_factors_rows(match: dict, side: int) -> list[dict[str, str]]:
    fs = match.get("feature_snapshot") or {}
    card_is_p1 = int(side) == 1
    style_left = fs.get("human_p1_style" if card_is_p1 else "human_p2_style", "—")
    style_right = fs.get("human_p2_style" if card_is_p1 else "human_p1_style", "—")
    jet_left = bool(fs.get("p1_jetlag_alert")) if card_is_p1 else bool(fs.get("p2_jetlag_alert"))
    jet_right = bool(fs.get("p2_jetlag_alert")) if card_is_p1 else bool(fs.get("p1_jetlag_alert"))
    try:
        cl_left = float(fs.get("p1_clutch52", 0.5) or 0.5) if card_is_p1 else float(fs.get("p2_clutch52", 0.5) or 0.5)
        cl_right = float(fs.get("p2_clutch52", 0.5) or 0.5) if card_is_p1 else float(fs.get("p1_clutch52", 0.5) or 0.5)
    except (TypeError, ValueError):
        cl_left = cl_right = 0.5
    smb_raw = fs.get("style_matchup_bias")
    try:
        smb_view = float(smb_raw) if smb_raw is not None else None
    except (TypeError, ValueError):
        smb_view = None
    if smb_view is not None and not card_is_p1:
        smb_view = -smb_view
    jet_parts = []
    if jet_left:
        jet_parts.append("Joueur")
    if jet_right:
        jet_parts.append("Adversaire")
    return [
        {"signal": "Style (joueur vs adversaire)", "value": f"{style_left} vs {style_right}"},
        {"signal": "Jetlag alert", "value": ", ".join(jet_parts) if jet_parts else "Aucune alerte"},
        {"signal": "Clutch 52s (joueur/adversaire)", "value": f"{cl_left * 100:.0f}% / {cl_right * 100:.0f}%"},
        {
            "signal": "Style matchup bias (joueur)",
            "value": f"{smb_view:+.3f}" if smb_view is not None else "—",
        },
    ]


def _strip_md_bold(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", str(text or ""))


def build_why_value_explain(
    match: dict,
    *,
    player_name: str,
    opp_name: str,
    side: int,
    odd_book: float,
    odd_true: float,
    val: dict | None = None,
) -> dict[str, Any]:
    """Payload structuré pour le panneau React « Pourquoi cette value ? »."""
    d = _get_dashboard()
    p_num = int(side)
    val = dict(val or {})
    p_label = d._player_display_plain(player_name, match, p_num)
    o_label = d._player_display_plain(opp_name, match, 2 if p_num == 1 else 1)

    comp_raw = d._build_comparison_rows(match, p_label, o_label, p_num, d._infobulle_hand_label)
    comparison = [
        {"indicator": r[0], "player": r[1], "opponent": r[2], "advantage": r[3]}
        for r in (comp_raw or [])
    ]

    quick = d._infobulle_quick_summary(player_name, float(odd_book), float(odd_true), val, match)
    analysis = d._infobulle_analysis_markdown(
        match,
        player_name,
        opp_name,
        float(odd_book),
        float(odd_true),
        val,
        skip_edge_paragraph=True,
    )

    return {
        "quick_summary": _strip_md_bold(quick),
        "human_factors": _human_factors_rows(match, p_num),
        "comparison": comparison,
        "dynamics": _df_records(d._infobulle_dynamics_df(match, player_name, opp_name, p_num)),
        "advanced": _df_records(d._infobulle_advanced_df(match)),
        "analysis": _strip_md_bold(analysis),
        "model_audit": _df_records(d._infobulle_model_audit_df(match)),
        "top_features": _df_records(d._infobulle_top_features_df(match)),
        "tour": str(match.get("tour") or "").upper(),
        "calibration_used": match.get("calibration_used"),
    }
