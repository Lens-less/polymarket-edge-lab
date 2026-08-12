from __future__ import annotations

import gc
import hashlib
import json
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.edge_lab import lifecycle_cohort_contract as cohort_contract
from src.edge_lab import phase2_lifecycle_audit as lifecycle_audit_module
from src.edge_lab.data_store import canonical_json_bytes, canonical_record_id
from src.edge_lab.lifecycle_cohort_contract import (
    LifecycleCohortContractError,
    ROOT_EVENT_TYPE,
    validate_cohort_payload,
)
from src.edge_lab.phase2_lifecycle_audit import (
    LifecycleAuditError,
    _load_inventory,
    _registry_targets,
    audit_phase2_lifecycle,
    write_phase2_lifecycle_report,
)
from src.edge_lab.short_crypto_registry import (
    merge_short_crypto_registry_snapshots,
    validate_short_crypto_registry_snapshot,
)


BASE_OPEN_MS = 1_800_000_000_000
SETTLEMENT_TIMEOUT_MS = 3_600_000


def test_cohort_truth_is_private_immutable_and_requires_native_json_lists(
) -> None:
    for public_name in ("ASSETS", "HORIZONS", "SELECTION_ORDER"):
        assert not hasattr(cohort_contract, public_name)
    for private_name in ("_ASSETS", "_HORIZONS", "_SELECTION_ORDER"):
        truth = getattr(cohort_contract, private_name)
        assert type(truth) is tuple
        with pytest.raises(TypeError):
            truth[0] = truth[0]

    received_at_ms = BASE_OPEN_MS - 300_000
    canonical = _root_cohort_payload(BASE_OPEN_MS)
    assert validate_cohort_payload(
        ROOT_EVENT_TYPE,
        canonical,
        outer_kind="command",
        record_id="a" * 64,
        received_at_ms=received_at_ms,
        settlement_timeout_ms=SETTLEMENT_TIMEOUT_MS,
    ) == canonical

    class _ListSubclass(list[object]):
        pass

    for field in ("assets", "horizons", "selection_order"):
        forged = dict(canonical)
        forged[field] = _ListSubclass(canonical[field])
        with pytest.raises(LifecycleCohortContractError):
            validate_cohort_payload(
                ROOT_EVENT_TYPE,
                forged,
                outer_kind="command",
                record_id="a" * 64,
                received_at_ms=received_at_ms,
                settlement_timeout_ms=SETTLEMENT_TIMEOUT_MS,
            )


def _iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _outer(
    source: str,
    payload: dict[str, object],
    *,
    received_at_ms: int,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": source,
        "received_at": _iso(received_at_ms),
        "event_at": None,
        "sequence": None,
        "payload": payload,
    }
    record["record_id"] = canonical_record_id(record)
    return record


def _service(
    event_type: str,
    payload: dict[str, object],
    *,
    received_at_ms: int,
    source: str = "dynamic_short_crypto_service",
    kind: str = "data",
) -> dict[str, object]:
    return _outer(
        source,
        {
            "schema_version": (
                "edge-lab-dynamic-short-crypto-service.record.v1"
            ),
            "source": "dynamic_short_crypto_service",
            "kind": kind,
            "event_type": event_type,
            "session_id": "service-session",
            "connection_id": None,
            "received_at": _iso(received_at_ms),
            "event_at": None,
            "sequence": None,
            "monotonic_ns": received_at_ms * 1_000_000,
            "payload": {
                "scheduler_now_ms": received_at_ms,
                **payload,
            },
        },
        received_at_ms=received_at_ms,
    )


def _remediation_plan_id(predecessor_record_id: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": (
                    "edge-lab.phase2-lifecycle-remediation-plan.v1"
                ),
                "predecessor_commitment_record_id": (
                    predecessor_record_id
                ),
                "remediation_reason_code": (
                    "scheduler_starved_by_gamma_sweep"
                ),
            }
        )
    ).hexdigest()


def _root_cohort_payload(
    eligibility_start_ms: int,
) -> dict[str, object]:
    return {
        "scheduler_now_ms": eligibility_start_ms - 600_000,
        "schema_version": (
            "edge-lab.phase2-lifecycle-prospective-cohort.v1"
        ),
        "selection_rule": (
            "first_mature_targets_opening_at_or_after_boundary"
        ),
        "eligibility_start_ms": eligibility_start_ms,
        "sample_size": 20,
        "threshold": "0.8",
        "settlement_timeout_ms": SETTLEMENT_TIMEOUT_MS,
        "assets": ["BTC", "ETH"],
        "horizons": ["5m", "15m"],
        "selection_order": ["opens_at_ms", "closes_at_ms", "slug"],
        "earliest_valid_commitment_wins": True,
        "must_be_durable_before_public_network": True,
        "actual_fill": False,
        "authenticated_fill": False,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }


def _remediation_cohort_payload(
    predecessor_record_id: str,
    eligibility_start_ms: int,
) -> dict[str, object]:
    return {
        "scheduler_now_ms": eligibility_start_ms - 600_000,
        "schema_version": (
            "edge-lab.phase2-lifecycle-remediation-cohort.v1"
        ),
        "selection_rule": (
            "first_mature_targets_opening_at_or_after_boundary"
        ),
        "predecessor_commitment_record_id": predecessor_record_id,
        "remediation_reason_code": "scheduler_starved_by_gamma_sweep",
        "remediation_plan_id": _remediation_plan_id(
            predecessor_record_id
        ),
        "eligibility_start_ms": eligibility_start_ms,
        "sample_size": 20,
        "threshold": "0.8",
        "settlement_timeout_ms": SETTLEMENT_TIMEOUT_MS,
        "assets": ["BTC", "ETH"],
        "horizons": ["5m", "15m"],
        "selection_order": ["opens_at_ms", "closes_at_ms", "slug"],
        "append_only": True,
        "prior_failure_retained": True,
        "must_be_durable_before_public_network": True,
        "actual_fill": False,
        "authenticated_fill": False,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }


def _transport(
    event_type: str,
    *,
    received_at_ms: int,
    connection_id: str,
    asset_id: str | None = None,
) -> dict[str, object]:
    schema = "edge-lab-recorder.lifecycle.v1"
    kind = "lifecycle"
    payload: dict[str, object] | None = None
    inner: dict[str, object] = {
        "schema_version": schema,
        "source": "clob_market_ws",
        "kind": kind,
        "event_type": event_type,
        "session_id": "market-session",
        "connection_id": connection_id,
        "received_at": _iso(received_at_ms),
        "event_at": None,
        "sequence": None,
        "monotonic_ns": received_at_ms * 1_000_000,
    }
    if event_type == "resync_complete":
        inner["detail"] = {
            "watermark_ns": received_at_ms * 1_000_000,
            "asset_watermarks_ns": {},
        }
    elif event_type == "heartbeat_ack":
        inner["schema_version"] = "edge-lab-recorder.heartbeat-ack.v1"
        inner["raw_frame"] = "PONG"
    elif event_type == "price_change":
        assert asset_id is not None
        inner["schema_version"] = "clob-market-ws.price-change.v1"
        inner["kind"] = "data"
        payload = {
            "event_type": "price_change",
            "market": "0x" + "ab" * 32,
            "price_changes": [
                {
                    "asset_id": asset_id,
                    "price": "0.4",
                    "size": "2",
                    "side": "BUY",
                    "hash": "book-hash",
                    "best_bid": "0.4",
                    "best_ask": "0.41",
                }
            ],
            "timestamp": str(received_at_ms),
        }
        inner["payload"] = payload
    elif event_type == "disconnected":
        inner["detail"] = {"reason": "fixture-gap"}
    return _outer(
        "clob_market_ws",
        inner,
        received_at_ms=received_at_ms,
    )


