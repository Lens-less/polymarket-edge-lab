"""Decision-vintage Chainlink price-to-beat extraction tests."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

import src.edge_lab.chainlink_ptb as chainlink_ptb_module
from src.edge_lab.chainlink_ptb import (
    FinalizedChainlinkBoundaryRequest,
    PriceToBeatRejection,
    extract_chainlink_boundary_batch_from_finalized_manifests,
    extract_chainlink_boundary_from_finalized_manifests,
    extract_chainlink_boundaries_from_finalized_manifests,
    extract_chainlink_price_to_beat,
    require_price_to_beat_for_decision,
)
from src.edge_lab.data_store import CaptureStore, canonical_record_id
from src.edge_lab.short_crypto_catalog import ShortCryptoTarget


OPEN_MS = 1_784_892_712_000
CLOSE_MS = OPEN_MS + 300_000
RULE_URL = "https://data.chain.link/streams/btc-usd"


def strict_target() -> ShortCryptoTarget:
    return ShortCryptoTarget(
        slug=f"btc-updown-5m-{OPEN_MS // 1_000}",
        market_id="3081000",
        condition_id="0x" + "ab" * 32,
        up_token_id="1" * 76,
        down_token_id="2" * 76,
        source_topic="crypto_prices_chainlink",
        source_symbol="btc/usd",
        horizon="5m",
        opens_at_ms=OPEN_MS,
        closes_at_ms=CLOSE_MS,
        announced_at="2026-07-24T10:00:00.000000Z",
        announcement_record_id="a" * 64,
        rule_hash="b" * 64,
    )


def chainlink_record(
    *,
    symbol: str = "btc/usd",
    timestamp_ms: int = OPEN_MS,
    value: str = "64970.34876688167",
    full_accuracy_value: str = "64970348766881670000000",
    received_at: str = "2026-07-24T11:31:53.393239Z",
) -> dict[str, object]:
    """Build the lossless raw envelope observed in finalized RTDS captures."""

    update = {
        "connection_id": "gVD3xq4lkWeIKEi-3A==",
        "payload": {
            "full_accuracy_value": full_accuracy_value,
            "symbol": symbol,
            "timestamp": timestamp_ms,
            "value": value,
        },
        "timestamp": timestamp_ms + 1_224,
        "topic": "crypto_prices_chainlink",
        "type": "update",
    }
    recorder_payload = {
        "connection_id": "capture-connection",
        "event_at": "2026-07-24T11:31:53.224000Z",
        "event_type": "crypto_prices_chainlink.update",
        "frame_index": 0,
        "kind": "data",
        "monotonic_ns": 1_166_022_953_408_958,
        "payload": update,
        "received_at": received_at,
        "schema_version": "rtds.crypto_prices_chainlink.update.v1",
        "sequence": None,
        "server_timestamp": timestamp_ms + 1_224,
        "session_id": "capture-session",
        "source": "rtds_ws",
    }
    record: dict[str, object] = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "rtds_ws",
        "received_at": received_at,
        "event_at": "2026-07-24T11:31:53.224000Z",
        "sequence": None,
        "payload": recorder_payload,
    }
    record["record_id"] = canonical_record_id(record)
    return record


def finalized_chainlink_manifest(
    tmp_path: Path,
    records: list[dict[str, object]],
) -> Path:
    writer = CaptureStore(tmp_path).open_raw_batch(
        source="rtds_ws",
        batch_id="chainlink-boundaries",
        schema_version="edge-lab-recorder.raw.v1",
    )
    for record in records:
        writer.append(
            received_at=record["received_at"],
            event_at=record["event_at"],
            sequence=record["sequence"],
            payload=record["payload"],
        )
    writer.finalize(finalized_at="2026-07-24T12:00:00.000000Z")
    return writer.manifest_path


def test_extracts_exact_open_and_close_from_finalized_rtds_manifest(
    tmp_path: Path,
) -> None:
    open_record = chainlink_record()
    close_record = chainlink_record(
        timestamp_ms=CLOSE_MS,
        value="64980",
        full_accuracy_value="64980000000000000000000",
        received_at="2026-07-24T11:36:53.393239Z",
    )
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [open_record, close_record],
    )

    boundaries = extract_chainlink_boundaries_from_finalized_manifests(
        [manifest_path],
        target=strict_target(),
        rule_url=RULE_URL,
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    assert boundaries.open_boundary.boundary_role == "open"
    assert boundaries.open_boundary.inner_timestamp_ms == OPEN_MS
    assert boundaries.open_boundary.line_number == 1
    assert boundaries.open_boundary.session_id == "capture-session"
    assert boundaries.open_boundary.connection_id == "capture-connection"
    assert boundaries.open_boundary.rule_hash == "b" * 64
    assert boundaries.open_boundary.source_manifest_path == str(manifest_path)
    assert boundaries.close_boundary.boundary_role == "close"
    assert boundaries.close_boundary.inner_timestamp_ms == CLOSE_MS
    assert boundaries.close_boundary.line_number == 2
    assert boundaries.close_boundary.price == Decimal("64980")


def test_batch_extracts_two_boundaries_with_one_manifest_inventory_pass(
    tmp_path: Path,
) -> None:
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            chainlink_record(),
            chainlink_record(
                timestamp_ms=CLOSE_MS,
                value="64980",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            ),
        ],
    )

    class OnePassManifestPaths:
        def __init__(self, path: Path) -> None:
            self.path = path
            self.iteration_count = 0

        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> Path:
            if index != 0:
                raise IndexError(index)
            return self.path

        def __iter__(self):
            self.iteration_count += 1
            if self.iteration_count > 1:
                raise AssertionError("manifest inventory was rescanned")
            yield self.path

    manifest_paths = OnePassManifestPaths(manifest_path)
    target = strict_target()

    results = extract_chainlink_boundary_batch_from_finalized_manifests(
        manifest_paths,
        requests={
            "open": FinalizedChainlinkBoundaryRequest(
                target=target,
                rule_url=RULE_URL,
                boundary_role="open",
                first_decision_at="2026-07-24T11:37:00.000000Z",
            ),
            "close": FinalizedChainlinkBoundaryRequest(
                target=target,
                rule_url=RULE_URL,
                boundary_role="close",
                first_decision_at="2026-07-24T11:37:00.000000Z",
            ),
        },
    )

    assert manifest_paths.iteration_count == 1
    assert results["open"] is not None
    assert results["open"].inner_timestamp_ms == OPEN_MS
    assert results["open"].line_number == 1
    assert results["close"] is not None
    assert results["close"].inner_timestamp_ms == CLOSE_MS
    assert results["close"].line_number == 2


def test_batch_does_not_deep_validate_irrelevant_manifest_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    irrelevant_manifest = finalized_chainlink_manifest(
        tmp_path / "irrelevant",
        [chainlink_record(timestamp_ms=OPEN_MS - 1_000)],
    )
    matching_manifest = finalized_chainlink_manifest(
        tmp_path / "matching",
        [chainlink_record()],
    )
    original = chainlink_ptb_module._verified_manifest_records
    deeply_validated: list[Path] = []

    def observe_deep_validation(
        manifest_path: Path,
    ) -> tuple[
        list[dict[str, object]],
        dict[str, tuple[int, str, str, str]],
    ]:
        deeply_validated.append(manifest_path)
        if manifest_path == irrelevant_manifest:
            raise AssertionError(
                "manifest raw without a requested timestamp must not be "
                "deep-validated"
            )
        return original(manifest_path)

    monkeypatch.setattr(
        chainlink_ptb_module,
        "_verified_manifest_records",
        observe_deep_validation,
    )
    target = strict_target()

    results = extract_chainlink_boundary_batch_from_finalized_manifests(
        [irrelevant_manifest, matching_manifest],
        requests={
            "open": FinalizedChainlinkBoundaryRequest(
                target=target,
                rule_url=RULE_URL,
                boundary_role="open",
                first_decision_at="2026-07-24T11:37:00.000000Z",
            ),
        },
    )

    assert results["open"] is not None
    assert deeply_validated == [matching_manifest]


def test_batch_prefilter_preserves_cross_manifest_duplicate_rejection(
    tmp_path: Path,
) -> None:
    first_manifest = finalized_chainlink_manifest(
        tmp_path / "first",
        [chainlink_record()],
    )
    second_manifest = finalized_chainlink_manifest(
        tmp_path / "second",
        [chainlink_record()],
    )
    target = strict_target()

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_boundary_batch_from_finalized_manifests(
            [first_manifest, second_manifest],
            requests={
                "open": FinalizedChainlinkBoundaryRequest(
                    target=target,
                    rule_url=RULE_URL,
                    boundary_role="open",
                    first_decision_at="2026-07-24T11:37:00.000000Z",
                ),
            },
        )

    assert caught.value.code == "duplicate_record_id"


def test_extracts_open_from_finalized_manifest_before_close_exists(
    tmp_path: Path,
) -> None:
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [chainlink_record()],
    )

    boundary = extract_chainlink_boundary_from_finalized_manifests(
        [manifest_path],
        target=strict_target(),
        rule_url=RULE_URL,
        boundary_role="open",
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    assert boundary.boundary_role == "open"
    assert boundary.inner_timestamp_ms == OPEN_MS
    assert boundary.available_at_first_decision is True
    assert boundary.retrospective is False


def test_close_boundary_can_never_be_rebound_as_price_to_beat(
    tmp_path: Path,
) -> None:
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            chainlink_record(
                timestamp_ms=CLOSE_MS,
                value="64980",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            )
        ],
    )
    close_boundary = extract_chainlink_boundary_from_finalized_manifests(
        [manifest_path],
        target=strict_target(),
        rule_url=RULE_URL,
        boundary_role="close",
        first_decision_at="2026-07-24T11:37:00.000000Z",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        require_price_to_beat_for_decision(
            close_boundary,
            decision_at="2026-07-24T11:37:01.000000Z",
        )

    assert caught.value.code == "boundary_role_not_price_to_beat"


@pytest.mark.parametrize(
    ("role", "timestamp_ms"),
    [
        ("open", OPEN_MS + 1),
        ("close", CLOSE_MS - 1),
    ],
)
def test_finalized_boundary_rejects_off_by_one_inner_timestamp(
    tmp_path: Path,
    role: str,
    timestamp_ms: int,
) -> None:
    open_timestamp = timestamp_ms if role == "open" else OPEN_MS
    close_timestamp = timestamp_ms if role == "close" else CLOSE_MS
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            chainlink_record(timestamp_ms=open_timestamp),
            chainlink_record(
                timestamp_ms=close_timestamp,
                value="64980",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            ),
        ],
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_boundaries_from_finalized_manifests(
            [manifest_path],
            target=strict_target(),
            rule_url=RULE_URL,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "price_to_beat_missing"


@pytest.mark.parametrize(
    ("target_mutation", "rule_url", "expected_code"),
    [
        (
            {"source_topic": "crypto_prices"},
            RULE_URL,
            "target_topic_mismatch",
        ),
        (
            {"source_symbol": "eth/usd"},
            RULE_URL,
            "target_rule_url_mismatch",
        ),
        (
            {},
            "https://data.chain.link/streams/eth-usd",
            "target_rule_url_mismatch",
        ),
        (
            {"rule_hash": "0" * 63},
            RULE_URL,
            "target_rule_hash_invalid",
        ),
    ],
)
def test_finalized_boundary_rejects_target_topic_symbol_or_rule_mismatch(
    tmp_path: Path,
    target_mutation: dict[str, object],
    rule_url: str,
    expected_code: str,
) -> None:
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            chainlink_record(),
            chainlink_record(
                timestamp_ms=CLOSE_MS,
                value="64980",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            ),
        ],
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_boundaries_from_finalized_manifests(
            [manifest_path],
            target=replace(strict_target(), **target_mutation),
            rule_url=rule_url,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == expected_code


def test_finalized_boundary_rejects_rtds_topic_mismatch(
    tmp_path: Path,
) -> None:
    open_record = chainlink_record()
    recorder = open_record["payload"]
    assert isinstance(recorder, dict)
    update = recorder["payload"]
    assert isinstance(update, dict)
    update["topic"] = "crypto_prices"
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            open_record,
            chainlink_record(
                timestamp_ms=CLOSE_MS,
                value="64980",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            ),
        ],
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_boundaries_from_finalized_manifests(
            [manifest_path],
            target=strict_target(),
            rule_url=RULE_URL,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "price_to_beat_missing"


def test_finalized_boundary_rejects_close_display_full_accuracy_mismatch(
    tmp_path: Path,
) -> None:
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            chainlink_record(),
            chainlink_record(
                timestamp_ms=CLOSE_MS,
                value="64980.00000000001",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            ),
        ],
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_boundaries_from_finalized_manifests(
            [manifest_path],
            target=strict_target(),
            rule_url=RULE_URL,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "price_value_mismatch"


@pytest.mark.parametrize("drift", ["manifest_schema", "raw_checksum"])
def test_finalized_boundary_rejects_manifest_or_checksum_drift(
    tmp_path: Path,
    drift: str,
) -> None:
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            chainlink_record(),
            chainlink_record(
                timestamp_ms=CLOSE_MS,
                value="64980",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            ),
        ],
    )
    if drift == "manifest_schema":
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["schema_version"] = "edge-lab-recorder.raw.v2"
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        raw_path = manifest_path.with_name("chainlink-boundaries.jsonl")
        raw_path.chmod(0o644)
        raw_path.write_bytes(raw_path.read_bytes() + b" \n")

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_boundaries_from_finalized_manifests(
            [manifest_path],
            target=strict_target(),
            rule_url=RULE_URL,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "source_manifest_invalid"


def test_retrospective_finalized_open_cannot_become_decision_ptb(
    tmp_path: Path,
) -> None:
    manifest_path = finalized_chainlink_manifest(
        tmp_path,
        [
            chainlink_record(),
            chainlink_record(
                timestamp_ms=CLOSE_MS,
                value="64980",
                full_accuracy_value="64980000000000000000000",
                received_at="2026-07-24T11:36:53.393239Z",
            ),
        ],
    )
    boundaries = extract_chainlink_boundaries_from_finalized_manifests(
        [manifest_path],
        target=strict_target(),
        rule_url=RULE_URL,
        first_decision_at="2026-07-24T11:31:53.393239Z",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        require_price_to_beat_for_decision(
            boundaries.open_boundary,
            decision_at="2026-07-24T11:32:00.000000Z",
        )

    assert caught.value.code == "retrospective_price_to_beat"


def test_extracts_exact_window_open_price_before_first_decision() -> None:
    record = chainlink_record()

    price_to_beat = extract_chainlink_price_to_beat(
        [record],
        source_symbol="btc/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    assert price_to_beat.source_topic == "crypto_prices_chainlink"
    assert price_to_beat.source_symbol == "btc/usd"
    assert price_to_beat.source_timestamp_ms == OPEN_MS
    assert price_to_beat.price == Decimal("64970.34876688167")
    assert (
        price_to_beat.full_accuracy_value
        == "64970348766881670000000"
    )
    assert price_to_beat.raw_record_id == record["record_id"]
    assert price_to_beat.received_at == "2026-07-24T11:31:53.393239Z"
    assert price_to_beat.first_decision_at == "2026-07-24T11:31:54.000000Z"
    assert price_to_beat.retrospective is False
    assert price_to_beat.available_at_first_decision is True


def test_marks_price_received_at_or_after_first_decision_as_retrospective() -> None:
    price_to_beat = extract_chainlink_price_to_beat(
        [chainlink_record()],
        source_symbol="btc/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:53.393239Z",
    )

    assert price_to_beat.retrospective is True
    assert price_to_beat.available_at_first_decision is False


def test_retrospective_price_cannot_be_rebound_to_a_later_decision() -> None:
    price_to_beat = extract_chainlink_price_to_beat(
        [chainlink_record()],
        source_symbol="btc/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:53.393239Z",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        require_price_to_beat_for_decision(
            price_to_beat,
            decision_at="2026-07-24T11:32:00.000000Z",
        )

    assert caught.value.code == "retrospective_price_to_beat"


def test_rejects_decision_that_precedes_local_price_receipt() -> None:
    price_to_beat = extract_chainlink_price_to_beat(
        [chainlink_record()],
        source_symbol="btc/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        require_price_to_beat_for_decision(
            price_to_beat,
            decision_at="2026-07-24T11:31:53.300000Z",
        )

    assert caught.value.code == "price_to_beat_not_yet_received"


def test_rejects_display_value_that_differs_from_full_accuracy_value() -> None:
    record = chainlink_record(value="64970.34876688168")

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [record],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "price_value_mismatch"


def test_accepts_canonical_binary64_display_but_uses_full_accuracy_price() -> None:
    record = chainlink_record(
        symbol="eth/usd",
        value="1877.7647104882285",
        full_accuracy_value="1877764710488228400000",
    )

    price_to_beat = extract_chainlink_price_to_beat(
        [record],
        source_symbol="eth/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    assert price_to_beat.price == Decimal("1877.7647104882284")
    assert price_to_beat.display_value == "1877.7647104882285"
    assert price_to_beat.display_encoding == "canonical_binary64"


def test_rejects_non_integer_full_accuracy_encoding() -> None:
    record = chainlink_record(
        full_accuracy_value="64970348766881670000000.0",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [record],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "price_encoding_invalid"


def test_rejects_duplicate_raw_record_instead_of_silently_deduplicating() -> None:
    record = chainlink_record()

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [record, record],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "duplicate_record_id"


def test_rejects_conflicting_prices_at_the_same_source_timestamp() -> None:
    first = chainlink_record()
    conflicting = chainlink_record(
        value="64971",
        full_accuracy_value="64971000000000000000000",
        received_at="2026-07-24T11:31:53.500000Z",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [first, conflicting],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "conflicting_price_to_beat"


def test_fails_closed_when_target_record_schema_version_drifts() -> None:
    record = deepcopy(chainlink_record())
    recorder = record["payload"]
    assert isinstance(recorder, dict)
    recorder["schema_version"] = "rtds.crypto_prices_chainlink.update.v2"
    record["record_id"] = canonical_record_id(record)

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [record],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "schema_drift"


def test_rejects_non_btc_eth_chainlink_target() -> None:
    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [chainlink_record(symbol="doge/usd")],
            source_symbol="doge/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "unsupported_source_symbol"


def test_rejects_non_integer_window_open_target() -> None:
    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [chainlink_record(timestamp_ms=True)],
            source_symbol="btc/usd",
            opens_at_ms=True,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "target_window_invalid"


def test_rejects_divergent_outer_and_recorder_receive_vintages() -> None:
    record = deepcopy(chainlink_record())
    recorder = record["payload"]
    assert isinstance(recorder, dict)
    recorder["received_at"] = "2026-07-24T11:31:52.000000Z"
    record["record_id"] = canonical_record_id(record)

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [record],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "schema_drift"


def test_rejects_boundary_received_before_its_inner_source_timestamp() -> None:
    record = chainlink_record(
        received_at="2026-07-24T11:31:51.999000Z",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [record],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "source_time_conflict"


def test_compares_full_accuracy_value_without_decimal_context_rounding() -> None:
    value = "12345678901234567890.123456789012345678"
    record = chainlink_record(
        value=value,
        full_accuracy_value="12345678901234567890123456789012345678",
    )

    price_to_beat = extract_chainlink_price_to_beat(
        [record],
        source_symbol="btc/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    assert price_to_beat.price == Decimal(value)


def test_selects_only_the_requested_symbol_at_the_exact_open_timestamp() -> None:
    exact = chainlink_record()
    records = [
        chainlink_record(symbol="eth/usd"),
        chainlink_record(timestamp_ms=OPEN_MS - 1_000),
        exact,
    ]

    price_to_beat = extract_chainlink_price_to_beat(
        records,
        source_symbol="btc/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    assert price_to_beat.raw_record_id == exact["record_id"]


def test_rejects_adjacent_second_when_exact_open_timestamp_is_missing() -> None:
    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [chainlink_record(timestamp_ms=OPEN_MS + 1_000)],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "price_to_beat_missing"


def test_rejects_raw_envelope_with_noncanonical_record_id() -> None:
    record = chainlink_record()
    record["record_id"] = "0" * 64

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [record],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "record_id_mismatch"


def test_rejects_repeated_ptb_value_from_distinct_raw_observations() -> None:
    first = chainlink_record()
    repeated = chainlink_record(
        received_at="2026-07-24T11:31:53.500000Z",
    )

    with pytest.raises(PriceToBeatRejection) as caught:
        extract_chainlink_price_to_beat(
            [first, repeated],
            source_symbol="btc/usd",
            opens_at_ms=OPEN_MS,
            first_decision_at="2026-07-24T11:31:54.000000Z",
        )

    assert caught.value.code == "duplicate_price_to_beat"


def test_allows_later_decision_to_bind_already_received_ptb() -> None:
    price_to_beat = extract_chainlink_price_to_beat(
        [chainlink_record()],
        source_symbol="btc/usd",
        opens_at_ms=OPEN_MS,
        first_decision_at="2026-07-24T11:31:54.000000Z",
    )

    bound = require_price_to_beat_for_decision(
        price_to_beat,
        decision_at="2026-07-24T11:31:54.000000Z",
    )

    assert bound is price_to_beat
