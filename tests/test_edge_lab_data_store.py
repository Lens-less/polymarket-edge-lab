"""Contract tests for immutable, provenance-rich edge-lab capture batches."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from src.edge_lab.data_store import (
    BatchFinalizedError,
    CaptureStore,
    CriticalFloatError,
    _can_reuse_integrity_validation,
    canonical_event_fingerprint,
    canonical_json_bytes,
    canonical_record_id,
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_canonical_record_id_is_order_independent_and_decimal_exact() -> None:
    first = {
        "schema_version": "clob.book.v1",
        "source": "clob_market_ws",
        "received_at": "2026-07-24T08:00:00Z",
        "event_at": "2026-07-24T07:59:59.123Z",
        "sequence": 7,
        "payload": {
            "size": Decimal("10.00"),
            "price": Decimal("0.4200"),
            "asset_id": "yes",
        },
    }
    reordered = {
        "payload": {
            "asset_id": "yes",
            "price": Decimal("0.4200"),
            "size": Decimal("10.00"),
        },
        "sequence": 7,
        "event_at": "2026-07-24T07:59:59.123000+00:00",
        "received_at": "2026-07-24T08:00:00+00:00",
        "source": "clob_market_ws",
        "schema_version": "clob.book.v1",
    }

    assert canonical_record_id(first) == canonical_record_id(reordered)
    event_only = {
        key: value for key, value in first.items() if key != "received_at"
    }
    assert canonical_event_fingerprint(first) == canonical_event_fingerprint(
        event_only
    )

    encoded = json.loads(canonical_json_bytes(first))
    assert encoded["payload"]["price"] == "0.4200"
    assert encoded["payload"]["size"] == "10.00"
    assert isinstance(encoded["payload"]["price"], str)


def test_raw_batch_is_separate_from_derived_and_becomes_read_only(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    writer = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="20260724T080000Z-0001",
        schema_version="clob.book.v1",
    )
    result = writer.append(
        received_at="2026-07-24T08:00:00Z",
        event_at="2026-07-24T07:59:59Z",
        sequence=1,
        payload={"asset_id": "yes", "price": Decimal("0.42")},
    )
    manifest = writer.finalize(finalized_at="2026-07-24T08:01:00Z")

    assert result.written
    assert writer.path == tmp_path / "raw/clob_market_ws/20260724T080000Z-0001.jsonl"
    assert writer.manifest_path == (
        tmp_path / "raw/clob_market_ws/20260724T080000Z-0001.manifest.json"
    )
    assert store.derived_dir == tmp_path / "derived"
    assert writer.path.is_file()
    assert writer.manifest_path.is_file()
    assert not writer.partial_path.exists()
    assert json.loads(writer.manifest_path.read_text(encoding="utf-8")) == manifest
    assert manifest["raw_path"] == (
        "raw/clob_market_ws/20260724T080000Z-0001.jsonl"
    )
    assert stat.S_IMODE(writer.path.stat().st_mode) & stat.S_IWUSR == 0

    with pytest.raises(BatchFinalizedError):
        writer.append(
            received_at="2026-07-24T08:00:01Z",
            event_at=None,
            sequence=2,
            payload={"asset_id": "yes"},
        )
    with pytest.raises(FileExistsError):
        store.open_raw_batch(
            source="clob_market_ws",
            batch_id="20260724T080000Z-0001",
            schema_version="clob.book.v1",
        )


def test_manifest_accounts_for_duplicates_regressions_gaps_and_schema_drift(
    tmp_path: Path,
) -> None:
    writer = CaptureStore(tmp_path).open_raw_batch(
        source="rtds_crypto",
        batch_id="batch-1",
        schema_version="rtds.crypto.v1",
    )
    common = {
        "received_at": "2026-07-24T08:00:00Z",
        "event_at": "2026-07-24T08:00:01Z",
        "sequence": 10,
        "payload": {"symbol": "BTCUSDT", "price": Decimal("118000.10")},
    }
    first = writer.append(**common)
    duplicate = writer.append(**common)
    writer.append(
        received_at="2026-07-24T08:00:02Z",
        event_at="2026-07-24T08:00:00Z",
        sequence=13,
        payload={
            "symbol": "BTCUSDT",
            "price": Decimal("118001.20"),
            "venue": "binance",
        },
    )
    manifest = writer.finalize(finalized_at="2026-07-24T08:02:00Z")

    assert first.written
    assert not duplicate.written
    assert first.record_id == duplicate.record_id
    assert manifest["record_count"] == 2
    assert manifest["attempted_count"] == 3
    assert manifest["duplicate_count"] == 1
    assert manifest["time_regression_count"] == 1
    assert manifest["sequence_gap_count"] == 1
    assert manifest["sequence_missing_count"] == 2
    assert manifest["sequence_gaps"] == [
        {
            "after": 10,
            "before": 13,
            "missing_start": 11,
            "missing_end": 12,
            "missing_count": 2,
        }
    ]
    assert len(manifest["schema_fingerprints"]) == 2
    assert manifest["schema_drift_count"] == 1
    assert manifest["schema_change_count"] == 1
    assert manifest["time_range"] == {
        "received_at": {
            "min": "2026-07-24T08:00:00.000000Z",
            "max": "2026-07-24T08:00:02.000000Z",
        },
        "event_at": {
            "min": "2026-07-24T08:00:00.000000Z",
            "max": "2026-07-24T08:00:01.000000Z",
        },
    }

    raw = writer.path.read_bytes()
    assert manifest["checksum"] == {
        "algorithm": "sha256",
        "value": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "lines": 2,
    }
    rows = _read_jsonl(writer.path)
    assert rows[0]["record_id"] == first.record_id
    assert rows[0]["payload"]["price"] == "118000.10"


def test_reconnected_copy_is_preserved_but_counted_as_semantic_duplicate(
    tmp_path: Path,
) -> None:
    writer = CaptureStore(tmp_path).open_raw_batch(
        source="clob_market_ws",
        batch_id="reconnect",
        schema_version="clob.trade.v1",
    )
    server_event = {
        "event_at": "2026-07-24T08:00:00Z",
        "sequence": 42,
        "payload": {
            "asset_id": "yes",
            "price": Decimal("0.4200"),
            "size": Decimal("5.00"),
        },
    }
    first = writer.append(
        received_at="2026-07-24T08:00:00.100Z",
        **server_event,
    )
    replayed_after_reconnect = writer.append(
        received_at="2026-07-24T08:00:03.500Z",
        **server_event,
    )
    manifest = writer.finalize(finalized_at="2026-07-24T08:01:00Z")

    assert first.written
    assert replayed_after_reconnect.written
    assert first.record_id != replayed_after_reconnect.record_id
    assert first.event_fingerprint == replayed_after_reconnect.event_fingerprint
    assert manifest["record_count"] == 2
    assert manifest["duplicate_count"] == 0
    assert manifest["unique_event_count"] == 1
    assert manifest["semantic_duplicate_count"] == 1
    assert manifest["semantic_duplicate_rate"] == "0.5"
    rows = _read_jsonl(writer.path)
    assert len(rows) == 2
    assert rows[0]["event_fingerprint"] == rows[1]["event_fingerprint"]


@pytest.mark.parametrize(
    "payload",
    [
        {"price": 0.42},
        {"book": {"best_bid": 0.41}},
        {"levels": [{"size": 10.0}]},
        {"fee_rate": 0.05},
        {"fd": {"r": 0.05, "e": 1}},
        {"outcomePrices": [0.42, 0.58]},
    ],
)
def test_critical_financial_float_is_rejected(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    writer = CaptureStore(tmp_path).open_raw_batch(
        source="test_source",
        batch_id="float-rejection",
        schema_version="test.v1",
    )
    with pytest.raises(CriticalFloatError):
        writer.append(
            received_at="2026-07-24T08:00:00Z",
            event_at=None,
            sequence=None,
            payload=payload,
        )
    manifest = writer.finalize(finalized_at="2026-07-24T08:00:01Z")
    assert manifest["record_count"] == 0


def test_noncritical_float_telemetry_is_allowed(tmp_path: Path) -> None:
    writer = CaptureStore(tmp_path).open_raw_batch(
        source="weather_feed",
        batch_id="telemetry",
        schema_version="weather.telemetry.v1",
    )
    result = writer.append(
        received_at="2026-07-24T08:00:00Z",
        event_at=None,
        sequence=None,
        payload={"station": "KLGA", "temperature_c": 23.5},
    )
    writer.finalize(finalized_at="2026-07-24T08:00:01Z")

    assert result.written
    assert _read_jsonl(writer.path)[0]["payload"]["temperature_c"] == 23.5


def test_atomic_checkpoint_round_trips_decimal_without_temp_files(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    first_path = store.write_checkpoint(
        "clob_market_ws",
        {
            "last_sequence": 9,
            "last_price": Decimal("0.4200"),
            "updated_by": "recorder-a",
        },
        updated_at="2026-07-24T08:00:00Z",
    )
    second_path = store.write_checkpoint(
        "clob_market_ws",
        {
            "last_sequence": 10,
            "last_price": Decimal("0.4300"),
            "updated_by": "recorder-b",
        },
        updated_at="2026-07-24T08:00:01Z",
    )

    assert first_path == second_path == tmp_path / "checkpoints/clob_market_ws.json"
    assert store.read_checkpoint("clob_market_ws") == {
        "schema_version": "capture-checkpoint.v1",
        "source": "clob_market_ws",
        "updated_at": "2026-07-24T08:00:01.000000Z",
        "checkpoint": {
            "last_sequence": 10,
            "last_price": "0.4300",
            "updated_by": "recorder-b",
        },
    }
    assert not list((tmp_path / "checkpoints").glob("*.tmp"))


def test_integrity_audit_reports_crash_artifacts_without_mutating_them(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    source_dir = store.raw_dir / "clob_market_ws"
    source_dir.mkdir(parents=True, exist_ok=True)

    orphan_partial = source_dir / "orphan.jsonl.partial"
    orphan_partial.write_text('{"unfinished":true}\n', encoding="utf-8")
    raw_without_manifest = source_dir / "raw-only.jsonl"
    raw_without_manifest.write_text('{"record_id":"raw-only"}\n', encoding="utf-8")
    manifest_without_raw = source_dir / "manifest-only.manifest.json"
    manifest_without_raw.write_text("{}\n", encoding="utf-8")

    writer = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="tampered",
        schema_version="clob.book.v1",
    )
    writer.append(
        received_at="2026-07-24T08:00:00Z",
        event_at="2026-07-24T08:00:00Z",
        sequence=1,
        payload={"asset_id": "yes", "price": Decimal("0.42")},
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    writer.path.chmod(0o644)
    with writer.path.open("ab") as handle:
        handle.write(b'{"tampered":true}\n')
    writer.path.chmod(0o444)

    evidence_paths = [
        orphan_partial,
        raw_without_manifest,
        manifest_without_raw,
        writer.path,
        writer.manifest_path,
    ]
    before = {
        path: (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in evidence_paths
    }
    report = store.audit_integrity()
    repeated_report = store.audit_integrity()
    after = {
        path: (path.stat().st_mode, path.stat().st_mtime_ns, path.read_bytes())
        for path in evidence_paths
    }

    assert report == repeated_report
    assert before == after
    assert report["orphan_partials"] == [
        "raw/clob_market_ws/orphan.jsonl.partial"
    ]
    assert report["raw_without_manifest"] == [
        "raw/clob_market_ws/raw-only.jsonl"
    ]
    assert report["manifest_without_raw"] == [
        "raw/clob_market_ws/manifest-only.manifest.json"
    ]
    assert len(report["checksum_mismatches"]) == 1
    mismatch = report["checksum_mismatches"][0]
    assert mismatch["raw_path"] == "raw/clob_market_ws/tampered.jsonl"
    assert mismatch["manifest_path"] == (
        "raw/clob_market_ws/tampered.manifest.json"
    )
    assert mismatch["expected"] != mismatch["actual"]


def test_repeated_integrity_audit_revalidates_only_new_clean_pairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CaptureStore(tmp_path)
    old_writer = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="old-clean",
        schema_version="clob.book.v1",
    )
    old_writer.append(
        received_at="2026-07-24T08:00:00Z",
        event_at="2026-07-24T08:00:00Z",
        sequence=1,
        payload={"asset_id": "old", "price": Decimal("0.42")},
    )
    old_writer.finalize(finalized_at="2026-07-24T08:01:00Z")

    assert store.audit_integrity() == {
        "orphan_partials": [],
        "raw_without_manifest": [],
        "manifest_without_raw": [],
        "checksum_mismatches": [],
        "invalid_manifests": [],
    }

    new_writer = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="new-clean",
        schema_version="clob.book.v1",
    )
    new_writer.append(
        received_at="2026-07-24T08:02:00Z",
        event_at="2026-07-24T08:02:00Z",
        sequence=2,
        payload={"asset_id": "new", "price": Decimal("0.43")},
    )
    new_writer.finalize(finalized_at="2026-07-24T08:03:00Z")

    raw_reads = {old_writer.path: 0, new_writer.path: 0}
    real_open = Path.open

    def tracking_open(
        path: Path, *args: object, **kwargs: object
    ) -> object:
        if path in raw_reads and args and args[0] == "rb":
            raw_reads[path] += 1
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)

    assert store.audit_integrity() == {
        "orphan_partials": [],
        "raw_without_manifest": [],
        "manifest_without_raw": [],
        "checksum_mismatches": [],
        "invalid_manifests": [],
    }
    if _can_reuse_integrity_validation():
        assert raw_reads == {old_writer.path: 0, new_writer.path: 1}
    else:
        assert raw_reads == {old_writer.path: 1, new_writer.path: 1}


def test_cached_integrity_audit_invalidates_same_size_raw_tamper(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    writer = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="cached-clean",
        schema_version="clob.book.v1",
    )
    writer.append(
        received_at="2026-07-24T08:00:00Z",
        event_at="2026-07-24T08:00:00Z",
        sequence=1,
        payload={"asset_id": "yes", "price": Decimal("0.42")},
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    assert store.audit_integrity()["checksum_mismatches"] == []

    metadata = writer.path.stat()
    original = writer.path.read_bytes()
    tampered = original.replace(b'"yes"', b'"no!"', 1)
    assert tampered != original
    assert len(tampered) == len(original)
    writer.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    writer.path.write_bytes(tampered)
    os.utime(
        writer.path,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
    )
    writer.path.chmod(stat.S_IMODE(metadata.st_mode))

    report = store.audit_integrity()
    assert len(report["checksum_mismatches"]) == 1
    assert report["checksum_mismatches"][0]["raw_path"] == (
        "raw/clob_market_ws/cached-clean.jsonl"
    )


def test_cached_integrity_audit_invalidates_same_size_manifest_tamper(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    writer = store.open_raw_batch(
        source="clob_market_ws",
        batch_id="cached-manifest",
        schema_version="clob.book.v1",
    )
    writer.append(
        received_at="2026-07-24T08:00:00Z",
        event_at="2026-07-24T08:00:00Z",
        sequence=1,
        payload={"asset_id": "yes", "price": Decimal("0.42")},
    )
    writer.finalize(finalized_at="2026-07-24T08:01:00Z")
    assert store.audit_integrity()["invalid_manifests"] == []

    metadata = writer.manifest_path.stat()
    original = writer.manifest_path.read_bytes()
    tampered = original.replace(b'"sha256"', b'"sha257"', 1)
    assert tampered != original
    assert len(tampered) == len(original)
    writer.manifest_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    writer.manifest_path.write_bytes(tampered)
    os.utime(
        writer.manifest_path,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
    )
    writer.manifest_path.chmod(stat.S_IMODE(metadata.st_mode))

    report = store.audit_integrity()
    assert len(report["invalid_manifests"]) == 1
    assert report["invalid_manifests"][0]["manifest_path"] == (
        "raw/clob_market_ws/cached-manifest.manifest.json"
    )


def test_path_components_cannot_escape_store(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path)
    with pytest.raises(ValueError):
        store.open_raw_batch(
            source="../outside",
            batch_id="batch",
            schema_version="test.v1",
        )
    with pytest.raises(ValueError):
        store.open_raw_batch(
            source="safe",
            batch_id="../../outside",
            schema_version="test.v1",
        )
