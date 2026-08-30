from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from src.profit_system.reports import (
    ACCEPTANCE_REPORT_SCHEMA_VERSION,
    SCAN_REPORT_SCHEMA_VERSION,
    SHADOW_REPORT_SCHEMA_VERSION,
    build_acceptance_report,
    build_desk_report,
    build_replay_report,
    build_scan_report,
    build_shadow_report,
    write_report,
)


def test_scan_report_uses_decimal_strings() -> None:
    report = build_scan_report(strategy="arbitrage")

    assert report["schema_version"] == SCAN_REPORT_SCHEMA_VERSION
    opportunity = cast(list[dict[str, Any]], report["opportunities"])[0]
    assert report["track"] == "A"
    assert opportunity["tradable_edge"] == "1.7"
    assert isinstance(opportunity["expected_net_edge"], str)
    assert report["gate_report"]["status"] == "GO"


def test_shadow_report_includes_gate_report() -> None:
    report = build_shadow_report(strategy="maker")

    assert report["schema_version"] == SHADOW_REPORT_SCHEMA_VERSION
    gate_report = cast(dict[str, Any], report["gate_report"])
    assert gate_report["gate"] == "SHADOW"
    assert gate_report["status"] == "NO_GO"
    assert cast(dict[str, Any], report["metrics"])["realized_net_pnl"] is None


def test_acceptance_report_contains_track_chain_proof() -> None:
    report = build_acceptance_report(track="all")

    assert report["schema_version"] == ACCEPTANCE_REPORT_SCHEMA_VERSION
    tracks = cast(list[dict[str, Any]], report["tracks"])
    assert [track["track"] for track in tracks] == ["A", "B"]
    assert tracks[0]["replay_execution"]["confirm_status"] == "CONFIRMED"
    assert tracks[0]["shadow_execution"]["confirm_status"] == "BLOCKED"
    assert tracks[0]["paper"]["realized_net_pnl"] == "2.5"
    assert tracks[0]["shadow"]["realized_net_pnl"] is None


def test_replay_report_uses_real_track_execution_result() -> None:
    report = build_replay_report(strategy="maker")

    assert report["track"] == "B"
    assert cast(dict[str, Any], report["stage"])["confirm_status"] == "CONFIRMED"
    row = cast(list[dict[str, Any]], report["rows"])[0]
    assert row["realized_net_pnl"] == "0"
    assert row["paper_reference_realized_net_pnl"] == "0"


def test_write_report_persists_json(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    report = build_replay_report(strategy="maker")

    write_report(path, report)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == report["schema_version"]


def test_desk_report_never_invents_live_profitability_evidence() -> None:
    report = build_desk_report()

    assert report["current_canary_status"] == "LIVE_BLOCKED"
    assert report["profitability_status"] == "NOT_CLAIMED"
    assert report["realized_net_pnl"] is None
    assert report["real_orders_submitted"] is False
    assert report["real_funds_changed"] is False
    gate_snapshots = cast(list[dict[str, Any]], report["gate_snapshots"])
    assert [gate["gate"] for gate in gate_snapshots] == ["CANARY"]