def _finalize(
    store_root: Path,
    source: str,
    batch_id: str,
    records: list[dict[str, object]],
    *,
    finalized_at_ms: int | None = None,
) -> Path:
    raw_dir = store_root / "raw" / source
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{batch_id}.jsonl"
    raw_bytes = b"".join(
        json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
        for record in records
    )
    raw_path.write_bytes(raw_bytes)
    manifest = {
        "manifest_version": "capture-manifest.v1",
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": source,
        "batch_id": batch_id,
        "raw_path": f"raw/{source}/{batch_id}.jsonl",
        "finalized_at": _iso(
            0 if finalized_at_ms is None else finalized_at_ms
        ),
        "record_count": len(records),
        "checksum": {
            "algorithm": "sha256",
            "value": hashlib.sha256(raw_bytes).hexdigest(),
            "bytes": len(raw_bytes),
            "lines": len(records),
        },
    }
    manifest_path = raw_dir / f"{batch_id}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return manifest_path


def _replace_finalized_records(
    raw_path: Path,
    records: list[dict[str, object]],
) -> None:
    raw_bytes = b"".join(
        json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
        for record in records
    )
    raw_path.write_bytes(raw_bytes)
    manifest_path = raw_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record_count"] = len(records)
    manifest["checksum"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(raw_bytes).hexdigest(),
        "bytes": len(raw_bytes),
        "lines": len(records),
    }
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _control_service_records(
    dynamic_root: Path,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for raw_path in sorted(
        (
            dynamic_root
            / "control"
            / "raw"
            / "dynamic_short_crypto_service"
        ).glob("*.jsonl")
    ):
        records.extend(
            json.loads(line)
            for line in raw_path.read_bytes().splitlines()
        )
    return records


def test_inventory_counts_but_does_not_retain_irrelevant_main_market_data(
    tmp_path: Path,
) -> None:
    main_root = tmp_path / "main"
    irrelevant = _transport(
        "price_change",
        received_at_ms=BASE_OPEN_MS,
        connection_id="main-capture",
        asset_id="1" * 76,
    )
    _finalize(
        main_root,
        "clob_market_ws",
        "irrelevant-main-market-data",
        [irrelevant],
    )

    by_id, service_events, manifest_count, record_count = _load_inventory(
        ((main_root, False),)
    )

    assert by_id[str(irrelevant["record_id"])] is None
    assert service_events == ()
    assert manifest_count == 1
    assert record_count == 1


def test_inventory_excludes_post_cutoff_finalized_evidence(
    tmp_path: Path,
) -> None:
    main_root = tmp_path / "main"
    future_announcement = _outer(
        "clob_market_ws",
        {
            "schema_version": "clob-market-ws.new-market.v1",
            "source": "clob_market_ws",
            "event_type": "new_market",
            "payload": {"event_type": "new_market"},
        },
        received_at_ms=BASE_OPEN_MS + 1,
    )
    future_service = _service(
        "lifecycle_cohort_committed",
        {},
        received_at_ms=BASE_OPEN_MS + 1,
    )
    _finalize(
        main_root,
        "clob_market_ws",
        "future-announcement",
        [future_announcement],
        finalized_at_ms=BASE_OPEN_MS + 1,
    )
    _finalize(
        main_root,
        "dynamic_short_crypto_service",
        "future-service",
        [future_service],
        finalized_at_ms=BASE_OPEN_MS + 1,
    )

    by_id, service_events, manifest_count, record_count = _load_inventory(
        ((main_root, False),),
        as_of_ms=BASE_OPEN_MS,
    )

    assert str(future_announcement["record_id"]) not in by_id
    assert service_events == ()
    assert manifest_count == 0
    assert record_count == 0


def test_inventory_indexes_irrelevant_ids_without_python_heap_hydration(
    tmp_path: Path,
) -> None:
    main_root = tmp_path / "main"
    records = [
        _transport(
            "price_change",
            received_at_ms=BASE_OPEN_MS + index,
            connection_id="main-capture",
            asset_id="1" * 76,
        )
        for index in range(2_000)
    ]
    _finalize(
        main_root,
        "clob_market_ws",
        "irrelevant-main-market-data",
        records,
    )

    by_id, service_events, manifest_count, record_count = _load_inventory(
        ((main_root, False),)
    )

    assert by_id.indexed_record_count == 2_000
    assert by_id.hydrated_record_count == 0
    assert str(records[0]["record_id"]) in by_id
    assert by_id[str(records[-1]["record_id"])] is None
    assert service_events == ()
    assert manifest_count == 1
    assert record_count == 2_000


def test_inventory_drops_unused_raw_frames_but_keeps_pong_proof(
    tmp_path: Path,
) -> None:
    dynamic_root = tmp_path / "dynamic-run"
    group_root = dynamic_root / "groups" / "group-1"
    price_change = _transport(
        "price_change",
        received_at_ms=BASE_OPEN_MS,
        connection_id="group-1",
        asset_id="1" * 76,
    )
    price_change["payload"]["raw_frame"] = "unused-market-frame"
    price_change["record_id"] = canonical_record_id(price_change)
    heartbeat = _transport(
        "heartbeat_ack",
        received_at_ms=BASE_OPEN_MS + 1,
        connection_id="group-1",
    )
    _finalize(
        group_root,
        "clob_market_ws",
        "group-market-data",
        [price_change, heartbeat],
    )

    manifest_entries: list[tuple[Path, str | None, int]] = []
    by_id, _, _, _ = _load_inventory(
        ((dynamic_root, True),),
        manifest_entries_out=manifest_entries,
    )
    lifecycle_audit_module._load_selected_group_records(
        manifest_entries,
        {"group-1"},
        by_id,
    )

    compact_price = by_id[str(price_change["record_id"])]
    compact_heartbeat = by_id[str(heartbeat["record_id"])]
    assert compact_price is not None
    assert compact_price.record["payload"] == {
        "event_type": "price_change",
    }
    assert compact_heartbeat is not None
    assert compact_heartbeat.record["payload"]["raw_frame"] == "PONG"
    assert compact_heartbeat.record["payload"]["session_id"] == (
        "market-session"
    )
    assert compact_heartbeat.record["payload"]["connection_id"] == "group-1"


def test_inventory_hydrates_only_selected_dynamic_groups(
    tmp_path: Path,
) -> None:
    dynamic_root = tmp_path / "dynamic-run"
    selected = _transport(
        "price_change",
        received_at_ms=BASE_OPEN_MS,
        connection_id="group-selected",
        asset_id="1" * 76,
    )
    ignored = _transport(
        "price_change",
        received_at_ms=BASE_OPEN_MS,
        connection_id="group-ignored",
        asset_id="2" * 76,
    )
    _finalize(
        dynamic_root / "groups" / "group-selected",
        "clob_market_ws",
        "selected-market-data",
        [selected],
    )
    _finalize(
        dynamic_root / "groups" / "group-ignored",
        "clob_market_ws",
        "ignored-market-data",
        [ignored],
    )
    manifest_entries: list[tuple[Path, str | None, int]] = []

    by_id, _, _, _ = _load_inventory(
        ((dynamic_root, True),),
        manifest_entries_out=manifest_entries,
    )

    assert by_id[str(selected["record_id"])] is None
    assert by_id[str(ignored["record_id"])] is None
    hydrated = lifecycle_audit_module._load_selected_group_records(
        manifest_entries,
        {"group-selected"},
        by_id,
    )
    assert str(selected["record_id"]) in {
        str(item.record["record_id"])
        for item in hydrated["group-selected"]
    }
    assert by_id[str(selected["record_id"])] is not None
    assert by_id[str(ignored["record_id"])] is None


def _target(index: int) -> dict[str, object]:
    opens_at_ms = BASE_OPEN_MS + index * 900_000
    slug = f"btc-updown-5m-{opens_at_ms // 1_000}"
    return {
        "slug": slug,
        "market_id": str(4_000_000 + index),
        "condition_id": "0x" + f"{index + 1:064x}",
        "up_token_id": f"{index * 2 + 1:076d}",
        "down_token_id": f"{index * 2 + 2:076d}",
        "source_symbol": "btc/usd",
        "source_topic": "crypto_prices_chainlink",
        "horizon": "5m",
        "opens_at_ms": opens_at_ms,
        "closes_at_ms": opens_at_ms + 300_000,
        "rule_hash": hashlib.sha256(b"chainlink-btc-usd").hexdigest(),
        "announced_at": _iso(opens_at_ms - 86_400_000),
    }


def _registry_snapshot(
    target: dict[str, object],
    *,
    path: str,
    input_sha256: str,
) -> dict[str, object]:
    descriptor = {
        "path": path,
        "sha256": input_sha256,
        "byte_count": 1,
        "line_count": 1,
    }
    frozen_target = {
        **target,
        "provenance": [
            {
                "record_id": target["announcement_record_id"],
                "path": path,
                "line": 1,
            }
        ],
    }
    snapshot: dict[str, object] = {
        "schema_version": "edge-lab.short-crypto-registry.v1",
        "inputs": [descriptor],
        "targets": [frozen_target],
        "rejections": [],
        "completeness": {
            "status": "complete",
            "input_file_count": 1,
            "input_line_count": 1,
            "parsed_object_count": 1,
            "target_announcement_count": 1,
            "unique_target_count": 1,
            "duplicate_target_announcement_count": 0,
            "unrelated_record_count": 0,
            "rejected_record_count": 0,
            "rejection_counts": {},
        },
    }
    snapshot["snapshot_sha256"] = hashlib.sha256(
        canonical_json_bytes(snapshot)
    ).hexdigest()
    return snapshot


def test_inventory_defers_registry_payload_in_compressed_form(
    tmp_path: Path,
) -> None:
    dynamic_root = tmp_path / "dynamic-run"
    target = _target(0)
    target["announcement_record_id"] = "a" * 64
    snapshot = _registry_snapshot(
        target,
        path="/capture/a.jsonl",
        input_sha256="1" * 64,
    )
    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "registry",
        [
            _service(
                "registry_revision",
                snapshot,
                received_at_ms=BASE_OPEN_MS - 100_000,
            )
        ],
    )

    _, service_events, _, _ = _load_inventory(
        ((dynamic_root, True),)
    )

    registry_event = service_events[0]
    assert registry_event.payload == {}
    assert isinstance(registry_event.compressed_payload, bytes)
    assert _registry_targets(service_events)[0]["slug"] == target["slug"]


