from __future__ import annotations

import gc
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import tracemalloc
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Callable

import pytest
import src.edge_lab.dynamic_short_crypto_recovery as recovery_module
from src.edge_lab.data_store import (
    CaptureStore,
    canonical_json_bytes,
    canonical_record_id_from_json,
)
from src.edge_lab.dynamic_short_crypto_recovery import (
    RecoveryInputError,
    recover_dynamic_short_crypto_runs,
)
from src.edge_lab.short_crypto_registry import (
    merge_short_crypto_registry_snapshots,
)


RAW_SCHEMA = "edge-lab-recorder.raw.v1"
RECEIVED_AT = "2026-07-24T15:00:00.000000Z"
REMEDIATION_RECEIVED_AT = "2026-07-24T16:00:00.000000Z"


def _service_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    session_id: str = "session-a",
    monotonic_ns: int = 1,
    received_at: str = RECEIVED_AT,
    kind: str = "data",
) -> dict[str, Any]:
    return {
        "schema_version": (
            "edge-lab-dynamic-short-crypto-service.record.v1"
        ),
        "source": "dynamic_short_crypto_service",
        "kind": kind,
        "event_type": event_type,
        "session_id": session_id,
        "connection_id": None,
        "received_at": received_at,
        "event_at": None,
        "sequence": None,
        "monotonic_ns": monotonic_ns,
        "payload": payload,
    }


def _finalize_events(
    run_root: Path,
    events: list[dict[str, Any]],
    *,
    source: str = "dynamic_short_crypto_service",
    batch_id: str = "batch-a",
    store_relative: str = "control",
    received_at: str = RECEIVED_AT,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    store = CaptureStore(run_root / store_relative)
    writer = store.open_raw_batch(
        source=source,
        batch_id=batch_id,
        schema_version=RAW_SCHEMA,
    )
    record_ids = []
    for sequence, event in enumerate(events):
        result = writer.append(
            received_at=received_at,
            event_at=None,
            sequence=sequence,
            payload=event,
        )
        record_ids.append(result.record_id)
    manifest = writer.finalize(finalized_at=received_at)
    return manifest, tuple(record_ids)


def _write_close_checkpoint(
    run_root: Path,
    manifest: dict[str, Any],
    *,
    recorder_checkpoint: dict[str, Any] | None = None,
) -> None:
    CaptureStore(run_root / "control").write_checkpoint(
        "dynamic_short_crypto_service",
        {
            "schema_version": "edge-lab-capture-runtime.checkpoint.v1",
            "recorder_checkpoint": recorder_checkpoint,
            "capture_runtime": {
                "reason": "close",
                "last_finalized_batch": {
                    "batch_id": manifest["batch_id"],
                    "raw_path": manifest["raw_path"],
                    "record_count": manifest["record_count"],
                    "finalized_at": manifest["finalized_at"],
                    "checksum": manifest["checksum"],
                },
                "startup_integrity": {
                    "orphan_partials": [],
                    "raw_without_manifest": [],
                    "manifest_without_raw": [],
                    "checksum_mismatches": [],
                    "invalid_manifests": [],
                },
            },
        },
        updated_at=RECEIVED_AT,
    )


def _finalize_recovery_anchor(
    run_root: Path,
    outcome: Any,
    *,
    batch_id: str = "recovery-anchor",
    monotonic_ns: int = 1,
    anchor_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    result = outcome.result
    persisted_anchor = (
        outcome.anchor_payload
        if anchor_payload is None
        else anchor_payload
    )
    manifest, record_ids = _finalize_events(
        run_root,
        [
            _service_event(
                "restart_recovery_decision",
                {
                    **result.recovery_decision,
                    "run_classifications": list(
                        result.run_classifications
                    ),
                    "gaps": list(result.gaps),
                    "exclusions": list(result.exclusions),
                    "worker_actions": list(result.worker_actions),
                    "scheduler_now_ms": 1_784_990_950_000,
                    "recovery_anchor": deepcopy(
                        persisted_anchor
                    ),
                },
                monotonic_ns=monotonic_ns,
                kind="command",
            )
        ],
        batch_id=batch_id,
    )
    _write_close_checkpoint(
        run_root,
        manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": record_ids[0],
            "reason": "restart_recovery_durable_before_apply",
            "updated_at": RECEIVED_AT,
        },
    )
    return manifest, record_ids


def _refresh_manifest_checksum(raw_path: Path) -> None:
    manifest_path = raw_path.with_suffix(".manifest.json")
    raw = raw_path.read_bytes()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["record_count"] = raw.count(b"\n")
    manifest["checksum"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "lines": raw.count(b"\n"),
    }
    manifest_path.chmod(0o644)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )


def _rehash_forged_snapshot(
    snapshot_path: Path,
    *,
    mutate_state: Callable[[dict[str, Any]], None],
) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    result = snapshot["result"]
    mutate_state(result["state"])
    state_hash = hashlib.sha256(
        canonical_json_bytes(result["state"])
    ).hexdigest()
    result["state_hash"] = state_hash
    decision_body = {
        key: value
        for key, value in result["recovery_decision"].items()
        if key != "decision_id"
    }
    decision_body["state_hash"] = state_hash
    decision = {
        **decision_body,
        "decision_id": hashlib.sha256(
            canonical_json_bytes(decision_body)
        ).hexdigest(),
    }
    result["recovery_decision"] = decision
    for action in result["worker_actions"]:
        action["blocked_until_recovery_decision_id"] = decision[
            "decision_id"
        ]
    body = {
        key: value for key, value in snapshot.items() if key != "snapshot_id"
    }
    snapshot = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(canonical_json_bytes(snapshot) + b"\n")
    snapshot_path.chmod(0o444)


def _rewrite_snapshot_from_forged_checkpoint(
    snapshot_path: Path,
    *,
    run_roots: tuple[Path, ...],
    mutate_checkpoint: Callable[[dict[str, Any]], None],
) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    checkpoint_value = snapshot["replay_checkpoint"]
    mutate_checkpoint(checkpoint_value)
    checkpoint = recovery_module._recovery_replay_checkpoint_from_snapshot(
        checkpoint_value,
        expected_run_ids=tuple(path.name for path in run_roots),
    )
    result = recovery_module._recover_dynamic_short_crypto_runs(
        run_roots,
        settlement_timeout_ms=3_600_000,
        _prefix_checkpoint=checkpoint,
        _checkpoint_only_reduction=True,
    )
    body = {
        **{
            key: value
            for key, value in snapshot.items()
            if key != "snapshot_id"
        },
        "result": result.to_dict(),
        "replay_checkpoint": checkpoint.to_dict(),
    }
    forged = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(canonical_json_bytes(forged) + b"\n")
    snapshot_path.chmod(0o444)


def _coordinated_rewrite_cache_artifacts(
    snapshot_path: Path,
    *,
    run_roots: tuple[Path, ...],
    mutate: Callable[
        [Any, dict[str, Any]],
        None,
    ],
) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert len(snapshot["record_indexes"]) == 1
    descriptor = snapshot["record_indexes"][0]
    original_index = snapshot_path.parent / descriptor["path"]
    pending_index = snapshot_path.parent / "coordinated-forgery.sqlite3"
    pending_index.write_bytes(original_index.read_bytes())
    checkpoint_value = deepcopy(snapshot["replay_checkpoint"])
    connection = recovery_module.sqlite3.connect(pending_index)
    try:
        mutate(connection, checkpoint_value)
        connection.commit()
        row_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM finalized_records"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    forged_index_bytes = pending_index.read_bytes()
    forged_index_hash = hashlib.sha256(
        forged_index_bytes
    ).hexdigest()
    forged_index_name = (
        f"{snapshot_path.stem}.record-index-"
        f"{forged_index_hash}.sqlite3"
    )
    forged_index = snapshot_path.parent / forged_index_name
    pending_index.replace(forged_index)
    forged_index.chmod(0o444)
    checkpoint = recovery_module._recovery_replay_checkpoint_from_snapshot(
        checkpoint_value,
        expected_run_ids=tuple(path.name for path in run_roots),
    )
    result = recovery_module._recover_dynamic_short_crypto_runs(
        run_roots,
        settlement_timeout_ms=3_600_000,
        _prefix_checkpoint=checkpoint,
        _checkpoint_only_reduction=True,
    )
    body = {
        **{
            key: value
            for key, value in snapshot.items()
            if key != "snapshot_id"
        },
        "result": result.to_dict(),
        "replay_checkpoint": checkpoint.to_dict(),
    }
    body["record_indexes"][0].update(
        {
            "path": forged_index_name,
            "sha256": forged_index_hash,
            "bytes": len(forged_index_bytes),
            "row_count": row_count,
        }
    )
    forged_snapshot = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(
        canonical_json_bytes(forged_snapshot) + b"\n"
    )
    snapshot_path.chmod(0o444)


def _target(slug: str = "btc-updown-5m-1784991000") -> dict[str, Any]:
    return {
        "slug": slug,
        "market_id": "3081518",
        "condition_id": "0x" + "ab" * 32,
        "up_token_id": "1" * 76,
        "down_token_id": "2" * 76,
        "source_symbol": "btc/usd",
        "source_topic": "crypto_prices_chainlink",
        "horizon": "5m",
        "opens_at_ms": 1_784_991_000_000,
        "closes_at_ms": 1_784_991_300_000,
        "rule_hash": "a" * 64,
        "announcement_record_id": "b" * 64,
        "announced_at": RECEIVED_AT,
        "provenance": [],
    }


def _registry_snapshot(
    *,
    descriptor: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    snapshot = {
        "schema_version": "edge-lab.short-crypto-registry.v1",
        "inputs": [descriptor],
        "targets": [target],
        "rejections": [],
        "completeness": {
            "status": "complete",
            "input_file_count": 1,
            "input_line_count": descriptor["line_count"],
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


def _capture_decision(
    *,
    sequence: int = 7,
    group_id: str = "short-crypto-" + "1" * 24,
) -> dict[str, Any]:
    body = {
        "schema_version": "edge-lab.short-crypto-capture-decision.v1",
        "sequence": sequence,
        "action": "subscribe",
        "group_id": group_id,
        "asset_ids": ["1" * 76, "2" * 76],
        "targets": [
            {
                "slug": _target()["slug"],
                "announcement_record_id": "b" * 64,
                "rule_hash": "a" * 64,
                "up_token_id": "1" * 76,
                "down_token_id": "2" * 76,
            }
        ],
        "emitted_at_ms": 1_784_990_940_000,
        "reason": "capture_window_due",
        "minimum_snapshot_watermark_ns": None,
    }
    return {
        **body,
        "decision_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }


def _cohort_commitment_payload(
    *,
    eligibility_start_ms: int = 1_784_905_500_000,
) -> dict[str, Any]:
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
        "settlement_timeout_ms": 3_600_000,
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


def _remediation_commitment_payload(
    predecessor_record_id: str,
    *,
    eligibility_start_ms: int = 1_784_909_100_000,
) -> dict[str, Any]:
    plan_id = hashlib.sha256(
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
        "remediation_plan_id": plan_id,
        "eligibility_start_ms": eligibility_start_ms,
        "sample_size": 20,
        "threshold": "0.8",
        "settlement_timeout_ms": 3_600_000,
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


def _transport_event(
    event_type: str,
    *,
    connection_id: str,
    monotonic_ns: int,
) -> dict[str, Any]:
    return {
        "schema_version": "edge-lab-recorder.lifecycle.v1",
        "source": "clob_market_ws",
        "kind": "lifecycle",
        "event_type": event_type,
        "session_id": "worker-session",
        "connection_id": connection_id,
        "received_at": RECEIVED_AT,
        "event_at": None,
        "sequence": None,
        "monotonic_ns": monotonic_ns,
        "payload": {},
    }


def _market_event(
    *,
    connection_id: str,
    monotonic_ns: int,
) -> dict[str, Any]:
    return {
        "schema_version": "clob-market-ws.book.v1",
        "source": "clob_market_ws",
        "kind": "data",
        "event_type": "book",
        "session_id": "worker-session",
        "connection_id": connection_id,
        "received_at": RECEIVED_AT,
        "event_at": RECEIVED_AT,
        "sequence": monotonic_ns,
        "monotonic_ns": monotonic_ns,
        "payload": {
            "asset_id": "1" * 76,
            "bids": [{"price": "0.49", "size": "1000"}],
            "asks": [{"price": "0.51", "size": "1000"}],
        },
    }


def test_recovery_preserves_validated_append_only_cohort_journal(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-cohort-journal"
    root_manifest, root_record_ids = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                monotonic_ns=1,
                kind="command",
            )
        ],
        batch_id="cohort-root",
    )
    root_record_id = root_record_ids[0]
    remediation_manifest, remediation_record_ids = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_remediation_cohort_committed",
                _remediation_commitment_payload(root_record_id),
                monotonic_ns=2,
                received_at=REMEDIATION_RECEIVED_AT,
                kind="command",
            )
        ],
        batch_id="cohort-remediation",
        received_at=REMEDIATION_RECEIVED_AT,
    )
    _write_close_checkpoint(run, remediation_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"

    full = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )
    cached = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    repeated = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full.state["schema_version"] == (
        "edge-lab-dynamic-short-crypto-recovered-state.v2"
    )
    assert full.state["lifecycle_cohort_journal"] == [
        {
            "event_type": "lifecycle_cohort_committed",
            "record_id": root_record_id,
            "run_id": run.name,
            "payload": _cohort_commitment_payload(),
        },
        {
            "event_type": "lifecycle_remediation_cohort_committed",
            "record_id": remediation_record_ids[0],
            "run_id": run.name,
            "payload": _remediation_commitment_payload(root_record_id),
        },
    ]
    assert cached.canonical_bytes() == full.canonical_bytes()
    assert repeated.canonical_bytes() == full.canonical_bytes()


def test_recovery_full_cache_suffix_repeat_share_exact_cohort_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_index_shard_counts: list[int] = []
    original_membership_validator = (
        recovery_module._validate_checkpoint_index_membership
    )

    def observe_membership_validation(
        checkpoint: object,
        *,
        record_index_paths: tuple[Path, ...],
    ) -> None:
        verified_index_shard_counts.append(len(record_index_paths))
        original_membership_validator(
            checkpoint,
            record_index_paths=record_index_paths,
        )

    monkeypatch.setattr(
        recovery_module,
        "_validate_checkpoint_index_membership",
        observe_membership_validation,
    )
    root_run = tmp_path / "runs" / "run-00-root"
    root_manifest, root_record_ids = _finalize_events(
        root_run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
        batch_id="cohort-root",
    )
    _write_close_checkpoint(root_run, root_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        root_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    suffix_run = tmp_path / "runs" / "run-01-remediation"
    _finalize_recovery_anchor(
        suffix_run,
        prefix,
        batch_id="recovery-anchor",
    )
    remediation_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "lifecycle_remediation_cohort_committed",
                _remediation_commitment_payload(root_record_ids[0]),
                received_at=REMEDIATION_RECEIVED_AT,
                kind="command",
            )
        ],
        batch_id="cohort-remediation",
        received_at=REMEDIATION_RECEIVED_AT,
    )
    _write_close_checkpoint(suffix_run, remediation_manifest)

    full = recover_dynamic_short_crypto_runs(
        (root_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    suffix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (root_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    repeated = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (root_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert prefix.state["lifecycle_cohort_journal"] == [
        full.state["lifecycle_cohort_journal"][0]
    ]
    assert suffix.canonical_bytes() == full.canonical_bytes()
    assert repeated.canonical_bytes() == full.canonical_bytes()
    assert verified_index_shard_counts == [1, 1, 2, 2, 1]


def test_recovery_rejects_data_kind_cohort_commitment(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-cohort-data-kind"
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="data",
            )
        ],
    )
    _write_close_checkpoint(run, manifest)

    with pytest.raises(RecoveryInputError) as captured:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert captured.value.code == "lifecycle_cohort_journal_invalid"


def test_recovery_cohort_journal_rejects_duplicate_record_ids(
    tmp_path: Path,
) -> None:
    manifest_root = (
        tmp_path
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
    )
    root = recovery_module._RecoveredCohortEvent(
        event_type="lifecycle_cohort_committed",
        record_id="a" * 64,
        run_id="run-a",
        manifest_path=manifest_root / "root.manifest.json",
        received_at_ms=1,
        payload=_cohort_commitment_payload(),
    )
    duplicate = recovery_module._RecoveredCohortEvent(
        event_type="lifecycle_remediation_cohort_committed",
        record_id="b" * 64,
        run_id="run-a",
        manifest_path=manifest_root / "duplicate.manifest.json",
        received_at_ms=2,
        payload=_remediation_commitment_payload(root.record_id),
    )

    with pytest.raises(
        RecoveryInputError,
        match="duplicate cohort commitment record ID",
    ):
        recovery_module._validated_cohort_journal(
            [root, duplicate, duplicate]
        )


def test_recovery_cohort_journal_reports_cycle_independently(
    tmp_path: Path,
) -> None:
    manifest_root = (
        tmp_path
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
    )
    root = recovery_module._RecoveredCohortEvent(
        event_type="lifecycle_cohort_committed",
        record_id="a" * 64,
        run_id="run-a",
        manifest_path=manifest_root / "root.manifest.json",
        received_at_ms=1,
        payload=_cohort_commitment_payload(),
    )
    cycle = [
        recovery_module._RecoveredCohortEvent(
            event_type="lifecycle_remediation_cohort_committed",
            record_id=record_id,
            run_id="run-a",
            manifest_path=manifest_root / f"cycle-{index}.manifest.json",
            received_at_ms=index + 2,
            payload=_remediation_commitment_payload(predecessor),
        )
        for index, (record_id, predecessor) in enumerate(
            (("b" * 64, "c" * 64), ("c" * 64, "b" * 64))
        )
    ]

    with pytest.raises(RecoveryInputError, match="cyclic"):
        recovery_module._validated_cohort_journal([root, *cycle])


def test_recovery_rejects_cohort_scheduler_after_durable_receive_time(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-cohort-time-drift"
    payload = _cohort_commitment_payload()
    payload["scheduler_now_ms"] = 1_784_905_260_000
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                payload,
                kind="command",
            )
        ],
        batch_id="cohort-time-drift",
    )
    _write_close_checkpoint(run, manifest)

    with pytest.raises(RecoveryInputError) as captured:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert captured.value.code == "lifecycle_cohort_journal_invalid"


def test_recovery_rejects_cohort_commitment_outside_control(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-cohort-wrong-location"
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
        batch_id="cohort-wrong-location",
        store_relative="other",
    )
    _write_close_checkpoint(run, manifest)

    with pytest.raises(RecoveryInputError) as captured:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert captured.value.code == "lifecycle_cohort_journal_invalid"


def test_recovery_uses_auditor_earliest_root_order(
    tmp_path: Path,
) -> None:
    earlier_run = tmp_path / "runs" / "z-earlier-root"
    earlier_manifest, earlier_record_ids = _finalize_events(
        earlier_run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
        batch_id="earlier-root",
    )
    _write_close_checkpoint(earlier_run, earlier_manifest)

    later_received_at = "2026-07-24T15:30:00.000000Z"
    later_run = tmp_path / "runs" / "a-later-root"
    later_manifest, _ = _finalize_events(
        later_run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(
                    eligibility_start_ms=1_784_907_300_000,
                ),
                received_at=later_received_at,
                kind="command",
            )
        ],
        batch_id="later-root",
        received_at=later_received_at,
    )
    _write_close_checkpoint(later_run, later_manifest)

    result = recover_dynamic_short_crypto_runs(
        (later_run, earlier_run),
        settlement_timeout_ms=3_600_000,
    )

    assert result.state["lifecycle_cohort_journal"][0]["record_id"] == (
        earlier_record_ids[0]
    )


