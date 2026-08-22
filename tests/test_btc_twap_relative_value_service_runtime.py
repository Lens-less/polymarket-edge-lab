"""Service-loop regressions for bootstrap clock refresh task handling."""

from __future__ import annotations

import asyncio
import json
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


def _write_preregistration(
    tmp_path: Path,
    *,
    document: dict[str, object] | None = None,
) -> dict[str, object]:
    preregistration = (
        {"runtime_identity": "match"} if document is None else dict(document)
    )
    (_service_config(tmp_path).preregistration_path).write_text(
        json.dumps(preregistration),
        encoding="utf-8",
    )
    return preregistration


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


def test_history_roots_prefers_newest_valid_unique_capture_roots(
    tmp_path: Path,
) -> None:
    config = _service_config(tmp_path)
    reports_dir = config.research_root / "reports"
    reports_dir.mkdir(parents=True)
    old_root = tmp_path / "old-capture"
    mid_root = tmp_path / "mid-capture"
    new_root = tmp_path / "new-capture"
    for root in (old_root, mid_root, new_root):
        root.mkdir()
    for root, started_at_ms in (
        (old_root, 1_000),
        (mid_root, 2_000),
        (new_root, 3_000),
    ):
        (root / "capture-config.json").write_text(
            json.dumps({"capture_started_at_ms": started_at_ms}),
            encoding="utf-8",
        )
    duplicate = reports_dir / "duplicate.json"
    duplicate.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T00:15:00Z",
                "inputs": {"capture_root": str(new_root)},
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "old.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T00:00:00Z",
                "inputs": {"capture_root": str(old_root)},
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "mid.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T00:10:00Z",
                "inputs": {"capture_root": str(mid_root)},
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "new.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T00:20:00Z",
                "inputs": {"capture_root": str(new_root)},
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "invalid.json").write_text(
        json.dumps({"generated_at": "2026-08-13T00:30:00Z", "inputs": {}}),
        encoding="utf-8",
    )
    config = ContinuousServiceConfig(**{**config.__dict__, "max_history_roots": 2})

    roots = runner._history_roots(config, exclude=tmp_path / "active-capture")

    assert roots == (new_root.resolve(), mid_root.resolve())


def test_history_roots_uses_capture_time_not_regenerated_report_time(
    tmp_path: Path,
) -> None:
    config = _service_config(tmp_path)
    reports_dir = config.research_root / "reports"
    reports_dir.mkdir(parents=True)
    older = tmp_path / "older"
    newer = tmp_path / "newer"
    for root, started_at_ms in ((older, 1_000), (newer, 2_000)):
        root.mkdir()
        (root / "capture-config.json").write_text(
            json.dumps({"capture_started_at_ms": started_at_ms}),
            encoding="utf-8",
        )
    (reports_dir / "regenerated-old.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T00:30:00Z",
                "inputs": {"capture_root": str(older)},
            }
        ),
        encoding="utf-8",
    )
    (reports_dir / "newer.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-13T00:20:00Z",
                "inputs": {"capture_root": str(newer)},
            }
        ),
        encoding="utf-8",
    )

    assert runner._history_roots(
        config,
        exclude=tmp_path / "active",
    ) == (newer.resolve(),)