def test_lifecycle_audit_does_not_revalidate_v1_registry_after_inventory(
    tmp_path: Path,
) -> None:
    main_root = tmp_path / "main"
    main_root.mkdir()
    dynamic_root = tmp_path / "dynamic-run"
    target = _target(0)
    target["announcement_record_id"] = "a" * 64
    snapshot = _registry_snapshot(
        target,
        path="/capture/input-0000.jsonl",
        input_sha256="1" * 64,
    )
    snapshot["inputs"] = [
        {
            "path": f"/capture/input-{index:04d}.jsonl",
            "sha256": f"{index + 1:064x}",
            "byte_count": index + 1,
            "line_count": 1,
        }
        for index in range(1_500)
    ]
    snapshot["snapshot_sha256"] = hashlib.sha256(
        canonical_json_bytes(
            {
                key: value
                for key, value in snapshot.items()
                if key != "snapshot_sha256"
            }
        )
    ).hexdigest()
    records = [
        _service(
            "registry_revision",
            snapshot,
            received_at_ms=BASE_OPEN_MS - 100_000 + index,
        )
        for index in range(24)
    ]
    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "production-v1-registry-history",
        records,
    )
    raw_path = next(dynamic_root.rglob("*.jsonl"))
    snapshots = [
        {
            key: json.loads(line)["payload"]["payload"][key]
            for key in (
                "schema_version",
                "inputs",
                "targets",
                "rejections",
                "completeness",
                "snapshot_sha256",
            )
        }
        for line in raw_path.read_bytes().splitlines()
    ]
    baseline_started = time.perf_counter()
    for snapshot_value in snapshots:
        validate_short_crypto_registry_snapshot(snapshot_value)
    single_validation_seconds = time.perf_counter() - baseline_started

    audit_started = time.perf_counter()
    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=BASE_OPEN_MS - 1,
    )
    audit_seconds = time.perf_counter() - audit_started

    assert report["registered_target_count"] == 1
    assert audit_seconds <= single_validation_seconds * 2.5 + 0.05, (
        "the public audit repeated already-proven v1 registry validation: "
        f"audit={audit_seconds:.6f}s, "
        f"single_validation={single_validation_seconds:.6f}s, "
        f"ratio={audit_seconds / single_validation_seconds:.2f}"
    )


def test_lifecycle_audit_reuses_identical_finalized_registry_payloads(
    tmp_path: Path,
) -> None:
    def measured_peak(
        revision_count: int,
        label: str,
    ) -> tuple[int, dict[str, object]]:
        main_root = tmp_path / label / "main"
        main_root.mkdir(parents=True)
        dynamic_root = tmp_path / label / "dynamic-run"
        target = _target(0)
        target["announcement_record_id"] = "a" * 64
        snapshot = _registry_snapshot(
            target,
            path="/capture/input-0000.jsonl",
            input_sha256="1" * 64,
        )
        snapshot["inputs"] = [
            {
                "path": (
                    "/capture/"
                    f"{hashlib.sha256(f'path:{index}'.encode()).hexdigest()}"
                    ".jsonl"
                ),
                "sha256": hashlib.sha256(
                    f"payload:{index}".encode()
                ).hexdigest(),
                "byte_count": index + 1,
                "line_count": 1,
            }
            for index in range(600)
        ]
        snapshot["snapshot_sha256"] = hashlib.sha256(
            canonical_json_bytes(
                {
                    key: value
                    for key, value in snapshot.items()
                    if key != "snapshot_sha256"
                }
            )
        ).hexdigest()
        for index in range(revision_count):
            _finalize(
                dynamic_root / "control",
                "dynamic_short_crypto_service",
                f"registry-{index:03d}",
                [
                    _service(
                        "registry_revision",
                        snapshot,
                        received_at_ms=(
                            BASE_OPEN_MS - 100_000 + index
                        ),
                    )
                ],
            )

        gc.collect()
        tracemalloc.start()
        try:
            report = audit_phase2_lifecycle(
                (dynamic_root,),
                main_capture_root=main_root,
                as_of_ms=BASE_OPEN_MS - 1,
            )
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return peak_bytes, report

    small_peak, small_report = measured_peak(6, "small")
    large_peak, large_report = measured_peak(48, "large")

    assert small_report["registered_target_count"] == 1
    assert large_report["registered_target_count"] == 1
    assert large_peak <= small_peak * 1.75, (
        "identical validated registry payloads accumulated in memory: "
        f"small_peak={small_peak}, large_peak={large_peak}, "
        f"ratio={large_peak / small_peak:.2f}"
    )


def _build_mixed_registry_capture(
    tmp_path: Path,
    *,
    cumulative_target_count: int = 2,
) -> tuple[Path, Path]:
    main_root = tmp_path / "main"
    main_root.mkdir()
    dynamic_root = tmp_path / "dynamic-run"
    first_target = _target(0)
    first_target["announcement_record_id"] = "a" * 64
    second_target = _target(1)
    second_target["announcement_record_id"] = "b" * 64
    first_snapshot = _registry_snapshot(
        first_target,
        path="/capture/a.jsonl",
        input_sha256="1" * 64,
    )
    second_delta = _registry_snapshot(
        second_target,
        path="/capture/b.jsonl",
        input_sha256="2" * 64,
    )
    cumulative = merge_short_crypto_registry_snapshots(
        first_snapshot,
        second_delta,
    )
    revisions = [
        _service(
            "registry_revision",
            first_snapshot,
            received_at_ms=BASE_OPEN_MS - 100_000,
        ),
        _service(
            "registry_revision",
            {
                "schema_version": (
                    "edge-lab-short-crypto-registry-revision.v2"
                ),
                "delta_snapshot": second_delta,
                "cumulative_snapshot_sha256": cumulative[
                    "snapshot_sha256"
                ],
                "cumulative_input_file_count": 2,
                "cumulative_target_count": cumulative_target_count,
            },
            received_at_ms=BASE_OPEN_MS - 90_000,
        ),
    ]
    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "registry",
        revisions,
    )
    return main_root, dynamic_root


