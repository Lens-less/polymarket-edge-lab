"""Offline capture-to-latency experiment tests."""

from __future__ import annotations

import json
from decimal import localcontext
from pathlib import Path

import pytest

import src.edge_lab.latency_capture_experiment as experiment_module
from src.edge_lab.data_store import CaptureStore, canonical_record_id
from src.edge_lab.latency_capture_experiment import (
    CaptureLatencyConfig,
    run_capture_latency_experiment,
)
from src.edge_lab.latency_capture_experiment_cli import main


CONDITION_ID = (
    "0x60cb891831b1e0d8f6834f35f150ec058d99075d62b75804ac7ce97b660a4dab"
)
UP_TOKEN = "up-token"
DOWN_TOKEN = "down-token"


def _write_record(
    root: Path,
    source: str,
    *,
    received_at: str,
    monotonic_ns: int,
    event_type: str,
    payload: dict[str, object],
    record_id: str,
    session_id: str = "session-1",
    connection_id: str | None = "connection-1",
    kind: str = "data",
    frame_index: int | None = 0,
    replay_eligible: bool | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    directory = root / "raw" / source
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "capture.jsonl"
    envelope = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": source,
        "received_at": received_at,
        "event_at": None,
        "sequence": None,
        "payload": {
            "schema_version": f"{source}.{event_type}.v1",
            "source": source,
            "received_at": received_at,
            "event_at": None,
            "sequence": None,
            "session_id": session_id,
            "connection_id": connection_id,
            "kind": kind,
            "event_type": event_type,
            "monotonic_ns": monotonic_ns,
            "frame_index": frame_index,
            "payload": payload,
        },
    }
    if replay_eligible is not None:
        envelope["payload"]["replay_eligible"] = replay_eligible
    if detail is not None:
        envelope["payload"]["detail"] = detail
    envelope["record_id"] = canonical_record_id(envelope)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, sort_keys=True))
        handle.write("\n")


def _seed_metadata(
    root: Path,
    *,
    session_id: str = "session-1",
    up_token: str = UP_TOKEN,
    down_token: str = DOWN_TOKEN,
    reverse_token_order: bool = False,
) -> None:
    tokens = [
        {"o": "Up", "t": up_token},
        {"o": "Down", "t": down_token},
    ]
    if reverse_token_order:
        tokens.reverse()
    _write_record(
        root,
        "clob_http",
        received_at="2026-07-24T10:00:00.000000Z",
        monotonic_ns=1,
        event_type="clob_snapshot",
        payload={
            "responses": [
                {
                    "resource": "clob_market",
                    "request_key": CONDITION_ID,
                    "raw_json": {
                        "c": CONDITION_ID,
                        "t": tokens,
                    },
                }
            ]
        },
        record_id="metadata",
        session_id=session_id,
        connection_id=None,
        kind="snapshot",
    )
    _write_record(
        root,
        "rules_http",
        received_at="2026-07-24T10:00:00.100000Z",
        monotonic_ns=2,
        event_type="rules_snapshot",
        payload={
            "responses": [
                {
                    "resource": "resolution_rules",
                    "request_key": "3030558",
                    "raw_json": {
                        "id": "3030558",
                        "conditionId": CONDITION_ID,
                        "closed": False,
                        "question": "Bitcoin Up or Down?",
                        "description": "Resolves using two Binance close candles.",
                        "clobTokenIds": json.dumps([UP_TOKEN, DOWN_TOKEN]),
                        "outcomes": json.dumps(["Up", "Down"]),
                    },
                }
            ]
        },
        record_id="rules",
        session_id=session_id,
        connection_id=None,
        kind="snapshot",
    )


def _seed_source(
    root: Path,
    *,
    session_id: str = "session-1",
    connection_id: str = "source-connection-1",
) -> None:
    _seed_ws_lifecycle(
        root,
        source="rtds_ws",
        session_id=session_id,
        connection_id=connection_id,
        connected_at="2026-07-24T10:00:00.800000Z",
        connected_monotonic_ns=800_000_000,
        heartbeat_at="2026-07-24T10:00:00.900000Z",
        heartbeat_monotonic_ns=900_000_000,
        heartbeat_interval_seconds=5,
    )
    for index, price in enumerate(("100", "101", "102", "101"), start=1):
        milliseconds = index * 1_000
        _write_record(
            root,
            "rtds_ws",
            received_at=f"2026-07-24T10:00:0{index}.000000Z",
            monotonic_ns=milliseconds * 1_000_000,
            event_type="crypto_prices.update",
            payload={
                "topic": "crypto_prices",
                "type": "update",
                "payload": {
                    "symbol": "btcusdt",
                    "full_accuracy_value": price,
                    "timestamp": milliseconds,
                },
            },
            record_id=f"source-{index}",
            session_id=session_id,
            connection_id=connection_id,
        )
    _write_record(
        root,
        "rtds_ws",
        received_at="2026-07-24T10:00:05.000000Z",
        monotonic_ns=5_000_000_000,
        event_type="heartbeat",
        payload={},
        record_id=f"rtds_ws-{session_id}-{connection_id}-ending-heartbeat",
        session_id=session_id,
        connection_id=connection_id,
        kind="lifecycle",
        detail={"interval_seconds": 5, "message": "PING"},
    )


def _seed_lifecycle_record(root: Path) -> None:
    target = root / "raw" / "rtds_ws" / "capture.jsonl"
    envelope = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "rtds_ws",
        "received_at": "2026-07-24T10:00:00.500000Z",
        "event_at": None,
        "sequence": None,
        "payload": {
            "schema_version": "edge-lab-recorder.lifecycle.v1",
            "source": "rtds_ws",
            "received_at": "2026-07-24T10:00:00.500000Z",
            "event_at": None,
            "sequence": None,
            "session_id": "session-1",
            "connection_id": "connection-1",
            "kind": "lifecycle",
            "event_type": "heartbeat",
            "monotonic_ns": 500_000_000,
            "detail": {"message": "PING"},
        },
    }
    envelope["record_id"] = canonical_record_id(envelope)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(envelope, sort_keys=True))
        handle.write("\n")


