#!/usr/bin/env python3
"""Run a regime-agnostic BTC 5m/15m public raw collector."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import signal
import tempfile
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(PROJECT_ROOT))

from src.edge_lab.btc_twap_relative_value_service import (
    _capture_expiry_seconds,
    fetch_gamma_market_by_slug,
    public_session,
    require_disk_capacity,
)
from src.edge_lab.capture_cli import (
    ForwardCaptureConfig,
    ForwardCaptureFinalizationError,
    load_capture_config,
    run_forward_capture,
)
from src.edge_lab.network_safety import safe_error_details
from src.edge_lab.settlement_regime import (
    RegimeClassification,
    SettlementRegimeRegistry,
    canonicalize_resolution_source,
    load_settlement_regime_registry,
)
from src.edge_lab.sources import PublicSourcesClient

CONFIG_SCHEMA_VERSION = "btc-regime-agnostic-collector.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000)


def _attempt_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _slug_window(slug: str) -> tuple[str, int, int]:
    parts = slug.split("-")
    if len(parts) != 4 or parts[0:2] != ["btc", "updown"]:
        raise ValueError("unsupported BTC Up/Down slug")
    horizon = parts[2]
    opens_at = int(parts[3])
    duration = {"5m": 300, "15m": 900}[horizon]
    return horizon, opens_at * 1_000, (opens_at + duration) * 1_000


def _json_string_list(value: Any, *, field: str) -> tuple[str, ...]:
    decoded = value
    if isinstance(value, str):
        decoded = json.loads(value)
    if (
        not isinstance(decoded, Sequence)
        or isinstance(decoded, (str, bytes, bytearray))
        or any(not isinstance(item, str) or not item for item in decoded)
    ):
        raise ValueError(f"{field} must contain non-empty strings")
    return tuple(decoded)


def _window_seconds(source: str) -> int | None:
    if "-30s-" in source:
        return 30
    if "-60s-" in source:
        return 60
    return None


@dataclass(frozen=True)
class RawCollectorConfig:
    data_root: Path
    status_path: Path | None = None
    registry_path: Path | None = None
    regime_registry_path: Path | None = None
    regime_status_path: Path | None = None
    settlement_grace_seconds: int = 360
    retry_seconds: int = 30
    status_interval_seconds: int = 15
    minimum_free_disk_bytes: int = 12 * 1024**3
    evidence_track_id: str = "btc-rawcap"

    def __post_init__(self) -> None:
        data_root = self.data_root.expanduser().resolve()
        status_path = (
            self.status_path.expanduser().resolve()
            if self.status_path is not None
            else (data_root / "service" / "status.json").resolve()
        )
        registry_path = self.registry_path or self.regime_registry_path
        if registry_path is None:
            raise ValueError("registry_path or regime_registry_path is required")
        object.__setattr__(self, "data_root", data_root)
        object.__setattr__(self, "status_path", status_path)
        object.__setattr__(
            self,
            "registry_path",
            registry_path.expanduser().resolve(),
        )
        object.__setattr__(
            self,
            "regime_registry_path",
            (
                self.registry_path
                if self.regime_registry_path is None
                else self.regime_registry_path
            ).expanduser().resolve(),
        )
        regime_status_path = self.regime_status_path
        if regime_status_path is None:
            regime_root = data_root.parent if data_root.name == "data" else data_root
            regime_status_path = regime_root / "monitor" / "regime-latest.json"
        object.__setattr__(
            self,
            "regime_status_path",
            regime_status_path.expanduser().resolve(),
        )
        for name in (
            "settlement_grace_seconds",
            "retry_seconds",
            "status_interval_seconds",
            "minimum_free_disk_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.evidence_track_id, str) or not self.evidence_track_id:
            raise ValueError("evidence_track_id must be a non-empty string")


@dataclass(frozen=True)
class RawCaptureDiscovery:
    expiry_seconds: int
    gamma_5: Mapping[str, Any]
    clob_5: Mapping[str, Any]
    gamma_15: Mapping[str, Any]
    clob_15: Mapping[str, Any]
    regime: RegimeClassification


@dataclass(frozen=True)
class RawCapturePlan:
    expiry_seconds: int
    duration_seconds: int
    capture_root: Path
    capture_config_path: Path
    capture_config: dict[str, Any]
    classification: RegimeClassification | None = None
    forward_config: ForwardCaptureConfig | None = None
    capture_document: Mapping[str, Any] | None = None


def load_collector_config(path: Path) -> RawCollectorConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TypeError("collector config must be an object")
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported collector config schema")
    data_root = Path(str(raw["data_root"]))
    status_path_value = raw.get("status_path")
    status_path = (
        Path(str(status_path_value))
        if isinstance(status_path_value, str) and status_path_value.strip()
        else data_root / "service" / "status.json"
    )
    return RawCollectorConfig(
        data_root=data_root,
        status_path=status_path,
        registry_path=Path(str(raw.get("registry_path", raw.get("regime_registry_path")))),
        regime_registry_path=(
            Path(str(raw["regime_registry_path"]))
            if raw.get("regime_registry_path") is not None
            else None
        ),
        regime_status_path=(
            Path(str(raw["regime_status_path"]))
            if raw.get("regime_status_path") is not None
            else None
        ),
        settlement_grace_seconds=int(raw.get("settlement_grace_seconds", 360)),
        retry_seconds=int(raw.get("retry_seconds", 30)),
        status_interval_seconds=int(raw.get("status_interval_seconds", 15)),
        minimum_free_disk_bytes=int(raw.get("minimum_free_disk_bytes", 12 * 1024**3)),
        evidence_track_id=str(raw.get("evidence_track_id", "btc-rawcap")),
    )


def _target_document(
    gamma: Mapping[str, Any],
    clob: Mapping[str, Any],
    *,
    qualification_status: str,
) -> dict[str, Any]:
    slug = str(gamma["slug"])
    horizon, opens_at_ms, closes_at_ms = _slug_window(slug)
    token_ids = _json_string_list(gamma["clobTokenIds"], field="clobTokenIds")
    source = str(gamma.get("resolutionSource", ""))
    try:
        canonical_source = canonicalize_resolution_source(source)
    except ValueError:
        canonical_source = None
    return {
        "horizon": horizon,
        "market_slug": slug,
        "market_id": str(gamma["id"]),
        "condition_id": str(gamma["conditionId"]).lower(),
        "opens_at_ms": opens_at_ms,
        "closes_at_ms": closes_at_ms,
        "duration_seconds": (closes_at_ms - opens_at_ms) // 1_000,
        "resolution_source_raw": source,
        "resolution_source_canonical": canonical_source,
        "rules_text_hash": hashlib.sha256(
            str(gamma.get("description", "")).encode("utf-8")
        ).hexdigest(),
        "gamma_payload_hash": hashlib.sha256(
            json.dumps(dict(gamma), sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "token_ids": list(token_ids),
        "twap_window_seconds": _window_seconds(source),
        "tick_size": str(clob.get("mts")),
        "minimum_order_size": str(clob.get("mos")),
        "accepting_orders": clob.get("ao"),
        "regime_classification": qualification_status,
    }


def build_capture_plan(
    *,
    config: RawCollectorConfig,
    discovery: RawCaptureDiscovery,
    registry: SettlementRegimeRegistry,
    now_ms: int,
    attempt_id: str,
) -> RawCapturePlan:
    capture_root = (
        config.data_root / "runs" / str(discovery.expiry_seconds) / attempt_id
    ).resolve()
    duration_seconds = max(
        60,
        math.ceil(
            (
                discovery.expiry_seconds * 1_000
                + config.settlement_grace_seconds * 1_000
                - now_ms
            )
            / 1_000
        ),
    )
    capture_config = {
        "schema_version": "edge-lab-forward-capture-config.v1",
        "data_root": str(capture_root),
        "capture_started_at_ms": now_ms,
        "evidence_track_id": config.evidence_track_id,
        "asset_ids": list(
            _json_string_list(
                discovery.gamma_15["clobTokenIds"],
                field="15m.clobTokenIds",
            )
            + _json_string_list(
                discovery.gamma_5["clobTokenIds"],
                field="5m.clobTokenIds",
            )
        ),
        "condition_ids": [
            str(discovery.gamma_15["conditionId"]).lower(),
            str(discovery.gamma_5["conditionId"]).lower(),
        ],
        "rule_market_ids": [
            str(discovery.gamma_15["id"]),
            str(discovery.gamma_5["id"]),
        ],
        "rtds_subscriptions": [
            {"topic": "crypto_prices", "type": "update"},
            {
                "topic": "crypto_prices_twap_thirty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            },
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            },
        ],
        "snapshot_intervals": {"gamma": 30, "clob": 5, "rules": 30},
        "checkpoint_every_records": 10_000,
        "max_records_per_batch": 10_000,
        "gamma_page_limit": 10,
        "gamma_max_pages": 1,
        "reward_page_limit": 1,
        "reward_max_pages": 1,
        "qualification_status": discovery.regime.qualification_status,
        "regime_classification": discovery.regime.to_document(),
        "targets": [
            _target_document(
                discovery.gamma_15,
                discovery.clob_15,
                qualification_status=discovery.regime.qualification_status,
            ),
            _target_document(
                discovery.gamma_5,
                discovery.clob_5,
                qualification_status=discovery.regime.qualification_status,
            ),
        ],
        "registry_sha256": registry.sha256,
    }
    forward_config = ForwardCaptureConfig(
        data_root=capture_root,
        asset_ids=tuple(capture_config["asset_ids"]),
        condition_ids=tuple(capture_config["condition_ids"]),
        rule_market_ids=tuple(capture_config["rule_market_ids"]),
        rtds_subscriptions=tuple(capture_config["rtds_subscriptions"]),
        snapshot_intervals=dict(capture_config["snapshot_intervals"]),
        checkpoint_every_records=int(capture_config["checkpoint_every_records"]),
        max_records_per_batch=int(capture_config["max_records_per_batch"]),
        gamma_page_limit=int(capture_config["gamma_page_limit"]),
        gamma_max_pages=int(capture_config["gamma_max_pages"]),
        reward_page_limit=int(capture_config["reward_page_limit"]),
        reward_max_pages=int(capture_config["reward_max_pages"]),
        targets=tuple(capture_config["targets"]),
        capture_started_at_ms=now_ms,
        evidence_track_id=config.evidence_track_id,
    )
    capture_document = {
        "schema_version": "btc-regime-agnostic-capture-plan.v1",
        "qualification_status": discovery.regime.qualification_status,
        "regime_destination": discovery.regime.destination,
        "reason_codes": list(discovery.regime.reason_codes),
        "regime_id": discovery.regime.regime_id,
        "non_canonical": True,
        "qualified_evidence": False,
        "resolution_source_canonical": dict(discovery.regime.normalized_sources),
        "targets": capture_config["targets"],
    }
    return RawCapturePlan(
        expiry_seconds=discovery.expiry_seconds,
        duration_seconds=duration_seconds,
        capture_root=capture_root,
        capture_config_path=capture_root / "capture-config.json",
        capture_config=capture_config,
        classification=discovery.regime,
        forward_config=forward_config,
        capture_document=capture_document,
    )


def build_raw_capture_plan(
    config: RawCollectorConfig,
    *,
    registry: SettlementRegimeRegistry,
    now_ms: int,
    attempt_id: str,
    gamma_5: Mapping[str, Any],
    clob_5: Mapping[str, Any],
    gamma_15: Mapping[str, Any],
    clob_15: Mapping[str, Any],
) -> RawCapturePlan:
    discovery = RawCaptureDiscovery(
        expiry_seconds=_capture_expiry_seconds(now_ms, None),
        gamma_5=gamma_5,
        clob_5=clob_5,
        gamma_15=gamma_15,
        clob_15=clob_15,
        regime=registry.classify(
            {
                "5m": str(gamma_5.get("resolutionSource", "")),
                "15m": str(gamma_15.get("resolutionSource", "")),
            }
        ),
    )
    return build_capture_plan(
        config=config,
        discovery=discovery,
        registry=registry,
        now_ms=now_ms,
        attempt_id=attempt_id,
    )


def _status(
    *,
    phase: str,
    completed_capture_count: int,
    current_capture_root: str | None,
    last_error: Mapping[str, Any] | None = None,
    latest_classification: Mapping[str, Any] | None = None,
    quarantine_count: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version": "btc-regime-agnostic-collector-status.v1",
        "phase": phase,
        "heartbeat_at": _utc_now(),
        "pid": os.getpid(),
        "current_capture_root": current_capture_root,
        "completed_capture_count": completed_capture_count,
        "latest_classification": latest_classification,
        "quarantine_count": quarantine_count,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
        "last_error": last_error,
    }


def _write_status(
    config: RawCollectorConfig,
    *,
    phase: str,
    completed_capture_count: int,
    current_capture_root: str | None,
    last_error: Mapping[str, Any] | None = None,
    latest_classification: Mapping[str, Any] | None = None,
    quarantine_count: int = 0,
) -> None:
    _write_json(
        config.status_path,
        _status(
            phase=phase,
            completed_capture_count=completed_capture_count,
            current_capture_root=current_capture_root,
            last_error=last_error,
            latest_classification=latest_classification,
            quarantine_count=quarantine_count,
        ),
    )


def update_regime_progress(
    previous: Mapping[str, Any],
    *,
    classification: RegimeClassification,
    market_slugs: tuple[str, ...],
    observed_at: str,
) -> dict[str, Any]:
    """Advance a compact progress document without affecting capture eligibility."""

    quarantined = classification.qualification_status == "quarantine"
    previous_count = previous.get("quarantine_count", 0)
    previous_streak = previous.get("consecutive_rejections", 0)
    quarantine_count = int(previous_count) + int(quarantined)
    consecutive = int(previous_streak) + 1 if quarantined else 0
    affected = tuple(
        dict.fromkeys(
            (
                *(str(value) for value in previous.get("affected_market_slugs", ())),
                *(str(value) for value in market_slugs),
            )
        )
    )[-20:]
    return {
        "schema_version": "btc-settlement-regime-progress.v1",
        "qualification_status": classification.qualification_status,
        "expected_regime": None,
        "observed_regime": classification.regime_id,
        "destination": classification.destination,
        "reason_codes": list(classification.reason_codes),
        "first_seen_at": (
            previous.get("first_seen_at") if quarantined else None
        )
        or (observed_at if quarantined else None),
        "last_observed_at": observed_at,
        "last_success_at": (
            observed_at if not quarantined else previous.get("last_success_at")
        ),
        "consecutive_rejections": consecutive,
        "quarantine_count": quarantine_count,
        "affected_market_slugs": list(affected if quarantined else ()),
    }


async def _run_capture_with_heartbeat(
    *,
    config: RawCollectorConfig,
    plan: RawCapturePlan,
    stop_event: asyncio.Event,
    completed_capture_count: int,
    quarantine_count: int = 0,
    runner: Callable[..., Awaitable[dict[str, Any]]] = run_forward_capture,
) -> dict[str, Any]:
    """Keep progress fresh and persist a final summary on every capture exit."""

    if plan.forward_config is None:
        raise ValueError("raw capture plan has no forward capture configuration")
    task = asyncio.create_task(
        runner(plan.forward_config, duration_seconds=plan.duration_seconds),
        name="btc-regime-agnostic-forward-capture",
    )
    try:
        while not task.done():
            try:
                summary = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=config.status_interval_seconds,
                )
            except TimeoutError:
                if stop_event.is_set():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    raise asyncio.CancelledError
                _write_status(
                    config,
                    phase="capturing",
                    completed_capture_count=completed_capture_count,
                    current_capture_root=str(plan.capture_root),
                    latest_classification=(
                        plan.classification.to_document()
                        if plan.classification is not None
                        else None
                    ),
                    quarantine_count=quarantine_count,
                )
                continue
            else:
                _write_json(plan.capture_root / "capture-summary.json", summary)
                return summary
        summary = await task
        _write_json(plan.capture_root / "capture-summary.json", summary)
        return summary
    except ForwardCaptureFinalizationError as exc:
        _write_json(plan.capture_root / "capture-summary.json", exc.summary)
        raise


async def run_collector(config: RawCollectorConfig) -> None:
    registry = load_settlement_regime_registry(config.registry_path)
    session = public_session(None)
    sources = PublicSourcesClient(
        session=session,
        timeout=20,
        retries=2,
        rate_per_second=8,
        burst=2,
        min_interval_seconds=0.05,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for candidate in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(candidate, stop_event.set)
            installed.append(candidate)
        except (NotImplementedError, RuntimeError):
            pass
    completed = 0
    regime_progress: dict[str, Any] = {}
    try:
        while not stop_event.is_set():
            try:
                require_disk_capacity(config)
                now_ms = _epoch_ms()
                expiry_seconds = _capture_expiry_seconds(now_ms, None)
                gamma_15 = fetch_gamma_market_by_slug(
                    session,
                    f"btc-updown-15m-{expiry_seconds - 900}",
                )
                gamma_5 = fetch_gamma_market_by_slug(
                    session,
                    f"btc-updown-5m-{expiry_seconds - 300}",
                )
                clob_15 = dict(
                    sources.clob_market(str(gamma_15["conditionId"])).value.raw
                )
                clob_5 = dict(
                    sources.clob_market(str(gamma_5["conditionId"])).value.raw
                )
                discovery = RawCaptureDiscovery(
                    expiry_seconds=expiry_seconds,
                    gamma_5=gamma_5,
                    clob_5=clob_5,
                    gamma_15=gamma_15,
                    clob_15=clob_15,
                    regime=registry.classify(
                        {
                            "5m": str(gamma_5.get("resolutionSource", "")),
                            "15m": str(gamma_15.get("resolutionSource", "")),
                        }
                    ),
                )
                regime_progress = update_regime_progress(
                    regime_progress,
                    classification=discovery.regime,
                    market_slugs=(
                        str(gamma_5.get("slug", "")),
                        str(gamma_15.get("slug", "")),
                    ),
                    observed_at=_utc_now(),
                )
                assert config.regime_status_path is not None
                _write_json(config.regime_status_path, regime_progress)
                plan = build_capture_plan(
                    config=config,
                    discovery=discovery,
                    registry=registry,
                    now_ms=now_ms,
                    attempt_id=_attempt_id(),
                )
                plan.capture_root.mkdir(parents=True, exist_ok=False)
                _write_json(plan.capture_config_path, plan.capture_config)
                _write_status(
                    config,
                    phase="capturing",
                    completed_capture_count=completed,
                    current_capture_root=str(plan.capture_root),
                    latest_classification=discovery.regime.to_document(),
                    quarantine_count=int(regime_progress["quarantine_count"]),
                )
                loaded_capture_config = load_capture_config(plan.capture_config_path)
                plan = RawCapturePlan(
                    expiry_seconds=plan.expiry_seconds,
                    duration_seconds=plan.duration_seconds,
                    capture_root=plan.capture_root,
                    capture_config_path=plan.capture_config_path,
                    capture_config=plan.capture_config,
                    classification=plan.classification,
                    forward_config=loaded_capture_config,
                    capture_document=plan.capture_document,
                )
                await _run_capture_with_heartbeat(
                    config=config,
                    plan=plan,
                    stop_event=stop_event,
                    completed_capture_count=completed,
                    quarantine_count=int(regime_progress["quarantine_count"]),
                )
                completed += 1
                _write_status(
                    config,
                    phase="cycle_complete",
                    completed_capture_count=completed,
                    current_capture_root=str(plan.capture_root),
                    latest_classification=discovery.regime.to_document(),
                    quarantine_count=int(regime_progress["quarantine_count"]),
                )
            except Exception as exc:  # noqa: BLE001
                _write_status(
                    config,
                    phase="error_wait",
                    completed_capture_count=completed,
                    current_capture_root=None,
                    last_error=safe_error_details(exc, code="collector_cycle_failed"),
                    quarantine_count=int(regime_progress.get("quarantine_count", 0)),
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=config.retry_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        session.close()
        for candidate in installed:
            loop.remove_signal_handler(candidate)
        _write_status(
            config,
            phase="stopped",
            completed_capture_count=completed,
            current_capture_root=None,
            quarantine_count=int(regime_progress.get("quarantine_count", 0)),
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--status-path", type=Path)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--settlement-grace-seconds", type=int, default=360)
    parser.add_argument("--retry-seconds", type=int, default=30)
    parser.add_argument("--status-interval-seconds", type=int, default=15)
    parser.add_argument(
        "--minimum-free-disk-bytes",
        type=int,
        default=12 * 1024**3,
    )
    parser.add_argument("--evidence-track-id", default="btc-rawcap")
    parser.add_argument("--validate-only", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> RawCollectorConfig:
    if args.config is not None:
        return load_collector_config(args.config)
    if args.data_root is None or args.registry is None:
        raise SystemExit(
            "either --config or both --data-root and --registry are required"
        )
    status_path = (
        args.status_path
        if args.status_path is not None
        else args.data_root / "service" / "status.json"
    )
    return RawCollectorConfig(
        data_root=args.data_root,
        status_path=status_path,
        registry_path=args.registry,
        settlement_grace_seconds=args.settlement_grace_seconds,
        retry_seconds=args.retry_seconds,
        status_interval_seconds=args.status_interval_seconds,
        minimum_free_disk_bytes=args.minimum_free_disk_bytes,
        evidence_track_id=args.evidence_track_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = _config_from_args(args)
    registry = load_settlement_regime_registry(config.registry_path)
    if args.validate_only:
        payload = {
            "schema_version": "btc-regime-agnostic-collector-validation.v1",
            "valid": True,
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
            "registry_sha256": registry.sha256,
            "status_path": str(config.status_path),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    asyncio.run(run_collector(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