def _build_capture(
    tmp_path: Path,
    *,
    target_count: int = 20,
    continuity_failures: frozenset[int] = frozenset({16, 17, 18, 19}),
    cohort_eligibility_start_ms: int | None = None,
    remediation_eligibility_start_ms: int | None = None,
    remediation_received_at_ms: int | None = None,
    remediation_predecessor_record_id: str | None = None,
    remediation_branch: bool = False,
) -> tuple[Path, Path, list[dict[str, object]]]:
    main_root = tmp_path / "main"
    dynamic_root = tmp_path / "dynamic-run"
    targets = [_target(index) for index in range(target_count)]
    announcements: list[dict[str, object]] = []
    rtds: list[dict[str, object]] = []
    gamma: list[dict[str, object]] = []
    control: list[dict[str, object]] = []

    for target in targets:
        open_ms = int(target["opens_at_ms"])
        close_ms = int(target["closes_at_ms"])
        announcement = _outer(
            "clob_market_ws",
            {
                "schema_version": "clob-market-ws.new_market.v1",
                "source": "clob_market_ws",
                "event_type": "new_market",
                "payload": {
                    "event_type": "new_market",
                    "id": target["market_id"],
                    "slug": target["slug"],
                    "condition_id": target["condition_id"],
                    "market": target["condition_id"],
                    "assets_ids": [
                        target["up_token_id"],
                        target["down_token_id"],
                    ],
                    "clob_token_ids": [
                        target["up_token_id"],
                        target["down_token_id"],
                    ],
                    "outcomes": ["Up", "Down"],
                },
            },
            received_at_ms=open_ms - 86_400_000,
        )
        target["announcement_record_id"] = announcement["record_id"]
        announcements.append(announcement)

        open_boundary = _outer(
            "rtds_ws",
            {
                "schema_version": "rtds-ws.crypto-prices.v1",
                "source": "rtds_ws",
                "event_type": "crypto_prices",
                "payload": {
                    "topic": "crypto_prices_chainlink",
                    "symbol": "btc/usd",
                    "timestamp": open_ms,
                    "full_accuracy_value": "100000000000000000000",
                    "value": "100",
                },
            },
            received_at_ms=open_ms + 100,
        )
        close_boundary = _outer(
            "rtds_ws",
            {
                "schema_version": "rtds-ws.crypto-prices.v1",
                "source": "rtds_ws",
                "event_type": "crypto_prices",
                "payload": {
                    "topic": "crypto_prices_chainlink",
                    "symbol": "btc/usd",
                    "timestamp": close_ms,
                    "full_accuracy_value": "101000000000000000000",
                    "value": "101",
                },
            },
            received_at_ms=close_ms + 100,
        )
        rtds.extend((open_boundary, close_boundary))

        initial_gamma = _outer(
            "gamma_http",
            {
                "schema_version": "gamma-http.target-snapshot.v1",
                "source": "gamma_http",
                "event_type": "gamma_market",
                "payload": {"slug": target["slug"], "closed": False},
            },
            received_at_ms=open_ms - 60_000,
        )
        resolved_gamma = _outer(
            "gamma_http",
            {
                "schema_version": "gamma-http.target-snapshot.v1",
                "source": "gamma_http",
                "event_type": "gamma_market",
                "payload": {
                    "slug": target["slug"],
                    "closed": True,
                    "outcomePrices": ["1", "0"],
                },
            },
            received_at_ms=close_ms + 5_000,
        )
        gamma.extend((initial_gamma, resolved_gamma))

        slug = str(target["slug"])
        group_id = f"group-{slug}"
        decision_id = hashlib.sha256(slug.encode()).hexdigest()
        before = _transport(
            "heartbeat_ack",
            received_at_ms=close_ms - 5_000,
            connection_id=group_id,
        )
        after = _transport(
            "heartbeat_ack",
            received_at_ms=close_ms + 5_000,
            connection_id=group_id,
        )
        group_records = [
            _outer(
                "clob_http",
                {
                    "schema_version": "clob-http.snapshot.v1",
                    "source": "clob_http",
                    "event_type": "clob_snapshot",
                    "payload": {
                        "asset_id": target["up_token_id"],
                        "condition_id": target["condition_id"],
                    },
                },
                received_at_ms=open_ms - 50_000,
            ),
            _transport(
                "resync_complete",
                received_at_ms=open_ms - 49_000,
                connection_id=group_id,
            ),
            _transport(
                "price_change",
                received_at_ms=open_ms + 10_000,
                connection_id=group_id,
                asset_id=str(target["up_token_id"]),
            ),
            before,
            after,
        ]
        index = targets.index(target)
        if index in continuity_failures:
            group_records.insert(
                4,
                _transport(
                    "disconnected",
                    received_at_ms=open_ms + 30_000,
                    connection_id=group_id,
                ),
            )
        _finalize(
            dynamic_root / "groups" / group_id,
            "clob_http",
            "snapshot",
            [group_records[0]],
        )
        _finalize(
            dynamic_root / "groups" / group_id,
            "clob_market_ws",
            "stream",
            group_records[1:],
        )

        control.extend(
            [
                _service(
                    "gamma_target_verified",
                    {
                        "slug": slug,
                        "market_id": target["market_id"],
                        "gamma_record_id": initial_gamma["record_id"],
                    },
                    received_at_ms=open_ms - 59_000,
                ),
                _service(
                    "capture_decision",
                    {
                        "schema_version": (
                            "edge-lab.short-crypto-capture-decision.v1"
                        ),
                        "decision_id": decision_id,
                        "action": "subscribe",
                        "group_id": group_id,
                        "asset_ids": [
                            target["up_token_id"],
                            target["down_token_id"],
                        ],
                        "targets": [
                            {
                                "slug": slug,
                                "announcement_record_id": target[
                                    "announcement_record_id"
                                ],
                                "rule_hash": target["rule_hash"],
                                "up_token_id": target["up_token_id"],
                                "down_token_id": target["down_token_id"],
                            }
                        ],
                        "emitted_at_ms": open_ms - 58_000,
                    },
                    received_at_ms=open_ms - 58_000,
                ),
                _service(
                    "worker_start_applied",
                    {
                        "decision_id": decision_id,
                        "group_id": group_id,
                        "target_slugs": [slug],
                    },
                    received_at_ms=open_ms - 57_000,
                ),
                _service(
                    "worker_ready",
                    {
                        "decision_id": decision_id,
                        "group_id": group_id,
                        "snapshot_watermark_ns": (
                            (open_ms - 49_000) * 1_000_000
                        ),
                    },
                    received_at_ms=open_ms - 48_000,
                ),
                _service(
                    "chainlink_boundary_evidence",
                    {
                        "slug": slug,
                        "boundary_role": "open",
                        "inner_timestamp_ms": open_ms,
                        "source_record_id": open_boundary["record_id"],
                    },
                    received_at_ms=open_ms + 1_000,
                    source="rtds_ws",
                ),
                _service(
                    "chainlink_boundary_evidence_committed",
                    {
                        "slug": slug,
                        "boundary_role": "open",
                        "source_record_id": open_boundary["record_id"],
                    },
                    received_at_ms=open_ms + 1_100,
                ),
                _service(
                    "chainlink_boundary_evidence",
                    {
                        "slug": slug,
                        "boundary_role": "close",
                        "inner_timestamp_ms": close_ms,
                        "source_record_id": close_boundary["record_id"],
                    },
                    received_at_ms=close_ms + 1_000,
                    source="rtds_ws",
                ),
                _service(
                    "chainlink_boundary_evidence_committed",
                    {
                        "slug": slug,
                        "boundary_role": "close",
                        "source_record_id": close_boundary["record_id"],
                    },
                    received_at_ms=close_ms + 1_100,
                ),
                _service(
                    "full_window_capture_close_checkpoint",
                    {
                        "slug": slug,
                        "condition_id": target["condition_id"],
                        "group_id": group_id,
                        "opens_at_ms": open_ms,
                        "closes_at_ms": close_ms,
                        "finalized_through_ms": close_ms + 6_000,
                    },
                    received_at_ms=close_ms + 6_000,
                ),
                _service(
                    "decision_to_trade_l2_closure",
                    {
                        "slug": slug,
                        "condition_id": target["condition_id"],
                        "group_id": group_id,
                        "opens_at_ms": open_ms,
                        "closes_at_ms": close_ms,
                        "closed_through_ms": close_ms,
                    },
                    received_at_ms=close_ms + 6_100,
                ),
                _service(
                    "settlement_reconciled",
                    {
                        "slug": slug,
                        "condition_id": target["condition_id"],
                        "status": "resolved",
                        "action": "strict_settlement_committed",
                        "outcome": "Up",
                        "gamma_record_ids": [resolved_gamma["record_id"]],
                        "chainlink_boundary_status": "committed",
                        "required_record_ids": [
                            target["announcement_record_id"],
                            resolved_gamma["record_id"],
                            open_boundary["record_id"],
                            close_boundary["record_id"],
                            before["record_id"],
                            after["record_id"],
                        ],
                        "close_liveness": {
                            "status": "verified",
                            "session_id": "market-session",
                            "connection_id": group_id,
                            "before_record_id": before["record_id"],
                            "before_received_at_ms": close_ms - 5_000,
                            "after_record_id": after["record_id"],
                            "after_received_at_ms": close_ms + 5_000,
                        },
                    },
                    received_at_ms=close_ms + 7_000,
                ),
            ]
        )

    control.insert(
        0,
        _service(
            "registry_revision",
            {
                "schema_version": (
                    "edge-lab.dynamic-short-crypto-registry.v1"
                ),
                "snapshot_sha256": hashlib.sha256(b"registry").hexdigest(),
                "inputs": [],
                "targets": targets,
                "rejections": [],
                "completeness": {"status": "complete"},
            },
            received_at_ms=BASE_OPEN_MS - 90_000,
        ),
    )
    cohort_commitment: dict[str, object] | None = None
    if cohort_eligibility_start_ms is not None:
        cohort_commitment = _service(
            "lifecycle_cohort_committed",
            {
                "schema_version": (
                    "edge-lab.phase2-lifecycle-prospective-cohort.v1"
                ),
                "selection_rule": (
                    "first_mature_targets_opening_at_or_after_boundary"
                ),
                "eligibility_start_ms": cohort_eligibility_start_ms,
                "sample_size": 20,
                "threshold": "0.8",
                "settlement_timeout_ms": SETTLEMENT_TIMEOUT_MS,
                "assets": ["BTC", "ETH"],
                "horizons": ["5m", "15m"],
                "selection_order": [
                    "opens_at_ms",
                    "closes_at_ms",
                    "slug",
                ],
                "earliest_valid_commitment_wins": True,
                "must_be_durable_before_public_network": True,
                "actual_fill": False,
                "authenticated_fill": False,
                "orders_submitted": 0,
                "authenticated_endpoints_used": 0,
            },
            received_at_ms=cohort_eligibility_start_ms - 600_000,
            kind="command",
        )
        control.insert(
            1,
            cohort_commitment,
        )
    if remediation_eligibility_start_ms is not None:
        assert cohort_commitment is not None
        assert remediation_received_at_ms is not None
        remediation_records = [
            _service(
                "lifecycle_remediation_cohort_committed",
                {
                    "schema_version": (
                        "edge-lab.phase2-lifecycle-remediation-cohort.v1"
                    ),
                    "selection_rule": (
                        "first_mature_targets_opening_at_or_after_boundary"
                    ),
                    "predecessor_commitment_record_id": (
                        remediation_predecessor_record_id
                        if remediation_predecessor_record_id is not None
                        else cohort_commitment["record_id"]
                    ),
                    "remediation_reason_code": (
                        "scheduler_starved_by_gamma_sweep"
                    ),
                    "remediation_plan_id": _remediation_plan_id(
                        remediation_predecessor_record_id
                        if remediation_predecessor_record_id is not None
                        else str(cohort_commitment["record_id"])
                    ),
                    "eligibility_start_ms": remediation_eligibility_start_ms,
                    "sample_size": 20,
                    "threshold": "0.8",
                    "settlement_timeout_ms": SETTLEMENT_TIMEOUT_MS,
                    "assets": ["BTC", "ETH"],
                    "horizons": ["5m", "15m"],
                    "selection_order": [
                        "opens_at_ms",
                        "closes_at_ms",
                        "slug",
                    ],
                    "append_only": True,
                    "prior_failure_retained": True,
                    "must_be_durable_before_public_network": True,
                    "actual_fill": False,
                    "authenticated_fill": False,
                    "orders_submitted": 0,
                    "authenticated_endpoints_used": 0,
                },
                received_at_ms=remediation_received_at_ms + offset,
                kind="command",
            )
            for offset in range(2 if remediation_branch else 1)
        ]
        for offset, remediation_record in enumerate(remediation_records):
            control.insert(2 + offset, remediation_record)
    _finalize(main_root, "clob_market_ws", "announcements", announcements)
    _finalize(main_root, "rtds_ws", "boundaries", rtds)
    _finalize(dynamic_root / "control", "gamma_http", "gamma", gamma)
    control_by_source: dict[str, list[dict[str, object]]] = {}
    for record in control:
        control_by_source.setdefault(str(record["source"]), []).append(record)
    for source, records in sorted(control_by_source.items()):
        _finalize(
            dynamic_root / "control",
            source,
            f"control-{source}",
            records,
        )
    return main_root, dynamic_root, targets