def _seed_ws_lifecycle(
    root: Path,
    *,
    source: str,
    session_id: str,
    connection_id: str,
    connected_at: str,
    connected_monotonic_ns: int,
    heartbeat_at: str,
    heartbeat_monotonic_ns: int,
    heartbeat_interval_seconds: object,
) -> None:
    _write_record(
        root,
        source,
        received_at=connected_at,
        monotonic_ns=connected_monotonic_ns,
        event_type="connected",
        payload={},
        record_id=f"{source}-{session_id}-{connection_id}-connected",
        session_id=session_id,
        connection_id=connection_id,
        kind="lifecycle",
        detail={"attempt": 1},
    )
    _write_record(
        root,
        source,
        received_at=heartbeat_at,
        monotonic_ns=heartbeat_monotonic_ns,
        event_type="heartbeat",
        payload={},
        record_id=f"{source}-{session_id}-{connection_id}-heartbeat",
        session_id=session_id,
        connection_id=connection_id,
        kind="lifecycle",
        detail={
            "interval_seconds": heartbeat_interval_seconds,
            "message": "PING",
        },
    )


def _rewrite_heartbeat_intervals(
    root: Path,
    value: object,
    *,
    sources: tuple[str, ...] = ("rtds_ws", "clob_market_ws"),
) -> None:
    for source in sources:
        raw_path = root / "raw" / source / "capture.jsonl"
        rows = [
            json.loads(line)
            for line in raw_path.read_text(encoding="utf-8").splitlines()
        ]
        for row in rows:
            recorder = row["payload"]
            if (
                recorder["kind"] == "lifecycle"
                and recorder["event_type"] == "heartbeat"
            ):
                recorder["detail"]["interval_seconds"] = value
                row["record_id"] = canonical_record_id(row)
        raw_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )


def _seed_quotes(
    root: Path,
    *,
    session_id: str = "session-1",
    connection_id: str = "quote-connection-1",
) -> None:
    _seed_ws_lifecycle(
        root,
        source="clob_market_ws",
        session_id=session_id,
        connection_id=connection_id,
        connected_at="2026-07-24T10:00:00.700000Z",
        connected_monotonic_ns=700_000_000,
        heartbeat_at="2026-07-24T10:00:00.800000Z",
        heartbeat_monotonic_ns=800_000_000,
        heartbeat_interval_seconds=10,
    )
    # Deliberately append out of receive order; the public seam must restore the
    # recorder's receive/monotonic chronology before assigning capture_seq.
    for index, (millisecond, midpoint, event_type) in enumerate(
        (
            (4_900, "0.55", "best_bid_ask"),
            (900, "0.50", "book"),
            (3_900, "0.60", "price_change"),
            (1_900, "0.50", "price_change"),
            (2_900, "0.55", "best_bid_ask"),
        )
    ):
        bid = str(float(midpoint) - 0.01)
        ask = str(float(midpoint) + 0.01)
        received_at = (
            f"2026-07-24T10:00:0{millisecond // 1000}."
            f"{millisecond % 1000:03d}000Z"
        )
        if event_type == "price_change":
            payload: dict[str, object] = {
                "event_type": event_type,
                "market": CONDITION_ID,
                "timestamp": str(millisecond),
                "price_changes": [
                    {
                        "asset_id": UP_TOKEN,
                        "best_bid": bid,
                        "best_ask": ask,
                    },
                    {
                        "asset_id": DOWN_TOKEN,
                        "best_bid": str(1 - float(ask)),
                        "best_ask": str(1 - float(bid)),
                    },
                ],
            }
            _write_record(
                root,
                "clob_market_ws",
                received_at=received_at,
                monotonic_ns=millisecond * 1_000_000,
                event_type=event_type,
                payload=payload,
                record_id=f"quote-{index}",
                session_id=session_id,
                connection_id=connection_id,
            )
            continue

        for token_index, token_id in enumerate((UP_TOKEN, DOWN_TOKEN)):
            token_bid, token_ask = (
                (bid, ask)
                if token_id == UP_TOKEN
                else (str(1 - float(ask)), str(1 - float(bid)))
            )
            if event_type == "book":
                payload = {
                    "event_type": event_type,
                    "market": CONDITION_ID,
                    "asset_id": token_id,
                    "timestamp": str(millisecond),
                    "bids": [
                        {"price": token_bid, "size": "10"},
                        {"price": "0.01", "size": "20"},
                    ],
                    "asks": [
                        {"price": "0.99", "size": "20"},
                        {"price": token_ask, "size": "10"},
                    ],
                    "last_trade_price": midpoint,
                }
            else:
                payload = {
                    "event_type": event_type,
                    "market": CONDITION_ID,
                    "asset_id": token_id,
                    "timestamp": str(millisecond),
                    "best_bid": token_bid,
                    "best_ask": token_ask,
                }
            _write_record(
                root,
                "clob_market_ws",
                received_at=received_at,
                monotonic_ns=(
                    millisecond * 1_000_000
                    if event_type == "book"
                    else millisecond * 1_000_000 + token_index
                ),
                event_type=event_type,
                payload=payload,
                record_id=f"quote-{index}-{token_index}",
                session_id=session_id,
                connection_id=connection_id,
                frame_index=token_index,
            )


def _write_test_manifests(root: Path) -> None:
    for raw_path in root.glob("raw/*/*.jsonl"):
        rows = [
            json.loads(line)
            for line in raw_path.read_text(encoding="utf-8").splitlines()
        ]
        manifest_path = raw_path.with_suffix(".manifest.json")
        raw_path.unlink()
        manifest_path.unlink(missing_ok=True)
        writer = CaptureStore(root).open_raw_batch(
            source=raw_path.parent.name,
            batch_id=raw_path.stem,
            schema_version="edge-lab-recorder.raw.v1",
        )
        for row in rows:
            writer.append(
                received_at=row["received_at"],
                event_at=row.get("event_at"),
                sequence=row.get("sequence"),
                payload=row["payload"],
            )
        writer.finalize(finalized_at="2026-07-24T10:01:00.000000Z")
        # Several adversarial tests intentionally mutate and re-finalize the
        # synthetic batch. Real capture batches remain immutable.
        raw_path.chmod(0o644)
        manifest_path.chmod(0o644)