def test_recovery_validates_repetitive_book_batches_with_bounded_overhead(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-repetitive-book-batch"
    levels = [
        {
            "price": f"0.{index % 100:02d}",
            "size": str(index + 1),
        }
        for index in range(200)
    ]
    event = {
        **_market_event(
            connection_id="connection-a",
            monotonic_ns=1,
        ),
        "payload": {
            "asset_id": "1" * 76,
            "bids": levels,
            "asks": levels,
        },
    }
    _finalize_events(
        run,
        [event] * 500,
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    raw_path = next(run.rglob("*.jsonl"))
    raw = raw_path.read_bytes()
    decode_samples: list[float] = []
    for _ in range(3):
        started = time.perf_counter()
        decoded = [json.loads(line) for line in raw.splitlines()]
        decode_samples.append(time.perf_counter() - started)
        assert len(decoded) == 500
    decode_baseline = min(decode_samples)

    started = time.perf_counter()
    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )
    recovery_seconds = time.perf_counter() - started

    assert result.replayed_record_count == 500
    assert recovery_seconds <= decode_baseline * 5, (
        "finalized validation overhead is unbounded: "
        f"recovery={recovery_seconds:.6f}s, "
        f"decode={decode_baseline:.6f}s, "
        f"ratio={recovery_seconds / decode_baseline:.2f}"
    )


def test_recovery_memory_does_not_scale_with_irrelevant_record_id_history(
    tmp_path: Path,
) -> None:
    def recover_peak_bytes(run: Path, *, batch_count: int) -> int:
        records_per_batch = 200
        for batch_index in range(batch_count):
            offset = batch_index * records_per_batch
            _finalize_events(
                run,
                [
                    _market_event(
                        connection_id="connection-a",
                        monotonic_ns=offset + record_index + 1,
                    )
                    for record_index in range(records_per_batch)
                ],
                source="clob_market_ws",
                batch_id=f"clob-{batch_index:04d}",
                store_relative="groups/group-a",
            )
        gc.collect()
        tracemalloc.start()
        try:
            result = recover_dynamic_short_crypto_runs(
                run,
                settlement_timeout_ms=3_600_000,
            )
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert result.replayed_record_count == (
            batch_count * records_per_batch
        )
        return peak_bytes

    small_peak = recover_peak_bytes(
        tmp_path / "runs" / "run-small-history",
        batch_count=1,
    )
    large_peak = recover_peak_bytes(
        tmp_path / "runs" / "run-large-history",
        batch_count=60,
    )

    assert large_peak <= small_peak * 3, (
        "irrelevant finalized market rows were retained in Python memory: "
        f"small_peak={small_peak}, large_peak={large_peak}, "
        f"ratio={large_peak / small_peak:.2f}"
    )


def test_recovery_compacts_production_v1_cumulative_registry_history(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-production-v1-registry"
    inputs = [
        {
            "path": f"raw/clob_market_ws/input-{index:04d}.jsonl",
            "sha256": f"{index:064x}",
            "byte_count": 100 + index,
            "line_count": 1,
        }
        for index in range(500)
    ]
    registry = {
        "schema_version": "edge-lab.short-crypto-registry.v1",
        "snapshot_sha256": "a" * 64,
        "inputs": inputs,
        "targets": [],
        "rejections": [],
        "completeness": {"status": "complete"},
    }
    _finalize_events(
        run,
        [
            _service_event(
                "registry_revision",
                registry,
                monotonic_ns=index + 1,
            )
            for index in range(80)
        ],
    )
    raw_path = next(run.rglob("*.jsonl"))
    raw = raw_path.read_bytes()
    decode_samples: list[float] = []
    for _ in range(3):
        started = time.perf_counter()
        decoded = [json.loads(line) for line in raw.splitlines()]
        decode_samples.append(time.perf_counter() - started)
        assert len(decoded) == 80
    decode_baseline = median(decode_samples)

    recovery_samples: list[float] = []
    for _ in range(3):
        started = time.perf_counter()
        result = recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )
        recovery_samples.append(time.perf_counter() - started)
    recovery_seconds = median(recovery_samples)

    assert result.replayed_record_count == 80
    assert len(
        result.state["processed_finalized_discovery_descriptors"]
    ) == 500
    # Keep the regression meaningful without making the benchmark brittle on
    # slower Windows runners or under full-suite contention.
    assert recovery_seconds <= decode_baseline * 16, (
        "production v1 cumulative revisions were replayed repeatedly: "
        f"recovery={recovery_seconds:.6f}s, "
        f"decode={decode_baseline:.6f}s, "
        f"ratio={recovery_seconds / decode_baseline:.2f}"
    )


def test_cached_recovery_indexes_canonical_rows_before_registry_compaction(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-canonical-registry-index"
    registry = {
        "schema_version": "edge-lab.short-crypto-registry.v1",
        "snapshot_sha256": "a" * 64,
        "inputs": [
            {
                "path": "/capture/input.jsonl",
                "sha256": "b" * 64,
                "byte_count": 100,
                "line_count": 1,
            }
        ],
        "targets": [],
        "rejections": [],
        "completeness": {"status": "complete"},
    }
    _finalize_events(
        run,
        [
            _service_event(
                "registry_revision",
                registry,
                monotonic_ns=index + 1,
            )
            for index in range(2)
        ],
    )

    outcome = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=tmp_path / "service" / "recovery-snapshot.json",
    )

    assert outcome.result.replayed_record_count == 2


def test_content_addressed_snapshot_reuses_exact_validated_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-snapshot-hit"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"

    first = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    assert first.anchor_payload["schema_version"] == (
        "edge-lab-dynamic-short-crypto-recovery-anchor.v1"
    )
    assert first.anchor_payload["recovered_from_run_ids"] == [
        "run-snapshot-hit"
    ]
    assert "recovery_anchor" not in first.result.to_dict()
    assert snapshot_path.stat().st_mode & 0o777 == 0o444
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert len(snapshot["record_indexes"]) == 1
    index_descriptor = snapshot["record_indexes"][0]
    index_path = snapshot_path.parent / index_descriptor["path"]
    index_bytes = index_path.read_bytes()
    assert index_path.stat().st_mode & 0o777 == 0o444
    assert index_descriptor == {
        "schema_version": (
            "edge-lab-dynamic-short-crypto-record-index.v2"
        ),
        "path": index_path.name,
        "sha256": hashlib.sha256(index_bytes).hexdigest(),
        "bytes": len(index_bytes),
        "row_count": 1,
        "run_ids": ["run-snapshot-hit"],
    }
    connection = recovery_module.sqlite3.connect(
        f"{index_path.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM finalized_records"
        ).fetchone() == (1,)
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {
            "finalized_record_sources",
            "finalized_records",
            "replay_records",
            "recovery_run_inventory",
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM replay_records"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM recovery_run_inventory"
        ).fetchone() == (1,)
    finally:
        connection.close()

    successor = tmp_path / "runs" / "run-snapshot-hit-anchor"
    _finalize_recovery_anchor(successor, first)
    expected = recover_dynamic_short_crypto_runs(
        (run, successor),
        settlement_timeout_ms=3_600_000,
    )
    original_validate = recovery_module._validate_finalized_pair

    def reject_prefix_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        if run_root == run.resolve():
            raise AssertionError(
                "an anchored snapshot must reuse prior row proofs"
            )
        return original_validate(run_root, run_id, raw_path)

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_prefix_revalidation,
    )
    second = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert second.canonical_bytes() == expected.canonical_bytes()


def test_cached_recovery_reverifies_byte_identical_prefix_after_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    initial_snapshot = json.loads(
        snapshot_path.read_text(encoding="utf-8")
    )
    initial_index = deepcopy(initial_snapshot["record_indexes"][0])

    successor = tmp_path / "runs" / "run-01-anchor"
    _finalize_recovery_anchor(successor, prefix)

    raw_path = next(prefix_run.rglob("*.jsonl"))
    original_metadata = raw_path.stat()
    replacement = raw_path.with_name(f".{raw_path.name}.replacement")
    replacement.write_bytes(raw_path.read_bytes())
    replacement.chmod(stat.S_IMODE(original_metadata.st_mode))
    os.utime(
        replacement,
        ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
    )
    if os.name == "nt":
        raw_path.chmod(0o666)
    replacement.replace(raw_path)
    assert raw_path.stat().st_ino != original_metadata.st_ino

    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    original_record_id = canonical_record_id_from_json

    def reject_prefix_row_revalidation(record: object) -> str:
        if (
            isinstance(record, dict)
            and record.get("source") == "clob_market_ws"
        ):
            raise AssertionError(
                "identity drift may rehash but must not reparse prefix rows"
            )
        return original_record_id(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        reject_prefix_row_revalidation,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    refreshed = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert actual.canonical_bytes() == expected.canonical_bytes()
    assert len(refreshed["record_indexes"]) == 2
    assert refreshed["record_indexes"][0] == initial_index


def test_cache_hit_never_opens_or_hashes_prefix_raw_or_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-no-prefix-reopen"
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
    )
    _write_close_checkpoint(run, manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-no-prefix-reopen-anchor"
    _finalize_recovery_anchor(successor, prefix)
    expected = recover_dynamic_short_crypto_runs(
        (run, successor),
        settlement_timeout_ms=3_600_000,
    )
    original_digest = recovery_module._stable_digest
    original_read = recovery_module._stable_read

    def reject_prefix_digest(
        path: Path,
        *,
        code: str,
        count_lines: bool,
    ) -> tuple[str, int, int | None]:
        if run.resolve() in path.resolve().parents and (
            path.suffix == ".jsonl"
            or path.name.endswith(".manifest.json")
        ):
            raise AssertionError(
                "cache hit must not open or hash prefix raw/manifests"
            )
        return original_digest(
            path,
            code=code,
            count_lines=count_lines,
        )

    def reject_prefix_read(path: Path, *, code: str) -> bytes:
        if run.resolve() in path.resolve().parents and (
            path.suffix == ".jsonl"
            or path.name.endswith(".manifest.json")
        ):
            raise AssertionError(
                "cache hit must not reopen prefix raw/manifests"
            )
        return original_read(path, code=code)

    monkeypatch.setattr(
        recovery_module,
        "_stable_digest",
        reject_prefix_digest,
    )
    monkeypatch.setattr(
        recovery_module,
        "_stable_read",
        reject_prefix_read,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_suffix_hit_hashes_only_suffix_and_never_reopens_prefix_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix-no-reopen"
    prefix_manifest, root_ids = _finalize_events(
        prefix_run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
    )
    _write_close_checkpoint(prefix_run, prefix_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    anchor_run = (
        tmp_path / "runs" / "run-prefix-no-reopen-z-anchor"
    )
    _finalize_recovery_anchor(anchor_run, prefix)
    suffix_run = tmp_path / "runs" / "run-suffix-hashed"
    suffix_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "lifecycle_remediation_cohort_committed",
                _remediation_commitment_payload(root_ids[0]),
                received_at=REMEDIATION_RECEIVED_AT,
                kind="command",
            )
        ],
        received_at=REMEDIATION_RECEIVED_AT,
    )
    _write_close_checkpoint(suffix_run, suffix_manifest)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, anchor_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    original_digest = recovery_module._stable_digest
    original_read = recovery_module._stable_read

    def reject_prefix_digest(
        path: Path,
        *,
        code: str,
        count_lines: bool,
    ) -> tuple[str, int, int | None]:
        if prefix_run.resolve() in path.resolve().parents and (
            path.suffix == ".jsonl"
            or path.name.endswith(".manifest.json")
        ):
            raise AssertionError(
                "suffix hit must not open or hash prefix raw/manifests"
            )
        return original_digest(
            path,
            code=code,
            count_lines=count_lines,
        )

    def reject_prefix_read(path: Path, *, code: str) -> bytes:
        if prefix_run.resolve() in path.resolve().parents and (
            path.suffix == ".jsonl"
            or path.name.endswith(".manifest.json")
        ):
            raise AssertionError(
                "suffix hit must not reopen prefix raw/manifests"
            )
        return original_read(path, code=code)

    monkeypatch.setattr(
        recovery_module,
        "_stable_digest",
        reject_prefix_digest,
    )
    monkeypatch.setattr(
        recovery_module,
        "_stable_read",
        reject_prefix_read,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, anchor_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_snapshot_without_direct_successor_anchor_forces_full_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-orphan-snapshot"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    first = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    repeated = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert repeated.canonical_bytes() == first.canonical_bytes()
    assert repeated.anchor_payload == first.anchor_payload


def test_snapshot_never_skips_direct_successor_for_later_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    direct_successor = tmp_path / "runs" / "run-01-no-anchor"
    _finalize_events(
        direct_successor,
        [
            _service_event(
                "recovery_cache_probe",
                {"probe": "direct-successor-without-anchor"},
                kind="command",
            )
        ],
    )
    late_anchor = tmp_path / "runs" / "run-02-late-anchor"
    _finalize_recovery_anchor(late_anchor, prefix)
    roots = (prefix_run, direct_successor, late_anchor)
    expected = recover_dynamic_short_crypto_runs(
        roots,
        settlement_timeout_ms=3_600_000,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        roots,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


@pytest.mark.parametrize("branch", (False, True))
def test_direct_successor_rejects_duplicate_or_branched_anchors(
    tmp_path: Path,
    branch: bool,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-anchor-branch"
    _finalize_recovery_anchor(
        successor,
        prefix,
        batch_id="anchor-a",
    )
    second_anchor = deepcopy(prefix.anchor_payload)
    if branch:
        second_anchor["snapshot_id"] = "f" * 64
        anchor_body = {
            key: value
            for key, value in second_anchor.items()
            if key != "anchor_id"
        }
        second_anchor["anchor_id"] = hashlib.sha256(
            canonical_json_bytes(anchor_body)
        ).hexdigest()
    _finalize_recovery_anchor(
        successor,
        prefix,
        batch_id="anchor-b",
        monotonic_ns=2,
        anchor_payload=second_anchor,
    )

    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, successor),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert caught.value.code == "recovery_anchor_invalid"


def test_direct_successor_rejects_anchored_and_legacy_restart_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-mixed-decisions"
    _finalize_recovery_anchor(
        successor,
        prefix,
        batch_id="anchored-decision",
    )
    _finalize_events(
        successor,
        [
            _service_event(
                "restart_recovery_decision",
                {
                    **prefix.result.recovery_decision,
                    "run_classifications": list(
                        prefix.result.run_classifications
                    ),
                    "gaps": list(prefix.result.gaps),
                    "exclusions": list(prefix.result.exclusions),
                    "worker_actions": list(
                        prefix.result.worker_actions
                    ),
                    "scheduler_now_ms": 1_784_990_951_000,
                },
                monotonic_ns=2,
                kind="command",
            )
        ],
        batch_id="legacy-decision",
    )
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if (
            not kwargs.get("_checkpoint_only_reduction", False)
            and kwargs.get("_prefix_checkpoint") is None
        ):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_anchor_discovery_reads_only_successor_control_service_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-successor"
    _finalize_recovery_anchor(successor, prefix)
    _finalize_events(
        successor,
        [
            _market_event(
                connection_id="connection-b",
                monotonic_ns=2,
            )
        ],
        source="clob_market_ws",
        batch_id="group-market",
        store_relative="groups/group-b",
    )
    _finalize_events(
        successor,
        [
            {
                "schema_version": "edge-lab-public-http.v1",
                "source": "gamma_http",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {"market_id": "market-successor"},
            }
        ],
        source="gamma_http",
        batch_id="main-public",
        store_relative="main",
    )
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    original_anchor_validator = (
        recovery_module._validate_direct_successor_anchor
    )
    original_pair_validator = recovery_module._validate_finalized_pair
    original_read = recovery_module._stable_read
    discovery_active = False
    suffix_pair_counts: dict[str, int] = {}
    allowed_anchor_root = (
        successor.resolve()
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
    )

    def observe_anchor_validation(*args: Any, **kwargs: Any) -> None:
        nonlocal discovery_active
        discovery_active = True
        try:
            original_anchor_validator(*args, **kwargs)
        finally:
            discovery_active = False

    def reject_non_anchor_discovery_read(
        path: Path,
        *,
        code: str,
    ) -> bytes:
        resolved = path.resolve()
        if (
            discovery_active
            and successor.resolve() in resolved.parents
            and allowed_anchor_root not in resolved.parents
        ):
            raise AssertionError(
                "anchor discovery must not open successor group/main pairs"
            )
        return original_read(path, code=code)

    def observe_suffix_pair(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        if (
            not discovery_active
            and run_root == successor.resolve()
        ):
            relative = raw_path.relative_to(run_root).as_posix()
            suffix_pair_counts[relative] = (
                suffix_pair_counts.get(relative, 0) + 1
            )
        return original_pair_validator(run_root, run_id, raw_path)

    monkeypatch.setattr(
        recovery_module,
        "_validate_direct_successor_anchor",
        observe_anchor_validation,
    )
    monkeypatch.setattr(
        recovery_module,
        "_stable_read",
        reject_non_anchor_discovery_read,
    )
    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        observe_suffix_pair,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    assert {
        path: count
        for path, count in suffix_pair_counts.items()
        if not path.startswith(
            "control/raw/dynamic_short_crypto_service/"
        )
    } == {
        (
            "groups/group-b/raw/clob_market_ws/"
            "group-market.jsonl"
        ): 1,
        "main/raw/gamma_http/main-public.jsonl": 1,
    }


@pytest.mark.parametrize(
    "symlink_component",
    ("control", "raw", "dynamic_short_crypto_service"),
)
def test_anchor_discovery_rejects_symlinked_parent_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_component: str,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    outside_run = tmp_path / "outside" / "run-external-anchor"
    _finalize_recovery_anchor(outside_run, prefix)
    (
        outside_run
        / "control"
        / "checkpoints"
        / "dynamic_short_crypto_service.json"
    ).unlink()
    successor = tmp_path / "runs" / "run-01-successor"
    successor.mkdir(parents=True)
    if symlink_component == "control":
        (successor / "control").symlink_to(
            outside_run / "control",
            target_is_directory=True,
        )
    elif symlink_component == "raw":
        (successor / "control").mkdir()
        (successor / "control" / "raw").symlink_to(
            outside_run / "control" / "raw",
            target_is_directory=True,
        )
    else:
        (successor / "control" / "raw").mkdir(parents=True)
        (
            successor
            / "control"
            / "raw"
            / "dynamic_short_crypto_service"
        ).symlink_to(
            outside_run
            / "control"
            / "raw"
            / "dynamic_short_crypto_service",
            target_is_directory=True,
        )
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if (
            not kwargs.get("_checkpoint_only_reduction", False)
            and kwargs.get("_prefix_checkpoint") is None
        ):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_anchor_discovery_binds_successor_root_across_suffix_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-successor"
    (
        successor
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
    ).mkdir(parents=True)
    outside_successor = (
        tmp_path / "outside" / "run-01-successor"
    )
    _finalize_recovery_anchor(outside_successor, prefix)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    held_local = successor.with_name(
        f"{successor.name}.held-local"
    )
    held_external = outside_successor.with_name(
        f"{outside_successor.name}.held-external"
    )
    swapped = False

    def swap_to_external() -> None:
        nonlocal swapped
        if swapped:
            return
        successor.rename(held_local)
        outside_successor.rename(successor)
        swapped = True

    def restore_local() -> None:
        nonlocal swapped
        if not swapped:
            return
        successor.rename(held_external)
        held_local.rename(successor)
        held_external.rename(outside_successor)
        swapped = False

    original_control_validator = (
        recovery_module._validated_anchor_control_root
    )
    original_anchor_validator = (
        recovery_module._validate_direct_successor_anchor
    )

    def swap_before_anchor_root(
        run_root: Path,
        *args: Any,
        **kwargs: Any,
    ) -> object:
        swap_to_external()
        return original_control_validator(
            run_root,
            *args,
            **kwargs,
        )

    def restore_after_anchor_validation(
        *args: Any,
        **kwargs: Any,
    ) -> object:
        try:
            return original_anchor_validator(*args, **kwargs)
        finally:
            restore_local()

    monkeypatch.setattr(
        recovery_module,
        "_validated_anchor_control_root",
        swap_before_anchor_root,
    )
    monkeypatch.setattr(
        recovery_module,
        "_validate_direct_successor_anchor",
        restore_after_anchor_validation,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if (
            not kwargs.get("_checkpoint_only_reduction", False)
            and kwargs.get("_prefix_checkpoint") is None
        ):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    try:
        actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, successor),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )
    finally:
        restore_local()

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


@pytest.mark.parametrize("replay_mode", ("suffix", "full"))
def test_cached_recovery_binds_all_replay_reads_to_held_run_root_against_swap_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replay_mode: str,
) -> None:
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    if replay_mode == "suffix":
        prefix_run = tmp_path / "runs" / "run-00-prefix"
        _finalize_events(
            prefix_run,
            [_market_event(connection_id="prefix", monotonic_ns=1)],
            source="clob_market_ws",
            store_relative="groups/prefix",
        )
        prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
            prefix_run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )
        red_root = tmp_path / "runs" / "run-01-successor"
        _finalize_recovery_anchor(red_root, prefix)
        green_root = tmp_path / "outside" / "run-01-successor"
        _finalize_recovery_anchor(green_root, prefix)
        _finalize_events(
            green_root,
            [_market_event(connection_id="green-only", monotonic_ns=2)],
            source="clob_market_ws",
            batch_id="green-only",
            store_relative="groups/green",
        )
        roots = (prefix_run, red_root)
    else:
        red_root = tmp_path / "runs" / "run-only"
        _finalize_events(
            red_root,
            [_market_event(connection_id="red", monotonic_ns=1)],
            source="clob_market_ws",
            store_relative="groups/red",
        )
        green_root = tmp_path / "outside" / "run-only"
        _finalize_events(
            green_root,
            [
                _market_event(connection_id="green-a", monotonic_ns=1),
                _market_event(connection_id="green-b", monotonic_ns=2),
            ],
            source="clob_market_ws",
            store_relative="groups/green",
        )
        roots = (red_root,)

    expected = recover_dynamic_short_crypto_runs(
        roots,
        settlement_timeout_ms=3_600_000,
    )
    red_path = red_root.resolve()
    held_red = red_root.with_name(f"{red_root.name}.held-red")
    held_green = green_root.with_name(f"{green_root.name}.held-green")
    original_checkpoint = recovery_module._checkpoint_artifact
    original_inventory = recovery_module._run_inventory_proof
    swapped = False
    attack_fired = False

    def swap_to_green() -> None:
        nonlocal swapped, attack_fired
        red_root.rename(held_red)
        green_root.rename(red_root)
        swapped = True
        attack_fired = True

    def restore_red() -> None:
        nonlocal swapped
        if not swapped:
            return
        red_root.rename(held_green)
        held_red.rename(red_root)
        held_green.rename(green_root)
        swapped = False

    def checkpoint_after_root_proof(run_root: Path) -> object:
        if run_root == red_path and not swapped:
            swap_to_green()
        return original_checkpoint(run_root)

    def inventory_then_restore(*args: Any, **kwargs: Any) -> object:
        try:
            return original_inventory(*args, **kwargs)
        finally:
            restore_red()

    monkeypatch.setattr(
        recovery_module,
        "_checkpoint_artifact",
        checkpoint_after_root_proof,
    )
    monkeypatch.setattr(
        recovery_module,
        "_run_inventory_proof",
        inventory_then_restore,
    )
    try:
        actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
            roots,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )
    finally:
        restore_red()

    assert attack_fired
    assert actual.result.canonical_bytes() == expected.canonical_bytes()