def test_first_twenty_mature_targets_require_at_least_eighty_percent(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(tmp_path)
    as_of_ms = int(targets[-1]["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
        sample_size=20,
        settlement_timeout_ms=SETTLEMENT_TIMEOUT_MS,
    )

    assert report["mature_target_count"] == 20
    assert report["sampled_target_count"] == 20
    assert report["complete_target_count"] == 16
    assert report["completeness_ratio"] == "0.8"
    assert report["threshold"] == "0.8"
    assert report["threshold_met"] is True
    assert report["gate_status"] == "passed"
    assert report["orders_submitted"] == 0
    assert report["authenticated_endpoints_used"] == 0
    assert [item["slug"] for item in report["targets"]] == [
        target["slug"] for target in targets
    ]
    for item in report["targets"][:16]:
        assert item["complete"] is True
        assert item["missing_stages"] == []
        assert all(item["checks"].values())
    for item in report["targets"][16:]:
        assert item["complete"] is False
        assert item["missing_stages"] == ["clob_increment_continuity"]


def test_prospective_commitment_preserves_failed_global_first_twenty(
    tmp_path: Path,
) -> None:
    cohort_start_ms = int(_target(20)["opens_at_ms"])
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=40,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=cohort_start_ms,
    )

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    assert report["cohort_mode"] == "prospective_committed"
    assert report["cohort_commitment"]["eligibility_start_ms"] == (
        cohort_start_ms
    )
    assert report["mature_target_count"] == 20
    assert report["sampled_target_count"] == 20
    assert report["complete_target_count"] == 20
    assert report["completeness_ratio"] == "1"
    assert report["gate_status"] == "passed"
    assert [item["slug"] for item in report["targets"]] == [
        target["slug"] for target in targets[20:]
    ]
    historical = report["historical_global_first_mature"]
    assert historical["mature_target_count"] == 40
    assert historical["sampled_target_count"] == 20
    assert historical["complete_target_count"] == 0
    assert historical["completeness_ratio"] == "0"
    assert historical["gate_status"] == "failed"
    assert [item["slug"] for item in historical["targets"]] == [
        target["slug"] for target in targets[:20]
    ]


def test_append_only_remediation_pass_retains_failed_root_cohort(
    tmp_path: Path,
) -> None:
    root_start_ms = int(_target(0)["opens_at_ms"])
    remediation_start_ms = int(_target(25)["opens_at_ms"])
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=root_start_ms,
        remediation_eligibility_start_ms=remediation_start_ms,
        remediation_received_at_ms=predecessor_mature_at_ms + 1,
    )

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    assert report["cohort_mode"] == "append_only_remediation"
    assert report["gate_status"] == (
        "passed_after_remediation_with_retained_failures"
    )
    assert report["threshold_met"] is True
    assert report["prior_failed_cohort_count"] == 1
    assert report["active_cohort_index"] == 1
    assert len(report["cohort_history"]) == 2

    root = report["cohort_history"][0]
    assert root["role"] == "root"
    assert root["gate_status"] == "failed_retained_for_remediation"
    assert root["complete_target_count"] == 0
    assert root["completeness_ratio"] == "0"
    assert [item["slug"] for item in root["targets"]] == [
        target["slug"] for target in targets[:20]
    ]

    remediation = report["cohort_history"][1]
    assert remediation["role"] == "remediation"
    assert remediation["predecessor_commitment_record_id"] == (
        root["commitment"]["record_id"]
    )
    assert remediation["gate_status"] == (
        "passed_after_remediation_with_retained_failures"
    )
    assert remediation["complete_target_count"] == 20
    assert remediation["completeness_ratio"] == "1"
    assert [item["slug"] for item in remediation["targets"]] == [
        target["slug"] for target in targets[25:45]
    ]
    assert report["historical_global_first_mature"]["gate_status"] == "failed"


