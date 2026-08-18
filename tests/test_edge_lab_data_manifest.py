"""Behavioral tests for deterministic Edge Lab data-audit artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

import src.edge_lab.data_manifest as data_manifest_module
from src.edge_lab.data_manifest import (
    DataAuditBundle,
    build_data_audit,
    write_data_audit,
)
from src.edge_lab.data_store import CaptureStore


requires_posix_descriptor_reads = pytest.mark.skipif(
    not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY"))
    or os.open not in os.supports_dir_fd,
    reason="requires POSIX descriptor-bound no-follow traversal",
)


def _append_forward_book(
    writer: object,
    *,
    received_at: str,
    event_at: str | None = None,
    sequence: int,
    price: str,
    server_timestamp: str | None = None,
) -> None:
    frame_hash = hashlib.sha256(
        f"clob-frame-{sequence}".encode("utf-8")
    ).hexdigest()
    writer.append(
        received_at=received_at,
        event_at=event_at or received_at,
        sequence=sequence,
        payload={
            "session_id": "test-session",
            "connection_id": "test-clob-connection",
            "frame_index": sequence,
            "frame_hash": frame_hash,
            "monotonic_ns": 1_000_000 + sequence,
            "server_timestamp": server_timestamp or str(sequence),
            "kind": "data",
            "event_type": "book",
            "payload": {
                "event_type": "book",
                "asset_id": "yes-token",
                "market": "condition",
                "timestamp": "1784880000000",
                "bids": [{"price": Decimal(price), "size": Decimal("10")}],
                "asks": [{"price": Decimal("0.43"), "size": Decimal("12")}],
            },
        },
    )


def _rtds_update_payload(
    price: str = "118000.10",
    *,
    symbol: str = "btcusdt",
    frame_index: int = 0,
) -> dict[str, object]:
    return {
        "session_id": "test-session",
        "connection_id": "test-rtds-connection",
        "frame_index": frame_index,
        "frame_hash": hashlib.sha256(
            f"rtds-{symbol}-{frame_index}".encode("utf-8")
        ).hexdigest(),
        "monotonic_ns": 2_000_000 + frame_index,
        "server_timestamp": 1784880000000,
        "kind": "data",
        "event_type": "crypto_prices.update",
        "payload": {
            "topic": "crypto_prices",
            "type": "update",
            "timestamp": 1784880000000,
            "payload": {
                "symbol": symbol,
                "timestamp": 1784880000000,
                "value": Decimal(price),
                "full_accuracy_value": price.replace(".", ""),
            },
        },
    }


def _make_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "project"
    capture_root = project_root / "data/edge_discovery_2026-07-24"
    legacy_root = project_root / "research/profit_redesign_2026-07-24"
    capture_root.mkdir(parents=True)
    legacy_root.mkdir(parents=True)
    return project_root, capture_root, legacy_root


def test_audit_is_deterministic_and_reports_forward_and_legacy_levels(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="clob_market_ws",
        batch_id="forward-books",
        schema_version="edge-lab-recorder.raw.v1",
    )
    _append_forward_book(
        writer,
        received_at="2026-07-24T08:00:00Z",
        sequence=1,
        price="0.42",
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")

    legacy_snapshot = legacy_root / "singapore_28_current.json"
    legacy_snapshot.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-24T07:00:00Z",
                "book": {"yes_best_bid": 0.42},
                "candidate": {"condition_id": "legacy-condition"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    legacy_observations = legacy_root / "singapore_28_observe.jsonl"
    legacy_observations.write_text(
        "\n".join(
            [
                '{"observed_at":"2026-07-24T07:00:00Z","ok":true}',
                '{"observed_at":"2026-07-24T07:00:10Z","ok":true}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    legacy_robustness = legacy_root / "singapore_robust_stress5.json"
    legacy_robustness.write_text(
        (
            '{"condition_id":"legacy-condition",'
            '"samples":[{"timestamp_ms":1784866223623}],'
            '"verdict":{"status":"research_only"}}\n'
        ),
        encoding="utf-8",
    )
    legacy_replay = legacy_root / "singapore_28_replay_12h.json"
    legacy_replay.write_text(
        (
            '{"condition_id":"legacy-condition",'
            '"samples":[{"timestamp_ms":1784866223623}],'
            '"verdict":{"status":"research_only"}}\n'
        ),
        encoding="utf-8",
    )

    first = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    second = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    assert isinstance(first, DataAuditBundle)
    assert first == second
    batch = first.manifest["capture"]["finalized_batches"][0]
    assert batch["evidence_level"] == "L2"
    assert batch["forward_capture"] is True
    assert batch["content"]["kind_counts"] == {"data": 1}
    assert batch["content"]["event_type_counts"] == {"book": 1}
    assert batch["checksum"]["verified"] is True

    legacy_by_name = {
        Path(item["path"]).name: item
        for item in first.manifest["legacy_artifacts"]
    }
    assert legacy_by_name["singapore_28_current.json"]["evidence_level"] == "L1"
    assert (
        legacy_by_name["singapore_28_observe.jsonl"]["evidence_level"] == "L1"
    )
    assert legacy_by_name["singapore_robust_stress5.json"]["evidence_level"] == "L1"
    assert all(
        item["evidence_level"] != "L2"
        for item in first.manifest["legacy_artifacts"]
    )
    assert first.quality["levels"]["highest_observed"] == "L2"
    assert first.quality["levels"]["legacy_maximum"] == "L1"
    assert first.quality["checksums"]["mismatch_count"] == 0
    assert first.manifest["totals"]["legacy_embedded_sample_count"] == 2
    assert first.manifest["totals"]["legacy_jsonl_record_count"] == 2
    assert first.manifest["totals"]["legacy_unique_condition_count"] == 1
    assert (
        first.manifest["totals"]["legacy_unique_sample_observation_key_count"]
        == 1
    )
    assert first.manifest["totals"]["legacy_reused_sample_row_count"] == 1
    assert first.quality["duplicates"]["legacy_reused_sample_row_count"] == 1
    assert legacy_by_name["singapore_28_current.json"]["artifact_kind"] == (
        "current_public_snapshot"
    )
    assert legacy_by_name["singapore_robust_stress5.json"]["artifact_kind"] == (
        "robustness_scenario"
    )
    assert (
        first.manifest["data_manifest_hash"]
        == first.quality["data_manifest_hash"]
    )


def test_lifecycle_only_and_partial_streams_are_not_promoted_to_l2(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    store = CaptureStore(capture_root)
    lifecycle = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="failed-connect",
        schema_version="edge-lab-recorder.raw.v1",
    )
    lifecycle.append(
        received_at="2026-07-24T08:00:00Z",
        event_at=None,
        sequence=None,
        payload={
            "kind": "lifecycle",
            "event_type": "disconnected",
            "detail": "public socket closed",
        },
    )
    lifecycle.finalize(finalized_at="2026-07-24T08:00:01Z")

    partial = store.raw_dir / "rtds_ws/live.jsonl.partial"
    partial.parent.mkdir(parents=True)
    partial.write_text(
        json.dumps(
            {
                "schema_version": "edge-lab-recorder.raw.v1",
                "source": "rtds_ws",
                "received_at": "2026-07-24T08:00:00Z",
                "event_at": "2026-07-24T07:59:59Z",
                "sequence": None,
                "payload": {
                    "kind": "data",
                    "event_type": "crypto_prices",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    batch = bundle.manifest["capture"]["finalized_batches"][0]
    assert batch["evidence_level"] == "L0"
    assert batch["content"]["domain_record_count"] == 0
    partial_entry = bundle.manifest["capture"]["partial_files"][0]
    assert partial_entry["evidence_level"] == "L0"
    assert partial_entry["potential_evidence_level_after_finalize"] == "L0"
    assert "_time_observations" not in partial_entry["content"]
    assert bundle.quality["levels"]["highest_observed"] == "L0"
    assert bundle.quality["levels"]["forward_l2_sources"] == []
    gap_codes = {item["code"] for item in bundle.quality["gaps"]}
    assert "finalized_batch_has_no_domain_records" in gap_codes
    assert "unfinalized_partial_excluded" in gap_codes


@requires_posix_descriptor_reads
def test_live_audit_records_partial_rotation_without_promoting_or_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    partial = (
        capture_root
        / "raw/clob_market_ws/live-batch.jsonl.partial"
    )
    partial.parent.mkdir(parents=True)
    partial.write_text('{"unfinished":true}\n', encoding="utf-8")
    original_open = data_manifest_module.os.open
    partial_open_count = 0

    def rotate_before_second_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal partial_open_count
        if path == partial.name and dir_fd is not None:
            partial_open_count += 1
            if partial_open_count == 2:
                partial.unlink()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        data_manifest_module.os,
        "open",
        rotate_before_second_open,
    )

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    relative_partial = (
        "data/edge_discovery_2026-07-24/raw/"
        "clob_market_ws/live-batch.jsonl.partial"
    )
    assert partial_open_count == 2
    assert bundle.manifest["capture"]["partial_files"] == []
    assert bundle.manifest["capture"]["unsafe_input_paths"] == []
    assert bundle.manifest["capture"]["partial_rotation_advisories"] == [
        {
            "path": relative_partial,
            "reason": (
                "mutable partial disappeared during audit; "
                "possible recorder rotation"
            ),
            "evidence_level": "L0",
            "excluded_from_empirical_evidence": True,
        }
    ]
    assert bundle.manifest["totals"]["partial_file_count"] == 0
    assert bundle.manifest["totals"]["partial_rotation_advisory_count"] == 1
    assert not bundle.manifest["capture"]["sources"]
    assert any(
        gap == {
            "code": "mutable_partial_rotated_during_audit",
            "severity": "info",
            "scope": relative_partial,
            "detail": (
                "mutable partial disappeared during audit; "
                "possible recorder rotation"
            ),
        }
        for gap in bundle.quality["gaps"]
    )


@requires_posix_descriptor_reads
def test_live_audit_treats_partial_missing_at_validation_as_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    partial = capture_root / "raw/rtds_ws/rotated.jsonl.partial"
    partial.parent.mkdir(parents=True)
    partial.write_text('{"unfinished":true}\n', encoding="utf-8")
    original_open = data_manifest_module.os.open
    rotated = False

    def rotate_before_validation_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal rotated
        if path == partial.name and dir_fd is not None and not rotated:
            partial.unlink()
            rotated = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        data_manifest_module.os,
        "open",
        rotate_before_validation_open,
    )

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    assert rotated
    assert bundle.manifest["capture"]["partial_files"] == []
    assert bundle.manifest["capture"]["unsafe_input_paths"] == []
    assert bundle.manifest["totals"]["unsafe_input_path_count"] == 0
    assert bundle.manifest["totals"]["partial_rotation_advisory_count"] == 1
    assert {
        gap["code"] for gap in bundle.quality["gaps"]
    } >= {"mutable_partial_rotated_during_audit"}
    assert not any(
        gap["code"] == "unsafe_input_path"
        for gap in bundle.quality["gaps"]
    )


@pytest.mark.parametrize(
    ("input_kind", "delete_on_open"),
    [
        ("manifest", 2),
        ("finalized_raw", 3),
        ("checkpoint", 2),
        ("legacy", 2),
    ],
)
@requires_posix_descriptor_reads
def test_audit_still_fails_closed_if_immutable_input_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_kind: str,
    delete_on_open: int,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    if input_kind in {"manifest", "finalized_raw"}:
        writer = CaptureStore(capture_root).open_raw_batch(
            source="clob_market_ws",
            batch_id="immutable",
            schema_version="edge-lab-recorder.raw.v1",
        )
        _append_forward_book(
            writer,
            received_at="2026-07-24T08:00:00Z",
            sequence=1,
            price="0.42",
        )
        writer.finalize(
            finalized_at="2026-07-24T08:01:00Z"
        )
        target = (
            writer.manifest_path
            if input_kind == "manifest"
            else writer.path
        )
    elif input_kind == "checkpoint":
        target = capture_root / "checkpoints/rtds_ws.json"
        target.parent.mkdir(parents=True)
        target.write_text(
            '{"schema_version":"capture-checkpoint.v1"}\n',
            encoding="utf-8",
        )
    else:
        target = legacy_root / "immutable-history.json"
        target.write_text('{"observed_at":"2026-07-24T08:00:00Z"}\n')

    original_open = data_manifest_module.os.open
    target_open_count = 0

    def remove_before_audit_read(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal target_open_count
        if path == target.name and dir_fd is not None:
            target_open_count += 1
            if target_open_count == delete_on_open:
                target.unlink()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        data_manifest_module.os,
        "open",
        remove_before_audit_read,
    )

    with pytest.raises(
        data_manifest_module.UnsafeInputPathError,
        match="input disappeared during audit",
    ):
        build_data_audit(
            project_root=project_root,
            capture_root=capture_root,
            legacy_root=legacy_root,
        )
    assert target_open_count == delete_on_open


def test_forward_clob_http_requires_an_actual_book_response_for_l2(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    store = CaptureStore(capture_root)
    metadata_only = store.open_raw_batch(
        source="clob_http",
        batch_id="metadata",
        schema_version="edge-lab-recorder.raw.v1",
    )
    metadata_only.append(
        received_at="2026-07-24T08:00:00Z",
        event_at=None,
        sequence=None,
        payload={
            "kind": "snapshot",
            "event_type": "clob_snapshot",
            "payload": {
                "snapshot_kind": "clob",
                "responses": [{"resource": "clob_market", "raw_json": "{}"}],
            },
        },
    )
    metadata_only.finalize(finalized_at="2026-07-24T08:00:01Z")
    raw_book = {
        "asset_id": "yes-token",
        "market": "condition",
        "timestamp": "1784880000000",
        "bids": [{"price": "0.42", "size": "10"}],
        "asks": [{"price": "0.43", "size": "12"}],
    }
    body_utf8 = json.dumps(
        raw_book, sort_keys=True, separators=(",", ":")
    )
    with_book = store.open_raw_batch(
        source="clob_http",
        batch_id="book",
        schema_version="edge-lab-recorder.raw.v1",
    )
    with_book.append(
        received_at="2026-07-24T08:00:02Z",
        event_at=None,
        sequence=None,
        payload={
            "kind": "snapshot",
            "event_type": "clob_snapshot",
            "payload": {
                "snapshot_kind": "clob",
                "responses": [
                    {
                        "resource": "clob_book",
                        "request_key": "yes-token",
                        "raw_json": raw_book,
                        "provenance": {
                            "method": "GET",
                            "status_code": 200,
                            "source": "clob_book",
                            "url": "https://clob.polymarket.com/book",
                            "request_params": {"token_id": "yes-token"},
                            "body_utf8": body_utf8,
                            "body_bytes": len(body_utf8.encode("utf-8")),
                            "body_sha256": hashlib.sha256(
                                body_utf8.encode("utf-8")
                            ).hexdigest(),
                        },
                    }
                ],
            },
        },
    )
    with_book.finalize(finalized_at="2026-07-24T08:00:03Z")
    bad_provenance = store.open_raw_batch(
        source="clob_http",
        batch_id="bad-provenance",
        schema_version="edge-lab-recorder.raw.v1",
    )
    bad_provenance.append(
        received_at="2026-07-24T08:00:04Z",
        event_at=None,
        sequence=None,
        payload={
            "kind": "snapshot",
            "event_type": "clob_snapshot",
            "payload": {
                "snapshot_kind": "clob",
                "responses": [
                    {
                        "resource": "clob_book",
                        "request_key": "yes-token",
                        "raw_json": raw_book,
                        "provenance": {
                            "method": "GET",
                            "status_code": 200,
                            "source": "clob_book",
                            "url": "https://clob.polymarket.com/book",
                            "request_params": {"token_id": "yes-token"},
                            "body_utf8": body_utf8,
                            "body_bytes": len(body_utf8.encode("utf-8")),
                            "body_sha256": "0" * 64,
                        },
                    }
                ],
            },
        },
    )
    bad_provenance.finalize(finalized_at="2026-07-24T08:00:05Z")

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    batches = {
        item["batch_id"]: item
        for item in bundle.manifest["capture"]["finalized_batches"]
    }

    assert batches["metadata"]["evidence_level"] == "L1"
    assert batches["book"]["evidence_level"] == "L2"
    assert batches["bad-provenance"]["evidence_level"] == "L1"
    assert (
        batches["bad-provenance"]["content"]["invalid_l2_candidate_count"]
        == 1
    )
    assert batches["book"]["content"]["resource_counts"] == {"clob_book": 1}


def test_tampered_capture_envelope_cannot_be_promoted_by_rehashing_manifest(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="clob_market_ws",
        batch_id="tampered-envelope",
        schema_version="edge-lab-recorder.raw.v1",
    )
    _append_forward_book(
        writer,
        received_at="2026-07-24T08:00:00Z",
        sequence=1,
        price="0.42",
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    row = json.loads(writer.path.read_text(encoding="utf-8"))
    row["source"] = "gamma_http"
    row.pop("received_at")
    raw = (
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    writer.path.chmod(0o644)
    writer.path.write_bytes(raw)
    manifest = json.loads(writer.manifest_path.read_text(encoding="utf-8"))
    manifest["checksum"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "lines": 1,
    }
    writer.manifest_path.chmod(0o644)
    writer.manifest_path.write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    batch = bundle.manifest["capture"]["finalized_batches"][0]

    assert batch["checksum"]["verified"] is True
    assert batch["integrity_valid"] is False
    assert batch["evidence_level"] == "L0"
    assert batch["content"]["capture_envelope_error_count"] == 1
    assert any(
        error.startswith("raw_capture_envelope_invalid")
        for error in batch["integrity_errors"]
    )


def test_manifest_gap_counts_are_recomputed_from_raw(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="rtds_ws",
        batch_id="lying-gap-count",
        schema_version="edge-lab-recorder.raw.v1",
    )
    for sequence, timestamp in ((1, "2026-07-24T08:00:00Z"), (4, "2026-07-24T08:00:01Z")):
        writer.append(
            received_at=timestamp,
            event_at=timestamp,
            sequence=sequence,
            payload={
                "kind": "data",
                "event_type": "crypto_prices.update",
                "payload": {
                    "topic": "crypto_prices",
                    "type": "update",
                    "timestamp": 1784880000000 + sequence,
                    "payload": {
                        "symbol": "btcusdt",
                        "timestamp": 1784880000000 + sequence,
                        "value": Decimal("118000.10"),
                        "full_accuracy_value": "11800010000000",
                    },
                },
            },
        )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    manifest = json.loads(writer.manifest_path.read_text(encoding="utf-8"))
    manifest["sequence_gap_count"] = 0
    manifest["sequence_missing_count"] = 0
    manifest["sequence_gaps"] = []
    writer.manifest_path.chmod(0o644)
    writer.manifest_path.write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    batch = bundle.manifest["capture"]["finalized_batches"][0]

    assert batch["integrity_valid"] is False
    assert batch["evidence_level"] == "L0"
    assert batch["content"]["sequence_gap_count_recomputed"] == 1
    assert batch["content"]["sequence_missing_count_recomputed"] == 2
    assert "manifest_sequence_gap_count_mismatch" in batch["integrity_errors"]
    assert bundle.quality["sequence"]["gap_count"] == 1
    assert bundle.quality["sequence"]["missing_count"] == 2


def test_invalid_manifest_types_and_checksum_metadata_fail_closed(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="clob_market_ws",
        batch_id="bad-metadata",
        schema_version="edge-lab-recorder.raw.v1",
    )
    _append_forward_book(
        writer,
        received_at="2026-07-24T08:00:00Z",
        sequence=1,
        price="0.42",
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    manifest = json.loads(writer.manifest_path.read_text(encoding="utf-8"))
    manifest["batch_id"] = "not-the-filename"
    manifest["sequence_gap_count"] = -7
    manifest["time_regression_count"] = "many"
    manifest["checksum"].pop("bytes")
    manifest["checksum"].pop("lines")
    writer.manifest_path.chmod(0o644)
    writer.manifest_path.write_text(
        json.dumps(manifest) + "\n", encoding="utf-8"
    )

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    batch = bundle.manifest["capture"]["finalized_batches"][0]

    assert batch["integrity_valid"] is False
    assert batch["evidence_level"] == "L0"
    assert batch["batch_id"] == "bad-metadata"
    assert "manifest_batch_id_does_not_match_filename" in batch[
        "integrity_errors"
    ]
    assert "invalid_manifest_count:sequence_gap_count" in batch[
        "integrity_errors"
    ]
    assert "invalid_manifest_count:time_regression_count" in batch[
        "integrity_errors"
    ]
    assert "manifest_checksum_bytes_invalid" in batch["integrity_errors"]
    assert "manifest_checksum_lines_invalid" in batch["integrity_errors"]


def test_unknown_rtds_data_event_is_not_l2(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="rtds_ws",
        batch_id="nonsense",
        schema_version="edge-lab-recorder.raw.v1",
    )
    writer.append(
        received_at="2026-07-24T08:00:00Z",
        event_at="2026-07-24T08:00:00Z",
        sequence=None,
        payload={
            "kind": "data",
            "event_type": "nonsense",
            "payload": {"value": "not-a-price"},
        },
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    batch = bundle.manifest["capture"]["finalized_batches"][0]

    assert batch["integrity_valid"] is True
    assert batch["evidence_level"] == "L1"
    assert batch["content"]["usable_l2_record_count"] == 0
    assert batch["content"]["invalid_l2_candidate_count"] == 1


def test_manifest_reports_duplicates_gaps_and_checksum_failures(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="rtds_ws",
        batch_id="prices",
        schema_version="edge-lab-recorder.raw.v1",
    )
    common = {
        "received_at": "2026-07-24T08:00:00Z",
        "event_at": "2026-07-24T08:00:00Z",
        "sequence": 1,
        "payload": {
            "kind": "data",
            "event_type": "crypto_prices",
            "symbol": "btcusdt",
            "price": Decimal("118000.10"),
        },
    }
    writer.append(**common)
    writer.append(**common)
    writer.append(
        received_at="2026-07-24T08:00:02Z",
        event_at="2026-07-24T08:00:02Z",
        sequence=4,
        payload={
            "kind": "data",
            "event_type": "crypto_prices",
            "symbol": "btcusdt",
            "price": Decimal("118001.20"),
        },
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    writer.path.chmod(0o644)
    with writer.path.open("ab") as handle:
        handle.write(b'{"tampered":true}\n')

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    batch = bundle.manifest["capture"]["finalized_batches"][0]
    assert batch["counts"]["duplicate_count"] == 1
    assert batch["counts"]["sequence_gap_count"] == 1
    assert batch["counts"]["sequence_missing_count"] == 2
    assert batch["checksum"]["verified"] is False
    assert batch["evidence_level"] == "L0"
    assert bundle.quality["duplicates"]["exact_duplicate_count_recomputed"] is None
    assert (
        bundle.quality["duplicates"][
            "declared_exact_duplicate_count_unverified"
        ]
        == 1
    )
    assert bundle.quality["sequence"]["gap_count"] == 1
    assert bundle.quality["sequence"]["missing_count"] == 2
    assert bundle.quality["checksums"]["mismatch_count"] == 1
    assert any(
        item["code"] == "raw_checksum_mismatch"
        for item in bundle.quality["gaps"]
    )
    assert any(
        item["code"] == "declared_duplicate_attempts_unverifiable"
        for item in bundle.quality["gaps"]
    )


def test_cross_source_l2_readiness_is_blocked_by_time_regression(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    store = CaptureStore(capture_root)
    clob = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="clob",
        schema_version="edge-lab-recorder.raw.v1",
    )
    _append_forward_book(
        clob,
        received_at="2026-07-24T08:00:01Z",
        sequence=1,
        price="0.42",
        server_timestamp="1784880000000",
    )
    clob.append(
        received_at="2026-07-24T08:00:01.500000Z",
        event_at=None,
        sequence=None,
        payload={
            "session_id": "test-session",
            "connection_id": "test-clob-connection",
            "kind": "diagnostic",
            "event_type": "anomaly.time_regression",
            "trigger_frame_hash": hashlib.sha256(
                b"clob-frame-2"
            ).hexdigest(),
            "detail": {
                "entity": "clob_market_ws:yes-token",
                "previous_server_timestamp": "1784880000000",
                "current_server_timestamp": "1784879999000",
            },
        },
    )
    clob.append(
        received_at="2026-07-24T08:00:02Z",
        event_at="2026-07-24T07:59:59Z",
        sequence=2,
        payload={
            "session_id": "test-session",
            "connection_id": "test-clob-connection",
            "frame_index": 2,
            "frame_hash": hashlib.sha256(
                b"clob-frame-2"
            ).hexdigest(),
            "monotonic_ns": 1_000_002,
            "server_timestamp": "1784879999000",
            "kind": "data",
            "event_type": "book",
            "payload": {
                "event_type": "book",
                "asset_id": "yes-token",
                "market": "condition",
                "timestamp": "1784880000001",
                "bids": [{"price": Decimal("0.42"), "size": Decimal("10")}],
                "asks": [{"price": Decimal("0.43"), "size": Decimal("12")}],
            },
        },
    )
    clob.finalize(finalized_at="2026-07-24T08:01:00Z")
    rtds = store.open_raw_batch(
        source="rtds_ws",
        batch_id="rtds",
        schema_version="edge-lab-recorder.raw.v1",
    )
    rtds.append(
        received_at="2026-07-24T08:00:01Z",
        event_at="2026-07-24T08:00:01Z",
        sequence=1,
        payload=_rtds_update_payload(),
    )
    rtds.finalize(finalized_at="2026-07-24T08:01:00Z")

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    assert bundle.quality["coverage"]["cross_source_latency"]["has_overlap"]
    assert bundle.quality["levels"]["cross_source_latency_inputs_present"]
    assert not bundle.quality["levels"]["cross_source_latency_l2_ready"]
    assert any(
        gap["code"] == "time_regression" and gap["severity"] == "error"
        for gap in bundle.quality["gaps"]
    )
    clob_batch = next(
        item
        for item in bundle.manifest["capture"]["finalized_batches"]
        if item["source"] == "clob_market_ws"
    )
    assert clob_batch["content"]["time_regression_count_recomputed"] == 1
    assert (
        clob_batch["content"][
            "recorder_global_time_regression_count_recomputed"
        ]
        == 1
    )
    regression = clob_batch["content"]["time_regressions_recomputed"][0]
    assert regression["entity"]["entity"] == "yes-token"
    assert regression["previous_frame_index"] == 1
    assert regression["frame_index"] == 2
    assert regression["recorder_diagnostic_observed"] is True
    assert (
        clob_batch["content"][
            "recorder_time_regression_diagnostic_count"
        ]
        == 1
    )


def test_cross_entity_event_interleaving_is_not_a_time_regression(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="rtds_ws",
        batch_id="cross-symbol-interleaving",
        schema_version="edge-lab-recorder.raw.v1",
    )
    writer.append(
        received_at="2026-07-24T08:00:01Z",
        event_at="2026-07-24T08:00:02Z",
        sequence=None,
        payload=_rtds_update_payload(
            symbol="btcusdt",
            frame_index=0,
        ),
    )
    writer.append(
        received_at="2026-07-24T08:00:02Z",
        event_at="2026-07-24T08:00:01Z",
        sequence=None,
        payload=_rtds_update_payload(
            symbol="ethusdt",
            frame_index=1,
        ),
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    batch = bundle.manifest["capture"]["finalized_batches"][0]
    assert batch["integrity_valid"] is True
    assert batch["content"]["time_regression_count_recomputed"] == 0
    assert (
        batch["content"][
            "ambiguous_time_regression_count_recomputed"
        ]
        == 0
    )
    assert (
        batch["content"][
            "recorder_global_time_regression_count_recomputed"
        ]
        == 1
    )
    assert not any(
        gap["code"] in {"time_regression", "ambiguous_time_regression"}
        for gap in bundle.quality["gaps"]
    )


def test_same_clock_domain_regression_is_detected_across_batches(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    store = CaptureStore(capture_root)
    first = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="first",
        schema_version="edge-lab-recorder.raw.v1",
    )
    _append_forward_book(
        first,
        received_at="2026-07-24T08:00:01Z",
        event_at="2026-07-24T08:00:02Z",
        sequence=1,
        price="0.42",
        server_timestamp="1784880002000",
    )
    first.finalize(finalized_at="2026-07-24T08:00:03Z")
    second = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="second",
        schema_version="edge-lab-recorder.raw.v1",
    )
    _append_forward_book(
        second,
        received_at="2026-07-24T08:00:04Z",
        event_at="2026-07-24T08:00:01Z",
        sequence=2,
        price="0.41",
        server_timestamp="1784880001000",
    )
    second.finalize(finalized_at="2026-07-24T08:00:05Z")

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    assert (
        bundle.quality["time"][
            "confirmed_same_clock_domain_regression_count"
        ]
        == 1
    )
    regression = bundle.manifest["capture"]["time_order"][
        "confirmed_regressions"
    ][0]
    assert regression["clock"] == "event_at"
    assert regression["entity"]["event_type"] == "book"
    assert any(
        gap["code"] == "time_regression" and gap["severity"] == "error"
        for gap in bundle.quality["gaps"]
    )


def test_missing_ws_monotonic_clock_is_ambiguous_not_confirmed(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    writer = CaptureStore(capture_root).open_raw_batch(
        source="clob_market_ws",
        batch_id="missing-monotonic",
        schema_version="edge-lab-recorder.raw.v1",
    )
    for frame_index, second in ((1, 1), (0, 2)):
        writer.append(
            received_at=f"2026-07-24T08:00:0{second}Z",
            event_at=f"2026-07-24T08:00:0{second}Z",
            sequence=None,
            payload={
                "session_id": "session",
                "connection_id": "connection",
                "frame_index": frame_index,
                "kind": "data",
                "event_type": "book",
                "payload": {
                    "event_type": "book",
                    "asset_id": "yes-token",
                    "market": "condition",
                    "timestamp": str(1784880000000 + second),
                    "bids": [{"price": "0.42", "size": "10"}],
                    "asks": [{"price": "0.43", "size": "12"}],
                },
            },
        )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    assert (
        bundle.quality["time"][
            "confirmed_same_clock_domain_regression_count"
        ]
        == 0
    )
    assert (
        bundle.quality["time"]["ambiguous_observation_warning_count"]
        == 2
    )
    assert any(
        gap["code"] == "ambiguous_time_entity"
        and gap["severity"] == "warning"
        for gap in bundle.quality["gaps"]
    )


def test_time_diagnostic_join_consumes_each_diagnostic_once() -> None:
    frame_hash = "a" * 64
    entity = {
        "session_id": "session",
        "connection_id": "connection",
        "recorder_entity": "clob_market_ws:yes-token",
    }
    regression = {
        "clock": "event_at",
        "entity": entity,
        "frame_hash": frame_hash,
        "previous_server_timestamp": "2",
        "current_server_timestamp": "1",
    }
    diagnostic = {
        "session_id": "session",
        "connection_id": "connection",
        "entity": "clob_market_ws:yes-token",
        "trigger_frame_hash": frame_hash,
        "previous_server_timestamp": "2",
        "current_server_timestamp": "1",
    }
    regressions = [dict(regression), dict(regression)]

    matched, unmatched = data_manifest_module._crosscheck_time_diagnostics(
        regressions,
        [diagnostic],
    )

    assert matched == 1
    assert unmatched == 0
    assert [
        item["recorder_diagnostic_observed"] for item in regressions
    ] == [True, False]

    one_regression = [dict(regression)]
    matched, unmatched = data_manifest_module._crosscheck_time_diagnostics(
        one_regression,
        [diagnostic, dict(diagnostic)],
    )
    assert matched == 1
    assert unmatched == 1


def test_checkpoint_batch_reference_is_reported_and_verified(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    store = CaptureStore(capture_root)
    writer = store.open_raw_batch(
        source="rtds_ws",
        batch_id="checkpointed",
        schema_version="edge-lab-recorder.raw.v1",
    )
    writer.append(
        received_at="2026-07-24T08:00:01Z",
        event_at="2026-07-24T08:00:01Z",
        sequence=1,
        payload=_rtds_update_payload(),
    )
    batch_manifest = writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    store.write_checkpoint(
        "rtds_ws",
        {
            "schema_version": "edge-lab-capture-runtime.checkpoint.v1",
            "capture_runtime": {
                "last_finalized_batch": {
                    "batch_id": batch_manifest["batch_id"],
                    "raw_path": batch_manifest["raw_path"],
                    "record_count": batch_manifest["record_count"],
                    "checksum": batch_manifest["checksum"],
                }
            },
            "recorder_checkpoint": {
                "schema_version": "edge-lab-recorder.checkpoint.v1"
            },
        },
        updated_at="2026-07-24T08:01:01Z",
    )

    valid = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    checkpoint = valid.manifest["capture"]["checkpoints"][0]
    assert checkpoint["last_finalized_batch"]["batch_id"] == "checkpointed"
    assert checkpoint["last_finalized_batch_reference_valid"] is True

    store.write_checkpoint(
        "rtds_ws",
        {
            "schema_version": "edge-lab-capture-runtime.checkpoint.v1",
            "capture_runtime": {
                "last_finalized_batch": {
                    "batch_id": "missing",
                    "raw_path": "raw/rtds_ws/missing.jsonl",
                    "record_count": 1,
                    "checksum": {
                        "algorithm": "sha256",
                        "value": "0" * 64,
                        "bytes": 1,
                        "lines": 1,
                    },
                }
            },
            "recorder_checkpoint": {
                "schema_version": "edge-lab-recorder.checkpoint.v1"
            },
        },
        updated_at="2026-07-24T08:01:02Z",
    )
    invalid = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    assert (
        invalid.manifest["capture"]["checkpoints"][0][
            "last_finalized_batch_reference_valid"
        ]
        is False
    )
    assert any(
        gap["code"] == "checkpoint_raw_reference_invalid"
        for gap in invalid.quality["gaps"]
    )


def test_writer_emits_canonical_json_and_refuses_to_overwrite(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    legacy_file = legacy_root / "compatibility_audit.json"
    legacy_file.write_text('{"new_live_orders_ready":false}\n', encoding="utf-8")
    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    output_dir = project_root / "research/edge_discovery_2026-07-24"

    manifest_path, quality_path = write_data_audit(bundle, output_dir=output_dir)

    assert manifest_path.name == "DATA_MANIFEST.json"
    assert quality_path.name == "DATA_QUALITY.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == bundle.manifest
    assert json.loads(quality_path.read_text(encoding="utf-8")) == bundle.quality
    expected_manifest = (
        json.dumps(
            bundle.manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    assert manifest_path.read_bytes() == expected_manifest

    with pytest.raises(FileExistsError):
        write_data_audit(bundle, output_dir=output_dir)


def test_writer_recovers_identical_quality_prepare_after_second_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )
    output_dir = project_root / "research/edge_discovery-atomic"
    original_write = data_manifest_module._write_new_file
    calls = 0

    def fail_second(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated process boundary failure")
        original_write(path, data)

    monkeypatch.setattr(
        data_manifest_module, "_write_new_file", fail_second
    )
    with pytest.raises(OSError, match="simulated"):
        write_data_audit(bundle, output_dir=output_dir)
    assert (output_dir / "DATA_QUALITY.json").is_file()
    assert not (output_dir / "DATA_MANIFEST.json").exists()

    monkeypatch.setattr(
        data_manifest_module, "_write_new_file", original_write
    )
    manifest_path, quality_path = write_data_audit(
        bundle, output_dir=output_dir
    )
    assert manifest_path.is_file()
    assert quality_path.is_file()


def test_external_symlink_input_is_reported_without_being_read(
    tmp_path: Path,
) -> None:
    project_root, capture_root, legacy_root = _make_roots(tmp_path)
    outside = tmp_path / "outside.manifest.json"
    outside.write_text('{"secret":"must-not-be-read"}\n', encoding="utf-8")
    link = (
        capture_root
        / "raw/clob_market_ws/external.manifest.json"
    )
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    bundle = build_data_audit(
        project_root=project_root,
        capture_root=capture_root,
        legacy_root=legacy_root,
    )

    assert bundle.manifest["capture"]["unsafe_input_paths"] == [
        {
            "path": (
                "data/edge_discovery_2026-07-24/raw/"
                "clob_market_ws/external.manifest.json"
            ),
            "reason": "symbolic links are not accepted as audit inputs",
            "evidence_level": "L0",
        }
    ]
    assert any(
        gap["code"] == "unsafe_input_path"
        for gap in bundle.quality["gaps"]
    )


@requires_posix_descriptor_reads
def test_descriptor_bound_read_rejects_symlink_swap_before_final_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, capture_root, _ = _make_roots(tmp_path)
    victim = capture_root / "raw/clob_market_ws/victim.json"
    victim.parent.mkdir(parents=True)
    victim.write_text('{"scope":"inside"}\n', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"must-not-be-read"}\n', encoding="utf-8")
    original_open = data_manifest_module.os.open
    swapped = False

    def swap_then_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == victim.name and dir_fd is not None and not swapped:
            victim.unlink()
            victim.symlink_to(outside)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(data_manifest_module.os, "open", swap_then_open)

    with pytest.raises(
        data_manifest_module.UnsafeInputPathError,
        match="symbolic links",
    ):
        data_manifest_module._read_bytes(
            victim,
            allowed_root=capture_root,
        )
    assert swapped


@requires_posix_descriptor_reads
def test_descriptor_bound_read_rejects_audit_root_ancestor_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, capture_root, _ = _make_roots(tmp_path)
    relative_victim = Path("raw/clob_market_ws/victim.json")
    victim = capture_root / relative_victim
    victim.parent.mkdir(parents=True)
    victim.write_text('{"scope":"inside"}\n', encoding="utf-8")
    outside_data = tmp_path / "outside-data"
    outside_capture = outside_data / capture_root.name
    outside_victim = outside_capture / relative_victim
    outside_victim.parent.mkdir(parents=True)
    outside_victim.write_text(
        '{"secret":"must-not-be-read"}\n',
        encoding="utf-8",
    )
    original_open = data_manifest_module.os.open
    swapped = False

    def swap_ancestor_then_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "data" and dir_fd is not None and not swapped:
            original_data = project_root / "data-original"
            (project_root / "data").rename(original_data)
            (project_root / "data").symlink_to(outside_data)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        data_manifest_module.os,
        "open",
        swap_ancestor_then_open,
    )

    with pytest.raises(
        data_manifest_module.UnsafeInputPathError,
        match="symbolic links",
    ):
        data_manifest_module._read_bytes(
            victim,
            allowed_root=capture_root,
        )
    assert swapped