def test_cached_recovery_rejects_replay_subtree_swap_after_bounded_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    red_root = tmp_path / "runs" / "run-subtree"
    _finalize_events(
        red_root,
        [_market_event(connection_id="red", monotonic_ns=1)],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    green_root = tmp_path / "outside" / "run-subtree"
    _finalize_events(
        green_root,
        [
            _market_event(connection_id="green-a", monotonic_ns=1),
            _market_event(connection_id="green-b", monotonic_ns=2),
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    red_component = red_root / "groups" / "group-a"
    green_component = green_root / "groups" / "group-a"
    held_red = red_component.with_name("group-a.held-red")
    held_green = green_component.with_name("group-a.held-green")
    original_checkpoint = recovery_module._checkpoint_artifact
    swapped = False
    attack_fired = False

    def swap_to_green() -> None:
        nonlocal swapped, attack_fired
        red_component.rename(held_red)
        green_component.rename(red_component)
        swapped = True
        attack_fired = True

    def restore_red() -> None:
        nonlocal swapped
        if not swapped:
            return
        red_component.rename(held_green)
        held_red.rename(red_component)
        held_green.rename(green_component)
        swapped = False

    def checkpoint_after_descriptor_walk(run_root: Path) -> object:
        if not swapped:
            swap_to_green()
        return original_checkpoint(run_root)

    monkeypatch.setattr(
        recovery_module,
        "_checkpoint_artifact",
        checkpoint_after_descriptor_walk,
    )
    try:
        with pytest.raises(RecoveryInputError) as caught:
            recovery_module.recover_dynamic_short_crypto_runs_cached(
                red_root,
                settlement_timeout_ms=3_600_000,
                snapshot_path=(
                    tmp_path / "service" / "recovery-snapshot.json"
                ),
            )
    finally:
        restore_red()

    assert attack_fired
    assert caught.value.code == "recovery_run_root_changed"


def test_cached_recovery_rejects_finalized_pair_added_after_held_view_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "runs" / "run-late-member"
    _finalize_events(
        run_root,
        [_market_event(connection_id="initial", monotonic_ns=1)],
        source="clob_market_ws",
        store_relative="groups/initial",
    )
    original_checkpoint = recovery_module._checkpoint_artifact
    attack_fired = False

    def add_pair_after_descriptor_walk(run_root_arg: Path) -> object:
        nonlocal attack_fired
        if run_root_arg == run_root.resolve() and not attack_fired:
            attack_fired = True
            _finalize_events(
                run_root,
                [_market_event(connection_id="late", monotonic_ns=2)],
                source="clob_market_ws",
                batch_id="late",
                store_relative="groups/late",
            )
        return original_checkpoint(run_root_arg)

    monkeypatch.setattr(
        recovery_module,
        "_checkpoint_artifact",
        add_pair_after_descriptor_walk,
    )
    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run_root,
            settlement_timeout_ms=3_600_000,
            snapshot_path=(
                tmp_path / "service" / "recovery-snapshot.json"
            ),
        )

    assert attack_fired
    assert caught.value.code == "recovery_run_root_changed"


def test_cached_recovery_rejects_same_name_nonartifact_replaced_by_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "runs" / "run-same-name-member"
    _finalize_events(
        run_root,
        [_market_event(connection_id="initial", monotonic_ns=1)],
        source="clob_market_ws",
        store_relative="groups/initial",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    replaced = run_root / "groups" / "late"
    replaced.symlink_to(outside, target_is_directory=True)
    original_checkpoint = recovery_module._checkpoint_artifact
    attack_fired = False

    def replace_after_descriptor_walk(run_root_arg: Path) -> object:
        nonlocal attack_fired
        if run_root_arg == run_root.resolve() and not attack_fired:
            attack_fired = True
            replaced.unlink()
            _finalize_events(
                run_root,
                [_market_event(connection_id="late", monotonic_ns=2)],
                source="clob_market_ws",
                batch_id="late",
                store_relative="groups/late",
            )
        return original_checkpoint(run_root_arg)

    monkeypatch.setattr(
        recovery_module,
        "_checkpoint_artifact",
        replace_after_descriptor_walk,
    )
    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run_root,
            settlement_timeout_ms=3_600_000,
            snapshot_path=(
                tmp_path / "service" / "recovery-snapshot.json"
            ),
        )

    assert attack_fired
    assert caught.value.code == "recovery_run_root_changed"


def test_cached_recovery_rejects_finalized_pair_removed_after_held_view_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "runs" / "run-removed-member"
    manifest, _ = _finalize_events(
        run_root,
        [_market_event(connection_id="removed", monotonic_ns=1)],
        source="clob_market_ws",
        store_relative="groups/removed",
    )
    raw_path = (
        run_root / "groups" / "removed" / str(manifest["raw_path"])
    )
    manifest_path = raw_path.with_suffix(".manifest.json")
    original_checkpoint = recovery_module._checkpoint_artifact
    attack_fired = False

    def remove_pair_after_descriptor_walk(run_root_arg: Path) -> object:
        nonlocal attack_fired
        if run_root_arg == run_root.resolve() and not attack_fired:
            attack_fired = True
            raw_path.unlink()
            manifest_path.unlink()
        return original_checkpoint(run_root_arg)

    monkeypatch.setattr(
        recovery_module,
        "_checkpoint_artifact",
        remove_pair_after_descriptor_walk,
    )
    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run_root,
            settlement_timeout_ms=3_600_000,
            snapshot_path=(
                tmp_path / "service" / "recovery-snapshot.json"
            ),
        )

    assert attack_fired
    assert caught.value.code in {
        "finalized_raw_unreadable",
        "recovery_run_root_changed",
    }


@pytest.mark.parametrize(
    "mutation_phase",
    ("before_input_commitment", "after_snapshot_write"),
)
def test_cached_recovery_rechecks_replayed_membership_through_snapshot_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation_phase: str,
) -> None:
    run_root = tmp_path / "runs" / "run-post-replay-member"
    _finalize_events(
        run_root,
        [_market_event(connection_id="initial", monotonic_ns=1)],
        source="clob_market_ws",
        store_relative="groups/initial",
    )
    original_commitment = recovery_module._recovery_input_commitment
    original_snapshot_write = recovery_module._write_recovery_snapshot
    attack_fired = False

    def add_finalized_pair() -> None:
        nonlocal attack_fired
        if attack_fired:
            return
        attack_fired = True
        _finalize_events(
            run_root,
            [_market_event(connection_id="late", monotonic_ns=2)],
            source="clob_market_ws",
            batch_id="late",
            store_relative="groups/late",
        )

    def mutate_before_commitment(
        *args: Any,
        **kwargs: Any,
    ) -> object:
        add_finalized_pair()
        return original_commitment(*args, **kwargs)

    def mutate_after_snapshot_write(
        *args: Any,
        **kwargs: Any,
    ) -> object:
        result = original_snapshot_write(*args, **kwargs)
        add_finalized_pair()
        return result

    monkeypatch.setattr(
        recovery_module,
        (
            "_recovery_input_commitment"
            if mutation_phase == "before_input_commitment"
            else "_write_recovery_snapshot"
        ),
        (
            mutate_before_commitment
            if mutation_phase == "before_input_commitment"
            else mutate_after_snapshot_write
        ),
    )
    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run_root,
            settlement_timeout_ms=3_600_000,
            snapshot_path=(
                tmp_path / "service" / "recovery-snapshot.json"
            ),
        )

    assert attack_fired
    assert caught.value.code in {
        "recovery_snapshot_input_drift",
        "recovery_run_root_changed",
    }
    expected = recover_dynamic_short_crypto_runs(
        run_root,
        settlement_timeout_ms=3_600_000,
    )
    recovered = (
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run_root,
            settlement_timeout_ms=3_600_000,
            snapshot_path=(
                tmp_path / "service" / "recovery-snapshot.json"
            ),
        )
    )
    assert recovered.canonical_bytes() == expected.canonical_bytes()


@pytest.mark.parametrize(
    "swapped_component",
    ("control", "raw", "dynamic_short_crypto_service"),
)
def test_anchor_discovery_holds_directory_chain_against_swap_back_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped_component: str,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    outside_run = tmp_path / "outside" / "run-external-anchor"
    _finalize_recovery_anchor(outside_run, prefix)
    (
        outside_run
        / "control"
        / "checkpoints"
        / "dynamic_short_crypto_service.json"
    ).unlink()
    successor = tmp_path / "runs" / "run-01-successor"
    local_service_root = (
        successor
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
    )
    local_service_root.mkdir(parents=True)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    component_paths = {
        "control": successor / "control",
        "raw": successor / "control" / "raw",
        "dynamic_short_crypto_service": local_service_root,
    }
    outside_paths = {
        "control": outside_run / "control",
        "raw": outside_run / "control" / "raw",
        "dynamic_short_crypto_service": (
            outside_run
            / "control"
            / "raw"
            / "dynamic_short_crypto_service"
        ),
    }
    component_path = component_paths[swapped_component]
    backup_path = component_path.with_name(
        f"{component_path.name}.held-local"
    )
    swapped = False

    def swap_to_external() -> None:
        nonlocal swapped
        if swapped:
            return
        component_path.rename(backup_path)
        component_path.symlink_to(
            outside_paths[swapped_component],
            target_is_directory=True,
        )
        swapped = True

    def restore_local() -> None:
        nonlocal swapped
        if not swapped:
            return
        component_path.unlink()
        backup_path.rename(component_path)
        swapped = False

    original_control_validator = (
        recovery_module._validated_anchor_control_root
    )
    original_control_revalidator = (
        recovery_module._revalidate_anchor_control_root
    )
    original_pair_validator = recovery_module._validate_finalized_pair
    external_pair_reads = 0

    def swap_after_directory_proof(
        run_root: Path,
        *args: Any,
        **kwargs: Any,
    ) -> object:
        proof = original_control_validator(
            run_root,
            *args,
            **kwargs,
        )
        swap_to_external()
        return proof

    def restore_after_path_pair_read(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        nonlocal external_pair_reads
        try:
            if outside_run.resolve() in raw_path.resolve().parents:
                external_pair_reads += 1
            return original_pair_validator(run_root, run_id, raw_path)
        finally:
            restore_local()

    def restore_before_directory_revalidation(proof: object) -> None:
        restore_local()
        original_control_revalidator(proof)

    monkeypatch.setattr(
        recovery_module,
        "_validated_anchor_control_root",
        swap_after_directory_proof,
    )
    monkeypatch.setattr(
        recovery_module,
        "_revalidate_anchor_control_root",
        restore_before_directory_revalidation,
    )
    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        restore_after_path_pair_read,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if (
            not kwargs.get("_checkpoint_only_reduction", False)
            and kwargs.get("_prefix_checkpoint") is None
        ):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    try:
        actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, successor),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )
    finally:
        restore_local()

    assert external_pair_reads == 0
    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


