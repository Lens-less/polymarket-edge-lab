"""Service-loop regressions for bootstrap clock refresh task handling."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

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
    ClockSyncMeasurement,
    ContinuousServiceConfig,
)


def _service_config(tmp_path: Path) -> ContinuousServiceConfig:
    return ContinuousServiceConfig(
        data_root=tmp_path / "data",
        research_root=tmp_path / "research",
        preregistration_path=tmp_path / "PREREGISTRATION.json",
        settlement_grace_seconds=50,
        retry_seconds=1,
        status_interval_seconds=1,
        minimum_free_disk_bytes=1,
        max_history_roots=1,
        decision_tau_seconds=(45, 90, 180, 240),
    )


async def _fast_wait_or_stop(
    stop_event: asyncio.Event,
    seconds: float,
) -> bool:
    del seconds
    await asyncio.sleep(0.001)
    return stop_event.is_set()


@pytest.mark.anyio
async def test_capture_uses_successful_bootstrap_clock_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed = ClockSyncMeasurement(
        measured_at_raw_ms=1_000,
        offset_seconds="0.010",
        uncertainty_seconds="0.050",
    )

    async def capture(*args: object, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.01)
        return {"capture_error": None}

    monkeypatch.setattr(runner, "run_compact_forward_capture", capture)
    monkeypatch.setattr(runner, "measure_sntp_clock_sync", lambda: refreshed)
    monkeypatch.setattr(runner, "_emit_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_wait_or_stop", _fast_wait_or_stop)

    summary, measurement = await runner._capture_with_heartbeat(
        config=_service_config(tmp_path),
        capture_config=SimpleNamespace(
            clock_sync={"measured_at_raw_ms": 500},
        ),
        plan=SimpleNamespace(duration_seconds=0.01),
        proxy_url=None,
        stop_event=asyncio.Event(),
        clock_refresh_at_ms=0,
    )

    assert measurement is refreshed
    assert summary["clock_sync_refresh"]["status"] == "success"
    assert summary["clock_sync_refresh"]["initial_evidence"] == {
        "measured_at_raw_ms": 500,
    }
    assert summary["clock_sync_refresh"]["evidence"] == refreshed.to_document()


@pytest.mark.anyio
async def test_capture_records_failed_bootstrap_clock_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def capture(*args: object, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.01)
        return {"capture_error": None}

    def fail_measurement() -> ClockSyncMeasurement:
        raise RuntimeError("simulated SNTP refresh failure")

    monkeypatch.setattr(runner, "run_compact_forward_capture", capture)
    monkeypatch.setattr(runner, "measure_sntp_clock_sync", fail_measurement)
    monkeypatch.setattr(runner, "_emit_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_wait_or_stop", _fast_wait_or_stop)

    summary, measurement = await runner._capture_with_heartbeat(
        config=_service_config(tmp_path),
        capture_config=SimpleNamespace(clock_sync=None),
        plan=SimpleNamespace(duration_seconds=0.01),
        proxy_url=None,
        stop_event=asyncio.Event(),
        clock_refresh_at_ms=0,
    )

    assert measurement is None
    assert summary["clock_sync_refresh"]["status"] == "failed"
    assert summary["clock_sync_refresh"]["error"]["error_code"] == (
        "clock_sync_refresh_failed"
    )


@pytest.mark.anyio
async def test_service_stop_cancels_pending_capture_and_clock_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_cancelled = asyncio.Event()
    capture_started = asyncio.Event()

    async def capture(*args: object, **kwargs: object) -> dict[str, object]:
        capture_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            capture_cancelled.set()

    monkeypatch.setattr(runner, "run_compact_forward_capture", capture)
    monkeypatch.setattr(runner, "_emit_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_wait_or_stop", _fast_wait_or_stop)
    stop_event = asyncio.Event()

    async def stop_after_capture_starts() -> None:
        await capture_started.wait()
        stop_event.set()

    stop_task = asyncio.create_task(stop_after_capture_starts())

    with pytest.raises(runner.ServiceStopRequested):
        await runner._capture_with_heartbeat(
            config=_service_config(tmp_path),
            capture_config=SimpleNamespace(clock_sync=None),
            plan=SimpleNamespace(duration_seconds=60),
            proxy_url=None,
            stop_event=stop_event,
            clock_refresh_at_ms=10**15,
        )
    await stop_task

    assert capture_cancelled.is_set()
