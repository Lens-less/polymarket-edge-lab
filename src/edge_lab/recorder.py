"""Recoverable public-market capture for the Polymarket edge lab.

This module deliberately exposes only read-only market-data seams.  It does not
import the trading/authentication stack, inspect environment variables, create
orders, or subscribe to authenticated user channels.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import parse_qsl, urlsplit

from websockets.asyncio.client import ClientConnection

from .network_safety import (
    high_confidence_secret_findings,
    is_sensitive_parameter_name,
    safe_error_details,
    sanitized_response_headers,
)


CLOB_MARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"

CLOB_EVENT_TYPES = frozenset(
    {
        "book",
        "price_change",
        "last_trade_price",
        "best_bid_ask",
        "tick_size_change",
        "new_market",
        "market_resolved",
    }
)
PUBLIC_RTDS_TOPICS = frozenset(
    {
        "crypto_prices",
        "crypto_prices_chainlink",
        "crypto_prices_twap_thirty",
        "crypto_prices_twap_sixty",
        "equity_prices",
    }
)


class RecorderWorkerError(RuntimeError):
    """A capture worker or persistence boundary failed irrecoverably."""

    def __init__(self, *, error_type: str, error_code: str) -> None:
        self.error_type = error_type
        self.error_code = error_code
        super().__init__(f"{error_type}:{error_code}")


@dataclass(frozen=True)
class SnapshotEnvelope:
    """Lossless public HTTP evidence plus its decoded value."""

    payload: Any
    raw_body: bytes
    status_code: int
    request_url: str
    response_headers: Mapping[str, str] = field(default_factory=dict)
    requested_at: Any = None
    received_at: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_body, bytes):
            raise TypeError("raw_body must be bytes")
        parsed = urlsplit(self.request_url)
        query_keys = {
            key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        }
        header_names = {
            str(name).lower().replace("-", "_")
            for name in self.response_headers
        }
        if (
            parsed.scheme.lower() != "https"
            or parsed.username is not None
            or parsed.password is not None
            or any(is_sensitive_parameter_name(key) for key in query_keys)
            or any(
                token in name
                for name in header_names
                for token in (
                    "authorization",
                    "cookie",
                    "token",
                    "secret",
                    "api_key",
                    "signature",
                    "credential",
                )
            )
        ):
            raise ValueError(
                "authenticated HTTP snapshot metadata is forbidden"
            )
        object.__setattr__(
            self,
            "response_headers",
            MappingProxyType(sanitized_response_headers(self.response_headers)),
        )


class RecorderSink(Protocol):
    """Persistence boundary used by :class:`PublicRecorder`."""

    def emit(self, record: Mapping[str, Any]) -> Any | Awaitable[Any]:
        """Persist one immutable raw or diagnostic record."""

    def checkpoint(
        self, source: str, checkpoint: Mapping[str, Any]
    ) -> Any | Awaitable[Any]:
        """Atomically persist progress needed for a safe restart."""


class PublicSnapshotClient(Protocol):
    """Read-only HTTP boundary for public metadata and book snapshots."""

    def fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str] = ()
    ) -> Any | Awaitable[Any]:
        """Fetch a named public snapshot."""


class WebSocketConnection(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class WebSocketFactory(Protocol):
    async def connect(self, url: str) -> WebSocketConnection: ...


class RecorderClock(Protocol):
    def utcnow(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...

    async def sleep(self, delay: float) -> None: ...


class SystemClock:
    def utcnow(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(delay)


class RecorderClientConnection(ClientConnection):
    """Close safely when a transport fails before ``connection_made``.

    ``websockets`` initializes ``recv_messages`` in ``connection_made`` but
    unconditionally closes it from ``connection_lost``.  A proxy or TCP reset
    in between those callbacks therefore raises ``AttributeError`` and skips
    the remaining connection cleanup.  Keep the library's normal path intact
    and complete only the missing early-failure cleanup here.
    """

    def connection_lost(self, exc: Exception | None) -> None:
        if hasattr(self, "recv_messages"):
            super().connection_lost(exc)
            return

        self.protocol.receive_eof()
        self.set_recv_exc(exc)
        self.terminate_pending_pings()
        if self.keepalive_task is not None:
            self.keepalive_task.cancel()
        if not self.connection_lost_waiter.done():
            self.connection_lost_waiter.set_result(None)
        if self.paused:  # pragma: no cover - impossible before connection_made
            self.paused = False
            for waiter in self.drain_waiters:
                if not waiter.done():
                    if exc is None:
                        waiter.set_result(None)
                    else:
                        waiter.set_exception(exc)


class DefaultWebSocketFactory:
    """Open public streams without inheriting ambient proxy configuration.

    A caller may explicitly inject an unauthenticated loopback HTTP(S) proxy.
    This keeps the network route reproducible without allowing credentials or
    silently consulting process environment variables.
    """

    def __init__(self, *, proxy_url: str | None = None) -> None:
        if proxy_url is not None:
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
                    "WebSocket proxy must be an unauthenticated loopback "
                    "HTTP(S) URL"
                )
        self._proxy_url = proxy_url

    async def connect(self, url: str) -> WebSocketConnection:
        import websockets

        # Never inherit proxy/authentication settings from process environment.
        return await websockets.connect(
            url,
            proxy=self._proxy_url,
            create_connection=RecorderClientConnection,
            max_queue=1_024,
        )


def _default_rtds_subscriptions() -> tuple[Mapping[str, Any], ...]:
    return (
        {
            "topic": "crypto_prices",
            "type": "update",
        },
    )


def _default_snapshot_intervals() -> Mapping[str, float]:
    return {
        "gamma": 300.0,
        "clob": 60.0,
        "rewards": 300.0,
        "rules": 900.0,
    }


@dataclass(frozen=True)
class RecorderConfig:
    """All recorder inputs are explicit and contain no credentials."""

    clob_asset_ids: tuple[str, ...]
    rtds_subscriptions: tuple[Mapping[str, Any], ...] = field(
        default_factory=_default_rtds_subscriptions
    )
    rtds_enabled: bool = True
    initial_clob_resnapshot: bool = False
    clob_url: str = CLOB_MARKET_WS
    rtds_url: str = RTDS_WS
    market_heartbeat_seconds: float = 10.0
    rtds_heartbeat_seconds: float = 5.0
    reconnect_initial_seconds: float = 0.5
    reconnect_max_seconds: float = 30.0
    snapshot_intervals: Mapping[str, float] = field(
        default_factory=_default_snapshot_intervals
    )
    checkpoint_every_records: int = 100
    close_timeout_seconds: float = 2.0
    sink_timeout_seconds: float = 2.0
    checkpoint_timeout_seconds: Optional[float] = None
    connect_timeout_seconds: float = 15.0
    send_timeout_seconds: float = 5.0
    rtds_receive_idle_timeout_seconds: float = 90.0
    snapshot_timeout_seconds: float = 35.0
    shutdown_timeout_seconds: float = 5.0
    receive_buffer_max_messages: int = 10_000
    resync_buffer_max_messages: int = 10_000

    def __post_init__(self) -> None:
        if self.checkpoint_timeout_seconds is None:
            object.__setattr__(
                self,
                "checkpoint_timeout_seconds",
                self.sink_timeout_seconds,
            )
        numeric_values = {
            "market_heartbeat_seconds": self.market_heartbeat_seconds,
            "rtds_heartbeat_seconds": self.rtds_heartbeat_seconds,
            "reconnect_initial_seconds": self.reconnect_initial_seconds,
            "reconnect_max_seconds": self.reconnect_max_seconds,
            "close_timeout_seconds": self.close_timeout_seconds,
            "sink_timeout_seconds": self.sink_timeout_seconds,
            "checkpoint_timeout_seconds": self.checkpoint_timeout_seconds,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "send_timeout_seconds": self.send_timeout_seconds,
            "rtds_receive_idle_timeout_seconds": (
                self.rtds_receive_idle_timeout_seconds
            ),
            "snapshot_timeout_seconds": self.snapshot_timeout_seconds,
            "shutdown_timeout_seconds": self.shutdown_timeout_seconds,
        }
        if any(
            not math.isfinite(float(value))
            for value in numeric_values.values()
        ):
            raise ValueError("recorder timing values must be finite")
        if not self.clob_asset_ids:
            raise ValueError("clob_asset_ids must contain at least one public asset")
        if any(
            not isinstance(asset_id, str) or not asset_id.strip()
            for asset_id in self.clob_asset_ids
        ):
            raise ValueError("clob_asset_ids must contain non-empty strings")
        if not isinstance(self.rtds_enabled, bool):
            raise ValueError("rtds_enabled must be a boolean")
        if not isinstance(self.initial_clob_resnapshot, bool):
            raise ValueError("initial_clob_resnapshot must be a boolean")
        if self.rtds_enabled and not self.rtds_subscriptions:
            raise ValueError("rtds_subscriptions must not be empty")
        if not self.rtds_enabled and self.rtds_subscriptions:
            raise ValueError(
                "rtds_subscriptions must be empty when RTDS is disabled"
            )
        if self.market_heartbeat_seconds <= 0 or self.rtds_heartbeat_seconds <= 0:
            raise ValueError("heartbeat intervals must be positive")
        if self.reconnect_initial_seconds < 0:
            raise ValueError("reconnect_initial_seconds cannot be negative")
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            raise ValueError(
                "reconnect_max_seconds cannot be below reconnect_initial_seconds"
            )
        if self.checkpoint_every_records <= 0:
            raise ValueError("checkpoint_every_records must be positive")
        if (
            isinstance(self.receive_buffer_max_messages, bool)
            or not isinstance(self.receive_buffer_max_messages, int)
            or self.receive_buffer_max_messages <= 0
        ):
            raise ValueError("receive_buffer_max_messages must be positive")
        if self.resync_buffer_max_messages <= 0:
            raise ValueError("resync_buffer_max_messages must be positive")
        if self.close_timeout_seconds <= 0:
            raise ValueError("close_timeout_seconds must be positive")
        if self.sink_timeout_seconds <= 0:
            raise ValueError("sink_timeout_seconds must be positive")
        if (
            self.checkpoint_timeout_seconds is None
            or self.checkpoint_timeout_seconds <= 0
        ):
            raise ValueError("checkpoint_timeout_seconds must be positive")
        if (
            self.connect_timeout_seconds <= 0
            or self.send_timeout_seconds <= 0
            or self.rtds_receive_idle_timeout_seconds <= 0
            or self.snapshot_timeout_seconds <= 0
            or self.shutdown_timeout_seconds <= 0
        ):
            raise ValueError("recorder operation timeouts must be positive")
        for kind, interval in self.snapshot_intervals.items():
            if kind not in {"gamma", "clob", "rewards", "rules"}:
                raise ValueError(f"unsupported public snapshot kind: {kind}")
            if not math.isfinite(float(interval)) or interval <= 0:
                raise ValueError("snapshot intervals must be positive")
        for subscription in self.rtds_subscriptions:
            unexpected_keys = set(subscription) - {"topic", "type", "filters"}
            channel_values = {
                str(subscription.get(key, "")).strip().lower()
                for key in ("topic", "type", "channel")
            }
            topic = str(subscription.get("topic", "")).strip().lower()
            update_type = str(subscription.get("type", "")).strip().lower()
            filters = subscription.get("filters", "")
            if (
                unexpected_keys
                or topic not in PUBLIC_RTDS_TOPICS
                or update_type != "update"
                or not isinstance(filters, str)
                or channel_values & {"user", "auth", "authenticated"}
                or high_confidence_secret_findings(subscription)
            ):
                raise ValueError("authenticated/user RTDS subscriptions are forbidden")
        for url in (self.clob_url, self.rtds_url):
            parsed = urlsplit(url)
            path_segments = {
                segment.lower() for segment in parsed.path.split("/") if segment
            }
            if (
                parsed.scheme.lower() != "wss"
                or parsed.username is not None
                or parsed.password is not None
                or bool(parsed.query)
                or bool(parsed.fragment)
                or path_segments & {"user", "auth", "authenticated"}
            ):
                raise ValueError(
                    "authenticated/user websocket URLs are forbidden"
                )
        if self.clob_url != CLOB_MARKET_WS or self.rtds_url != RTDS_WS:
            raise ValueError(
                "recorder websocket URLs must be the exact official public "
                "CLOB market and RTDS endpoints"
            )
        object.__setattr__(
            self,
            "clob_asset_ids",
            tuple(str(asset_id) for asset_id in self.clob_asset_ids),
        )
        object.__setattr__(
            self,
            "rtds_subscriptions",
            tuple(
                MappingProxyType(dict(subscription))
                for subscription in self.rtds_subscriptions
            ),
        )
        object.__setattr__(
            self,
            "snapshot_intervals",
            MappingProxyType(dict(self.snapshot_intervals)),
        )


@dataclass
class _CursorState:
    last_event_at_ns: Optional[int] = None
    last_server_timestamp: Any = None
    last_sequence: Optional[int] = None


@dataclass(frozen=True)
class _CapturedFrame:
    raw: str | bytes
    received_at: str
    monotonic_ns: int
    resync_buffered: bool = False


@dataclass(frozen=True)
class _CapturedPrefixAction:
    action: Callable[[], Awaitable[Any]]
    result: asyncio.Future[Any]


class _ReceiveBufferOverflow(ConnectionError):
    def __init__(self, frame: _CapturedFrame) -> None:
        self.frame = frame
        super().__init__("receive buffer capacity exceeded")


class _ReceiveIdleTimeout(ConnectionError):
    def __init__(
        self,
        *,
        timeout_seconds: float,
        received_at: str,
        monotonic_ns: int,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.received_at = received_at
        self.monotonic_ns = monotonic_ns
        super().__init__("websocket receive idle timeout")


@dataclass
class _ResyncGate:
    active: bool = True
    failed: bool = False
    frames: list[_CapturedFrame] = field(default_factory=list)
    received_frames: int = 0
    asset_watermarks_ns: Optional[dict[str, int]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _SourceState:
    records: int = 0
    last_server_timestamp: Any = None
    last_sequence: Optional[int] = None
    last_server_hash: Optional[str] = None
    last_frame_hash: Optional[str] = None
    session_id: Optional[str] = None
    connection_id: Optional[str] = None
    recent_ids: dict[str, None] = field(default_factory=dict)
    cursors: dict[str, _CursorState] = field(default_factory=dict)
    schemas: dict[str, Any] = field(default_factory=dict)
    configs: dict[str, str] = field(default_factory=dict)


class PublicRecorder:
    """Capture public CLOB/RTDS streams plus periodic public HTTP snapshots."""

    def __init__(
        self,
        *,
        config: RecorderConfig,
        sink: RecorderSink,
        snapshot_client: PublicSnapshotClient,
        websocket_factory: Optional[WebSocketFactory] = None,
        clock: Optional[RecorderClock] = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.snapshot_client = snapshot_client
        self.websocket_factory = websocket_factory or DefaultWebSocketFactory()
        self.clock = clock or SystemClock()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._child_tasks: set[asyncio.Task[Any]] = set()
        self._stop_cancelled_tasks: set[asyncio.Task[Any]] = set()
        self._stop_task: Optional[asyncio.Task[None]] = None
        self._run_waiters = 0
        self._generation = 0
        self._running = False
        self._session_id: Optional[str] = None
        self._states: dict[str, _SourceState] = {}
        self._active_connections: dict[int, WebSocketConnection] = {}
        self._sink_lock = asyncio.Lock()
        self._capture_frontier_lock = asyncio.Lock()
        self._clob_capture_buffers: dict[
            str,
            asyncio.Queue[_CapturedFrame | _CapturedPrefixAction],
        ] = {}
        self._pending_capture_frontiers: dict[
            asyncio.Future[Any],
            asyncio.Queue[_CapturedFrame | _CapturedPrefixAction],
        ] = {}
        self._fatal_error: Optional[RecorderWorkerError] = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def fatal_error(self) -> Optional[RecorderWorkerError]:
        return self._fatal_error

    async def start(self) -> None:
        if self._running:
            if self._stop.is_set():
                raise RecorderWorkerError(
                    error_type="RuntimeError",
                    error_code="recorder_shutdown_in_progress",
                )
            return
        if self._run_waiters:
            raise RecorderWorkerError(
                error_type="RuntimeError",
                error_code="recorder_previous_run_finalizing",
            )
        unfinished = [
            task
            for task in (*self._tasks, *self._child_tasks)
            if not task.done()
        ]
        if unfinished:
            raise RecorderWorkerError(
                error_type="RuntimeError",
                error_code="recorder_previous_shutdown_incomplete",
            )
        self._stop_task = None
        self._stop.clear()
        self._stop_cancelled_tasks.clear()
        self._running = True
        self._generation += 1
        generation = self._generation
        self._session_id = uuid.uuid4().hex
        self._fatal_error = None
        self._states = {}
        self._tasks = [
            asyncio.create_task(
                self._stream_forever(
                    source="clob_market_ws",
                    url=self.config.clob_url,
                    subscription={
                        "assets_ids": list(self.config.clob_asset_ids),
                        "type": "market",
                        "custom_feature_enabled": True,
                    },
                    heartbeat_seconds=self.config.market_heartbeat_seconds,
                    resnapshot_on_reconnect=True,
                    receive_idle_timeout_seconds=None,
                ),
                name="edge-lab-clob-market-recorder",
            ),
        ]
        if self.config.rtds_enabled:
            self._tasks.append(
                asyncio.create_task(
                    self._stream_forever(
                        source="rtds_ws",
                        url=self.config.rtds_url,
                        subscription={
                            "action": "subscribe",
                            "subscriptions": [
                                dict(item)
                                for item in self.config.rtds_subscriptions
                            ],
                        },
                        heartbeat_seconds=self.config.rtds_heartbeat_seconds,
                        resnapshot_on_reconnect=False,
                        receive_idle_timeout_seconds=(
                            self.config.rtds_receive_idle_timeout_seconds
                        ),
                    ),
                    name="edge-lab-rtds-recorder",
                )
            )
        self._tasks.extend(
            asyncio.create_task(
                self._snapshot_forever(kind, interval),
                name=f"edge-lab-{kind}-snapshot-recorder",
            )
            for kind, interval in self.config.snapshot_intervals.items()
        )
        for task in self._tasks:
            task.add_done_callback(
                lambda completed, generation=generation: self._worker_done(
                    completed,
                    generation=generation,
                )
            )

    async def run(self, *, run_for_seconds: Optional[float] = None) -> None:
        """Run until stopped, or for a caller-bounded duration."""
        if run_for_seconds is not None and (
            not math.isfinite(float(run_for_seconds))
            or run_for_seconds < 0
        ):
            raise ValueError("run_for_seconds must be finite and non-negative")
        await self.start()
        self._run_waiters += 1
        try:
            try:
                if run_for_seconds is None:
                    await self._stop.wait()
                else:
                    await self._sleep_or_stop(run_for_seconds)
            finally:
                await self.stop()
        finally:
            self._run_waiters -= 1

    async def stop(self) -> None:
        stop_task = self._stop_task
        if stop_task is None:
            if not self._running:
                return
            self._stop.set()
            stop_task = asyncio.create_task(
                self._stop_once(),
                name="edge-lab-recorder-stop",
            )
            self._stop_task = stop_task
        await asyncio.shield(stop_task)

    async def run_at_clob_capture_frontier(
        self,
        action: Callable[[], Awaitable[Any]],
    ) -> Any | None:
        """Run ``action`` after every CLOB frame already received locally.

        The marker shares the transport reader's FIFO.  Frames stamped before
        the marker are therefore persisted first, while frames stamped later
        cannot overtake the action.  ``None`` means no single live CLOB stream
        was available, so callers must fail closed and retry later.
        """

        if not callable(action):
            raise TypeError("capture-frontier action must be callable")
        if not self._running or self._stop.is_set():
            return None
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Any] = loop.create_future()
        async with self._capture_frontier_lock:
            if len(self._clob_capture_buffers) != 1:
                return None
            frame_buffer = next(iter(self._clob_capture_buffers.values()))
            marker = _CapturedPrefixAction(action=action, result=result)
            try:
                frame_buffer.put_nowait(marker)
            except asyncio.QueueFull:
                return None
            self._pending_capture_frontiers[result] = frame_buffer
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            result.cancel()
            raise
        finally:
            self._pending_capture_frontiers.pop(result, None)

    async def _stop_once(self) -> None:
        """Execute one shared shutdown and surface an incomplete quiescence."""
        try:
            self._stop.set()
            top_level_tasks = set(self._tasks)
            tasks = tuple(
                dict.fromkeys((*self._tasks, *self._child_tasks))
            )
            for task in tasks:
                self._cancel_task_for_stop(task)

            # Closing transports before waiting is required for receive
            # implementations that defer cancellation until the socket dies.
            connections = tuple(self._active_connections.values())
            if connections:
                await asyncio.gather(
                    *(
                        self._close_connection(connection)
                        for connection in connections
                    ),
                    return_exceptions=True,
                )

            done: set[asyncio.Task[Any]] = set()
            pending: set[asyncio.Task[Any]] = set(tasks)
            deadline = (
                asyncio.get_running_loop().time()
                + self.config.shutdown_timeout_seconds
            )
            while True:
                late_children = {
                    task for task in self._child_tasks if not task.done()
                }
                for task in late_children:
                    self._cancel_task_for_stop(task)
                pending.update(late_children)
                if not pending:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                completed, still_pending = await asyncio.wait(
                    pending,
                    timeout=remaining,
                )
                done.update(completed)
                pending = set(still_pending)
                if pending:
                    continue
            if tasks or pending:
                if pending:
                    for task in pending:
                        task.cancel()
                    for connection in tuple(
                        self._active_connections.values()
                    ):
                        self._abort_connection_transport(connection)
                    self._remember_fatal(
                        "capture worker shutdown failed",
                        TimeoutError(
                            f"{len(pending)} worker(s) ignored cancellation"
                        ),
                    )
                for task in done:
                    if task not in top_level_tasks:
                        continue
                    if task.cancelled():
                        continue
                    result = task.exception()
                    if result is not None:
                        self._remember_fatal("capture worker failed", result)
            self._tasks = [
                task for task in pending if task in top_level_tasks
            ]
            for task in self._tasks:
                task.add_done_callback(_consume_task_result)

            # A stop checkpoint is only meaningful after every producer is
            # quiescent; a timed-out worker could otherwise write after it.
            if not pending and self._fatal_error is None:
                for source in tuple(self._states):
                    try:
                        await self._write_checkpoint(source, reason="stop")
                    except Exception as exc:
                        self._remember_fatal(
                            "persistence checkpoint failed",
                            exc,
                        )
        finally:
            self._running = False
        if self._fatal_error is not None:
            raise self._fatal_error

    def _worker_done(
        self,
        task: asyncio.Task[Any],
        *,
        generation: Optional[int] = None,
    ) -> None:
        self._stop_cancelled_tasks.discard(task)
        if generation is not None and generation != self._generation:
            _consume_task_result(task)
            return
        if task.cancelled() or self._stop.is_set():
            return
        exception = task.exception()
        if exception is None:
            exception = RuntimeError(
                f"capture worker {task.get_name()} stopped unexpectedly"
            )
        self._remember_fatal(
            f"capture worker {task.get_name()} failed",
            exception,
        )
        self._stop.set()

    def _remember_fatal(self, context: str, error: BaseException) -> None:
        if self._fatal_error is not None:
            return
        details = safe_error_details(
            error,
            code="recorder_worker_failed",
        )
        if isinstance(error, RecorderWorkerError):
            details = {
                "error_type": error.error_type,
                "error_code": error.error_code,
            }
        self._fatal_error = RecorderWorkerError(**details)

    async def _stream_forever(
        self,
        *,
        source: str,
        url: str,
        subscription: Mapping[str, Any],
        heartbeat_seconds: float,
        resnapshot_on_reconnect: bool,
        receive_idle_timeout_seconds: float | None,
    ) -> None:
        attempt = 0
        connection_number = 0
        while not self._stop.is_set():
            if self._session_id is None:
                raise RuntimeError("recorder session was not initialized")
            session_id = self._session_id
            connection_id = uuid.uuid4().hex
            state = self._state(source)
            state.session_id = session_id
            state.connection_id = connection_id
            connection: Optional[WebSocketConnection] = None
            heartbeat: Optional[asyncio.Task[Any]] = None
            receiver: Optional[asyncio.Task[Any]] = None
            resnapshot_task: Optional[asyncio.Task[Any]] = None
            resync_gate: Optional[_ResyncGate] = None
            data_received = asyncio.Event()
            persistence_failed = False
            disconnected_received_at: Optional[str] = None
            disconnected_monotonic_ns: Optional[int] = None

            def mark_disconnected(
                received_at: str | None = None,
                monotonic_ns: int | None = None,
            ) -> None:
                nonlocal disconnected_received_at
                nonlocal disconnected_monotonic_ns
                if disconnected_received_at is not None:
                    return
                disconnected_received_at = (
                    received_at or _utc_text(self.clock.utcnow())
                )
                disconnected_monotonic_ns = (
                    monotonic_ns
                    if monotonic_ns is not None
                    else self.clock.monotonic_ns()
                )

            try:
                connection = await asyncio.wait_for(
                    self.websocket_factory.connect(url),
                    timeout=self.config.connect_timeout_seconds,
                )
                self._active_connections[id(connection)] = connection
                connection_number += 1
                await self._emit_lifecycle(
                    source,
                    "connected",
                    session_id=session_id,
                    connection_id=connection_id,
                    detail={"url": url, "connection_number": connection_number},
                )
                await asyncio.wait_for(
                    connection.send(
                        json.dumps(
                            subscription,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    ),
                    timeout=self.config.send_timeout_seconds,
                )
                await self._emit_lifecycle(
                    source,
                    "subscribed",
                    session_id=session_id,
                    connection_id=connection_id,
                    detail={"subscription": subscription},
                )
                heartbeat = self._create_child_task(
                    self._heartbeat_loop(
                        source=source,
                        connection=connection,
                        seconds=heartbeat_seconds,
                        session_id=session_id,
                        connection_id=connection_id,
                    )
                )
                is_resync = resnapshot_on_reconnect and (
                    connection_number > 1
                    or self.config.initial_clob_resnapshot
                )
                if is_resync:
                    resync_gate = _ResyncGate()
                    await self._emit_lifecycle(
                        source,
                        "resync_begin",
                        session_id=session_id,
                        connection_id=connection_id,
                        detail={"connection_number": connection_number},
                    )
                receiver = self._create_child_task(
                    self._receive_loop(
                        source=source,
                        connection=connection,
                        session_id=session_id,
                        connection_id=connection_id,
                        data_received=data_received,
                        resync_gate=resync_gate,
                        receive_idle_timeout_seconds=(
                            receive_idle_timeout_seconds
                        ),
                        on_transport_stopped=mark_disconnected,
                    )
                )
                if is_resync:
                    resnapshot_task = self._create_child_task(
                        self._capture_resnapshot(
                            session_id=session_id,
                            connection_id=connection_id,
                            reason=(
                                "initial_websocket_connect"
                                if connection_number == 1
                                else "websocket_reconnect"
                            ),
                        )
                    )
                    resync_done, _ = await asyncio.wait(
                        {resnapshot_task, receiver, heartbeat},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if resnapshot_task not in resync_done:
                        mark_disconnected()
                        await self._cancel_child_task(resnapshot_task)
                        if receiver in resync_done:
                            receiver.result()
                            raise ConnectionError(
                                f"{source} disconnected during resnapshot"
                            )
                        heartbeat.result()
                        raise ConnectionError(
                            f"{source} heartbeat failed during resnapshot"
                    )
                    asset_watermarks_ns = resnapshot_task.result()
                    resnapshot_task = None
                    buffered_count = await self._flush_resync_gate(
                        resync_gate,
                        status="success",
                        asset_watermarks_ns=asset_watermarks_ns,
                    )
                    watermark_ns = max(asset_watermarks_ns.values())
                    await self._emit_lifecycle(
                        source,
                        "resync_complete",
                        session_id=session_id,
                        connection_id=connection_id,
                        detail={
                            "connection_number": connection_number,
                            "buffered_count": buffered_count,
                            "watermark_ns": watermark_ns,
                            "asset_watermarks_ns": dict(
                                sorted(asset_watermarks_ns.items())
                            ),
                        },
                    )
                done, _ = await asyncio.wait(
                    {receiver, heartbeat},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if data_received.is_set():
                    # A confirmed data frame proves the connection recovered.
                    attempt = 0
                if receiver in done:
                    mark_disconnected()
                    receiver.result()
                    raise ConnectionError(
                        f"{source} receive loop ended unexpectedly"
                    )
                if heartbeat in done:
                    mark_disconnected()
                    heartbeat.result()
                    raise ConnectionError(
                        f"{source} heartbeat loop ended unexpectedly"
                    )
            except asyncio.CancelledError:
                mark_disconnected()
                raise
            except RecorderWorkerError:
                mark_disconnected()
                persistence_failed = True
                raise
            except Exception as exc:
                mark_disconnected()
                if not self._stop.is_set():
                    if resync_gate is not None and resync_gate.active:
                        if receiver is not None:
                            await self._cancel_child_task(receiver)
                            receiver = None
                        await self._flush_resync_gate(
                            resync_gate,
                            status="failed",
                            asset_watermarks_ns=None,
                        )
                    await self._emit_lifecycle(
                        source,
                        "error",
                        session_id=session_id,
                        connection_id=connection_id,
                        detail=safe_error_details(
                            exc,
                            code="websocket_transport_failed",
                        ),
                    )
            finally:
                mark_disconnected()
                if resnapshot_task is not None:
                    await self._cancel_child_task(resnapshot_task)
                if (
                    resync_gate is not None
                    and resync_gate.active
                    and not persistence_failed
                ):
                    if receiver is not None:
                        await self._cancel_child_task(receiver)
                        receiver = None
                    await self._flush_resync_gate(
                        resync_gate,
                        status="failed",
                        asset_watermarks_ns=None,
                    )
                if heartbeat is not None:
                    await self._cancel_child_task(heartbeat)
                if connection is not None:
                    self._active_connections.pop(id(connection), None)
                    await self._close_connection(connection)
                if receiver is not None:
                    await self._cancel_child_task(receiver)
                await self._emit_lifecycle(
                    source,
                    "disconnected",
                    session_id=session_id,
                    connection_id=connection_id,
                    detail={},
                    received_at=disconnected_received_at,
                    monotonic_ns=disconnected_monotonic_ns,
                )
                await self._write_checkpoint(source, reason="disconnect")
            if self._stop.is_set():
                break
            delay = (
                0.0
                if self.config.reconnect_initial_seconds == 0
                else min(
                    self.config.reconnect_initial_seconds
                    * (2 ** min(attempt, 63)),
                    self.config.reconnect_max_seconds,
                )
            )
            attempt += 1
            await self._emit_lifecycle(
                source,
                "reconnect_scheduled",
                session_id=session_id,
                connection_id=connection_id,
                detail={"backoff_seconds": delay, "attempt": attempt},
            )
            await self._sleep_or_stop(delay)

    def _create_child_task(
        self,
        coroutine: Awaitable[Any],
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._child_tasks.add(task)
        task.add_done_callback(self._child_done)
        return task

    def _child_done(self, task: asyncio.Task[Any]) -> None:
        self._child_tasks.discard(task)
        self._stop_cancelled_tasks.discard(task)
        _consume_task_result(task)

    def _cancel_task_for_stop(self, task: asyncio.Task[Any]) -> None:
        if task in self._stop_cancelled_tasks:
            return
        self._stop_cancelled_tasks.add(task)
        task.cancel()

    async def _cancel_child_task(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        if self._stop.is_set():
            self._cancel_task_for_stop(task)
        else:
            task.cancel()
        done, pending = await asyncio.wait(
            {task},
            timeout=self.config.shutdown_timeout_seconds,
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)
        for orphan in pending:
            orphan.add_done_callback(_consume_task_result)

    async def _receive_loop(
        self,
        *,
        source: str,
        connection: WebSocketConnection,
        session_id: str,
        connection_id: str,
        data_received: asyncio.Event,
        resync_gate: Optional[_ResyncGate],
        receive_idle_timeout_seconds: float | None,
        on_transport_stopped: Callable[[str, int], None],
    ) -> None:
        frame_buffer: asyncio.Queue[
            _CapturedFrame | _CapturedPrefixAction
        ] = asyncio.Queue(
            maxsize=self.config.receive_buffer_max_messages
        )
        if source == "clob_market_ws":
            async with self._capture_frontier_lock:
                self._clob_capture_buffers[connection_id] = frame_buffer
        reader = asyncio.create_task(
            self._read_captured_frames(
                connection=connection,
                frame_buffer=frame_buffer,
                resync_gate=resync_gate,
                receive_idle_timeout_seconds=receive_idle_timeout_seconds,
                on_transport_stopped=on_transport_stopped,
            ),
            name=f"edge-lab-{source}-transport-reader",
        )
        inflight: asyncio.Task[None] | None = None
        try:
            while not self._stop.is_set():
                frame = await self._next_captured_frame(
                    frame_buffer=frame_buffer,
                    reader=reader,
                )
                terminal_error: BaseException | None = None
                if frame is None:
                    reader_result = (
                        await asyncio.gather(
                            reader,
                            return_exceptions=True,
                        )
                    )[0]
                    if isinstance(reader_result, _ReceiveBufferOverflow):
                        frame = reader_result.frame
                        terminal_error = ConnectionError(
                            "receive buffer capacity exceeded"
                        )
                    elif isinstance(reader_result, _ReceiveIdleTimeout):
                        await self._emit_lifecycle(
                            source,
                            "receive_idle_timeout",
                            session_id=session_id,
                            connection_id=connection_id,
                            detail={
                                "timeout_seconds": (
                                    reader_result.timeout_seconds
                                ),
                            },
                            received_at=reader_result.received_at,
                            monotonic_ns=reader_result.monotonic_ns,
                        )
                        raise ConnectionError(
                            f"{source} receive idle timeout"
                        ) from reader_result
                    elif isinstance(reader_result, BaseException):
                        raise reader_result
                    else:
                        raise ConnectionError(
                            f"{source} transport reader ended unexpectedly"
                        )

                inflight = asyncio.create_task(
                    self._consume_capture_buffer_item(
                        source=source,
                        item=frame,
                        session_id=session_id,
                        connection_id=connection_id,
                        data_received=data_received,
                        resync_gate=resync_gate,
                    )
                )
                # Shield the one frame already removed from the queue.  A
                # graceful stop can then close the transport and drain every
                # frame stamped before that boundary without retrying a sink
                # call whose filesystem side effect may already be in flight.
                await asyncio.shield(inflight)
                inflight = None
                if terminal_error is not None:
                    raise terminal_error
        except asyncio.CancelledError as cancellation:
            # Some transports return a final frame while cancellation or close
            # is in progress.  Keep draining until the reader acknowledges a
            # cancellation; repeated cancellation requests are used to stop a
            # transport that yielded such a late frame.  Shield each in-flight
            # persistence call so it is neither retried nor silently dropped.
            overflow_boundary_consumed = False
            while True:
                if not reader.done():
                    reader.cancel()
                try:
                    if inflight is not None:
                        await asyncio.shield(inflight)
                        inflight = None
                    while True:
                        buffered_frame = await self._next_captured_frame(
                            frame_buffer=frame_buffer,
                            reader=reader,
                        )
                        if buffered_frame is None:
                            reader_result = (
                                await asyncio.gather(
                                    reader,
                                    return_exceptions=True,
                                )
                            )[0]
                            if isinstance(
                                reader_result,
                                _ReceiveBufferOverflow,
                            ):
                                if overflow_boundary_consumed:
                                    break
                                overflow_boundary_consumed = True
                                buffered_frame = reader_result.frame
                            else:
                                break
                        inflight = asyncio.create_task(
                            self._consume_capture_buffer_item(
                                source=source,
                                item=buffered_frame,
                                session_id=session_id,
                                connection_id=connection_id,
                                data_received=data_received,
                                resync_gate=resync_gate,
                            )
                        )
                        await asyncio.shield(inflight)
                        inflight = None
                    break
                except asyncio.CancelledError:
                    if inflight is not None and inflight.cancelled():
                        raise RecorderWorkerError(
                            error_type="CancelledError",
                            error_code="persistence_sink_cancelled",
                        )
                    continue
            raise cancellation
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            if source == "clob_market_ws":
                async with self._capture_frontier_lock:
                    if (
                        self._clob_capture_buffers.get(connection_id)
                        is frame_buffer
                    ):
                        self._clob_capture_buffers.pop(connection_id, None)
                    abandoned = [
                        future
                        for future, queue in self._pending_capture_frontiers.items()
                        if queue is frame_buffer and not future.done()
                    ]
                    for future in abandoned:
                        future.set_result(None)

    async def _read_captured_frames(
        self,
        *,
        connection: WebSocketConnection,
        frame_buffer: asyncio.Queue[
            _CapturedFrame | _CapturedPrefixAction
        ],
        resync_gate: Optional[_ResyncGate],
        receive_idle_timeout_seconds: float | None,
        on_transport_stopped: Callable[[str, int], None],
    ) -> None:
        try:
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(
                        connection.recv(),
                        timeout=receive_idle_timeout_seconds,
                    )
                except TimeoutError as exc:
                    raise _ReceiveIdleTimeout(
                        timeout_seconds=(
                            receive_idle_timeout_seconds
                            if receive_idle_timeout_seconds is not None
                            else 0.0
                        ),
                        received_at=_utc_text(self.clock.utcnow()),
                        monotonic_ns=self.clock.monotonic_ns(),
                    ) from exc
                async with self._capture_frontier_lock:
                    resync_buffered = bool(
                        resync_gate is not None and resync_gate.active
                    )
                    frame = _CapturedFrame(
                        raw=raw,
                        received_at=_utc_text(self.clock.utcnow()),
                        monotonic_ns=self.clock.monotonic_ns(),
                        resync_buffered=resync_buffered,
                    )
                    if resync_gate is not None and resync_buffered:
                        resync_gate.received_frames += 1
                    try:
                        frame_buffer.put_nowait(frame)
                    except asyncio.QueueFull as exc:
                        raise _ReceiveBufferOverflow(frame) from exc
                    if (
                        resync_gate is not None
                        and resync_buffered
                        and resync_gate.received_frames
                        > self.config.resync_buffer_max_messages
                    ):
                        raise ConnectionError(
                            "resync buffer capacity exceeded"
                        )
        finally:
            on_transport_stopped(
                _utc_text(self.clock.utcnow()),
                self.clock.monotonic_ns(),
            )

    async def _next_captured_frame(
        self,
        *,
        frame_buffer: asyncio.Queue[
            _CapturedFrame | _CapturedPrefixAction
        ],
        reader: asyncio.Task[None],
    ) -> _CapturedFrame | _CapturedPrefixAction | None:
        while True:
            try:
                return frame_buffer.get_nowait()
            except asyncio.QueueEmpty:
                pass
            if reader.done():
                return None
            getter = asyncio.create_task(frame_buffer.get())
            try:
                done, _ = await asyncio.wait(
                    {getter, reader},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if getter in done:
                    return getter.result()
            finally:
                if not getter.done():
                    getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)

    async def _consume_capture_buffer_item(
        self,
        *,
        source: str,
        item: _CapturedFrame | _CapturedPrefixAction,
        session_id: str,
        connection_id: str,
        data_received: asyncio.Event,
        resync_gate: Optional[_ResyncGate],
    ) -> None:
        if isinstance(item, _CapturedPrefixAction):
            if item.result.cancelled() or self._stop.is_set():
                if not item.result.done():
                    item.result.set_result(None)
                return
            try:
                value = await item.action()
            except BaseException as exc:
                if not item.result.done():
                    item.result.set_exception(exc)
            else:
                if not item.result.done():
                    item.result.set_result(value)
            return
        await self._consume_captured_frame(
            source=source,
            frame=item,
            session_id=session_id,
            connection_id=connection_id,
            data_received=data_received,
            resync_gate=resync_gate,
        )

    async def _consume_captured_frame(
        self,
        *,
        source: str,
        frame: _CapturedFrame,
        session_id: str,
        connection_id: str,
        data_received: asyncio.Event,
        resync_gate: Optional[_ResyncGate],
    ) -> None:
        resync_status: str | None = None
        asset_watermarks_ns: Optional[Mapping[str, int]] = None
        if resync_gate is not None:
            async with resync_gate.lock:
                if frame.resync_buffered and resync_gate.active:
                    resync_gate.frames.append(frame)
                    data_received.set()
                    return
                if frame.resync_buffered:
                    resync_status = (
                        "failed" if resync_gate.failed else "success"
                    )
                    asset_watermarks_ns = resync_gate.asset_watermarks_ns
                elif resync_gate.failed:
                    resync_status = "failed"
        await self._capture_frame(
            source=source,
            raw=frame.raw,
            session_id=session_id,
            connection_id=connection_id,
            received_at=frame.received_at,
            monotonic_ns=frame.monotonic_ns,
            resync_status=resync_status,
            resync_asset_watermarks_ns=asset_watermarks_ns,
        )
        data_received.set()

    async def _flush_resync_gate(
        self,
        gate: _ResyncGate,
        *,
        status: str,
        asset_watermarks_ns: Optional[Mapping[str, int]],
    ) -> int:
        async with gate.lock:
            if not gate.active:
                return 0
            frames = list(gate.frames)
            gate.frames.clear()
            for frame in frames:
                await self._capture_frame(
                    source="clob_market_ws",
                    raw=frame.raw,
                    session_id=self._session_id or "",
                    connection_id=(
                        self._state("clob_market_ws").connection_id or ""
                    ),
                    received_at=frame.received_at,
                    monotonic_ns=frame.monotonic_ns,
                    resync_status=status,
                    resync_asset_watermarks_ns=asset_watermarks_ns,
                )
            gate.failed = status == "failed"
            gate.asset_watermarks_ns = (
                None
                if asset_watermarks_ns is None
                else dict(asset_watermarks_ns)
            )
            gate.active = False
            return len(frames)

    async def _heartbeat_loop(
        self,
        *,
        source: str,
        connection: WebSocketConnection,
        seconds: float,
        session_id: str,
        connection_id: str,
    ) -> None:
        while not self._stop.is_set():
            await self._sleep_or_stop(seconds)
            if self._stop.is_set():
                return
            await asyncio.wait_for(
                connection.send("PING"),
                timeout=self.config.send_timeout_seconds,
            )
            await self._emit_lifecycle(
                source,
                "heartbeat",
                session_id=session_id,
                connection_id=connection_id,
                detail={"message": "PING", "interval_seconds": seconds},
            )

    async def _capture_frame(
        self,
        *,
        source: str,
        raw: str | bytes,
        session_id: str,
        connection_id: str,
        received_at: Optional[str] = None,
        monotonic_ns: Optional[int] = None,
        resync_status: Optional[str] = None,
        resync_asset_watermarks_ns: Optional[Mapping[str, int]] = None,
    ) -> None:
        received_at = received_at or _utc_text(self.clock.utcnow())
        if monotonic_ns is None:
            monotonic_ns = self.clock.monotonic_ns()
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        frame_hash = hashlib.sha256(raw_bytes).hexdigest()
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            await self._emit_record(
                {
                    **self._record_base(
                        source=source,
                        kind="diagnostic",
                        event_type="parse_error",
                        session_id=session_id,
                        connection_id=connection_id,
                        received_at=received_at,
                        monotonic_ns=monotonic_ns,
                    ),
                    "schema_version": "edge-lab-recorder.parse-error.v1",
                    "frame_hash": frame_hash,
                    "raw_frame_base64": base64.b64encode(raw_bytes).decode("ascii"),
                    "raw_bytes": len(raw_bytes),
                    **safe_error_details(
                        exc,
                        code="frame_utf8_decode_failed",
                    ),
                }
            )
            return
        if raw_text.strip().upper() == "PONG":
            await self._emit_record(
                {
                    **self._record_base(
                        source=source,
                        kind="lifecycle",
                        event_type="heartbeat_ack",
                        session_id=session_id,
                        connection_id=connection_id,
                        received_at=received_at,
                        monotonic_ns=monotonic_ns,
                    ),
                    "schema_version": "edge-lab-recorder.heartbeat-ack.v1",
                    "frame_hash": frame_hash,
                    "raw_frame": raw_text,
                }
            )
            return
        try:
            parsed = json.loads(
                raw_text,
                parse_float=str,
                parse_constant=lambda value: value,
            )
        except json.JSONDecodeError as exc:
            await self._emit_record(
                {
                    **self._record_base(
                        source=source,
                        kind="diagnostic",
                        event_type="parse_error",
                        session_id=session_id,
                        connection_id=connection_id,
                        received_at=received_at,
                        monotonic_ns=monotonic_ns,
                    ),
                    "frame_hash": frame_hash,
                    "raw_frame": raw_text,
                    **safe_error_details(
                        exc,
                        code="frame_json_decode_failed",
                    ),
                }
            )
            return
        events = parsed if isinstance(parsed, list) else [parsed]
        for index, payload in enumerate(events):
            if not isinstance(payload, Mapping):
                payload = {"value": payload}
            event_type = _event_type(source, payload)
            server_timestamp = _first_nested(
                payload,
                (
                    "timestamp",
                    "ts",
                    "event_timestamp",
                    "eventTimestamp",
                ),
            )
            server_hash = _first_nested(payload, ("hash", "event_hash", "eventHash"))
            sequence = _optional_int(
                _first_nested(payload, ("sequence", "seq", "sequence_number"))
            )
            schema_version = _schema_version(source, event_type)
            record = {
                **self._record_base(
                    source=source,
                    kind="data",
                    event_type=event_type,
                    session_id=session_id,
                    connection_id=connection_id,
                    received_at=received_at,
                    monotonic_ns=monotonic_ns,
                ),
                "schema_version": schema_version,
                "server_timestamp": server_timestamp,
                "event_at": _event_at_text(server_timestamp),
                "server_hash": str(server_hash) if server_hash is not None else None,
                "sequence": sequence,
                "frame_hash": frame_hash,
                "payload_hash": _stable_hash(payload),
                "frame_index": index,
                "payload": dict(payload),
                "raw_frame": raw_text,
            }
            if resync_status is None:
                await self._inspect_and_emit(record)
                continue
            event_ns = _timestamp_ns(server_timestamp)
            event_asset_ids = _event_asset_ids(payload)
            relevant_watermarks = (
                []
                if resync_asset_watermarks_ns is None
                else [
                    resync_asset_watermarks_ns[asset_id]
                    for asset_id in event_asset_ids
                    if asset_id in resync_asset_watermarks_ns
                ]
            )
            asset_scope_complete = (
                bool(event_asset_ids)
                and resync_asset_watermarks_ns is not None
                and len(relevant_watermarks) == len(event_asset_ids)
            )
            event_watermark_ns = (
                max(relevant_watermarks)
                if asset_scope_complete
                else None
            )
            replay_eligible = (
                resync_status == "success"
                and asset_scope_complete
                and event_watermark_ns is not None
                and event_ns is not None
                and event_ns > event_watermark_ns
            )
            buffered_record = {
                **record,
                "kind": "data" if replay_eligible else "quarantine",
                "resync_buffered": True,
                "resync_status": resync_status,
                "resync_watermark_ns": event_watermark_ns,
                "resync_asset_ids": list(event_asset_ids),
                "resync_asset_watermarks_ns": (
                    None
                    if resync_asset_watermarks_ns is None
                    else {
                        asset_id: resync_asset_watermarks_ns[asset_id]
                        for asset_id in event_asset_ids
                        if asset_id in resync_asset_watermarks_ns
                    }
                ),
                "replay_eligible": replay_eligible,
            }
            if replay_eligible:
                await self._inspect_and_emit(buffered_record)
            else:
                await self._emit_record(buffered_record)

    async def _inspect_and_emit(self, record: Mapping[str, Any]) -> None:
        # Diagnostics are emitted before their triggering data record.
        source = str(record["source"])
        state = self._state(source)
        if record.get("session_id") is not None:
            state.session_id = str(record["session_id"])
        if record.get("connection_id") is not None:
            state.connection_id = str(record["connection_id"])
        payload = record.get("payload")
        entity = _entity_key(
            source,
            payload,
            str(record.get("event_type") or "unknown"),
        )
        cursor = state.cursors.setdefault(entity, _CursorState())
        if record.get("kind") == "snapshot":
            identity = _stable_hash(
                {
                    "entity": entity,
                    "event_type": record.get("event_type"),
                    "payload_hash": record.get("payload_hash"),
                    "received_at": record.get("received_at"),
                    "monotonic_ns": record.get("monotonic_ns"),
                }
            )
        else:
            identity = _stable_hash(
                {
                    "entity": entity,
                    "event_type": record.get("event_type"),
                    "server_timestamp": record.get("server_timestamp"),
                    "sequence": record.get("sequence"),
                    "payload_hash": record.get("payload_hash"),
                }
            )
        if identity in state.recent_ids:
            await self._emit_anomaly(
                record,
                "duplicate",
                {"identity": identity, "entity": entity},
            )
        else:
            state.recent_ids[identity] = None
            if len(state.recent_ids) > 10_000:
                state.recent_ids.pop(next(iter(state.recent_ids)))

        event_at_ns = _timestamp_ns(record.get("server_timestamp"))
        if (
            event_at_ns is not None
            and cursor.last_event_at_ns is not None
            and event_at_ns < cursor.last_event_at_ns
        ):
            await self._emit_anomaly(
                record,
                "time_regression",
                {
                    "entity": entity,
                    "previous_server_timestamp": cursor.last_server_timestamp,
                    "current_server_timestamp": record.get("server_timestamp"),
                },
            )
        if event_at_ns is not None and (
            cursor.last_event_at_ns is None or event_at_ns >= cursor.last_event_at_ns
        ):
            cursor.last_event_at_ns = event_at_ns
            cursor.last_server_timestamp = record.get("server_timestamp")
            state.last_server_timestamp = record.get("server_timestamp")

        sequence = record.get("sequence")
        if (
            isinstance(sequence, int)
            and cursor.last_sequence is not None
            and sequence > cursor.last_sequence + 1
        ):
            await self._emit_anomaly(
                record,
                "sequence_gap",
                {
                    "entity": entity,
                    "after": cursor.last_sequence,
                    "before": sequence,
                    "missing_start": cursor.last_sequence + 1,
                    "missing_end": sequence - 1,
                    "missing_count": sequence - cursor.last_sequence - 1,
                },
            )
        if isinstance(sequence, int):
            cursor.last_sequence = max(cursor.last_sequence or sequence, sequence)
            state.last_sequence = sequence

        event_type = str(record["event_type"])
        schema_key = f"{entity}|{event_type}"
        schema = _schema_descriptor(record.get("payload"))
        previous_schema = state.schemas.get(schema_key)
        if previous_schema is not None and not _schemas_compatible(
            previous_schema, schema
        ):
            await self._emit_anomaly(
                record,
                "schema_drift",
                {
                    "entity": entity,
                    "event_type": event_type,
                    "previous_fingerprint": _stable_hash(previous_schema),
                    "current_fingerprint": _stable_hash(schema),
                },
            )
            state.schemas[schema_key] = schema
        elif previous_schema is None:
            state.schemas[schema_key] = schema
        else:
            state.schemas[schema_key] = _merge_schemas(previous_schema, schema)

        config = _config_projection(record.get("payload"))
        if config:
            config_hash = _stable_hash(config)
            previous_config = state.configs.get(schema_key)
            if previous_config is not None and previous_config != config_hash:
                await self._emit_anomaly(
                    record,
                    "config_change",
                    {
                        "event_type": event_type,
                        "entity": entity,
                        "previous_fingerprint": previous_config,
                        "current_fingerprint": config_hash,
                        "config": config,
                    },
                )
            elif event_type == "tick_size_change":
                await self._emit_anomaly(
                    record,
                    "config_change",
                    {
                        "event_type": event_type,
                        "entity": entity,
                        "previous_fingerprint": None,
                        "current_fingerprint": config_hash,
                        "config": config,
                    },
                )
            state.configs[schema_key] = config_hash

        await self._emit_record(record)
        state.records += 1
        state.last_server_hash = (
            str(record["server_hash"]) if record.get("server_hash") is not None else None
        )
        state.last_frame_hash = str(record.get("frame_hash") or "")
        if state.records % self.config.checkpoint_every_records == 0:
            await self._write_checkpoint(source, reason="record_interval")

    async def _snapshot_forever(self, kind: str, interval: float) -> None:
        while not self._stop.is_set():
            await self._capture_http_snapshot(kind)
            await self._sleep_or_stop(interval)

    async def _capture_http_snapshot(self, kind: str) -> None:
        source = f"{kind}_http"
        try:
            fetched = await self._fetch_snapshot(
                kind,
                asset_ids=(
                    self.config.clob_asset_ids if kind == "clob" else ()
                ),
            )
            payload, http_evidence = _snapshot_parts(fetched)
            payload = _safe_json_numbers(payload)
            record = {
                **self._record_base(
                    source=source,
                    kind="snapshot",
                    event_type=f"{kind}_snapshot",
                    session_id=self._session_id,
                    connection_id=None,
                ),
                "schema_version": f"{kind}-http.snapshot.v1",
                "server_timestamp": _first_nested(
                    payload if isinstance(payload, Mapping) else {},
                    ("timestamp", "ts"),
                ),
                "server_hash": None,
                "sequence": None,
                "payload_hash": _stable_hash(payload),
                "event_at": _event_at_text(
                    _first_nested(
                        payload if isinstance(payload, Mapping) else {},
                        ("timestamp", "ts"),
                    )
                ),
                "payload": payload,
                **http_evidence,
            }
            await self._inspect_and_emit(record)
        except asyncio.CancelledError:
            raise
        except RecorderWorkerError:
            raise
        except Exception as exc:
            await self._emit_lifecycle(
                source,
                "snapshot_error",
                session_id=self._session_id,
                connection_id=None,
                detail={
                    "snapshot_kind": kind,
                    **safe_error_details(
                        exc,
                        code="snapshot_request_failed",
                    ),
                },
            )

    async def _capture_resnapshot(
        self,
        *,
        session_id: str,
        connection_id: str,
        reason: str = "websocket_reconnect",
    ) -> dict[str, int]:
        try:
            fetched = await self._fetch_snapshot(
                "full_book",
                asset_ids=self.config.clob_asset_ids,
            )
            payload, http_evidence = _snapshot_parts(fetched)
            payload = _safe_json_numbers(payload)
            server_timestamp = _first_nested(
                payload if isinstance(payload, Mapping) else {},
                ("timestamp", "ts"),
            )
            await self._inspect_and_emit(
                {
                    **self._record_base(
                        source="clob_http",
                        kind="snapshot",
                        event_type="resnapshot",
                        session_id=session_id,
                        connection_id=connection_id,
                    ),
                    "schema_version": "clob-http.resnapshot.v1",
                    "server_timestamp": server_timestamp,
                    "event_at": _event_at_text(server_timestamp),
                    "server_hash": None,
                    "sequence": None,
                    "payload_hash": _stable_hash(payload),
                    "reason": reason,
                    "asset_ids": list(self.config.clob_asset_ids),
                    "payload": payload,
                    **http_evidence,
                }
            )
            return _snapshot_asset_watermarks_ns(
                payload,
                self.config.clob_asset_ids,
            )
        except asyncio.CancelledError:
            raise
        except RecorderWorkerError:
            raise
        except Exception as exc:
            await self._emit_lifecycle(
                "clob_http",
                "resnapshot_error",
                session_id=session_id,
                connection_id=connection_id,
                detail=safe_error_details(
                    exc,
                    code="resnapshot_request_failed",
                ),
            )
            # Without a fresh full book, queued deltas cannot reconstruct a
            # trustworthy state.  Force another clean connection attempt.
            raise

    async def _emit_anomaly(
        self,
        triggering_record: Mapping[str, Any],
        anomaly: str,
        detail: Mapping[str, Any],
    ) -> None:
        await self._emit_record(
            {
                **self._record_base(
                    source=str(triggering_record["source"]),
                    kind="diagnostic",
                    event_type=f"anomaly.{anomaly}",
                    session_id=triggering_record.get("session_id"),
                    connection_id=triggering_record.get("connection_id"),
                ),
                "schema_version": "edge-lab-recorder.anomaly.v1",
                "trigger_frame_hash": triggering_record.get("frame_hash"),
                "trigger_server_hash": triggering_record.get("server_hash"),
                "detail": dict(detail),
            }
        )

    async def _emit_lifecycle(
        self,
        source: str,
        event: str,
        *,
        session_id: Optional[str],
        connection_id: Optional[str],
        detail: Mapping[str, Any],
        received_at: Optional[str] = None,
        monotonic_ns: Optional[int] = None,
    ) -> None:
        await self._emit_record(
            {
                **self._record_base(
                    source=source,
                    kind="lifecycle",
                    event_type=event,
                    session_id=session_id,
                    connection_id=connection_id,
                    received_at=received_at,
                    monotonic_ns=monotonic_ns,
                ),
                "schema_version": "edge-lab-recorder.lifecycle.v1",
                "detail": dict(detail),
            }
        )

    def _record_base(
        self,
        *,
        source: str,
        kind: str,
        event_type: str,
        session_id: Optional[str],
        connection_id: Optional[str],
        received_at: Optional[str] = None,
        monotonic_ns: Optional[int] = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "edge-lab-recorder.record.v1",
            "source": source,
            "kind": kind,
            "event_type": event_type,
            "session_id": session_id,
            "connection_id": connection_id,
            "received_at": received_at or _utc_text(self.clock.utcnow()),
            "monotonic_ns": (
                monotonic_ns
                if monotonic_ns is not None
                else self.clock.monotonic_ns()
            ),
        }

    async def _emit_record(self, record: Mapping[str, Any]) -> None:
        await self._call_sink(self.sink.emit, record)

    async def _fetch_snapshot(
        self, kind: str, *, asset_ids: Sequence[str]
    ) -> Any:
        async def invoke() -> Any:
            method = self.snapshot_client.fetch_snapshot
            if inspect.iscoroutinefunction(method):
                return await method(kind, asset_ids=asset_ids)
            result = await asyncio.to_thread(method, kind, asset_ids=asset_ids)
            return await _maybe_await(result)

        return await asyncio.wait_for(
            invoke(),
            timeout=self.config.snapshot_timeout_seconds,
        )

    async def _call_sink(
        self,
        method: Any,
        *args: Any,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        timeout = (
            self.config.sink_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        async with self._sink_lock:
            try:
                if inspect.iscoroutinefunction(method):
                    return await asyncio.wait_for(
                        method(*args),
                        timeout=timeout,
                    )
                result = await asyncio.wait_for(
                    asyncio.to_thread(method, *args),
                    timeout=timeout,
                )
                if inspect.isawaitable(result):
                    return await asyncio.wait_for(
                        result,
                        timeout=timeout,
                    )
                return result
            except asyncio.TimeoutError as exc:
                raise RecorderWorkerError(
                    error_type="TimeoutError",
                    error_code="persistence_sink_timeout",
                ) from exc
            except asyncio.CancelledError as exc:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                raise RecorderWorkerError(
                    error_type="CancelledError",
                    error_code="persistence_sink_cancelled",
                ) from exc
            except RecorderWorkerError:
                raise
            except Exception as exc:
                details = safe_error_details(
                    exc,
                    code="persistence_sink_failed",
                )
                raise RecorderWorkerError(**details) from exc

    async def _write_checkpoint(self, source: str, *, reason: str) -> None:
        state = self._states.get(source)
        if state is None:
            return
        await self._call_sink(
            self.sink.checkpoint,
            source,
            {
                "schema_version": "edge-lab-recorder.checkpoint.v1",
                "updated_at": _utc_text(self.clock.utcnow()),
                "monotonic_ns": self.clock.monotonic_ns(),
                "reason": reason,
                "records": state.records,
                "last_server_timestamp": state.last_server_timestamp,
                "last_sequence": state.last_sequence,
                "last_server_hash": state.last_server_hash,
                "last_frame_hash": state.last_frame_hash,
                "session_id": state.session_id,
                "connection_id": state.connection_id,
                "cursors": {
                    entity: {
                        "last_server_timestamp": cursor.last_server_timestamp,
                        "last_sequence": cursor.last_sequence,
                    }
                    for entity, cursor in sorted(state.cursors.items())
                },
            },
            timeout_seconds=self.config.checkpoint_timeout_seconds,
        )

    def _state(self, source: str) -> _SourceState:
        return self._states.setdefault(source, _SourceState())

    async def _sleep_or_stop(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        sleeper = self._create_child_task(self.clock.sleep(delay))
        stopper = self._create_child_task(self._stop.wait())
        try:
            done, pending = await asyncio.wait(
                {sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                task.add_done_callback(_consume_task_result)
            for task in done:
                task.result()
        finally:
            for task in (sleeper, stopper):
                if not task.done():
                    task.cancel()
                    task.add_done_callback(_consume_task_result)

    async def _close_connection(self, connection: WebSocketConnection) -> None:
        close_task = self._create_child_task(connection.close())
        done, pending = await asyncio.wait(
            {close_task},
            timeout=self.config.close_timeout_seconds,
        )
        if done:
            results = await asyncio.gather(*done, return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                self._abort_connection_transport(connection)
        for orphan in pending:
            orphan.cancel()
            self._abort_connection_transport(connection)
            orphan.add_done_callback(_consume_task_result)

    @staticmethod
    def _abort_connection_transport(
        connection: WebSocketConnection,
    ) -> None:
        transport = getattr(connection, "transport", None)
        abort = getattr(transport, "abort", None)
        if not callable(abort):
            return
        try:
            abort()
        except Exception:
            # Shutdown is already bounded and fail-closed at the worker
            # boundary.  A transport-specific abort error cannot be made
            # actionable without exposing implementation details.
            return


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        return


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _event_type(source: str, payload: Mapping[str, Any]) -> str:
    if source == "clob_market_ws":
        event = payload.get("event_type") or payload.get("type") or "unknown"
        return str(event)
    topic = str(payload.get("topic") or "unknown")
    update_type = str(payload.get("type") or payload.get("event_type") or "update")
    return f"{topic}.{update_type}"


def _schema_version(source: str, event_type: str) -> str:
    prefix = "clob-market-ws" if source == "clob_market_ws" else "rtds"
    return f"{prefix}.{event_type}.v1"


def _first_nested(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    nested = payload.get("payload")
    if isinstance(nested, Mapping):
        for key in keys:
            value = nested.get(key)
            if value is not None:
                return value
    return None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_at_text(value: Any) -> Optional[str]:
    nanoseconds = _timestamp_ns(value)
    if nanoseconds is None:
        if isinstance(value, str) and ("T" in value or value.endswith("Z")):
            try:
                text = value.replace("Z", "+00:00")
                return _utc_text(datetime.fromisoformat(text))
            except ValueError:
                return None
        return None
    try:
        parsed = datetime.fromtimestamp(
            nanoseconds / 1_000_000_000, timezone.utc
        )
    except (OSError, OverflowError, ValueError):
        return None
    return _utc_text(parsed)


def _timestamp_ns(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and ("T" in value or value.endswith("Z")):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1_000_000_000)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        decimal_number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal_number.is_finite():
        return None
    absolute = abs(decimal_number)
    if absolute < 100_000_000_000:  # seconds
        return int(decimal_number * Decimal(1_000_000_000))
    if absolute < 100_000_000_000_000:  # milliseconds
        return int(decimal_number * Decimal(1_000_000))
    if absolute < 100_000_000_000_000_000:  # microseconds
        return int(decimal_number * Decimal(1_000))
    return int(decimal_number)


def _entity_key(source: str, payload: Any, event_type: str) -> str:
    if not isinstance(payload, Mapping):
        return f"{source}:stream"
    nested = payload.get("payload")
    nested_mapping = nested if isinstance(nested, Mapping) else {}
    identifier: Any = None
    for key in (
        "asset_id",
        "token_id",
        "market",
        "condition_id",
        "conditionId",
        "symbol",
    ):
        identifier = payload.get(key)
        if identifier not in (None, ""):
            break
        identifier = nested_mapping.get(key)
        if identifier not in (None, ""):
            break
    topic = payload.get("topic")
    if source == "rtds_ws":
        return f"{source}:{topic or event_type}:{identifier or 'stream'}"
    return f"{source}:{identifier or 'stream'}"


def _schema_descriptor(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            "kind": "object",
            "fields": {
                str(key): _schema_descriptor(nested)
                for key, nested in sorted(
                    value.items(), key=lambda item: str(item[0])
                )
            },
        }
    if isinstance(value, list):
        unique: dict[str, Any] = {}
        for item in value:
            item_schema = _schema_descriptor(item)
            unique[_stable_hash(item_schema)] = item_schema
        return {
            "kind": "array",
            "items": [unique[key] for key in sorted(unique)],
        }
    if value is None:
        return {"kind": "null"}
    return {"kind": type(value).__name__}


def _schemas_compatible(previous: Any, current: Any) -> bool:
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return previous == current
    if previous.get("kind") != current.get("kind"):
        return False
    kind = previous.get("kind")
    if kind == "object":
        previous_fields = previous.get("fields", {})
        current_fields = current.get("fields", {})
        if not isinstance(previous_fields, Mapping) or not isinstance(
            current_fields, Mapping
        ):
            return False
        return all(
            _schemas_compatible(previous_fields[key], current_fields[key])
            for key in set(previous_fields) & set(current_fields)
        )
    if kind == "array":
        previous_items = previous.get("items", [])
        current_items = current.get("items", [])
        if not previous_items or not current_items:
            return True
        return all(
            any(_schemas_compatible(old, new) for old in previous_items)
            for new in current_items
        )
    return True


def _merge_schemas(previous: Any, current: Any) -> Any:
    if not _schemas_compatible(previous, current):
        return current
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return previous
    kind = previous.get("kind")
    if kind == "object":
        previous_fields = dict(previous.get("fields", {}))
        current_fields = dict(current.get("fields", {}))
        merged: dict[str, Any] = {}
        for key in set(previous_fields) | set(current_fields):
            if key in previous_fields and key in current_fields:
                merged[key] = _merge_schemas(
                    previous_fields[key], current_fields[key]
                )
            else:
                merged[key] = previous_fields.get(key, current_fields.get(key))
        return {"kind": "object", "fields": merged}
    if kind == "array":
        unique: dict[str, Any] = {}
        for item in [
            *previous.get("items", []),
            *current.get("items", []),
        ]:
            unique[_stable_hash(item)] = item
        return {
            "kind": "array",
            "items": [unique[key] for key in sorted(unique)],
        }
    return previous


def _schema_fingerprint(payload: Any) -> str:
    return _stable_hash(_schema_descriptor(payload))


def _config_projection(payload: Any) -> dict[str, Any]:
    config_keys = {
        "tick_size",
        "tickSize",
        "min_order_size",
        "minOrderSize",
        "minimum_order_size",
        "fee_schedule",
        "feeSchedule",
        "feeRateBps",
        "fd",
        "rewards",
        "rewards_config",
        "rewardConfig",
        "r",
        "max_spread",
        "maxSpread",
        "ma",
        "min_size",
        "minSize",
        "mi",
        "order_age",
        "orderAge",
        "moas",
        "mos",
        "mts",
        "new_tick_size",
        "old_tick_size",
        "minimum_tick_size",
        "neg_risk",
        "negRisk",
        "enableNegRisk",
        "negRiskAugmented",
        "rules",
        "resolutionSource",
        "resolution_source",
        "endDate",
        "end_date_iso",
    }

    def visit(value: Any) -> Any:
        if isinstance(value, Mapping):
            selected: dict[str, Any] = {}
            for key, nested in value.items():
                if str(key) in config_keys:
                    selected[str(key)] = nested
                else:
                    child = visit(nested)
                    if child not in ({}, [], None):
                        selected[str(key)] = child
            return selected
        if isinstance(value, list):
            children = [visit(item) for item in value]
            return [item for item in children if item not in ({}, [], None)]
        return None

    result = visit(payload)
    if isinstance(result, dict):
        return result
    if isinstance(result, list) and result:
        return {"items": result}
    return {}


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_parts(value: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(value, SnapshotEnvelope):
        return value, {}
    raw_hash = hashlib.sha256(value.raw_body).hexdigest()
    return value.payload, {
        "raw_body_base64": base64.b64encode(value.raw_body).decode("ascii"),
        "raw_body_sha256": raw_hash,
        "raw_body_bytes": len(value.raw_body),
        "http": {
            "status_code": value.status_code,
            "request_url": value.request_url,
            "response_headers": dict(value.response_headers),
            "requested_at": value.requested_at,
            "received_at": value.received_at,
        },
    }


def _event_asset_ids(value: Any) -> tuple[str, ...]:
    identifiers: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if str(key) in {"asset_id", "token_id"}:
                    if isinstance(nested, str) and nested:
                        identifiers.add(nested)
                elif isinstance(nested, (Mapping, list, tuple)):
                    visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(sorted(identifiers))


def _snapshot_asset_watermarks_ns(
    value: Any,
    expected_asset_ids: Sequence[str],
) -> dict[str, int]:
    expected = tuple(expected_asset_ids)
    expected_set = set(expected)
    if (
        not expected
        or len(expected_set) != len(expected)
        or not isinstance(value, Mapping)
    ):
        raise ValueError("full-book snapshot asset scope is invalid")

    watermarks: dict[str, int] = {}
    if value.get("schema_version") == "edge-lab-public-snapshot.v1":
        requested = value.get("requested_asset_ids")
        responses = value.get("responses")
        if (
            value.get("snapshot_kind") != "full_book"
            or not isinstance(requested, list)
            or tuple(requested) != expected
            or not isinstance(responses, list)
            or len(responses) != len(expected)
        ):
            raise ValueError(
                "full-book snapshot did not return the exact requested scope"
            )
        for response in responses:
            if (
                not isinstance(response, Mapping)
                or response.get("resource") != "clob_book"
                or response.get("response_status") == "error"
            ):
                raise ValueError(
                    "full-book snapshot contains an unsuccessful response"
                )
            asset_id = response.get("request_key")
            raw_json = response.get("raw_json")
            if (
                not isinstance(asset_id, str)
                or asset_id not in expected_set
                or asset_id in watermarks
                or not isinstance(raw_json, Mapping)
                or raw_json.get("asset_id") != asset_id
            ):
                raise ValueError(
                    "full-book response identity does not match its asset"
                )
            timestamp = _first_nested(
                raw_json,
                ("timestamp", "ts", "event_timestamp", "eventTimestamp"),
            )
            watermark = _timestamp_ns(timestamp)
            if watermark is None:
                raise ValueError(
                    "full-book response lacks an asset timestamp"
                )
            watermarks[asset_id] = watermark
    else:
        books = value.get("books")
        if not isinstance(books, list) or len(books) != len(expected):
            raise ValueError(
                "full-book snapshot did not return one book per asset"
            )
        shared_timestamp = _first_nested(
            value,
            ("timestamp", "ts", "event_timestamp", "eventTimestamp"),
        )
        for book in books:
            if not isinstance(book, Mapping):
                raise ValueError("full-book snapshot contains a non-object book")
            asset_id = book.get("asset_id")
            if (
                not isinstance(asset_id, str)
                or asset_id not in expected_set
                or asset_id in watermarks
            ):
                raise ValueError(
                    "full-book response identity does not match its asset"
                )
            timestamp = _first_nested(
                book,
                ("timestamp", "ts", "event_timestamp", "eventTimestamp"),
            )
            watermark = _timestamp_ns(
                shared_timestamp if timestamp is None else timestamp
            )
            if watermark is None:
                raise ValueError(
                    "full-book response lacks an asset timestamp"
                )
            watermarks[asset_id] = watermark

    if set(watermarks) != expected_set:
        raise ValueError("full-book snapshot asset coverage is incomplete")
    return dict(sorted(watermarks.items()))


def _safe_json_numbers(value: Any) -> Any:
    """Prevent a sync HTTP decoder from leaking binary floats into raw storage."""
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Mapping):
        return {
            str(key): _safe_json_numbers(nested)
            for key, nested in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_json_numbers(item) for item in value]
    return value