def test_predecessor_first_twenty_ignores_registry_target_finalized_late(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    remediation_received_at_ms = predecessor_mature_at_ms + 1
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=remediation_received_at_ms,
    )
    late_target = _target(999)
    late_open_ms = int(targets[0]["opens_at_ms"]) + 1
    late_target.update(
        {
            "slug": "btc-updown-5m-late-registry",
            "opens_at_ms": late_open_ms,
            "closes_at_ms": late_open_ms + 300_000,
            "announcement_record_id": "f" * 64,
        }
    )
    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "late-registry-after-remediation",
        [
            _service(
                "registry_revision",
                {
                    "schema_version": (
                        "edge-lab.dynamic-short-crypto-registry.v1"
                    ),
                    "snapshot_sha256": hashlib.sha256(
                        b"late-registry"
                    ).hexdigest(),
                    "inputs": [],
                    "targets": [late_target],
                    "rejections": [],
                    "completeness": {"status": "complete"},
                },
                received_at_ms=remediation_received_at_ms + 1,
            )
        ],
    )

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    root = report["cohort_history"][0]
    assert [item["slug"] for item in root["targets"]] == [
        target["slug"] for target in targets[:20]
    ]
    assert all(
        item["slug"] != late_target["slug"] for item in root["targets"]
    )
    assert root["evidence_cutoff_ms"] == remediation_received_at_ms


def test_predecessor_failure_ignores_evidence_finalized_late(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    remediation_received_at_ms = predecessor_mature_at_ms + 1
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=remediation_received_at_ms,
    )
    root_slugs = {str(target["slug"]) for target in targets[:5]}
    raw_path = (
        dynamic_root
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
        / "control-dynamic_short_crypto_service.jsonl"
    )
    records = [
        json.loads(line) for line in raw_path.read_bytes().splitlines()
    ]
    retained: list[dict[str, object]] = []
    late_records: list[dict[str, object]] = []
    for record in records:
        inner = record["payload"]
        event_payload = inner.get("payload")
        if (
            inner.get("event_type") == "gamma_target_verified"
            and isinstance(event_payload, dict)
            and event_payload.get("slug") in root_slugs
        ):
            late_records.append(record)
        else:
            retained.append(record)
    assert len(late_records) == 5
    _replace_finalized_records(raw_path, retained)
    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "late-gamma-after-remediation",
        late_records,
        finalized_at_ms=remediation_received_at_ms + 1,
    )

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    root = report["cohort_history"][0]
    assert root["complete_target_count"] == 15
    assert root["completeness_ratio"] == "0.75"
    assert root["gate_status"] == "failed_retained_for_remediation"
    assert root["evidence_cutoff_ms"] == remediation_received_at_ms
    assert report["gate_status"] == (
        "passed_after_remediation_with_retained_failures"
    )


def test_multilevel_remediation_retains_every_failed_predecessor(
    tmp_path: Path,
) -> None:
    first_received_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS + 1
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=70,
        continuity_failures=frozenset(
            {*range(20), *range(25, 45)}
        ),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=first_received_at_ms,
    )
    first_remediation = next(
        record
        for record in _control_service_records(dynamic_root)
        if record["payload"].get("event_type")
        == "lifecycle_remediation_cohort_committed"
    )
    first_remediation_record_id = str(first_remediation["record_id"])
    second_received_at_ms = (
        int(targets[44]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS
        + 1
    )
    second_payload = _remediation_cohort_payload(
        first_remediation_record_id,
        int(targets[50]["opens_at_ms"]),
    )
    second_payload["scheduler_now_ms"] = second_received_at_ms
    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "second-remediation",
        [
            _service(
                "lifecycle_remediation_cohort_committed",
                second_payload,
                received_at_ms=second_received_at_ms,
                kind="command",
            )
        ],
    )

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    assert report["prior_failed_cohort_count"] == 2
    assert [
        cohort["gate_status"] for cohort in report["cohort_history"]
    ] == [
        "failed_retained_for_remediation",
        "failed_retained_for_remediation",
        "passed_after_remediation_with_retained_failures",
    ]
    assert report["gate_status"] == (
        "passed_after_remediation_with_retained_failures"
    )


def test_remediation_cannot_be_committed_before_predecessor_is_mature(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=predecessor_mature_at_ms - 1,
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=int(targets[-1]["closes_at_ms"])
            + SETTLEMENT_TIMEOUT_MS,
        )

    assert captured.value.code == "cohort_remediation_committed_too_early"


def test_remediation_requires_exact_hex_predecessor(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=predecessor_mature_at_ms + 1,
        remediation_predecessor_record_id="g" * 64,
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=int(targets[-1]["closes_at_ms"])
            + SETTLEMENT_TIMEOUT_MS,
        )

    assert captured.value.code == "cohort_remediation_invalid"


def test_remediation_chain_rejects_branches(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=predecessor_mature_at_ms + 1,
        remediation_branch=True,
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=int(targets[-1]["closes_at_ms"])
            + SETTLEMENT_TIMEOUT_MS,
        )

    assert captured.value.code == "cohort_remediation_branch"


def test_passing_predecessor_cannot_be_replaced_by_remediation(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=predecessor_mature_at_ms + 1,
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=int(targets[-1]["closes_at_ms"])
            + SETTLEMENT_TIMEOUT_MS,
        )

    assert captured.value.code == (
        "cohort_remediation_predecessor_not_failed"
    )


def test_future_remediation_is_excluded_by_as_of_cutoff(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=predecessor_mature_at_ms + 1,
    )

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=predecessor_mature_at_ms,
    )

    assert report["cohort_mode"] == "prospective_committed"
    assert len(report["cohort_history"]) == 1
    assert report["cohort_history"][0]["gate_status"] == "failed"
    assert report["active_cohort_index"] == 0
    assert [item["slug"] for item in report["targets"]] == [
        target["slug"] for target in targets[:20]
    ]


def test_remediation_report_is_canonical_and_byte_reproducible(
    tmp_path: Path,
) -> None:
    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(range(20)),
        cohort_eligibility_start_ms=int(_target(0)["opens_at_ms"]),
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=predecessor_mature_at_ms + 1,
    )
    as_of_ms = (
        int(targets[-1]["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )

    first = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )
    second = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )
    first_path = write_phase2_lifecycle_report(
        first,
        output_base=tmp_path / "reports",
    )
    first_bytes = first_path.read_bytes()
    second_path = write_phase2_lifecycle_report(
        second,
        output_base=tmp_path / "reports",
    )

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["report_id"] == second["report_id"]
    assert first_path == second_path
    assert second_path.read_bytes() == first_bytes


def test_fixed_as_of_report_ignores_later_finalization_and_partial_rotation(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )
    as_of_ms = (
        int(targets[-1]["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    first_partial = (
        dynamic_root
        / "groups"
        / "partial-diagnostics"
        / "raw"
        / "clob_market_ws"
        / "first.jsonl.partial"
    )
    first_partial.parent.mkdir(parents=True)
    first_partial.write_text("not-finalized\n", encoding="utf-8")
    first = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )

    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "finalized-after-fixed-cutoff",
        [
            _service(
                "post_cutoff_diagnostic",
                {"ignored": True},
                received_at_ms=as_of_ms + 1,
            )
        ],
        finalized_at_ms=as_of_ms + 2,
    )
    second_partial = first_partial.with_name("second.jsonl.partial")
    second_partial.write_text("also-not-finalized\n", encoding="utf-8")
    second = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )

    assert canonical_json_bytes(second) == canonical_json_bytes(first)
    assert second["report_id"] == first["report_id"]
    assert second["input_inventory"] == first["input_inventory"]
    assert second["excluded_partial_count"] == 0
    assert second["excluded_partial_paths"] == []