@pytest.mark.parametrize("entry_kind", ("raw", "manifest"))
def test_anchor_discovery_binds_open_files_to_held_directory_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-successor"
    local_manifest, _ = _finalize_events(
        successor,
        [
            _service_event(
                "recovery_cache_probe",
                {"probe": "local-non-anchor"},
                kind="command",
            )
        ],
        batch_id="recovery-anchor",
    )
    local_raw = successor / "control" / str(local_manifest["raw_path"])
    local_manifest_path = local_raw.with_suffix(".manifest.json")
    local_bytes = (local_raw.read_bytes(), local_manifest_path.read_bytes())
    outside_run = tmp_path / "outside" / "run-external-anchor"
    outside_manifest, _ = _finalize_recovery_anchor(outside_run, prefix)
    outside_raw = outside_run / "control" / str(outside_manifest["raw_path"])
    outside_manifest_path = outside_raw.with_suffix(".manifest.json")
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    if entry_kind == "raw":
        local_by_name = {local_raw.name: local_raw}
        outside_by_name = {outside_raw.name: outside_raw}
    else:
        local_by_name = {
            local_manifest_path.name: local_manifest_path,
        }
        outside_by_name = {
            outside_manifest_path.name: outside_manifest_path,
        }
    original_control_validator = (
        recovery_module._validated_anchor_control_root
    )
    original_open = recovery_module.os.open
    armed = False
    swapped_names: list[str] = []

    def arm_after_directory_proof(
        run_root: Path,
        *args: Any,
        **kwargs: Any,
    ) -> object:
        nonlocal armed
        proof = original_control_validator(
            run_root,
            *args,
            **kwargs,
        )
        armed = True
        return proof

    def swap_entry_while_opening(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        name = str(path)
        local_path = local_by_name.get(name)
        outside_path = outside_by_name.get(name)
        if (
            armed
            and kwargs.get("dir_fd") is not None
            and local_path is not None
            and outside_path is not None
            and name not in swapped_names
        ):
            backup = local_path.with_name(f"{name}.held-local")
            local_path.rename(backup)
            os.link(outside_path, local_path)
            try:
                descriptor = original_open(
                    path,
                    flags,
                    *args,
                    **kwargs,
                )
            finally:
                local_path.unlink()
                backup.rename(local_path)
            swapped_names.append(name)
            return descriptor
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_validated_anchor_control_root",
        arm_after_directory_proof,
    )
    monkeypatch.setattr(recovery_module.os, "open", swap_entry_while_opening)
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if (
            not kwargs.get("_checkpoint_only_reduction", False)
            and kwargs.get("_prefix_checkpoint") is None
        ):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert set(swapped_names) == set(local_by_name)
    assert local_bytes == (
        local_raw.read_bytes(),
        local_manifest_path.read_bytes(),
    )
    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="requires FIFO support",
)
def test_anchor_discovery_fifo_cannot_block_cached_recovery(
    tmp_path: Path,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-successor"
    _finalize_recovery_anchor(successor, prefix)
    fifo_manifest, _ = _finalize_events(
        successor,
        [
            _service_event(
                "recovery_cache_probe",
                {"probe": "must-not-block-on-fifo"},
                kind="command",
            )
        ],
        batch_id="000-blocked",
    )
    fifo_raw = successor / "control" / str(fifo_manifest["raw_path"])
    fifo_raw.unlink()
    os.mkfifo(fifo_raw, mode=0o444)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
    )
    expected_digest = hashlib.sha256(
        expected.canonical_bytes()
    ).hexdigest()
    script = "\n".join(
        (
            "import hashlib",
            "import sys",
            "from pathlib import Path",
            (
                "from src.edge_lab.dynamic_short_crypto_recovery "
                "import recover_dynamic_short_crypto_runs_cached"
            ),
            "runs = tuple(Path(value) for value in sys.argv[1:3])",
            "result = recover_dynamic_short_crypto_runs_cached(",
            "    runs,",
            "    settlement_timeout_ms=3_600_000,",
            "    snapshot_path=Path(sys.argv[3]),",
            ")",
            (
                "sys.stdout.write("
                "hashlib.sha256(result.canonical_bytes()).hexdigest())"
            ),
        )
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(prefix_run),
                str(successor),
                str(snapshot_path),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("cached recovery blocked while opening finalized FIFO")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == expected_digest


@pytest.mark.parametrize("duplicate_anchor", (False, True))
def test_anchor_discovery_closes_all_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_anchor: bool,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-successor"
    _finalize_recovery_anchor(successor, prefix)
    if duplicate_anchor:
        _finalize_recovery_anchor(
            successor,
            prefix,
            batch_id="duplicate-anchor",
            monotonic_ns=2,
        )
    original_open = recovery_module.os.open
    original_close = recovery_module.os.close
    opened: list[int] = []
    closed: list[int] = []
    tracking = False

    def tracked_open(*args: Any, **kwargs: Any) -> int:
        descriptor = original_open(*args, **kwargs)
        if tracking:
            opened.append(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        if tracking and descriptor in opened:
            closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(recovery_module.os, "open", tracked_open)
    monkeypatch.setattr(recovery_module.os, "close", tracked_close)
    tracking = True
    try:
        if duplicate_anchor:
            with pytest.raises(RecoveryInputError) as caught:
                recovery_module._validate_direct_successor_anchor(
                    successor=(successor.name, successor.resolve()),
                    expected_anchor=prefix.anchor_payload,
                )
            assert caught.value.code == "recovery_anchor_invalid"
        else:
            recovery_module._validate_direct_successor_anchor(
                successor=(successor.name, successor.resolve()),
                expected_anchor=prefix.anchor_payload,
            )
    finally:
        tracking = False

    assert opened
    assert sorted(opened) == sorted(closed)
    for descriptor in set(opened):
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("fail_after_hold", (False, True))
def test_cached_recovery_bounds_and_closes_run_root_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_after_hold: bool,
) -> None:
    run_root = tmp_path / "runs" / "run-held-root"
    _finalize_events(
        run_root,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    original_open = recovery_module.os.open
    original_close = recovery_module.os.close
    opened: list[int] = []
    closed: list[int] = []
    active: list[int] = []
    peak_active = 0

    def tracked_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal peak_active
        descriptor = original_open(
            path,
            flags,
            *args,
            **kwargs,
        )
        if (
            kwargs.get("dir_fd") is None
            and Path(path) == run_root.resolve()
            and flags & getattr(os, "O_DIRECTORY", 0)
        ):
            opened.append(descriptor)
            active.append(descriptor)
            peak_active = max(peak_active, len(active))
        return descriptor

    def tracked_close(descriptor: int) -> None:
        if descriptor in active:
            active.remove(descriptor)
            closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(recovery_module.os, "open", tracked_open)
    monkeypatch.setattr(recovery_module.os, "close", tracked_close)
    if fail_after_hold:
        def fail_held_recovery(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("forced failure after run-root hold")

        monkeypatch.setattr(
            recovery_module,
            "_recover_dynamic_short_crypto_runs_cached_held",
            fail_held_recovery,
        )
        with pytest.raises(
            RuntimeError,
            match="forced failure after run-root hold",
        ):
            recovery_module.recover_dynamic_short_crypto_runs_cached(
                run_root,
                settlement_timeout_ms=3_600_000,
                snapshot_path=snapshot_path,
            )
    else:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run_root,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert opened
    assert len(closed) == len(opened)
    assert active == []
    assert peak_active <= 2
    for descriptor in set(opened):
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_recovery_bounds_held_directory_descriptors_across_many_roots(
    tmp_path: Path,
) -> None:
    import resource

    roots: list[Path] = []
    for run_index in range(2):
        run_root = tmp_path / "runs" / f"run-{run_index:02d}"
        for directory_index in range(20):
            (
                run_root
                / "irrelevant"
                / f"directory-{directory_index:02d}"
            ).mkdir(parents=True)
        roots.append(run_root)

    original_soft, original_hard = resource.getrlimit(
        resource.RLIMIT_NOFILE
    )
    baseline = len(os.listdir("/dev/fd"))
    constrained_soft = baseline + 40
    if (
        original_soft != resource.RLIM_INFINITY
        and original_soft <= constrained_soft
    ):
        pytest.skip("process FD limit is already too low for this test")
    if (
        original_hard != resource.RLIM_INFINITY
        and original_hard < constrained_soft
    ):
        pytest.skip("hard FD limit is too low for this test")

    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (constrained_soft, original_hard),
    )
    try:
        result = recover_dynamic_short_crypto_runs(
            tuple(roots),
            settlement_timeout_ms=3_600_000,
        )
    finally:
        resource.setrlimit(
            resource.RLIMIT_NOFILE,
            (original_soft, original_hard),
        )

    assert result.recovered_from_run_ids == tuple(
        run_root.name for run_root in roots
    )


def test_recovery_bounds_run_root_proofs_across_many_roots(
    tmp_path: Path,
) -> None:
    import resource

    roots: list[Path] = []
    for run_index in range(80):
        run_root = tmp_path / "runs" / f"run-{run_index:02d}"
        run_root.mkdir(parents=True)
        roots.append(run_root)

    original_soft, original_hard = resource.getrlimit(
        resource.RLIMIT_NOFILE
    )
    baseline = len(os.listdir("/dev/fd"))
    constrained_soft = baseline + 40
    if (
        original_soft != resource.RLIM_INFINITY
        and original_soft <= constrained_soft
    ):
        pytest.skip("process FD limit is already too low for this test")
    if (
        original_hard != resource.RLIM_INFINITY
        and original_hard < constrained_soft
    ):
        pytest.skip("hard FD limit is too low for this test")

    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (constrained_soft, original_hard),
    )
    try:
        result = recover_dynamic_short_crypto_runs(
            tuple(roots),
            settlement_timeout_ms=3_600_000,
        )
    finally:
        resource.setrlimit(
            resource.RLIMIT_NOFILE,
            (original_soft, original_hard),
        )

    assert result.recovered_from_run_ids == tuple(
        run_root.name for run_root in roots
    )


def test_recovery_bounds_held_directory_descriptors_within_single_run(
    tmp_path: Path,
) -> None:
    import resource

    run_root = tmp_path / "runs" / "run-many-directories"
    for directory_index in range(80):
        (
            run_root
            / "irrelevant"
            / f"directory-{directory_index:02d}"
        ).mkdir(parents=True)

    original_soft, original_hard = resource.getrlimit(
        resource.RLIMIT_NOFILE
    )
    baseline = len(os.listdir("/dev/fd"))
    constrained_soft = baseline + 40
    if (
        original_soft != resource.RLIM_INFINITY
        and original_soft <= constrained_soft
    ):
        pytest.skip("process FD limit is already too low for this test")
    if (
        original_hard != resource.RLIM_INFINITY
        and original_hard < constrained_soft
    ):
        pytest.skip("hard FD limit is too low for this test")

    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (constrained_soft, original_hard),
    )
    try:
        result = recover_dynamic_short_crypto_runs(
            run_root,
            settlement_timeout_ms=3_600_000,
        )
    finally:
        resource.setrlimit(
            resource.RLIMIT_NOFILE,
            (original_soft, original_hard),
        )

    assert result.recovered_from_run_ids == (run_root.name,)


def test_run_roots_closes_held_descriptors_when_later_root_is_invalid(
    tmp_path: Path,
) -> None:
    valid_root = tmp_path / "runs" / "run-valid-first"
    valid_root.mkdir(parents=True)
    missing_root = tmp_path / "runs" / "run-missing-second"
    held: list[object] = []
    try:
        with pytest.raises(ValueError, match="must exist"):
            recovery_module._run_roots(
                (valid_root, missing_root),
                _proof_sink=held,
            )
        assert held == []
    finally:
        for proof in held:
            proof.close()


@pytest.mark.parametrize(
    ("attack", "expected_code"),
    (
        ("raw", "finalized_checksum_mismatch"),
        ("manifest", "finalized_manifest_invalid"),
        ("path", "recovery_anchor_invalid"),
        ("record_id", "canonical_record_id_mismatch"),
    ),
)
def test_recovery_anchor_tamper_fails_closed(
    tmp_path: Path,
    attack: str,
    expected_code: str,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-anchor"
    manifest, _ = _finalize_recovery_anchor(successor, prefix)
    raw_path = successor / "control" / str(manifest["raw_path"])
    manifest_path = raw_path.with_suffix(".manifest.json")
    if attack == "raw":
        raw_path.chmod(0o644)
        raw_path.write_bytes(raw_path.read_bytes() + b"{}\n")
        raw_path.chmod(0o444)
    elif attack == "manifest":
        document = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        document["source"] = "attacker_source"
        manifest_path.chmod(0o644)
        manifest_path.write_bytes(
            canonical_json_bytes(document) + b"\n"
        )
        manifest_path.chmod(0o444)
    elif attack == "path":
        relocated_raw = (
            successor
            / "groups"
            / "relocated-anchor"
            / str(manifest["raw_path"])
        )
        relocated_raw.parent.mkdir(parents=True)
        relocated_manifest = relocated_raw.with_suffix(
            ".manifest.json"
        )
        raw_path.replace(relocated_raw)
        manifest_path.replace(relocated_manifest)
    else:
        row = json.loads(raw_path.read_text(encoding="utf-8"))
        row["record_id"] = "f" * 64
        raw_path.chmod(0o644)
        raw_path.write_bytes(canonical_json_bytes(row) + b"\n")
        raw_path.chmod(0o444)
        _refresh_manifest_checksum(raw_path)

    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, successor),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert caught.value.code == expected_code


def test_record_index_v1_descriptor_is_rebuilt_as_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-00-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-01-anchor"
    _finalize_recovery_anchor(successor, prefix)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_id"
    }
    body["record_indexes"][0]["schema_version"] = (
        "edge-lab-dynamic-short-crypto-record-index.v1"
    )
    forged = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(canonical_json_bytes(forged) + b"\n")
    snapshot_path.chmod(0o444)
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    rebuilt = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert full_replay_calls == 1
    assert {
        descriptor["schema_version"]
        for descriptor in rebuilt["record_indexes"]
    } == {"edge-lab-dynamic-short-crypto-record-index.v2"}


