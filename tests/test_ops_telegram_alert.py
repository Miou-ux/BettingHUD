#!/usr/bin/env python3
"""Tests anti-doublon ops alerts."""
from __future__ import annotations

import json
from pathlib import Path

import scripts.ops_telegram_alert as opa


def test_ops_alert_dedup_skips_second(tmp_path, monkeypatch):
    dedup = tmp_path / "ops_alert_dedup.json"
    monkeypatch.setattr(opa, "_DEDUP_PATH", dedup)
    monkeypatch.setenv("BETTINGHUD_OPS_ALERT_DEDUP", "1")
    monkeypatch.setenv("BETTINGHUD_OPS_ALERT_COOLDOWN_SEC", "600")
    monkeypatch.setenv("BETTINGHUD_OPS_ALERT", "1")

    sent: list[str] = []

    def fake_send(text, token=None, chat_id=None):
        sent.append(text)
        return {"ok": True}

    monkeypatch.setattr(opa, "admin_chat_ids", lambda: ["1"])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(
        "scripts.telegram_top5_notify.send_telegram_message",
        fake_send,
        raising=False,
    )
    # Patch import path used inside send_ops_alert
    import scripts.telegram_top5_notify as tg

    monkeypatch.setattr(tg, "send_telegram_message", fake_send)

    assert opa.send_ops_alert("QC post-sync FAIL", "block", dedup_key="qc_post_sync_fail") is True
    assert len(sent) == 1
    assert opa.send_ops_alert("Sync tours 03:30 — ÉCHEC (code 1)", "tail", dedup_key="qc_post_sync_fail") is True
    assert len(sent) == 1  # skipped
    assert opa.send_ops_alert("Sync tours 03:30 — ÉCHEC (code 1)", "tail", dedup_key="qc_post_sync_fail", force=True) is True
    assert len(sent) == 2


def test_normalize_maps_sync_and_qc_together():
    assert opa._normalize_dedup_key("QC post-sync FAIL", None) == "qc_post_sync_fail"
    assert opa._normalize_dedup_key("Sync tours 03:30 — ÉCHEC (code 1)", None) == "qc_post_sync_fail"