def test_runner_uses_receive_order_and_reports_missing_evidence(tmp_path: Path) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:02.500000Z",
        monotonic_ns=2_500_000_000,
        event_type="crypto_prices.update",
        payload={
            "topic": "crypto_prices",
            "type": "update",
            "payload": {
                "symbol": "btcusdt",
                "full_accuracy_value": "999999",
                "timestamp": 2_500,
            },
        },
        record_id="quarantined-source",
        kind="quarantine",
        replay_eligible=False,
    )
    _seed_lifecycle_record(tmp_path)
    _seed_quotes(tmp_path)
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0, 1_000),
            min_capture_duration_ms=60_000,
            min_source_observations=10,
            min_lag_samples=10,
        )
    )

    assert result["status"] == "insufficient_data"
    assert result["safety"] == {
        "network_requests": False,
        "orders_submitted": False,
        "credentials_accessed": False,
        "fills_simulated": False,
        "backtest_performed": False,
    }
    assert result["coverage"]["source_observations"] == 4
    assert result["coverage"]["quarantined_records_excluded"] == 1
    assert result["coverage"]["quote_observations"] == 10
    assert result["coverage"]["non_data_records"] == 9
    assert result["coverage"]["observed_target_data_union_span_ms"] == 4_000
    assert (
        result["coverage"]["observed_target_data_overlap_span_ms"]
        == 3_000
    )
    assert result["coverage"]["capture_duration_ms"] == 3_000
    assert (
        result["coverage"]["selected_continuous_online_overlap_ms"]
        == 4_100
    )
    assert result["coverage"]["lifecycle_coverage"][
        "selected_overlap_segment_ms"
    ]["duration"] == 4_100
    assert result["integrity"]["finalized_files_checked"] == 4
    assert result["integrity"]["manifest_complete"] is True
    assert result["integrity"]["missing_manifests"] == []
    assert result["integrity"]["ignored_partial_files"] == []
    assert result["target"]["source_binding"] == {
        "recorder_source": "rtds_ws",
        "topic": "crypto_prices",
        "event_type": "crypto_prices.update",
        "symbol": "btcusdt",
        "derived_source_name": "polymarket_rtds.crypto_prices",
    }
    assert result["target"]["token_metadata_provenance"]["cross_session"] is False
    assert result["coverage"]["quote_event_types"] == {
        "best_bid_ask": 4,
        "book": 2,
        "price_change": 4,
    }
    assert result["ordering"]["basis"] == [
        "received_at",
        "monotonic_ns",
        "connection_id",
        "frame_index",
        "record_id",
    ]
    assert result["ordering"]["monotonic_inversions"] == 0
    assert result["ordering"]["monotonic_ties"] == 1
    assert result["evidence_availability"]["clock_probes"]["available"] is False
    assert result["evidence_availability"]["price_to_beat"]["available"] is False
    assert result["evidence_availability"]["settlement"]["available"] is False
    assert result["evidence_availability"]["last_trade_events"]["available"] is False
    assert (
        result["evidence_availability"]["last_trade_events"][
            "embedded_book_values_ignored"
        ]
        == 2
    )
    up = next(series for series in result["series"] if series["outcome"] == "Up")
    assert up["lag_scores"][1]["lag_ms"] == 1_000
    # The final source move cannot receive a 1s label because the capture ends
    # at 4.9s. Carrying the last quote past the observed horizon would invent a
    # flat future state.
    assert up["lag_scores"][1]["samples"] == 2
    assert up["lag_scores"][1]["deduplicated_quote_pair_samples"] == 2
    assert up["lag_scores"][1]["independent_samples"] == 1
    assert result["sample_gate"]["max_independent_samples_at_any_lag"] == 2
    assert result["sample_gate"][
        "max_deduplicated_quote_pair_samples_at_any_lag"
    ] == max(
        score["deduplicated_quote_pair_samples"]
        for series in result["series"]
        for score in series["lag_scores"]
    )
    assert {
        item["path"] for item in result["method"]["code_identity"]["files"]
    } == {
        "src/edge_lab/latency_capture_experiment.py",
        "src/edge_lab/latency.py",
        "src/edge_lab/data_store.py",
        "src/edge_lab/data_manifest.py",
    }
    assert (
        result["rejection_counts"]["lead_lag"][
            "lag_horizon_after_observation_end"
        ]
        == 2
    )
    assert "missing_clock_probes" in result["rejection_reasons"]
    assert "capture_duration_below_minimum" in result["rejection_reasons"]
    assert (
        result["rejection_counts"]["parser"].get(
            "malformed_capture_record",
            0,
        )
        == 0
    )


def test_cli_is_offline_and_writes_one_atomic_json(
    tmp_path: Path,
    capsys: object,
) -> None:
    data_root = tmp_path / "capture"
    output = tmp_path / "reports" / "latency.json"
    _seed_metadata(data_root)
    _seed_source(data_root)
    _seed_quotes(data_root)
    _write_test_manifests(data_root)
    inputs_before = sorted(
        path.relative_to(data_root)
        for path in data_root.rglob("*")
        if path.is_file()
    )

    exit_code = main(
        [
            "--data-root",
            str(data_root),
            "--condition-id",
            CONDITION_ID,
            "--lag-grid-ms",
            "0",
            "1000",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["safety"]["network_requests"] is False
    assert written["safety"]["orders_submitted"] is False
    assert not list(output.parent.glob("*.partial"))
    assert inputs_before == sorted(
        path.relative_to(data_root)
        for path in data_root.rglob("*")
        if path.is_file()
    )
    printed = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert printed["result_path"] == str(output.resolve())
    assert printed["status"] == "insufficient_data"


def test_experiment_identity_is_content_addressed_not_path_addressed(
    tmp_path: Path,
) -> None:
    roots = (tmp_path / "first", tmp_path / "second")
    results = []
    for root in roots:
        _seed_metadata(root)
        _seed_source(root)
        _seed_quotes(root)
        _write_test_manifests(root)
        results.append(
            run_capture_latency_experiment(
                CaptureLatencyConfig(
                    data_root=root,
                    condition_id=CONDITION_ID,
                    lag_grid_ms=(0, 1_000),
                )
            )
        )

    assert results[0]["input_digest_sha256"] == results[1][
        "input_digest_sha256"
    ]
    assert results[0]["experiment_id"] == results[1]["experiment_id"]


def test_runner_never_stitches_source_and_quotes_across_sessions(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path, session_id="source-session")
    _seed_quotes(tmp_path, session_id="quote-session")
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0, 1_000),
        )
    )

    assert result["status"] == "rejected"
    assert result["coverage"]["selected_analysis_session_id"] is None
    assert result["coverage"]["source_observations"] == 0
    assert result["coverage"]["quote_observations"] == 0
    assert "missing_common_capture_session" in result["rejection_reasons"]