def test_main_validate_only_loads_and_validates_preregistration_before_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _service_config(tmp_path)
    preregistration = _write_preregistration(tmp_path)
    calls: list[str] = []

    class DummySession:
        def close(self) -> None:
            calls.append("public_session.close")

    def fake_validate(
        document: dict[str, object],
        *,
        config: ContinuousServiceConfig,
    ) -> None:
        assert document == preregistration
        assert config == _service_config(tmp_path)
        calls.append("validate")

    monkeypatch.setattr(runner, "load_service_config", lambda path: config)
    monkeypatch.setattr(
        runner,
        "verify_paper_only_guard",
        lambda: calls.append("verify_paper_only_guard"),
    )
    monkeypatch.setattr(
        runner,
        "public_session",
        lambda proxy: calls.append("public_session") or DummySession(),
    )
    monkeypatch.setattr(
        runner,
        "require_disk_capacity",
        lambda cfg: calls.append("require_disk_capacity"),
    )
    monkeypatch.setattr(
        runner,
        "validate_preregistration_runtime_identity",
        fake_validate,
    )
    monkeypatch.setattr(
        runner,
        "_async_main",
        lambda *args, **kwargs: pytest.fail(
            "_async_main should not run in validate-only"
        ),
    )

    assert (
        runner.main(["--config", str(tmp_path / "service.json"), "--validate-only"])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["paper_only"] is True
    assert payload["public_only"] is True
    assert payload["new_orders_disabled"] is True
    assert payload["authenticated_endpoints_used"] == 0
    assert payload["orders_submitted"] == 0
    assert calls == [
        "verify_paper_only_guard",
        "validate",
        "public_session",
        "public_session.close",
        "require_disk_capacity",
    ]


def test_service_entrypoint_rejects_proxy_configuration() -> None:
    with pytest.raises(SystemExit) as caught:
        runner.build_parser().parse_args(
            ["--config", "service.json", "--proxy", "http://127.0.0.1:7897"]
        )

    assert caught.value.code == 2


def test_main_validate_only_fails_closed_on_mismatched_preregistration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _service_config(tmp_path)
    _write_preregistration(tmp_path, document={"runtime_identity": "mismatch"})
    calls: list[str] = []

    class DummySession:
        def close(self) -> None:
            calls.append("public_session.close")

    def fail_validate(
        document: dict[str, object],
        *,
        config: ContinuousServiceConfig,
    ) -> None:
        assert document["runtime_identity"] == "mismatch"
        raise ValueError(
            "service evidence_track_id does not match preregistration scope"
        )

    monkeypatch.setattr(runner, "load_service_config", lambda path: config)
    monkeypatch.setattr(
        runner,
        "verify_paper_only_guard",
        lambda: calls.append("verify_paper_only_guard"),
    )
    monkeypatch.setattr(
        runner,
        "public_session",
        lambda proxy: calls.append("public_session") or DummySession(),
    )
    monkeypatch.setattr(
        runner,
        "require_disk_capacity",
        lambda cfg: calls.append("require_disk_capacity"),
    )
    monkeypatch.setattr(
        runner,
        "validate_preregistration_runtime_identity",
        fail_validate,
    )

    with pytest.raises(ValueError, match="does not match preregistration scope"):
        runner.main(["--config", str(tmp_path / "service.json"), "--validate-only"])

    assert capsys.readouterr().out == ""
    assert calls == ["verify_paper_only_guard"]


@pytest.mark.anyio
async def test_run_service_rejects_mismatched_preregistration_before_dirs_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _service_config(tmp_path)
    _write_preregistration(tmp_path, document={"runtime_identity": "mismatch"})
    calls: list[str] = []

    def fail_validate(
        document: dict[str, object],
        *,
        config: ContinuousServiceConfig,
    ) -> None:
        assert document["runtime_identity"] == "mismatch"
        raise ValueError("service decision_tau_seconds do not match preregistration")

    monkeypatch.setattr(
        runner,
        "verify_paper_only_guard",
        lambda: calls.append("verify_paper_only_guard"),
    )
    monkeypatch.setattr(
        runner,
        "validate_preregistration_runtime_identity",
        fail_validate,
    )
    monkeypatch.setattr(
        runner,
        "public_session",
        lambda proxy: pytest.fail(
            "public_session should not run after identity mismatch"
        ),
    )

    with pytest.raises(ValueError, match="decision_tau_seconds"):
        await runner.run_service(
            config,
            proxy_url=None,
            stop_event=asyncio.Event(),
        )

    assert calls == ["verify_paper_only_guard"]
    assert not config.data_root.exists()
    assert not config.research_root.exists()


@pytest.mark.anyio
async def test_run_service_accepts_matching_preregistration_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _service_config(tmp_path)
    preregistration = _write_preregistration(tmp_path)
    calls: list[str] = []

    class DummySession:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True
            calls.append("public_session.close")

    session = DummySession()

    def fake_validate(
        document: dict[str, object],
        *,
        config: ContinuousServiceConfig,
    ) -> None:
        assert document == preregistration
        calls.append("validate")

    def stop_after_validation(cfg: ContinuousServiceConfig) -> None:
        assert cfg == config
        calls.append("require_disk_capacity")
        raise runner.ServiceStopRequested

    monkeypatch.setattr(
        runner,
        "verify_paper_only_guard",
        lambda: calls.append("verify_paper_only_guard"),
    )
    monkeypatch.setattr(
        runner,
        "validate_preregistration_runtime_identity",
        fake_validate,
    )
    monkeypatch.setattr(
        runner,
        "public_session",
        lambda proxy: calls.append("public_session") or session,
    )
    monkeypatch.setattr(
        runner,
        "PublicSourcesClient",
        lambda **kwargs: calls.append("PublicSourcesClient") or object(),
    )
    monkeypatch.setattr(runner, "_emit_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "require_disk_capacity", stop_after_validation)

    await runner.run_service(
        config,
        proxy_url=None,
        stop_event=asyncio.Event(),
    )

    assert config.data_root.exists()
    assert config.research_root.exists()
    assert calls == [
        "verify_paper_only_guard",
        "validate",
        "public_session",
        "PublicSourcesClient",
        "require_disk_capacity",
        "public_session.close",
    ]


def test_every_capture_targets_an_expiry_with_future_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_epoch_ms", lambda: 1_786_532_400_000)

    assert runner._next_capture_expiry_seconds() == 1_786_534_200


def test_attempt_receipt_preserves_terminal_timestamp_on_failed_rewrite(
    tmp_path: Path,
) -> None:
    config = _service_config(tmp_path)
    receipt = runner._write_attempt_receipt(
        config,
        attempt_id="attempt-1",
        scheduled_expiry_seconds=1_786_532_400,
        status="started",
    )
    failed = runner._write_attempt_receipt(
        config,
        attempt_id="attempt-1",
        scheduled_expiry_seconds=1_786_532_400,
        status="failed",
        expiry_seconds=1_786_532_400,
        error={"error_code": "service_cycle_failed"},
    )
    rewritten = runner._write_attempt_receipt(
        config,
        attempt_id="attempt-1",
        scheduled_expiry_seconds=1_786_532_400,
        status="failed",
        expiry_seconds=1_786_532_400,
        error={"error_code": "service_cycle_failed"},
    )

    assert receipt["status"] == "started"
    assert failed["terminal_at"] == rewritten["terminal_at"]


def test_attempt_receipt_surfaces_posix_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _service_config(tmp_path)
    monkeypatch.setattr(runner.os, "name", "posix")
    monkeypatch.setattr(
        runner,
        "_fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("dir fsync failed")),
    )

    with pytest.raises(OSError, match="dir fsync failed"):
        runner._write_attempt_receipt(
            config,
            attempt_id="attempt-1",
            scheduled_expiry_seconds=1_786_532_400,
            status="started",
        )


@pytest.mark.anyio
async def test_run_service_records_failed_receipt_when_clock_measurement_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _service_config(tmp_path)
    _write_preregistration(tmp_path)

    class DummySession:
        def close(self) -> None:
            return None

    async def stop_after_failure(
        stop_event: asyncio.Event,
        seconds: float,
    ) -> bool:
        del stop_event, seconds
        return True

    monkeypatch.setattr(runner, "verify_paper_only_guard", lambda: None)
    monkeypatch.setattr(runner, "_validate_runtime_identity", lambda config: None)
    monkeypatch.setattr(runner, "public_session", lambda proxy: DummySession())
    monkeypatch.setattr(runner, "PublicSourcesClient", lambda **kwargs: object())
    monkeypatch.setattr(runner, "require_disk_capacity", lambda config: None)
    monkeypatch.setattr(
        runner,
        "_measure_clock_sync",
        lambda config: (_ for _ in ()).throw(RuntimeError("clock failed")),
    )
    monkeypatch.setattr(runner, "_wait_or_stop", stop_after_failure)
    monkeypatch.setattr(runner, "_attempt_id", lambda: "attempt-1")
    monkeypatch.setattr(runner, "_next_capture_expiry_seconds", lambda: 123)

    await runner.run_service(
        config,
        proxy_url=None,
        stop_event=asyncio.Event(),
    )

    receipt_path = (
        config.data_root / "service" / "attempt-receipts" / "attempt-1.json"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["scheduled_expiry_seconds"] == 123
    assert receipt["error"]["error_code"] == "service_cycle_failed"


@pytest.mark.anyio
async def test_run_service_persists_failed_capture_finalization_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorder failure must remain auditable even when no report is built."""

    import src.edge_lab.btc_twap_relative_value_service as service

    config = _service_config(tmp_path)
    _write_preregistration(tmp_path)
    capture_root = config.data_root / "runs" / "1786532400" / "attempt-1"
    capture_config_path = capture_root / "capture-config.json"
    plan = SimpleNamespace(
        expiry_seconds=1_786_532_400,
        capture_root=capture_root,
        capture_config_path=capture_config_path,
        capture_config={
            "schema_version": "edge-lab-forward-capture-config.v1",
            "data_root": str(capture_root),
        },
        duration_seconds=1,
    )
    summary = {
        "schema_version": "btc-twap-compact-forward-capture-summary.v1",
        "capture_error": {
            "error_code": "capture_failed",
            "error_type": "RuntimeError",
        },
        "recorder_leg_count": 2,
        "recorder_leg_failures": [
            {"error_code": "recorder_leg_failed", "error_type": "ConnectionError"},
            {"error_code": "recorder_leg_failed", "error_type": "ConnectionError"},
        ],
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
    }

    class DummySession:
        def close(self) -> None:
            return None

    async def fail_capture(*args: object, **kwargs: object) -> object:
        raise service.CaptureFinalizationError(summary)

    async def stop_after_failure(
        stop_event: asyncio.Event,
        seconds: float,
    ) -> bool:
        del stop_event, seconds
        return True

    monkeypatch.setattr(runner, "verify_paper_only_guard", lambda: None)
    monkeypatch.setattr(runner, "_validate_runtime_identity", lambda config: None)
    monkeypatch.setattr(runner, "public_session", lambda proxy: DummySession())
    monkeypatch.setattr(runner, "PublicSourcesClient", lambda **kwargs: object())
    monkeypatch.setattr(runner, "require_disk_capacity", lambda config: None)
    monkeypatch.setattr(
        runner,
        "_measure_clock_sync",
        lambda config: ClockSyncMeasurement(
            measured_at_raw_ms=1,
            offset_seconds="0",
            uncertainty_seconds="0.001",
        ),
    )
    monkeypatch.setattr(runner, "discover_next_pair", lambda **kwargs: plan)
    monkeypatch.setattr(runner, "load_capture_config", lambda path: object())
    monkeypatch.setattr(runner, "_capture_with_heartbeat", fail_capture)
    monkeypatch.setattr(runner, "_wait_or_stop", stop_after_failure)

    await runner.run_service(
        config,
        proxy_url=None,
        stop_event=asyncio.Event(),
    )

    persisted = json.loads(
        (capture_root / "capture-summary.json").read_text(encoding="utf-8")
    )
    assert persisted == summary
    assert not config.research_root.joinpath("reports").exists()


@pytest.mark.anyio
async def test_v08_clean_capture_succeeds_without_paper_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _service_config(tmp_path)
    _write_preregistration(
        tmp_path,
        document={
            "schema_version": "btc-5m-15m-edge-readiness-preregistration.v08-draft",
        },
    )
    capture_root = config.data_root / "runs" / "1" / "attempt-1"
    capture_config_path = capture_root / "capture-config.json"
    plan = SimpleNamespace(
        expiry_seconds=1_786_532_400,
        duration_seconds=1,
        capture_root=capture_root,
        capture_config_path=capture_config_path,
        capture_config={"clock_sync": None},
    )
    summary = {
        "schema_version": "btc-twap-compact-forward-capture-summary.v1",
        "capture_error": None,
        "recorder_leg_failures": [],
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
    }

    class DummySession:
        def close(self) -> None:
            return None

    async def succeed_capture(*args: object, **kwargs: object):
        return summary, None

    async def stop_after_success(
        stop_event: asyncio.Event,
        seconds: float,
    ) -> bool:
        del seconds
        stop_event.set()
        return True

    def fail_report(*args: object, **kwargs: object) -> None:
        raise AssertionError("v0.8 capture must not build the retired paper report")

    monkeypatch.setattr(runner, "verify_paper_only_guard", lambda: None)
    monkeypatch.setattr(runner, "_validate_runtime_identity", lambda config: None)
    monkeypatch.setattr(runner, "public_session", lambda proxy: DummySession())
    monkeypatch.setattr(runner, "PublicSourcesClient", lambda **kwargs: object())
    monkeypatch.setattr(runner, "require_disk_capacity", lambda config: None)
    monkeypatch.setattr(
        runner,
        "_measure_clock_sync",
        lambda config: ClockSyncMeasurement(
            measured_at_raw_ms=1,
            offset_seconds="0",
            uncertainty_seconds="0.001",
        ),
    )
    monkeypatch.setattr(runner, "discover_next_pair", lambda **kwargs: plan)
    monkeypatch.setattr(runner, "load_capture_config", lambda path: object())
    monkeypatch.setattr(runner, "_capture_with_heartbeat", succeed_capture)
    monkeypatch.setattr(runner, "build_report", fail_report)
    monkeypatch.setattr(runner, "_wait_or_stop", stop_after_success)
    ids = iter(("attempt-1", "attempt-2"))
    monkeypatch.setattr(runner, "_attempt_id", lambda: next(ids))
    monkeypatch.setattr(runner, "_next_capture_expiry_seconds", lambda: 1_786_532_400)

    await runner.run_service(
        config,
        proxy_url=None,
        stop_event=asyncio.Event(),
    )

    receipt_path = config.data_root / "service" / "attempt-receipts" / "attempt-1.json"
    summary_path = capture_root / "capture-summary.json"
    assert receipt_path.is_file(), sorted(p.name for p in (config.data_root / "service" / "attempt-receipts").glob("*.json")) if (config.data_root / "service" / "attempt-receipts").exists() else "no receipts dir"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "succeeded", receipt
    assert summary_path.is_file(), receipt
    persisted = json.loads(summary_path.read_text(encoding="utf-8"))
    assert persisted["capture_error"] is None
    assert not config.research_root.joinpath("reports").exists()
