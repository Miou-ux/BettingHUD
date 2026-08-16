"""Tests formatage alertes ops human-friendly."""
from __future__ import annotations

from scripts.ops_alert_human import format_cron_failure, parse_log_problems


def test_parse_log_wta_c1_duplicates():
    log = """
[QC post_sync] blocking=1 warnings=0
  BLOCK wta_c1_duplicates: C1 doublons: 307 doublon(s)
=== fin sync ATP+WTA rc=1 ===
"""
    problems = parse_log_problems(log)
    assert any("double" in p.lower() or "Doublons" in p for p in problems)
    assert any("307" in p for p in problems)


def test_format_cron_failure_sync_tours():
    log = "BLOCK wta_c1_duplicates: C1 doublons: 2 doublon(s)\n=== fin sync ATP+WTA rc=1 ==="
    subject, body = format_cron_failure("Sync tours 03:30", 1, log)
    assert "Sync données tennis" in subject
    assert "Ce qui bloque" in body
    assert "double" in body.lower() or "Doublons" in body


def test_parse_script_failure():
    log = "enrich_wta_delta_metadata.py a echoue."
    problems = parse_log_problems(log)
    assert any("WTA" in p for p in problems)
