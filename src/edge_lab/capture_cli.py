"""Configuration and orchestration for credential-free forward capture."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from .capture_runtime import (
    CaptureStoreRecorderSink,
    PublicSourcesSnapshotClient,
)
from .compatibility import LiveExecutionBlocked, assert_new_orders_disabled
from .data_store import CaptureStore
from .network_safety import configure_public_session, safe_error_details
from .recorder import (
    DefaultWebSocketFactory,
    PublicRecorder,
    RecorderConfig,
)
from .sources import PublicSourcesClient

CONFIG_SCHEMA_VERSION = "edge-lab-forward-capture-config.v1"
_SAFE_TRACK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "passphrase",
    "private_key",
    "secret",
    "signature",
    "wallet",
)


class ForwardCaptureFinalizationError(RuntimeError):
    """A capture failed after producing an auditable final summary."""

    def __init__(self, summary: Mapping[str, Any]) -> None:
        self.summary = dict(summary)
        super().__init__("forward capture failed; structured finalization is available")


@dataclass(frozen=True)
class ForwardCaptureConfig:
    data_root: Path
    asset_ids: tuple[str, ...]
    condition_ids: tuple[str, ...]
    rule_market_ids: tuple[str, ...]
    rtds_subscriptions: tuple[Mapping[str, Any], ...]
    snapshot_intervals: Mapping[str, float]
    checkpoint_every_records: int
    max_records_per_batch: int
    gamma_page_limit: int
    gamma_max_pages: int
    reward_page_limit: int
    reward_max_pages: int
    targets: tuple[Mapping[str, Any], ...]
    clock_sync: Mapping[str, Any] | None = None
    capture_started_at_ms: int | None = None
    evidence_track_id: str | None = None

    def __post_init__(self) -> None:
        if self.capture_started_at_ms is not None and (
            isinstance(self.capture_started_at_ms, bool)
            or not isinstance(self.capture_started_at_ms, int)
            or self.capture_started_at_ms < 0
        ):
            raise ValueError("capture_started_at_ms must be a non-negative integer")
        if self.evidence_track_id is not None and (
            not isinstance(self.evidence_track_id, str)
            or _SAFE_TRACK_ID.fullmatch(self.evidence_track_id) is None
        ):
            raise ValueError("evidence_track_id must be a non-empty safe token")


def _unique_strings(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a JSON array")
    result = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} cannot contain duplicates")
    return result


def _reject_sensitive_keys(value: Any, *, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(
                    f"{path} contains forbidden credential-like key: {key}"
                )
            _reject_sensitive_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_keys(nested, path=f"{path}[{index}]")


def load_capture_config(
    path: Path,
    *,
    data_root_override: Path | None = None,
) -> ForwardCaptureConfig:
    """Load a public-only capture config without consulting environment files."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TypeError("capture config must be a JSON object")
    _reject_sensitive_keys(raw)
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("unsupported capture config schema_version")
    allowed = {
        "schema_version",
        "data_root",
        "asset_ids",
        "condition_ids",
        "rule_market_ids",
        "rtds_subscriptions",
        "snapshot_intervals",
        "checkpoint_every_records",
        "max_records_per_batch",
        "gamma_page_limit",
        "gamma_max_pages",
        "reward_page_limit",
        "reward_max_pages",
        "targets",
        "clock_sync",
        "capture_started_at_ms",
        "evidence_track_id",
        "settlement_regime_id",
        "qualification_status",
        "regime_classification",
        "registry_sha256",
    }
    unexpected = set(raw) - allowed
    if unexpected:
        raise ValueError(
            "unsupported capture config fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    root_value = data_root_override or Path(str(raw.get("data_root", "")))
    if not str(root_value):
        raise ValueError("data_root is required")
    subscriptions = raw.get("rtds_subscriptions")
    if not isinstance(subscriptions, list) or not subscriptions:
        raise ValueError("rtds_subscriptions must be a non-empty array")
    if any(not isinstance(item, Mapping) for item in subscriptions):
        raise ValueError("rtds_subscriptions entries must be objects")
    intervals = raw.get("snapshot_intervals")
    if not isinstance(intervals, Mapping):
        raise TypeError("snapshot_intervals must be an object")
    targets = raw.get("targets", [])
    if not isinstance(targets, list) or any(
        not isinstance(target, Mapping) for target in targets
    ):
        raise ValueError("targets must be an array of objects")
    clock_sync = raw.get("clock_sync")
    if clock_sync is not None and not isinstance(clock_sync, Mapping):
        raise ValueError("clock_sync must be an object when present")
    config = ForwardCaptureConfig(
        data_root=Path(root_value).expanduser().resolve(),
        asset_ids=_unique_strings(raw.get("asset_ids"), field="asset_ids"),
        condition_ids=_unique_strings(
            raw.get("condition_ids", []), field="condition_ids"
        ),
        rule_market_ids=_unique_strings(
            raw.get("rule_market_ids", []), field="rule_market_ids"
        ),
        rtds_subscriptions=tuple(dict(item) for item in subscriptions),
        snapshot_intervals={
            str(key): float(value) for key, value in intervals.items()
        },
        checkpoint_every_records=int(
            raw.get("checkpoint_every_records", 100)
        ),
        max_records_per_batch=int(raw.get("max_records_per_batch", 10_000)),
        gamma_page_limit=int(raw.get("gamma_page_limit", 100)),
        gamma_max_pages=int(raw.get("gamma_max_pages", 1)),
        reward_page_limit=int(raw.get("reward_page_limit", 500)),
        reward_max_pages=int(raw.get("reward_max_pages", 1)),
        targets=tuple(dict(target) for target in targets),
        clock_sync=(dict(clock_sync) if isinstance(clock_sync, Mapping) else None),
        capture_started_at_ms=raw.get("capture_started_at_ms"),
        evidence_track_id=raw.get("evidence_track_id"),
    )
    # Reuse the recorder's strict allowlist and timing validation.
    RecorderConfig(
        clob_asset_ids=config.asset_ids,
        rtds_subscriptions=config.rtds_subscriptions,
        snapshot_intervals=config.snapshot_intervals,
        checkpoint_every_records=config.checkpoint_every_records,
    )
    return config


def _local_proxy_mapping(proxy_url: str | None) -> dict[str, str]:
    if proxy_url is None:
        return {}
    parsed = urlsplit(proxy_url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "proxy must be an unauthenticated loopback HTTP(S) URL"
        )
    return {"http": proxy_url, "https": proxy_url}


def _verify_live_order_guard() -> None:
    try:
        assert_new_orders_disabled()
    except LiveExecutionBlocked:
        return
    raise RuntimeError("new-order safety guard is not active")


async def run_forward_capture(
    config: ForwardCaptureConfig,
    *,
    duration_seconds: float,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Run a bounded public capture and return a sanitized integrity summary."""

    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    _verify_live_order_guard()
    session = requests.Session()
    session.trust_env = False
    session.proxies.update(_local_proxy_mapping(proxy_url))
    configure_public_session(session)
    sources = PublicSourcesClient(
        session=session,
        timeout=20,
        retries=2,
        rate_per_second=8,
        burst=2,
        min_interval_seconds=0.05,
    )
    store = CaptureStore(config.data_root)
    sink = CaptureStoreRecorderSink(
        store,
        max_records_per_batch=config.max_records_per_batch,
    )
    snapshots = PublicSourcesSnapshotClient(
        sources,
        gamma_page_limit=config.gamma_page_limit,
        gamma_max_pages=config.gamma_max_pages,
        condition_ids=config.condition_ids,
        reward_page_limit=config.reward_page_limit,
        reward_max_pages=config.reward_max_pages,
        rule_market_ids=config.rule_market_ids,
    )
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_asset_ids=config.asset_ids,
            rtds_subscriptions=config.rtds_subscriptions,
            snapshot_intervals=config.snapshot_intervals,
            checkpoint_every_records=config.checkpoint_every_records,
            sink_timeout_seconds=60.0,
            checkpoint_timeout_seconds=60.0,
        ),
        sink=sink,
        snapshot_client=snapshots,
        websocket_factory=DefaultWebSocketFactory(proxy_url=proxy_url),
    )
    capture_error: BaseException | None = None
    try:
        await recorder.run(run_for_seconds=duration_seconds)
    except BaseException as exc:  # noqa: BLE001 - cancellation must still finalize
        capture_error = exc
    finally:
        manifests = await sink.close()
        session.close()
    integrity = store.audit_integrity()
    summary = {
        "schema_version": "edge-lab-forward-capture-summary.v1",
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "data_root": str(config.data_root),
        "duration_seconds": str(duration_seconds),
        "target_count": len(config.targets),
        "asset_count": len(config.asset_ids),
        "manifest_count": len(manifests),
        "manifest_record_count": sum(
            int(manifest.get("record_count", 0)) for manifest in manifests
        ),
        "integrity": integrity,
        "new_orders_disabled": True,
        "capture_error": (
            None
            if capture_error is None
            else safe_error_details(
                capture_error,
                code="capture_failed",
            )
        ),
    }
    if capture_error is not None:
        raise ForwardCaptureFinalizationError(summary) from capture_error
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run credential-free CLOB/RTDS forward capture."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--proxy")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate config and live-order guard without network access",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_capture_config(
        args.config,
        data_root_override=args.data_root,
    )
    _local_proxy_mapping(args.proxy)
    _verify_live_order_guard()
    if args.validate_only:
        result = {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "valid": True,
            "new_orders_disabled": True,
            "asset_count": len(config.asset_ids),
            "condition_count": len(config.condition_ids),
            "rule_market_count": len(config.rule_market_ids),
        }
    else:
        result = asyncio.run(
            run_forward_capture(
                config,
                duration_seconds=args.duration,
                proxy_url=args.proxy,
            )
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
