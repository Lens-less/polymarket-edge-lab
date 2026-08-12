"""CLI for the credential-free dynamic short-crypto capture sidecar."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import signal
import stat
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

import requests

from .capture_cli import _local_proxy_mapping
from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .data_store import canonical_json_bytes
from .dynamic_short_crypto_service import (
    DynamicShortCryptoService,
    DynamicShortCryptoServiceConfig,
    LifecycleRemediationPlan,
    freeze_finalized_discovery_paths,
)
from .network_safety import configure_public_session
from .recorder import DefaultWebSocketFactory
from .sources import PublicSourcesClient


STATUS_SCHEMA_VERSION = "edge-lab-dynamic-short-crypto-cli-status.v1"
RESTART_RECOVERY = "finalized_only_reducer_v1"


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


def _load_lifecycle_remediation_plan(
    path: Path,
) -> LifecycleRemediationPlan:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("remediation plan must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("remediation plan does not exist") from exc
    mode = resolved.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("remediation plan must be a regular file")
    if stat.S_IMODE(mode) != 0o444:
        raise ValueError("remediation plan must have exact mode 0444")
    raw = resolved.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("remediation plan is not canonical JSON") from exc
    if (
        not isinstance(document, dict)
        or canonical_json_bytes(document) + b"\n" != raw
    ):
        raise ValueError("remediation plan is not canonical JSON")
    plan = LifecycleRemediationPlan.from_document(document)
    if resolved.name != (
        f"lifecycle-remediation-plan-{plan.plan_id}.json"
    ):
        raise ValueError(
            "remediation plan filename must contain its exact plan_id"
        )
    return plan


def _service_config(args: argparse.Namespace) -> DynamicShortCryptoServiceConfig:
    duration = args.duration
    if duration is not None and (
        not math.isfinite(duration) or duration <= 0
    ):
        raise ValueError("duration must be positive and finite")
    inferred_rtds = args.discovery_dir.parent / "rtds_ws"
    rtds_manifest_dir = (
        args.rtds_manifest_dir
        if args.rtds_manifest_dir is not None
        else (inferred_rtds if inferred_rtds.is_dir() else None)
    )
    return DynamicShortCryptoServiceConfig(
        discovery_dir=args.discovery_dir,
        output_root=args.data_root,
        rtds_manifest_dir=rtds_manifest_dir,
        subscribe_lead_ms=int(args.subscribe_lead_seconds * 1_000),
        scan_interval_seconds=args.scan_interval,
        gamma_poll_interval_seconds=args.gamma_poll_interval,
        settlement_timeout_ms=int(args.settlement_timeout * 1_000),
        close_liveness_tolerance_ms=int(
            args.close_liveness_tolerance * 1_000
        ),
        clob_snapshot_interval_seconds=args.clob_snapshot_interval,
        max_records_per_batch=args.max_records_per_batch,
        max_assets_per_group=args.max_assets_per_group,
        recovery_cpu_ratio=args.recovery_cpu_ratio,
        lifecycle_remediation_plan=(
            None
            if args.lifecycle_remediation_plan is None
            else _load_lifecycle_remediation_plan(
                args.lifecycle_remediation_plan
            )
        ),
    )


def _isolated_run_config(
    config: DynamicShortCryptoServiceConfig,
) -> DynamicShortCryptoServiceConfig:
    timestamp = (
        datetime.now(timezone.utc)
        .strftime("%Y%m%dT%H%M%S.%fZ")
    )
    runs_root = config.output_root / "runs"
    prior_run_roots = (
        tuple(
            sorted(
                (
                    path.resolve()
                    for path in runs_root.iterdir()
                    if path.is_dir() and not path.is_symlink()
                ),
                key=str,
            )
        )
        if runs_root.is_dir()
        else ()
    )
    run_root = runs_root / f"{timestamp}-{uuid.uuid4().hex}"
    recovery_snapshot_path = (
        config.recovery_snapshot_path
        if config.recovery_snapshot_path is not None
        else (
            config.output_root
            / "service"
            / "restart-recovery-snapshot-v2.json"
        )
    )
    return replace(
        config,
        output_root=run_root,
        recovery_run_roots=prior_run_roots,
        recovery_snapshot_path=recovery_snapshot_path,
    )


@contextmanager
def _exclusive_service_lock(output_root: Path) -> Iterator[None]:
    service_dir = output_root / "service"
    service_dir.mkdir(parents=True, exist_ok=True)
    lock_path = service_dir / "dynamic-short-crypto.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "another dynamic short-crypto capture process owns the data root"
            ) from exc
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


async def run_dynamic_short_crypto_capture(
    config: DynamicShortCryptoServiceConfig,
    *,
    proxy_url: str | None,
    duration_seconds: float | None,
) -> dict[str, Any]:
    """Run one public-only sidecar instance and close it on SIGTERM."""

    _verify_live_order_guard()
    proxy_mapping = _local_proxy_mapping(proxy_url)
    session = requests.Session()
    session.trust_env = False
    session.proxies.update(proxy_mapping)
    configure_public_session(session)
    sources = PublicSourcesClient(
        session=session,
        timeout=10,
        retries=1,
        rate_per_second=8,
        burst=2,
        min_interval_seconds=0.05,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    signal_handler_installed = False
    try:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        signal_handler_installed = True
    except (NotImplementedError, RuntimeError):
        pass
    try:
        service = DynamicShortCryptoService(
            config,
            source_client=sources,
            websocket_factory=DefaultWebSocketFactory(
                proxy_url=proxy_url
            ),
        )
        status = await service.run(
            duration_seconds=duration_seconds,
            stop_event=stop_event,
        )
    finally:
        if signal_handler_installed:
            loop.remove_signal_handler(signal.SIGTERM)
        session.close()
    return {
        **status,
        "schema_version": STATUS_SCHEMA_VERSION,
        "data_root": str(config.output_root),
        "public_only": True,
        "network_route": (
            "direct" if proxy_url is None else "explicit_loopback_proxy"
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover strict BTC/ETH 5m/15m announcements and dynamically "
            "record their public CLOB/Gamma data without credentials or orders."
        )
    )
    parser.add_argument("--discovery-dir", type=Path, required=True)
    parser.add_argument("--rtds-manifest-dir", type=Path)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--proxy")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--subscribe-lead-seconds", type=float, default=300)
    parser.add_argument("--scan-interval", type=float, default=30)
    parser.add_argument("--gamma-poll-interval", type=float, default=300)
    parser.add_argument("--settlement-timeout", type=float, default=3_600)
    parser.add_argument(
        "--close-liveness-tolerance",
        type=float,
        default=30,
    )
    parser.add_argument(
        "--clob-snapshot-interval",
        type=float,
        default=30,
    )
    parser.add_argument("--max-records-per-batch", type=int, default=5_000)
    parser.add_argument(
        "--max-assets-per-group",
        type=int,
        default=2,
        help=(
            "maximum public CLOB token assets per immutable worker scope"
        ),
    )
    parser.add_argument(
        "--recovery-cpu-ratio",
        type=float,
        default=0.4,
        help=(
            "maximum sustained process CPU ratio for a full snapshot-miss "
            "recovery; exact snapshot hits are content-hash bounded"
        ),
    )
    parser.add_argument(
        "--lifecycle-remediation-plan",
        type=Path,
        help=(
            "one exact 0444 content-addressed append-only remediation plan"
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "verify paths, manifests, proxy policy, and order guard without "
            "network access or writes"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _verify_live_order_guard()
    _local_proxy_mapping(args.proxy)
    config = _service_config(args)
    if args.validate_only:
        finalized = freeze_finalized_discovery_paths(
            config.discovery_dir
        )
        result = {
            "schema_version": STATUS_SCHEMA_VERSION,
            "valid": True,
            "public_only": True,
            "new_orders_disabled": True,
            "network_requests": False,
            "writes_performed": False,
            "manifest_validation_enabled": True,
            "finalized_discovery_file_count": len(finalized),
            "input_output_separate": True,
            "restart_recovery": RESTART_RECOVERY,
            "run_isolation": "unique_append_only_run_root",
        }
    else:
        with _exclusive_service_lock(config.output_root):
            run_config = _isolated_run_config(config)
            result = asyncio.run(
                run_dynamic_short_crypto_capture(
                    run_config,
                    proxy_url=args.proxy,
                    duration_seconds=args.duration,
                )
            )
            result["base_data_root"] = str(config.output_root)
            result["run_isolation"] = "unique_append_only_run_root"
            result["restart_recovery"] = RESTART_RECOVERY
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