def test_submillisecond_capture_end_is_censored_conservatively(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_ws_lifecycle(
        tmp_path,
        source="rtds_ws",
        session_id="session-1",
        connection_id="connection-1",
        connected_at="2026-07-24T10:00:00.700000Z",
        connected_monotonic_ns=700_000_000,
        heartbeat_at="2026-07-24T10:00:00.800000Z",
        heartbeat_monotonic_ns=800_000_000,
        heartbeat_interval_seconds=5,
    )
    _seed_ws_lifecycle(
        tmp_path,
        source="clob_market_ws",
        session_id="session-1",
        connection_id="connection-1",
        connected_at="2026-07-24T10:00:00.700000Z",
        connected_monotonic_ns=700_000_000,
        heartbeat_at="2026-07-24T10:00:00.800000Z",
        heartbeat_monotonic_ns=800_000_000,
        heartbeat_interval_seconds=10,
    )
    for index, (received_at, price) in enumerate(
        (
            ("2026-07-24T10:00:01.000500Z", "100"),
            ("2026-07-24T10:00:02.000500Z", "101"),
        )
    ):
        _write_record(
            tmp_path,
            "rtds_ws",
            received_at=received_at,
            monotonic_ns=1_000_500_000 + index * 1_000_000_000,
            event_type="crypto_prices.update",
            payload={
                "topic": "crypto_prices",
                "type": "update",
                "payload": {
                    "symbol": "btcusdt",
                    "full_accuracy_value": price,
                    "timestamp": 1_000 + index * 1_000,
                },
            },
            record_id=f"fractional-source-{index}",
        )
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:03.100000Z",
        monotonic_ns=3_100_000_000,
        event_type="heartbeat",
        payload={},
        record_id="fractional-source-ending-heartbeat",
        kind="lifecycle",
        detail={"interval_seconds": 5, "message": "PING"},
    )
    for index, received_at in enumerate(
        (
            "2026-07-24T10:00:00.900000Z",
            "2026-07-24T10:00:01.900000Z",
            "2026-07-24T10:00:03.000400Z",
        )
    ):
        _write_record(
            tmp_path,
            "clob_market_ws",
            received_at=received_at,
            monotonic_ns=900_000_000 + index * 1_000_000_000,
            event_type="price_change",
            payload={
                "event_type": "price_change",
                "market": CONDITION_ID,
                "timestamp": str(900 + index * 1_000),
                "price_changes": [
                    {
                        "asset_id": UP_TOKEN,
                        "best_bid": str(0.49 + index * 0.05),
                        "best_ask": str(0.51 + index * 0.05),
                    },
                    {
                        "asset_id": DOWN_TOKEN,
                        "best_bid": str(0.49 - index * 0.05),
                        "best_ask": str(0.51 - index * 0.05),
                    },
                ],
            },
            record_id=f"fractional-quote-{index}",
        )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(1_000,),
        )
    )

    up = next(series for series in result["series"] if series["outcome"] == "Up")
    assert up["lag_scores"][0]["samples"] == 0
    assert (
        result["rejection_counts"]["lead_lag"][
            "lag_horizon_after_observation_end"
        ]
        == 2
    )


def test_runner_selects_one_overlapping_connection_pair(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path, connection_id="source-primary")
    _seed_quotes(tmp_path, connection_id="quote-primary")
    for index, received_at in enumerate(
        (
            "2026-07-24T10:00:02.250000Z",
            "2026-07-24T10:00:02.750000Z",
        )
    ):
        _write_record(
            tmp_path,
            "rtds_ws",
            received_at=received_at,
            monotonic_ns=2_250_000_000 + index * 500_000_000,
            event_type="crypto_prices.update",
            payload={
                "topic": "crypto_prices",
                "type": "update",
                "payload": {
                    "symbol": "btcusdt",
                    "full_accuracy_value": str(900 + index),
                    "timestamp": 2_250 + index * 500,
                },
            },
            record_id=f"reconnected-source-{index}",
            connection_id="source-reconnected",
        )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0, 1_000),
        )
    )

    assert result["coverage"]["source_observations"] == 4
    assert (
        result["coverage"]["selected_source_connection_id"]
        == "source-primary"
    )
    assert (
        result["coverage"]["selected_quote_connection_id"]
        == "quote-primary"
    )


def test_cli_cannot_overwrite_raw_capture_or_existing_report(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "capture"
    _seed_metadata(data_root)
    _seed_source(data_root)
    _seed_quotes(data_root)
    _write_test_manifests(data_root)
    raw_target = data_root / "raw" / "rtds_ws" / "capture.jsonl"
    raw_before = raw_target.read_bytes()

    with pytest.raises(ValueError, match="outside the capture data root"):
        main(
            [
                "--data-root",
                str(data_root),
                "--output",
                str(raw_target),
            ]
        )
    assert raw_target.read_bytes() == raw_before

    report = tmp_path / "latency.json"
    report.write_text("keep-me", encoding="utf-8")
    with pytest.raises(FileExistsError):
        main(
            [
                "--data-root",
                str(data_root),
                "--output",
                str(report),
            ]
        )
    assert report.read_text(encoding="utf-8") == "keep-me"


def test_prior_consistent_static_metadata_can_fill_selected_ws_session(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path, session_id="old-session")
    _seed_source(tmp_path, session_id="selected-session")
    _seed_quotes(tmp_path, session_id="selected-session")
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0, 1_000),
        )
    )

    assert result["status"] == "insufficient_data"
    assert len(result["series"]) == 2
    provenance = result["target"]["token_metadata_provenance"]
    assert provenance["cross_session"] is True
    assert provenance["all_snapshots_precede_analysis_window"] is True
    assert provenance["snapshot_session_ids"] == ["old-session"]
    assert "missing_target_token_metadata" not in result["rejection_reasons"]


def test_conflicting_prior_static_metadata_is_rejected(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path, session_id="metadata-session-a")
    _seed_metadata(
        tmp_path,
        session_id="metadata-session-b",
        up_token="different-up-token",
        down_token="different-down-token",
    )
    _seed_source(tmp_path, session_id="selected-session")
    _seed_quotes(tmp_path, session_id="selected-session")
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0, 1_000),
        )
    )

    assert result["status"] == "rejected"
    assert result["series"] == []
    assert (
        result["rejection_counts"]["parser"][
            "conflicting_target_token_metadata"
        ]
        == 1
    )
    assert "missing_target_token_metadata" in result["rejection_reasons"]


def test_binary_labels_bind_to_outcome_names_not_token_array_position(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path, reverse_token_order=True)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert {
        token["outcome"]: token["internal_binary_label"]
        for token in result["target"]["tokens"]
    } == {"Up": "YES", "Down": "NO"}