@pytest.mark.parametrize(
    "forgery",
    (
        "bogus_recovered_state",
        "bogus_target_state",
        "bogus_tip",
        "bad_journal",
    ),
)
def test_snapshot_forgery_forces_full_deep_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    run = tmp_path / "runs" / f"run-forged-{forgery}"
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
        batch_id="cohort-root",
    )
    _write_close_checkpoint(run, manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = (
        tmp_path
        / "runs"
        / f"run-forged-{forgery}-z-recovery-anchor"
    )
    _finalize_recovery_anchor(successor, expected)
    expected_full = recover_dynamic_short_crypto_runs(
        (run, successor),
        settlement_timeout_ms=3_600_000,
    )

    def mutate_state(state: dict[str, Any]) -> None:
        if forgery == "bogus_recovered_state":
            state["attacker_controlled_field"] = True
        elif forgery == "bogus_target_state":
            state["targets"]["attacker-controlled-slug"] = {
                "state": "settled"
            }
        elif forgery == "bad_journal":
            state["lifecycle_cohort_journal"] = {
                "not": "an exact ordered journal"
            }
        else:
            journal = state["lifecycle_cohort_journal"]
            predecessor = journal[-1]["record_id"]
            journal.append(
                {
                    "event_type": (
                        "lifecycle_remediation_cohort_committed"
                    ),
                    "record_id": "f" * 64,
                    "run_id": run.name,
                    "payload": _remediation_commitment_payload(
                        predecessor
                    ),
                }
            )

    _rehash_forged_snapshot(
        snapshot_path,
        mutate_state=mutate_state,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected_full.canonical_bytes()
    repaired = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert repaired["result"]["state"] == expected_full.state


def test_cache_rejects_checkpoint_missing_index_bound_root_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-missing-root-commitment"
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
        batch_id="cohort-root",
    )
    _write_close_checkpoint(run, manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    assert len(expected.state["lifecycle_cohort_journal"]) == 1

    _rewrite_snapshot_from_forged_checkpoint(
        snapshot_path,
        run_roots=(run.resolve(),),
        mutate_checkpoint=lambda checkpoint: checkpoint[
            "replay_records"
        ].clear(),
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()
    assert len(actual.state["lifecycle_cohort_journal"]) == 1


@pytest.mark.parametrize("attack", ("missing", "extra", "rewired"))
def test_cache_rejects_coordinated_index_and_snapshot_root_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    run = tmp_path / "runs" / "run-coordinated-root-forgery"
    manifest, record_ids = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            ),
            _service_event(
                "recovery_cache_probe",
                {"probe": "state-neutral"},
                monotonic_ns=2,
                kind="command",
            ),
        ],
        batch_id="cohort-root",
    )
    _write_close_checkpoint(run, manifest)
    raw_path = run / "control" / str(manifest["raw_path"])
    manifest_path = raw_path.with_suffix(".manifest.json")
    immutable_pair_before = (
        raw_path.read_bytes(),
        manifest_path.read_bytes(),
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    successor = tmp_path / "runs" / "run-coordinated-anchor"
    _finalize_recovery_anchor(successor, expected)
    expected_full = recover_dynamic_short_crypto_runs(
        (run, successor),
        settlement_timeout_ms=3_600_000,
    )

    def mutate(
        connection: Any,
        checkpoint: dict[str, Any],
    ) -> None:
        target_id = record_ids[0]
        if attack == "missing":
            connection.execute(
                "DELETE FROM replay_records WHERE record_id = ?",
                (bytes.fromhex(target_id),),
            )
            connection.execute(
                "DELETE FROM finalized_records WHERE record_id = ?",
                (bytes.fromhex(target_id),),
            )
            checkpoint["replay_records"] = [
                item
                for item in checkpoint["replay_records"]
                if item["row"]["record_id"] != target_id
            ]
            checkpoint["validated_record_count"] -= 1
            return
        if attack == "rewired":
            nonexistent = (
                "control/raw/dynamic_short_crypto_service/"
                "nonexistent.jsonl"
            )
            connection.execute(
                "UPDATE replay_records SET relative_path = ? "
                "WHERE record_id = ?",
                (nonexistent, bytes.fromhex(target_id)),
            )
            for item in checkpoint["replay_records"]:
                if item["row"]["record_id"] == target_id:
                    item["relative_path"] = nonexistent
            return
        template = deepcopy(checkpoint["replay_records"][-1])
        forged_row = template["row"]
        forged_row["payload"]["event_type"] = (
            "attacker_inserted_state_neutral"
        )
        forged_row["payload"]["monotonic_ns"] = 999
        forged_row.pop("record_id")
        forged_id = canonical_record_id_from_json(forged_row)
        forged_row["record_id"] = forged_id
        source_id = int(
            connection.execute(
                "SELECT source_id FROM finalized_record_sources "
                "WHERE source = ?",
                ("dynamic_short_crypto_service",),
            ).fetchone()[0]
        )
        connection.execute(
            "INSERT INTO finalized_records "
            "(record_id, source_id, evidence_zlib) VALUES (?, ?, NULL)",
            (bytes.fromhex(forged_id), source_id),
        )
        connection.execute(
            "INSERT INTO replay_records "
            "(record_id, run_id, relative_path, row_zlib) "
            "VALUES (?, ?, ?, ?)",
            (
                bytes.fromhex(forged_id),
                run.name,
                template["relative_path"],
                recovery_module.zlib.compress(
                    canonical_json_bytes(forged_row),
                    level=1,
                ),
            ),
        )
        checkpoint["replay_records"].append(template)
        checkpoint["validated_record_count"] += 1

    _coordinated_rewrite_cache_artifacts(
        snapshot_path,
        run_roots=(run.resolve(),),
        mutate=mutate,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (run, successor),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert immutable_pair_before == (
        raw_path.read_bytes(),
        manifest_path.read_bytes(),
    )
    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected_full.canonical_bytes()
    assert len(actual.state["lifecycle_cohort_journal"]) == 1


def test_cache_rebuilds_event_type_and_connection_from_index_bound_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-forged-lifecycle-maps"
    _, record_ids = _finalize_events(
        run,
        [
            _transport_event(
                "heartbeat_ack",
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        batch_id="clob-liveness",
        store_relative="groups/group-a",
    )
    control_manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                kind="command",
            )
        ],
        batch_id="cohort-root",
    )
    _write_close_checkpoint(run, control_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    def forge_maps(checkpoint: dict[str, Any]) -> None:
        record_id = record_ids[0]
        checkpoint["record_connections"][record_id] = (
            "attacker-connection"
        )
        checkpoint["record_event_types"][record_id] = "book"

    _rewrite_snapshot_from_forged_checkpoint(
        snapshot_path,
        run_roots=(run.resolve(),),
        mutate_checkpoint=forge_maps,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_cache_rejects_forged_settlement_liveness_reference_maps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-forged-settlement-maps"
    target = _target()
    _, gamma_ids = _finalize_events(
        run,
        [
            {
                "schema_version": "edge-lab-public-http.v1",
                "source": "gamma_http",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {"market_id": target["market_id"]},
            }
        ],
        source="gamma_http",
        batch_id="gamma",
    )
    boundary_ids: list[str] = []
    for role, timestamp_ms in (
        ("open", target["opens_at_ms"]),
        ("close", target["closes_at_ms"]),
    ):
        _, record_ids = _finalize_events(
            run,
            [
                {
                    "schema_version": "edge-lab-rtds-chainlink.v1",
                    "source": "rtds_ws",
                    "received_at": RECEIVED_AT,
                    "event_at": None,
                    "sequence": None,
                    "payload": {
                        "symbol": "btc/usd",
                        "timestamp_ms": timestamp_ms,
                    },
                }
            ],
            source="rtds_ws",
            batch_id=f"rtds-{role}",
        )
        boundary_ids.append(record_ids[0])
    book_ids: list[str] = []
    for role, monotonic_ns in (("before", 10), ("after", 12)):
        _, record_ids = _finalize_events(
            run,
            [
                _market_event(
                    connection_id="connection-a",
                    monotonic_ns=monotonic_ns,
                )
            ],
            source="clob_market_ws",
            batch_id=f"book-{role}",
            store_relative="groups/group-a",
        )
        book_ids.append(record_ids[0])
    registry = _registry_snapshot(
        descriptor={
            "path": "/capture/a.jsonl",
            "sha256": "1" * 64,
            "byte_count": 10,
            "line_count": 1,
        },
        target=target,
    )
    service_manifest, _ = _finalize_events(
        run,
        [
            _service_event("registry_revision", registry, monotonic_ns=1),
            _service_event(
                "gamma_target_verified",
                {
                    "slug": target["slug"],
                    "market_id": target["market_id"],
                    "gamma_record_id": gamma_ids[0],
                    "gamma_body_sha256": "9" * 64,
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                    "scheduler_now_ms": 1_784_990_901_000,
                },
                monotonic_ns=2,
            ),
            *[
                _service_event(
                    "chainlink_boundary_evidence_committed",
                    {
                        "slug": target["slug"],
                        "boundary_role": role,
                        "evidence_record_id": record_id,
                        "scheduler_now_ms": timestamp_ms,
                    },
                    monotonic_ns=monotonic_ns,
                )
                for role, record_id, timestamp_ms, monotonic_ns in (
                    (
                        "open",
                        boundary_ids[0],
                        target["opens_at_ms"],
                        3,
                    ),
                    (
                        "close",
                        boundary_ids[1],
                        target["closes_at_ms"],
                        4,
                    ),
                )
            ],
            _service_event(
                "settlement_reconciled",
                {
                    "slug": target["slug"],
                    "status": "resolved",
                    "action": "capture_settled",
                    "reason_codes": [],
                    "gamma_record_ids": [gamma_ids[0]],
                    "evidence_record_id": gamma_ids[0],
                    "outcome": "up",
                    "available_at_ms": 1_784_991_301_000,
                    "chainlink_boundary_status": "verified",
                    "close_liveness": {
                        "connection_id": "connection-a",
                        "before_record_id": book_ids[0],
                        "after_record_id": book_ids[1],
                    },
                    "exclusion_reason": None,
                    "scheduler_now_ms": 1_784_991_301_000,
                },
                monotonic_ns=5,
            ),
        ],
    )
    _write_close_checkpoint(run, service_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    assert expected.state["targets"][target["slug"]]["state"] == "excluded"

    def forge_liveness_maps(checkpoint: dict[str, Any]) -> None:
        for record_id in book_ids:
            checkpoint["record_connections"][record_id] = "connection-a"
            checkpoint["record_event_types"][record_id] = "heartbeat_ack"

    _rewrite_snapshot_from_forged_checkpoint(
        snapshot_path,
        run_roots=(run.resolve(),),
        mutate_checkpoint=forge_liveness_maps,
    )
    forged = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert forged["result"]["state"]["targets"][target["slug"]][
        "state"
    ] == "settled"
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_cache_rebuilds_integrity_gaps_and_classification_from_index_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-forged-integrity-gap"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    raw_path = next(run.rglob("*.jsonl"))
    manifest_path = raw_path.with_suffix(".manifest.json")
    if os.name == "nt":
        manifest_path.chmod(0o666)
    manifest_path.unlink()
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    assert "raw_without_manifest" in {
        gap["code"] for gap in expected.gaps
    }

    def suppress_integrity_gap(checkpoint: dict[str, Any]) -> None:
        checkpoint["gaps"].clear()
        checkpoint["exclusions"].clear()
        checkpoint["runs_with_input_integrity_gaps"].clear()
        checkpoint["classifications"] = [
            {
                "run_id": run.name,
                "classification": "clean_completed",
                "conditions": ["clean_completed"],
            }
        ]

    _rewrite_snapshot_from_forged_checkpoint(
        snapshot_path,
        run_roots=(run.resolve(),),
        mutate_checkpoint=suppress_integrity_gap,
    )
    forged = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert forged["result"]["gaps"] == []
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


@pytest.mark.parametrize("forgery", ("extra", "duplicate", "rewired"))
def test_cache_requires_checkpoint_index_replay_bijection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forgery: str,
) -> None:
    run = tmp_path / "runs" / f"run-replay-{forgery}"
    _, market_ids = _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        batch_id="irrelevant-book",
        store_relative="groups/group-a",
    )
    control_manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "lifecycle_cohort_committed",
                _cohort_commitment_payload(),
                monotonic_ns=2,
                kind="command",
            ),
            _service_event(
                "diagnostic_marker",
                {"safe": True},
                monotonic_ns=3,
            ),
        ],
        batch_id="control",
    )
    _write_close_checkpoint(run, control_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    def forge_replay(checkpoint: dict[str, Any]) -> None:
        replay = checkpoint["replay_records"]
        marker = next(
            item
            for item in replay
            if item["row"]["payload"].get("event_type")
            == "diagnostic_marker"
        )
        if forgery == "extra":
            raw_path = next(
                path
                for path in run.rglob("*.jsonl")
                if market_ids[0] in path.read_text(encoding="utf-8")
            )
            replay.append(
                {
                    "run_id": run.name,
                    "relative_path": raw_path.relative_to(run).as_posix(),
                    "row": json.loads(
                        raw_path.read_text(encoding="utf-8")
                    ),
                }
            )
        elif forgery == "duplicate":
            replay.append(deepcopy(marker))
        else:
            marker["relative_path"] = (
                "control/raw/dynamic_short_crypto_service/rewired.jsonl"
            )
        replay.sort(
            key=lambda item: recovery_module._replay_sort_key(
                (
                    item["run_id"],
                    item["relative_path"],
                    item["row"],
                )
            )
        )

    _rewrite_snapshot_from_forged_checkpoint(
        snapshot_path,
        run_roots=(run.resolve(),),
        mutate_checkpoint=forge_replay,
    )
    original_recover = recovery_module._recover_dynamic_short_crypto_runs
    full_replay_calls = 0

    def counted_recover(*args: Any, **kwargs: Any) -> Any:
        nonlocal full_replay_calls
        if not kwargs.get("_checkpoint_only_reduction", False):
            full_replay_calls += 1
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_recover_dynamic_short_crypto_runs",
        counted_recover,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert full_replay_calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_snapshot_reuses_validated_prefix_for_recovery_only_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    capture_decision = _capture_decision()
    prefix_manifest, prefix_record_ids = _finalize_events(
        prefix_run,
        [
            _service_event(
                "capture_decision",
                capture_decision,
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(
        prefix_run,
        prefix_manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": prefix_record_ids[0],
            "reason": "capture_decision_durable_before_apply",
            "updated_at": RECEIVED_AT,
        },
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix_result = (
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            prefix_run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )
    )

    suffix_run = tmp_path / "runs" / "run-recovery-only"
    recovery_event = _service_event(
        "restart_recovery_decision",
        {
            **prefix_result.recovery_decision,
            "run_classifications": list(
                prefix_result.run_classifications
            ),
            "gaps": list(prefix_result.gaps),
            "exclusions": list(prefix_result.exclusions),
            "worker_actions": list(prefix_result.worker_actions),
            "scheduler_now_ms": 1_784_990_950_000,
            "recovery_anchor": deepcopy(
                prefix_result.anchor_payload
            ),
        },
        monotonic_ns=2,
    )
    recovery_event["kind"] = "command"
    suffix_manifest, suffix_record_ids = _finalize_events(
        suffix_run,
        [recovery_event],
    )
    _write_close_checkpoint(
        suffix_run,
        suffix_manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": suffix_record_ids[0],
            "reason": "restart_recovery_durable_before_apply",
            "updated_at": RECEIVED_AT,
        },
    )
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    original = canonical_record_id_from_json

    def reject_prefix_row_revalidation(record: object) -> str:
        if (
            isinstance(record, dict)
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("session_id")
            == "worker-session"
        ):
            raise AssertionError(
                "an append-only cache hit must not revalidate prefix rows"
            )
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        reject_prefix_row_revalidation,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_snapshot_checkpoint_omits_unreferenced_public_evidence_maps(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-compact-evidence-checkpoint"
    _, gamma_record_ids = _finalize_events(
        run,
        [
            {
                "schema_version": "edge-lab-public-http.v1",
                "source": "gamma_http",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {"market_id": f"market-{index}"},
            }
            for index in range(25)
        ],
        source="gamma_http",
        batch_id="gamma-many",
    )
    _finalize_events(
        run,
        [
            _service_event(
                "gamma_target_verified",
                {
                    "slug": "btc-updown-5m-1784990900",
                    "market_id": "market-0",
                    "gamma_record_id": gamma_record_ids[0],
                    "gamma_body_sha256": "9" * 64,
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                    "scheduler_now_ms": 1_784_990_901_000,
                },
                monotonic_ns=2,
            )
        ],
        batch_id="service-reference",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    checkpoint = snapshot["replay_checkpoint"]

    assert checkpoint["record_sources"] == {
        gamma_record_ids[0]: "gamma_http"
    }
    assert set(checkpoint["records_by_id"]) == {gamma_record_ids[0]}
    assert snapshot["record_indexes"][0]["row_count"] == 26


def test_cached_recovery_does_not_requery_replay_proven_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-replay-proven-source"
    _, record_ids = _finalize_events(
        run,
        [
            _transport_event(
                "connected",
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    def reject_redundant_scalar_lookup(
        self: object,
        record_id: str,
    ) -> str | None:
        raise AssertionError(
            "replay-proven sources must not be queried again: "
            f"{record_id}"
        )

    monkeypatch.setattr(
        recovery_module._FinalizedRecordIndex,
        "source_for",
        reject_redundant_scalar_lookup,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["replay_checkpoint"]["record_sources"] == {
        record_ids[0]: "clob_market_ws"
    }


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_code"),
    [
        (
            "gamma_target_verified",
            {
                "slug": "btc-updown-5m-1784990900",
                "gamma_record_id": "not-a-record-id",
            },
            "gamma_evidence_invalid",
        ),
        (
            "chainlink_boundary_evidence_committed",
            {
                "slug": "btc-updown-5m-1784990900",
                "boundary_role": "open",
                "evidence_record_id": "not-a-record-id",
            },
            "chainlink_evidence_invalid",
        ),
    ],
)
def test_malformed_public_evidence_reference_has_stable_error_code(
    tmp_path: Path,
    event_type: str,
    payload: dict[str, Any],
    expected_code: str,
) -> None:
    run = tmp_path / "runs" / f"run-{event_type}"
    _finalize_events(
        run,
        [_service_event(event_type, payload, monotonic_ns=1)],
    )

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == expected_code


def test_snapshot_reduces_state_changing_suffix_without_revalidating_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    _, prefix_market_record_ids = _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    prefix_decision = _capture_decision()
    prefix_receipt = {
        "decision_id": prefix_decision["decision_id"],
        "group_id": prefix_decision["group_id"],
        "asset_ids": prefix_decision["asset_ids"],
        "target_slugs": [_target()["slug"]],
        "scheduler_now_ms": 1_784_990_941_000,
    }
    prefix_manifest, prefix_control_record_ids = _finalize_events(
        prefix_run,
        [
            _service_event(
                "capture_decision",
                prefix_decision,
                monotonic_ns=2,
            ),
            _service_event(
                "worker_start_applied",
                prefix_receipt,
                monotonic_ns=3,
            ),
        ],
    )
    retained_partial = (
        prefix_run
        / "groups"
        / "group-a"
        / "raw"
        / "clob_market_ws"
        / "retained-bad-sample.jsonl.partial"
    )
    retained_partial.write_text(
        "retained-bad-sample",
        encoding="utf-8",
    )
    _write_close_checkpoint(
        prefix_run,
        prefix_manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": prefix_control_record_ids[1],
            "reason": "worker_start_receipt",
            "updated_at": RECEIVED_AT,
        },
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    suffix_run = tmp_path / "runs" / "run-state-changing"
    _finalize_recovery_anchor(suffix_run, prefix)
    suffix_decision = _capture_decision(
        sequence=8,
        group_id="short-crypto-" + "2" * 24,
    )
    suffix_event = _service_event(
        "capture_decision",
        suffix_decision,
        monotonic_ns=3,
    )
    suffix_event["kind"] = "command"
    suffix_manifest, suffix_record_ids = _finalize_events(
        suffix_run,
        [suffix_event],
    )
    _write_close_checkpoint(
        suffix_run,
        suffix_manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": suffix_record_ids[0],
            "reason": "capture_decision_durable_before_apply",
            "updated_at": RECEIVED_AT,
        },
    )
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    prefix_record_ids = {
        *prefix_market_record_ids,
        *prefix_control_record_ids,
    }
    original = canonical_record_id_from_json

    def reject_prefix_row_revalidation(record: object) -> str:
        if (
            isinstance(record, dict)
            and record.get("record_id") in prefix_record_ids
        ):
            raise AssertionError(
                "an append-only cache hit must not revalidate prefix rows"
            )
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        reject_prefix_row_revalidation,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    assert retained_partial.read_text(encoding="utf-8") == (
        "retained-bad-sample"
    )


def test_snapshot_reduces_v2_registry_delta_against_validated_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    first_delta = _registry_snapshot(
        descriptor={
            "path": "/capture/a.jsonl",
            "sha256": "1" * 64,
            "byte_count": 10,
            "line_count": 1,
        },
        target={
            **_target(),
            "provenance": [
                {
                    "record_id": "b" * 64,
                    "path": "/capture/a.jsonl",
                    "line": 1,
                }
            ],
        },
    )
    prefix_manifest, prefix_record_ids = _finalize_events(
        prefix_run,
        [
            _service_event(
                "registry_revision",
                first_delta,
                monotonic_ns=1,
            )
        ],
    )
    _write_close_checkpoint(prefix_run, prefix_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    suffix_run = tmp_path / "runs" / "run-registry-delta"
    _finalize_recovery_anchor(suffix_run, prefix)
    second_delta = _registry_snapshot(
        descriptor={
            "path": "/capture/b.jsonl",
            "sha256": "2" * 64,
            "byte_count": 11,
            "line_count": 1,
        },
        target={
            **_target(),
            "provenance": [
                {
                    "record_id": "c" * 64,
                    "path": "/capture/b.jsonl",
                    "line": 1,
                }
            ],
        },
    )
    cumulative = merge_short_crypto_registry_snapshots(
        first_delta,
        second_delta,
    )
    second_revision = {
        "schema_version": (
            "edge-lab-short-crypto-registry-revision.v2"
        ),
        "delta_snapshot": second_delta,
        "cumulative_snapshot_sha256": cumulative[
            "snapshot_sha256"
        ],
        "cumulative_input_file_count": len(cumulative["inputs"]),
        "cumulative_target_count": len(cumulative["targets"]),
    }
    suffix_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "registry_revision",
                second_revision,
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(suffix_run, suffix_manifest)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    original = canonical_record_id_from_json

    def reject_prefix_row_revalidation(record: object) -> str:
        if (
            isinstance(record, dict)
            and record.get("record_id") in prefix_record_ids
        ):
            raise AssertionError(
                "a registry delta must reduce from the validated prefix"
            )
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        reject_prefix_row_revalidation,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert [
        descriptor["run_ids"]
        for descriptor in snapshot["record_indexes"]
    ] == [["run-prefix"], ["run-registry-delta"]]
    assert snapshot["replay_checkpoint"]["run_ids"] == [
        "run-prefix",
        "run-registry-delta",
    ]


def test_snapshot_extends_legacy_registry_compaction_without_prefix_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    first_delta = _registry_snapshot(
        descriptor={
            "path": "/capture/a.jsonl",
            "sha256": "1" * 64,
            "byte_count": 10,
            "line_count": 1,
        },
        target={
            **_target(),
            "provenance": [
                {
                    "record_id": "b" * 64,
                    "path": "/capture/a.jsonl",
                    "line": 1,
                }
            ],
        },
    )
    prefix_manifest, prefix_record_ids = _finalize_events(
        prefix_run,
        [
            _service_event(
                "registry_revision",
                first_delta,
                monotonic_ns=1,
            )
        ],
    )
    _write_close_checkpoint(prefix_run, prefix_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    suffix_run = tmp_path / "runs" / "run-registry-cumulative"
    _finalize_recovery_anchor(suffix_run, prefix)
    second_delta = _registry_snapshot(
        descriptor={
            "path": "/capture/b.jsonl",
            "sha256": "2" * 64,
            "byte_count": 11,
            "line_count": 1,
        },
        target={
            **_target(),
            "provenance": [
                {
                    "record_id": "c" * 64,
                    "path": "/capture/b.jsonl",
                    "line": 1,
                }
            ],
        },
    )
    cumulative = merge_short_crypto_registry_snapshots(
        first_delta,
        second_delta,
    )
    suffix_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "registry_revision",
                cumulative,
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(suffix_run, suffix_manifest)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    original = canonical_record_id_from_json

    def reject_prefix_row_revalidation(record: object) -> str:
        if (
            isinstance(record, dict)
            and record.get("record_id") in prefix_record_ids
        ):
            raise AssertionError(
                "legacy registry compaction must reuse its checkpoint"
            )
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        reject_prefix_row_revalidation,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    assert actual.state["registry"]["snapshot_sha256"] == cumulative[
        "snapshot_sha256"
    ]


def test_snapshot_exact_index_rejects_suffix_duplicate_of_prefix_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    suffix_run = tmp_path / "runs" / "run-suffix"
    _finalize_recovery_anchor(suffix_run, prefix)
    _finalize_events(
        suffix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    original = recovery_module._validate_finalized_pair

    def reject_prefix_pair_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        if run_root == prefix_run.resolve():
            raise AssertionError(
                "duplicate detection must query the exact prefix index"
            )
        return original(run_root, run_id, raw_path)

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_prefix_pair_revalidation,
    )
    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, suffix_run),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert caught.value.code == "duplicate_record_id"
    assert not list(
        snapshot_path.parent.glob(
            f".{snapshot_path.name}.record-index.*.sqlite3"
        )
    )


def test_snapshot_exact_index_resolves_suffix_reference_to_prefix_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    target = _target()
    _, gamma_record_ids = _finalize_events(
        prefix_run,
        [
            {
                "schema_version": "edge-lab-public-http.v1",
                "source": "gamma_http",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {"market_id": target["market_id"]},
            }
        ],
        source="gamma_http",
        batch_id="gamma-a",
    )
    registry = _registry_snapshot(
        descriptor={
            "path": "/capture/a.jsonl",
            "sha256": "1" * 64,
            "byte_count": 10,
            "line_count": 1,
        },
        target=target,
    )
    prefix_manifest, _ = _finalize_events(
        prefix_run,
        [
            _service_event(
                "registry_revision",
                registry,
                monotonic_ns=1,
            )
        ],
    )
    _write_close_checkpoint(prefix_run, prefix_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    suffix_run = tmp_path / "runs" / "run-suffix"
    _finalize_recovery_anchor(suffix_run, prefix)
    suffix_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "gamma_target_verified",
                {
                    "slug": target["slug"],
                    "market_id": target["market_id"],
                    "gamma_record_id": gamma_record_ids[0],
                    "gamma_body_sha256": "9" * 64,
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                    "scheduler_now_ms": 1_784_990_901_000,
                },
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(suffix_run, suffix_manifest)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    original = recovery_module._validate_finalized_pair

    def reject_prefix_pair_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        if run_root == prefix_run.resolve():
            raise AssertionError(
                "prefix evidence must resolve from the exact index"
            )
        return original(run_root, run_id, raw_path)

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_prefix_pair_revalidation,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    assert actual.state["latest_gamma_records"][target["slug"]][
        "record_id"
    ] == gamma_record_ids[0]


def test_snapshot_exact_index_rejects_wrong_prefix_evidence_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    target = _target()
    _, clob_record_ids = _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    suffix_run = tmp_path / "runs" / "run-suffix"
    _finalize_recovery_anchor(suffix_run, prefix)
    suffix_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "gamma_target_verified",
                {
                    "slug": target["slug"],
                    "market_id": target["market_id"],
                    "gamma_record_id": clob_record_ids[0],
                    "gamma_body_sha256": "9" * 64,
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                    "scheduler_now_ms": 1_784_990_901_000,
                },
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(suffix_run, suffix_manifest)
    original = recovery_module._validate_finalized_pair

    def reject_prefix_pair_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        if run_root == prefix_run.resolve():
            raise AssertionError(
                "wrong source must resolve from the exact prefix index"
            )
        return original(run_root, run_id, raw_path)

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_prefix_pair_revalidation,
    )
    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, suffix_run),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert caught.value.code == "gamma_evidence_not_finalized"


def test_snapshot_reduces_cross_run_settlement_from_exact_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    target = _target()
    group_id = "short-crypto-" + "1" * 24
    _, gamma_record_ids = _finalize_events(
        prefix_run,
        [
            {
                "schema_version": "edge-lab-public-http.v1",
                "source": "gamma_http",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {"market_id": target["market_id"]},
            }
        ],
        source="gamma_http",
        batch_id="gamma-a",
    )
    _, open_boundary_ids = _finalize_events(
        prefix_run,
        [
            {
                "schema_version": "edge-lab-rtds-chainlink.v1",
                "source": "rtds_ws",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp_ms": target["opens_at_ms"],
                },
            }
        ],
        source="rtds_ws",
        batch_id="rtds-open",
    )
    _, before_liveness_ids = _finalize_events(
        prefix_run,
        [
            _transport_event(
                "heartbeat_ack",
                connection_id="connection-a",
                monotonic_ns=10,
            )
        ],
        source="clob_market_ws",
        batch_id="clob-before",
        store_relative=f"groups/{group_id}",
    )
    decision = _capture_decision(sequence=4, group_id=group_id)
    registry = _registry_snapshot(
        descriptor={
            "path": "/capture/a.jsonl",
            "sha256": "1" * 64,
            "byte_count": 10,
            "line_count": 1,
        },
        target=target,
    )
    prefix_manifest, _ = _finalize_events(
        prefix_run,
        [
            _service_event(
                "registry_revision",
                registry,
                monotonic_ns=1,
            ),
            _service_event(
                "capture_decision",
                decision,
                monotonic_ns=2,
            ),
            _service_event(
                "worker_start_applied",
                {
                    "decision_id": decision["decision_id"],
                    "group_id": group_id,
                    "asset_ids": decision["asset_ids"],
                    "target_slugs": [target["slug"]],
                    "scheduler_now_ms": 1_784_990_941_000,
                },
                monotonic_ns=3,
            ),
        ],
    )
    _write_close_checkpoint(prefix_run, prefix_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    prefix = recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    suffix_run = tmp_path / "runs" / "run-suffix"
    _finalize_recovery_anchor(suffix_run, prefix)
    _, close_boundary_ids = _finalize_events(
        suffix_run,
        [
            {
                "schema_version": "edge-lab-rtds-chainlink.v1",
                "source": "rtds_ws",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp_ms": target["closes_at_ms"],
                },
            }
        ],
        source="rtds_ws",
        batch_id="rtds-close",
    )
    _, after_liveness_ids = _finalize_events(
        suffix_run,
        [
            _transport_event(
                "heartbeat_ack",
                connection_id="connection-a",
                monotonic_ns=13,
            )
        ],
        source="clob_market_ws",
        batch_id="clob-after",
        store_relative=f"groups/{group_id}",
    )
    suffix_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "gamma_target_verified",
                {
                    "slug": target["slug"],
                    "market_id": target["market_id"],
                    "gamma_record_id": gamma_record_ids[0],
                    "gamma_body_sha256": "9" * 64,
                    "active": True,
                    "closed": False,
                    "accepting_orders": True,
                    "scheduler_now_ms": 1_784_990_901_000,
                },
                monotonic_ns=2,
            ),
            _service_event(
                "chainlink_boundary_evidence_committed",
                {
                    "slug": target["slug"],
                    "boundary_role": "open",
                    "evidence_record_id": open_boundary_ids[0],
                    "scheduler_now_ms": target["opens_at_ms"],
                },
                monotonic_ns=3,
            ),
            _service_event(
                "chainlink_boundary_evidence_committed",
                {
                    "slug": target["slug"],
                    "boundary_role": "close",
                    "evidence_record_id": close_boundary_ids[0],
                    "scheduler_now_ms": target["closes_at_ms"],
                },
                monotonic_ns=4,
            ),
            _service_event(
                "settlement_reconciled",
                {
                    "slug": target["slug"],
                    "status": "resolved",
                    "action": "capture_settled",
                    "reason_codes": [],
                    "gamma_record_ids": [gamma_record_ids[0]],
                    "evidence_record_id": gamma_record_ids[0],
                    "outcome": "up",
                    "available_at_ms": 1_784_991_301_000,
                    "chainlink_boundary_status": "verified",
                    "close_liveness": {
                        "connection_id": "connection-a",
                        "before_record_id": before_liveness_ids[0],
                        "after_record_id": after_liveness_ids[0],
                    },
                    "exclusion_reason": None,
                    "scheduler_now_ms": 1_784_991_301_000,
                },
                monotonic_ns=5,
            ),
            _service_event(
                "worker_stopped",
                {
                    "group_id": group_id,
                    "manifest_count": 3,
                    "acknowledged": False,
                    "decision_id": None,
                    "scheduler_now_ms": 1_784_991_302_000,
                },
                monotonic_ns=6,
            ),
        ],
    )
    _write_close_checkpoint(suffix_run, suffix_manifest)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    original = recovery_module._validate_finalized_pair

    def reject_prefix_pair_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        if run_root == prefix_run.resolve():
            raise AssertionError(
                "settlement suffix must use the validated prefix"
            )
        return original(run_root, run_id, raw_path)

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_prefix_pair_revalidation,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    assert actual.state["targets"][target["slug"]]["state"] == "settled"
    assert actual.state["targets"][target["slug"]]["settlement"][
        "outcome"
    ] == "up"


def test_snapshot_write_failure_leaves_unreferenced_readonly_index_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    prefix_manifest, _ = _finalize_events(
        prefix_run,
        [
            _service_event(
                "capture_decision",
                _capture_decision(),
                monotonic_ns=1,
            )
        ],
    )
    _write_close_checkpoint(prefix_run, prefix_manifest)
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    committed_snapshot = snapshot_path.read_bytes()
    committed_indexes = set(
        snapshot_path.parent.glob(
            "recovery-snapshot.record-index-*.sqlite3"
        )
    )

    suffix_run = tmp_path / "runs" / "run-suffix"
    suffix_manifest, _ = _finalize_events(
        suffix_run,
        [
            _service_event(
                "capture_decision",
                _capture_decision(
                    sequence=8,
                    group_id="short-crypto-" + "2" * 24,
                ),
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(suffix_run, suffix_manifest)
    expected = recover_dynamic_short_crypto_runs(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
    )
    atomic_write = recovery_module._atomic_write_bytes

    def fail_snapshot_commit(path: Path, content: bytes) -> None:
        raise OSError(f"synthetic snapshot commit failure: {path}")

    monkeypatch.setattr(
        recovery_module,
        "_atomic_write_bytes",
        fail_snapshot_commit,
    )
    with pytest.raises(OSError, match="synthetic snapshot commit failure"):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, suffix_run),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert snapshot_path.read_bytes() == committed_snapshot
    all_indexes = set(
        snapshot_path.parent.glob(
            "recovery-snapshot.record-index-*.sqlite3"
        )
    )
    orphan_indexes = all_indexes - committed_indexes
    assert len(orphan_indexes) == 1
    orphan_path = orphan_indexes.pop()
    assert orphan_path.stat().st_mode & 0o777 == 0o444
    monkeypatch.setattr(
        recovery_module,
        "_atomic_write_bytes",
        atomic_write,
    )
    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert actual.canonical_bytes() == expected.canonical_bytes()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["record_indexes"][-1]["path"] == orphan_path.name
    assert not list(
        snapshot_path.parent.glob(
            f".{snapshot_path.name}.record-index.*.sqlite3"
        )
    )


def test_record_index_directory_fsync_failure_never_commits_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-directory-fsync-failure"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    original_open = recovery_module.os.open

    def reject_directory_open(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        candidate = Path(path)  # type: ignore[arg-type]
        if candidate == snapshot_path.parent and candidate.is_dir():
            raise OSError("synthetic directory open failure")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(recovery_module.os, "open", reject_directory_open)
    with pytest.raises(OSError, match="synthetic directory open failure"):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert not snapshot_path.exists()
    orphan_indexes = list(
        snapshot_path.parent.glob(
            "recovery-snapshot.record-index-*.sqlite3"
        )
    )
    assert len(orphan_indexes) == 1
    assert orphan_indexes[0].stat().st_mode & 0o777 == 0o444
    assert not list(
        snapshot_path.parent.glob(
            f".{snapshot_path.name}.record-index.*.sqlite3"
        )
    )


def test_record_index_failure_cleans_temporary_sqlite_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-temporary-index-sidecar"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"

    def fail_with_sidecar(
        snapshot_path: Path,
        *,
        index: object,
        pending_path: Path,
        run_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        pending_path.with_name(pending_path.name + "-wal").write_bytes(
            b"synthetic-pending-wal"
        )
        raise OSError("synthetic index finalization failure")

    monkeypatch.setattr(
        recovery_module,
        "_finalize_record_index",
        fail_with_sidecar,
    )
    with pytest.raises(
        OSError,
        match="synthetic index finalization failure",
    ):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert not snapshot_path.exists()
    assert not list(
        snapshot_path.parent.glob(
            f".{snapshot_path.name}.record-index.*"
        )
    )


@pytest.mark.parametrize("mutation", ["reverse", "drop_suffix"])
def test_snapshot_rejects_reordered_or_incomplete_index_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    prefix_run = tmp_path / "runs" / "run-prefix"
    _finalize_events(
        prefix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        prefix_run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    suffix_run = tmp_path / "runs" / "run-suffix"
    _finalize_events(
        suffix_run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=2,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        (prefix_run, suffix_run),
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_id"
    }
    if mutation == "reverse":
        body["record_indexes"] = list(
            reversed(body["record_indexes"])
        )
    else:
        body["record_indexes"] = body["record_indexes"][:-1]
    forged = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(canonical_json_bytes(forged) + b"\n")
    snapshot_path.chmod(0o444)
    original = recovery_module._validate_finalized_pair

    def reject_prefix_pair_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        if run_root == prefix_run.resolve():
            raise AssertionError(
                "invalid shard coverage must force full replay"
            )
        return original(run_root, run_id, raw_path)

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_prefix_pair_revalidation,
    )
    with pytest.raises(
        AssertionError,
        match="invalid shard coverage must force full replay",
    ):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            (prefix_run, suffix_run),
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert not list(
        snapshot_path.parent.glob(
            f".{snapshot_path.name}.record-index.*.sqlite3"
        )
    )


def test_snapshot_raw_drift_forces_full_canonical_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-snapshot-raw-drift"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    raw_path = next(run.rglob("*.jsonl"))
    row = json.loads(raw_path.read_bytes())
    row["received_at"] = "2026-07-24T15:00:01.000000Z"
    row["record_id"] = canonical_record_id_from_json(row)
    raw_path.chmod(0o644)
    raw_path.write_bytes(canonical_json_bytes(row) + b"\n")
    _refresh_manifest_checksum(raw_path)
    calls = 0
    original = canonical_record_id_from_json

    def count_revalidation(record: object) -> str:
        nonlocal calls
        calls += 1
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        count_revalidation,
    )
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert calls == 1


def test_added_partial_invalidates_snapshot_without_reading_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-snapshot-partial"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    partial = (
        run
        / "groups"
        / "group-a"
        / "raw"
        / "clob_market_ws"
        / "crash.jsonl.partial"
    )
    partial.write_text("must-not-be-opened", encoding="utf-8")
    original_digest = recovery_module._stable_digest

    def reject_partial_reads(
        path: Path,
        *,
        code: str,
        count_lines: bool,
    ) -> tuple[str, int, int | None]:
        assert not str(path).endswith(".jsonl.partial")
        return original_digest(
            path,
            code=code,
            count_lines=count_lines,
        )

    monkeypatch.setattr(
        recovery_module,
        "_stable_digest",
        reject_partial_reads,
    )
    result = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert {
        "code": "orphan_partial_ignored",
        "run_id": run.name,
        "path": partial.relative_to(run).as_posix(),
        "recovery": "earlier_finalized_state_or_public_reacquisition",
    } in result.exclusions


def test_corrupt_snapshot_is_never_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-snapshot-corrupt"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(b'{"forged":true}\n')
    calls = 0
    original = canonical_record_id_from_json

    def count_revalidation(record: object) -> str:
        nonlocal calls
        calls += 1
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        count_revalidation,
    )
    result = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert calls == 1
    assert result.replayed_record_count == 1
    assert snapshot_path.stat().st_mode & 0o777 == 0o444


def test_legacy_result_only_snapshot_forces_full_v2_proof_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-legacy-snapshot"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    current = json.loads(snapshot_path.read_text(encoding="utf-8"))
    legacy_body = {
        "schema_version": (
            "edge-lab-dynamic-short-crypto-recovery-snapshot.v1"
        ),
        "validator_contract": current["validator_contract"],
        "settlement_timeout_ms": current["settlement_timeout_ms"],
        "input_commitment": current["input_commitment"],
        "input_summary": current["input_summary"],
        "recovered_from_run_ids": current["recovered_from_run_ids"],
        "result": current["result"],
    }
    legacy = {
        **legacy_body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(legacy_body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(canonical_json_bytes(legacy) + b"\n")
    snapshot_path.chmod(0o444)
    calls = 0
    original = canonical_record_id_from_json

    def count_revalidation(record: object) -> str:
        nonlocal calls
        calls += 1
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        count_revalidation,
    )
    result = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    refreshed = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert calls == 1
    assert result.replayed_record_count == 1
    assert refreshed["schema_version"] == (
        "edge-lab-dynamic-short-crypto-recovery-snapshot.v2"
    )
    assert refreshed["replay_checkpoint"]["run_ids"] == [run.name]
    assert refreshed["record_indexes"][0]["row_count"] == 1


def test_malformed_input_summary_falls_back_to_full_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-malformed-input-summary"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    expected = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["input_summary"]["root_content_commitments"] = [
        "not-an-object"
    ]
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_id"
    }
    forged = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(canonical_json_bytes(forged) + b"\n")
    snapshot_path.chmod(0o444)
    calls = 0
    original_validate = recovery_module._validate_finalized_pair

    def count_full_revalidation(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        count_full_revalidation,
    )

    actual = recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )

    assert calls == 1
    assert actual.canonical_bytes() == expected.canonical_bytes()


def test_snapshot_rejects_index_path_without_content_hash_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-index-name-forgery"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    descriptor = snapshot["record_indexes"][0]
    original_index = snapshot_path.parent / descriptor["path"]
    forged_name = "recovery-snapshot.record-index-forged.sqlite3"
    forged_index = snapshot_path.parent / forged_name
    forged_index.write_bytes(original_index.read_bytes())
    forged_index.chmod(0o444)
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_id"
    }
    body["record_indexes"][0]["path"] = forged_name
    forged_snapshot = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(
        canonical_json_bytes(forged_snapshot) + b"\n"
    )
    snapshot_path.chmod(0o444)
    original_validate = recovery_module._validate_finalized_pair

    def reject_full_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        raise AssertionError(
            "non-content-addressed index path must force full replay"
        )

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_full_revalidation,
    )
    with pytest.raises(
        AssertionError,
        match="non-content-addressed index path must force full replay",
    ):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )
    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        original_validate,
    )


