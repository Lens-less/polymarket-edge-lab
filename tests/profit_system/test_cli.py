from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.profit_system import cli


def test_doctor_command_prints_live_blocked_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["doctor"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "LIVE_BLOCKED"
    assert "credentials_signing" in payload["remaining_conditions"]


def test_scan_command_writes_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "scan.json"

    exit_code = cli.main(["scan", "--output", str(output_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "scan"
    assert payload["track"] == "A"
    assert payload["gate_report"]["status"] == "GO"
    assert payload["opportunities"][0]["strategy_id"] == "track_a.complete_set.arbitrage.v0_2"
    assert json.loads(output_path.read_text(encoding="utf-8"))["command"] == "scan"


def test_status_command_uses_doctor_surface(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["status"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "status"
    assert payload["gate_report"]["status"] == "LIVE_BLOCKED"


def test_acceptance_command_emits_track_reports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "acceptance.json"

    exit_code = cli.main(["acceptance", "--track", "all", "--output", str(output_path)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "acceptance"
    assert [track["track"] for track in payload["tracks"]] == ["A", "B"]
    assert payload["tracks"][0]["shadow_execution"]["confirm_status"] == "BLOCKED"
    assert json.loads(output_path.read_text(encoding="utf-8"))["command"] == "acceptance"


def test_replay_default_uses_real_maker_track(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["replay"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "replay"
    assert payload["track"] == "B"
    assert payload["strategy_id"] == "track_b.maker.quote.v0_2"
    assert payload["rows"][0]["paper_reference_realized_net_pnl"] == "0"


def test_shadow_reports_observational_only_pnl(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["shadow"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "shadow"
    assert payload["track"] == "B"
    assert payload["metrics"]["realized_net_pnl"] is None
    assert payload["metrics"]["pnl_basis"] == "shadow_is_observational_only"


def test_no_live_switch_is_exposed() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["doctor", "--live"])
