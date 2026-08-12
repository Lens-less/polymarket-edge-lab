"""Audited registry tests for frozen short-crypto announcement batches."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.edge_lab.data_store import canonical_json_bytes, canonical_record_id
from src.edge_lab.short_crypto_catalog import ShortCryptoTarget
from src.edge_lab.short_crypto_registry import (
    RegistryConflictError,
    RegistryInputError,
    build_short_crypto_registry,
    merge_short_crypto_registry_snapshots,
)


def _announcement(
    *,
    slug: str = "btc-updown-5m-1784979900",
    market_id: str = "3081000",
    condition_byte: str = "ab",
    received_at: str = "2026-07-24T13:20:00.000000Z",
) -> dict[str, object]:
    asset, _, horizon, _ = slug.split("-")
    display_asset = {"btc": "Bitcoin", "eth": "Ethereum"}[asset]
    condition_id = "0x" + condition_byte * 32
    token_ids = ["1" * 76, "2" * 76]
    event = {
        "event_type": "new_market",
        "id": market_id,
        "slug": slug,
        "condition_id": condition_id,
        "market": condition_id,
        "assets_ids": token_ids,
        "clob_token_ids": token_ids,
        "outcomes": ["Up", "Down"],
        "description": (
            f"The resolution source is Chainlink, specifically the "
            f"{asset.upper()}/USD data stream at "
            f"https://data.chain.link/streams/{asset}-usd."
        ),
        "event_message": {"slug": slug},
        "horizon": horizon,
    }
    recorder = {
        "schema_version": "clob-market-ws.new_market.v1",
        "source": "clob_market_ws",
        "event_type": "new_market",
        "payload": event,
    }
    record: dict[str, object] = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "clob_market_ws",
        "received_at": received_at,
        "event_at": None,
        "sequence": None,
        "payload": recorder,
    }
    record["record_id"] = canonical_record_id(record)
    return record


def _write_jsonl(path: Path, *records: object) -> Path:
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    return path


def test_builds_json_serializable_content_addressed_registry(
    tmp_path: Path,
) -> None:
    raw_path = _write_jsonl(tmp_path / "batch.jsonl", _announcement())

    snapshot = build_short_crypto_registry((raw_path,))

    assert json.loads(json.dumps(snapshot)) == snapshot
    assert snapshot["schema_version"] == "edge-lab.short-crypto-registry.v1"
    content_address = snapshot["snapshot_sha256"]
    body = dict(snapshot)
    del body["snapshot_sha256"]
    assert content_address == hashlib.sha256(
        canonical_json_bytes(body)
    ).hexdigest()
    assert snapshot["inputs"] == [
        {
            "path": str(raw_path.resolve()),
            "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "byte_count": len(raw_path.read_bytes()),
            "line_count": 1,
        }
    ]
    assert snapshot["completeness"] == {
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
    }
    assert snapshot["targets"][0]["slug"] == "btc-updown-5m-1784979900"
    assert snapshot["targets"][0]["market_id"] == "3081000"
    assert len(snapshot["targets"][0]["rule_hash"]) == 64
    assert snapshot["targets"][0]["announced_at"] == (
        "2026-07-24T13:20:00.000000Z"
    )
    assert snapshot["targets"][0]["announcement_record_id"] == (
        _announcement()["record_id"]
    )
    reconstructed = {
        key: value
        for key, value in snapshot["targets"][0].items()
        if key != "provenance"
    }
    assert ShortCryptoTarget(**reconstructed).slug == (
        "btc-updown-5m-1784979900"
    )
    assert snapshot["targets"][0]["provenance"] == [
        {
            "record_id": _announcement()["record_id"],
            "path": str(raw_path.resolve()),
            "line": 1,
        }
    ]


def test_rejects_nonfinal_partial_input_before_reading_it(tmp_path: Path) -> None:
    partial_path = _write_jsonl(
        tmp_path / "batch.jsonl.partial",
        _announcement(),
    )

    with pytest.raises(RegistryInputError) as caught:
        build_short_crypto_registry((partial_path,))

    assert caught.value.code == "unfinalized_input"


def test_symlink_cannot_disguise_partial_input(tmp_path: Path) -> None:
    partial_path = _write_jsonl(
        tmp_path / "batch.jsonl.partial",
        _announcement(),
    )
    disguised_path = tmp_path / "disguised.jsonl"
    disguised_path.symlink_to(partial_path)

    with pytest.raises(RegistryInputError) as caught:
        build_short_crypto_registry((disguised_path,))

    assert caught.value.code == "unfinalized_input"


def test_requires_explicit_unique_tuple_or_list_of_existing_files(
    tmp_path: Path,
) -> None:
    raw_path = _write_jsonl(tmp_path / "batch.jsonl", _announcement())

    with pytest.raises(RegistryInputError) as implicit:
        build_short_crypto_registry(path for path in (raw_path,))
    with pytest.raises(RegistryInputError) as duplicate:
        build_short_crypto_registry((raw_path, raw_path))
    with pytest.raises(RegistryInputError) as missing:
        build_short_crypto_registry((tmp_path / "missing.jsonl",))

    assert implicit.value.code == "explicit_paths_required"
    assert duplicate.value.code == "duplicate_input"
    assert missing.value.code == "missing_input"


def test_deduplicates_identical_slug_and_retains_every_raw_location(
    tmp_path: Path,
) -> None:
    record = _announcement()
    later = _announcement(received_at="2026-07-24T13:21:00.000000Z")
    first = _write_jsonl(tmp_path / "a.jsonl", record)
    second = _write_jsonl(tmp_path / "b.jsonl", later)

    forward = build_short_crypto_registry([first, second])
    reverse = build_short_crypto_registry([second, first])

    assert reverse == forward
    assert forward["completeness"][
        "duplicate_target_announcement_count"
    ] == 1
    assert forward["targets"][0]["announced_at"] == (
        "2026-07-24T13:20:00.000000Z"
    )
    assert forward["targets"][0]["announcement_record_id"] == (
        record["record_id"]
    )
    assert forward["targets"][0]["provenance"] == [
        {
            "record_id": record["record_id"],
            "path": str(first.resolve()),
            "line": 1,
        },
        {
            "record_id": later["record_id"],
            "path": str(second.resolve()),
            "line": 1,
        },
    ]


def test_incremental_merge_is_byte_identical_to_full_registry_rebuild(
    tmp_path: Path,
) -> None:
    later = _announcement(received_at="2026-07-24T13:21:00.000000Z")
    earlier = _announcement(received_at="2026-07-24T13:20:00.000000Z")
    z_path = _write_jsonl(tmp_path / "z.jsonl", later)
    a_path = _write_jsonl(
        tmp_path / "a.jsonl",
        earlier,
        {"unrelated": True},
        "not-an-object",
    )

    base = build_short_crypto_registry((z_path,))
    delta = build_short_crypto_registry((a_path,))
    merged = merge_short_crypto_registry_snapshots(base, delta)
    rebuilt = build_short_crypto_registry((a_path, z_path))

    assert merged == rebuilt
    assert canonical_json_bytes(merged) == canonical_json_bytes(rebuilt)


def test_same_slug_with_different_identity_fails_closed_with_provenance(
    tmp_path: Path,
) -> None:
    first = _write_jsonl(tmp_path / "a.jsonl", _announcement())
    conflict = _announcement(
        market_id="3081999",
        condition_byte="cd",
        received_at="2026-07-24T13:21:00.000000Z",
    )
    second = _write_jsonl(tmp_path / "b.jsonl", conflict)

    with pytest.raises(RegistryConflictError) as caught:
        build_short_crypto_registry((first, second))

    assert caught.value.code == "slug_identity_conflict"
    assert caught.value.slug == "btc-updown-5m-1784979900"
    assert caught.value.first_provenance == {
        "record_id": _announcement()["record_id"],
        "path": str(first.resolve()),
        "line": 1,
    }
    assert caught.value.conflicting_provenance == {
        "record_id": conflict["record_id"],
        "path": str(second.resolve()),
        "line": 1,
    }


def test_same_slug_and_identity_with_different_rule_fails_closed(
    tmp_path: Path,
) -> None:
    first_record = _announcement()
    changed_rule = _announcement(
        received_at="2026-07-24T13:21:00.000000Z",
    )
    recorder = changed_rule["payload"]
    assert isinstance(recorder, dict)
    event = recorder["payload"]
    assert isinstance(event, dict)
    event["description"] = str(event["description"]) + " Contract text changed."
    changed_rule["record_id"] = canonical_record_id(changed_rule)
    first = _write_jsonl(tmp_path / "a.jsonl", first_record)
    second = _write_jsonl(tmp_path / "b.jsonl", changed_rule)

    with pytest.raises(RegistryConflictError) as caught:
        build_short_crypto_registry((first, second))

    assert caught.value.code == "slug_identity_conflict"
    assert caught.value.first_provenance["record_id"] == first_record["record_id"]
    assert (
        caught.value.conflicting_provenance["record_id"]
        == changed_rule["record_id"]
    )


def test_counts_and_audits_rejected_jsonl_records(tmp_path: Path) -> None:
    valid = _announcement()
    rejected = _announcement(
        received_at="2026-07-24T13:22:00.000000Z",
    )
    recorder = rejected["payload"]
    assert isinstance(recorder, dict)
    event = recorder["payload"]
    assert isinstance(event, dict)
    event["outcomes"] = ["Down", "Up"]
    rejected["record_id"] = canonical_record_id(rejected)
    raw_path = tmp_path / "mixed.jsonl"
    raw_path.write_text(
        json.dumps(valid) + "\n"
        + json.dumps(rejected)
        + "\n"
        + "{not-json\n"
        + "[]\n",
        encoding="utf-8",
    )

    snapshot = build_short_crypto_registry((raw_path,))

    assert snapshot["completeness"] == {
        "status": "incomplete",
        "input_file_count": 1,
        "input_line_count": 4,
        "parsed_object_count": 2,
        "target_announcement_count": 1,
        "unique_target_count": 1,
        "duplicate_target_announcement_count": 0,
        "unrelated_record_count": 0,
        "rejected_record_count": 3,
        "rejection_counts": {
            "invalid_json": 1,
            "non_object_record": 1,
            "outcomes_mismatch": 1,
        },
    }
    assert snapshot["rejections"] == [
        {
            "code": "outcomes_mismatch",
            "detail": (
                "outcomes_mismatch: outcomes must be exactly "
                "['Up', 'Down'] in token order"
            ),
            "record_id": rejected["record_id"],
            "path": str(raw_path.resolve()),
            "line": 2,
        },
        {
            "code": "invalid_json",
            "detail": "line is not valid JSON",
            "record_id": None,
            "path": str(raw_path.resolve()),
            "line": 3,
        },
        {
            "code": "non_object_record",
            "detail": "JSONL record must be an object",
            "record_id": None,
            "path": str(raw_path.resolve()),
            "line": 4,
        },
    ]


def test_invalid_utf8_is_distinguished_from_invalid_json(tmp_path: Path) -> None:
    raw_path = tmp_path / "invalid-utf8.jsonl"
    raw_path.write_bytes(b"\xff\n")

    snapshot = build_short_crypto_registry((raw_path,))

    assert snapshot["completeness"]["rejection_counts"] == {
        "invalid_utf8": 1
    }
    assert snapshot["rejections"] == [
        {
            "code": "invalid_utf8",
            "detail": "line is not valid UTF-8",
            "record_id": None,
            "path": str(raw_path.resolve()),
            "line": 1,
        }
    ]


def test_target_envelope_contract_error_is_counted_not_promoted(
    tmp_path: Path,
) -> None:
    malformed_target = _announcement()
    recorder_payload = malformed_target["payload"]
    assert isinstance(recorder_payload, dict)
    recorder_payload["price"] = 0.5
    raw_path = _write_jsonl(tmp_path / "malformed.jsonl", malformed_target)

    snapshot = build_short_crypto_registry((raw_path,))

    assert snapshot["targets"] == []
    assert snapshot["completeness"]["status"] == "incomplete"
    assert snapshot["completeness"]["rejection_counts"] == {
        "catalog_contract_error": 1
    }
    assert snapshot["rejections"] == [
        {
            "code": "catalog_contract_error",
            "detail": "target announcement violates the raw data contract",
            "record_id": malformed_target["record_id"],
            "path": str(raw_path.resolve()),
            "line": 1,
        }
    ]