def test_independent_gate_reuses_neither_quote_nor_overlapping_horizon(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_ws_lifecycle(
        tmp_path,
        source="rtds_ws",
        session_id="session-1",
        connection_id="source-independent",
        connected_at="2026-07-24T10:00:01.000000Z",
        connected_monotonic_ns=1_000_000_000,
        heartbeat_at="2026-07-24T10:00:01.050000Z",
        heartbeat_monotonic_ns=1_050_000_000,
        heartbeat_interval_seconds=5,
    )
    _seed_ws_lifecycle(
        tmp_path,
        source="clob_market_ws",
        session_id="session-1",
        connection_id="quote-independent",
        connected_at="2026-07-24T10:00:01.000000Z",
        connected_monotonic_ns=1_000_000_000,
        heartbeat_at="2026-07-24T10:00:01.025000Z",
        heartbeat_monotonic_ns=1_025_000_000,
        heartbeat_interval_seconds=10,
    )
    for index, price in enumerate(("100", "101", "102"), start=1):
        millisecond = 1_000 + index * 100
        _write_record(
            tmp_path,
            "rtds_ws",
            received_at=(
                f"2026-07-24T10:00:01.{index}00000Z"
            ),
            monotonic_ns=millisecond * 1_000_000,
            event_type="crypto_prices.update",
            payload={
                "topic": "crypto_prices",
                "type": "update",
                "payload": {
                    "symbol": "btcusdt",
                    "full_accuracy_value": price,
                    "timestamp": millisecond,
                },
            },
            record_id=f"source-independent-{index}",
            connection_id="source-independent",
        )
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:02.400000Z",
        monotonic_ns=2_400_000_000,
        event_type="heartbeat",
        payload={},
        record_id="source-independent-ending-heartbeat",
        connection_id="source-independent",
        kind="lifecycle",
        detail={"interval_seconds": 5, "message": "PING"},
    )
    for index, (millisecond, midpoint) in enumerate(
        ((1_050, 0.50), (2_000, 0.55), (2_300, 0.60)),
    ):
        _write_record(
            tmp_path,
            "clob_market_ws",
            received_at=(
                "2026-07-24T10:00:"
                f"{millisecond // 1_000:02d}.{millisecond % 1_000:03d}000Z"
            ),
            monotonic_ns=millisecond * 1_000_000,
            event_type="price_change",
            payload={
                "event_type": "price_change",
                "market": CONDITION_ID,
                "timestamp": str(millisecond),
                "price_changes": [
                    {
                        "asset_id": UP_TOKEN,
                        "best_bid": str(midpoint - 0.01),
                        "best_ask": str(midpoint + 0.01),
                    },
                    {
                        "asset_id": DOWN_TOKEN,
                        "best_bid": str(0.99 - midpoint),
                        "best_ask": str(1.01 - midpoint),
                    },
                ],
            },
            record_id=f"quote-independent-{index}",
            connection_id="quote-independent",
        )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(1_000,),
            min_lag_samples=2,
        )
    )

    up = result["series"][0]["lag_scores"][0]
    assert up["samples"] == 2
    assert up["deduplicated_quote_pair_samples"] == 1
    assert up["independent_samples"] == 1
    assert (
        "independent_lag_samples_below_minimum"
        in result["rejection_reasons"]
    )


def test_pin_from_rejects_empty_changed_or_differently_configured_replay(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "capture"
    _seed_metadata(data_root)
    _seed_source(data_root)
    _seed_quotes(data_root)
    _write_test_manifests(data_root)
    original = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=data_root,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0, 1_000),
        )
    )
    prior = tmp_path / "prior.json"
    prior.write_text(json.dumps(original), encoding="utf-8")
    active_partial = (
        data_root
        / "raw"
        / "rtds_ws"
        / "unbound-active.jsonl.partial"
    )
    active_partial.write_text("must not be inventoried by pinned replay\n")

    replay = tmp_path / "replay.json"
    assert (
        main(
            [
                "--data-root",
                str(data_root),
                "--condition-id",
                CONDITION_ID,
                "--lag-grid-ms",
                "0",
                "1000",
                "--pin-from",
                str(prior),
                "--output",
                str(replay),
            ]
        )
        == 0
    )
    replay_result = json.loads(replay.read_text(encoding="utf-8"))
    assert replay_result["input_digest_sha256"] == original[
        "input_digest_sha256"
    ]
    assert replay_result["integrity"]["ignored_partial_files"] == []

    with pytest.raises(ValueError, match="configuration"):
        main(
            [
                "--data-root",
                str(data_root),
                "--condition-id",
                CONDITION_ID,
                "--lag-grid-ms",
                "0",
                "--pin-from",
                str(prior),
                "--output",
                str(tmp_path / "wrong-config.json"),
            ]
        )

    _write_record(
        data_root,
        "rtds_ws",
        received_at="2026-07-24T10:00:05.000000Z",
        monotonic_ns=5_000_000_000,
        event_type="crypto_prices.update",
        payload={
            "topic": "crypto_prices",
            "type": "update",
            "payload": {
                "symbol": "btcusdt",
                "full_accuracy_value": "103",
                "timestamp": 5_000,
            },
        },
        record_id="changed-pinned-input",
        connection_id="source-connection-1",
    )
    _write_test_manifests(data_root)
    changed_output = tmp_path / "changed.json"
    with pytest.raises(ValueError, match="exact replay"):
        main(
            [
                "--data-root",
                str(data_root),
                "--condition-id",
                CONDITION_ID,
                "--lag-grid-ms",
                "0",
                "1000",
                "--pin-from",
                str(prior),
                "--output",
                str(changed_output),
            ]
        )
    assert not changed_output.exists()

    empty_prior = tmp_path / "empty-prior.json"
    empty = dict(original)
    empty["frozen_input"] = dict(original["frozen_input"])
    empty["frozen_input"]["pinned_batches"] = []
    empty_prior.write_text(json.dumps(empty), encoding="utf-8")
    with pytest.raises(ValueError, match="at least one"):
        main(
            [
                "--data-root",
                str(data_root),
                "--pin-from",
                str(empty_prior),
                "--output",
                str(tmp_path / "empty.json"),
            ]
        )


def test_pinned_paths_are_restricted_to_supported_raw_sources(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "other" / "capture.jsonl"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="supported raw source"):
        run_capture_latency_experiment(
            CaptureLatencyConfig(
                data_root=tmp_path,
                pinned_raw_paths=("other/capture.jsonl",),
            )
        )