def test_fixed_as_of_excludes_post_cutoff_pair_before_raw_validation(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )
    as_of_ms = (
        int(targets[-1]["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    expected = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )
    future_manifest = _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "corrupt-raw-finalized-after-cutoff",
        [
            _service(
                "post_cutoff_diagnostic",
                {"ignored": True},
                received_at_ms=as_of_ms + 1,
            )
        ],
        finalized_at_ms=as_of_ms + 2,
    )
    future_raw = future_manifest.with_name(
        "corrupt-raw-finalized-after-cutoff.jsonl"
    )
    future_raw.write_bytes(b"{not-valid-finalized-json\n")

    actual = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )

    assert canonical_json_bytes(actual) == canonical_json_bytes(expected)
    assert actual["report_id"] == expected["report_id"]


def test_fixed_as_of_finalization_skips_post_cutoff_group_raw(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )
    as_of_ms = (
        int(targets[-1]["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    expected = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )
    group_id = f"group-{targets[0]['slug']}"
    future_manifest = _finalize(
        dynamic_root / "groups" / group_id,
        "clob_market_ws",
        "corrupt-group-raw-finalized-after-cutoff",
        [
            _transport(
                "price_change",
                received_at_ms=as_of_ms + 1,
                connection_id=group_id,
                asset_id=str(targets[0]["up_token_id"]),
            )
        ],
        finalized_at_ms=as_of_ms + 2,
    )
    future_raw = future_manifest.with_name(
        "corrupt-group-raw-finalized-after-cutoff.jsonl"
    )
    future_raw.write_bytes(b"{not-valid-finalized-json\n")

    actual = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=as_of_ms,
    )

    assert canonical_json_bytes(actual) == canonical_json_bytes(expected)
    assert actual["report_id"] == expected["report_id"]


def test_fixed_as_of_rejects_post_cutoff_manifest_identity_escape(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )
    as_of_ms = (
        int(targets[-1]["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    future_manifest = _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "malicious-manifest-finalized-after-cutoff",
        [
            _service(
                "post_cutoff_diagnostic",
                {"ignored": True},
                received_at_ms=as_of_ms + 1,
            )
        ],
        finalized_at_ms=as_of_ms + 2,
    )
    document = json.loads(future_manifest.read_text(encoding="utf-8"))
    document["raw_path"] = "../../outside-cutoff.jsonl"
    future_manifest.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=as_of_ms,
        )

    assert captured.value.code == "source_manifest_identity_invalid"


def test_audit_rejects_omitted_sibling_run_root(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    omitted = runs_root / "run-a"
    supplied = runs_root / "run-b"
    omitted.mkdir(parents=True)
    supplied.mkdir()
    main_root = tmp_path / "main"
    main_root.mkdir()

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (supplied,),
            main_capture_root=main_root,
            as_of_ms=BASE_OPEN_MS,
        )

    assert captured.value.code == "dynamic_roots_incomplete"


def test_commitment_outer_envelope_requires_command_kind(
    tmp_path: Path,
) -> None:
    main_root = tmp_path / "main"
    main_root.mkdir()
    dynamic_root = tmp_path / "dynamic-run"
    _finalize(
        dynamic_root / "control",
        "dynamic_short_crypto_service",
        "data-kind-commitment",
        [
            _service(
                "lifecycle_cohort_committed",
                _root_cohort_payload(BASE_OPEN_MS),
                received_at_ms=BASE_OPEN_MS - 600_000,
                kind="data",
            )
        ],
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=BASE_OPEN_MS,
        )

    assert captured.value.code == "cohort_commitment_invalid"


def test_remediation_chain_rejects_duplicate_record_ids(
    tmp_path: Path,
) -> None:
    manifest_root = (
        tmp_path
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
    )
    root = lifecycle_audit_module._ServiceEvent(
        event_type="lifecycle_cohort_committed",
        payload=_root_cohort_payload(BASE_OPEN_MS),
        record_id="a" * 64,
        manifest_path=manifest_root / "root.manifest.json",
        received_at_ms=BASE_OPEN_MS - 600_000,
    )
    remediation_payload = _remediation_cohort_payload(
        root.record_id,
        BASE_OPEN_MS + 3_600_000,
    )
    remediation_payload["scheduler_now_ms"] = BASE_OPEN_MS + 1
    duplicates = tuple(
        lifecycle_audit_module._ServiceEvent(
            event_type="lifecycle_remediation_cohort_committed",
            payload=remediation_payload,
            record_id="b" * 64,
            manifest_path=manifest_root
            / f"duplicate-{index}.manifest.json",
            received_at_ms=BASE_OPEN_MS + index + 1,
        )
        for index in range(2)
    )

    with pytest.raises(LifecycleAuditError) as captured:
        lifecycle_audit_module._cohort_commitment_chain(
            (root, *duplicates),
            as_of_ms=BASE_OPEN_MS + 7_200_000,
            sample_size=20,
            settlement_timeout_ms=SETTLEMENT_TIMEOUT_MS,
            threshold=lifecycle_audit_module.DEFAULT_THRESHOLD,
        )

    assert captured.value.code == "cohort_remediation_duplicate"


def test_remediation_chain_reports_cycle_independently(
    tmp_path: Path,
) -> None:
    manifest_root = (
        tmp_path
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
    )
    root = lifecycle_audit_module._ServiceEvent(
        event_type="lifecycle_cohort_committed",
        payload=_root_cohort_payload(BASE_OPEN_MS),
        record_id="a" * 64,
        manifest_path=manifest_root / "root.manifest.json",
        received_at_ms=BASE_OPEN_MS - 600_000,
    )
    cycle: list[lifecycle_audit_module._ServiceEvent] = []
    for index, (record_id, predecessor_id) in enumerate(
        (("b" * 64, "c" * 64), ("c" * 64, "b" * 64))
    ):
        payload = _remediation_cohort_payload(
            predecessor_id,
            BASE_OPEN_MS + 3_600_000 + index * 300_000,
        )
        payload["scheduler_now_ms"] = BASE_OPEN_MS + index + 1
        cycle.append(
            lifecycle_audit_module._ServiceEvent(
                event_type="lifecycle_remediation_cohort_committed",
                payload=payload,
                record_id=record_id,
                manifest_path=manifest_root
                / f"cycle-{index}.manifest.json",
                received_at_ms=BASE_OPEN_MS + index + 1,
            )
        )

    with pytest.raises(LifecycleAuditError) as captured:
        lifecycle_audit_module._cohort_commitment_chain(
            (root, *cycle),
            as_of_ms=BASE_OPEN_MS + 7_200_000,
            sample_size=20,
            settlement_timeout_ms=SETTLEMENT_TIMEOUT_MS,
            threshold=lifecycle_audit_module.DEFAULT_THRESHOLD,
        )

    assert captured.value.code == "cohort_remediation_cycle"


def test_audit_closes_record_inventory_on_post_load_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_root = tmp_path / "main"
    dynamic_root = tmp_path / "dynamic-run"
    main_root.mkdir()
    dynamic_root.mkdir()
    closes = 0
    original_close = lifecycle_audit_module._RecordInventory.close

    def counted_close(
        inventory: lifecycle_audit_module._RecordInventory,
    ) -> None:
        nonlocal closes
        closes += 1
        original_close(inventory)

    def reject_after_inventory(*args: object, **kwargs: object) -> object:
        raise LifecycleAuditError("forced_after_inventory", "fixture")

    monkeypatch.setattr(
        lifecycle_audit_module._RecordInventory,
        "close",
        counted_close,
    )
    monkeypatch.setattr(
        lifecycle_audit_module,
        "_cohort_commitment_chain",
        reject_after_inventory,
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=BASE_OPEN_MS,
        )

    assert captured.value.code == "forced_after_inventory"
    assert closes == 1


def test_commitment_rejects_boolean_zero_safety_counter(
    tmp_path: Path,
) -> None:
    manifest_path = (
        tmp_path
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
        / "commitment.manifest.json"
    )
    payload = {
        "scheduler_now_ms": BASE_OPEN_MS - 600_000,
        "schema_version": (
            "edge-lab.phase2-lifecycle-prospective-cohort.v1"
        ),
        "selection_rule": (
            "first_mature_targets_opening_at_or_after_boundary"
        ),
        "eligibility_start_ms": BASE_OPEN_MS,
        "sample_size": 20,
        "threshold": "0.8",
        "settlement_timeout_ms": SETTLEMENT_TIMEOUT_MS,
        "assets": ["BTC", "ETH"],
        "horizons": ["5m", "15m"],
        "selection_order": ["opens_at_ms", "closes_at_ms", "slug"],
        "earliest_valid_commitment_wins": True,
        "must_be_durable_before_public_network": True,
        "actual_fill": False,
        "authenticated_fill": False,
        "orders_submitted": False,
        "authenticated_endpoints_used": 0,
    }
    event = lifecycle_audit_module._ServiceEvent(
        event_type="lifecycle_cohort_committed",
        payload=payload,
        record_id="a" * 64,
        manifest_path=manifest_path,
        received_at_ms=BASE_OPEN_MS - 600_000,
    )

    with pytest.raises(LifecycleAuditError) as captured:
        lifecycle_audit_module._prospective_cohort_commitment(
            (event,),
            as_of_ms=BASE_OPEN_MS,
            sample_size=20,
            settlement_timeout_ms=SETTLEMENT_TIMEOUT_MS,
            threshold=lifecycle_audit_module.DEFAULT_THRESHOLD,
        )

    assert captured.value.code == "cohort_commitment_invalid"


def test_partial_evidence_is_ignored_and_cannot_raise_completeness(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(tmp_path)
    fake = _transport(
        "price_change",
        received_at_ms=int(targets[16]["opens_at_ms"]) + 40_000,
        connection_id=f"group-{targets[16]['slug']}",
        asset_id=str(targets[16]["up_token_id"]),
    )
    partial = (
        dynamic_root
        / "groups"
        / f"group-{targets[16]['slug']}"
        / "raw"
        / "clob_market_ws"
        / "unfinalized.jsonl.partial"
    )
    partial.write_text(json.dumps(fake) + "\n", encoding="utf-8")

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    assert report["complete_target_count"] == 16
    assert report["excluded_partial_count"] == 0
    assert report["excluded_partial_paths"] == []
    assert report["targets"][16]["missing_stages"] == [
        "clob_increment_continuity",
    ]


def test_gate_waits_until_twenty_targets_are_mature(tmp_path: Path) -> None:
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[18]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    assert report["mature_target_count"] == 19
    assert report["sampled_target_count"] == 19
    assert report["complete_target_count"] == 19
    assert report["threshold_met"] is False
    assert report["gate_status"] == "waiting_for_mature_targets"


def test_lifecycle_audit_reconstructs_mixed_v1_v2_registry_journal(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root = _build_mixed_registry_capture(tmp_path)

    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=BASE_OPEN_MS - 1,
    )

    assert report["registered_target_count"] == 2
    assert report["mature_target_count"] == 0
    assert report["gate_status"] == "waiting_for_mature_targets"


def test_lifecycle_audit_rejects_v2_registry_cumulative_proof_mismatch(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root = _build_mixed_registry_capture(
        tmp_path,
        cumulative_target_count=3,
    )

    with pytest.raises(LifecycleAuditError) as captured:
        audit_phase2_lifecycle(
            (dynamic_root,),
            main_capture_root=main_root,
            as_of_ms=BASE_OPEN_MS - 1,
        )

    assert captured.value.code == "registry_revision_cumulative_mismatch"


def test_lifecycle_report_is_content_addressed_and_byte_reproducible(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )
    report = audit_phase2_lifecycle(
        (dynamic_root,),
        main_capture_root=main_root,
        as_of_ms=int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    )

    first = write_phase2_lifecycle_report(
        report,
        output_base=tmp_path / "reports",
    )
    first_bytes = first.read_bytes()
    second = write_phase2_lifecycle_report(
        report,
        output_base=tmp_path / "reports",
    )

    assert first == second
    assert first.name == "LIFECYCLE_COMPLETENESS.json"
    assert first.parent.name == report["report_id"]
    assert second.read_bytes() == first_bytes


def test_lifecycle_report_is_independent_of_dynamic_root_argument_order(
    tmp_path: Path,
) -> None:
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )
    empty_dynamic_root = tmp_path / "empty-dynamic-run"
    empty_dynamic_root.mkdir()
    arguments = {
        "main_capture_root": main_root,
        "as_of_ms": int(targets[-1]["closes_at_ms"])
        + SETTLEMENT_TIMEOUT_MS,
    }

    first = audit_phase2_lifecycle(
        (dynamic_root, empty_dynamic_root),
        **arguments,
    )
    second = audit_phase2_lifecycle(
        (empty_dynamic_root, dynamic_root),
        **arguments,
    )

    assert second == first


def test_lifecycle_cli_writes_report_without_network_or_orders(
    tmp_path: Path,
    capsys: object,
) -> None:
    from scripts.build_phase2_lifecycle_report import main

    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        continuity_failures=frozenset(),
    )
    exit_code = main(
        [
            "--dynamic-run-root",
            str(dynamic_root),
            "--main-capture-root",
            str(main_root),
            "--as-of-ms",
            str(
                int(targets[-1]["closes_at_ms"])
                + SETTLEMENT_TIMEOUT_MS
            ),
            "--output-base",
            str(tmp_path / "reports"),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["gate_status"] == "passed"
    assert payload["orders_submitted"] == 0
    assert payload["authenticated_endpoints_used"] == 0
    assert Path(payload["report_path"]).is_file()


def test_lifecycle_cli_fails_closed_for_active_remediation_failure(
    tmp_path: Path,
    capsys: object,
) -> None:
    from scripts.build_phase2_lifecycle_report import main

    predecessor_mature_at_ms = (
        int(_target(19)["closes_at_ms"]) + SETTLEMENT_TIMEOUT_MS
    )
    root_start_ms = int(_target(0)["opens_at_ms"])
    main_root, dynamic_root, targets = _build_capture(
        tmp_path,
        target_count=45,
        continuity_failures=frozenset(
            {*range(20), *range(25, 45)}
        ),
        cohort_eligibility_start_ms=root_start_ms,
        remediation_eligibility_start_ms=int(
            _target(25)["opens_at_ms"]
        ),
        remediation_received_at_ms=predecessor_mature_at_ms + 1,
    )
    assert root_start_ms == targets[0]["opens_at_ms"]

    exit_code = main(
        [
            "--dynamic-run-root",
            str(dynamic_root),
            "--main-capture-root",
            str(main_root),
            "--as-of-ms",
            str(
                int(targets[-1]["closes_at_ms"])
                + SETTLEMENT_TIMEOUT_MS
            ),
            "--output-base",
            str(tmp_path / "reports"),
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    report = json.loads(
        Path(payload["report_path"]).read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == (
        "edge-lab.phase2-lifecycle-completeness-cli.v3"
    )
    assert payload["gate_status"] == (
        "remediation_failed_with_retained_failures"
    )
    assert payload["active_cohort_index"] == 1
    assert payload["active_cohort_commitment_record_id"] == report[
        "active_cohort_commitment"
    ]["record_id"]
    assert payload["active_cohort_eligibility_start_ms"] == report[
        "active_cohort_commitment"
    ]["eligibility_start_ms"]
    assert payload["prior_failed_cohort_count"] == 1
