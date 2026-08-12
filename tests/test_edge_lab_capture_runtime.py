"""Behavioral tests for the public-recorder runtime adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import pytest

from src.edge_lab.capture_runtime import (
    CaptureStoreRecorderSink,
    PublicSourcesSnapshotClient,
    SinkClosedError,
)
from src.edge_lab.data_store import CaptureStore, CriticalFloatError
from src.edge_lab.recorder import SnapshotEnvelope
from src.edge_lab.sources import (
    FetchMetadata,
    Fetched,
    GammaPage,
    PublicSourceError,
    RawResponse,
    RewardPage,
)


def _recorder_record(
    *,
    source: str = "clob_market_ws",
    session_id: str = "session-a",
    sequence: int | None = 1,
) -> dict[str, object]:
    return {
        "schema_version": "clob-market-ws.book.v1",
        "source": source,
        "kind": "data",
        "event_type": "book",
        "session_id": session_id,
        "connection_id": "connection-a",
        "received_at": "2026-07-24T08:00:00.100000Z",
        "event_at": "2026-07-24T08:00:00.000000Z",
        "sequence": sequence,
        "payload": {
            "asset_id": "yes-token",
            "bids": [{"price": "0.41", "size": "12.50"}],
            "asks": [{"price": "0.42", "size": "7.00"}],
        },
    }


def _fetched(
    body: bytes,
    value: Any,
    *,
    source: str,
    url: str,
    requested_at: float = 1_721_808_000.125,
    received_at: float = 1_721_808_000.375,
    request_params: Mapping[str, Any] | None = None,
    method: str = "GET",
    response_headers: Mapping[str, str] | None = None,
) -> Fetched[Any]:
    return Fetched(
        raw=RawResponse(
            body=body,
            text=body.decode("utf-8"),
            metadata=FetchMetadata(
                source=source,
                method=method,
                url=url,
                request_params=MappingProxyType(
                    dict(
                        {"limit": 100}
                        if request_params is None
                        else request_params
                    )
                ),
                status_code=200,
                requested_at=requested_at,
                received_at=received_at,
                attempt=1,
                response_headers=MappingProxyType(
                    dict(
                        response_headers
                        or {"Content-Type": "application/json"}
                    )
                ),
            ),
        ),
        value=value,
    )


class FakePublicSources:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def gamma_events(
        self, *, limit: int, cursor: str | None = None
    ) -> Fetched[GammaPage]:
        self.calls.append(("gamma_events", limit, cursor))
        body = (
            b'{"events":[{"id":"event-1","title":"Weather","markets":[],'
            b'"probability":0.4200}],"next_cursor":null}'
        )
        return _fetched(
            body,
            GammaPage(
                items=(),
                next_cursor=None,
                response_limit=100,
            ),
            source="gamma",
            url="https://gamma-api.polymarket.com/events/keyset",
            request_params={"limit": limit},
        )

    def gamma_markets(
        self, *, limit: int, cursor: str | None = None
    ) -> Fetched[GammaPage]:
        self.calls.append(("gamma_markets", limit, cursor))
        body = b'{"markets":[],"next_cursor":null}'
        return _fetched(
            body,
            GammaPage(
                items=(),
                next_cursor=None,
                response_limit=100,
            ),
            source="gamma",
            url="https://gamma-api.polymarket.com/markets/keyset",
            request_params={"limit": limit},
        )

    def server_time(self) -> Fetched[Decimal]:
        self.calls.append(("server_time",))
        return _fetched(
            b"1721808000",
            Decimal("1721808000"),
            source="clob_time",
            url="https://clob.polymarket.com/time",
            request_params={},
        )

    def clob_market(self, condition_id: str) -> Fetched[object]:
        self.calls.append(("clob_market", condition_id))
        body = (
            b'{"c":"condition-1","t":[],"mos":5,"mts":0.01,'
            b'"ao":true,"r":{"mi":0,"ma":0,"e":false,"moas":0},'
            b'"fd":{"r":0,"e":0,"to":true}}'
        )
        return _fetched(
            body,
            object(),
            source="clob",
            url=(
                "https://clob.polymarket.com/clob-markets/"
                f"{condition_id}"
            ),
            request_params={},
        )

    def book(self, token_id: str) -> Fetched[object]:
        self.calls.append(("book", token_id))
        body = (
            f'{{"asset_id":"{token_id}","bids":[{{"price":0.41,'
            '"size":12.50}],"asks":[]}'
        ).encode()
        return _fetched(
            body,
            object(),
            source="clob_book",
            url="https://clob.polymarket.com/book",
            request_params={"token_id": token_id},
        )

    def current_reward_pages(
        self,
        *,
        limit: int,
        max_pages: int,
    ) -> Any:
        self.calls.append(("current_reward_pages", limit, max_pages))
        for page_number in range(1, max_pages + 1):
            body = (
                f'{{"data":[],"limit":{limit},"count":0,'
                f'"next_cursor":{json.dumps(None if page_number == max_pages else "next")}}}'
            ).encode()
            yield _fetched(
                body,
                RewardPage(
                    items=(),
                    next_cursor=(
                        None if page_number == max_pages else "next"
                    ),
                    response_limit=limit,
                    count=0,
                ),
                source="clob_rewards",
                url=(
                    "https://clob.polymarket.com/"
                    "rewards/markets/current"
                ),
                request_params={"limit": limit},
            )

    def resolution_rules(self, market_id: str) -> Fetched[object]:
        self.calls.append(("resolution_rules", market_id))
        body = (
            f'{{"id":"{market_id}","question":"Q",'
            '"conditionId":"condition-1","description":"Rules",'
            '"resolutionSource":"https://example.test/source",'
            '"endDate":"2026-07-26T23:59:00Z","closed":false}'
        ).encode()
        return _fetched(
            body,
            object(),
            source="gamma",
            url=(
                "https://gamma-api.polymarket.com/markets/"
                f"{market_id}"
            ),
            request_params={},
        )


@pytest.mark.asyncio
async def test_sink_finalizes_recorder_record_into_immutable_raw_batch(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    sink = CaptureStoreRecorderSink(store)

    result = await sink.emit(_recorder_record())
    manifests = await sink.close()

    assert result.written is True
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest["source"] == "clob_market_ws"
    assert manifest["record_count"] == 1
    raw_path = tmp_path / str(manifest["raw_path"])
    row = json.loads(raw_path.read_text(encoding="utf-8"))
    assert row["source"] == "clob_market_ws"
    assert row["sequence"] == 1
    assert row["payload"]["kind"] == "data"
    assert row["payload"]["payload"]["bids"][0] == {
        "price": "0.41",
        "size": "12.50",
    }
    assert not list((tmp_path / "raw").rglob("*.partial"))


@pytest.mark.asyncio
async def test_sink_serializes_concurrent_emits_and_checkpoints_finalized_batches(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    sink = CaptureStoreRecorderSink(store, max_records_per_batch=3)
    records = []
    for sequence in range(1, 11):
        record = _recorder_record(sequence=sequence)
        record["received_at"] = (
            f"2026-07-24T08:00:{sequence:02d}.100000Z"
        )
        records.append(record)

    results = await asyncio.gather(*(sink.emit(record) for record in records))
    rotation_checkpoint = store.read_checkpoint("clob_market_ws")
    await sink.checkpoint(
        "clob_market_ws",
        {
            "schema_version": "edge-lab-recorder.checkpoint.v1",
            "updated_at": "2026-07-24T08:01:00Z",
            "reason": "record_interval",
            "records": 10,
            "last_sequence": 10,
            "session_id": "session-a",
        },
    )
    await sink.close()

    assert all(result.written for result in results)
    assert rotation_checkpoint is not None
    assert rotation_checkpoint["checkpoint"]["capture_runtime"][
        "reason"
    ] == "batch_rotation"
    raw_paths = sorted((tmp_path / "raw/clob_market_ws").glob("*.jsonl"))
    assert len(raw_paths) == 4
    rows = [
        json.loads(line)
        for path in raw_paths
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert sorted(row["sequence"] for row in rows) == list(range(1, 11))
    persisted = store.read_checkpoint("clob_market_ws")
    assert persisted is not None
    assert persisted["checkpoint"]["recorder_checkpoint"][
        "last_sequence"
    ] == 10
    assert persisted["checkpoint"]["capture_runtime"]["reason"] == "close"
    assert persisted["checkpoint"]["capture_runtime"][
        "last_finalized_batch"
    ]["record_count"] == 1


@pytest.mark.asyncio
async def test_restart_loads_recorder_checkpoint_and_preserves_orphan_partial(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    first = CaptureStoreRecorderSink(store)
    await first.emit(_recorder_record(sequence=17))
    await first.checkpoint(
        "clob_market_ws",
        {
            "schema_version": "edge-lab-recorder.checkpoint.v1",
            "updated_at": "2026-07-24T08:02:00Z",
            "last_sequence": 17,
            "session_id": "session-a",
        },
    )
    await first.close()
    orphan = tmp_path / "raw/clob_market_ws/crashed.jsonl.partial"
    orphan.write_bytes(b'{"incomplete":true}\n')

    restarted = CaptureStoreRecorderSink(store)

    assert restarted.load_recorder_checkpoint("clob_market_ws") == {
        "schema_version": "edge-lab-recorder.checkpoint.v1",
        "updated_at": "2026-07-24T08:02:00Z",
        "last_sequence": 17,
        "session_id": "session-a",
    }
    assert restarted.startup_integrity_report["orphan_partials"] == [
        "raw/clob_market_ws/crashed.jsonl.partial"
    ]
    restarted_record = _recorder_record(
        session_id="session-b", sequence=18
    )
    restarted_record["received_at"] = "2026-07-24T08:03:00Z"
    await restarted.emit(restarted_record)
    await restarted.close()

    assert orphan.read_bytes() == b'{"incomplete":true}\n'
    raw_paths = sorted((tmp_path / "raw/clob_market_ws").glob("*.jsonl"))
    assert len(raw_paths) == 2
    checkpoint = store.read_checkpoint("clob_market_ws")
    assert checkpoint is not None
    assert checkpoint["checkpoint"]["capture_runtime"][
        "startup_integrity"
    ]["orphan_partials"] == [
        "raw/clob_market_ws/crashed.jsonl.partial"
    ]


@pytest.mark.asyncio
async def test_sink_preserves_diagnostics_and_rejects_critical_binary_float(
    tmp_path: Path,
) -> None:
    sink = CaptureStoreRecorderSink(CaptureStore(tmp_path))
    invalid = _recorder_record()
    invalid["payload"] = {"price": 0.42, "size": "1.00"}

    with pytest.raises(CriticalFloatError, match="binary float"):
        await sink.emit(invalid)

    diagnostic = {
        "schema_version": "edge-lab-recorder.anomaly.v1",
        "source": "clob_market_ws",
        "kind": "diagnostic",
        "event_type": "anomaly.sequence_gap",
        "session_id": "session-a",
        "connection_id": "connection-a",
        "received_at": "2026-07-24T08:04:00Z",
        "detail": {
            "missing_start": 8,
            "missing_end": 9,
            "reference_price": Decimal("0.4200"),
        },
    }
    await sink.emit(diagnostic)
    await sink.close()

    raw_path = next((tmp_path / "raw/clob_market_ws").glob("*.jsonl"))
    rows = [
        json.loads(line)
        for line in raw_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["event_at"] is None
    assert rows[0]["sequence"] is None
    assert rows[0]["payload"]["kind"] == "diagnostic"
    assert rows[0]["payload"]["detail"]["reference_price"] == "0.4200"
    with pytest.raises(SinkClosedError, match="closed"):
        await sink.emit(diagnostic)


@pytest.mark.asyncio
async def test_invalid_record_does_not_create_an_empty_raw_batch(
    tmp_path: Path,
) -> None:
    sink = CaptureStoreRecorderSink(CaptureStore(tmp_path))
    invalid = _recorder_record()
    invalid["payload"] = {"price": 0.42}

    with pytest.raises(CriticalFloatError):
        await sink.emit(invalid)
    manifests = await sink.close()

    assert manifests == ()
    assert not list((tmp_path / "raw").rglob("*.jsonl"))
    assert not list((tmp_path / "raw").rglob("*.partial"))


def test_gamma_snapshot_retains_exact_public_response_provenance() -> None:
    sources = FakePublicSources()
    client = PublicSourcesSnapshotClient(
        sources,
        gamma_page_limit=100,
        gamma_max_pages=1,
    )

    snapshot = client.fetch_snapshot("gamma")

    assert sources.calls == [
        ("gamma_events", 100, None),
        ("gamma_markets", 100, None),
    ]
    assert snapshot["schema_version"] == "edge-lab-public-snapshot.v1"
    assert snapshot["snapshot_kind"] == "gamma"
    assert len(snapshot["responses"]) == 2
    events = snapshot["responses"][0]
    assert events["resource"] == "gamma_events"
    assert events["page_number"] == 1
    assert events["raw_json"]["events"][0]["probability"] == Decimal(
        "0.4200"
    )
    body = (
        '{"events":[{"id":"event-1","title":"Weather","markets":[],'
        '"probability":0.4200}],"next_cursor":null}'
    )
    assert events["provenance"] == {
        "source": "gamma",
        "method": "GET",
        "url": "https://gamma-api.polymarket.com/events/keyset",
        "request_params": {"limit": 100},
        "status_code": 200,
        "requested_at_epoch_seconds": "1721808000.125",
        "received_at_epoch_seconds": "1721808000.375",
        "attempt": 1,
        "response_headers": {"Content-Type": "application/json"},
        "body_utf8": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "body_bytes": len(body.encode()),
    }


def test_clob_and_reconnect_snapshots_use_only_public_get_methods() -> None:
    sources = FakePublicSources()
    client = PublicSourcesSnapshotClient(
        sources,
        condition_ids=("condition-1",),
    )

    clob = client.fetch_snapshot(
        "clob", asset_ids=("yes-token", "no-token")
    )
    full_book = client.fetch_snapshot(
        "full_book", asset_ids=("yes-token", "no-token")
    )

    assert sources.calls == [
        ("server_time",),
        ("clob_market", "condition-1"),
        ("book", "yes-token"),
        ("book", "no-token"),
        ("book", "yes-token"),
        ("book", "no-token"),
    ]
    assert [item["resource"] for item in clob["responses"]] == [
        "clob_server_time",
        "clob_market",
        "clob_book",
        "clob_book",
    ]
    assert [item.get("request_key") for item in clob["responses"]] == [
        None,
        "condition-1",
        "yes-token",
        "no-token",
    ]
    assert clob["responses"][2]["raw_json"]["bids"][0]["price"] == Decimal(
        "0.41"
    )
    assert clob["responses"][2]["raw_json"]["bids"][0]["size"] == Decimal(
        "12.50"
    )
    assert [item["resource"] for item in full_book["responses"]] == [
        "clob_book",
        "clob_book",
    ]


def test_clob_snapshot_captures_public_server_clock_probe() -> None:
    sources = FakePublicSources()
    snapshot = PublicSourcesSnapshotClient(
        sources,
        condition_ids=("condition-1",),
    ).fetch_snapshot("clob", asset_ids=("yes-token",))

    assert sources.calls[0] == ("server_time",)
    clock_probe = snapshot["responses"][0]
    assert clock_probe["resource"] == "clob_server_time"
    assert clock_probe["raw_json"] == 1_721_808_000
    assert clock_probe["provenance"]["source"] == "clob_time"
    assert clock_probe["provenance"]["url"] == (
        "https://clob.polymarket.com/time"
    )
    assert clock_probe["provenance"]["request_params"] == {}
    assert clock_probe["provenance"]["requested_at_epoch_seconds"] == (
        "1721808000.125"
    )
    assert clock_probe["provenance"]["received_at_epoch_seconds"] == (
        "1721808000.375"
    )
    assert clock_probe["clock_probe"] == {
        "server_time_unit": "unix_seconds",
        "server_precision_ms": 1000,
        "ordering_role": "coarse_drift_diagnostic_only",
        "cross_source_reordering_allowed": False,
    }


def test_clob_snapshot_preserves_successes_and_sanitizes_resource_errors() -> None:
    secret = "do-not-persist"

    class PartiallyFailingSources(FakePublicSources):
        def clob_market(self, condition_id: str) -> Fetched[object]:
            if condition_id != "condition-bad":
                return super().clob_market(condition_id)
            self.calls.append(("clob_market", condition_id))
            body = b'{"error":"upstream unavailable"}'
            raw = RawResponse(
                body=body,
                text=body.decode("utf-8"),
                metadata=FetchMetadata(
                    source="clob",
                    method="GET",
                    url=(
                        "https://clob.polymarket.com/clob-markets/"
                        f"{condition_id}?api_key={secret}"
                    ),
                    request_params=MappingProxyType(
                        {"api_key": secret, "condition_id": condition_id}
                    ),
                    status_code=503,
                    requested_at=1_721_808_001.125,
                    received_at=1_721_808_001.375,
                    attempt=2,
                    response_headers=MappingProxyType(
                        {
                            "Content-Type": "application/json",
                            "Set-Cookie": f"session={secret}",
                        }
                    ),
                ),
            )
            raise PublicSourceError(
                f"GET {raw.metadata.url} failed with {secret}",
                raw=raw,
                code="http_status_failure",
                error_type="HTTPStatus",
            )

    sources = PartiallyFailingSources()
    snapshot = PublicSourcesSnapshotClient(
        sources,
        condition_ids=("condition-good", "condition-bad"),
    ).fetch_snapshot("clob", asset_ids=("yes-token",))

    assert not isinstance(snapshot, SnapshotEnvelope)
    assert sources.calls == [
        ("server_time",),
        ("clob_market", "condition-good"),
        ("clob_market", "condition-bad"),
        ("book", "yes-token"),
    ]
    assert [
        (item["resource"], item.get("request_key"))
        for item in snapshot["responses"]
    ] == [
        ("clob_server_time", None),
        ("clob_market", "condition-good"),
        ("clob_market", "condition-bad"),
        ("clob_book", "yes-token"),
    ]
    failed = snapshot["responses"][2]
    assert failed["response_status"] == "error"
    assert "raw_json" not in failed
    assert failed["error"] == {
        "error_type": "HTTPStatus",
        "error_code": "http_status_failure",
    }
    assert failed["provenance"] == {
        "raw_response_available": True,
        "source": "clob",
        "method": "GET",
        "url": (
            "https://clob.polymarket.com/clob-markets/condition-bad"
        ),
        "request_parameter_names": ["condition_id"],
        "status_code": 503,
        "requested_at_epoch_seconds": "1721808001.125",
        "received_at_epoch_seconds": "1721808001.375",
        "attempt": 2,
        "response_headers": {"Content-Type": "application/json"},
        "omitted_response_headers": ["Set-Cookie"],
        "body_sha256": hashlib.sha256(
            b'{"error":"upstream unavailable"}'
        ).hexdigest(),
        "body_bytes": 32,
    }
    persisted = json.dumps(snapshot, default=str)
    assert secret not in persisted
    assert "api_key" not in persisted
    assert "?" not in failed["provenance"]["url"]


def test_single_failed_resource_is_an_explicit_error_snapshot() -> None:
    class FailingBookSources(FakePublicSources):
        def book(self, token_id: str) -> Fetched[object]:
            self.calls.append(("book", token_id))
            raise PublicSourceError(
                "timeout at https://clob.polymarket.com/book?token=secret",
                code="request_timeout",
                error_type="Timeout",
            )

    sources = FailingBookSources()
    snapshot = PublicSourcesSnapshotClient(sources).fetch_snapshot(
        "full_book",
        asset_ids=("missing-token",),
    )

    assert not isinstance(snapshot, SnapshotEnvelope)
    assert snapshot["responses"] == [
        {
            "resource": "clob_book",
            "response_status": "error",
            "error": {
                "error_type": "Timeout",
                "error_code": "request_timeout",
            },
            "provenance": {"raw_response_available": False},
            "request_key": "missing-token",
        }
    ]
    assert "secret" not in json.dumps(snapshot)


def test_snapshot_propagates_unexpected_global_source_failure() -> None:
    class BrokenSources(FakePublicSources):
        def book(self, token_id: str) -> Fetched[object]:
            raise RuntimeError("source client invariant failed")

    with pytest.raises(RuntimeError, match="invariant"):
        PublicSourcesSnapshotClient(BrokenSources()).fetch_snapshot(
            "full_book",
            asset_ids=("yes-token",),
        )


def test_single_http_response_uses_recorder_lossless_envelope() -> None:
    sources = FakePublicSources()
    client = PublicSourcesSnapshotClient(sources)

    snapshot = client.fetch_snapshot(
        "full_book", asset_ids=("yes-token",)
    )

    assert isinstance(snapshot, SnapshotEnvelope)
    assert snapshot.raw_body == (
        b'{"asset_id":"yes-token","bids":[{"price":0.41,'
        b'"size":12.50}],"asks":[]}'
    )
    assert snapshot.status_code == 200
    assert snapshot.request_url == "https://clob.polymarket.com/book"
    assert snapshot.requested_at == "1721808000.125"
    assert snapshot.received_at == "1721808000.375"
    assert snapshot.payload["responses"][0]["raw_json"]["bids"][0][
        "price"
    ] == Decimal("0.41")


def test_reward_and_rule_snapshots_are_bounded_and_explicitly_scoped() -> None:
    sources = FakePublicSources()
    client = PublicSourcesSnapshotClient(
        sources,
        reward_page_limit=50,
        reward_max_pages=2,
        rule_market_ids=("market-1", "market-2"),
    )

    rewards = client.fetch_snapshot("rewards")
    rules = client.fetch_snapshot("rules")

    assert sources.calls == [
        ("current_reward_pages", 50, 2),
        ("resolution_rules", "market-1"),
        ("resolution_rules", "market-2"),
    ]
    assert [item["resource"] for item in rewards["responses"]] == [
        "current_rewards",
        "current_rewards",
    ]
    assert [item["page_number"] for item in rewards["responses"]] == [1, 2]
    assert [item["request_key"] for item in rules["responses"]] == [
        "market-1",
        "market-2",
    ]
    assert all(
        item["resource"] == "resolution_rules"
        for item in rules["responses"]
    )


def test_snapshot_rejects_lookalike_host_before_persisting_provenance() -> None:
    class LookalikeSources(FakePublicSources):
        def gamma_events(
            self, *, limit: int, cursor: str | None = None
        ) -> Fetched[GammaPage]:
            return _fetched(
                b'{"data":[],"limit":100,"next_cursor":null}',
                GammaPage(
                    items=(),
                    next_cursor=None,
                    response_limit=100,
                ),
                source="gamma",
                url=(
                    "https://gamma-api.polymarket.com.evil.test/"
                    "events/keyset"
                ),
            )

    with pytest.raises(PublicSourceError, match="non-public source"):
        PublicSourcesSnapshotClient(LookalikeSources()).fetch_snapshot(
            "gamma"
        )


def test_snapshot_rejects_credential_metadata_without_echoing_value() -> None:
    class CredentialSources(FakePublicSources):
        def gamma_events(
            self, *, limit: int, cursor: str | None = None
        ) -> Fetched[GammaPage]:
            return _fetched(
                b'{"data":[],"limit":100,"next_cursor":null}',
                GammaPage(
                    items=(),
                    next_cursor=None,
                    response_limit=100,
                ),
                source="gamma",
                url="https://gamma-api.polymarket.com/events/keyset",
                request_params={"x-api-key": "do-not-echo"},
            )

    with pytest.raises(
        PublicSourceError, match="credential-like"
    ) as caught:
        PublicSourcesSnapshotClient(CredentialSources()).fetch_snapshot(
            "gamma"
        )

    assert "do-not-echo" not in str(caught.value)


def test_provenance_omits_sensitive_response_header_values() -> None:
    class CookieSources(FakePublicSources):
        def gamma_events(
            self, *, limit: int, cursor: str | None = None
        ) -> Fetched[GammaPage]:
            return _fetched(
                b'{"data":[],"limit":100,"next_cursor":null}',
                GammaPage(
                    items=(),
                    next_cursor=None,
                    response_limit=100,
                ),
                source="gamma",
                url="https://gamma-api.polymarket.com/events/keyset",
                response_headers={
                    "Content-Type": "application/json",
                    "Set-Cookie": "session=do-not-persist",
                },
            )

    snapshot = PublicSourcesSnapshotClient(
        CookieSources()
    ).fetch_snapshot("gamma")
    provenance = snapshot["responses"][0]["provenance"]

    assert provenance["response_headers"] == {
        "Content-Type": "application/json"
    }
    assert provenance["omitted_response_headers"] == ["Set-Cookie"]
    assert "do-not-persist" not in json.dumps(provenance)


def test_bounded_gamma_snapshot_marks_unfetched_cursor_as_truncated() -> None:
    class MoreGammaSources(FakePublicSources):
        def gamma_events(
            self, *, limit: int, cursor: str | None = None
        ) -> Fetched[GammaPage]:
            return _fetched(
                b'{"data":[],"limit":100,"next_cursor":"more-events"}',
                GammaPage(
                    items=(),
                    next_cursor="more-events",
                    response_limit=100,
                ),
                source="gamma",
                url="https://gamma-api.polymarket.com/events/keyset",
            )

    snapshot = PublicSourcesSnapshotClient(
        MoreGammaSources(), gamma_max_pages=1
    ).fetch_snapshot("gamma")

    assert snapshot["truncated_resources"] == ["gamma_events"]


def test_snapshot_fails_closed_when_requested_scope_is_empty() -> None:
    sources = FakePublicSources()
    client = PublicSourcesSnapshotClient(sources)

    with pytest.raises(ValueError, match="rule_market_ids"):
        client.fetch_snapshot("rules")
    with pytest.raises(ValueError, match="asset_ids"):
        client.fetch_snapshot("full_book")
    with pytest.raises(ValueError, match="unsupported"):
        client.fetch_snapshot("orders")

    assert sources.calls == []


def test_batch_and_page_limits_require_positive_integers(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        CaptureStoreRecorderSink(
            CaptureStore(tmp_path), max_records_per_batch=1.5  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="gamma_page_limit"):
        PublicSourcesSnapshotClient(
            FakePublicSources(),
            gamma_page_limit=1.5,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="reward_max_pages"):
        PublicSourcesSnapshotClient(
            FakePublicSources(),
            reward_max_pages=True,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_snapshot_client_serializes_shared_session_and_rate_limiter() -> None:
    class OverlapDetectingSources(FakePublicSources):
        def __init__(self) -> None:
            super().__init__()
            self.guard = threading.Lock()
            self.active = 0
            self.max_active = 0

        def gamma_events(
            self, *, limit: int, cursor: str | None = None
        ) -> Fetched[GammaPage]:
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            try:
                return super().gamma_events(limit=limit, cursor=cursor)
            finally:
                with self.guard:
                    self.active -= 1

    sources = OverlapDetectingSources()
    client = PublicSourcesSnapshotClient(sources)

    await asyncio.gather(
        *(
            asyncio.to_thread(client.fetch_snapshot, "gamma")
            for _ in range(4)
        )
    )

    assert sources.max_active == 1


@pytest.mark.asyncio
async def test_close_surfaces_failure_from_emit_cancelled_by_outer_timeout(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class FailingWriter:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate

        def append(self, **kwargs: Any) -> Any:
            started.set()
            release.wait(timeout=1)
            raise OSError("simulated disk failure")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.delegate, name)

    class FailingStore(CaptureStore):
        def open_raw_batch(self, **kwargs: Any) -> Any:
            return FailingWriter(super().open_raw_batch(**kwargs))

    sink = CaptureStoreRecorderSink(FailingStore(tmp_path))
    emit_task = asyncio.create_task(sink.emit(_recorder_record()))
    assert await asyncio.to_thread(started.wait, 1)
    emit_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await emit_task
    release.set()

    with pytest.raises(RuntimeError, match="background persistence"):
        await sink.close()


@pytest.mark.asyncio
async def test_checkpoint_snapshot_is_immune_to_caller_nested_mutation(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    sink = CaptureStoreRecorderSink(store)
    checkpoint = {
        "schema_version": "edge-lab-recorder.checkpoint.v1",
        "updated_at": "2026-07-24T08:05:00Z",
        "cursors": {"yes-token": {"last_sequence": 7}},
    }

    await sink.checkpoint("clob_market_ws", checkpoint)
    checkpoint["cursors"]["yes-token"]["last_sequence"] = 999
    await sink.close()

    persisted = store.read_checkpoint("clob_market_ws")
    assert persisted is not None
    assert persisted["checkpoint"]["recorder_checkpoint"]["cursors"][
        "yes-token"
    ]["last_sequence"] == 7


@pytest.mark.asyncio
async def test_restart_rejects_checkpoint_whose_finalized_raw_was_tampered(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path)
    first = CaptureStoreRecorderSink(store)
    await first.emit(_recorder_record(sequence=23))
    await first.checkpoint(
        "clob_market_ws",
        {
            "schema_version": "edge-lab-recorder.checkpoint.v1",
            "updated_at": "2026-07-24T08:06:00Z",
            "last_sequence": 23,
        },
    )
    manifests = await first.close()
    raw_path = tmp_path / str(manifests[0]["raw_path"])
    raw_path.chmod(0o644)
    with raw_path.open("ab") as handle:
        handle.write(b'{"tampered":true}\n')

    restarted = CaptureStoreRecorderSink(store)

    with pytest.raises(RuntimeError, match="checksum"):
        restarted.load_recorder_checkpoint("clob_market_ws")
    await restarted.close()


def test_reward_adapter_hard_caps_a_misbehaving_page_iterator() -> None:
    class UnboundedRewardSources(FakePublicSources):
        def __init__(self) -> None:
            super().__init__()
            self.pages_yielded = 0

        def current_reward_pages(
            self, *, limit: int, max_pages: int
        ) -> Any:
            for page_number in range(100):
                self.pages_yielded += 1
                body = (
                    b'{"data":[],"limit":50,"count":0,'
                    b'"next_cursor":"still-more"}'
                )
                yield _fetched(
                    body,
                    RewardPage(
                        items=(),
                        next_cursor="still-more",
                        response_limit=limit,
                        count=0,
                    ),
                    source="clob_rewards",
                    url=(
                        "https://clob.polymarket.com/"
                        "rewards/markets/current"
                    ),
                    request_params={"limit": limit},
                )

    sources = UnboundedRewardSources()
    snapshot = PublicSourcesSnapshotClient(
        sources,
        reward_page_limit=50,
        reward_max_pages=2,
    ).fetch_snapshot("rewards")

    assert not isinstance(snapshot, SnapshotEnvelope)
    assert len(snapshot["responses"]) == 2
    assert snapshot["truncated_resources"] == ["current_rewards"]
    assert sources.pages_yielded == 2
