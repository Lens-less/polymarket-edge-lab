"""Behavioral tests for the public, credential-free edge-lab recorder."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pytest

from src.edge_lab.recorder import (
    CLOB_MARKET_WS,
    DefaultWebSocketFactory,
    PublicRecorder,
    RecorderConfig,
    RecorderClientConnection,
    RecorderWorkerError,
    RTDS_WS,
    SnapshotEnvelope,
    _snapshot_asset_watermarks_ns,
)


@pytest.mark.asyncio
async def test_default_websocket_factory_uses_only_explicit_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import websockets

    calls: list[tuple[str, str | None, type[Any], int]] = []
    connection = QueueWebSocket()

    async def fake_connect(
        url: str,
        *,
        proxy: str | None,
        create_connection: type[Any],
        max_queue: int,
    ) -> QueueWebSocket:
        calls.append((url, proxy, create_connection, max_queue))
        return connection

    monkeypatch.setattr(websockets, "connect", fake_connect)
    direct = DefaultWebSocketFactory()
    proxied = DefaultWebSocketFactory(
        proxy_url="http://127.0.0.1:7897"
    )

    assert await direct.connect(CLOB_MARKET_WS) is connection
    assert await proxied.connect(RTDS_WS) is connection
    assert calls == [
        (CLOB_MARKET_WS, None, RecorderClientConnection, 1_024),
        (
            RTDS_WS,
            "http://127.0.0.1:7897",
            RecorderClientConnection,
            1_024,
        ),
    ]


@pytest.mark.asyncio
async def test_recorder_client_connection_handles_reset_before_connection_made(
) -> None:
    from websockets.client import ClientProtocol
    from websockets.uri import parse_uri

    connection = RecorderClientConnection(
        ClientProtocol(parse_uri("wss://example.com"))
    )
    error = ConnectionResetError("synthetic pre-connection reset")

    connection.connection_lost(error)

    assert connection.recv_exc is error
    assert connection.connection_lost_waiter.done()


def test_full_book_watermarks_require_every_requested_asset() -> None:
    snapshot = {
        "schema_version": "edge-lab-public-snapshot.v1",
        "snapshot_kind": "full_book",
        "requested_asset_ids": ["yes-token", "no-token"],
        "responses": [
            {
                "resource": "clob_book",
                "request_key": "yes-token",
                "raw_json": {
                    "asset_id": "yes-token",
                    "timestamp": "1000",
                },
            },
            {
                "resource": "clob_book",
                "request_key": "no-token",
                "response_status": "error",
                "error": {"error_code": "proxy_unavailable"},
            },
        ],
    }

    with pytest.raises(ValueError, match="unsuccessful"):
        _snapshot_asset_watermarks_ns(
            snapshot,
            ("yes-token", "no-token"),
        )


def test_full_book_watermarks_are_bound_per_asset() -> None:
    snapshot = {
        "schema_version": "edge-lab-public-snapshot.v1",
        "snapshot_kind": "full_book",
        "requested_asset_ids": ["yes-token", "no-token"],
        "responses": [
            {
                "resource": "clob_book",
                "request_key": "yes-token",
                "raw_json": {
                    "asset_id": "yes-token",
                    "timestamp": "1000",
                },
            },
            {
                "resource": "clob_book",
                "request_key": "no-token",
                "raw_json": {
                    "asset_id": "no-token",
                    "timestamp": "3000",
                },
            },
        ],
    }

    assert _snapshot_asset_watermarks_ns(
        snapshot,
        ("yes-token", "no-token"),
    ) == {
        "no-token": 3_000_000_000_000,
        "yes-token": 1_000_000_000_000,
    }


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://example.com:7897",
        "socks5://127.0.0.1:7897",
        "http://user:" + "password@127.0.0.1:7897",
        "http://127.0.0.1:7897/path",
        "http://127.0.0.1:7897?" + "token=forbidden",
    ],
)
def test_default_websocket_factory_rejects_unsafe_proxy(
    proxy_url: str,
) -> None:
    with pytest.raises(
        ValueError,
        match=r"unauthenticated loopback HTTP\(S\)",
    ):
        DefaultWebSocketFactory(proxy_url=proxy_url)


class MemorySink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.checkpoints: list[tuple[str, dict[str, Any]]] = []
        self.changed = asyncio.Event()

    async def emit(self, record: Mapping[str, Any]) -> None:
        self.records.append(dict(record))
        self.changed.set()

    async def checkpoint(
        self, source: str, checkpoint: Mapping[str, Any]
    ) -> None:
        self.checkpoints.append((source, dict(checkpoint)))
        self.changed.set()

    async def wait_until(self, predicate: Any, timeout: float = 1.0) -> None:
        async def _wait() -> None:
            while not predicate():
                self.changed.clear()
                await self.changed.wait()

        await asyncio.wait_for(_wait(), timeout=timeout)


class BackpressuredDataSink(MemorySink):
    def __init__(self) -> None:
        super().__init__()
        self.first_data_started = asyncio.Event()
        self.release_first_data = asyncio.Event()

    async def emit(self, record: Mapping[str, Any]) -> None:
        if (
            record.get("kind") == "data"
            and not self.first_data_started.is_set()
        ):
            self.first_data_started.set()
            await self.release_first_data.wait()
        await super().emit(record)


class HangingSink:
    async def emit(self, record: Mapping[str, Any]) -> None:
        await asyncio.Event().wait()

    async def checkpoint(
        self, source: str, checkpoint: Mapping[str, Any]
    ) -> None:
        await asyncio.Event().wait()


class SlowCheckpointSink(MemorySink):
    async def checkpoint(
        self, source: str, checkpoint: Mapping[str, Any]
    ) -> None:
        await asyncio.sleep(0.03)
        await super().checkpoint(source, checkpoint)


class QueueWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, (str, bytes)):
            return item
        return str(item)

    async def close(self) -> None:
        self.closed = True


class ObservedQueueWebSocket(QueueWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.recv_calls = 0
        self.second_recv_started = asyncio.Event()
        self.third_recv_started = asyncio.Event()

    async def recv(self) -> str | bytes:
        self.recv_calls += 1
        if self.recv_calls >= 2:
            self.second_recv_started.set()
        if self.recv_calls >= 3:
            self.third_recv_started.set()
        return await super().recv()


class FailingPingWebSocket(QueueWebSocket):
    async def send(self, message: str) -> None:
        if message == "PING":
            raise ConnectionError("heartbeat transport failed")
        await super().send(message)


class HangingCloseWebSocket(QueueWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await asyncio.Event().wait()


class _AbortableTransport:
    def __init__(self) -> None:
        self.aborted = asyncio.Event()

    def abort(self) -> None:
        self.aborted.set()


class HardCloseOnlyWebSocket(QueueWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.transport = _AbortableTransport()

    async def close(self) -> None:
        self.close_started.set()
        await asyncio.Event().wait()


class RaisingCloseWebSocket(QueueWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.transport = _AbortableTransport()

    async def close(self) -> None:
        raise ConnectionError("synthetic close failure")


class CancellationResistantCloseWebSocket(QueueWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        while not self.release_close.is_set():
            try:
                await self.release_close.wait()
            except asyncio.CancelledError:
                continue


class FailedResyncRaceWebSocket(QueueWebSocket):
    """Returns one late frame on cancel and another after close starts."""

    def __init__(self) -> None:
        super().__init__()
        self.recv_started = asyncio.Event()
        self._returned_cancel_frame = False
        self._returned_close_frame = False
        self._close_frame_ready = asyncio.Event()

    async def recv(self) -> str | bytes:
        self.recv_started.set()
        if not self._returned_close_frame and self._close_frame_ready.is_set():
            self._returned_close_frame = True
            return (
                '{"event_type":"book","asset_id":"yes-token",'
                '"timestamp":9999,"sequence":99,"bids":[],"asks":[]}'
            )
        try:
            await self._close_frame_ready.wait()
        except asyncio.CancelledError:
            if not self._returned_cancel_frame:
                self._returned_cancel_frame = True
                return (
                    '{"event_type":"book","asset_id":"yes-token",'
                    '"timestamp":9898,"sequence":98,"bids":[],"asks":[]}'
                )
            raise
        if not self._returned_close_frame:
            self._returned_close_frame = True
            return (
                '{"event_type":"book","asset_id":"yes-token",'
                '"timestamp":9999,"sequence":99,"bids":[],"asks":[]}'
            )
        return await super().recv()

    async def close(self) -> None:
        self.closed = True
        self._close_frame_ready.set()
        await asyncio.sleep(0)


class QueueWebSocketFactory:
    def __init__(self, connections: Mapping[str, Sequence[QueueWebSocket]]) -> None:
        self.connections = {
            url: asyncio.Queue[QueueWebSocket]() for url in connections
        }
        for url, sockets in connections.items():
            for socket in sockets:
                self.connections[url].put_nowait(socket)

    async def connect(self, url: str) -> QueueWebSocket:
        return await self.connections[url].get()


class IdleSnapshotClient:
    async def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any:
        return {"kind": kind, "asset_ids": list(asset_ids)}


class RecordingSnapshotClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any:
        call = (kind, tuple(asset_ids))
        self.calls.append(call)
        return {
            "snapshot_kind": kind,
            "asset_ids": list(asset_ids),
            "timestamp": "1721808005000",
            "books": [{"asset_id": asset_id} for asset_id in asset_ids],
        }


class FloatSnapshotClient:
    async def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any:
        return {
            "kind": kind,
            "book": {"asset_id": asset_ids[0], "price": 0.4200, "size": 10.5},
        }


class RawSnapshotClient:
    async def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any:
        return SnapshotEnvelope(
            payload={"book": {"asset_id": asset_ids[0], "price": "0.4200"}},
            raw_body=b'{"book":{"asset_id":"yes-token","price":0.4200}}',
            status_code=200,
            request_url="https://clob.polymarket.com/book",
            response_headers={"content-type": "application/json"},
        )


class FlakyResnapshotClient:
    def __init__(self) -> None:
        self.full_book_attempts = 0

    async def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any:
        assert kind == "full_book"
        self.full_book_attempts += 1
        if self.full_book_attempts == 1:
            await asyncio.sleep(0)
            raise ConnectionError("full snapshot unavailable")
        return {
            "timestamp": "500",
            "books": [{"asset_id": asset_id} for asset_id in asset_ids],
        }


class CoordinatedFlakyResnapshotClient:
    def __init__(self, failed_connection: FailedResyncRaceWebSocket) -> None:
        self.failed_connection = failed_connection
        self.full_book_attempts = 0

    async def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any:
        assert kind == "full_book"
        self.full_book_attempts += 1
        if self.full_book_attempts == 1:
            await self.failed_connection.recv_started.wait()
            raise ConnectionError("coordinated full snapshot failure")
        return {
            "timestamp": "500",
            "books": [{"asset_id": asset_id} for asset_id in asset_ids],
        }


class BlockingInitialResnapshotClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any:
        assert kind == "full_book"
        self.started.set()
        await self.release.wait()
        return {
            "timestamp": "1000",
            "books": [{"asset_id": asset_id} for asset_id in asset_ids],
        }


class FixedClock:
    def __init__(self) -> None:
        self._monotonic_ns = 1_000

    def utcnow(self) -> datetime:
        return datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)

    def monotonic_ns(self) -> int:
        self._monotonic_ns += 1
        return self._monotonic_ns

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


class CancellationResistantClock(FixedClock):
    def __init__(self) -> None:
        super().__init__()
        self.sleep_started = asyncio.Event()
        self.release_sleep = asyncio.Event()

    async def sleep(self, delay: float) -> None:
        self.sleep_started.set()
        while not self.release_sleep.is_set():
            try:
                await self.release_sleep.wait()
            except asyncio.CancelledError:
                continue


class DisconnectTimingClock(FixedClock):
    def __init__(self) -> None:
        super().__init__()
        self.now = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)

    def utcnow(self) -> datetime:
        return self.now


class SlowCleanupWebSocket(QueueWebSocket):
    def __init__(self, clock: DisconnectTimingClock) -> None:
        super().__init__()
        self.clock = clock

    async def close(self) -> None:
        self.clock.now += timedelta(seconds=2)
        await super().close()


class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.origin = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def utcnow(self) -> datetime:
        return self.origin + timedelta(seconds=self.now)

    def monotonic_ns(self) -> int:
        return int(self.now * 1_000_000_000)

    async def sleep(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for deadline, future in tuple(self._sleepers):
            if deadline <= self.now and not future.done():
                future.set_result(None)
        self._sleepers = [
            (deadline, future)
            for deadline, future in self._sleepers
            if not future.done()
        ]
        await asyncio.sleep(0)

    async def wait_for_sleepers(self, count: int) -> None:
        while len(self._sleepers) < count:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_disconnected_timestamp_precedes_slow_connection_cleanup() -> None:
    clob_url = CLOB_MARKET_WS
    clock = DisconnectTimingClock()
    clob = SlowCleanupWebSocket(clock)
    clob.incoming.put_nowait(ConnectionError("transport lost"))
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=clock,
    )

    await recorder.start()
    await sink.wait_until(
        lambda: any(
            row["event_type"] == "disconnected"
            for row in sink.records
        )
    )
    await recorder.stop()

    disconnected = next(
        row
        for row in sink.records
        if row["event_type"] == "disconnected"
    )
    assert disconnected["received_at"] == "2026-07-24T08:00:00.000000Z"
    assert clock.now == datetime(
        2026,
        7,
        24,
        8,
        0,
        2,
        tzinfo=timezone.utc,
    )


@pytest.mark.asyncio
async def test_disconnected_timestamp_precedes_receive_buffer_drain() -> None:
    clob_url = CLOB_MARKET_WS
    clock = DisconnectTimingClock()
    clob = ObservedQueueWebSocket()
    clob.incoming.put_nowait(
        '{"event_type":"book","asset_id":"yes-token",'
        '"timestamp":1000,"sequence":1,"bids":[],"asks":[]}'
    )
    clob.incoming.put_nowait(ConnectionError("transport lost"))
    sink = BackpressuredDataSink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=clock,
    )

    await recorder.start()
    try:
        await asyncio.wait_for(sink.first_data_started.wait(), timeout=1)
        await asyncio.wait_for(clob.second_recv_started.wait(), timeout=1)
        clock.now += timedelta(seconds=5)
        sink.release_first_data.set()
        await sink.wait_until(
            lambda: any(
                row["event_type"] == "disconnected"
                for row in sink.records
            )
        )
    finally:
        sink.release_first_data.set()
        await recorder.stop()

    disconnected = next(
        row for row in sink.records if row["event_type"] == "disconnected"
    )
    assert disconnected["received_at"] == "2026-07-24T08:00:00.000000Z"


@pytest.mark.asyncio
async def test_clob_only_worker_does_not_open_duplicate_rtds_connection() -> None:
    clob_url = CLOB_MARKET_WS
    clob = QueueWebSocket()
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token", "no-token"),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(lambda: bool(clob.sent))
    await recorder.stop()

    assert json.loads(clob.sent[0])["assets_ids"] == [
        "yes-token",
        "no-token",
    ]
    assert not any(row["source"] == "rtds_ws" for row in sink.records)


@pytest.mark.asyncio
async def test_receive_drain_continues_while_first_frame_is_persisting() -> None:
    clob_url = CLOB_MARKET_WS
    clob = ObservedQueueWebSocket()
    sink = BackpressuredDataSink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    frames = (
        '{"event_type":"book","asset_id":"yes-token",'
        '"timestamp":1000,"sequence":1,"bids":[],"asks":[]}',
        '{"event_type":"book","asset_id":"yes-token",'
        '"timestamp":2000,"sequence":2,"bids":[],"asks":[]}',
    )

    await recorder.start()
    try:
        await sink.wait_until(lambda: bool(clob.sent))
        for frame in frames:
            clob.incoming.put_nowait(frame)
        await asyncio.wait_for(sink.first_data_started.wait(), timeout=1)

        # Persistence of frame one is deliberately blocked.  The transport
        # reader must still drain frame two so protocol-level PONG/data cannot
        # starve behind local parsing or filesystem work.
        await asyncio.wait_for(clob.second_recv_started.wait(), timeout=0.2)
        assert not [row for row in sink.records if row["kind"] == "data"]
    finally:
        sink.release_first_data.set()
        await sink.wait_until(
            lambda: len(
                [row for row in sink.records if row["kind"] == "data"]
            )
            == 2
        )
        await recorder.stop()

    data_rows = [row for row in sink.records if row["kind"] == "data"]
    assert [row["sequence"] for row in data_rows] == [1, 2]


@pytest.mark.asyncio
async def test_capture_frontier_runs_after_already_received_frames() -> None:
    clob_url = CLOB_MARKET_WS
    clob = ObservedQueueWebSocket()
    sink = BackpressuredDataSink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    frames = (
        '{"event_type":"book","asset_id":"yes-token",'
        '"timestamp":1000,"sequence":1,"bids":[],"asks":[]}',
        '{"event_type":"price_change","market":"condition",'
        '"timestamp":2000,"sequence":2,"price_changes":[]}',
    )
    observed_at_frontier: list[list[int]] = []

    async def observe_frontier() -> str:
        observed_at_frontier.append(
            [
                int(row["sequence"])
                for row in sink.records
                if row["kind"] == "data"
            ]
        )
        return "frontier-drained"

    await recorder.start()
    frontier_task: asyncio.Task[str | None] | None = None
    try:
        await sink.wait_until(lambda: bool(clob.sent))
        for frame in frames:
            clob.incoming.put_nowait(frame)
        await asyncio.wait_for(sink.first_data_started.wait(), timeout=1)
        await asyncio.wait_for(clob.third_recv_started.wait(), timeout=1)

        frontier_task = asyncio.create_task(
            recorder.run_at_clob_capture_frontier(observe_frontier)
        )
        await asyncio.sleep(0)

        assert not frontier_task.done()
        assert observed_at_frontier == []

        sink.release_first_data.set()
        assert await asyncio.wait_for(frontier_task, timeout=1) == (
            "frontier-drained"
        )
    finally:
        sink.release_first_data.set()
        if frontier_task is not None and not frontier_task.done():
            frontier_task.cancel()
            await asyncio.gather(frontier_task, return_exceptions=True)
        await recorder.stop()

    assert observed_at_frontier == [[1, 2]]


@pytest.mark.asyncio
async def test_stop_drains_frames_stamped_before_transport_close() -> None:
    clob_url = CLOB_MARKET_WS
    clob = ObservedQueueWebSocket()
    sink = BackpressuredDataSink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            shutdown_timeout_seconds=1.0,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    for sequence in (1, 2):
        clob.incoming.put_nowait(
            '{"event_type":"book","asset_id":"yes-token",'
            f'"timestamp":{sequence}000,"sequence":{sequence},'
            '"bids":[],"asks":[]}'
        )

    await recorder.start()
    await asyncio.wait_for(sink.first_data_started.wait(), timeout=1)
    await asyncio.wait_for(clob.second_recv_started.wait(), timeout=1)
    stop_task = asyncio.create_task(recorder.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()

    sink.release_first_data.set()
    await asyncio.wait_for(stop_task, timeout=1)

    data_rows = [row for row in sink.records if row["kind"] == "data"]
    assert [row["sequence"] for row in data_rows] == [1, 2]
    stop_checkpoint = next(
        checkpoint
        for source, checkpoint in reversed(sink.checkpoints)
        if source == "clob_market_ws" and checkpoint["reason"] == "stop"
    )
    assert stop_checkpoint["last_sequence"] == 2


@pytest.mark.asyncio
async def test_receive_buffer_overflow_persists_boundary_frame_and_reconnects(
) -> None:
    clob_url = CLOB_MARKET_WS
    clob = ObservedQueueWebSocket()
    sink = BackpressuredDataSink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            reconnect_initial_seconds=0,
            receive_buffer_max_messages=1,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    def frame(sequence: int) -> str:
        return (
            '{"event_type":"book","asset_id":"yes-token",'
            f'"timestamp":{sequence}000,"sequence":{sequence},'
            '"bids":[],"asks":[]}'
        )

    clob.incoming.put_nowait(frame(1))
    await recorder.start()
    try:
        await asyncio.wait_for(sink.first_data_started.wait(), timeout=1)
        clob.incoming.put_nowait(frame(2))
        clob.incoming.put_nowait(frame(3))
        await asyncio.wait_for(clob.third_recv_started.wait(), timeout=1)
        sink.release_first_data.set()
        await sink.wait_until(
            lambda: len(
                [row for row in sink.records if row["kind"] == "data"]
            )
            == 3
            and any(
                row["event_type"] == "error"
                and row["detail"]["error_code"]
                == "websocket_transport_failed"
                for row in sink.records
            )
        )
    finally:
        sink.release_first_data.set()
        await recorder.stop()

    data_rows = [row for row in sink.records if row["kind"] == "data"]
    assert [row["sequence"] for row in data_rows] == [1, 2, 3]
    transport_error = next(
        row for row in sink.records if row["event_type"] == "error"
    )
    assert transport_error["detail"]["error_type"] == "ConnectionError"


@pytest.mark.asyncio
async def test_initial_clob_resnapshot_gates_deltas_until_book_is_frozen() -> None:
    clob_url = CLOB_MARKET_WS
    clob = QueueWebSocket()
    sink = MemorySink()
    snapshots = BlockingInitialResnapshotClient()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            initial_clob_resnapshot=True,
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=snapshots,
        clock=FixedClock(),
    )

    await recorder.start()
    await asyncio.wait_for(snapshots.started.wait(), timeout=1)
    await clob.incoming.put(
        '{"event_type":"book","asset_id":"yes-token",'
        '"timestamp":2000,"sequence":1,"bids":[],"asks":[]}'
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not any(row["kind"] == "data" for row in sink.records)

    snapshots.release.set()
    await sink.wait_until(
        lambda: any(
            row["kind"] == "data"
            and row["source"] == "clob_market_ws"
            for row in sink.records
        )
    )
    await recorder.stop()

    frame = next(row for row in sink.records if row["kind"] == "data")
    assert frame["resync_buffered"] is True
    assert frame["resync_status"] == "success"
    assert frame["resync_watermark_ns"] == 1_000_000_000_000
    assert frame["replay_eligible"] is True
    resnapshot = next(
        row for row in sink.records if row["event_type"] == "resnapshot"
    )
    assert resnapshot["reason"] == "initial_websocket_connect"


@pytest.mark.asyncio
async def test_public_streams_subscribe_and_preserve_supported_raw_events() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = QueueWebSocket()
    rtds = QueueWebSocket()
    factory = QueueWebSocketFactory({clob_url: [clob], rtds_url: [rtds]})
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token", "no-token"),
            rtds_subscriptions=(
                {
                    "topic": "crypto_prices",
                    "type": "update",
                    "filters": "btcusdt",
                },
            ),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=factory,
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(lambda: bool(clob.sent) and bool(rtds.sent))

    assert json.loads(clob.sent[0]) == {
        "assets_ids": ["yes-token", "no-token"],
        "type": "market",
        "custom_feature_enabled": True,
    }
    assert json.loads(rtds.sent[0]) == {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices",
                "type": "update",
                "filters": "btcusdt",
            }
        ],
    }

    await clob.incoming.put(
        '{"event_type":"book","asset_id":"yes-token","timestamp":"1721808000000",'
        '"hash":"server-book-hash","sequence":41,'
        '"bids":[{"price":"0.41","size":"10.0"}],'
        '"asks":[{"price":"0.43","size":"9.0"}]}'
    )
    await rtds.incoming.put(
        '{"topic":"crypto_prices","type":"update","timestamp":1721808000001,'
        '"payload":{"symbol":"btcusdt","value":65432.125}}'
    )
    await sink.wait_until(
        lambda: {
            (row["source"], row["event_type"])
            for row in sink.records
            if row["kind"] == "data"
        }
        >= {("clob_market_ws", "book"), ("rtds_ws", "crypto_prices.update")}
    )
    await recorder.stop()

    rows = [row for row in sink.records if row["kind"] == "data"]
    clob_row = next(row for row in rows if row["source"] == "clob_market_ws")
    rtds_row = next(row for row in rows if row["source"] == "rtds_ws")

    assert clob_row["payload"]["bids"][0] == {
        "price": "0.41",
        "size": "10.0",
    }
    assert clob_row["server_timestamp"] == "1721808000000"
    assert clob_row["server_hash"] == "server-book-hash"
    assert clob_row["sequence"] == 41
    assert clob_row["session_id"]
    assert clob_row["connection_id"]
    assert clob_row["session_id"] == rtds_row["session_id"]
    assert clob_row["received_at"] == "2026-07-24T08:00:00.000000Z"
    assert isinstance(clob_row["monotonic_ns"], int)
    assert clob_row["schema_version"] == "clob-market-ws.book.v1"
    assert clob_row["raw_frame"].startswith('{"event_type":"book"')

    # JSON floats are parsed as exact strings before they can enter persistence.
    assert rtds_row["payload"]["payload"]["value"] == "65432.125"
    assert rtds_row["schema_version"] == "rtds.crypto_prices.update.v1"
    assert clob.closed
    assert rtds.closed


@pytest.mark.asyncio
async def test_stream_heartbeats_use_their_documented_independent_cadence() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = QueueWebSocket()
    rtds = QueueWebSocket()
    sink = MemorySink()
    clock = ManualClock()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [clob], rtds_url: [rtds]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=clock,
    )

    await recorder.start()
    await sink.wait_until(lambda: bool(clob.sent) and bool(rtds.sent))
    await clock.wait_for_sleepers(2)

    await clock.advance(5)
    await sink.wait_until(lambda: rtds.sent.count("PING") == 1)
    assert clob.sent.count("PING") == 0

    # Let RTDS register its next five-second deadline before moving the clock.
    await clock.wait_for_sleepers(2)
    await clock.advance(5)
    await sink.wait_until(
        lambda: rtds.sent.count("PING") == 2 and clob.sent.count("PING") == 1
    )
    await recorder.stop()

    heartbeat_rows = [
        (row["source"], row["detail"]["interval_seconds"])
        for row in sink.records
        if row["kind"] == "lifecycle" and row["event_type"] == "heartbeat"
    ]
    assert ("rtds_ws", 5.0) in heartbeat_rows
    assert ("clob_market_ws", 10.0) in heartbeat_rows


@pytest.mark.asyncio
async def test_disconnect_reconnect_resnapshots_and_reports_capture_anomalies() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    first_clob = QueueWebSocket()
    second_clob = QueueWebSocket()
    rtds = QueueWebSocket()
    first_clob.incoming.put_nowait(
        '{"event_type":"book","hash":"same","timestamp":2000,'
        '"sequence":10,"market":"condition","bids":[],"asks":[]}'
    )
    first_clob.incoming.put_nowait(
        '{"event_type":"book","hash":"same","timestamp":2000,'
        '"sequence":10,"market":"condition","bids":[],"asks":[]}'
    )
    first_clob.incoming.put_nowait(
        '{"event_type":"price_change","hash":"second","timestamp":1000,'
        '"sequence":13,"price_changes":[],"market":"condition"}'
    )
    first_clob.incoming.put_nowait(
        '{"event_type":"price_change","hash":"third","timestamp":3000,'
        '"sequence":14,"price_changes":{"unexpected":"object"},'
        '"market":"condition"}'
    )
    first_clob.incoming.put_nowait(
        '{"event_type":"tick_size_change","hash":"fourth","timestamp":4000,'
        '"sequence":15,"market":"condition",'
        '"old_tick_size":"0.01","new_tick_size":"0.001"}'
    )
    first_clob.incoming.put_nowait(ConnectionError("simulated public feed drop"))
    sink = MemorySink()
    snapshots = RecordingSnapshotClient()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token", "no-token"),
            reconnect_initial_seconds=0,
            snapshot_intervals={},
            checkpoint_every_records=2,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [first_clob, second_clob],
                rtds_url: [rtds],
            }
        ),
        snapshot_client=snapshots,
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: any(row["event_type"] == "resnapshot" for row in sink.records)
        and {
            row["event_type"]
            for row in sink.records
            if row["kind"] == "diagnostic"
        }
        >= {
            "anomaly.duplicate",
            "anomaly.time_regression",
            "anomaly.sequence_gap",
            "anomaly.schema_drift",
            "anomaly.config_change",
        }
    )
    await recorder.stop()

    assert snapshots.calls == [
        ("full_book", ("yes-token", "no-token")),
    ]
    resnapshot = next(
        row for row in sink.records if row["event_type"] == "resnapshot"
    )
    assert resnapshot["reason"] == "websocket_reconnect"
    assert resnapshot["payload"]["books"] == [
        {"asset_id": "yes-token"},
        {"asset_id": "no-token"},
    ]

    lifecycle = [
        row["event_type"]
        for row in sink.records
        if row["source"] == "clob_market_ws" and row["kind"] == "lifecycle"
    ]
    assert lifecycle[:2] == ["connected", "subscribed"]
    assert "error" in lifecycle
    assert "disconnected" in lifecycle
    assert "reconnect_scheduled" in lifecycle
    assert lifecycle.count("connected") == 2
    connected_rows = [
        row
        for row in sink.records
        if row["source"] == "clob_market_ws"
        and row["kind"] == "lifecycle"
        and row["event_type"] == "connected"
    ]
    assert len({row["session_id"] for row in connected_rows}) == 1
    assert len({row["connection_id"] for row in connected_rows}) == 2

    gap = next(
        row
        for row in sink.records
        if row["event_type"] == "anomaly.sequence_gap"
    )
    assert gap["detail"] == {
        "entity": "clob_market_ws:condition",
        "after": 10,
        "before": 13,
        "missing_start": 11,
        "missing_end": 12,
        "missing_count": 2,
    }
    assert any(
        source == "clob_market_ws"
        and checkpoint["reason"] in {"record_interval", "disconnect", "stop"}
        and checkpoint["last_sequence"] == 15
        for source, checkpoint in sink.checkpoints
    )
    assert first_clob.closed
    assert second_clob.closed


@pytest.mark.asyncio
async def test_reconnect_does_not_consume_deltas_until_resnapshot_succeeds() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    first_clob = QueueWebSocket()
    failed_resnapshot_connection = QueueWebSocket()
    recovered_connection = QueueWebSocket()
    rtds = QueueWebSocket()
    first_clob.incoming.put_nowait(ConnectionError("feed disconnected"))
    failed_resnapshot_connection.incoming.put_nowait(
        '{"event_type":"book","asset_id":"yes-token","timestamp":9999,'
        '"sequence":99,"bids":[],"asks":[]}'
    )
    sink = MemorySink()
    snapshots = FlakyResnapshotClient()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            reconnect_initial_seconds=0,
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [
                    first_clob,
                    failed_resnapshot_connection,
                    recovered_connection,
                ],
                rtds_url: [rtds],
            }
        ),
        snapshot_client=snapshots,
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: any(
            row["event_type"] == "resync_complete" for row in sink.records
        )
    )
    await recovered_connection.incoming.put(
        '{"event_type":"book","asset_id":"yes-token","timestamp":1000,'
        '"sequence":1,"bids":[],"asks":[]}'
    )
    await sink.wait_until(
        lambda: any(
            row["kind"] == "data"
            and row["event_type"] == "book"
            and row["sequence"] == 1
            for row in sink.records
        )
    )
    await recorder.stop()

    assert snapshots.full_book_attempts == 2
    assert failed_resnapshot_connection.closed
    assert recovered_connection.closed
    assert len(
        [
            row
            for row in sink.records
            if row["source"] == "clob_market_ws"
            and row["event_type"] == "connected"
        ]
    ) == 3
    assert any(
        row["source"] == "clob_http"
        and row["event_type"] == "resnapshot_error"
        for row in sink.records
    )
    quarantined = [
        row
        for row in sink.records
        if row["kind"] == "quarantine" and row.get("sequence") == 99
    ]
    assert len(quarantined) == 1
    assert quarantined[0]["resync_status"] == "failed"
    assert quarantined[0]["replay_eligible"] is False
    assert not [
        row
        for row in sink.records
        if row["event_type"]
        in {"anomaly.time_regression", "anomaly.sequence_gap"}
    ]


@pytest.mark.asyncio
async def test_failed_resync_quarantines_late_frames_during_connection_close() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    first_clob = QueueWebSocket()
    failed_connection = FailedResyncRaceWebSocket()
    recovered_connection = QueueWebSocket()
    rtds = QueueWebSocket()
    first_clob.incoming.put_nowait(ConnectionError("feed disconnected"))
    sink = MemorySink()
    snapshots = CoordinatedFlakyResnapshotClient(failed_connection)
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            reconnect_initial_seconds=0,
            snapshot_intervals={},
            shutdown_timeout_seconds=0.01,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [first_clob, failed_connection, recovered_connection],
                rtds_url: [rtds],
            }
        ),
        snapshot_client=snapshots,
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: any(
            row["event_type"] == "resync_complete" for row in sink.records
        )
    )
    await recovered_connection.incoming.put(
        '{"event_type":"book","asset_id":"yes-token","timestamp":1000,'
        '"sequence":1,"bids":[],"asks":[]}'
    )
    await sink.wait_until(
        lambda: any(
            row["kind"] == "data"
            and row["event_type"] == "book"
            and row["sequence"] == 1
            for row in sink.records
        )
    )
    await recorder.stop()

    late_frames = [
        row
        for row in sink.records
        if row.get("sequence") in {98, 99}
    ]
    assert {row["sequence"] for row in late_frames} == {98, 99}
    assert all(row["kind"] == "quarantine" for row in late_frames)
    assert all(row["resync_status"] == "failed" for row in late_frames)
    assert all(row["replay_eligible"] is False for row in late_frames)
    assert not [
        row
        for row in sink.records
        if row["kind"] == "data" and row.get("sequence") in {98, 99}
    ]
    assert not [
        row
        for row in sink.records
        if row["event_type"]
        in {"anomaly.time_regression", "anomaly.sequence_gap"}
    ]
    clob_stop_checkpoint = next(
        checkpoint
        for source, checkpoint in reversed(sink.checkpoints)
        if source == "clob_market_ws" and checkpoint["reason"] == "stop"
    )
    assert clob_stop_checkpoint["last_sequence"] == 1
    assert (
        clob_stop_checkpoint["cursors"]["clob_market_ws:yes-token"][
            "last_sequence"
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_clob_batch_frame_keeps_every_supported_event_without_false_duplicates() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = QueueWebSocket()
    rtds = QueueWebSocket()
    supported = [
        "book",
        "price_change",
        "last_trade_price",
        "best_bid_ask",
        "tick_size_change",
        "new_market",
        "market_resolved",
    ]
    await clob.incoming.put(
        json.dumps(
            [
                {
                    "event_type": event_type,
                    "timestamp": 1721808000000 + index,
                    "asset_id": f"asset-{index}",
                }
                for index, event_type in enumerate(supported)
            ]
        )
    )
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [clob], rtds_url: [rtds]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: len(
            [
                row
                for row in sink.records
                if row["source"] == "clob_market_ws" and row["kind"] == "data"
            ]
        )
        == len(supported)
    )
    await recorder.stop()

    rows = [
        row
        for row in sink.records
        if row["source"] == "clob_market_ws" and row["kind"] == "data"
    ]
    assert [row["event_type"] for row in rows] == supported
    assert [row["frame_index"] for row in rows] == list(range(7))
    assert len({row["frame_hash"] for row in rows}) == 1
    assert not [
        row
        for row in sink.records
        if row["event_type"] == "anomaly.duplicate"
    ]


@pytest.mark.asyncio
async def test_invalid_utf8_is_retained_as_evidence_without_killing_the_stream() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = QueueWebSocket()
    rtds = QueueWebSocket()
    await clob.incoming.put(b"\xff")
    await clob.incoming.put(
        '{"event_type":"book","asset_id":"asset-a","bids":[],"asks":[]}'
    )
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("asset-a",),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [clob], rtds_url: [rtds]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: any(row["event_type"] == "parse_error" for row in sink.records)
        and any(
            row["kind"] == "data" and row["event_type"] == "book"
            for row in sink.records
        )
    )
    await recorder.stop()

    error = next(
        row for row in sink.records if row["event_type"] == "parse_error"
    )
    assert error["raw_frame_base64"] == "/w=="
    assert error["raw_bytes"] == 1
    assert not [
        row
        for row in sink.records
        if row["source"] == "clob_market_ws" and row["event_type"] == "error"
    ]


@pytest.mark.asyncio
async def test_anomaly_cursors_are_partitioned_and_empty_books_are_schema_compatible() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = QueueWebSocket()
    rtds = QueueWebSocket()
    await clob.incoming.put(
        '{"event_type":"book","asset_id":"asset-a","timestamp":2000,'
        '"sequence":10,"bids":[],"asks":[]}'
    )
    await clob.incoming.put(
        '{"event_type":"book","asset_id":"asset-b","timestamp":1000,'
        '"sequence":20,"bids":[],"asks":[]}'
    )
    await clob.incoming.put(
        '{"event_type":"book","asset_id":"asset-a","timestamp":3000,'
        '"sequence":11,"bids":[{"price":"0.4","size":"1"}],"asks":[]}'
    )
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("asset-a", "asset-b"),
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [clob], rtds_url: [rtds]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: len(
            [
                row
                for row in sink.records
                if row["source"] == "clob_market_ws" and row["kind"] == "data"
            ]
        )
        == 3
    )
    await recorder.stop()

    assert not [
        row
        for row in sink.records
        if row["event_type"]
        in {
            "anomaly.time_regression",
            "anomaly.sequence_gap",
            "anomaly.schema_drift",
        }
    ]


@pytest.mark.asyncio
async def test_public_http_snapshots_start_immediately_and_repeat_on_schedule() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = QueueWebSocket()
    rtds = QueueWebSocket()
    clock = ManualClock()
    sink = MemorySink()
    snapshots = RecordingSnapshotClient()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token", "no-token"),
            snapshot_intervals={
                "gamma": 30,
                "clob": 10,
                "rewards": 20,
                "rules": 40,
            },
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [clob], rtds_url: [rtds]}
        ),
        snapshot_client=snapshots,
        clock=clock,
    )

    await recorder.start()
    await sink.wait_until(lambda: len(snapshots.calls) >= 4)
    initial_counts = defaultdict(int)
    for kind, _ in snapshots.calls:
        initial_counts[kind] += 1
    assert initial_counts == {
        "gamma": 1,
        "clob": 1,
        "rewards": 1,
        "rules": 1,
    }
    assert ("clob", ("yes-token", "no-token")) in snapshots.calls
    assert all(
        not asset_ids
        for kind, asset_ids in snapshots.calls
        if kind != "clob"
    )

    # Four HTTP schedules plus two independent heartbeat schedules are armed.
    await clock.wait_for_sleepers(6)
    await clock.advance(10)
    await sink.wait_until(
        lambda: sum(kind == "clob" for kind, _ in snapshots.calls) == 2
    )
    await recorder.stop()

    snapshot_events = {
        (row["source"], row["event_type"])
        for row in sink.records
        if row["kind"] == "snapshot"
    }
    assert snapshot_events >= {
        ("gamma_http", "gamma_snapshot"),
        ("clob_http", "clob_snapshot"),
        ("rewards_http", "rewards_snapshot"),
        ("rules_http", "rules_snapshot"),
    }
    assert {
        source for source, _ in sink.checkpoints
    } >= {"gamma_http", "clob_http", "rewards_http", "rules_http"}
    assert not [
        row
        for row in sink.records
        if row["source"] == "clob_http"
        and row["event_type"] == "anomaly.duplicate"
    ]


@pytest.mark.asyncio
async def test_http_snapshot_floats_are_safe_for_decimal_exact_persistence() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            snapshot_intervals={"clob": 60},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [QueueWebSocket()],
                rtds_url: [QueueWebSocket()],
            }
        ),
        snapshot_client=FloatSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: any(row["event_type"] == "clob_snapshot" for row in sink.records)
    )
    await recorder.stop()

    snapshot = next(
        row for row in sink.records if row["event_type"] == "clob_snapshot"
    )
    assert snapshot["payload"]["book"]["price"] == "0.42"
    assert snapshot["payload"]["book"]["size"] == "10.5"


@pytest.mark.asyncio
async def test_http_snapshot_retains_exact_raw_body_and_response_metadata() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            snapshot_intervals={"clob": 60},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [QueueWebSocket()],
                rtds_url: [QueueWebSocket()],
            }
        ),
        snapshot_client=RawSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: any(row["event_type"] == "clob_snapshot" for row in sink.records)
    )
    await recorder.stop()

    snapshot = next(
        row for row in sink.records if row["event_type"] == "clob_snapshot"
    )
    assert snapshot["payload"]["book"]["price"] == "0.4200"
    assert snapshot["raw_body_base64"] == (
        "eyJib29rIjp7ImFzc2V0X2lkIjoieWVzLXRva2VuIiwicHJpY2UiOjAuNDIwMH19"
    )
    assert snapshot["raw_body_bytes"] == 48
    assert snapshot["http"]["status_code"] == 200
    assert snapshot["http"]["request_url"] == "https://clob.polymarket.com/book"


@pytest.mark.asyncio
async def test_failed_heartbeat_is_a_disconnect_and_reconnect_signal() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = QueueWebSocket()
    first_rtds = FailingPingWebSocket()
    second_rtds = QueueWebSocket()
    sink = MemorySink()
    clock = ManualClock()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            reconnect_initial_seconds=0,
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [clob],
                rtds_url: [first_rtds, second_rtds],
            }
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=clock,
    )

    await recorder.start()
    await sink.wait_until(
        lambda: bool(clob.sent) and bool(first_rtds.sent)
    )
    await clock.wait_for_sleepers(2)
    await clock.advance(5)
    await sink.wait_until(
        lambda: len(
            [
                row
                for row in sink.records
                if row["source"] == "rtds_ws"
                and row["event_type"] == "connected"
            ]
        )
        == 2
    )
    await recorder.stop()

    errors = [
        row
        for row in sink.records
        if row["source"] == "rtds_ws" and row["event_type"] == "error"
    ]
    assert len(errors) == 1
    assert errors[0]["detail"]["error_type"] == "ConnectionError"
    assert errors[0]["detail"]["error_code"] == "websocket_transport_failed"
    assert "message" not in errors[0]["detail"]
    assert first_rtds.closed
    assert second_rtds.closed


@pytest.mark.asyncio
async def test_rtds_receive_idle_timeout_is_a_disconnect_and_reconnect_signal(
) -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    first_rtds = QueueWebSocket()
    second_rtds = QueueWebSocket()
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            rtds_receive_idle_timeout_seconds=0.01,
            reconnect_initial_seconds=0,
            snapshot_intervals={},
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [QueueWebSocket()],
                rtds_url: [first_rtds, second_rtds],
            }
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    await sink.wait_until(
        lambda: len(
            [
                row
                for row in sink.records
                if row["source"] == "rtds_ws"
                and row["event_type"] == "connected"
            ]
        )
        == 2
    )
    await recorder.stop()

    idle_timeout = next(
        row
        for row in sink.records
        if row["source"] == "rtds_ws"
        and row["event_type"] == "receive_idle_timeout"
    )
    assert idle_timeout["detail"] == {"timeout_seconds": 0.01}
    assert first_rtds.closed
    assert second_rtds.closed


@pytest.mark.asyncio
async def test_bounded_run_cancels_blocked_reads_and_bounds_transport_close() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    clob = HangingCloseWebSocket()
    rtds = HangingCloseWebSocket()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            snapshot_intervals={},
            close_timeout_seconds=0.01,
        ),
        sink=MemorySink(),
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [clob], rtds_url: [rtds]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await asyncio.wait_for(
        recorder.run(run_for_seconds=0.001),
        timeout=0.2,
    )

    assert not recorder.running
    assert clob.close_started.is_set()
    assert rtds.close_started.is_set()
    # Stop is intentionally idempotent for cancellation/shutdown handlers.
    await recorder.stop()


@pytest.mark.asyncio
async def test_run_finally_and_external_stop_share_one_shutdown() -> None:
    clob_url = CLOB_MARKET_WS
    clob = QueueWebSocket()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            shutdown_timeout_seconds=1.0,
        ),
        sink=MemorySink(),
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    cancellation_started = asyncio.Event()
    release_worker = asyncio.Event()

    async def delayed_cancellation_worker() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await release_worker.wait()

    run_task = asyncio.create_task(recorder.run())
    while not recorder.running:
        await asyncio.sleep(0)
    delayed_worker = asyncio.create_task(delayed_cancellation_worker())
    recorder._tasks.append(delayed_worker)
    delayed_worker.add_done_callback(recorder._worker_done)

    external_stop = asyncio.create_task(recorder.stop())
    await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
    with pytest.raises(RecorderWorkerError) as restart_error:
        await recorder.start()
    assert restart_error.value.error_code == "recorder_shutdown_in_progress"
    run_finished_before_shared_stop = False
    try:
        await asyncio.wait_for(asyncio.shield(run_task), timeout=0.02)
        run_finished_before_shared_stop = True
    except asyncio.TimeoutError:
        pass
    finally:
        release_worker.set()
        await asyncio.wait_for(
            asyncio.gather(external_stop, run_task),
            timeout=0.2,
        )

    assert not run_finished_before_shared_stop
    assert delayed_worker.done()
    assert recorder._tasks == []


@pytest.mark.asyncio
async def test_restart_waits_until_outer_run_consumes_shared_stop() -> None:
    clob_url = CLOB_MARKET_WS
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
        ),
        sink=MemorySink(),
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [QueueWebSocket(), QueueWebSocket()]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    original_stop = recorder.stop
    run_reached_finally = asyncio.Event()
    allow_run_to_finish = asyncio.Event()

    async def gated_stop() -> None:
        if asyncio.current_task() is run_task:
            run_reached_finally.set()
            await allow_run_to_finish.wait()
        await original_stop()

    recorder.stop = gated_stop  # type: ignore[method-assign]
    run_task = asyncio.create_task(recorder.run())
    while not recorder.running:
        await asyncio.sleep(0)

    await original_stop()
    await asyncio.wait_for(run_reached_finally.wait(), timeout=0.2)
    assert not recorder.running
    with pytest.raises(RecorderWorkerError) as restart_error:
        await recorder.start()
    assert (
        restart_error.value.error_code
        == "recorder_previous_run_finalizing"
    )

    allow_run_to_finish.set()
    await asyncio.wait_for(run_task, timeout=0.2)
    await recorder.start()
    await recorder.stop()


@pytest.mark.asyncio
async def test_cancelled_stop_waiter_does_not_cancel_shared_shutdown() -> None:
    clob_url = CLOB_MARKET_WS
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            shutdown_timeout_seconds=1.0,
        ),
        sink=MemorySink(),
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [QueueWebSocket()]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    cancellation_started = asyncio.Event()
    release_worker = asyncio.Event()

    async def delayed_cancellation_worker() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await release_worker.wait()

    await recorder.start()
    delayed_worker = asyncio.create_task(delayed_cancellation_worker())
    recorder._tasks.append(delayed_worker)
    delayed_worker.add_done_callback(recorder._worker_done)

    first_waiter = asyncio.create_task(recorder.stop())
    await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    second_waiter = asyncio.create_task(recorder.stop())
    await asyncio.sleep(0)
    assert not second_waiter.done()

    release_worker.set()
    await asyncio.wait_for(second_waiter, timeout=0.2)
    assert delayed_worker.done()


@pytest.mark.asyncio
async def test_stop_hard_closes_transport_before_waiting_for_producers() -> None:
    clob_url = CLOB_MARKET_WS
    clob = QueueWebSocket()
    hard_close = HardCloseOnlyWebSocket()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            close_timeout_seconds=0.01,
            shutdown_timeout_seconds=0.1,
        ),
        sink=MemorySink(),
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    async def transport_bound_worker() -> None:
        while not hard_close.transport.aborted.is_set():
            try:
                await hard_close.transport.aborted.wait()
            except asyncio.CancelledError:
                continue

    await recorder.start()
    producer = asyncio.create_task(transport_bound_worker())
    recorder._tasks.append(producer)
    producer.add_done_callback(recorder._worker_done)
    recorder._active_connections[id(hard_close)] = hard_close

    await asyncio.wait_for(recorder.stop(), timeout=0.2)

    assert hard_close.close_started.is_set()
    assert hard_close.transport.aborted.is_set()
    assert producer.done()
    assert recorder.fatal_error is None


@pytest.mark.asyncio
async def test_close_failure_aborts_the_underlying_transport() -> None:
    connection = RaisingCloseWebSocket()
    recorder = PublicRecorder(
        config=RecorderConfig(clob_asset_ids=("yes-token",)),
        sink=MemorySink(),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder._close_connection(connection)

    assert connection.transport.aborted.is_set()


@pytest.mark.asyncio
async def test_cancellation_resistant_close_prevents_clean_stop() -> None:
    clob_url = CLOB_MARKET_WS
    stubborn_close = CancellationResistantCloseWebSocket()
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            close_timeout_seconds=0.005,
            shutdown_timeout_seconds=0.01,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [QueueWebSocket()]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await recorder.start()
    recorder._active_connections[id(stubborn_close)] = stubborn_close
    with pytest.raises(RecorderWorkerError) as stop_error:
        await recorder.stop()

    assert stop_error.value.error_type == "TimeoutError"
    assert stubborn_close.close_started.is_set()
    assert any(not task.done() for task in recorder._child_tasks)
    assert not [
        checkpoint
        for _, checkpoint in sink.checkpoints
        if checkpoint["reason"] == "stop"
    ]

    stubborn_close.release_close.set()
    await asyncio.gather(
        *tuple(recorder._child_tasks),
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_cancellation_resistant_clock_sleep_prevents_clean_stop() -> None:
    clob_url = CLOB_MARKET_WS
    clock = CancellationResistantClock()
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            shutdown_timeout_seconds=0.01,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [QueueWebSocket()]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=clock,
    )

    await recorder.start()
    await asyncio.wait_for(clock.sleep_started.wait(), timeout=0.2)
    with pytest.raises(RecorderWorkerError) as stop_error:
        await recorder.stop()

    assert stop_error.value.error_type == "TimeoutError"
    assert any(not task.done() for task in recorder._child_tasks)
    assert not [
        checkpoint
        for _, checkpoint in sink.checkpoints
        if checkpoint["reason"] == "stop"
    ]

    clock.release_sleep.set()
    await asyncio.gather(
        *tuple(recorder._tasks),
        *tuple(recorder._child_tasks),
        return_exceptions=True,
    )


@pytest.mark.asyncio
async def test_cancellation_resistant_producer_fails_every_stop_waiter() -> None:
    clob_url = CLOB_MARKET_WS
    clob = QueueWebSocket()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            shutdown_timeout_seconds=0.01,
        ),
        sink=MemorySink(),
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    cancellation_started = asyncio.Event()
    release_worker = asyncio.Event()

    async def cancellation_resistant_worker() -> None:
        while not release_worker.is_set():
            try:
                await release_worker.wait()
            except asyncio.CancelledError:
                cancellation_started.set()

    run_task = asyncio.create_task(recorder.run())
    while not recorder.running:
        await asyncio.sleep(0)
    stubborn_worker = asyncio.create_task(cancellation_resistant_worker())
    recorder._tasks.append(stubborn_worker)
    stubborn_worker.add_done_callback(recorder._worker_done)

    external_stop = asyncio.create_task(recorder.stop())
    await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
    stop_result, run_result = await asyncio.wait_for(
        asyncio.gather(
            external_stop,
            run_task,
            return_exceptions=True,
        ),
        timeout=0.2,
    )

    assert isinstance(stop_result, RecorderWorkerError)
    assert isinstance(run_result, RecorderWorkerError)
    assert stop_result is run_result
    assert stop_result.error_type == "TimeoutError"
    assert run_result.error_type == "TimeoutError"
    assert not recorder.running
    assert not stubborn_worker.done()

    release_worker.set()
    await asyncio.wait_for(stubborn_worker, timeout=0.2)


@pytest.mark.asyncio
async def test_stale_worker_callback_cannot_stop_restarted_generation() -> None:
    clob_url = CLOB_MARKET_WS
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            shutdown_timeout_seconds=0.01,
        ),
        sink=MemorySink(),
        websocket_factory=QueueWebSocketFactory(
            {clob_url: [QueueWebSocket(), QueueWebSocket()]}
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    release_old_worker = asyncio.Event()

    async def cancellation_resistant_worker() -> None:
        while not release_old_worker.is_set():
            try:
                await release_old_worker.wait()
            except asyncio.CancelledError:
                continue

    await recorder.start()
    old_generation = recorder._generation
    old_worker = asyncio.create_task(cancellation_resistant_worker())
    recorder._tasks.append(old_worker)
    old_worker.add_done_callback(
        lambda completed: recorder._worker_done(
            completed,
            generation=old_generation,
        )
    )
    with pytest.raises(RecorderWorkerError):
        await recorder.stop()

    release_old_worker.set()
    restart_task = asyncio.create_task(recorder.start())
    await asyncio.wait_for(restart_task, timeout=0.2)
    await asyncio.sleep(0)

    assert old_worker.done()
    assert recorder.running
    assert not recorder._stop.is_set()
    assert recorder.fatal_error is None
    await recorder.stop()


@pytest.mark.asyncio
async def test_cancellation_resistant_child_prevents_clean_checkpoint() -> None:
    clob_url = CLOB_MARKET_WS
    clob = QueueWebSocket()
    sink = MemorySink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            shutdown_timeout_seconds=0.01,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )
    cancellation_started = asyncio.Event()
    release_child = asyncio.Event()

    async def cancellation_resistant_child() -> None:
        while not release_child.is_set():
            try:
                await release_child.wait()
            except asyncio.CancelledError:
                cancellation_started.set()

    run_task = asyncio.create_task(recorder.run())
    while not recorder.running:
        await asyncio.sleep(0)
    child = recorder._create_child_task(cancellation_resistant_child())

    async def child_owner() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await recorder._cancel_child_task(child)

    owner = asyncio.create_task(child_owner())
    recorder._tasks.append(owner)
    owner.add_done_callback(recorder._worker_done)

    external_stop = asyncio.create_task(recorder.stop())
    await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
    stop_result, run_result = await asyncio.wait_for(
        asyncio.gather(
            external_stop,
            run_task,
            return_exceptions=True,
        ),
        timeout=0.2,
    )

    assert isinstance(stop_result, RecorderWorkerError)
    assert stop_result is run_result
    assert stop_result.error_type == "TimeoutError"
    assert not child.done()
    assert not [
        checkpoint
        for _, checkpoint in sink.checkpoints
        if checkpoint["reason"] == "stop"
    ]

    release_child.set()
    await asyncio.wait_for(child, timeout=0.2)
    await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_bounded_run_surfaces_a_stalled_persistence_sink() -> None:
    clob_url = CLOB_MARKET_WS
    rtds_url = RTDS_WS
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            rtds_url=rtds_url,
            clob_asset_ids=("yes-token",),
            snapshot_intervals={},
            sink_timeout_seconds=0.01,
        ),
        sink=HangingSink(),
        websocket_factory=QueueWebSocketFactory(
            {
                clob_url: [QueueWebSocket()],
                rtds_url: [QueueWebSocket()],
            }
        ),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    with pytest.raises(RecorderWorkerError, match="persistence"):
        await asyncio.wait_for(
            recorder.run(run_for_seconds=0.001),
            timeout=0.2,
        )
    assert not recorder.running


@pytest.mark.asyncio
async def test_bounded_run_allows_checkpoint_to_outlive_emit_timeout() -> None:
    clob_url = CLOB_MARKET_WS
    clob = QueueWebSocket()
    await clob.incoming.put(
        '{"event_type":"book","asset_id":"yes-token",'
        '"timestamp":1000,"sequence":1,"bids":[],"asks":[]}'
    )
    sink = SlowCheckpointSink()
    recorder = PublicRecorder(
        config=RecorderConfig(
            clob_url=clob_url,
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
            rtds_subscriptions=(),
            snapshot_intervals={},
            checkpoint_every_records=1,
            sink_timeout_seconds=0.01,
            checkpoint_timeout_seconds=0.1,
        ),
        sink=sink,
        websocket_factory=QueueWebSocketFactory({clob_url: [clob]}),
        snapshot_client=IdleSnapshotClient(),
        clock=FixedClock(),
    )

    await asyncio.wait_for(
        recorder.run(run_for_seconds=0.05),
        timeout=0.5,
    )

    assert recorder.fatal_error is None
    checkpoint_reasons = [
        checkpoint["reason"] for _, checkpoint in sink.checkpoints
    ]
    assert checkpoint_reasons[0] == "record_interval"
    assert checkpoint_reasons[-1] == "stop"


def test_current_chainlink_twap_topics_are_allowed_for_public_capture() -> None:
    config = RecorderConfig(
        clob_asset_ids=("yes-token",),
        rtds_subscriptions=(
            {
                "topic": "crypto_prices",
                "type": "update",
                "filters": "btcusdt",
            },
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
        ),
    )

    assert tuple(
        subscription["topic"] for subscription in config.rtds_subscriptions
    ) == (
        "crypto_prices",
        "crypto_prices_twap_thirty",
        "crypto_prices_twap_sixty",
    )


def test_default_binance_subscription_omits_broken_empty_filter() -> None:
    config = RecorderConfig(clob_asset_ids=("yes-token",))

    assert config.rtds_subscriptions == (
        {"topic": "crypto_prices", "type": "update"},
    )


def test_recorder_config_allows_rtds_only_capture_without_clob_assets() -> None:
    config = RecorderConfig(
        clob_enabled=False,
        clob_asset_ids=(),
        rtds_subscriptions=(
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            },
        ),
        snapshot_intervals={},
    )

    assert config.clob_enabled is False
    assert config.clob_asset_ids == ()
    assert config.rtds_subscriptions == (
        {
            "topic": "crypto_prices_twap_sixty",
            "type": "update",
            "filters": '{"symbol":"btc/usd"}',
        },
    )

@pytest.mark.parametrize(
    "overrides",
    [
        {
            "rtds_subscriptions": (
                {"topic": "activity", "type": "user", "filters": ""},
            )
        },
        {
            "rtds_subscriptions": (
                {
                    "topic": "crypto_prices",
                    "type": "update",
                    "filters": "",
                    "api_key": "must-not-be-accepted",
                },
            )
        },
        {"clob_url": "wss://ws-subscriptions-clob.polymarket.com/ws/user"},
        {
            "rtds_url": (
                "wss://ws-live-data.polymarket.com"
                "?" + "access_token=must-not-be-accepted"
            )
        },
        {"clob_url": "wss://evil.example/ws/market"},
        {"rtds_url": "wss://evil.example"},
    ],
)
def test_authenticated_or_user_channels_are_rejected_by_configuration(
    overrides: Mapping[str, Any],
) -> None:
    with pytest.raises(ValueError, match="authenticated|user|official"):
        RecorderConfig(clob_asset_ids=("yes-token",), **overrides)


def test_disabled_rtds_requires_an_explicit_empty_subscription_scope() -> None:
    with pytest.raises(ValueError, match="must be empty"):
        RecorderConfig(
            clob_asset_ids=("yes-token",),
            rtds_enabled=False,
        )