def test_ws_data_without_frame_index_is_integrity_fatal(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:02.500000Z",
        monotonic_ns=2_500_000_000,
        event_type="crypto_prices.update",
        payload={
            "topic": "crypto_prices",
            "type": "update",
            "payload": {
                "symbol": "btcusdt",
                "full_accuracy_value": "103",
                "timestamp": 2_500,
            },
        },
        record_id="missing-frame-index",
        connection_id="source-connection-1",
        frame_index=None,
    )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["status"] == "rejected"
    assert (
        result["rejection_counts"]["parser"]["malformed_capture_record"]
        == 1
    )
    assert "capture_integrity_failed" in result["rejection_reasons"]


def test_decimal_context_is_fixed_and_bound_to_experiment_identity(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _rewrite_heartbeat_intervals(
        tmp_path,
        "5.0000000000000000000001",
    )
    _write_test_manifests(tmp_path)
    config = CaptureLatencyConfig(
        data_root=tmp_path,
        condition_id=CONDITION_ID,
        lag_grid_ms=(0, 1_000),
    )

    with localcontext() as context:
        context.prec = 10
        low_precision = run_capture_latency_experiment(config)
    with localcontext() as context:
        context.prec = 28
        default_precision = run_capture_latency_experiment(config)

    assert low_precision["series"] == default_precision["series"]
    assert low_precision["coverage"]["lifecycle_coverage"] == (
        default_precision["coverage"]["lifecycle_coverage"]
    )
    assert low_precision["coverage"]["lifecycle_coverage"][
        "source_connection"
    ]["declared_heartbeat_intervals_ms"] == [5_001]
    assert low_precision["input_digest_sha256"] == default_precision[
        "input_digest_sha256"
    ]
    assert low_precision["method"]["runtime_identity"] == {
        "python_implementation": "CPython",
        "python_version": low_precision["method"]["runtime_identity"][
            "python_version"
        ],
        "decimal_libmpdec_version": low_precision["method"][
            "runtime_identity"
        ]["decimal_libmpdec_version"],
        "decimal_precision": 50,
        "decimal_rounding": "ROUND_HALF_EVEN",
    }


def test_method_code_change_during_run_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _write_test_manifests(tmp_path)
    identities = iter(
        (
            {"files": [{"path": "first", "sha256": "a"}], "combined_sha256": "a"},
            {"files": [{"path": "second", "sha256": "b"}], "combined_sha256": "b"},
        )
    )
    monkeypatch.setattr(
        experiment_module,
        "_method_code_identity",
        lambda: next(identities),
    )

    with pytest.raises(RuntimeError, match="changed during analysis"):
        run_capture_latency_experiment(
            CaptureLatencyConfig(
                data_root=tmp_path,
                condition_id=CONDITION_ID,
                lag_grid_ms=(0,),
            )
        )


def test_invalid_optional_evidence_is_reported_missing(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _write_record(
        tmp_path,
        "rules_http",
        received_at="2026-07-24T10:00:00.200000Z",
        monotonic_ns=3,
        event_type="rules_snapshot",
        payload={
            "responses": [
                {
                    "resource": "resolution_rules",
                    "request_key": "invalid-evidence",
                    "raw_json": {
                        "conditionId": CONDITION_ID,
                        "closed": True,
                        "priceToBeat": True,
                        "resolution": "pending",
                    },
                }
            ]
        },
        record_id="invalid-rules-evidence",
        session_id="metadata-session",
        connection_id=None,
        kind="snapshot",
    )
    _write_record(
        tmp_path,
        "clob_market_ws",
        received_at="2026-07-24T10:00:03.500000Z",
        monotonic_ns=3_500_000_000,
        event_type="last_trade_price",
        payload={
            "event_type": "last_trade_price",
            "market": CONDITION_ID,
            "asset_id": "not-a-target-token",
            "price": "NaN",
        },
        record_id="invalid-last-trade",
        connection_id="quote-connection-1",
    )
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:03.600000Z",
        monotonic_ns=3_600_000_000,
        event_type="clock_probe",
        payload={},
        record_id="invalid-clock-probe",
        connection_id="source-connection-1",
    )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    for evidence in (
        "clock_probes",
        "price_to_beat",
        "settlement",
        "last_trade_events",
    ):
        assert result["evidence_availability"][evidence]["available"] is False
    assert (
        result["rejection_counts"]["parser"]["invalid_last_trade_price"]
        == 1
    )
    assert (
        result["rejection_counts"]["parser"]["invalid_clock_probe"]
        == 1
    )


def test_session_and_connection_pair_are_selected_jointly(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path, session_id="metadata-session")
    _seed_source(
        tmp_path,
        session_id="session-a",
        connection_id="source-a",
    )
    _seed_ws_lifecycle(
        tmp_path,
        source="rtds_ws",
        session_id="session-b",
        connection_id="source-b",
        connected_at="2026-07-24T10:00:19.000000Z",
        connected_monotonic_ns=19_000_000_000,
        heartbeat_at="2026-07-24T10:00:19.500000Z",
        heartbeat_monotonic_ns=19_500_000_000,
        heartbeat_interval_seconds=5,
    )
    _seed_ws_lifecycle(
        tmp_path,
        source="clob_market_ws",
        session_id="session-b",
        connection_id="quote-b",
        connected_at="2026-07-24T10:00:19.000000Z",
        connected_monotonic_ns=19_000_000_000,
        heartbeat_at="2026-07-24T10:00:19.500000Z",
        heartbeat_monotonic_ns=19_500_000_000,
        heartbeat_interval_seconds=10,
    )

    def write_quote(
        *,
        session_id: str,
        connection_id: str,
        second: int,
        index: int,
    ) -> None:
        _write_record(
            tmp_path,
            "clob_market_ws",
            received_at=f"2026-07-24T10:00:{second:02d}.000000Z",
            monotonic_ns=second * 1_000_000_000,
            event_type="price_change",
            payload={
                "event_type": "price_change",
                "market": CONDITION_ID,
                "price_changes": [
                    {
                        "asset_id": UP_TOKEN,
                        "best_bid": str(0.49 + index * 0.01),
                        "best_ask": str(0.51 + index * 0.01),
                    },
                    {
                        "asset_id": DOWN_TOKEN,
                        "best_bid": str(0.49 - index * 0.01),
                        "best_ask": str(0.51 - index * 0.01),
                    },
                ],
            },
            record_id=f"joint-quote-{session_id}-{index}",
            session_id=session_id,
            connection_id=connection_id,
        )

    for index, second in enumerate((1, 2, 3, 4)):
        write_quote(
            session_id="session-a",
            connection_id="quote-a",
            second=second,
            index=index,
        )
    for index, (second, price) in enumerate(((20, "100"), (22, "101"))):
        _write_record(
            tmp_path,
            "rtds_ws",
            received_at=f"2026-07-24T10:00:{second:02d}.000000Z",
            monotonic_ns=second * 1_000_000_000,
            event_type="crypto_prices.update",
            payload={
                "topic": "crypto_prices",
                "payload": {
                    "symbol": "btcusdt",
                    "full_accuracy_value": price,
                },
            },
            record_id=f"joint-source-b-{index}",
            session_id="session-b",
            connection_id="source-b",
        )
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:24.000000Z",
        monotonic_ns=24_000_000_000,
        event_type="heartbeat",
        payload={},
        record_id="joint-source-b-ending-heartbeat",
        session_id="session-b",
        connection_id="source-b",
        kind="lifecycle",
        detail={"interval_seconds": 5, "message": "PING"},
    )
    for index, second in enumerate((21, 23)):
        write_quote(
            session_id="session-b",
            connection_id="quote-b",
            second=second,
            index=index,
        )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["coverage"]["selected_analysis_session_id"] == "session-b"
    assert result["coverage"]["selected_source_connection_id"] == "source-b"
    assert result["coverage"]["selected_quote_connection_id"] == "quote-b"
    assert result["status"] == "insufficient_data"


