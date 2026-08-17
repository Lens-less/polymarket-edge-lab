import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.edge_lab.capture_cli import ForwardCaptureConfig
from src.edge_lab.capture_runtime import CaptureStoreRecorderSink
from src.edge_lab.data_store import CaptureStore


def _capture_config(root: Path) -> ForwardCaptureConfig:
    return ForwardCaptureConfig(
        data_root=root,
        asset_ids=("token",),
        condition_ids=("condition",),
        rule_market_ids=("market",),
        rtds_subscriptions=(
            {"topic": "crypto_prices", "type": "update"},
        ),
        snapshot_intervals={"gamma": 30.0, "clob": 5.0, "rules": 30.0},
        checkpoint_every_records=10,
        max_records_per_batch=100,
        gamma_page_limit=1,
        gamma_max_pages=1,
        reward_page_limit=1,
        reward_max_pages=1,
        targets=({"asset_id": "token"},),
        clock_sync=None,
    )


def _load_raw_records(
    root: Path,
    source: str = "clob_market_ws",
) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted((root / "raw" / source).glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _clob_record(
    *,
    event_type: str,
    payload: dict[str, Any],
    payload_hash: str,
    connection_id: str,
    received_at: str = "2026-08-12T11:00:00Z",
    event_at: str = "1970-01-01T00:00:01Z",
    server_timestamp: str = "1000",
    sequence: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": f"clob-market-ws.{event_type}.v1",
        "source": "clob_market_ws",
        "received_at": received_at,
        "event_at": event_at,
        "event_type": event_type,
        "kind": "data",
        "sequence": sequence,
        "session_id": "session-1",
        "connection_id": connection_id,
        "frame_hash": "a" * 64,
        "payload_hash": payload_hash,
        "server_hash": "b" * 64,
        "server_timestamp": server_timestamp,
        "payload": payload,
    }


@pytest.mark.anyio
async def test_semantic_duplicate_from_two_connections_persists_once(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    payload = {
        "event_type": "book",
        "timestamp": "1000",
        "market": "condition",
        "asset_id": "token",
        "bids": [{"price": "0.49", "size": "20"}],
        "asks": [{"price": "0.51", "size": "20"}],
    }

    await sink.emit(
        _clob_record(
            event_type="book",
            payload=payload,
            payload_hash="1" * 64,
            connection_id="primary-connection",
            received_at="2026-08-12T11:00:00Z",
        )
    )
    await sink.emit(
        _clob_record(
            event_type="book",
            payload=payload,
            payload_hash="1" * 64,
            connection_id="secondary-connection",
            received_at="2026-08-12T11:00:01Z",
        )
    )
    await sink.close()

    stored = _load_raw_records(tmp_path / "capture")
    assert len(stored) == 1
    assert stored[0]["received_at"] == "2026-08-12T11:00:00.000000Z"
    assert stored[0]["payload"]["connection_id"] == "primary-connection"


@pytest.mark.anyio
async def test_same_connection_repeats_remain_auditable(tmp_path: Path) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    payload = {
        "event_type": "last_trade_price",
        "timestamp": "1000",
        "market": "condition",
        "asset_id": "token",
        "price": "0.49",
    }
    first = _clob_record(
        event_type="last_trade_price",
        payload=payload,
        payload_hash="6" * 64,
        connection_id="primary-connection",
    )

    await sink.emit(first)
    await sink.emit(
        {
            **first,
            "received_at": "2026-08-12T11:00:00.100000Z",
        }
    )
    await sink.close()

    assert len(_load_raw_records(tmp_path / "capture")) == 2


@pytest.mark.anyio
async def test_rtds_transport_connection_id_does_not_defeat_dedup(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    message = {
        "topic": "crypto_prices",
        "type": "update",
        "timestamp": 1_786_532_401_156,
        "payload": {
            "symbol": "btcusdt",
            "timestamp": 1_786_532_401_000,
            "value": 65_320,
            "full_accuracy_value": "65320.00000000",
        },
    }

    for recorder_connection, server_connection, payload_hash, delivery_delta in (
        ("primary", "server-primary", "7" * 64, 0),
        ("secondary", "server-secondary", "8" * 64, 7),
    ):
        await sink.emit(
            {
                "schema_version": "rtds.crypto_prices.update.v1",
                "source": "rtds_ws",
                "received_at": "2026-08-12T11:00:00Z",
                "event_at": "2026-08-12T11:00:00Z",
                "event_type": "crypto_prices.update",
                "kind": "data",
                "sequence": None,
                "session_id": "session-1",
                "connection_id": recorder_connection,
                "frame_hash": payload_hash,
                "payload_hash": payload_hash,
                "server_hash": None,
                "server_timestamp": message["timestamp"] + delivery_delta,
                "payload": {
                    **message,
                    "connection_id": server_connection,
                    "timestamp": message["timestamp"] + delivery_delta,
                },
            }
        )
    await sink.close()

    assert len(_load_raw_records(tmp_path / "capture", "rtds_ws")) == 1


@pytest.mark.anyio
async def test_same_timestamp_with_distinct_payload_survives_dedup(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))

    await sink.emit(
        _clob_record(
            event_type="last_trade_price",
            payload={
                "event_type": "last_trade_price",
                "timestamp": "1000",
                "market": "condition",
                "asset_id": "token",
                "price": "0.49",
            },
            payload_hash="2" * 64,
            connection_id="primary-connection",
            sequence=7,
        )
    )
    await sink.emit(
        _clob_record(
            event_type="last_trade_price",
            payload={
                "event_type": "last_trade_price",
                "timestamp": "1000",
                "market": "condition",
                "asset_id": "token",
                "price": "0.50",
            },
            payload_hash="3" * 64,
            connection_id="secondary-connection",
            sequence=7,
        )
    )
    await sink.close()

    stored = _load_raw_records(tmp_path / "capture")
    assert len(stored) == 2
    assert {item["payload"]["payload_hash"] for item in stored} == {
        "2" * 64,
        "3" * 64,
    }


@pytest.mark.anyio
async def test_cross_connection_sequence_does_not_defeat_semantic_dedup(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    payload = {
        "event_type": "last_trade_price",
        "timestamp": "1000",
        "market": "condition",
        "asset_id": "token",
        "price": "0.49",
    }

    await sink.emit(
        _clob_record(
            event_type="last_trade_price",
            payload=payload,
            payload_hash="9" * 64,
            connection_id="primary-connection",
            sequence=41,
        )
    )
    await sink.emit(
        _clob_record(
            event_type="last_trade_price",
            payload=payload,
            payload_hash="9" * 64,
            connection_id="secondary-connection",
            sequence=42,
        )
    )
    await sink.close()

    assert len(_load_raw_records(tmp_path / "capture")) == 1


@pytest.mark.anyio
async def test_duplicate_price_change_does_not_duplicate_reconstructed_book(
    tmp_path: Path,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))

    await sink.emit(
        _clob_record(
            event_type="book",
            payload={
                "event_type": "book",
                "timestamp": "1000",
                "market": "condition",
                "asset_id": "token",
                "bids": [{"price": "0.49", "size": "20"}],
                "asks": [
                    {"price": "0.50", "size": "10"},
                    {"price": "0.51", "size": "12"},
                ],
            },
            payload_hash="4" * 64,
            connection_id="primary-connection",
        )
    )
    duplicate_change = _clob_record(
        event_type="price_change",
        payload={
            "event_type": "price_change",
            "market": "condition",
            "timestamp": "1250",
            "price_changes": [
                {
                    "asset_id": "token",
                    "price": "0.50",
                    "size": "0",
                    "side": "SELL",
                    "best_bid": "0.49",
                    "best_ask": "0.51",
                    "hash": "c" * 40,
                }
            ],
        },
        payload_hash="5" * 64,
        connection_id="primary-connection",
        received_at="1970-01-01T00:00:01.250000Z",
        event_at="1970-01-01T00:00:01.250000Z",
        server_timestamp="1250",
    )

    await sink.emit(duplicate_change)
    await sink.emit(
        {
            **duplicate_change,
            "connection_id": "secondary-connection",
            "received_at": "1970-01-01T00:00:01.260000Z",
        }
    )
    await sink.close()

    stored = _load_raw_records(tmp_path / "capture")
    data = [item["payload"] for item in stored if item["payload"]["kind"] == "data"]
    assert [item["event_type"] for item in data] == ["book", "book"]
    reconstructed = data[-1]["payload"]
    assert reconstructed["asset_id"] == "token"
    assert reconstructed["asks"][0] == {"price": "0.51", "size": "12"}
    assert reconstructed["source_event_type"] == "price_change"


@pytest.mark.anyio
async def test_reconstructed_book_write_failure_marks_shared_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.edge_lab.btc_twap_relative_value_service import CompactRecorderSink

    backing = CaptureStoreRecorderSink(
        CaptureStore(tmp_path / "capture"),
        max_records_per_batch=100,
    )
    sink = CompactRecorderSink(backing, allowed_asset_ids=("token",))
    await sink.emit(
        _clob_record(
            event_type="book",
            payload={
                "event_type": "book",
                "timestamp": "1000",
                "market": "condition",
                "asset_id": "token",
                "bids": [{"price": "0.49", "size": "20"}],
                "asks": [{"price": "0.51", "size": "20"}],
            },
            payload_hash="a" * 64,
            connection_id="primary-connection",
        )
    )

    failure = OSError("simulated reconstructed-book write failure")

    async def fail_emit(record: Any) -> None:
        raise failure

    monkeypatch.setattr(backing, "emit", fail_emit)

    with pytest.raises(OSError, match="simulated reconstructed-book"):
        await sink.emit(
            _clob_record(
                event_type="price_change",
                payload={
                    "event_type": "price_change",
                    "market": "condition",
                    "timestamp": "1250",
                    "price_changes": [
                        {
                            "asset_id": "token",
                            "price": "0.50",
                            "size": "10",
                            "side": "SELL",
                            "best_bid": "0.49",
                            "best_ask": "0.50",
                            "hash": "c" * 40,
                        }
                    ],
                },
                payload_hash="b" * 64,
                connection_id="primary-connection",
                received_at="1970-01-01T00:00:01.250000Z",
                event_at="1970-01-01T00:00:01.250000Z",
                server_timestamp="1250",
            )
        )

    assert sink.persistence_error is failure


@pytest.mark.anyio
async def test_run_capture_builds_primary_and_secondary_recorders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.edge_lab.btc_twap_relative_value_service as service

    created: list[dict[str, Any]] = []

    class StubSession:
        def close(self) -> None:
            return None

    class StubSourcesClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

    class StubSnapshotClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

    class StubRecorder:
        def __init__(
            self,
            *,
            config: Any,
            sink: Any,
            snapshot_client: Any,
            websocket_factory: Any,
        ) -> None:
            created.append(
                {
                    "config": config,
                    "sink": sink,
                    "snapshot_client": snapshot_client,
                    "websocket_factory": websocket_factory,
                }
            )

        async def run(self, *, run_for_seconds: float) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(service, "verify_paper_only_guard", lambda: None)
    monkeypatch.setattr(service, "public_session", lambda proxy_url: StubSession())
    monkeypatch.setattr(service, "PublicSourcesClient", StubSourcesClient)
    monkeypatch.setattr(
        service,
        "PublicSourcesSnapshotClient",
        StubSnapshotClient,
    )
    monkeypatch.setattr(service, "PublicRecorder", StubRecorder)

    config = _capture_config(tmp_path / "capture-root")
    summary = await service.run_compact_forward_capture(
        config,
        duration_seconds=0.01,
        proxy_url=None,
    )

    assert summary["capture_error"] is None
    assert summary["generated_at"].endswith("Z")
    assert summary["recorder_leg_count"] == 2
    assert summary["recorder_leg_failures"] == []
    assert summary["websocket_redundancy"] == {"clob_market_ws": 2, "rtds_ws": 2}
    assert len(created) == 2
    assert created[0]["config"].snapshot_intervals == config.snapshot_intervals
    assert created[1]["config"].snapshot_intervals == {}
    assert created[0]["config"].clob_asset_ids == config.asset_ids
    assert created[1]["config"].rtds_subscriptions == config.rtds_subscriptions


@pytest.mark.anyio
async def test_run_capture_preserves_recorder_worker_failure_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.edge_lab.btc_twap_relative_value_service as service
    from src.edge_lab.recorder import RecorderWorkerError

    failures = (
        RecorderWorkerError(
            error_type="TimeoutError",
            error_code="persistence_sink_timeout",
        ),
        RecorderWorkerError(
            error_type="CancelledError",
            error_code="persistence_sink_cancelled",
        ),
    )
    created = 0

    class StubSession:
        def close(self) -> None:
            return None

    class StubSourcesClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

    class StubSnapshotClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            return None

    class FailedRecorder:
        def __init__(self, **kwargs: Any) -> None:
            nonlocal created
            self.failure = failures[created]
            created += 1

        async def run(self, *, run_for_seconds: float) -> None:
            raise self.failure

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(service, "verify_paper_only_guard", lambda: None)
    monkeypatch.setattr(service, "public_session", lambda proxy_url: StubSession())
    monkeypatch.setattr(service, "PublicSourcesClient", StubSourcesClient)
    monkeypatch.setattr(
        service,
        "PublicSourcesSnapshotClient",
        StubSnapshotClient,
    )
    monkeypatch.setattr(service, "PublicRecorder", FailedRecorder)

    with pytest.raises(service.CaptureFinalizationError) as raised:
        await service.run_compact_forward_capture(
            _capture_config(tmp_path / "capture-root"),
            duration_seconds=0.01,
            proxy_url=None,
        )

    recorder_leg_failures = raised.value.summary["recorder_leg_failures"]
    assert len(recorder_leg_failures) == 2
    assert {
        (failure["error_type"], failure["error_code"])
        for failure in recorder_leg_failures
    } == {
        ("TimeoutError", "persistence_sink_timeout"),
        ("CancelledError", "persistence_sink_cancelled"),
    }


@pytest.mark.anyio
async def test_one_recorder_leg_failure_does_not_stop_healthy_sibling() -> None:
    import src.edge_lab.btc_twap_relative_value_service as service

    class FailedRecorder:
        async def run(self, *, run_for_seconds: float) -> None:
            raise ConnectionError("simulated recorder-leg failure")

    class HealthyRecorder:
        def __init__(self) -> None:
            self.completed = False

        async def run(self, *, run_for_seconds: float) -> None:
            await asyncio.sleep(0.01)
            self.completed = True

    class Sink:
        persistence_error = None

    healthy = HealthyRecorder()
    failures = await service._run_redundant_recorders(
        FailedRecorder(),  # type: ignore[arg-type]
        healthy,  # type: ignore[arg-type]
        duration_seconds=1,
        shared_sink=Sink(),  # type: ignore[arg-type]
    )

    assert healthy.completed is True
    assert len(failures) == 1
    assert isinstance(failures[0], ConnectionError)


@pytest.mark.anyio
async def test_redundant_capture_cancellation_stops_both_legs() -> None:
    import src.edge_lab.btc_twap_relative_value_service as service

    class BlockingRecorder:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run(self, *, run_for_seconds: float) -> None:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()

    class Sink:
        persistence_error = None

    primary = BlockingRecorder()
    secondary = BlockingRecorder()
    task = asyncio.create_task(
        service._run_redundant_recorders(
            primary,  # type: ignore[arg-type]
            secondary,  # type: ignore[arg-type]
            duration_seconds=60,
            shared_sink=Sink(),  # type: ignore[arg-type]
        )
    )
    await primary.started.wait()
    await secondary.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    assert primary.cancelled.is_set()
    assert secondary.cancelled.is_set()
