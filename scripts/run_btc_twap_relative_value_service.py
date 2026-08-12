#!/usr/bin/env python3
"""Run the continuous public-only BTC TWAP paper-validation service."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.build_btc_twap_relative_value_pilot_report import (  # noqa: E402
    build_report,
)
from scripts.build_btc_twap_relative_value_validation_summary import (  # noqa: E402
    build_summary,
)
from src.edge_lab.btc_twap_relative_value_schedule import (  # noqa: E402
    bootstrap_clock_refresh_at_ms,
    next_bootstrap_expiry_seconds,
)
from src.edge_lab.btc_twap_relative_value_service import (  # noqa: E402
    CLOCK_SYNC_SOURCE_CHRONY_AMAZON,
    ClockSyncMeasurement,
    ContinuousServiceConfig,
    ServiceStatus,
    discover_next_pair,
    load_service_config,
    measure_chrony_clock_sync,
    measure_sntp_clock_sync,
    public_session,
    require_disk_capacity,
    run_compact_forward_capture,
    verify_paper_only_guard,
    write_json_document,
    write_service_status,
)
from src.edge_lab.capture_cli import load_capture_config  # noqa: E402
from src.edge_lab.network_safety import safe_error_details  # noqa: E402
from src.edge_lab.sources import PublicSourcesClient  # noqa: E402


class ServiceStopRequested(Exception):
    """The local supervisor requested an orderly capture shutdown."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000)


def _attempt_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def _measure_clock_sync(config: ContinuousServiceConfig) -> ClockSyncMeasurement:
    if config.clock_sync_source == CLOCK_SYNC_SOURCE_CHRONY_AMAZON:
        return measure_chrony_clock_sync()
    return measure_sntp_clock_sync()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _report_paths(config: ContinuousServiceConfig) -> tuple[Path, ...]:
    generated = tuple(sorted((config.research_root / "reports").glob("*.json")))
    unique: dict[Path, None] = {}
    for path in (*config.seed_report_paths, *generated):
        resolved = path.resolve()
        if resolved.is_file():
            unique[resolved] = None
    return tuple(unique)


def _generated_report_paths(config: ContinuousServiceConfig) -> tuple[Path, ...]:
    return tuple(sorted((config.research_root / "reports").glob("*.json")))


def _decision_report_path(
    *,
    plan: Any,
    decision_tau_seconds: int,
    decision_count: int,
) -> Path:
    if decision_count == 1:
        return plan.report_path
    return plan.report_path.with_name(
        f"{plan.report_path.stem}_TAU{decision_tau_seconds}.json"
    )