@pytest.mark.parametrize(
    "forged_mode",
    (0o400, 0o4644, 0o2444, 0o1444),
)
def test_snapshot_rejects_record_index_without_exact_readonly_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    forged_mode: int,
) -> None:
    run = tmp_path / "runs" / "run-index-mode-drift"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    index_path = (
        snapshot_path.parent / snapshot["record_indexes"][0]["path"]
    )
    index_path.chmod(forged_mode)
    if stat.S_IMODE(index_path.stat().st_mode) != forged_mode:
        pytest.skip("filesystem did not retain requested permission bits")

    def reject_full_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        raise AssertionError(
            "record index mode drift must force full replay"
        )

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_full_revalidation,
    )
    with pytest.raises(
        AssertionError,
        match="record index mode drift must force full replay",
    ):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )


def test_snapshot_rejects_record_index_with_weakened_sqlite_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-index-schema-drift"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    descriptor = snapshot["record_indexes"][0]
    original_index = snapshot_path.parent / descriptor["path"]
    original_connection = recovery_module.sqlite3.connect(
        f"{original_index.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    try:
        sources = original_connection.execute(
            "SELECT source_id, source FROM finalized_record_sources"
        ).fetchall()
        records = original_connection.execute(
            "SELECT record_id, source_id, evidence_zlib "
            "FROM finalized_records"
        ).fetchall()
    finally:
        original_connection.close()
    pending_forgery = snapshot_path.parent / "weakened.sqlite3"
    forged_connection = recovery_module.sqlite3.connect(pending_forgery)
    try:
        forged_connection.execute(
            "CREATE TABLE finalized_record_sources ("
            "source_id INTEGER PRIMARY KEY, "
            "source TEXT NOT NULL"
            ")"
        )
        forged_connection.execute(
            "CREATE TABLE finalized_records ("
            "record_id BLOB PRIMARY KEY, "
            "source_id INTEGER NOT NULL, "
            "evidence_zlib BLOB"
            ") WITHOUT ROWID"
        )
        forged_connection.executemany(
            "INSERT INTO finalized_record_sources(source_id, source) "
            "VALUES (?, ?)",
            sources,
        )
        forged_connection.executemany(
            "INSERT INTO finalized_records("
            "record_id, source_id, evidence_zlib"
            ") VALUES (?, ?, ?)",
            records,
        )
        forged_connection.commit()
    finally:
        forged_connection.close()
    forged_bytes = pending_forgery.read_bytes()
    forged_hash = hashlib.sha256(forged_bytes).hexdigest()
    forged_name = (
        f"{snapshot_path.stem}.record-index-{forged_hash}.sqlite3"
    )
    forged_index = snapshot_path.parent / forged_name
    pending_forgery.replace(forged_index)
    forged_index.chmod(0o444)
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_id"
    }
    body["record_indexes"][0].update(
        {
            "path": forged_name,
            "sha256": forged_hash,
            "bytes": len(forged_bytes),
        }
    )
    forged_snapshot = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(
        canonical_json_bytes(forged_snapshot) + b"\n"
    )
    snapshot_path.chmod(0o444)

    def reject_full_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        raise AssertionError(
            "weakened SQLite schema must force full replay"
        )

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_full_revalidation,
    )
    with pytest.raises(
        AssertionError,
        match="weakened SQLite schema must force full replay",
    ):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )


def test_snapshot_rejects_record_index_with_orphan_source_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-index-source-orphan"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    descriptor = snapshot["record_indexes"][0]
    original_index = snapshot_path.parent / descriptor["path"]
    pending_forgery = snapshot_path.parent / "orphan-source.sqlite3"
    pending_forgery.write_bytes(original_index.read_bytes())
    forged_connection = recovery_module.sqlite3.connect(pending_forgery)
    try:
        forged_connection.execute(
            "UPDATE finalized_records SET source_id = 999999"
        )
        forged_connection.commit()
    finally:
        forged_connection.close()
    forged_bytes = pending_forgery.read_bytes()
    forged_hash = hashlib.sha256(forged_bytes).hexdigest()
    forged_name = (
        f"{snapshot_path.stem}.record-index-{forged_hash}.sqlite3"
    )
    forged_index = snapshot_path.parent / forged_name
    pending_forgery.replace(forged_index)
    forged_index.chmod(0o444)
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_id"
    }
    body["record_indexes"][0].update(
        {
            "path": forged_name,
            "sha256": forged_hash,
            "bytes": len(forged_bytes),
        }
    )
    forged_snapshot = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(
        canonical_json_bytes(forged_snapshot) + b"\n"
    )
    snapshot_path.chmod(0o444)

    def reject_full_revalidation(
        run_root: Path,
        run_id: str,
        raw_path: Path,
    ) -> object:
        raise AssertionError(
            "orphan record source must force full replay"
        )

    monkeypatch.setattr(
        recovery_module,
        "_validate_finalized_pair",
        reject_full_revalidation,
    )
    with pytest.raises(
        AssertionError,
        match="orphan record source must force full replay",
    ):
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )


def test_snapshot_rejects_uncommitted_sqlite_sidecar(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-index-sidecar"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    index_path = (
        snapshot_path.parent / snapshot["record_indexes"][0]["path"]
    )
    sidecar_path = index_path.with_name(index_path.name + "-wal")
    sidecar_path.write_bytes(b"uncommitted-index-state")

    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert caught.value.code == "recovery_record_index_unsafe"
    assert sidecar_path.read_bytes() == b"uncommitted-index-state"


