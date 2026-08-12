"""Linux clock-source regressions for the continuous BTC TWAP paper service."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_installed_fcntl_stub = False
try:
    __import__("fcntl")
except ModuleNotFoundError:
    fcntl = types.ModuleType("fcntl")
    fcntl.LOCK_EX = 1
    fcntl.LOCK_NB = 2
    fcntl.LOCK_UN = 8
    fcntl.flock = lambda *args, **kwargs: None
    sys.modules["fcntl"] = fcntl
    _installed_fcntl_stub = True

import scripts.run_btc_twap_relative_value_service as runner  # noqa: E402

if _installed_fcntl_stub:
    sys.modules.pop("fcntl", None)

from src.edge_lab.btc_twap_relative_value_service import (  # noqa: E402
    CLOCK_SYNC_SOURCE_CHRONY_AMAZON,
    CLOCK_SYNC_SOURCE_SNTP,
    ContinuousServiceConfig,
    load_service_config,
    measure_chrony_clock_sync,
    parse_chronyc_tracking_output,
)


def _chronyc_output(
    *,
    reference: str = "A9FEA97B (169.254.169.123)",
    system_time: str = "0.000321 seconds slow of NTP time",
    last_offset: str = "-0.000012345",
    root_delay: str = "0.000246",
    root_dispersion: str = "0.000400",
    leap_status: str = "Normal",
) -> str:
    return "\n".join(
        (
            f"Reference ID    : {reference}",
            "Stratum         : 4",
            f"System time     : {system_time}",
            f"Last offset     : {last_offset} seconds",
            f"Root delay      : {root_delay} seconds",
            f"Root dispersion : {root_dispersion} seconds",
            f"Leap status     : {leap_status}",
        )
    )


def _service_config(
    tmp_path: Path,
    *,
    clock_sync_source: str,
) -> ContinuousServiceConfig:
    preregistration = tmp_path / "PREREGISTRATION.json"
    preregistration.write_text("{}", encoding="utf-8")
    return ContinuousServiceConfig(
        data_root=tmp_path / "data",
        research_root=tmp_path / "research",
        preregistration_path=preregistration,
        settlement_grace_seconds=360,
        retry_seconds=30,
        status_interval_seconds=15,
        minimum_free_disk_bytes=1,
        max_history_roots=1,
        decision_tau_seconds=(240, 180, 120, 60),
        clock_sync_source=clock_sync_source,
    )


def test_chronyc_parser_accepts_amazon_time_sync_reference() -> None:
    measurement = parse_chronyc_tracking_output(
        _chronyc_output(),
        measured_at_raw_ms=123_000,
    )

    assert measurement.source == CLOCK_SYNC_SOURCE_CHRONY_AMAZON
    assert measurement.offset_seconds == "0.000321"
    assert measurement.uncertainty_seconds == "0.000535345"
    assert measurement.causal_receipt_offset_ms == 1
    assert measurement.uncertainty_ms == 1
    assert measurement.to_document()["system_clock_mutated"] is False


def test_chronyc_parser_flips_fast_system_time_negative() -> None:
    measurement = parse_chronyc_tracking_output(
        _chronyc_output(system_time="0.000321 seconds fast of NTP time"),
        measured_at_raw_ms=123_000,
    )

    assert measurement.offset_seconds == "-0.000321"


@pytest.mark.parametrize(
    ("reference", "leap_status", "message"),
    (
        ("A9FEA97B (169.254.169.124)", "Normal", "169.254.169.123"),
        ("A9FEA97B (169.254.169.123)", "Insert second", "leap status"),
    ),
)
def test_chronyc_parser_rejects_unfrozen_reference_or_leap_status(
    reference: str,
    leap_status: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_chronyc_tracking_output(
            _chronyc_output(reference=reference, leap_status=leap_status),
            measured_at_raw_ms=123_000,
        )


def test_chrony_measurement_uses_best_uncertainty_across_bounded_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = iter(
        (
            (0, _chronyc_output(last_offset="-0.000050", root_delay="0.000100")),
            (1, "temporary chronyc failure"),
            (0, _chronyc_output(last_offset="-0.000010", root_delay="0.000040")),
        )
    )
    calls: list[tuple[object, ...]] = []

    class Completed:
        def __init__(self, returncode: int, output: str) -> None:
            self.returncode = returncode
            self.stdout = output if returncode == 0 else ""
            self.stderr = output if returncode != 0 else ""

    def fake_run(*args: object, **kwargs: object) -> Completed:
        calls.append(args)
        return Completed(*next(outputs))

    monkeypatch.setattr(
        "src.edge_lab.btc_twap_relative_value_service.subprocess.run",
        fake_run,
    )

    measurement = measure_chrony_clock_sync()

    assert len(calls) == 3
    assert measurement.source == CLOCK_SYNC_SOURCE_CHRONY_AMAZON
    assert measurement.uncertainty_seconds == "0.000430"


def test_chrony_measurement_fails_after_three_unsuccessful_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "temporary chronyc failure"

    def fake_run(*args: object, **kwargs: object) -> Completed:
        nonlocal calls
        calls += 1
        return Completed()

    monkeypatch.setattr(
        "src.edge_lab.btc_twap_relative_value_service.subprocess.run",
        fake_run,
    )

    with pytest.raises(RuntimeError, match="chrony clock measurement failed"):
        measure_chrony_clock_sync()

    assert calls == 3


def test_service_config_defaults_to_frozen_macos_source(tmp_path: Path) -> None:
    config = _service_config(tmp_path, clock_sync_source=CLOCK_SYNC_SOURCE_SNTP)
    assert config.clock_sync_source == CLOCK_SYNC_SOURCE_SNTP


def test_service_config_loader_accepts_explicit_linux_clock_source(
    tmp_path: Path,
) -> None:
    preregistration = tmp_path / "PREREGISTRATION.json"
    preregistration.write_text("{}", encoding="utf-8")
    path = tmp_path / "SERVICE_CONFIG.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "btc-twap-relative-value-continuous-service.v1",
                "data_root": str(tmp_path / "data"),
                "research_root": str(tmp_path / "research"),
                "preregistration_path": str(preregistration),
                "settlement_grace_seconds": 360,
                "retry_seconds": 30,
                "status_interval_seconds": 15,
                "minimum_free_disk_bytes": 1,
                "max_history_roots": 1,
                "decision_tau_seconds": [240, 180, 120, 60],
                "clock_sync_source": CLOCK_SYNC_SOURCE_CHRONY_AMAZON,
            }
        ),
        encoding="utf-8",
    )

    config = load_service_config(path)

    assert config.clock_sync_source == CLOCK_SYNC_SOURCE_CHRONY_AMAZON


def test_runner_dispatches_linux_clock_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _service_config(
        tmp_path, clock_sync_source=CLOCK_SYNC_SOURCE_CHRONY_AMAZON
    )
    calls: list[str] = []

    def fake_sntp() -> object:
        calls.append("sntp")
        return object()

    def fake_chrony() -> object:
        calls.append("chrony")
        return object()

    monkeypatch.setattr(runner, "measure_sntp_clock_sync", fake_sntp)
    monkeypatch.setattr(runner, "measure_chrony_clock_sync", fake_chrony)

    runner._measure_clock_sync(config)

    assert calls == ["chrony"]