def test_static_metadata_at_same_time_but_later_order_is_rejected(
    tmp_path: Path,
) -> None:
    _write_record(
        tmp_path,
        "clob_http",
        received_at="2026-07-24T10:00:01.000000Z",
        monotonic_ns=9_000_000_000,
        event_type="clob_snapshot",
        payload={
            "responses": [
                {
                    "resource": "clob_market",
                    "request_key": CONDITION_ID,
                    "raw_json": {
                        "c": CONDITION_ID,
                        "t": [
                            {"o": "Up", "t": UP_TOKEN},
                            {"o": "Down", "t": DOWN_TOKEN},
                        ],
                    },
                }
            ]
        },
        record_id="late-tie-metadata",
        session_id="metadata-session",
        connection_id=None,
        kind="snapshot",
    )
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["status"] == "rejected"
    assert "missing_target_token_metadata" in result["rejection_reasons"]


def test_missing_manifest_raw_bytes_are_never_loaded(
    tmp_path: Path,
) -> None:
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:01.000000Z",
        monotonic_ns=1_000_000_000,
        event_type="crypto_prices.update",
        payload={
            "topic": "crypto_prices",
            "payload": {
                "symbol": "btcusdt",
                "full_accuracy_value": "100",
            },
        },
        record_id="unmanifested-record",
        connection_id="source-unmanifested",
    )

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["integrity"]["manifest_complete"] is False
    assert result["integrity"]["missing_manifests"] == [
        "raw/rtds_ws/capture.jsonl"
    ]
    assert result["coverage"]["loaded_records"] == 0
    assert result["rejection_counts"]["parser"]["unfrozen_raw_file"] == 1
    assert result["safety"]["credentials_accessed"] is False


def test_symlinked_raw_is_rejected_without_reading_target(
    tmp_path: Path,
) -> None:
    _seed_source(tmp_path)
    _write_test_manifests(tmp_path)
    raw_path = tmp_path / "raw" / "rtds_ws" / "capture.jsonl"
    outside_path = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside_path.write_bytes(raw_path.read_bytes())
    raw_path.unlink()
    raw_path.symlink_to(outside_path)
    try:
        result = run_capture_latency_experiment(
            CaptureLatencyConfig(
                data_root=tmp_path,
                condition_id=CONDITION_ID,
                lag_grid_ms=(0,),
            )
        )
    finally:
        outside_path.unlink(missing_ok=True)

    assert result["status"] == "rejected"
    assert result["integrity"]["manifest_complete"] is False
    assert result["integrity"]["invalid_manifests"]
    assert result["coverage"]["loaded_records"] == 0
    assert result["rejection_counts"]["parser"]["unfrozen_raw_file"] == 1
    assert result["coverage"]["relevant_record_ids"] == []
    assert result["safety"]["credentials_accessed"] is False