@pytest.mark.parametrize(
    "attack",
    (
        "canonical_payload",
        "raw_schema",
        "indexed_source",
    ),
)
def test_exact_index_revalidates_canonical_evidence_on_lookup(
    tmp_path: Path,
    attack: str,
) -> None:
    run = tmp_path / "runs" / "run-index-evidence-forgery"
    _, record_ids = _finalize_events(
        run,
        [
            {
                "schema_version": "edge-lab-public-http.v1",
                "source": "gamma_http",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {"market_id": "market-a"},
            }
        ],
        source="gamma_http",
        batch_id="gamma-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    descriptor = snapshot["record_indexes"][0]
    original_index = snapshot_path.parent / descriptor["path"]
    pending_forgery = snapshot_path.parent / "forged-evidence.sqlite3"
    pending_forgery.write_bytes(original_index.read_bytes())
    raw_path = next(run.rglob("*.jsonl"))
    forged_row = json.loads(raw_path.read_text(encoding="utf-8"))
    query_record_id = record_ids[0]
    if attack == "canonical_payload":
        forged_row["payload"]["payload"]["market_id"] = "forged-market"
    elif attack == "raw_schema":
        forged_row["schema_version"] = "attacker.raw.v1"
        query_record_id = canonical_record_id_from_json(forged_row)
        forged_row["record_id"] = query_record_id
    else:
        forged_row["source"] = "rtds_ws"
        query_record_id = canonical_record_id_from_json(forged_row)
        forged_row["record_id"] = query_record_id
    forged_blob = recovery_module.zlib.compress(
        canonical_json_bytes(forged_row),
        level=1,
    )
    forged_connection = recovery_module.sqlite3.connect(pending_forgery)
    try:
        forged_connection.execute(
            "UPDATE finalized_records "
            "SET record_id = ?, evidence_zlib = ? "
            "WHERE record_id = ?",
            (
                bytes.fromhex(query_record_id),
                forged_blob,
                bytes.fromhex(record_ids[0]),
            ),
        )
        forged_connection.commit()
    finally:
        forged_connection.close()
    forged_bytes = pending_forgery.read_bytes()
    forged_hash = hashlib.sha256(forged_bytes).hexdigest()
    forged_name = (
        f"{snapshot_path.stem}.record-index-{forged_hash}.sqlite3"
    )
    forged_index = snapshot_path.parent / forged_name
    pending_forgery.replace(forged_index)
    forged_index.chmod(0o444)
    body = {
        key: value
        for key, value in snapshot.items()
        if key != "snapshot_id"
    }
    body["record_indexes"][0].update(
        {
            "path": forged_name,
            "sha256": forged_hash,
            "bytes": len(forged_bytes),
        }
    )
    forged_snapshot = {
        **body,
        "snapshot_id": hashlib.sha256(
            canonical_json_bytes(body)
        ).hexdigest(),
    }
    snapshot_path.chmod(0o644)
    snapshot_path.write_bytes(
        canonical_json_bytes(forged_snapshot) + b"\n"
    )
    snapshot_path.chmod(0o444)
    record_index = recovery_module._FinalizedRecordIndex(
        prefix_paths=(forged_index,),
    )
    try:
        with pytest.raises(RecoveryInputError) as caught:
            record_index.evidence_row(query_record_id)
    finally:
        record_index.close()

    assert caught.value.code == "recovery_record_index_invalid"


def test_corrupt_exact_index_is_never_trusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-index-corrupt"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=snapshot_path,
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    corrupt_index_path = (
        snapshot_path.parent / snapshot["record_indexes"][0]["path"]
    )
    corrupt_index_path.chmod(0o644)
    with corrupt_index_path.open("ab") as handle:
        handle.write(b"corrupt")
    corrupt_index_path.chmod(0o444)
    calls = 0
    original = canonical_record_id_from_json

    def count_revalidation(record: object) -> str:
        nonlocal calls
        calls += 1
        return original(record)

    monkeypatch.setattr(
        recovery_module,
        "canonical_record_id_from_json",
        count_revalidation,
    )
    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_path,
        )

    assert calls == 1
    assert caught.value.code == "recovery_record_index_unsafe"