def _history_roots(
    config: ContinuousServiceConfig,
    *,
    exclude: Path,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    for report_path in _report_paths(config):
        document = _read_json(report_path)
        inputs = document.get("inputs") if isinstance(document, dict) else None
        root_value = inputs.get("capture_root") if isinstance(inputs, dict) else None
        if not isinstance(root_value, str):
            continue
        root = Path(root_value).resolve()
        if root != exclude and root.is_dir():
            roots.append(root)
    return tuple(dict.fromkeys(roots))[-config.max_history_roots :]


def _latest_summary(config: ContinuousServiceConfig) -> dict[str, Any] | None:
    return _read_json(config.research_root / "VALIDATION_SUMMARY.json")


def _status(
    config: ContinuousServiceConfig,
    *,
    phase: str,
    plan: Any = None,
    last_error: Mapping[str, Any] | None = None,
) -> ServiceStatus:
    summary = _latest_summary(config)
    return ServiceStatus(
        phase=phase,
        heartbeat_at=_utc_now(),
        pid=os.getpid(),
        child_pid=None,
        current_expiry_seconds=(None if plan is None else int(plan.expiry_seconds)),
        current_capture_root=(None if plan is None else str(plan.capture_root)),
        completed_report_count=len(_generated_report_paths(config)),
        latest_summary_path=(
            None
            if summary is None
            else str((config.research_root / "VALIDATION_SUMMARY.json").resolve())
        ),
        latest_classification=(
            None if summary is None else str(summary.get("classification"))
        ),
        last_error=last_error,
    )


def _emit_status(
    config: ContinuousServiceConfig,
    *,
    phase: str,
    plan: Any = None,
    last_error: Mapping[str, Any] | None = None,
) -> None:
    write_service_status(
        config.data_root / "service" / "status.json",
        _status(config, phase=phase, plan=plan, last_error=last_error),
    )


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def _capture_with_heartbeat(
    *,
    config: ContinuousServiceConfig,
    capture_config: Any,
    plan: Any,
    proxy_url: str | None,
    stop_event: asyncio.Event,
    clock_refresh_at_ms: int | None = None,
) -> tuple[dict[str, Any], ClockSyncMeasurement | None]:
    task = asyncio.create_task(
        run_compact_forward_capture(
            capture_config,
            duration_seconds=plan.duration_seconds,
            proxy_url=proxy_url,
        ),
        name="btc-twap-relative-value-capture",
    )
    refresh_task: asyncio.Task[ClockSyncMeasurement] | None = None
    initial_clock_sync = getattr(capture_config, "clock_sync", None)
    refresh_context = {
        "scheduled_at_ms": clock_refresh_at_ms,
        "initial_evidence": (
            dict(initial_clock_sync)
            if isinstance(initial_clock_sync, Mapping)
            else None
        ),
    }
    if clock_refresh_at_ms is not None:

        async def refresh_clock() -> ClockSyncMeasurement:
            delay_seconds = max(
                0.0,
                (clock_refresh_at_ms - _epoch_ms()) / 1_000,
            )
            await asyncio.sleep(delay_seconds)
            return await asyncio.to_thread(_measure_clock_sync, config)

        refresh_task = asyncio.create_task(
            refresh_clock(),
            name="btc-twap-bootstrap-clock-refresh",
        )
    try:
        while not task.done():
            _emit_status(config, phase="capturing", plan=plan)
            if await _wait_or_stop(stop_event, config.status_interval_seconds):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise ServiceStopRequested
        capture_summary = await task
        refreshed_clock_sync: ClockSyncMeasurement | None = None
        if refresh_task is not None:
            if refresh_task.done():
                try:
                    refreshed_clock_sync = refresh_task.result()
                except Exception as exc:
                    capture_summary["clock_sync_refresh"] = {
                        **refresh_context,
                        "status": "failed",
                        "error": safe_error_details(
                            exc,
                            code="clock_sync_refresh_failed",
                        ),
                    }
                else:
                    capture_summary["clock_sync_refresh"] = {
                        **refresh_context,
                        "status": "success",
                        "evidence": refreshed_clock_sync.to_document(),
                    }
            else:
                capture_summary["clock_sync_refresh"] = {
                    **refresh_context,
                    "status": "not_reached",
                }
        return capture_summary, refreshed_clock_sync
    finally:
        tasks_to_join: list[asyncio.Task[Any]] = [task]
        if not task.done():
            task.cancel()
        if refresh_task is not None:
            tasks_to_join.append(refresh_task)
            if not refresh_task.done():
                refresh_task.cancel()
        await asyncio.gather(*tasks_to_join, return_exceptions=True)


def _rebuild_summary(config: ContinuousServiceConfig) -> dict[str, Any]:
    reports = _report_paths(config)
    summary = build_summary(reports)
    write_json_document(config.research_root / "VALIDATION_SUMMARY.json", summary)
    return summary


async def run_service(
    config: ContinuousServiceConfig,
    *,
    proxy_url: str | None,
    stop_event: asyncio.Event,
) -> None:
    verify_paper_only_guard()
    config.data_root.mkdir(parents=True, exist_ok=True)
    config.research_root.mkdir(parents=True, exist_ok=True)
    session = public_session(proxy_url)
    sources = PublicSourcesClient(
        session=session,
        timeout=20,
        retries=2,
        rate_per_second=8,
        burst=2,
        min_interval_seconds=0.05,
    )
    _emit_status(config, phase="starting")
    bootstrap_required = True
    try:
        while not stop_event.is_set():
            plan = None
            try:
                require_disk_capacity(config)
                _emit_status(config, phase="measuring_clock")
                clock_sync = await asyncio.to_thread(_measure_clock_sync, config)
                _emit_status(config, phase="discovering")
                plan = await asyncio.to_thread(
                    discover_next_pair,
                    session=session,
                    source_client=sources,
                    config=config,
                    now_ms=_epoch_ms(),
                    attempt_id=_attempt_id(),
                    clock_sync=clock_sync,
                    expiry_seconds=(
                        next_bootstrap_expiry_seconds(_epoch_ms())
                        if bootstrap_required
                        else None
                    ),
                )
                plan.capture_root.mkdir(parents=True, exist_ok=False)
                write_json_document(plan.capture_config_path, plan.capture_config)
                capture_config = load_capture_config(plan.capture_config_path)
                capture_summary, refreshed_clock_sync = await _capture_with_heartbeat(
                    config=config,
                    capture_config=capture_config,
                    plan=plan,
                    proxy_url=proxy_url,
                    stop_event=stop_event,
                    clock_refresh_at_ms=(
                        bootstrap_clock_refresh_at_ms(plan.expiry_seconds)
                        if bootstrap_required
                        else None
                    ),
                )
                if refreshed_clock_sync is not None:
                    refreshed_capture_config = dict(plan.capture_config)
                    refreshed_capture_config["clock_sync"] = (
                        refreshed_clock_sync.to_document()
                    )
                    write_json_document(
                        plan.capture_config_path,
                        refreshed_capture_config,
                    )
                write_json_document(
                    plan.capture_root / "capture-summary.json",
                    capture_summary,
                )
                _emit_status(config, phase="building_report", plan=plan)
                histories = _history_roots(
                    config,
                    exclude=plan.capture_root,
                )
                for decision_tau_seconds in config.decision_tau_seconds:
                    report = await asyncio.to_thread(
                        build_report,
                        capture_root=plan.capture_root,
                        predictor_root=plan.capture_root,
                        capture_config_path=plan.capture_config_path,
                        preregistration_path=config.preregistration_path,
                        history_roots=histories,
                        decision_tau_seconds=decision_tau_seconds,
                    )
                    write_json_document(
                        _decision_report_path(
                            plan=plan,
                            decision_tau_seconds=decision_tau_seconds,
                            decision_count=len(config.decision_tau_seconds),
                        ),
                        report,
                    )
                await asyncio.to_thread(_rebuild_summary, config)
                _emit_status(config, phase="cycle_complete", plan=plan)
                bootstrap_required = False
            except ServiceStopRequested:
                break
            except BaseException as exc:
                error = safe_error_details(exc, code="service_cycle_failed")
                _emit_status(
                    config,
                    phase="error_wait",
                    plan=plan,
                    last_error=error,
                )
                if await _wait_or_stop(stop_event, config.retry_seconds):
                    break
    finally:
        session.close()
        _emit_status(config, phase="stopped")


@contextmanager
def _exclusive_lock(config: ContinuousServiceConfig) -> Iterator[None]:
    service_dir = config.data_root / "service"
    service_dir.mkdir(parents=True, exist_ok=True)
    path = service_dir / "service.lock"
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another BTC TWAP service owns this data root") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


async def _async_main(
    config: ContinuousServiceConfig,
    *,
    proxy_url: str | None,
) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for candidate in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(candidate, stop_event.set)
            installed.append(candidate)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await run_service(config, proxy_url=proxy_url, stop_event=stop_event)
    finally:
        for candidate in installed:
            loop.remove_signal_handler(candidate)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--proxy")
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_service_config(args.config)
    verify_paper_only_guard()
    public_session(args.proxy).close()
    require_disk_capacity(config)
    if args.validate_only:
        print(
            json.dumps(
                {
                    "schema_version": "btc-twap-relative-value-service-validation.v1",
                    "valid": True,
                    "paper_only": True,
                    "public_only": True,
                    "new_orders_disabled": True,
                    "authenticated_endpoints_used": 0,
                    "orders_submitted": 0,
                    "data_root": str(config.data_root),
                    "research_root": str(config.research_root),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    with _exclusive_lock(config):
        asyncio.run(_async_main(config, proxy_url=args.proxy))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
