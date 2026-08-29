"""Process-bounded regression tests for recorder cancellation failures."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_cancelled_persistence_call_fails_without_spinning() -> None:
    """A cancelled sink call must not trap recorder shutdown in a CPU loop."""

    program = textwrap.dedent(
        """
        import asyncio

        from src.edge_lab.recorder import (
            PublicRecorder,
            RecorderConfig,
            RecorderWorkerError,
        )


        class CancelledBookSink:
            async def emit(self, record):
                if record.get("event_type") == "book":
                    raise asyncio.CancelledError()

            async def checkpoint(self, source, checkpoint):
                return None


        class OneFrameWebSocket:
            def __init__(self):
                self.delivered = False

            async def send(self, message):
                return None

            async def recv(self):
                if not self.delivered:
                    self.delivered = True
                    return (
                        '{"event_type":"book","asset_id":"yes-token",'
                        '"timestamp":1000,"sequence":1,"bids":[],"asks":[]}'
                    )
                await asyncio.Event().wait()

            async def close(self):
                return None


        class OneConnectionFactory:
            def __init__(self):
                self.connection = OneFrameWebSocket()

            async def connect(self, url):
                return self.connection


        class IdleSnapshotClient:
            async def fetch_snapshot(self, kind, *, asset_ids=()):
                return {"kind": kind, "asset_ids": list(asset_ids)}


        async def exercise():
            recorder = PublicRecorder(
                config=RecorderConfig(
                    clob_asset_ids=("yes-token",),
                    rtds_enabled=False,
                    rtds_subscriptions=(),
                    snapshot_intervals={},
                    shutdown_timeout_seconds=0.05,
                ),
                sink=CancelledBookSink(),
                websocket_factory=OneConnectionFactory(),
                snapshot_client=IdleSnapshotClient(),
            )
            try:
                await recorder.run(run_for_seconds=0.05)
            except RecorderWorkerError as exc:
                assert exc.error_code == "persistence_sink_cancelled"
                return
            raise AssertionError("cancelled persistence did not fail closed")


        asyncio.run(exercise())
        """
    )
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_cancelled_frame_owner_fails_without_spinning() -> None:
    """A cancelled frame owner must not spin in cancellation draining."""

    program = textwrap.dedent(
        """
        import asyncio

        from src.edge_lab.recorder import (
            PublicRecorder,
            RecorderConfig,
            RecorderWorkerError,
        )


        class CancelsFrameOwnerSink:
            async def emit(self, record):
                if record.get("event_type") != "book":
                    return None
                owner = asyncio.current_task()
                assert owner is not None
                owner.cancel()
                await asyncio.sleep(0)

            async def checkpoint(self, source, checkpoint):
                return None


        class OneFrameWebSocket:
            def __init__(self):
                self.delivered = False

            async def send(self, message):
                return None

            async def recv(self):
                if not self.delivered:
                    self.delivered = True
                    return (
                        '{"event_type":"book","asset_id":"yes-token",'
                        '"timestamp":1000,"sequence":1,"bids":[],"asks":[]}'
                    )
                await asyncio.Event().wait()

            async def close(self):
                return None


        class OneConnectionFactory:
            def __init__(self):
                self.connection = OneFrameWebSocket()

            async def connect(self, url):
                return self.connection


        class IdleSnapshotClient:
            async def fetch_snapshot(self, kind, *, asset_ids=()):
                return {"kind": kind, "asset_ids": list(asset_ids)}


        async def exercise():
            recorder = PublicRecorder(
                config=RecorderConfig(
                    clob_asset_ids=("yes-token",),
                    rtds_enabled=False,
                    rtds_subscriptions=(),
                    snapshot_intervals={},
                    shutdown_timeout_seconds=0.05,
                ),
                sink=CancelsFrameOwnerSink(),
                websocket_factory=OneConnectionFactory(),
                snapshot_client=IdleSnapshotClient(),
            )
            try:
                await recorder.run(run_for_seconds=0.05)
            except RecorderWorkerError as exc:
                assert exc.error_code == "persistence_sink_cancelled"
                return
            raise AssertionError("cancelled frame owner did not fail closed")


        asyncio.run(exercise())
        """
    )
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_stop_consumes_receive_overflow_boundary_once() -> None:
    """Stopping after reader overflow must not replay its terminal frame."""

    program = textwrap.dedent(
        """
        import asyncio

        from src.edge_lab.recorder import PublicRecorder, RecorderConfig


        def frame(sequence):
            return (
                '{"event_type":"book","asset_id":"yes-token",'
                f'"timestamp":{sequence}000,"sequence":{sequence},'
                '"bids":[],"asks":[]}'
            )


        class BlockingFirstBookSink:
            def __init__(self):
                self.records = []
                self.first_book_started = asyncio.Event()
                self.release_first_book = asyncio.Event()

            async def emit(self, record):
                if (
                    record.get("event_type") == "book"
                    and not self.first_book_started.is_set()
                ):
                    self.first_book_started.set()
                    await self.release_first_book.wait()
                self.records.append(dict(record))

            async def checkpoint(self, source, checkpoint):
                return None


        class OverflowWebSocket:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.recv_count = 0
                self.boundary_delivered = asyncio.Event()

            async def send(self, message):
                return None

            async def recv(self):
                self.recv_count += 1
                value = await self.incoming.get()
                if self.recv_count == 3:
                    self.boundary_delivered.set()
                return value

            async def close(self):
                return None


        class OneConnectionFactory:
            def __init__(self, connection):
                self.connection = connection

            async def connect(self, url):
                return self.connection


        class IdleSnapshotClient:
            async def fetch_snapshot(self, kind, *, asset_ids=()):
                return {"kind": kind, "asset_ids": list(asset_ids)}


        async def exercise():
            connection = OverflowWebSocket()
            sink = BlockingFirstBookSink()
            connection.incoming.put_nowait(frame(1))
            recorder = PublicRecorder(
                config=RecorderConfig(
                    clob_asset_ids=("yes-token",),
                    rtds_enabled=False,
                    rtds_subscriptions=(),
                    snapshot_intervals={},
                    receive_buffer_max_messages=1,
                    shutdown_timeout_seconds=0.2,
                ),
                sink=sink,
                websocket_factory=OneConnectionFactory(connection),
                snapshot_client=IdleSnapshotClient(),
            )
            await recorder.start()
            await asyncio.wait_for(sink.first_book_started.wait(), timeout=0.2)
            connection.incoming.put_nowait(frame(2))
            connection.incoming.put_nowait(frame(3))
            await asyncio.wait_for(connection.boundary_delivered.wait(), timeout=0.2)
            await asyncio.sleep(0)
            stop_task = asyncio.create_task(recorder.stop())
            await asyncio.sleep(0)
            sink.release_first_book.set()
            await asyncio.wait_for(stop_task, timeout=0.5)

            books = [
                record
                for record in sink.records
                if record.get("event_type") == "book"
            ]
            assert [record["sequence"] for record in books] == [1, 2, 3]
            assert not [
                record
                for record in sink.records
                if record.get("event_type") == "anomaly.duplicate"
            ]


        asyncio.run(exercise())
        """
    )
    repo_root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