def test_snapshot_symlink_is_rejected_without_following_or_replacing_it(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-snapshot-symlink"
    _finalize_events(
        run,
        [
            _market_event(
                connection_id="connection-a",
                monotonic_ns=1,
            )
        ],
        source="clob_market_ws",
        store_relative="groups/group-a",
    )
    real_snapshot = tmp_path / "service" / "real-snapshot.json"
    recovery_module.recover_dynamic_short_crypto_runs_cached(
        run,
        settlement_timeout_ms=3_600_000,
        snapshot_path=real_snapshot,
    )
    snapshot_link = tmp_path / "service" / "snapshot-link.json"
    snapshot_link.symlink_to(real_snapshot)

    with pytest.raises(RecoveryInputError) as caught:
        recovery_module.recover_dynamic_short_crypto_runs_cached(
            run,
            settlement_timeout_ms=3_600_000,
            snapshot_path=snapshot_link,
        )

    assert caught.value.code == "recovery_snapshot_unsafe"
    assert snapshot_link.is_symlink()
    assert real_snapshot.stat().st_mode & 0o777 == 0o444


def test_recovery_cpu_limiter_sleeps_to_respect_ratio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_times = iter((0.0, 1.0))
    monotonic_times = iter((0.0, 1.0))
    sleeps: list[float] = []
    monkeypatch.setattr(
        recovery_module.time,
        "process_time",
        lambda: next(process_times),
    )
    monkeypatch.setattr(
        recovery_module.time,
        "monotonic",
        lambda: next(monotonic_times),
    )
    monkeypatch.setattr(recovery_module.time, "sleep", sleeps.append)

    limiter = recovery_module._RecoveryCpuLimiter(0.4)
    limiter.checkpoint()

    assert sleeps == [pytest.approx(1.5)]


def test_clean_completed_recovery_is_byte_identical_on_repeat(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-a"
    descriptor = {
        "path": "/public/finalized-discovery.jsonl",
        "sha256": "c" * 64,
        "byte_count": 123,
        "line_count": 1,
    }
    revision = {
        "schema_version": "edge-lab-short-crypto-registry.v1",
        "snapshot_sha256": hashlib.sha256(b"registry").hexdigest(),
        "inputs": [descriptor],
        "targets": [_target()],
        "rejections": [],
        "completeness": {"status": "complete"},
        "scheduler_now_ms": 1_784_990_900_000,
    }
    manifest, _ = _finalize_events(
        run,
        [_service_event("registry_revision", revision)],
    )
    _write_close_checkpoint(run, manifest)

    first = recover_dynamic_short_crypto_runs(
        [run],
        settlement_timeout_ms=3_600_000,
    )
    second = recover_dynamic_short_crypto_runs(
        [run],
        settlement_timeout_ms=3_600_000,
    )

    assert first.recovered_from_run_ids == ("run-a",)
    assert first.replayed_record_count == 1
    assert first.run_classifications == (
        {
            "run_id": "run-a",
            "classification": "clean_completed",
            "conditions": ["clean_completed"],
        },
    )
    assert first.state["registry"]["snapshot_sha256"] == (
        revision["snapshot_sha256"]
    )
    assert first.state["processed_finalized_discovery_descriptors"] == [
        descriptor
    ]
    assert len(first.state_hash) == 64
    assert first.canonical_bytes() == second.canonical_bytes()


@pytest.mark.parametrize("first_format", ["v1", "v2"])
@pytest.mark.parametrize("second_format", ["v1", "v2"])
def test_replays_compact_registry_deltas_to_exact_cumulative_snapshot(
    tmp_path: Path,
    first_format: str,
    second_format: str,
) -> None:
    run = tmp_path / "runs" / "run-registry-v2"
    first_target = {
        **_target(),
        "provenance": [
            {
                "record_id": "b" * 64,
                "path": "/capture/a.jsonl",
                "line": 1,
            }
        ],
    }
    second_target = {
        **_target(),
        "provenance": [
            {
                "record_id": "c" * 64,
                "path": "/capture/b.jsonl",
                "line": 1,
            }
        ],
    }
    first_delta = _registry_snapshot(
        descriptor={
            "path": "/capture/a.jsonl",
            "sha256": "1" * 64,
            "byte_count": 10,
            "line_count": 1,
        },
        target=first_target,
    )
    second_delta = _registry_snapshot(
        descriptor={
            "path": "/capture/b.jsonl",
            "sha256": "2" * 64,
            "byte_count": 11,
            "line_count": 1,
        },
        target=second_target,
    )
    cumulative = merge_short_crypto_registry_snapshots(
        first_delta,
        second_delta,
    )
    first_revision = (
        first_delta
        if first_format == "v1"
        else {
            "schema_version": (
                "edge-lab-short-crypto-registry-revision.v2"
            ),
            "delta_snapshot": first_delta,
            "cumulative_snapshot_sha256": first_delta[
                "snapshot_sha256"
            ],
            "cumulative_input_file_count": 1,
            "cumulative_target_count": 1,
        }
    )
    second_revision = (
        cumulative
        if second_format == "v1"
        else {
            "schema_version": (
                "edge-lab-short-crypto-registry-revision.v2"
            ),
            "delta_snapshot": second_delta,
            "cumulative_snapshot_sha256": cumulative[
                "snapshot_sha256"
            ],
            "cumulative_input_file_count": 2,
            "cumulative_target_count": 1,
        }
    )
    events = [
        _service_event(
            "registry_revision",
            first_revision,
            monotonic_ns=1,
        ),
        _service_event(
            "registry_revision",
            second_revision,
            monotonic_ns=2,
        ),
    ]
    manifest, revision_ids = _finalize_events(run, events)
    _write_close_checkpoint(run, manifest)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.state["registry"] == {
        "snapshot_sha256": cumulative["snapshot_sha256"],
        "revision_record_ids": list(revision_ids),
        "snapshot": cumulative,
    }
    assert result.state[
        "processed_finalized_discovery_descriptors"
    ] == cumulative["inputs"]
    assert result.state["targets"][_target()["slug"]]["identity"] == (
        cumulative["targets"][0]
    )


def test_crash_orphan_partial_is_reported_but_never_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-crashed"
    revision = {
        "schema_version": "edge-lab-short-crypto-registry.v1",
        "snapshot_sha256": "d" * 64,
        "inputs": [],
        "targets": [_target()],
        "rejections": [],
        "completeness": {"status": "complete"},
        "scheduler_now_ms": 1_784_990_900_000,
    }
    _finalize_events(
        run,
        [_service_event("registry_revision", revision)],
    )
    partial = (
        run
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
        / "crash.jsonl.partial"
    )
    partial.write_bytes(b"secret-unfinalized-content-must-not-be-read")
    real_open: Callable[..., Any] = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name.endswith(".jsonl.partial"):
            raise AssertionError("recovery attempted to read partial content")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.run_classifications == (
        {
            "run_id": "run-crashed",
            "classification": "crash_with_orphan_partial",
            "conditions": [
                "checkpoint_missing",
                "orphan_partial",
            ],
        },
    )
    assert result.exclusions == (
        {
            "code": "orphan_partial_ignored",
            "run_id": "run-crashed",
            "path": (
                "control/raw/dynamic_short_crypto_service/"
                "crash.jsonl.partial"
            ),
            "recovery": "earlier_finalized_state_or_public_reacquisition",
        },
    )
    target = result.state["targets"][_target()["slug"]]
    assert target["state"] == "reacquire_required"
    assert target["exclusion_reason"] == "orphan_partial_not_recovered"


def test_finalized_decision_without_receipt_returns_blocked_worker_plan(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-decision-only"
    decision = _capture_decision()
    manifest, record_ids = _finalize_events(
        run,
        [
            _service_event(
                "capture_decision",
                decision,
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(
        run,
        manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": record_ids[0],
            "reason": "capture_decision_durable_before_apply",
            "updated_at": RECEIVED_AT,
        },
    )

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.run_classifications == (
        {
            "run_id": "run-decision-only",
            "classification": "incomplete_decision_without_receipt",
            "conditions": [
                "clean_completed",
                "incomplete_decision_without_receipt",
            ],
        },
    )
    assert result.gaps == (
        {
            "code": "decision_without_receipt",
            "run_id": "run-decision-only",
            "decision_id": decision["decision_id"],
            "group_id": decision["group_id"],
        },
    )
    assert result.state["subscription_decisions"] == {
        decision["decision_id"]: decision
    }
    assert result.recovery_decision[
        "must_be_durable_before_worker_actions"
    ]
    assert result.worker_actions == (
        {
            "action": "start_public_worker_after_revalidation",
            "reason": "decision_without_receipt",
            "decision_id": decision["decision_id"],
            "group_id": decision["group_id"],
            "asset_ids": decision["asset_ids"],
            "target_slugs": [_target()["slug"]],
            "generation": 8,
            "blocked_until_recovery_decision_id": (
                result.recovery_decision["decision_id"]
            ),
        },
    )
    assert result.callback_generation == 8
    assert not result.allows_callback(7)
    assert result.allows_callback(8)


def test_recovery_accepts_service_timestamp_on_finalized_capture_decision(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-service-decision"
    decision = _capture_decision()
    persisted_decision = {
        **decision,
        "scheduler_now_ms": decision["emitted_at_ms"],
    }
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "capture_decision",
                persisted_decision,
                monotonic_ns=2,
            )
        ],
    )
    _write_close_checkpoint(run, manifest)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.state["subscription_decisions"] == {
        decision["decision_id"]: decision
    }


def test_start_receipt_without_terminal_worker_requires_new_generation(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-receipt-only"
    decision = _capture_decision(sequence=3)
    receipt = {
        "decision_id": decision["decision_id"],
        "group_id": decision["group_id"],
        "asset_ids": decision["asset_ids"],
        "target_slugs": [_target()["slug"]],
        "scheduler_now_ms": 1_784_990_941_000,
    }
    manifest, record_ids = _finalize_events(
        run,
        [
            _service_event(
                "capture_decision",
                decision,
                monotonic_ns=2,
            ),
            _service_event(
                "worker_start_applied",
                receipt,
                monotonic_ns=3,
            ),
        ],
    )
    _write_close_checkpoint(
        run,
        manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": record_ids[1],
            "reason": "worker_start_receipt",
            "updated_at": RECEIVED_AT,
        },
    )

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.run_classifications == (
        {
            "run_id": "run-receipt-only",
            "classification": "receipt_without_recoverable_worker",
            "conditions": [
                "clean_completed",
                "receipt_without_recoverable_worker",
            ],
        },
    )
    assert result.gaps == (
        {
            "code": "receipt_without_recoverable_worker",
            "run_id": "run-receipt-only",
            "decision_id": decision["decision_id"],
            "group_id": decision["group_id"],
        },
    )
    assert result.state["worker_receipts"][
        decision["decision_id"]
    ]["start_record_id"] == record_ids[1]
    assert result.worker_actions[0]["reason"] == (
        "receipt_without_recoverable_worker"
    )
    assert result.worker_actions[0]["generation"] == 4
    assert result.worker_actions[0][
        "blocked_until_recovery_decision_id"
    ] == result.recovery_decision["decision_id"]


def test_clean_interrupted_run_restores_exclusion_and_no_worker_action(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-interrupted"
    target = _target()
    decision = _capture_decision(sequence=2)
    revision = {
        "schema_version": "edge-lab-short-crypto-registry.v1",
        "snapshot_sha256": "e" * 64,
        "inputs": [],
        "targets": [target],
        "rejections": [],
        "completeness": {"status": "complete"},
        "scheduler_now_ms": 1_784_990_900_000,
    }
    manifest, _ = _finalize_events(
        run,
        [
            _service_event(
                "registry_revision",
                revision,
                monotonic_ns=1,
            ),
            _service_event(
                "capture_decision",
                decision,
                monotonic_ns=2,
            ),
            _service_event(
                "worker_start_applied",
                {
                    "decision_id": decision["decision_id"],
                    "group_id": decision["group_id"],
                    "asset_ids": decision["asset_ids"],
                    "target_slugs": [target["slug"]],
                    "scheduler_now_ms": 1_784_990_941_000,
                },
                monotonic_ns=3,
            ),
            _service_event(
                "target_capture_interrupted",
                {
                    "group_id": decision["group_id"],
                    "slug": target["slug"],
                    "reason": "capture_interrupted_during_market",
                    "announcement_record_id": (
                        target["announcement_record_id"]
                    ),
                    "scheduler_now_ms": 1_784_991_100_000,
                },
                monotonic_ns=4,
            ),
            _service_event(
                "worker_stopped",
                {
                    "group_id": decision["group_id"],
                    "manifest_count": 3,
                    "acknowledged": False,
                    "decision_id": None,
                    "scheduler_now_ms": 1_784_991_100_001,
                },
                monotonic_ns=5,
            ),
        ],
    )
    _write_close_checkpoint(run, manifest)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.run_classifications == (
        {
            "run_id": "run-interrupted",
            "classification": "clean_interrupted",
            "conditions": ["clean_completed", "clean_interrupted"],
        },
    )
    recovered = result.state["targets"][target["slug"]]
    assert recovered["state"] == "excluded"
    assert recovered["exclusion_reason"] == (
        "capture_interrupted_during_market"
    )
    assert len(recovered["exclusion_record_id"]) == 64
    assert result.gaps == ()
    assert result.worker_actions == ()


def test_rebuilds_evidence_liveness_settlement_and_pending_deadline(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-evidence"
    target = _target()
    pending = {
        **_target("eth-updown-5m-1784991000"),
        "market_id": "3081519",
        "condition_id": "0x" + "cd" * 32,
        "up_token_id": "3" * 76,
        "down_token_id": "4" * 76,
        "source_symbol": "eth/usd",
        "announcement_record_id": "c" * 64,
        "rule_hash": "d" * 64,
    }
    group_id = "short-crypto-" + "1" * 24
    _, gamma_ids = _finalize_events(
        run,
        [
            {
                "schema_version": "edge-lab-public-http.v1",
                "source": "gamma_http",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {"market_id": target["market_id"]},
            }
        ],
        source="gamma_http",
        batch_id="gamma-a",
    )
    _, chainlink_ids = _finalize_events(
        run,
        [
            {
                "schema_version": "edge-lab-rtds-chainlink.v1",
                "source": "rtds_ws",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp_ms": target["opens_at_ms"],
                },
            },
            {
                "schema_version": "edge-lab-rtds-chainlink.v1",
                "source": "rtds_ws",
                "received_at": RECEIVED_AT,
                "event_at": None,
                "sequence": None,
                "payload": {
                    "symbol": "btc/usd",
                    "timestamp_ms": target["closes_at_ms"],
                },
            },
        ],
        source="rtds_ws",
        batch_id="rtds-a",
    )
    _, liveness_ids = _finalize_events(
        run,
        [
            _transport_event(
                "heartbeat_ack",
                connection_id="connection-a",
                monotonic_ns=10,
            ),
            _market_event(
                connection_id="connection-a",
                monotonic_ns=11,
            ),
            _transport_event(
                "disconnected",
                connection_id="connection-a",
                monotonic_ns=12,
            ),
            _transport_event(
                "heartbeat_ack",
                connection_id="connection-a",
                monotonic_ns=13,
            ),
        ],
        source="clob_market_ws",
        batch_id="clob-a",
        store_relative=f"groups/{group_id}",
    )
    decision = _capture_decision(sequence=4, group_id=group_id)
    revision = {
        "schema_version": "edge-lab-short-crypto-registry.v1",
        "snapshot_sha256": "f" * 64,
        "inputs": [],
        "targets": [target, pending],
        "rejections": [],
        "completeness": {"status": "complete"},
        "scheduler_now_ms": 1_784_990_900_000,
    }
    control_events = [
        _service_event("registry_revision", revision, monotonic_ns=1),
        _service_event(
            "gamma_target_verified",
            {
                "slug": target["slug"],
                "market_id": target["market_id"],
                "gamma_record_id": gamma_ids[0],
                "gamma_body_sha256": "9" * 64,
                "active": True,
                "closed": False,
                "accepting_orders": True,
                "scheduler_now_ms": 1_784_990_901_000,
            },
            monotonic_ns=2,
        ),
        _service_event(
            "capture_decision",
            decision,
            monotonic_ns=3,
        ),
        _service_event(
            "worker_start_applied",
            {
                "decision_id": decision["decision_id"],
                "group_id": group_id,
                "asset_ids": decision["asset_ids"],
                "target_slugs": [target["slug"]],
                "scheduler_now_ms": 1_784_990_941_000,
            },
            monotonic_ns=4,
        ),
        _service_event(
            "chainlink_boundary_evidence_committed",
            {
                "slug": target["slug"],
                "boundary_role": "open",
                "evidence_record_id": chainlink_ids[0],
                "scheduler_now_ms": 1_784_991_000_000,
            },
            monotonic_ns=5,
        ),
        _service_event(
            "chainlink_boundary_evidence_committed",
            {
                "slug": target["slug"],
                "boundary_role": "close",
                "evidence_record_id": chainlink_ids[1],
                "scheduler_now_ms": 1_784_991_300_000,
            },
            monotonic_ns=6,
        ),
        _service_event(
            "settlement_reconciled",
            {
                "slug": target["slug"],
                "status": "resolved",
                "action": "capture_settled",
                "reason_codes": [],
                "gamma_record_ids": [gamma_ids[0]],
                "evidence_record_id": gamma_ids[0],
                "outcome": "up",
                "available_at_ms": 1_784_991_301_000,
                "chainlink_boundary_status": "verified",
                "close_liveness": {
                    "connection_id": "connection-a",
                    "before_record_id": liveness_ids[0],
                    "after_record_id": liveness_ids[3],
                },
                "exclusion_reason": None,
                "scheduler_now_ms": 1_784_991_301_000,
            },
            monotonic_ns=7,
        ),
        _service_event(
            "worker_stopped",
            {
                "group_id": group_id,
                "manifest_count": 3,
                "acknowledged": False,
                "decision_id": None,
                "scheduler_now_ms": 1_784_991_302_000,
            },
            monotonic_ns=8,
        ),
    ]
    manifest, _ = _finalize_events(run, control_events)
    _write_close_checkpoint(run, manifest)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    recovered = result.state["targets"][target["slug"]]
    assert recovered["state"] == "settled"
    assert recovered["settlement"]["outcome"] == "up"
    assert recovered["pending_settlement_deadline_ms"] is None
    assert result.state["targets"][pending["slug"]][
        "pending_settlement_deadline_ms"
    ] == pending["closes_at_ms"] + 3_600_000
    assert result.state["gamma_evidence"][target["slug"]][0][
        "gamma_record_id"
    ] == gamma_ids[0]
    assert result.state["chainlink_evidence"][target["slug"]][
        "open"
    ][0]["evidence_record_id"] == chainlink_ids[0]
    assert result.state["chainlink_evidence"][target["slug"]][
        "close"
    ][0]["evidence_record_id"] == chainlink_ids[1]
    liveness = result.state["worker_liveness"][group_id]
    assert liveness["connection_ids"] == ["connection-a"]
    assert liveness["disconnect_record_ids"] == [liveness_ids[2]]
    assert liveness["lifecycle_record_ids"] == [
        liveness_ids[0],
        liveness_ids[2],
        liveness_ids[3],
    ]
    assert result.replayed_record_count == 15
    assert result.gaps == ()


def test_non_control_source_cannot_spoof_a_capture_decision(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-spoof"
    decision = _capture_decision(sequence=99)
    spoof = _service_event(
        "capture_decision",
        decision,
        monotonic_ns=99,
    )
    spoof["source"] = "dynamic_short_crypto_service"
    _finalize_events(
        run,
        [spoof],
        source="rtds_ws",
        batch_id="spoof-a",
    )

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.replayed_record_count == 1
    assert result.state["subscription_decisions"] == {}
    assert result.worker_actions == ()
    assert result.callback_generation == 1


def test_unpaired_and_symlink_artifacts_are_explicitly_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = tmp_path / "runs" / "run-integrity-gap"
    manifest, _ = _finalize_events(
        run,
        [_service_event("registry_revision", {
            "schema_version": "edge-lab-short-crypto-registry.v1",
            "snapshot_sha256": "1" * 64,
            "inputs": [],
            "targets": [_target()],
            "rejections": [],
            "completeness": {"status": "complete"},
            "scheduler_now_ms": 1_784_990_900_000,
        })],
    )
    _write_close_checkpoint(run, manifest)
    raw_dir = run / "control" / "raw" / "dynamic_short_crypto_service"
    finalized_raw = next(raw_dir.glob("batch-a.jsonl"))
    finalized_manifest = next(raw_dir.glob("batch-a.manifest.json"))
    (raw_dir / "unpaired.jsonl").write_bytes(
        b"unfinalized-raw-must-not-be-read"
    )
    (raw_dir / "manifest-only.manifest.json").write_text(
        "{}",
        encoding="utf-8",
    )
    linked_raw = raw_dir / "linked.jsonl"
    linked_manifest = raw_dir / "linked.manifest.json"
    linked_raw.symlink_to(finalized_raw)
    linked_manifest.symlink_to(finalized_manifest)
    real_open: Callable[..., Any] = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path.name in {
            "unpaired.jsonl",
            "manifest-only.manifest.json",
            "linked.jsonl",
            "linked.manifest.json",
        }:
            raise AssertionError("recovery opened an excluded artifact")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.run_classifications == (
        {
            "run_id": "run-integrity-gap",
            "classification": "finalized_input_integrity_gap",
            "conditions": [
                "clean_completed",
                "raw_without_manifest",
                "manifest_without_raw",
                "symlink_finalized_artifact",
            ],
        },
    )
    assert {gap["code"] for gap in result.gaps} == {
        "raw_without_manifest",
        "manifest_without_raw",
        "symlink_finalized_artifact",
    }
    assert {item["code"] for item in result.exclusions} == {
        "raw_without_manifest_excluded",
        "manifest_without_raw_excluded",
        "symlink_finalized_artifact_excluded",
    }
    recovered = result.state["targets"][_target()["slug"]]
    assert recovered["state"] == "reacquire_required"
    assert recovered["exclusion_reason"] == (
        "prior_run_finalized_input_integrity_gap"
    )


def test_dirty_checkpoint_startup_integrity_prevents_clean_recovery(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-dirty-startup"
    manifest, _ = _finalize_events(
        run,
        [_service_event("registry_revision", {
            "schema_version": "edge-lab-short-crypto-registry.v1",
            "snapshot_sha256": "2" * 64,
            "inputs": [],
            "targets": [_target()],
            "rejections": [],
            "completeness": {"status": "complete"},
            "scheduler_now_ms": 1_784_990_900_000,
        })],
    )
    _write_close_checkpoint(run, manifest)
    checkpoint_path = (
        run
        / "control"
        / "checkpoints"
        / "dynamic_short_crypto_service.json"
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["checkpoint"]["capture_runtime"]["startup_integrity"][
        "orphan_partials"
    ] = ["raw/dynamic_short_crypto_service/prior.jsonl.partial"]
    checkpoint_path.write_text(
        json.dumps(checkpoint, sort_keys=True),
        encoding="utf-8",
    )

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.run_classifications == (
        {
            "run_id": "run-dirty-startup",
            "classification": "finalized_input_integrity_gap",
            "conditions": [
                "clean_completed",
                "checkpoint_startup_integrity_dirty",
            ],
        },
    )
    assert result.gaps == (
        {
            "code": "checkpoint_startup_integrity_dirty",
            "run_id": "run-dirty-startup",
            "category": "orphan_partials",
            "finding": (
                "raw/dynamic_short_crypto_service/"
                "prior.jsonl.partial"
            ),
        },
    )
    assert result.state["targets"][_target()["slug"]]["state"] == (
        "reacquire_required"
    )


def test_manifest_schema_fingerprint_drift_is_rejected(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-schema-drift"
    _finalize_events(
        run,
        [_service_event("registry_revision", {
            "schema_version": "edge-lab-short-crypto-registry.v1",
            "snapshot_sha256": "3" * 64,
            "inputs": [],
            "targets": [],
            "rejections": [],
            "completeness": {"status": "complete"},
            "scheduler_now_ms": 1_784_990_900_000,
        })],
    )
    manifest_path = next(run.rglob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_fingerprints"][0]["fingerprint"] = "0" * 64
    manifest_path.chmod(0o644)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == "finalized_schema_mismatch"


def test_nonempty_array_schema_cannot_validate_an_empty_array(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-empty-array-schema-drift"
    _finalize_events(
        run,
        [
            {
                "schema_version": "clob-http.snapshot.v1",
                "values": ["observed"],
            }
        ],
        source="clob_http",
    )
    raw_path = next(run.rglob("*.jsonl"))
    row = json.loads(raw_path.read_bytes())
    row["payload"]["values"] = []
    row["record_id"] = canonical_record_id_from_json(row)
    raw_path.chmod(0o644)
    raw_path.write_bytes(canonical_json_bytes(row) + b"\n")
    _refresh_manifest_checksum(raw_path)

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == "finalized_schema_mismatch"


def test_recovery_accepts_finalized_decimal_payload_round_trip(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-decimal-payload"
    _finalize_events(
        run,
        [
            {
                "schema_version": "clob-http.snapshot.v1",
                "decimal_value": Decimal("0.07"),
                "string_value": "0.07",
            }
        ],
        source="clob_http",
    )

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.replayed_record_count == 1


@pytest.mark.parametrize(
    ("drift", "expected_code"),
    [
        ("schema", "finalized_manifest_invalid"),
        ("sha256", "finalized_checksum_mismatch"),
        ("bytes", "finalized_checksum_mismatch"),
        ("lines", "finalized_checksum_mismatch"),
    ],
)
def test_manifest_schema_bytes_lines_and_sha_drift_are_rejected(
    tmp_path: Path,
    drift: str,
    expected_code: str,
) -> None:
    run = tmp_path / "runs" / f"run-{drift}"
    _finalize_events(
        run,
        [_service_event("registry_revision", {
            "schema_version": "edge-lab-short-crypto-registry.v1",
            "snapshot_sha256": "4" * 64,
            "inputs": [],
            "targets": [],
            "rejections": [],
            "completeness": {"status": "complete"},
            "scheduler_now_ms": 1_784_990_900_000,
        })],
    )
    manifest_path = next(run.rglob("*.manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if drift == "schema":
        manifest["schema_version"] = "edge-lab-recorder.raw.v2"
    elif drift == "sha256":
        manifest["checksum"]["value"] = "0" * 64
    elif drift == "bytes":
        manifest["checksum"]["bytes"] += 1
    else:
        manifest["checksum"]["lines"] += 1
    manifest_path.chmod(0o644)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == expected_code


def test_canonical_record_id_mismatch_is_rejected(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-record-id"
    _finalize_events(
        run,
        [_service_event("registry_revision", {
            "schema_version": "edge-lab-short-crypto-registry.v1",
            "snapshot_sha256": "5" * 64,
            "inputs": [],
            "targets": [],
            "rejections": [],
            "completeness": {"status": "complete"},
            "scheduler_now_ms": 1_784_990_900_000,
        })],
    )
    raw_path = next(run.rglob("*.jsonl"))
    row = json.loads(raw_path.read_text(encoding="utf-8"))
    row["record_id"] = "0" * 64
    raw_path.chmod(0o644)
    raw_path.write_text(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _refresh_manifest_checksum(raw_path)

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == "canonical_record_id_mismatch"


def test_duplicate_record_id_across_finalized_batches_is_rejected(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-duplicate-record-id"
    event = _market_event(
        connection_id="connection-a",
        monotonic_ns=1,
    )
    _finalize_events(
        run,
        [event],
        source="clob_market_ws",
        batch_id="clob-a",
        store_relative="groups/group-a",
    )
    _finalize_events(
        run,
        [event],
        source="clob_market_ws",
        batch_id="clob-b",
        store_relative="groups/group-a",
    )

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == "duplicate_record_id"


def test_checkpoint_cannot_reference_a_non_finalized_record(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-checkpoint-drift"
    manifest, _ = _finalize_events(
        run,
        [_service_event("registry_revision", {
            "schema_version": "edge-lab-short-crypto-registry.v1",
            "snapshot_sha256": "6" * 64,
            "inputs": [],
            "targets": [],
            "rejections": [],
            "completeness": {"status": "complete"},
            "scheduler_now_ms": 1_784_990_900_000,
        })],
    )
    _write_close_checkpoint(
        run,
        manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": "0" * 64,
            "reason": "capture_decision_durable_before_apply",
            "updated_at": RECEIVED_AT,
        },
    )

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            run,
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == "checkpoint_record_not_finalized"


def test_checkpoint_cannot_reference_a_record_from_another_run(
    tmp_path: Path,
) -> None:
    first_run = tmp_path / "runs" / "run-checkpoint-source"
    _, first_record_ids = _finalize_events(
        first_run,
        [_service_event("worker_stopped", {
            "group_id": "group-a",
            "manifest_count": 0,
            "acknowledged": False,
            "decision_id": None,
            "scheduler_now_ms": 1_784_990_900_000,
        })],
    )
    second_run = tmp_path / "runs" / "run-checkpoint-target"
    second_manifest, _ = _finalize_events(
        second_run,
        [_service_event("worker_stopped", {
            "group_id": "group-b",
            "manifest_count": 0,
            "acknowledged": False,
            "decision_id": None,
            "scheduler_now_ms": 1_784_990_901_000,
        })],
    )
    _write_close_checkpoint(
        second_run,
        second_manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": first_record_ids[0],
            "reason": "capture_decision_durable_before_apply",
            "updated_at": RECEIVED_AT,
        },
    )

    with pytest.raises(RecoveryInputError) as caught:
        recover_dynamic_short_crypto_runs(
            [first_run, second_run],
            settlement_timeout_ms=3_600_000,
        )

    assert caught.value.code == "checkpoint_record_not_finalized"


def test_stubborn_worker_shutdown_is_not_turned_into_restart_action(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-stubborn"
    decision = _capture_decision(sequence=5)
    events = [
        _service_event(
            "capture_decision",
            decision,
            monotonic_ns=1,
        ),
        _service_event(
            "worker_start_applied",
            {
                "decision_id": decision["decision_id"],
                "group_id": decision["group_id"],
                "asset_ids": decision["asset_ids"],
                "target_slugs": [_target()["slug"]],
                "scheduler_now_ms": 1_784_990_941_000,
            },
            monotonic_ns=2,
        ),
        _service_event(
            "worker_stop_failed",
            {
                "group_id": decision["group_id"],
                "manifest_count": 0,
                "error_code": "stubborn_child",
                "error_type": "TimeoutError",
                "scheduler_now_ms": 1_784_991_100_000,
            },
            monotonic_ns=3,
        ),
    ]
    manifest, _ = _finalize_events(run, events)
    _write_close_checkpoint(run, manifest)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.run_classifications == (
        {
            "run_id": "run-stubborn",
            "classification": "stubborn_worker_shutdown_unconfirmed",
            "conditions": [
                "clean_completed",
                "stubborn_worker_shutdown_unconfirmed",
            ],
        },
    )
    assert result.gaps == (
        {
            "code": "stubborn_worker_shutdown_unconfirmed",
            "run_id": "run-stubborn",
            "decision_id": decision["decision_id"],
            "group_id": decision["group_id"],
        },
    )
    assert result.worker_actions == ()
    assert result.exclusions == (
        {
            "code": "stubborn_worker_shutdown_unconfirmed",
            "run_id": "run-stubborn",
            "group_id": decision["group_id"],
            "recovery": "manual_process_ownership_revalidation_required",
        },
    )


def test_gamma_only_resolved_settlement_is_recovered_as_excluded(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-gamma-only"
    target = _target()
    _, gamma_ids = _finalize_events(
        run,
        [{
            "schema_version": "edge-lab-public-http.v1",
            "source": "gamma_http",
            "received_at": RECEIVED_AT,
            "event_at": None,
            "sequence": None,
            "payload": {"market_id": target["market_id"]},
        }],
        source="gamma_http",
        batch_id="gamma-only",
    )
    revision = {
        "schema_version": "edge-lab-short-crypto-registry.v1",
        "snapshot_sha256": "7" * 64,
        "inputs": [],
        "targets": [target],
        "rejections": [],
        "completeness": {"status": "complete"},
        "scheduler_now_ms": 1_784_990_900_000,
    }
    events = [
        _service_event("registry_revision", revision, monotonic_ns=1),
        _service_event(
            "gamma_target_verified",
            {
                "slug": target["slug"],
                "market_id": target["market_id"],
                "gamma_record_id": gamma_ids[0],
                "gamma_body_sha256": "8" * 64,
                "active": False,
                "closed": True,
                "accepting_orders": False,
                "scheduler_now_ms": 1_784_991_301_000,
            },
            monotonic_ns=2,
        ),
        _service_event(
            "settlement_reconciled",
            {
                "slug": target["slug"],
                "status": "resolved",
                "gamma_record_ids": [gamma_ids[0]],
                "outcome": "up",
                "available_at_ms": 1_784_991_301_000,
                "chainlink_boundary_status": "missing_not_integrated",
                "close_liveness": None,
                "exclusion_reason": None,
                "scheduler_now_ms": 1_784_991_301_000,
            },
            monotonic_ns=3,
        ),
    ]
    manifest, settlement_record_ids = _finalize_events(run, events)
    _write_close_checkpoint(run, manifest)

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    recovered = result.state["targets"][target["slug"]]
    assert recovered["state"] == "excluded"
    assert recovered["exclusion_reason"] == (
        "settlement_not_recoverable_from_finalized_evidence"
    )
    assert result.gaps == (
        {
            "code": "settlement_evidence_gap",
            "run_id": "run-gamma-only",
            "slug": target["slug"],
            "settlement_record_id": settlement_record_ids[2],
        },
    )


def test_receipt_without_finalized_decision_cannot_construct_worker_scope(
    tmp_path: Path,
) -> None:
    run = tmp_path / "runs" / "run-orphan-receipt"
    manifest, record_ids = _finalize_events(
        run,
        [_service_event(
            "worker_start_applied",
            {
                "decision_id": "a" * 64,
                "group_id": "short-crypto-" + "2" * 24,
                "asset_ids": ["1" * 76, "2" * 76],
                "target_slugs": [_target()["slug"]],
                "scheduler_now_ms": 1_784_990_941_000,
            },
            monotonic_ns=1,
        )],
    )
    _write_close_checkpoint(
        run,
        manifest,
        recorder_checkpoint={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-control-checkpoint.v1"
            ),
            "record_id": record_ids[0],
            "reason": "worker_start_receipt",
            "updated_at": RECEIVED_AT,
        },
    )

    result = recover_dynamic_short_crypto_runs(
        run,
        settlement_timeout_ms=3_600_000,
    )

    assert result.gaps == (
        {
            "code": "receipt_without_decision",
            "run_id": "run-orphan-receipt",
            "decision_id": "a" * 64,
            "group_id": "short-crypto-" + "2" * 24,
        },
    )
    assert result.worker_actions == ()
    assert result.exclusions == (
        {
            "code": "receipt_without_decision",
            "run_id": "run-orphan-receipt",
            "group_id": "short-crypto-" + "2" * 24,
            "recovery": "scope_not_recoverable_from_finalized_decision",
        },
    )


if os.name == "nt":
    _requires_posix_descriptor_traversal = pytest.mark.skip(
        reason=(
            "requires POSIX dir_fd/O_NOFOLLOW traversal, directory fsync, "
            "or production-path performance characteristics"
        )
    )
    for _test_name in (
        "test_recovery_validates_repetitive_book_batches_with_bounded_overhead",
        "test_cached_recovery_binds_all_replay_reads_to_held_run_root_against_swap_back",
        "test_cached_recovery_rejects_replay_subtree_swap_after_bounded_walk",
        "test_cached_recovery_rejects_finalized_pair_added_after_held_view_walk",
        "test_cached_recovery_rejects_same_name_nonartifact_replaced_by_directory",
        "test_cached_recovery_rejects_finalized_pair_removed_after_held_view_walk",
        "test_cached_recovery_rechecks_replayed_membership_through_snapshot_commit",
        "test_anchor_discovery_binds_open_files_to_held_directory_entries",
        "test_anchor_discovery_closes_all_descriptors",
        "test_cached_recovery_bounds_and_closes_run_root_descriptors",
        "test_recovery_bounds_held_directory_descriptors_across_many_roots",
        "test_recovery_bounds_run_root_proofs_across_many_roots",
        "test_recovery_bounds_held_directory_descriptors_within_single_run",
        "test_run_roots_closes_held_descriptors_when_later_root_is_invalid",
        "test_record_index_directory_fsync_failure_never_commits_snapshot",
    ):
        globals()[_test_name] = _requires_posix_descriptor_traversal(
            globals()[_test_name]
        )