def test_manifest_provenance_mismatch_is_integrity_fatal(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _write_test_manifests(tmp_path)
    manifest_path = (
        tmp_path / "raw" / "rtds_ws" / "capture.manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"] = "wrong-source"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["status"] == "rejected"
    assert result["integrity"]["manifest_complete"] is False
    assert result["integrity"]["invalid_manifests"]
    assert "capture_integrity_failed" in result["rejection_reasons"]


def test_outer_and_recorder_received_at_must_match(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    raw_path = tmp_path / "raw" / "rtds_ws" / "capture.jsonl"
    rows = [
        json.loads(line)
        for line in raw_path.read_text(encoding="utf-8").splitlines()
    ]
    rows[0]["payload"]["received_at"] = "2026-07-24T09:59:59.000000Z"
    rows[0]["record_id"] = canonical_record_id(rows[0])
    raw_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["status"] == "rejected"
    assert (
        result["rejection_counts"]["parser"]["malformed_capture_record"]
        == 1
    )


def test_lifecycle_gap_splits_analysis_and_prevents_cross_gap_move(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    for source, connection in (
        ("rtds_ws", "source-gap"),
        ("clob_market_ws", "quote-gap"),
    ):
        _seed_ws_lifecycle(
            tmp_path,
            source=source,
            session_id="session-1",
            connection_id=connection,
            connected_at="2026-07-24T10:00:00.000000Z",
            connected_monotonic_ns=0,
            heartbeat_at="2026-07-24T10:00:00.500000Z",
            heartbeat_monotonic_ns=500_000_000,
            heartbeat_interval_seconds=1,
        )
    for index, second in enumerate((1, 2, 10, 11)):
        _write_record(
            tmp_path,
            "rtds_ws",
            received_at=f"2026-07-24T10:00:{second:02d}.000000Z",
            monotonic_ns=second * 1_000_000_000,
            event_type="crypto_prices.update",
            payload={
                "topic": "crypto_prices",
                "payload": {
                    "symbol": "btcusdt",
                    "full_accuracy_value": str(100 + index),
                },
            },
            record_id=f"gap-source-{index}",
            connection_id="source-gap",
        )
        quote_second = second
        _write_record(
            tmp_path,
            "clob_market_ws",
            received_at=(
                f"2026-07-24T10:00:{quote_second:02d}.000000Z"
            ),
            monotonic_ns=second * 1_000_000_000 + 100_000_000,
            event_type="price_change",
            payload={
                "event_type": "price_change",
                "market": CONDITION_ID,
                "price_changes": [
                    {
                        "asset_id": UP_TOKEN,
                        "best_bid": str(0.49 + index * 0.01),
                        "best_ask": str(0.51 + index * 0.01),
                    },
                    {
                        "asset_id": DOWN_TOKEN,
                        "best_bid": str(0.49 - index * 0.01),
                        "best_ask": str(0.51 - index * 0.01),
                    },
                ],
            },
            record_id=f"gap-quote-{index}",
            connection_id="quote-gap",
        )
    for source, connection in (
        ("rtds_ws", "source-gap"),
        ("clob_market_ws", "quote-gap"),
    ):
        for suffix, received_at, monotonic_ns in (
            ("first", "2026-07-24T10:00:02.500000Z", 2_500_000_000),
            ("second", "2026-07-24T10:00:11.500000Z", 11_500_000_000),
        ):
            _write_record(
                tmp_path,
                source,
                received_at=received_at,
                monotonic_ns=monotonic_ns,
                event_type="heartbeat",
                payload={},
                record_id=f"{source}-{suffix}-gap-heartbeat",
                connection_id=connection,
                kind="lifecycle",
                detail={"interval_seconds": 1, "message": "PING"},
            )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    lifecycle = result["coverage"]["lifecycle_coverage"]
    assert len(lifecycle["overlap_segments_ms"]) == 2
    assert lifecycle["selected_overlap_segment_ms"] == (
        lifecycle["overlap_segments_ms"][0]
    )
    assert result["coverage"]["source_observations"] == 2
    assert result["sample_gate"]["max_raw_samples_at_any_lag"] == 1


def test_conflicting_heartbeat_cadence_fails_closed(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:04.500000Z",
        monotonic_ns=4_500_000_000,
        event_type="heartbeat",
        payload={},
        record_id="conflicting-heartbeat",
        connection_id="source-connection-1",
        kind="lifecycle",
        detail={"interval_seconds": 30, "message": "PING"},
    )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    session = result["coverage"]["target_session_coverage"]["session-1"]
    assert session[
        "raw_candidate_connection_pairs_with_positive_overlap"
    ] == 1
    assert session[
        "lifecycle_validated_candidate_connection_pairs"
    ] == 0
    assert session["lifecycle_candidate_rejections"] == {
        "source:conflicting_heartbeat_cadence": 1
    }
    assert result["status"] == "rejected"


def test_implausible_heartbeat_cadence_fails_closed(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    _rewrite_heartbeat_intervals(
        tmp_path,
        "60.0000000001",
        sources=("rtds_ws",),
    )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    session = result["coverage"]["target_session_coverage"]["session-1"]
    assert session["lifecycle_candidate_rejections"] == {
        "source:invalid_or_implausible_heartbeat_cadence": 1
    }
    assert result["status"] == "rejected"


def test_data_ordered_after_disconnect_at_same_time_is_excluded(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    for source, connection, cadence in (
        ("rtds_ws", "source-boundary", 5),
        ("clob_market_ws", "quote-boundary", 10),
    ):
        _seed_ws_lifecycle(
            tmp_path,
            source=source,
            session_id="session-1",
            connection_id=connection,
            connected_at="2026-07-24T10:00:00.000000Z",
            connected_monotonic_ns=0,
            heartbeat_at="2026-07-24T10:00:00.500000Z",
            heartbeat_monotonic_ns=500_000_000,
            heartbeat_interval_seconds=cadence,
        )
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:01.000000Z",
        monotonic_ns=1_000_000_000,
        event_type="crypto_prices.update",
        payload={
            "topic": "crypto_prices",
            "payload": {
                "symbol": "btcusdt",
                "full_accuracy_value": "100",
            },
        },
        record_id="before-disconnect",
        connection_id="source-boundary",
        frame_index=0,
    )
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:02.000000Z",
        monotonic_ns=2_000_000_000,
        event_type="disconnected",
        payload={},
        record_id="disconnect-boundary",
        connection_id="source-boundary",
        kind="lifecycle",
        frame_index=0,
        detail={"reason": "test"},
    )
    _write_record(
        tmp_path,
        "rtds_ws",
        received_at="2026-07-24T10:00:02.000000Z",
        monotonic_ns=2_000_000_000,
        event_type="crypto_prices.update",
        payload={
            "topic": "crypto_prices",
            "payload": {
                "symbol": "btcusdt",
                "full_accuracy_value": "101",
            },
        },
        record_id="after-disconnect",
        connection_id="source-boundary",
        frame_index=1,
    )
    for index, second in enumerate((1, 2)):
        _write_record(
            tmp_path,
            "clob_market_ws",
            received_at=f"2026-07-24T10:00:0{second}.000000Z",
            monotonic_ns=second * 1_000_000_000,
            event_type="price_change",
            payload={
                "event_type": "price_change",
                "market": CONDITION_ID,
                "price_changes": [
                    {
                        "asset_id": UP_TOKEN,
                        "best_bid": str(0.49 + index * 0.01),
                        "best_ask": str(0.51 + index * 0.01),
                    },
                    {
                        "asset_id": DOWN_TOKEN,
                        "best_bid": str(0.49 - index * 0.01),
                        "best_ask": str(0.51 - index * 0.01),
                    },
                ],
            },
            record_id=f"boundary-quote-{index}",
            connection_id="quote-boundary",
            frame_index=index,
        )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["coverage"]["selected_analysis_session_id"] == "session-1"
    assert result["coverage"]["source_observations"] == 1


def test_missing_lifecycle_cadence_is_fatal_not_assumed_online(
    tmp_path: Path,
) -> None:
    _seed_metadata(tmp_path)
    _seed_source(tmp_path)
    _seed_quotes(tmp_path)
    for raw_path in (
        tmp_path / "raw" / "rtds_ws" / "capture.jsonl",
        tmp_path / "raw" / "clob_market_ws" / "capture.jsonl",
    ):
        rows = [
            json.loads(line)
            for line in raw_path.read_text(encoding="utf-8").splitlines()
        ]
        raw_path.write_text(
            "".join(
                json.dumps(row, sort_keys=True) + "\n"
                for row in rows
                if row["payload"]["kind"] == "data"
            ),
            encoding="utf-8",
        )
    _write_test_manifests(tmp_path)

    result = run_capture_latency_experiment(
        CaptureLatencyConfig(
            data_root=tmp_path,
            condition_id=CONDITION_ID,
            lag_grid_ms=(0,),
        )
    )

    assert result["status"] == "rejected"
    assert (
        "missing_validated_continuous_online_interval"
        in result["rejection_reasons"]
    )
    assert result["coverage"]["capture_duration_ms"] == 0
