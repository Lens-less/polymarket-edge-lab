from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.edge_lab.dynamic_short_crypto_service as service_module
from src.edge_lab.chainlink_ptb import (
    FinalizedChainlinkBoundary,
    PriceToBeatRejection,
)
from src.edge_lab.data_store import CaptureStore, canonical_record_id
from src.edge_lab.short_crypto_registry import build_short_crypto_registry
from src.edge_lab.dynamic_short_crypto_service import (
    DiscoveryInputError,
    DynamicShortCryptoService,
    DynamicShortCryptoServiceConfig,
    LifecycleRemediationPlan,
    _ObservedRecorderSink,
    freeze_finalized_discovery_paths,
)
from src.edge_lab.sources import (
    Fetched,
    FetchMetadata,
    PublicSourceError,
    RawResponse,
)


def _finalize(path: Path, content: str = "{}\n") -> Path:
    path.write_text(content, encoding="utf-8")
    raw = path.read_bytes()
    manifest = {
        "manifest_version": "capture-manifest.v1",
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": path.parent.name,
        "batch_id": path.stem,
        "raw_path": f"raw/{path.parent.name}/{path.name}",
        "record_count": content.count("\n"),
        "checksum": {
            "algorithm": "sha256",
            "value": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "lines": content.count("\n"),
        },
    }
    path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return path


def test_freeze_finalized_discovery_paths_is_sorted_and_ignores_partials(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    second = _finalize(discovery / "b.jsonl")
    first = _finalize(discovery / "a.jsonl")
    (discovery / "active.jsonl.partial").write_text("{}\n", encoding="utf-8")
    (discovery / "not-finalized.jsonl").write_text("{}\n", encoding="utf-8")

    frozen = freeze_finalized_discovery_paths(discovery)

    assert frozen == (first.resolve(), second.resolve())


def test_freeze_rejects_manifest_checksum_mismatch(tmp_path: Path) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    raw_path = _finalize(discovery / "batch.jsonl")
    raw_path.write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(DiscoveryInputError) as caught:
        freeze_finalized_discovery_paths(discovery)

    assert caught.value.code == "finalized_checksum_mismatch"


def test_freeze_rejects_manifest_schema_drift(tmp_path: Path) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    raw_path = _finalize(discovery / "batch.jsonl")
    manifest_path = raw_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "edge-lab-recorder.raw.v2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DiscoveryInputError) as caught:
        freeze_finalized_discovery_paths(discovery)

    assert caught.value.code == "finalized_manifest_invalid"


@pytest.mark.asyncio
async def test_registry_rejects_bytes_changed_after_manifest_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    real_builder = service_module.build_short_crypto_registry

    def changed_builder(paths: tuple[Path, ...]) -> dict[str, object]:
        snapshot = real_builder(paths)
        snapshot["inputs"][0]["sha256"] = "0" * 64
        return snapshot

    monkeypatch.setattr(
        service_module,
        "build_short_crypto_registry",
        changed_builder,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
        ),
        source_client=object(),
        websocket_factory=object(),
    )

    with pytest.raises(DiscoveryInputError) as caught:
        await service.scan_once(
            now_ms=OPEN_SECONDS * 1_000 - 120_000
        )

    assert (
        caught.value.code
        == "registry_input_changed_after_manifest_validation"
    )
    assert service.snapshot()["processed_discovery_file_count"] == 0
    assert service.snapshot()["target_count"] == 0
    await service.close()


OPEN_SECONDS = 1_784_979_900


def _iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class _MemoryPersistence:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.checkpoints: list[str] = []
        self.operations: list[tuple[str, str, str | None]] = []
        self.block_event_type: str | None = None
        self.block_entered = asyncio.Event()
        self.block_release = asyncio.Event()

    async def emit(self, record: dict[str, object]) -> SimpleNamespace:
        self.records.append(dict(record))
        self.operations.append(
            (
                "emit",
                str(record.get("source")),
                (
                    None
                    if record.get("event_type") is None
                    else str(record["event_type"])
                ),
            )
        )
        if record.get("event_type") == self.block_event_type:
            self.block_entered.set()
            await self.block_release.wait()
        record_id = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return SimpleNamespace(record_id=record_id)

    async def checkpoint(
        self,
        source: str,
        checkpoint: dict[str, object],
    ) -> None:
        del checkpoint
        self.checkpoints.append(source)
        self.operations.append(("checkpoint", source, None))

    async def close(self) -> tuple[dict[str, object], ...]:
        return ()


def _recovery_result(
    state: dict[str, object],
    *,
    worker_actions: tuple[dict[str, object], ...] = (),
    callback_generation: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        recovered_from_run_ids=("prior-run",),
        replayed_record_count=9,
        state_hash="a" * 64,
        gaps=(),
        exclusions=(),
        run_classifications=(
            {
                "run_id": "prior-run",
                "classification": "clean_completed",
                "conditions": ["clean_completed"],
            },
        ),
        state=state,
        recovery_decision={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-recovery-decision.v1"
            ),
            "decision_id": "b" * 64,
            "state_hash": "a" * 64,
            "must_be_durable_before_worker_actions": True,
        },
        worker_actions=worker_actions,
        callback_generation=callback_generation,
    )


def _empty_recovered_state(
    *,
    lifecycle_cohort_journal: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": (
            "edge-lab-dynamic-short-crypto-recovered-state.v2"
        ),
        "processed_finalized_discovery_descriptors": [],
        "registry": {
            "snapshot_sha256": None,
            "revision_record_ids": [],
            "snapshot": None,
        },
        "targets": {},
        "gamma_evidence": {},
        "latest_gamma_records": {},
        "chainlink_evidence": {},
        "subscription_decisions": {},
        "worker_receipts": {},
        "worker_liveness": {},
        "lifecycle_cohort_journal": (
            []
            if lifecycle_cohort_journal is None
            else lifecycle_cohort_journal
        ),
    }


def _recovered_root_commitment(
    record_id: str,
) -> dict[str, object]:
    return {
        "event_type": "lifecycle_cohort_committed",
        "record_id": record_id,
        "run_id": "prior-run",
        "payload": {
            "scheduler_now_ms": OPEN_SECONDS * 1_000 - 600_000,
            "schema_version": (
                "edge-lab.phase2-lifecycle-prospective-cohort.v1"
            ),
            "selection_rule": (
                "first_mature_targets_opening_at_or_after_boundary"
            ),
            "eligibility_start_ms": OPEN_SECONDS * 1_000,
            "sample_size": 20,
            "threshold": "0.8",
            "settlement_timeout_ms": 3_600_000,
            "assets": ["BTC", "ETH"],
            "horizons": ["5m", "15m"],
            "selection_order": [
                "opens_at_ms",
                "closes_at_ms",
                "slug",
            ],
            "earliest_valid_commitment_wins": True,
            "must_be_durable_before_public_network": True,
            "actual_fill": False,
            "authenticated_fill": False,
            "orders_submitted": 0,
            "authenticated_endpoints_used": 0,
        },
    }


def _transport_record(
    event_type: str,
    *,
    received_at_ms: int,
    monotonic_ns: int,
    connection_id: str = "connection",
    schema_version: str = "edge-lab-recorder.lifecycle.v1",
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": schema_version,
        "source": "clob_market_ws",
        "kind": "lifecycle",
        "event_type": event_type,
        "session_id": "session",
        "connection_id": connection_id,
        "received_at": _iso(received_at_ms),
        "event_at": None,
        "sequence": None,
        "monotonic_ns": monotonic_ns,
    }
    if event_type == "resync_complete":
        record["detail"] = {
            "watermark_ns": 9_000,
            "asset_watermarks_ns": {
                "1" * 76: 8_000,
                "2" * 76: 9_000,
            },
        }
    elif event_type == "heartbeat_ack":
        record["schema_version"] = schema_version
        record["raw_frame"] = "PONG"
    return record


def _finalized_boundary(
    target: object,
    *,
    role: str,
    received_at_ms: int,
) -> FinalizedChainlinkBoundary:
    boundary_ms = (
        target.opens_at_ms if role == "open" else target.closes_at_ms
    )
    return FinalizedChainlinkBoundary(
        boundary_role=role,
        source_topic=target.source_topic,
        source_symbol=target.source_symbol,
        rule_url="https://data.chain.link/streams/btc-usd",
        rule_hash=target.rule_hash,
        inner_timestamp_ms=boundary_ms,
        price=Decimal(
            "100.000000000000000000"
            if role == "open"
            else "101.000000000000000000"
        ),
        display_value="100" if role == "open" else "101",
        display_encoding="exact_decimal",
        full_accuracy_value=(
            "100000000000000000000"
            if role == "open"
            else "101000000000000000000"
        ),
        record_id=("a" if role == "open" else "b") * 64,
        session_id="rtds-session",
        connection_id="rtds-connection",
        received_at=_iso(received_at_ms),
        source_manifest_path="/verified/rtds.manifest.json",
        source_manifest_sha256="c" * 64,
        raw_path="/verified/rtds.jsonl",
        raw_sha256="d" * 64,
        line_number=1 if role == "open" else 2,
        retrospective=role == "close",
        available_at_first_decision=role == "open",
    )


@pytest.mark.asyncio
async def test_close_liveness_requires_durable_same_connection_pong_bracket(
) -> None:
    close_ms = OPEN_SECONDS * 1_000 + 300_000
    persistence = _MemoryPersistence()
    sink = _ObservedRecorderSink(
        persistence,
        asset_ids=("1" * 76, "2" * 76),
    )
    await sink.emit(
        _transport_record(
            "resync_complete",
            received_at_ms=close_ms - 20_000,
            monotonic_ns=100,
        )
    )
    await sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms - 10_000,
            monotonic_ns=200,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        )
    )
    await sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms + 10_000,
            monotonic_ns=300,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        )
    )

    proof = sink.close_liveness_proof(
        closes_at_ms=close_ms,
        tolerance_ms=30_000,
        as_of_ms=close_ms + 10_000,
    )

    assert proof is not None
    assert proof.connection_id == "connection"
    assert persistence.checkpoints == [
        "clob_http",
        "clob_market_ws",
    ]


@pytest.mark.asyncio
async def test_close_liveness_rejects_unknown_ack_and_incomplete_resync(
) -> None:
    close_ms = OPEN_SECONDS * 1_000 + 300_000
    persistence = _MemoryPersistence()
    sink = _ObservedRecorderSink(
        persistence,
        asset_ids=("1" * 76, "2" * 76),
    )
    await sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms - 10_000,
            monotonic_ns=100,
            schema_version="edge-lab-recorder.heartbeat-ack.v2",
        )
    )
    await sink.emit(
        _transport_record(
            "resync_complete",
            received_at_ms=close_ms - 5_000,
            monotonic_ns=200,
        )
    )
    await sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms + 5_000,
            monotonic_ns=300,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        )
    )

    assert (
        sink.close_liveness_proof(
            closes_at_ms=close_ms,
            tolerance_ms=30_000,
            as_of_ms=close_ms + 5_000,
        )
        is None
    )


@pytest.mark.asyncio
async def test_unknown_lifecycle_schema_cannot_set_or_clear_readiness(
) -> None:
    close_ms = OPEN_SECONDS * 1_000 + 300_000
    persistence = _MemoryPersistence()
    sink = _ObservedRecorderSink(
        persistence,
        asset_ids=("1" * 76, "2" * 76),
    )
    await sink.emit(
        _transport_record(
            "resync_complete",
            received_at_ms=close_ms - 20_000,
            monotonic_ns=100,
            schema_version="edge-lab-recorder.lifecycle.v2",
        )
    )
    assert not sink.ready.is_set()
    assert persistence.checkpoints == []

    await sink.emit(
        _transport_record(
            "resync_complete",
            received_at_ms=close_ms - 19_000,
            monotonic_ns=200,
        )
    )
    assert sink.ready.is_set()
    await sink.emit(
        _transport_record(
            "disconnected",
            received_at_ms=close_ms - 18_000,
            monotonic_ns=300,
            schema_version="edge-lab-recorder.lifecycle.v2",
        )
    )
    assert sink.ready.is_set()
    assert [event["event_type"] for event in sink.lifecycle_events] == [
        "resync_complete"
    ]


@pytest.mark.asyncio
async def test_close_liveness_rejects_single_or_implausibly_wide_bracket(
) -> None:
    close_ms = OPEN_SECONDS * 1_000 + 300_000
    persistence = _MemoryPersistence()
    sink = _ObservedRecorderSink(
        persistence,
        asset_ids=("1" * 76, "2" * 76),
    )
    await sink.emit(
        _transport_record(
            "resync_complete",
            received_at_ms=close_ms - 10_000,
            monotonic_ns=100,
        )
    )
    await sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms,
            monotonic_ns=200,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        )
    )
    assert (
        sink.close_liveness_proof(
            closes_at_ms=close_ms,
            tolerance_ms=30_000,
            as_of_ms=close_ms,
        )
        is None
    )

    await sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms + 1_000,
            monotonic_ns=10_800_000_000_000,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        )
    )
    assert (
        sink.close_liveness_proof(
            closes_at_ms=close_ms,
            tolerance_ms=30_000,
            as_of_ms=close_ms + 1_000,
        )
        is None
    )


@pytest.mark.asyncio
async def test_close_liveness_is_pending_while_disconnect_persistence_inflight(
) -> None:
    close_ms = OPEN_SECONDS * 1_000 + 300_000
    persistence = _MemoryPersistence()
    sink = _ObservedRecorderSink(
        persistence,
        asset_ids=("1" * 76, "2" * 76),
    )
    for record in (
        _transport_record(
            "resync_complete",
            received_at_ms=close_ms - 20_000,
            monotonic_ns=100,
        ),
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms - 10_000,
            monotonic_ns=200,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        ),
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms + 10_000,
            monotonic_ns=300,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        ),
    ):
        await sink.emit(record)
    persistence.block_event_type = "disconnected"
    pending = asyncio.create_task(
        sink.emit(
            _transport_record(
                "disconnected",
                received_at_ms=close_ms + 20_000,
                monotonic_ns=400,
            )
        )
    )
    await asyncio.wait_for(persistence.block_entered.wait(), timeout=1)

    assert (
        sink.close_liveness_proof(
            closes_at_ms=close_ms,
            tolerance_ms=30_000,
            as_of_ms=close_ms + 20_000,
        )
        is None
    )

    persistence.block_release.set()
    await pending


def _announcement() -> dict[str, object]:
    slug = f"btc-updown-5m-{OPEN_SECONDS}"
    condition_id = "0x" + "ab" * 32
    token_ids = ["1" * 76, "2" * 76]
    description = (
        "The resolution source is Chainlink, specifically the BTC/USD "
        "data stream at https://data.chain.link/streams/btc-usd."
    )
    event = {
        "event_type": "new_market",
        "id": "3081000",
        "slug": slug,
        "condition_id": condition_id,
        "market": condition_id,
        "assets_ids": token_ids,
        "clob_token_ids": token_ids,
        "outcomes": ["Up", "Down"],
        "description": description,
        "event_message": {"slug": slug},
    }
    inner = {
        "schema_version": "clob-market-ws.new_market.v1",
        "source": "clob_market_ws",
        "event_type": "new_market",
        "payload": event,
    }
    record: dict[str, object] = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "clob_market_ws",
        "received_at": _iso(OPEN_SECONDS * 1_000 - 86_400_000),
        "event_at": None,
        "sequence": None,
        "payload": inner,
    }
    record["record_id"] = canonical_record_id(record)
    return record


class _FakeGammaSource:
    def __init__(self, announcement: dict[str, object]) -> None:
        inner = announcement["payload"]
        assert isinstance(inner, dict)
        event = inner["payload"]
        assert isinstance(event, dict)
        close_ms = OPEN_SECONDS * 1_000 + 300_000
        self.market = {
            "id": event["id"],
            "slug": event["slug"],
            "conditionId": event["condition_id"],
            "outcomes": ["Up", "Down"],
            "clobTokenIds": event["clob_token_ids"],
            "description": event["description"],
            "resolutionSource": (
                "https://data.chain.link/streams/btc-usd"
            ),
            "endDate": _iso(close_ms),
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "outcomePrices": ["0.5", "0.5"],
        }
        self.calls: list[str] = []
        self.response_time_seconds = float(OPEN_SECONDS - 59)

    def gamma_market(self, market_id: str) -> Fetched[dict[str, object]]:
        self.calls.append(market_id)
        body = json.dumps(
            self.market,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        now = self.response_time_seconds
        return Fetched(
            raw=RawResponse(
                body=body,
                text=body.decode(),
                metadata=FetchMetadata(
                    source="gamma",
                    method="GET",
                    url=f"https://gamma-api.polymarket.com/markets/{market_id}",
                    request_params={},
                    status_code=200,
                    requested_at=float(now),
                    received_at=float(now) + 0.01,
                    attempt=1,
                    response_headers={"content-type": "application/json"},
                ),
            ),
            value=self.market,
        )


@pytest.mark.asyncio
async def test_gamma_verification_commits_evidence_before_control_once(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    announcement = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(
            announcement,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    source = _FakeGammaSource(announcement)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=source,
        websocket_factory=object(),
    )
    await service._control_sink.close()
    persistence = _MemoryPersistence()
    service._control_sink = persistence

    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 600_000)
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 599_000)

    gamma_record = persistence.operations.index(
        ("emit", "gamma_http", "gamma_market")
    )
    gamma_checkpoint = persistence.operations.index(
        ("checkpoint", "gamma_http", None)
    )
    verification = persistence.operations.index(
        (
            "emit",
            "dynamic_short_crypto_service",
            "gamma_target_verified",
        )
    )
    assert gamma_record < gamma_checkpoint < verification
    assert persistence.checkpoints.count("gamma_http") == 1
    assert [
        record["event_type"]
        for record in persistence.records
    ].count("gamma_target_verified") == 1
    assert source.calls == ["3081000", "3081000"]
    await service.close()


@pytest.mark.asyncio
async def test_live_gamma_sweep_rechecks_scheduler_after_slow_public_poll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    announcement = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(
            announcement,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    source = _FakeGammaSource(announcement)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service._control_sink.close()
    persistence = _MemoryPersistence()
    service._control_sink = persistence
    open_ms = OPEN_SECONDS * 1_000

    initial = await service.scan_once(now_ms=open_ms - 120_000)

    assert initial["active_worker_count"] == 0
    assert initial["target_states"] == {"announced": 1}
    clock_ms = [open_ms - 61_000]

    async def slow_public_poll(
        target: object,
        *,
        now_ms: int,
        initial: bool = False,
    ) -> None:
        del target, initial
        assert now_ms == open_ms - 61_000
        clock_ms[0] = open_ms - 59_000

    monkeypatch.setattr(service, "_poll_gamma_target", slow_public_poll)
    monkeypatch.setattr(
        service_module.time,
        "time",
        lambda: clock_ms[0] / 1_000,
    )

    status = await service.scan_once()

    assert status["active_worker_count"] == 1
    capture_decision = next(
        record
        for record in persistence.records
        if record["event_type"] == "capture_decision"
    )
    assert capture_decision["payload"]["scheduler_now_ms"] == (
        open_ms - 59_000
    )
    await service.close()


@pytest.mark.asyncio
async def test_new_registry_gamma_sweep_rechecks_scheduler_between_slow_polls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    first = _announcement()
    second = json.loads(json.dumps(first))
    second_event = second["payload"]["payload"]
    second_event["id"] = "3081001"
    second_event["slug"] = f"eth-updown-5m-{OPEN_SECONDS}"
    second_event["condition_id"] = "0x" + "cd" * 32
    second_event["market"] = second_event["condition_id"]
    second_event["assets_ids"] = ["3" * 76, "4" * 76]
    second_event["clob_token_ids"] = ["3" * 76, "4" * 76]
    second_event["event_message"]["slug"] = second_event["slug"]
    second_event["description"] = (
        "The resolution source is Chainlink, specifically the ETH/USD "
        "data stream at https://data.chain.link/streams/eth-usd."
    )
    second["record_id"] = canonical_record_id(second)
    _finalize(
        discovery / "batch.jsonl",
        "".join(
            json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
            for item in (first, second)
        ),
    )
    delegates = {}
    for announcement in (first, second):
        inner = announcement["payload"]
        assert isinstance(inner, dict)
        event = inner["payload"]
        assert isinstance(event, dict)
        delegate = _FakeGammaSource(announcement)
        asset = str(event["slug"]).split("-", 1)[0]
        delegate.market["resolutionSource"] = (
            f"https://data.chain.link/streams/{asset}-usd"
        )
        delegates[str(event["id"])] = delegate
    open_ms = OPEN_SECONDS * 1_000
    clock_ms = [open_ms - 120_000]
    second_poll_entered = threading.Event()
    release_second_poll = threading.Event()

    class _BlockingMultiGammaSource:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def gamma_market(self, market_id: str) -> Fetched[dict[str, object]]:
            self.calls.append(market_id)
            if len(self.calls) == 1:
                clock_ms[0] = open_ms - 59_000
            elif len(self.calls) == 2:
                second_poll_entered.set()
                if not release_second_poll.wait(timeout=2):
                    raise AssertionError("second Gamma poll was not released")
            delegate = delegates[market_id]
            delegate.response_time_seconds = clock_ms[0] / 1_000
            return delegate.gamma_market(market_id)

    source = _BlockingMultiGammaSource()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service._control_sink.close()
    persistence = _MemoryPersistence()
    service._control_sink = persistence
    monkeypatch.setattr(
        service_module.time,
        "time",
        lambda: clock_ms[0] / 1_000,
    )

    scan_task = asyncio.create_task(service.scan_once())
    entered = await asyncio.to_thread(second_poll_entered.wait, 1)
    active_during_second_poll = service.snapshot()["active_worker_count"]
    await asyncio.wait_for(
        _FakeRecorder.instances[0].sink.ready.wait(),
        timeout=2,
    )
    clock_ms[0] = open_ms + 1_000
    release_second_poll.set()
    status = await asyncio.wait_for(scan_task, timeout=2)

    assert entered is True
    assert active_during_second_poll == 1
    assert status["active_worker_count"] == 1
    assert status["target_states"] == {"excluded": 1, "open": 1}
    assert _FakeRecorder.instances[0].stopped is False
    capture_decision = next(
        record
        for record in persistence.records
        if record["event_type"] == "capture_decision"
    )
    assert capture_decision["payload"]["scheduler_now_ms"] == open_ms - 59_000
    await service.close()


@pytest.mark.asyncio
async def test_live_chainlink_backfill_does_not_block_next_subscription_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    first = _announcement()
    second = json.loads(json.dumps(first))
    second_open_seconds = OPEN_SECONDS + 300
    second_event = second["payload"]["payload"]
    second_event["id"] = "3081001"
    second_event["slug"] = (
        f"eth-updown-5m-{second_open_seconds}"
    )
    second_event["condition_id"] = "0x" + "cd" * 32
    second_event["market"] = second_event["condition_id"]
    second_event["assets_ids"] = ["3" * 76, "4" * 76]
    second_event["clob_token_ids"] = ["3" * 76, "4" * 76]
    second_event["event_message"]["slug"] = second_event["slug"]
    second_event["description"] = (
        "The resolution source is Chainlink, specifically the ETH/USD "
        "data stream at https://data.chain.link/streams/eth-usd."
    )
    second["record_id"] = canonical_record_id(second)
    _finalize(
        discovery / "batch.jsonl",
        "".join(
            json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
            for item in (first, second)
        ),
    )
    delegates = {}
    for announcement in (first, second):
        inner = announcement["payload"]
        assert isinstance(inner, dict)
        event = inner["payload"]
        assert isinstance(event, dict)
        delegate = _FakeGammaSource(announcement)
        asset = str(event["slug"]).split("-", 1)[0]
        target_open_seconds = int(
            str(event["slug"]).rsplit("-", 1)[1]
        )
        delegate.market["resolutionSource"] = (
            f"https://data.chain.link/streams/{asset}-usd"
        )
        delegate.market["endDate"] = _iso(
            (target_open_seconds + 300) * 1_000
        )
        delegates[str(event["id"])] = delegate

    class _MultiGammaSource:
        def gamma_market(
            self,
            market_id: str,
        ) -> Fetched[dict[str, object]]:
            delegate = delegates[market_id]
            delegate.response_time_seconds = clock_ms[0] / 1_000
            return delegate.gamma_market(market_id)

    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    (rtds / "historical.manifest.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    backfill_entered = threading.Event()
    release_backfill = threading.Event()

    def blocking_backfill(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary | None]:
        assert manifest_paths == (rtds / "historical.manifest.json",)
        backfill_entered.set()
        if not release_backfill.wait(timeout=2):
            raise AssertionError("Chainlink backfill was not released")
        return dict.fromkeys(requests)

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        blocking_backfill,
    )
    clock_ms = [OPEN_SECONDS * 1_000 - 59_000]
    monkeypatch.setattr(
        service_module.time,
        "time",
        lambda: clock_ms[0] / 1_000,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=_MultiGammaSource(),
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service._control_sink.close()
    persistence = _MemoryPersistence()
    service._control_sink = persistence
    scan_task: asyncio.Task[dict[str, object]] | None = None
    first_scan_returned = False
    second_status: dict[str, object] | None = None
    try:
        scan_task = asyncio.create_task(service.scan_once())
        entered = await asyncio.to_thread(backfill_entered.wait, 1)
        assert entered is True
        try:
            await asyncio.wait_for(
                asyncio.shield(scan_task),
                timeout=0.1,
            )
            first_scan_returned = True
        except TimeoutError:
            first_scan_returned = False
        if first_scan_returned:
            clock_ms[0] = second_open_seconds * 1_000 - 59_000
            second_status = await asyncio.wait_for(
                service.scan_once(),
                timeout=0.5,
            )
    finally:
        release_backfill.set()
        if scan_task is not None:
            await scan_task
        await service.close()

    assert first_scan_returned is True
    assert second_status is not None
    decision_slugs = [
        target["slug"]
        for record in persistence.records
        if record["event_type"] == "capture_decision"
        for target in record["payload"]["targets"]
    ]
    assert decision_slugs == [
        f"btc-updown-5m-{OPEN_SECONDS}",
        f"eth-updown-5m-{second_open_seconds}",
    ]


@pytest.mark.asyncio
async def test_live_chainlink_source_time_conflict_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    manifest = rtds / "historical.manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")

    def rejected_batch(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary | None]:
        assert manifest_paths == (manifest,)
        assert requests
        raise PriceToBeatRejection(
            "source_time_conflict",
            "local receipt cannot precede the inner Chainlink timestamp",
        )

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        rejected_batch,
    )
    clock_ms = [OPEN_SECONDS * 1_000 - 59_000]
    monkeypatch.setattr(
        service_module.time,
        "time",
        lambda: clock_ms[0] / 1_000,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service._control_sink.close()
    persistence = _MemoryPersistence()
    service._control_sink = persistence
    try:
        await service.scan_once()
        live_scan = service._live_chainlink_scan
        assert live_scan is not None
        assert live_scan.task is not None
        for _ in range(100):
            if live_scan.task.done():
                break
            await asyncio.sleep(0.01)
        assert live_scan.task.done()

        status = await service.scan_once()
        follow_up_status = await service.scan_once()

        assert status["active_worker_count"] == 1
        assert follow_up_status["active_worker_count"] == 1
        assert service._live_chainlink_scan is None
        assert service._chainlink_scanned_manifest_paths == {manifest}
        rejection = next(
            item
            for item in persistence.records
            if item["event_type"] == "chainlink_boundary_scan_rejected"
        )
        assert rejection["payload"]["error_code"] == "source_time_conflict"
        assert rejection["payload"]["manifest_count"] == 1
        assert rejection["payload"]["request_count"] == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_live_sweep_defers_known_far_target_before_subscription_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    far = _announcement()
    far_event = far["payload"]["payload"]
    far_event["slug"] = f"btc-updown-5m-{OPEN_SECONDS + 86_400}"
    far_event["assets_ids"] = ["3" * 76, "4" * 76]
    far_event["clob_token_ids"] = ["3" * 76, "4" * 76]
    far_event["event_message"]["slug"] = far_event["slug"]
    far["record_id"] = canonical_record_id(far)
    near = _announcement()
    near_event = near["payload"]["payload"]
    near_event["id"] = "3081001"
    near_event["slug"] = f"eth-updown-5m-{OPEN_SECONDS}"
    near_event["condition_id"] = "0x" + "cd" * 32
    near_event["market"] = near_event["condition_id"]
    near_event["event_message"]["slug"] = near_event["slug"]
    near_event["description"] = (
        "The resolution source is Chainlink, specifically the ETH/USD "
        "data stream at https://data.chain.link/streams/eth-usd."
    )
    near["record_id"] = canonical_record_id(near)
    _finalize(
        discovery / "batch.jsonl",
        "".join(
            json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
            for item in (far, near)
        ),
    )
    far_source = _FakeGammaSource(far)
    far_source.market["endDate"] = _iso(
        (OPEN_SECONDS + 86_400) * 1_000 + 300_000
    )
    near_source = _FakeGammaSource(near)
    near_source.market["resolutionSource"] = (
        "https://data.chain.link/streams/eth-usd"
    )
    delegates = {
        "3081000": far_source,
        "3081001": near_source,
    }
    open_ms = OPEN_SECONDS * 1_000
    clock_ms = [open_ms - 120_000]

    class _KnownTargetGammaSource:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.live = False

        def gamma_market(
            self,
            market_id: str,
        ) -> Fetched[dict[str, object]]:
            self.calls.append(market_id)
            if self.live:
                if market_id == "3081000":
                    clock_ms[0] = open_ms + 1_000
                else:
                    clock_ms[0] = open_ms - 59_000
            delegate = delegates[market_id]
            delegate.response_time_seconds = clock_ms[0] / 1_000
            return delegate.gamma_market(market_id)

    source = _KnownTargetGammaSource()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )

    initial = await service.scan_once(now_ms=clock_ms[0])

    assert initial["verified_target_count"] == 2
    assert initial["active_worker_count"] == 0
    source.calls.clear()
    source.live = True
    clock_ms[0] = open_ms - 61_000
    monkeypatch.setattr(
        service_module.time,
        "time",
        lambda: clock_ms[0] / 1_000,
    )

    status = await service.scan_once()

    assert source.calls == ["3081001"]
    assert status["active_worker_count"] == 1
    assert _FakeRecorder.instances[0].stopped is False
    await service.close()


@pytest.mark.asyncio
async def test_live_registry_defers_gamma_until_subscription_horizon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    near = _announcement()
    far = json.loads(json.dumps(near))
    far_event = far["payload"]["payload"]
    far_event["id"] = "3081001"
    far_event["slug"] = f"btc-updown-5m-{OPEN_SECONDS + 3_600}"
    far_event["condition_id"] = "0x" + "cd" * 32
    far_event["market"] = far_event["condition_id"]
    far_event["assets_ids"] = ["3" * 76, "4" * 76]
    far_event["clob_token_ids"] = ["3" * 76, "4" * 76]
    far_event["event_message"]["slug"] = far_event["slug"]
    far["record_id"] = canonical_record_id(far)
    _finalize(
        discovery / "batch.jsonl",
        "".join(
            json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
            for item in (near, far)
        ),
    )
    near_source = _FakeGammaSource(near)
    far_source = _FakeGammaSource(far)
    far_source.market["endDate"] = _iso(
        (OPEN_SECONDS + 3_600) * 1_000 + 300_000
    )
    delegates = {
        "3081000": near_source,
        "3081001": far_source,
    }

    class _MultiGammaSource:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def gamma_market(
            self,
            market_id: str,
        ) -> Fetched[dict[str, object]]:
            self.calls.append(market_id)
            return delegates[market_id].gamma_market(market_id)

    source = _MultiGammaSource()
    open_ms = OPEN_SECONDS * 1_000
    clock_ms = [open_ms - 121_000]
    monkeypatch.setattr(
        service_module.time,
        "time",
        lambda: clock_ms[0] / 1_000,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )

    before_horizon = await service.scan_once()

    assert source.calls == []
    assert before_horizon["target_count"] == 2
    assert before_horizon["verified_target_count"] == 0
    assert before_horizon["active_worker_count"] == 0

    clock_ms[0] = open_ms - 119_000
    inside_horizon = await service.scan_once()

    assert source.calls == ["3081000"]
    assert inside_horizon["verified_target_count"] == 1
    assert inside_horizon["active_worker_count"] == 0

    clock_ms[0] = open_ms - 59_000
    inside_subscription_lead = await service.scan_once()

    assert source.calls == ["3081000"]
    assert inside_subscription_lead["verified_target_count"] == 1
    assert inside_subscription_lead["active_worker_count"] == 1
    await service.close()


@pytest.mark.asyncio
async def test_live_gamma_sweep_acknowledges_ready_workers_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    first = _announcement()
    second = json.loads(json.dumps(first))
    second_event = second["payload"]["payload"]
    second_event["id"] = "3081001"
    second_event["slug"] = f"eth-updown-5m-{OPEN_SECONDS}"
    second_event["condition_id"] = "0x" + "cd" * 32
    second_event["market"] = second_event["condition_id"]
    second_event["assets_ids"] = ["3" * 76, "4" * 76]
    second_event["clob_token_ids"] = ["3" * 76, "4" * 76]
    second_event["event_message"]["slug"] = second_event["slug"]
    second_event["description"] = (
        "The resolution source is Chainlink, specifically the ETH/USD "
        "data stream at https://data.chain.link/streams/eth-usd."
    )
    second["record_id"] = canonical_record_id(second)
    _finalize(
        discovery / "batch.jsonl",
        "".join(
            json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
            for item in (first, second)
        ),
    )

    delegates = {}
    for announcement in (first, second):
        inner = announcement["payload"]
        assert isinstance(inner, dict)
        event = inner["payload"]
        assert isinstance(event, dict)
        delegate = _FakeGammaSource(announcement)
        asset = str(event["slug"]).split("-", 1)[0]
        delegate.market["resolutionSource"] = (
            f"https://data.chain.link/streams/{asset}-usd"
        )
        delegates[str(event["id"])] = delegate
    open_ms = OPEN_SECONDS * 1_000
    clock_ms = [open_ms - 120_000]
    second_poll_entered = threading.Event()
    release_second_poll = threading.Event()

    class _CrossingGammaSource:
        def __init__(self) -> None:
            self.crossing = False
            self.crossing_calls = 0

        def gamma_market(
            self,
            market_id: str,
        ) -> Fetched[dict[str, object]]:
            if self.crossing:
                self.crossing_calls += 1
                if self.crossing_calls == 1:
                    clock_ms[0] = open_ms - 59_000
                elif self.crossing_calls == 2:
                    second_poll_entered.set()
                    if not release_second_poll.wait(timeout=2):
                        raise AssertionError(
                            "second Gamma poll was not released"
                        )
            delegate = delegates[market_id]
            delegate.response_time_seconds = clock_ms[0] / 1_000
            return delegate.gamma_market(market_id)

    class _ScopeReadyRecorder(_FakeRecorder):
        instances: list["_ScopeReadyRecorder"] = []

        async def run(self) -> None:
            asset_watermarks = {
                asset_id: 8_000 + index
                for index, asset_id in enumerate(
                    self.config.clob_asset_ids
                )
            }
            common = {
                "schema_version": "edge-lab-recorder.record.v1",
                "session_id": "session",
                "connection_id": "connection",
                "received_at": _iso(open_ms - 58_000),
                "monotonic_ns": 10_000,
            }
            await self.sink.emit(
                {
                    **common,
                    "source": "clob_http",
                    "kind": "snapshot",
                    "event_type": "resnapshot",
                    "event_at": None,
                    "sequence": None,
                    "payload": {"responses": []},
                }
            )
            await self.sink.emit(
                {
                    **common,
                    "schema_version": "edge-lab-recorder.lifecycle.v1",
                    "source": "clob_market_ws",
                    "kind": "lifecycle",
                    "event_type": "resync_complete",
                    "detail": {
                        "watermark_ns": max(asset_watermarks.values()),
                        "asset_watermarks_ns": asset_watermarks,
                    },
                }
            )
            await self._stop.wait()

    _ScopeReadyRecorder.instances.clear()
    source = _CrossingGammaSource()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=0.01,
            max_assets_per_group=2,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_ScopeReadyRecorder,
    )
    monkeypatch.setattr(
        service_module.time,
        "time",
        lambda: clock_ms[0] / 1_000,
    )
    initial = await service.scan_once(now_ms=open_ms - 120_000)
    assert initial["verified_target_count"] == 2
    assert initial["active_worker_count"] == 0
    source.crossing = True
    clock_ms[0] = open_ms - 119_000
    scan_task = asyncio.create_task(service.scan_once())
    try:
        entered = await asyncio.to_thread(second_poll_entered.wait, 1)
        assert entered is True
        assert len(_ScopeReadyRecorder.instances) == 2
        await asyncio.wait_for(
            asyncio.gather(
                *(
                    recorder.sink.ready.wait()
                    for recorder in _ScopeReadyRecorder.instances
                )
            ),
            timeout=2,
        )
        clock_ms[0] = open_ms + 1_000
        release_second_poll.set()

        status = await asyncio.wait_for(scan_task, timeout=2)

        assert status["active_worker_count"] == 2, (
            status,
            [
                recorder.stopped
                for recorder in _ScopeReadyRecorder.instances
            ],
        )
        assert status["target_states"] == {"open": 2}
        assert all(
            recorder.stopped is False
            for recorder in _ScopeReadyRecorder.instances
        )
    finally:
        release_second_poll.set()
        if not scan_task.done():
            scan_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scan_task
        await service.close()


@pytest.mark.asyncio
async def test_finalized_chainlink_pair_and_pong_commit_strict_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    (rtds / "batch.manifest.json").write_text(
        json.dumps({"finalized_at": _iso(OPEN_SECONDS * 1_000 + 1_000)}),
        encoding="utf-8",
    )
    source = _FakeGammaSource(record)

    def fake_boundary_batch(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary]:
        assert manifest_paths == (rtds / "batch.manifest.json",)
        for request in requests.values():
            assert (
                request.rule_url
                == "https://data.chain.link/streams/btc-usd"
            )
            assert request.first_decision_at
        return {
            request_id: _finalized_boundary(
                request.target,
                role=request.boundary_role,
                received_at_ms=(
                    request.target.opens_at_ms + 1_000
                    if request.boundary_role == "open"
                    else request.target.closes_at_ms + 1_000
                ),
            )
            for request_id, request in requests.items()
        }

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        fake_boundary_batch,
        raising=False,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            close_liveness_tolerance_ms=30_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    close_ms = open_ms + 300_000
    await service.scan_once(now_ms=open_ms - 60_000)
    recorder = _FakeRecorder.instances[0]
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    await service.scan_once(now_ms=open_ms + 2_000)
    await recorder.sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms - 5_000,
            monotonic_ns=20_000,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        )
    )
    await recorder.sink.emit(
        _transport_record(
            "heartbeat_ack",
            received_at_ms=close_ms + 5_000,
            monotonic_ns=30_000,
            schema_version="edge-lab-recorder.heartbeat-ack.v1",
        )
    )
    source.market.update(
        {
            "active": False,
            "closed": True,
            "acceptingOrders": False,
            "outcomePrices": ["1", "0"],
            "updatedAt": _iso(close_ms + 1_000),
        }
    )
    source.response_time_seconds = (close_ms + 10_000) / 1_000

    status = await service.scan_once(now_ms=close_ms + 11_000)
    lifecycle = service.supervisor.target_snapshot(
        f"btc-updown-5m-{OPEN_SECONDS}"
    )

    assert status["target_states"] == {"settled": 1}
    assert status["label_states"] == {"strict_settled": 1}
    assert status["label_tracking_complete_count"] == 1
    assert lifecycle.settlement_record_id is not None
    assert lifecycle.exclusion_reason is None
    control_manifests = tuple(
        (tmp_path / "dynamic" / "control" / "raw").rglob(
            "*.manifest.json"
        )
    )
    assert control_manifests
    assert any(
        lifecycle.settlement_record_id
        in manifest.with_name(
            manifest.name.removesuffix(".manifest.json") + ".jsonl"
        ).read_text(encoding="utf-8")
        for manifest in control_manifests
    )
    control_rows = [
        json.loads(line)["payload"]
        for manifest in control_manifests
        for line in manifest.with_name(
            manifest.name.removesuffix(".manifest.json") + ".jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    by_event = {
        row["event_type"]: row["payload"]
        for row in control_rows
        if row["event_type"]
        in {
            "full_window_capture_close_checkpoint",
            "decision_to_trade_l2_closure",
        }
    }
    assert set(by_event) == {
        "full_window_capture_close_checkpoint",
        "decision_to_trade_l2_closure",
    }
    assert by_event[
        "full_window_capture_close_checkpoint"
    ]["finalized_through_ms"] >= close_ms
    assert by_event["decision_to_trade_l2_closure"][
        "closed_through_ms"
    ] == close_ms
    await service.close()


@pytest.mark.asyncio
async def test_open_and_close_boundaries_share_incremental_rtds_scans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    first_manifest = rtds / "batch-1.manifest.json"
    first_manifest.write_text(
        json.dumps({"finalized_at": _iso(OPEN_SECONDS * 1_000 + 1_000)}),
        encoding="utf-8",
    )
    calls: list[tuple[tuple[Path, ...], tuple[str, ...]]] = []

    def fake_batch(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary | None]:
        roles = tuple(
            sorted(request.boundary_role for request in requests.values())
        )
        calls.append((manifest_paths, roles))
        if manifest_paths == (first_manifest,):
            return dict.fromkeys(requests)
        return {
            request_id: _finalized_boundary(
                request.target,
                role=request.boundary_role,
                received_at_ms=(
                    request.target.opens_at_ms + 1_000
                    if request.boundary_role == "open"
                    else request.target.closes_at_ms + 1_000
                ),
            )
            for request_id, request in requests.items()
        }

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        fake_batch,
        raising=False,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=60,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    close_ms = open_ms + 300_000

    await service.scan_once(now_ms=open_ms - 60_000)
    await asyncio.wait_for(
        _FakeRecorder.instances[0].sink.ready.wait(),
        timeout=1,
    )
    second_manifest = rtds / "batch-2.manifest.json"
    second_manifest.write_text(
        json.dumps({"finalized_at": _iso(close_ms + 1_000)}),
        encoding="utf-8",
    )
    await service.scan_once(now_ms=close_ms + 2_000)

    assert calls == [
        (
            (first_manifest,),
            ("close", "open"),
        ),
        (
            (second_manifest,),
            ("close", "open"),
        )
    ]
    await service.close()


@pytest.mark.asyncio
async def test_open_boundary_after_scheduler_cutoff_is_retried_next_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    first_manifest = rtds / "batch-1.manifest.json"
    first_manifest.write_text(
        json.dumps(
            {"finalized_at": _iso(OPEN_SECONDS * 1_000 - 59_000)}
        ),
        encoding="utf-8",
    )
    open_ms = OPEN_SECONDS * 1_000
    source_received_at_ms = open_ms + 1_000
    open_cutoffs: list[str] = []

    def fake_batch(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary | None]:
        result: dict[str, FinalizedChainlinkBoundary | None] = {}
        for request_id, request in requests.items():
            if request.boundary_role != "open":
                result[request_id] = None
                continue
            open_cutoffs.append(request.first_decision_at)
            cutoff = datetime.fromisoformat(
                request.first_decision_at.replace("Z", "+00:00")
            )
            cutoff_ms = int(cutoff.timestamp() * 1_000)
            if manifest_paths == (first_manifest,):
                result[request_id] = None
                continue
            available = source_received_at_ms < cutoff_ms
            result[request_id] = replace(
                _finalized_boundary(
                    request.target,
                    role="open",
                    received_at_ms=source_received_at_ms,
                ),
                retrospective=not available,
                available_at_first_decision=available,
            )
        return result

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        fake_batch,
        raising=False,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=60,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )

    await service.scan_once(now_ms=open_ms - 60_000)
    await asyncio.wait_for(
        _FakeRecorder.instances[0].sink.ready.wait(),
        timeout=1,
    )
    second_manifest = rtds / "batch-2.manifest.json"
    second_manifest.write_text(
        json.dumps({"finalized_at": _iso(source_received_at_ms)}),
        encoding="utf-8",
    )

    await service.scan_once(now_ms=open_ms + 500)

    slug = f"btc-updown-5m-{OPEN_SECONDS}"
    assert "open" not in service._chainlink_boundaries.get(slug, {})

    await service.scan_once(now_ms=open_ms + 2_000)

    boundary = service._chainlink_boundaries[slug]["open"]
    assert boundary.retrospective is False
    assert boundary.available_at_first_decision is True
    assert open_cutoffs == [
        _iso(open_ms - 60_000),
        _iso(open_ms + 500),
        _iso(open_ms + 2_000),
    ]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intervening_mode",
    ["none", "before_input", "after_input"],
)
async def test_open_boundary_and_live_l2_emit_finalized_ghost_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intervening_mode: str,
) -> None:
    _GhostReadyRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    (rtds / "batch.manifest.json").write_text(
        json.dumps({"finalized_at": _iso(OPEN_SECONDS * 1_000 + 1_000)}),
        encoding="utf-8",
    )

    def fake_boundary_batch(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary]:
        del manifest_paths
        return {
            request_id: _finalized_boundary(
                request.target,
                role=request.boundary_role,
                received_at_ms=(
                    request.target.opens_at_ms + 1_000
                    if request.boundary_role == "open"
                    else request.target.closes_at_ms + 1_000
                ),
            )
            for request_id, request in requests.items()
        }

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        fake_boundary_batch,
        raising=False,
    )

    class _GhostTradeSource(_FakeGammaSource):
        def __init__(self, announcement: dict[str, object]) -> None:
            super().__init__(announcement)
            self.trade_calls: list[str] = []

        def data_trades(
            self,
            condition_id: str,
            *,
            taker_only: bool,
            limit: int,
            offset: int = 0,
        ) -> Fetched[tuple[dict[str, object], ...]]:
            assert taker_only is True
            assert limit == 1_000
            assert offset == 0
            self.trade_calls.append(condition_id)
            trade = {
                "conditionId": condition_id,
                "asset": "1" * 76,
                "side": "SELL",
                "size": "10",
                "price": "0.40",
                "timestamp": OPEN_SECONDS + 4,
                "transactionHash": "0x" + "ef" * 32,
                "proxyWallet": "0x" + "12" * 20,
                "outcome": "Up",
                "outcomeIndex": 0,
            }
            body = json.dumps([trade], separators=(",", ":")).encode()
            return Fetched(
                raw=RawResponse(
                    body=body,
                    text=body.decode(),
                    metadata=FetchMetadata(
                        source="data_api_trades",
                        method="GET",
                        url="https://data-api.polymarket.com/trades",
                        request_params={
                            "market": condition_id,
                            "takerOnly": "true",
                            "limit": 1_000,
                            "offset": 0,
                        },
                        status_code=200,
                        requested_at=float(OPEN_SECONDS + 4),
                        received_at=float(OPEN_SECONDS + 4.01),
                        attempt=1,
                        response_headers={
                            "content-type": "application/json"
                        },
                    ),
                ),
                value=(trade,),
            )

    source = _GhostTradeSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=60,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_GhostReadyRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    await service.scan_once(now_ms=open_ms - 60_000)
    recorder = _GhostReadyRecorder.instances[0]
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    await service.scan_once(now_ms=open_ms - 57_000)
    await recorder.emit_decision_state(received_at_ms=open_ms + 1_500)
    price_change = {
        "schema_version": "clob-market-ws.price_change.v1",
        "source": "clob_market_ws",
        "kind": "data",
        "event_type": "price_change",
        "session_id": "session",
        "connection_id": "connection",
        "received_at": _iso(open_ms + 1_700),
        "event_at": _iso(open_ms + 1_700),
        "sequence": None,
        "monotonic_ns": 17_000,
        "payload": {
            "event_type": "price_change",
            "market": "0x" + "ab" * 32,
            "price_changes": [
                {
                    "asset_id": "1" * 76,
                    "price": "0.40",
                    "size": "2",
                    "side": "BUY",
                    "hash": "updated-book-state",
                    "best_bid": "0.40",
                    "best_ask": "0.41",
                }
            ],
            "timestamp": str(open_ms + 1_700),
        },
    }
    if intervening_mode == "before_input":
        await recorder.sink.emit(price_change)
        decision_status = await service.scan_once(
            now_ms=open_ms + 2_000
        )
    elif intervening_mode == "after_input":
        original_emit = (
            recorder.sink.emit_ghost_decision_if_current
        )
        inputs_observed = asyncio.Event()
        allow_emit = asyncio.Event()

        async def delayed_emit(*args: object, **kwargs: object) -> object:
            inputs_observed.set()
            await allow_emit.wait()
            return await original_emit(*args, **kwargs)

        monkeypatch.setattr(
            recorder.sink,
            "emit_ghost_decision_if_current",
            delayed_emit,
        )
        scan_task = asyncio.create_task(
            service.scan_once(now_ms=open_ms + 2_000)
        )
        await asyncio.wait_for(inputs_observed.wait(), timeout=1)
        await recorder.sink.emit(price_change)
        allow_emit.set()
        decision_status = await scan_task
    else:
        decision_status = await service.scan_once(
            now_ms=open_ms + 2_000
        )
    status = await service.scan_once(now_ms=open_ms + 5_000)

    if intervening_mode != "none":
        assert decision_status["ghost_decision_count"] == 0
        assert status["ghost_decision_count"] == 0
        assert status["data_api_trade_snapshot_count"] == 0
        assert source.trade_calls == []
        await service.close()
        assert not tuple(
            (tmp_path / "dynamic" / "groups").rglob(
                "raw/edge_lab_ghost_decision/*.manifest.json"
            )
        )
        return

    assert decision_status["ghost_decision_count"] == 1
    assert status["ghost_decision_count"] == 1
    assert status["data_api_trade_snapshot_count"] == 1
    assert source.trade_calls == ["0x" + "ab" * 32]
    await service.close()
    manifests = tuple(
        (tmp_path / "dynamic" / "groups").rglob(
            "raw/edge_lab_ghost_decision/*.manifest.json"
        )
    )
    assert len(manifests) == 1
    raw_path = manifests[0].with_name(
        manifests[0].name.removesuffix(".manifest.json") + ".jsonl"
    )
    rows = [
        json.loads(line)
        for line in raw_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    decision = rows[0]["payload"]
    payload = decision["payload"]
    assert decision["schema_version"] == "edge-lab.ghost-decision.v1"
    assert payload["side"] == "BUY"
    assert payload["price"] == "0.40"
    assert payload["quantity"] == "1"
    assert payload["visible_queue"] == "3"
    assert payload["tick_size"] == "0.01"
    assert payload["minimum_order_size"] == "1"
    assert payload["split"] == "test"
    assert payload["announcement_record_id"] == record["record_id"]
    assert payload["ptb_source_record_id"] == "a" * 64
    assert payload["submit_latency_ms"] > 0
    assert payload["cancel_latency_ms"] > payload["submit_latency_ms"]
    trade_manifests = tuple(
        (tmp_path / "dynamic" / "groups").rglob(
            "raw/data_api_trades/*.manifest.json"
        )
    )
    assert len(trade_manifests) == 1
    control_rows = [
        json.loads(line)["payload"]
        for manifest in (
            tmp_path / "dynamic" / "control" / "raw"
        ).rglob("*.manifest.json")
        for line in manifest.with_name(
            manifest.name.removesuffix(".manifest.json") + ".jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assumptions = {
        row["event_type"]: row["payload"]
        for row in control_rows
        if row["event_type"]
        in {
            "pessimistic_submit_latency",
            "settlement_operation_cost",
        }
    }
    assert assumptions["pessimistic_submit_latency"][
        "latency_ms"
    ] == 250
    assert assumptions["pessimistic_submit_latency"][
        "decision_record_id"
    ] == rows[0]["record_id"]
    assert assumptions["settlement_operation_cost"]["amount"] == "1"
    assert assumptions["settlement_operation_cost"][
        "currency"
    ] == "USDC"


@pytest.mark.asyncio
async def test_buffered_l2_prefix_prevents_live_ghost_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _BackloggedGhostReadyRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    (rtds / "batch.manifest.json").write_text(
        json.dumps(
            {"finalized_at": _iso(OPEN_SECONDS * 1_000 + 1_000)}
        ),
        encoding="utf-8",
    )

    def fake_boundary_batch(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary]:
        del manifest_paths
        return {
            request_id: _finalized_boundary(
                request.target,
                role=request.boundary_role,
                received_at_ms=(
                    request.target.opens_at_ms + 1_000
                    if request.boundary_role == "open"
                    else request.target.closes_at_ms + 1_000
                ),
            )
            for request_id, request in requests.items()
        }

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        fake_boundary_batch,
        raising=False,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=60,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_BackloggedGhostReadyRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    try:
        await service.scan_once(now_ms=open_ms - 60_000)
        recorder = _BackloggedGhostReadyRecorder.instances[0]
        await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
        await service.scan_once(now_ms=open_ms - 57_000)
        await recorder.emit_decision_state(received_at_ms=open_ms + 1_500)

        status = await service.scan_once(now_ms=open_ms + 2_000)

        assert status["ghost_decision_count"] == 0
    finally:
        await service.close()

    assert not tuple(
        (tmp_path / "dynamic" / "groups").rglob(
            "raw/edge_lab_ghost_decision/*.manifest.json"
        )
    )


@pytest.mark.asyncio
async def test_live_scan_refreshes_clock_before_freezing_ghost_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _GhostReadyRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    (rtds / "batch.manifest.json").write_text(
        json.dumps({"finalized_at": _iso(OPEN_SECONDS * 1_000 + 1_000)}),
        encoding="utf-8",
    )

    def fake_boundary_batch(
        manifest_paths: tuple[Path, ...],
        *,
        requests: dict[str, object],
    ) -> dict[str, FinalizedChainlinkBoundary]:
        del manifest_paths
        return {
            request_id: _finalized_boundary(
                request.target,
                role=request.boundary_role,
                received_at_ms=(
                    request.target.opens_at_ms + 1_000
                    if request.boundary_role == "open"
                    else request.target.closes_at_ms + 1_000
                ),
            )
            for request_id, request in requests.items()
        }

    monkeypatch.setattr(
        service_module,
        "extract_chainlink_boundary_batch_from_finalized_manifests",
        fake_boundary_batch,
        raising=False,
    )
    source = _FakeGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_GhostReadyRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    await service.scan_once(now_ms=open_ms - 60_000)
    recorder = _GhostReadyRecorder.instances[0]
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    await service.scan_once(now_ms=open_ms + 1_500)
    assert service._chainlink_boundaries[
        f"btc-updown-5m-{OPEN_SECONDS}"
    ]["open"].record_id == "a" * 64
    await recorder.emit_decision_state(received_at_ms=open_ms + 2_500)

    original_poll = service._poll_gamma_target
    poll_complete = False
    post_poll_clock_reads = 0

    async def tracked_poll(*args: object, **kwargs: object) -> None:
        nonlocal poll_complete
        await original_poll(*args, **kwargs)
        poll_complete = True

    def live_time() -> float:
        nonlocal post_poll_clock_reads
        if not poll_complete:
            return (open_ms + 2_000) / 1_000
        post_poll_clock_reads += 1
        clock_ms = (
            open_ms + 2_000
            if post_poll_clock_reads == 1
            else open_ms + 3_000
        )
        return clock_ms / 1_000

    monkeypatch.setattr(service, "_poll_gamma_target", tracked_poll)
    monkeypatch.setattr(service_module.time, "time", live_time)

    status = await service.scan_once()

    assert status["ghost_decision_count"] == 1
    await service.close()


@pytest.mark.asyncio
async def test_service_routes_recovery_through_snapshot_and_cpu_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    snapshot_path = tmp_path / "service" / "recovery-snapshot.json"
    recovery = _recovery_result({})
    anchor_payload = {
        "schema_version": (
            "edge-lab-dynamic-short-crypto-recovery-anchor.v1"
        ),
        "opaque": "owned-by-recovery-module",
    }
    calls: list[tuple[tuple[Path, ...], int, Path, float]] = []

    def fake_cached_recover(
        roots: tuple[Path, ...],
        *,
        settlement_timeout_ms: int,
        snapshot_path: Path,
        cpu_ratio: float,
    ) -> SimpleNamespace:
        calls.append(
            (
                roots,
                settlement_timeout_ms,
                snapshot_path,
                cpu_ratio,
            )
        )
        return SimpleNamespace(
            result=recovery,
            anchor_payload=anchor_payload,
        )

    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs_cached",
        fake_cached_recover,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic-run",
            recovery_run_roots=(prior_run,),
            recovery_snapshot_path=snapshot_path,
            recovery_cpu_ratio=0.4,
        ),
        source_client=_FakeGammaSource(_announcement()),
        websocket_factory=object(),
    )

    assert calls == [
        (
            (prior_run.resolve(),),
            3_600_000,
            snapshot_path.resolve(),
            0.4,
        )
    ]
    assert service._recovery_result is recovery
    assert service._recovery_anchor_payload == anchor_payload
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("emit", "checkpoint"))
async def test_recovery_anchor_persistence_failure_blocks_public_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    announcement = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(
            announcement,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    recovery = _recovery_result({})
    anchor_payload = {
        "schema_version": (
            "edge-lab-dynamic-short-crypto-recovery-anchor.v1"
        ),
        "opaque": "owned-by-recovery-module",
    }
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs_cached",
        lambda *args, **kwargs: SimpleNamespace(
            result=recovery,
            anchor_payload=anchor_payload,
        ),
    )

    class _FailingPersistence(_MemoryPersistence):
        async def emit(
            self,
            record: dict[str, object],
        ) -> SimpleNamespace:
            if failure_stage == "emit":
                raise RuntimeError("anchor emit failed")
            return await super().emit(record)

        async def checkpoint(
            self,
            source: str,
            checkpoint: dict[str, object],
        ) -> None:
            if failure_stage == "checkpoint":
                raise RuntimeError("anchor checkpoint failed")
            await super().checkpoint(source, checkpoint)

    source = _FakeGammaSource(announcement)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
            recovery_snapshot_path=(
                tmp_path / "service" / "recovery-snapshot.json"
            ),
        ),
        source_client=source,
        websocket_factory=object(),
    )
    await service._control_sink.close()
    persistence = _FailingPersistence()
    service._control_sink = persistence

    with pytest.raises(RuntimeError, match=f"anchor {failure_stage} failed"):
        await service.scan_once(
            now_ms=OPEN_SECONDS * 1_000 - 600_000
        )

    assert source.calls == []
    assert service._recovery_initialized is False
    assert [
        record["event_type"] for record in persistence.records
    ] == (
        []
        if failure_stage == "emit"
        else ["restart_recovery_decision"]
    )
    if persistence.records:
        assert persistence.records[0]["payload"]["recovery_anchor"] == (
            anchor_payload
        )
    assert persistence.checkpoints == []
    await service.close()


@pytest.mark.parametrize(
    "field",
    (
        "predecessor_commitment_record_id",
        "remediation_reason_code",
        "plan_id",
    ),
)
def test_remediation_plan_requires_native_exact_strings(
    field: str,
) -> None:
    plan = LifecycleRemediationPlan.create("c" * 64)
    document: dict[str, object] = plan.to_document()

    class _StringLike:
        def __init__(self, value: str) -> None:
            self.value = value

        def __str__(self) -> str:
            return self.value

    document[field] = _StringLike(str(document[field]))

    with pytest.raises(ValueError, match="native exact strings"):
        LifecycleRemediationPlan.from_document(document)


@pytest.mark.asyncio
async def test_recovered_chain_tip_emits_one_durable_remediation_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    announcement = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(
            announcement,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    predecessor_record_id = "c" * 64
    plan = LifecycleRemediationPlan.create(predecessor_record_id)
    recovery = _recovery_result(
        _empty_recovered_state(
            lifecycle_cohort_journal=[
                _recovered_root_commitment(predecessor_record_id)
            ]
        )
    )
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: recovery,
    )
    persistence = _MemoryPersistence()

    class _OrderingSource(_FakeGammaSource):
        def gamma_market(
            self,
            market_id: str,
        ) -> Fetched[dict[str, object]]:
            assert [
                record["event_type"]
                for record in persistence.records[:2]
            ] == [
                "restart_recovery_decision",
                "lifecycle_remediation_cohort_committed",
            ]
            assert persistence.checkpoints == [
                "dynamic_short_crypto_service",
                "dynamic_short_crypto_service",
            ]
            return super().gamma_market(market_id)

    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
            lifecycle_remediation_plan=plan,
        ),
        source_client=_OrderingSource(announcement),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = persistence

    await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 600_000
    )

    assert [
        record["event_type"] for record in persistence.records[:2]
    ] == [
        "restart_recovery_decision",
        "lifecycle_remediation_cohort_committed",
    ]
    assert persistence.checkpoints[:2] == [
        "dynamic_short_crypto_service",
        "dynamic_short_crypto_service",
    ]
    remediation = persistence.records[1]["payload"]
    assert remediation["predecessor_commitment_record_id"] == (
        predecessor_record_id
    )
    assert remediation["remediation_plan_id"] == plan.plan_id
    assert remediation["remediation_reason_code"] == (
        "scheduler_starved_by_gamma_sweep"
    )
    assert remediation["actual_fill"] is False
    assert remediation["authenticated_fill"] is False
    assert remediation["orders_submitted"] == 0
    assert remediation["authenticated_endpoints_used"] == 0
    await service.close()


@pytest.mark.asyncio
async def test_recovered_same_remediation_plan_is_not_emitted_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    predecessor_record_id = "c" * 64
    remediation_record_id = "d" * 64
    plan = LifecycleRemediationPlan.create(predecessor_record_id)
    remediation_payload = {
        "scheduler_now_ms": OPEN_SECONDS * 1_000,
        "schema_version": (
            "edge-lab.phase2-lifecycle-remediation-cohort.v1"
        ),
        "selection_rule": (
            "first_mature_targets_opening_at_or_after_boundary"
        ),
        "predecessor_commitment_record_id": predecessor_record_id,
        "remediation_reason_code": plan.remediation_reason_code,
        "remediation_plan_id": plan.plan_id,
        "eligibility_start_ms": OPEN_SECONDS * 1_000 + 600_000,
        "sample_size": 20,
        "threshold": "0.8",
        "settlement_timeout_ms": 3_600_000,
        "assets": ["BTC", "ETH"],
        "horizons": ["5m", "15m"],
        "selection_order": ["opens_at_ms", "closes_at_ms", "slug"],
        "append_only": True,
        "prior_failure_retained": True,
        "must_be_durable_before_public_network": True,
        "actual_fill": False,
        "authenticated_fill": False,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    recovery = _recovery_result(
        _empty_recovered_state(
            lifecycle_cohort_journal=[
                _recovered_root_commitment(predecessor_record_id),
                {
                    "event_type": (
                        "lifecycle_remediation_cohort_committed"
                    ),
                    "record_id": remediation_record_id,
                    "run_id": "prior-run",
                    "payload": remediation_payload,
                },
            ]
        )
    )
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: recovery,
    )
    persistence = _MemoryPersistence()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
            lifecycle_remediation_plan=plan,
        ),
        source_client=object(),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = persistence

    await service.scan_once(now_ms=OPEN_SECONDS * 1_000)

    assert [
        record["event_type"] for record in persistence.records
    ] == ["restart_recovery_decision"]
    assert persistence.checkpoints == ["dynamic_short_crypto_service"]
    await service.close()


@pytest.mark.asyncio
async def test_remediation_plan_mismatch_fails_before_public_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    recovery = _recovery_result(
        _empty_recovered_state(
            lifecycle_cohort_journal=[
                _recovered_root_commitment("c" * 64)
            ]
        )
    )
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: recovery,
    )
    persistence = _MemoryPersistence()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
            lifecycle_remediation_plan=(
                LifecycleRemediationPlan.create("e" * 64)
            ),
        ),
        source_client=object(),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = persistence

    with pytest.raises(DiscoveryInputError) as captured:
        await service.scan_once(now_ms=OPEN_SECONDS * 1_000)

    assert captured.value.code == "lifecycle_remediation_plan_mismatch"
    assert [
        record["event_type"] for record in persistence.records
    ] == ["restart_recovery_decision"]
    assert persistence.checkpoints == ["dynamic_short_crypto_service"]
    await service.close()


@pytest.mark.asyncio
async def test_recovery_decision_is_finalized_before_any_public_network_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    persistence = _MemoryPersistence()

    class _OrderingGammaSource(_FakeGammaSource):
        def gamma_market(
            self,
            market_id: str,
        ) -> Fetched[dict[str, object]]:
            assert [
                record["event_type"]
                for record in persistence.records[:2]
            ] == [
                "restart_recovery_decision",
                "lifecycle_cohort_committed",
            ]
            assert persistence.checkpoints == [
                "dynamic_short_crypto_service",
                "dynamic_short_crypto_service",
            ]
            commitment = persistence.records[1]["payload"]
            assert commitment == {
                "scheduler_now_ms": OPEN_SECONDS * 1_000 - 600_000,
                "schema_version": (
                    "edge-lab.phase2-lifecycle-prospective-cohort.v1"
                ),
                "selection_rule": (
                    "first_mature_targets_opening_at_or_after_boundary"
                ),
                "eligibility_start_ms": OPEN_SECONDS * 1_000,
                "sample_size": 20,
                "threshold": "0.8",
                "settlement_timeout_ms": 3_600_000,
                "assets": ["BTC", "ETH"],
                "horizons": ["5m", "15m"],
                "selection_order": [
                    "opens_at_ms",
                    "closes_at_ms",
                    "slug",
                ],
                "earliest_valid_commitment_wins": True,
                "must_be_durable_before_public_network": True,
                "actual_fill": False,
                "authenticated_fill": False,
                "orders_submitted": 0,
                "authenticated_endpoints_used": 0,
            }
            return super().gamma_market(market_id)

    recovery = SimpleNamespace(
        recovered_from_run_ids=("prior-run",),
        replayed_record_count=7,
        state_hash="a" * 64,
        gaps=(),
        exclusions=(),
        run_classifications=(
            {
                "run_id": "prior-run",
                "classification": "clean_completed",
                "conditions": ["clean_completed"],
            },
        ),
        state={
            "processed_finalized_discovery_descriptors": [],
            "registry": {
                "snapshot_sha256": None,
                "revision_record_ids": [],
            },
            "targets": {},
            "gamma_evidence": {},
            "chainlink_evidence": {},
            "subscription_decisions": {},
            "worker_receipts": {},
            "worker_liveness": {},
        },
        recovery_decision={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-recovery-decision.v1"
            ),
            "decision_id": "b" * 64,
            "state_hash": "a" * 64,
            "must_be_durable_before_worker_actions": True,
        },
        worker_actions=(),
        callback_generation=1,
    )
    calls: list[tuple[tuple[Path, ...], int]] = []

    def fake_recover(
        roots: tuple[Path, ...],
        *,
        settlement_timeout_ms: int,
    ) -> SimpleNamespace:
        calls.append((roots, settlement_timeout_ms))
        return recovery

    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        fake_recover,
        raising=False,
    )
    source = _OrderingGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
        ),
        source_client=source,
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = persistence

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 600_000
    )

    assert calls == [((prior_run.resolve(),), 3_600_000)]
    assert source.calls == ["3081000"]
    assert status["restart_recovery"] == {
        "status": "replayed_finalized_only",
        "recovered_from_run_ids": ["prior-run"],
        "replayed_record_count": 7,
        "state_hash": "a" * 64,
        "gap_count": 0,
        "exclusion_count": 0,
        "callback_generation": 1,
    }
    await service.close()


@pytest.mark.asyncio
async def test_recovery_applies_verified_discovery_before_registry_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    raw_path = _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    registry = build_short_crypto_registry((raw_path,))
    target = registry["targets"][0]
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    recovery = SimpleNamespace(
        recovered_from_run_ids=("prior-run",),
        replayed_record_count=9,
        state_hash="a" * 64,
        gaps=(),
        exclusions=(),
        run_classifications=(
            {
                "run_id": "prior-run",
                "classification": "clean_completed",
                "conditions": ["clean_completed"],
            },
        ),
        state={
            "processed_finalized_discovery_descriptors": registry["inputs"],
            "registry": {
                "snapshot_sha256": registry["snapshot_sha256"],
                "revision_record_ids": ["c" * 64],
            },
            "targets": {
                target["slug"]: {
                    "identity": target,
                    "state": "gamma_verified",
                    "pending_settlement_deadline_ms": (
                        target["closes_at_ms"] + 3_600_000
                    ),
                }
            },
            "gamma_evidence": {},
            "chainlink_evidence": {},
            "subscription_decisions": {},
            "worker_receipts": {},
            "worker_liveness": {},
        },
        recovery_decision={
            "schema_version": (
                "edge-lab-dynamic-short-crypto-recovery-decision.v1"
            ),
            "decision_id": "b" * 64,
            "state_hash": "a" * 64,
            "must_be_durable_before_worker_actions": True,
        },
        worker_actions=(),
        callback_generation=1,
    )

    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: recovery,
    )

    def unexpected_registry_rebuild(
        paths: tuple[Path, ...],
    ) -> dict[str, object]:
        raise AssertionError(
            f"recovered finalized discovery was rebuilt: {paths}"
        )

    monkeypatch.setattr(
        service_module,
        "build_short_crypto_registry",
        unexpected_registry_rebuild,
    )
    source = _FakeGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
        ),
        source_client=source,
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = _MemoryPersistence()

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 600_000
    )

    assert status["processed_discovery_file_count"] == 1
    assert status["target_count"] == 1
    assert status["verified_target_count"] == 1
    assert source.calls == ["3081000"]
    await service.close()


@pytest.mark.asyncio
async def test_recovery_restores_finalized_exclusion_without_network_or_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    raw_path = _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    registry = build_short_crypto_registry((raw_path,))
    target = registry["targets"][0]
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    state = {
        "processed_finalized_discovery_descriptors": registry["inputs"],
        "registry": {
            "snapshot_sha256": registry["snapshot_sha256"],
            "revision_record_ids": ["c" * 64],
            "snapshot": registry,
        },
        "targets": {
            target["slug"]: {
                "identity": target,
                "state": "excluded",
                "pending_settlement_deadline_ms": (
                    target["closes_at_ms"] + 3_600_000
                ),
                "exclusion_reason": "capture_stopped_before_open",
                "exclusion_record_id": "d" * 64,
                "exclusion_observed_at_ms": (
                    target["opens_at_ms"] - 600_000
                ),
            }
        },
        "gamma_evidence": {},
        "latest_gamma_records": {},
        "chainlink_evidence": {},
        "subscription_decisions": {},
        "worker_receipts": {},
        "worker_liveness": {},
    }
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: _recovery_result(state),
    )

    class _NoNetwork:
        def gamma_market(self, market_id: str) -> object:
            raise AssertionError(f"unexpected Gamma request: {market_id}")

    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
        ),
        source_client=_NoNetwork(),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = _MemoryPersistence()

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 500_000
    )
    lifecycle = service.supervisor.target_snapshot(target["slug"])

    assert lifecycle.state.value == "excluded"
    assert lifecycle.exclusion_reason == "capture_stopped_before_open"
    assert status["target_states"] == {"excluded": 1}
    assert status["active_worker_count"] == 0
    assert status["recovered_state_application"] == {
        "processed_discovery_descriptor_count": 1,
        "target_state_count": 1,
        "settlement_deadline_count": 1,
        "worker_action_target_count": 0,
        "worker_liveness_group_count": 0,
    }
    await service.close()


@pytest.mark.asyncio
async def test_recovery_rehydrates_committed_chainlink_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    announcement = _announcement()
    raw_path = _finalize(
        discovery / "batch.jsonl",
        json.dumps(
            announcement,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    registry = build_short_crypto_registry((raw_path,))
    target_payload = registry["targets"][0]
    target = service_module.ShortCryptoTarget(
        **{
            key: value
            for key, value in target_payload.items()
            if key != "provenance"
        }
    )
    boundary_received_at_ms = target.opens_at_ms + 1_000
    boundary = _finalized_boundary(
        target,
        role="open",
        received_at_ms=boundary_received_at_ms,
    )
    event = {
        "schema_version": (
            "edge-lab-dynamic-short-crypto-service.record.v1"
        ),
        "source": "rtds_ws",
        "kind": "data",
        "event_type": "chainlink_boundary_evidence",
        "session_id": "prior-session",
        "connection_id": None,
        "received_at": _iso(boundary_received_at_ms),
        "event_at": None,
        "sequence": None,
        "monotonic_ns": 1,
        "payload": DynamicShortCryptoService._boundary_payload(
            target,
            boundary,
        ),
    }
    evidence_store = CaptureStore(tmp_path / "chainlink-evidence")
    writer = evidence_store.open_raw_batch(
        source="rtds_ws",
        batch_id="rtds-a",
        schema_version="edge-lab-recorder.raw.v1",
    )
    boundary_result = writer.append(
        received_at=_iso(boundary_received_at_ms),
        event_at=None,
        sequence=0,
        payload=event,
    )
    manifest = writer.finalize(
        finalized_at=_iso(boundary_received_at_ms)
    )
    boundary_record = json.loads(
        (evidence_store.root / manifest["raw_path"])
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    state = {
        "processed_finalized_discovery_descriptors": registry["inputs"],
        "registry": {
            "snapshot_sha256": registry["snapshot_sha256"],
            "revision_record_ids": ["c" * 64],
            "snapshot": registry,
        },
        "targets": {
            target.slug: {
                "identity": target_payload,
                "state": "excluded",
                "pending_settlement_deadline_ms": (
                    target.closes_at_ms + 3_600_000
                ),
                "exclusion_reason": "capture_interrupted_during_market",
                "exclusion_record_id": "d" * 64,
                "exclusion_observed_at_ms": target.opens_at_ms + 500,
            }
        },
        "gamma_evidence": {},
        "latest_gamma_records": {},
        "chainlink_evidence": {
            target.slug: {
                "open": [
                    {
                        "control_record_id": "e" * 64,
                        "evidence_record_id": boundary_result.record_id,
                        "evidence_record": boundary_record,
                    }
                ],
                "close": [],
            }
        },
        "subscription_decisions": {},
        "worker_receipts": {},
        "worker_liveness": {},
    }
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: _recovery_result(state),
    )

    class _NoNetwork:
        def gamma_market(self, market_id: str) -> object:
            raise AssertionError(f"unexpected Gamma request: {market_id}")

    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
        ),
        source_client=_NoNetwork(),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = _MemoryPersistence()

    await service.scan_once(now_ms=target.opens_at_ms + 2_000)

    assert service._chainlink_boundaries[target.slug]["open"] == boundary
    assert service._chainlink_commit_record_ids[target.slug]["open"] == (
        boundary_result.record_id
    )
    await service.close()


@pytest.mark.asyncio
async def test_recovery_rehydrates_finalized_gamma_without_refetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    announcement = _announcement()
    raw_path = _finalize(
        discovery / "batch.jsonl",
        json.dumps(
            announcement,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    registry = build_short_crypto_registry((raw_path,))
    target_payload = registry["targets"][0]
    target = service_module.ShortCryptoTarget(
        **{
            key: value
            for key, value in target_payload.items()
            if key != "provenance"
        }
    )
    gamma_source = _FakeGammaSource(announcement)
    gamma_source.response_time_seconds = float(OPEN_SECONDS - 700)
    fetched = gamma_source.gamma_market(target.market_id)
    event, received_at = DynamicShortCryptoService._gamma_public_response(
        target,
        fetched,
    )
    event["session_id"] = "prior-session"
    evidence_store = CaptureStore(tmp_path / "gamma-evidence")
    writer = evidence_store.open_raw_batch(
        source="gamma_http",
        batch_id="gamma-a",
        schema_version="edge-lab-recorder.raw.v1",
    )
    gamma_result = writer.append(
        received_at=received_at,
        event_at=None,
        sequence=0,
        payload=event,
    )
    manifest = writer.finalize(finalized_at=received_at)
    gamma_record = json.loads(
        (evidence_store.root / manifest["raw_path"])
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    state = {
        "processed_finalized_discovery_descriptors": registry["inputs"],
        "registry": {
            "snapshot_sha256": registry["snapshot_sha256"],
            "revision_record_ids": ["c" * 64],
            "snapshot": registry,
        },
        "targets": {
            target.slug: {
                "identity": target_payload,
                "state": "gamma_verified",
                "pending_settlement_deadline_ms": (
                    target.closes_at_ms + 3_600_000
                ),
            }
        },
        "gamma_evidence": {
            target.slug: [
                {
                    "control_record_id": "d" * 64,
                    "gamma_record_id": gamma_result.record_id,
                    "market_id": target.market_id,
                    "status": "verified",
                }
            ]
        },
        "latest_gamma_records": {target.slug: gamma_record},
        "chainlink_evidence": {},
        "subscription_decisions": {},
        "worker_receipts": {},
        "worker_liveness": {},
    }
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: _recovery_result(state),
    )

    class _NoNetwork:
        def gamma_market(self, market_id: str) -> object:
            raise AssertionError(f"unexpected Gamma request: {market_id}")

    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
        ),
        source_client=_NoNetwork(),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = _MemoryPersistence()

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 600_000
    )
    repeated = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 599_000
    )

    assert status["verified_target_count"] == 1
    assert repeated["verified_target_count"] == 1
    assert status["target_states"] == {"announced": 1}
    assert service._gamma_records[target.slug][0]["record_id"] == (
        gamma_result.record_id
    )
    assert [
        record["event_type"]
        for record in service._control_sink.records
    ].count("restart_recovery_decision") == 1
    await service.close()


@pytest.mark.asyncio
async def test_recovery_worker_action_revalidates_then_uses_next_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    announcement = _announcement()
    raw_path = _finalize(
        discovery / "batch.jsonl",
        json.dumps(
            announcement,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    registry = build_short_crypto_registry((raw_path,))
    target = registry["targets"][0]
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    state = {
        "processed_finalized_discovery_descriptors": registry["inputs"],
        "registry": {
            "snapshot_sha256": registry["snapshot_sha256"],
            "revision_record_ids": ["c" * 64],
            "snapshot": registry,
        },
        "targets": {
            target["slug"]: {
                "identity": target,
                "state": "gamma_verified",
                "pending_settlement_deadline_ms": (
                    target["closes_at_ms"] + 3_600_000
                ),
            }
        },
        "gamma_evidence": {},
        "latest_gamma_records": {},
        "chainlink_evidence": {},
        "subscription_decisions": {},
        "worker_receipts": {},
        "worker_liveness": {},
    }
    action = {
        "action": "start_public_worker_after_revalidation",
        "reason": "decision_without_receipt",
        "decision_id": "d" * 64,
        "group_id": "short-crypto-" + "1" * 24,
        "asset_ids": [target["up_token_id"], target["down_token_id"]],
        "target_slugs": [target["slug"]],
        "generation": 8,
        "blocked_until_recovery_decision_id": "b" * 64,
    }
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: _recovery_result(
            state,
            worker_actions=(action,),
            callback_generation=8,
        ),
    )
    persistence = _MemoryPersistence()

    class _OrderingSource(_FakeGammaSource):
        def gamma_market(
            self,
            market_id: str,
        ) -> Fetched[dict[str, object]]:
            assert [
                record["event_type"]
                for record in persistence.records[:2]
            ] == [
                "restart_recovery_decision",
                "lifecycle_cohort_committed",
            ]
            assert persistence.checkpoints == [
                "dynamic_short_crypto_service",
                "dynamic_short_crypto_service"
            ]
            return super().gamma_market(market_id)

    source = _OrderingSource(announcement)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
            subscribe_lead_ms=60_000,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service._control_sink.close()
    service._control_sink = persistence

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 60_000
    )
    capture_decision = next(
        record
        for record in persistence.records
        if record["event_type"] == "capture_decision"
    )

    assert source.calls == [target["market_id"]]
    assert capture_decision["payload"]["sequence"] == 8
    assert status["active_worker_count"] == 1
    assert status["recovered_state_application"][
        "worker_action_target_count"
    ] == 1
    await service.close()


@pytest.mark.asyncio
async def test_recovery_rejects_discovery_bytes_changed_since_finalized_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    raw_path = _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    registry = build_short_crypto_registry((raw_path,))
    prior_run = tmp_path / "prior-run"
    prior_run.mkdir()
    state = {
        "processed_finalized_discovery_descriptors": registry["inputs"],
        "registry": {
            "snapshot_sha256": registry["snapshot_sha256"],
            "revision_record_ids": ["c" * 64],
            "snapshot": registry,
        },
        "targets": {},
        "gamma_evidence": {},
        "latest_gamma_records": {},
        "chainlink_evidence": {},
        "subscription_decisions": {},
        "worker_receipts": {},
        "worker_liveness": {},
    }
    _finalize(
        raw_path,
        json.dumps(record, separators=(", ", ": "), sort_keys=True) + "\n",
    )
    monkeypatch.setattr(
        service_module,
        "recover_dynamic_short_crypto_runs",
        lambda *args, **kwargs: _recovery_result(state),
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            recovery_run_roots=(prior_run,),
        ),
        source_client=object(),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = _MemoryPersistence()

    with pytest.raises(DiscoveryInputError) as caught:
        await service.scan_once(
            now_ms=OPEN_SECONDS * 1_000 - 600_000
        )

    assert caught.value.code == "recovered_discovery_descriptor_mismatch"
    assert service.snapshot()["processed_discovery_file_count"] == 0
    await service.close()


@pytest.mark.parametrize(
    ("with_liveness", "expected_reason"),
    [
        (False, "clob_close_liveness_timeout"),
        (
            True,
            "chainlink_boundary_evidence_missing_after_timeout",
        ),
    ],
)
@pytest.mark.asyncio
async def test_gamma_only_winner_never_promotes_without_capture_gates(
    tmp_path: Path,
    with_liveness: bool,
    expected_reason: str,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _FakeGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            close_liveness_tolerance_ms=30_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    close_ms = open_ms + 300_000
    await service.scan_once(now_ms=open_ms - 60_000)
    recorder = _FakeRecorder.instances[0]
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    await service.scan_once(now_ms=open_ms - 57_000)
    if with_liveness:
        await recorder.sink.emit(
            _transport_record(
                "heartbeat_ack",
                received_at_ms=close_ms - 5_000,
                monotonic_ns=20_000,
                schema_version="edge-lab-recorder.heartbeat-ack.v1",
            )
        )
        await recorder.sink.emit(
            _transport_record(
                "heartbeat_ack",
                received_at_ms=close_ms + 5_000,
                monotonic_ns=30_000,
                schema_version="edge-lab-recorder.heartbeat-ack.v1",
            )
        )
    source.market.update(
        {
            "active": False,
            "closed": True,
            "acceptingOrders": False,
            "outcomePrices": ["1", "0"],
            "updatedAt": _iso(close_ms + 1_000),
        }
    )
    source.response_time_seconds = (close_ms + 10_000) / 1_000

    interim = await service.scan_once(now_ms=close_ms + 10_000)

    assert interim["target_states"] == {"closed": 1}
    assert interim["label_states"] == {
        "gamma_resolved_chainlink_pending": 1
    }
    assert interim["label_tracking_complete_count"] == 0

    terminal = await service.scan_once(now_ms=close_ms + 60_001)
    lifecycle = service.supervisor.target_snapshot(
        f"btc-updown-5m-{OPEN_SECONDS}"
    )

    assert terminal["target_states"] == {"excluded": 1}
    assert terminal["label_tracking_complete_count"] == 1
    assert lifecycle.exclusion_reason == expected_reason
    assert lifecycle.state.value != "settled"
    assert len(source.calls) >= 4
    await service.close()


@pytest.mark.asyncio
async def test_terminal_target_is_not_reopened_by_later_chainlink_backfill(
    tmp_path: Path,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    rtds = tmp_path / "rtds_ws"
    rtds.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _FakeGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            rtds_manifest_dir=rtds,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            close_liveness_tolerance_ms=30_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    close_ms = open_ms + 300_000
    try:
        await service.scan_once(now_ms=open_ms - 60_000)
        recorder = _FakeRecorder.instances[0]
        await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
        await service.scan_once(now_ms=open_ms - 57_000)
        source.market.update(
            {
                "active": False,
                "closed": True,
                "acceptingOrders": False,
                "outcomePrices": ["1", "0"],
                "updatedAt": _iso(close_ms + 1_000),
            }
        )
        source.response_time_seconds = (close_ms + 10_000) / 1_000
        terminal = await service.scan_once(now_ms=close_ms + 60_001)
        assert terminal["target_states"] == {"excluded": 1}
        assert terminal["label_tracking_complete_count"] == 1

        (rtds / "unrelated.manifest.json").write_text(
            "{}\n",
            encoding="utf-8",
        )
        repeated = await service.scan_once(now_ms=close_ms + 60_002)
    finally:
        await service.close()

    assert repeated["target_states"] == {"excluded": 1}
    assert repeated["label_tracking_complete_count"] == 1


class _FakeRecorder:
    instances: list["_FakeRecorder"] = []

    def __init__(
        self,
        *,
        config: object,
        sink: object,
        snapshot_client: object,
        websocket_factory: object,
    ) -> None:
        self.config = config
        self.sink = sink
        self.snapshot_client = snapshot_client
        self.websocket_factory = websocket_factory
        self.stopped = False
        self._stop = asyncio.Event()
        self.__class__.instances.append(self)

    async def run(self) -> None:
        ready_ms = OPEN_SECONDS * 1_000 - 58_000
        common = {
            "schema_version": "edge-lab-recorder.record.v1",
            "session_id": "session",
            "connection_id": "connection",
            "received_at": _iso(ready_ms),
            "monotonic_ns": 10_000,
        }
        await self.sink.emit(
            {
                **common,
                "source": "clob_http",
                "kind": "snapshot",
                "event_type": "resnapshot",
                "event_at": None,
                "sequence": None,
                "payload": {"responses": []},
            }
        )
        await self.sink.emit(
            {
                **common,
                "schema_version": "edge-lab-recorder.lifecycle.v1",
                "source": "clob_market_ws",
                "kind": "lifecycle",
                "event_type": "resync_complete",
                "detail": {
                    "watermark_ns": 9_000,
                    "asset_watermarks_ns": {
                        "1" * 76: 8_000,
                        "2" * 76: 9_000,
                    },
                },
            }
        )
        await self._stop.wait()

    async def stop(self) -> None:
        self.stopped = True
        self._stop.set()


class _GhostReadyRecorder(_FakeRecorder):
    async def run(self) -> None:
        ready_ms = OPEN_SECONDS * 1_000 - 58_000
        await self.sink.emit(
            _transport_record(
                "resync_complete",
                received_at_ms=ready_ms,
                monotonic_ns=10_000,
            )
        )
        await self._stop.wait()

    async def run_at_clob_capture_frontier(
        self,
        action: object,
    ) -> object:
        return await action()

    async def emit_decision_state(self, *, received_at_ms: int) -> None:
        condition_id = "0x" + "ab" * 32
        up_token_id = "1" * 76
        down_token_id = "2" * 76
        await self.sink.emit(
            {
                "schema_version": "clob-http.snapshot.v1",
                "source": "clob_http",
                "kind": "snapshot",
                "event_type": "clob_snapshot",
                "session_id": "session",
                "connection_id": None,
                "received_at": _iso(received_at_ms),
                "event_at": None,
                "sequence": None,
                "monotonic_ns": 15_000,
                "payload": {
                    "schema_version": "edge-lab-public-snapshot.v1",
                    "snapshot_kind": "clob",
                    "responses": [
                        {
                            "resource": "clob_market",
                            "request_key": condition_id,
                            "raw_json": {
                                "c": condition_id,
                                "fd": {"r": "0.02", "e": 1, "to": True},
                                "mts": "0.01",
                                "mos": 1,
                                "t": [
                                    {"o": "Up", "t": up_token_id},
                                    {"o": "Down", "t": down_token_id},
                                ],
                            },
                            "provenance": {"status_code": 200},
                        }
                    ],
                    "truncated_resources": [],
                },
            }
        )
        await self.sink.emit(
            {
                "schema_version": "clob-market-ws.book.v1",
                "source": "clob_market_ws",
                "kind": "data",
                "event_type": "book",
                "session_id": "session",
                "connection_id": "connection",
                "received_at": _iso(received_at_ms + 100),
                "event_at": _iso(received_at_ms + 100),
                "sequence": None,
                "monotonic_ns": 16_000,
                "server_hash": "book-state",
                "payload": {
                    "event_type": "book",
                    "market": condition_id,
                    "asset_id": up_token_id,
                    "bids": [{"price": "0.40", "size": "3"}],
                    "asks": [{"price": "0.41", "size": "4"}],
                    "hash": "book-state",
                    "timestamp": str(received_at_ms + 100),
                },
            }
        )


class _BackloggedGhostReadyRecorder(_GhostReadyRecorder):
    async def run_at_clob_capture_frontier(
        self,
        action: object,
    ) -> object:
        open_ms = OPEN_SECONDS * 1_000
        await self.sink.emit(
            {
                "schema_version": "clob-market-ws.price_change.v1",
                "source": "clob_market_ws",
                "kind": "data",
                "event_type": "price_change",
                "session_id": "session",
                "connection_id": "connection",
                "received_at": _iso(open_ms + 1_900),
                "event_at": _iso(open_ms + 1_900),
                "sequence": None,
                "monotonic_ns": 17_000,
                "payload": {
                    "event_type": "price_change",
                    "market": "0x" + "ab" * 32,
                    "price_changes": [
                        {
                            "asset_id": "1" * 76,
                            "price": "0.40",
                            "size": "2",
                            "side": "BUY",
                            "hash": "buffered-book-state",
                            "best_bid": "0.40",
                            "best_ask": "0.41",
                        }
                    ],
                    "timestamp": str(open_ms + 1_900),
                },
            }
        )
        return await action()


@pytest.mark.asyncio
async def test_stopped_initial_ingest_retries_unconsumed_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    first = _announcement()
    second = json.loads(json.dumps(first))
    second_event = second["payload"]["payload"]
    second_event["id"] = "3081001"
    second_event["slug"] = f"eth-updown-5m-{OPEN_SECONDS}"
    second_event["condition_id"] = "0x" + "cd" * 32
    second_event["market"] = second_event["condition_id"]
    second_event["assets_ids"] = ["3" * 76, "4" * 76]
    second_event["clob_token_ids"] = ["3" * 76, "4" * 76]
    second_event["event_message"]["slug"] = second_event["slug"]
    second_event["description"] = (
        "The resolution source is Chainlink, specifically the ETH/USD "
        "data stream at https://data.chain.link/streams/eth-usd."
    )
    second["record_id"] = canonical_record_id(second)
    _finalize(
        discovery / "batch.jsonl",
        "".join(
            json.dumps(item, separators=(",", ":"), sort_keys=True) + "\n"
            for item in (first, second)
        ),
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
        ),
        source_client=object(),
        websocket_factory=object(),
    )
    stop_event = asyncio.Event()
    polled: list[str] = []

    async def fake_poll(
        target: object,
        *,
        now_ms: int,
        initial: bool = False,
    ) -> None:
        del now_ms
        if not initial:
            return
        polled.append(target.slug)
        if len(polled) == 1:
            stop_event.set()

    monkeypatch.setattr(service, "_poll_gamma_target", fake_poll)

    first_status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 120_000,
        stop_event=stop_event,
    )
    stop_event.clear()
    second_status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 120_000,
        stop_event=stop_event,
    )

    assert first_status["target_count"] == 1
    assert first_status["processed_discovery_file_count"] == 0
    assert second_status["target_count"] == 2
    assert second_status["processed_discovery_file_count"] == 1
    assert polled == [
        f"btc-updown-5m-{OPEN_SECONDS}",
        f"eth-updown-5m-{OPEN_SECONDS}",
    ]
    await service.close()


@pytest.mark.asyncio
async def test_registry_revision_is_cumulative_when_earlier_file_arrives_late(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    later = _announcement()
    earlier = json.loads(json.dumps(later))
    later["received_at"] = "2026-07-24T13:21:00.000000Z"
    later["payload"]["received_at"] = later["received_at"]
    later["record_id"] = canonical_record_id(later)
    earlier["received_at"] = "2026-07-24T13:20:00.000000Z"
    earlier["payload"]["received_at"] = earlier["received_at"]
    earlier["record_id"] = canonical_record_id(earlier)
    z_path = _finalize(
        discovery / "z.jsonl",
        json.dumps(later, separators=(",", ":"), sort_keys=True) + "\n",
    )
    registry_build_calls: list[tuple[Path, ...]] = []

    def recording_builder(paths: tuple[Path, ...]) -> dict[str, object]:
        registry_build_calls.append(paths)
        return build_short_crypto_registry(paths)

    monkeypatch.setattr(
        service_module,
        "build_short_crypto_registry",
        recording_builder,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
        ),
        source_client=_FakeGammaSource(later),
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    now_ms = OPEN_SECONDS * 1_000 - 600_000
    first = await service.scan_once(now_ms=now_ms)
    a_path = _finalize(
        discovery / "a.jsonl",
        json.dumps(earlier, separators=(",", ":"), sort_keys=True) + "\n",
    )

    second = await service.scan_once(now_ms=now_ms + 1_000)
    expected = build_short_crypto_registry((a_path, z_path))

    assert first["processed_discovery_file_count"] == 1
    assert second["processed_discovery_file_count"] == 2
    assert second["target_count"] == 1
    assert second["registry_snapshot_sha256"] == expected["snapshot_sha256"]
    assert expected["targets"][0]["announcement_record_id"] == earlier[
        "record_id"
    ]
    assert registry_build_calls == [
        (z_path.resolve(),),
        (a_path.resolve(),),
    ]
    await service.close()


@pytest.mark.asyncio
async def test_registry_uses_manifest_schema_to_avoid_unrelated_payload_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    unrelated = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "clob_market_ws",
        "received_at": "2026-07-24T13:19:00.000000Z",
        "event_at": None,
        "sequence": None,
        "payload": {
            "schema_version": "clob-market-ws.price_change.v1",
            "source": "clob_market_ws",
            "event_type": "price_change",
            "payload": {
                "event_type": "price_change",
                "market": "0x" + "ef" * 32,
            },
        },
    }
    unrelated["record_id"] = canonical_record_id(unrelated)
    target = _announcement()
    unrelated_path = _finalize(
        discovery / "a.jsonl",
        json.dumps(
            unrelated,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
    )
    target_path = _finalize(
        discovery / "b.jsonl",
        json.dumps(target, separators=(",", ":"), sort_keys=True) + "\n",
    )
    unrelated_manifest_path = unrelated_path.with_suffix(".manifest.json")
    unrelated_manifest = json.loads(
        unrelated_manifest_path.read_text(encoding="utf-8")
    )
    unrelated_manifest["schema_fingerprints"] = [
        {
            "count": 1,
            "descriptor": {
                "object": {
                    "payload": {
                        "object": {
                            "event_type": "string",
                            "market": "string",
                        }
                    }
                }
            },
            "fingerprint": "a" * 64,
        }
    ]
    unrelated_manifest_path.write_text(
        json.dumps(unrelated_manifest),
        encoding="utf-8",
    )
    target_manifest_path = target_path.with_suffix(".manifest.json")
    target_manifest = json.loads(
        target_manifest_path.read_text(encoding="utf-8")
    )
    target_manifest["schema_fingerprints"] = [
        {
            "count": 1,
            "descriptor": {
                "object": {
                    "payload": {
                        "object": {
                            "clob_token_ids": {"array": ["string"]},
                            "condition_id": "string",
                            "outcomes": {"array": ["string"]},
                            "slug": "string",
                        }
                    }
                }
            },
            "fingerprint": "b" * 64,
        }
    ]
    target_manifest_path.write_text(
        json.dumps(target_manifest),
        encoding="utf-8",
    )
    registry_build_calls: list[tuple[Path, ...]] = []

    def recording_builder(paths: tuple[Path, ...]) -> dict[str, object]:
        registry_build_calls.append(paths)
        return build_short_crypto_registry(paths)

    monkeypatch.setattr(
        service_module,
        "build_short_crypto_registry",
        recording_builder,
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
        ),
        source_client=_FakeGammaSource(target),
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 600_000
    )
    expected = build_short_crypto_registry(
        (unrelated_path, target_path)
    )

    assert registry_build_calls == [(target_path.resolve(),)]
    assert status["processed_discovery_file_count"] == 2
    assert status["registry_snapshot_sha256"] == expected["snapshot_sha256"]
    await service.close()


@pytest.mark.asyncio
async def test_registry_journal_persists_delta_with_cumulative_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    first_path = _finalize(
        discovery / "a.jsonl",
        json.dumps({"unrelated": 1}, sort_keys=True) + "\n",
    )
    persistence = _MemoryPersistence()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
        ),
        source_client=object(),
        websocket_factory=object(),
    )
    await service._control_sink.close()
    service._control_sink = persistence
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 600_000)
    second_path = _finalize(
        discovery / "b.jsonl",
        json.dumps({"unrelated": 2}, sort_keys=True) + "\n",
    )

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 599_000
    )
    revisions = [
        record["payload"]
        for record in persistence.records
        if record["event_type"] == "registry_revision"
    ]
    second = revisions[1]

    assert second["schema_version"] == (
        "edge-lab-short-crypto-registry-revision.v2"
    )
    assert second["delta_snapshot"]["inputs"] == [
        {
            "path": str(second_path.resolve()),
            "sha256": hashlib.sha256(
                second_path.read_bytes()
            ).hexdigest(),
            "byte_count": len(second_path.read_bytes()),
            "line_count": 1,
        }
    ]
    assert second["cumulative_snapshot_sha256"] == (
        status["registry_snapshot_sha256"]
    )
    assert second["cumulative_input_file_count"] == 2
    assert second["cumulative_target_count"] == 0
    assert "inputs" not in second
    assert "targets" not in second
    assert status["processed_discovery_file_count"] == 2
    assert first_path.resolve() in service._processed_paths
    await service.close()


def test_service_rejects_dirty_reused_output_root(tmp_path: Path) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    output = tmp_path / "dynamic"
    orphan = (
        output
        / "control"
        / "raw"
        / "dynamic_short_crypto_service"
        / "orphan.jsonl.partial"
    )
    orphan.parent.mkdir(parents=True)
    orphan.write_text("{}\n", encoding="utf-8")

    with pytest.raises(DiscoveryInputError) as caught:
        DynamicShortCryptoService(
            DynamicShortCryptoServiceConfig(
                discovery_dir=discovery,
                output_root=output,
            ),
            source_client=object(),
            websocket_factory=object(),
        )

    assert caught.value.code == "output_integrity_failed"


@pytest.mark.asyncio
async def test_registry_discovery_scan_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
        ),
        source_client=object(),
        websocket_factory=object(),
    )
    event_loop_thread_id = threading.get_ident()
    scan_thread_ids: list[int] = []

    def record_scan_thread() -> tuple[object, ...]:
        scan_thread_ids.append(threading.get_ident())
        return ()

    monkeypatch.setattr(service, "_new_finalized_paths", record_scan_thread)

    await service._ingest_registry(now_ms=1)

    assert scan_thread_ids
    assert scan_thread_ids[0] != event_loop_thread_id
    await service.close()


@pytest.mark.asyncio
async def test_scan_and_single_flight_close_are_mutually_exclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
        ),
        source_client=object(),
        websocket_factory=object(),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_ingest(**kwargs: object) -> None:
        del kwargs
        entered.set()
        await release.wait()

    monkeypatch.setattr(service, "_ingest_registry", blocked_ingest)
    scan_task = asyncio.create_task(service.scan_once(now_ms=1))
    await asyncio.wait_for(entered.wait(), timeout=1)
    first_close = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    second_close = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not second_close.done()
    first_close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_close
    release.set()

    assert (await scan_task)["target_count"] == 0
    await second_close
    with pytest.raises(RuntimeError, match="closing or closed"):
        await service.scan_once(now_ms=2)


@pytest.mark.asyncio
async def test_due_group_uses_exact_read_only_scope_and_snapshot_gate(
    tmp_path: Path,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    raw = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    _finalize(discovery / "batch.jsonl", raw)
    source = _FakeGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
            max_records_per_batch=257,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )

    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)
    await asyncio.wait_for(
        _FakeRecorder.instances[0].sink.ready.wait(),
        timeout=1,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 57_000)
    status = service.snapshot()

    assert status["target_count"] == 1
    assert status["active_worker_count"] == 1
    assert status["target_states"] == {"subscribed": 1}
    recorder = _FakeRecorder.instances[0]
    assert recorder.config.clob_asset_ids == ("1" * 76, "2" * 76)
    assert recorder.config.rtds_enabled is False
    assert recorder.config.rtds_subscriptions == ()
    assert recorder.config.initial_clob_resnapshot is True
    assert recorder.config.checkpoint_every_records == 257
    assert recorder.config.sink_timeout_seconds == 60.0
    assert recorder.config.checkpoint_timeout_seconds == 60.0
    assert recorder.snapshot_client.condition_ids == ("0x" + "ab" * 32,)
    assert source.calls == ["3081000", "3081000"]

    await service.close()

    assert recorder.stopped
    assert service.snapshot()["target_states"] == {"excluded": 1}
    assert list((tmp_path / "dynamic" / "groups").glob("*/raw/**/*.jsonl"))


@pytest.mark.asyncio
async def test_subscribe_decision_is_finalized_before_recorder_construction(
    tmp_path: Path,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    output = tmp_path / "dynamic"

    def recorder_factory(**kwargs: object) -> _FakeRecorder:
        rows = [
            json.loads(line)
            for path in (output / "control" / "raw").rglob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        decisions = [
            row["payload"]
            for row in rows
            if row["payload"].get("event_type") == "capture_decision"
        ]
        assert len(decisions) == 1
        decision = decisions[0]["payload"]
        assert decision["action"] == "subscribe"
        assert decision["sequence"] == 1
        assert decision["reason"] == "capture_window_due"
        assert decision["targets"][0]["announcement_record_id"] == record[
            "record_id"
        ]
        return _FakeRecorder(**kwargs)

    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=output,
            subscribe_lead_ms=60_000,
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=recorder_factory,
    )

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 60_000
    )

    assert status["active_worker_count"] == 1
    await service.close()


class _TransientGammaSource(_FakeGammaSource):
    def gamma_market(self, market_id: str) -> Fetched[dict[str, object]]:
        if not self.calls:
            self.calls.append(market_id)
            raise PublicSourceError(
                "temporary public source failure",
                code="proxy_unavailable",
                error_type="ProxyError",
            )
        return super().gamma_market(market_id)


@pytest.mark.asyncio
async def test_transient_gamma_failure_is_retried_not_rejected(
    tmp_path: Path,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _TransientGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )

    first = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 60_000
    )
    second = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 59_000
    )

    assert first["verified_target_count"] == 0
    assert first["rejected_target_count"] == 0
    assert second["verified_target_count"] == 1
    assert second["rejected_target_count"] == 0
    assert source.calls == ["3081000", "3081000"]
    await service.close()


@pytest.mark.asyncio
async def test_exact_gamma_preactivation_is_deferred_then_verified(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _FakeGammaSource(record)
    del source.market["acceptingOrders"]
    source.market.update(
        {
            "active": False,
            "closed": False,
            "ready": False,
            "funded": False,
            "outcomePrices": None,
        }
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            gamma_poll_interval_seconds=0.01,
        ),
        source_client=source,
        websocket_factory=object(),
    )
    first = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 600_000
    )
    source.market.update(
        {
            "active": True,
            "closed": False,
            "ready": True,
            "funded": True,
            "acceptingOrders": True,
            "outcomePrices": ["0.5", "0.5"],
        }
    )

    second = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 599_000
    )

    assert first["verified_target_count"] == 0
    assert first["rejected_target_count"] == 0
    assert second["verified_target_count"] == 1
    assert second["rejected_target_count"] == 0
    assert source.calls == ["3081000", "3081000"]
    await service.close()


@pytest.mark.asyncio
async def test_unresolved_settlement_excludes_and_stops_after_timeout(
    tmp_path: Path,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _FakeGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)
    await asyncio.wait_for(
        _FakeRecorder.instances[0].sink.ready.wait(),
        timeout=1,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 57_000)

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 + 360_000
    )

    assert status["target_states"] == {"excluded": 1}
    assert status["active_worker_count"] == 0
    assert _FakeRecorder.instances[0].stopped is True
    await service.close()


class _FailingRecorder(_FakeRecorder):
    async def run(self) -> None:
        raise RuntimeError("simulated worker failure")


@pytest.mark.asyncio
async def test_worker_failure_excludes_scope_and_finalizes_worker(
    tmp_path: Path,
) -> None:
    _FailingRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_FailingRecorder,
    )

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 60_000
    )

    assert status["target_states"] == {"excluded": 1}
    assert status["active_worker_count"] == 0
    assert _FailingRecorder.instances[0].stopped is True
    lifecycle = service.supervisor.target_snapshot(
        f"btc-updown-5m-{OPEN_SECONDS}"
    )
    assert lifecycle.exclusion_reason == "worker_failed"
    assert lifecycle.exclusion_record_id != record["record_id"]
    await service.close()


@pytest.mark.asyncio
async def test_capture_excluded_target_still_collects_final_gamma_label(
    tmp_path: Path,
) -> None:
    _FailingRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _FakeGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FailingRecorder,
    )
    open_ms = OPEN_SECONDS * 1_000
    close_ms = open_ms + 300_000
    await service.scan_once(now_ms=open_ms - 60_000)
    excluded = service.supervisor.target_snapshot(
        f"btc-updown-5m-{OPEN_SECONDS}"
    )
    assert excluded.exclusion_reason == "worker_failed"
    slug = f"btc-updown-5m-{OPEN_SECONDS}"
    target = service._targets[slug]
    service._chainlink_boundaries[slug] = {
        "open": _finalized_boundary(
            target,
            role="open",
            received_at_ms=open_ms + 1_000,
        ),
        "close": _finalized_boundary(
            target,
            role="close",
            received_at_ms=close_ms + 1_000,
        ),
    }
    calls_before_close = len(source.calls)
    source.market.update(
        {
            "active": False,
            "closed": True,
            "acceptingOrders": False,
            "outcomePrices": ["1", "0"],
            "updatedAt": _iso(close_ms + 1_000),
        }
    )
    source.response_time_seconds = (close_ms + 10_000) / 1_000

    pending = await service.scan_once(now_ms=close_ms + 10_000)
    await service._control_sink.close()
    persistence = _MemoryPersistence()
    service._control_sink = persistence
    await service._reconcile_target_settlement_legacy(
        target,
        now_ms=close_ms + 60_001,
    )
    terminal = service.snapshot()
    still_excluded = service.supervisor.target_snapshot(
        f"btc-updown-5m-{OPEN_SECONDS}"
    )

    assert len(source.calls) > calls_before_close
    assert pending["label_states"] == {
        "gamma_resolved_chainlink_pending": 1
    }
    assert pending["label_tracking_complete_count"] == 0
    assert terminal["label_tracking_complete_count"] == 1
    assert still_excluded.exclusion_reason == "worker_failed"
    assert still_excluded.state.value == "excluded"
    reconciliation_events = [
        record["payload"]
        for record in persistence.records
        if record["event_type"] == "settlement_reconciled"
    ]
    assert reconciliation_events
    assert {
        event["chainlink_boundary_status"]
        for event in reconciliation_events
    } == {"verified"}
    await service.close()


class _NeverReadyRecorder(_FakeRecorder):
    async def run(self) -> None:
        await self._stop.wait()


@pytest.mark.asyncio
async def test_never_ready_worker_is_stopped_at_open_deadline(
    tmp_path: Path,
) -> None:
    _NeverReadyRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_NeverReadyRecorder,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)

    status = await service.scan_once(now_ms=OPEN_SECONDS * 1_000)

    assert status["target_states"] == {"excluded": 1}
    assert status["active_worker_count"] == 0
    assert _NeverReadyRecorder.instances[0].stopped is True
    await service.close()


class _DisconnectBeforeAckRecorder(_FakeRecorder):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.disconnected = asyncio.Event()
        self.allow_reconnect = asyncio.Event()

    async def _emit_resync(
        self,
        *,
        received_at_ms: int,
        monotonic_ns: int,
    ) -> None:
        await self.sink.emit(
            {
                "schema_version": "edge-lab-recorder.lifecycle.v1",
                "session_id": "session",
                "connection_id": "connection",
                "source": "clob_market_ws",
                "kind": "lifecycle",
                "event_type": "resync_complete",
                "received_at": _iso(received_at_ms),
                "monotonic_ns": monotonic_ns,
                "detail": {
                    "watermark_ns": monotonic_ns - 1,
                    "asset_watermarks_ns": {
                        "1" * 76: monotonic_ns - 2,
                        "2" * 76: monotonic_ns - 1,
                    },
                },
            }
        )

    async def run(self) -> None:
        await self._emit_resync(
            received_at_ms=OPEN_SECONDS * 1_000 - 58_000,
            monotonic_ns=10_000,
        )
        await self.sink.emit(
            {
                "schema_version": "edge-lab-recorder.lifecycle.v1",
                "session_id": "session",
                "connection_id": "connection",
                "source": "clob_market_ws",
                "kind": "lifecycle",
                "event_type": "disconnected",
                "received_at": _iso(
                    OPEN_SECONDS * 1_000 - 57_500
                ),
                "monotonic_ns": 11_000,
                "detail": {},
            }
        )
        self.disconnected.set()
        await self.allow_reconnect.wait()
        await self._emit_resync(
            received_at_ms=OPEN_SECONDS * 1_000 - 50_000,
            monotonic_ns=12_000,
        )
        await self._stop.wait()


@pytest.mark.asyncio
async def test_disconnect_clears_stale_initial_readiness(
    tmp_path: Path,
) -> None:
    _DisconnectBeforeAckRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_DisconnectBeforeAckRecorder,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)
    recorder = _DisconnectBeforeAckRecorder.instances[0]
    await asyncio.wait_for(recorder.disconnected.wait(), timeout=1)

    stale = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 57_000
    )
    assert stale["target_states"] == {"announced": 1}

    recorder.allow_reconnect.set()
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    recovered = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 49_000
    )

    assert recovered["target_states"] == {"subscribed": 1}
    await service.close()


class _AlwaysFailGammaSource(_FakeGammaSource):
    def gamma_market(self, market_id: str) -> Fetched[dict[str, object]]:
        self.calls.append(market_id)
        raise PublicSourceError(
            "public Gamma unavailable",
            code="proxy_unavailable",
            error_type="ProxyError",
        )


@pytest.mark.asyncio
async def test_unverified_target_is_terminal_at_open_deadline(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _AlwaysFailGammaSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 + 60_000)

    assert status["verified_target_count"] == 0
    assert status["rejected_target_count"] == 1
    assert status["active_worker_count"] == 0
    assert source.calls == ["3081000"]
    await service.close()


class _PostAckGapRecorder(_DisconnectBeforeAckRecorder):
    def __init__(self, **kwargs: object) -> None:
        _FakeRecorder.__init__(self, **kwargs)
        self.trigger_disconnect = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.allow_reconnect = asyncio.Event()

    async def run(self) -> None:
        await self._emit_resync(
            received_at_ms=OPEN_SECONDS * 1_000 - 58_000,
            monotonic_ns=10_000,
        )
        await self.trigger_disconnect.wait()
        await self.sink.emit(
            {
                "schema_version": "edge-lab-recorder.lifecycle.v1",
                "session_id": "session",
                "connection_id": "connection",
                "source": "clob_market_ws",
                "kind": "lifecycle",
                "event_type": "disconnected",
                "received_at": _iso(
                    OPEN_SECONDS * 1_000 - 30_000
                ),
                "monotonic_ns": 11_000,
                "detail": {},
            }
        )
        self.disconnected.set()
        reconnect = asyncio.create_task(self.allow_reconnect.wait())
        stopped = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {reconnect, stopped},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if stopped in done:
            return
        await self._emit_resync(
            received_at_ms=OPEN_SECONDS * 1_000 - 10_000,
            monotonic_ns=12_000,
        )
        await self._stop.wait()


async def _subscribed_gap_service(
    tmp_path: Path,
) -> tuple[DynamicShortCryptoService, _PostAckGapRecorder]:
    _PostAckGapRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_PostAckGapRecorder,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)
    recorder = _PostAckGapRecorder.instances[0]
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 57_000)
    return service, recorder


@pytest.mark.asyncio
async def test_post_ack_resnapshot_uses_local_monotonic_chronology(
    tmp_path: Path,
) -> None:
    service, recorder = await _subscribed_gap_service(tmp_path)
    recorder.trigger_disconnect.set()
    await asyncio.wait_for(recorder.disconnected.wait(), timeout=1)
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 29_000)
    group_id = next(iter(service._workers))
    assert service.supervisor.group_snapshot(group_id).resync_required

    recorder.allow_reconnect.set()
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 - 9_000
    )

    group = service.supervisor.group_snapshot(group_id)
    assert status["target_states"] == {"subscribed": 1}
    assert group.resync_required is False
    assert group.last_resnapshot_watermark_ns == 12_000
    await service.close()


@pytest.mark.asyncio
async def test_unrecovered_preopen_gap_excludes_and_stops_at_open(
    tmp_path: Path,
) -> None:
    service, recorder = await _subscribed_gap_service(tmp_path)
    recorder.trigger_disconnect.set()
    await asyncio.wait_for(recorder.disconnected.wait(), timeout=1)
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 29_000)

    status = await service.scan_once(now_ms=OPEN_SECONDS * 1_000)

    assert status["target_states"] == {"excluded": 1}
    assert status["active_worker_count"] == 0
    assert recorder.stopped is True
    await service.close()


class _FailsAfterVerifiedSource(_FakeGammaSource):
    fail = False

    def gamma_market(self, market_id: str) -> Fetched[dict[str, object]]:
        if self.fail:
            self.calls.append(market_id)
            raise PublicSourceError(
                "public Gamma unavailable",
                code="proxy_unavailable",
                error_type="ProxyError",
            )
        return super().gamma_market(market_id)


@pytest.mark.asyncio
async def test_settlement_timeout_survives_gamma_transport_failure(
    tmp_path: Path,
) -> None:
    _FakeRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    source = _FailsAfterVerifiedSource(record)
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=source,
        websocket_factory=object(),
        recorder_factory=_FakeRecorder,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)
    await asyncio.wait_for(
        _FakeRecorder.instances[0].sink.ready.wait(),
        timeout=1,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 57_000)
    source.fail = True

    status = await service.scan_once(
        now_ms=OPEN_SECONDS * 1_000 + 360_000
    )

    assert status["target_states"] == {"excluded": 1}
    assert status["active_worker_count"] == 0
    await service.close()


class _RaisesAfterStopRecorder(_FakeRecorder):
    async def run(self) -> None:
        await super().run()
        raise RuntimeError("checkpoint failed after stop")


@pytest.mark.asyncio
async def test_shutdown_worker_failure_is_not_acknowledged_as_clean(
    tmp_path: Path,
) -> None:
    _RaisesAfterStopRecorder.instances.clear()
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    record = _announcement()
    _finalize(
        discovery / "batch.jsonl",
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
    )
    service = DynamicShortCryptoService(
        DynamicShortCryptoServiceConfig(
            discovery_dir=discovery,
            output_root=tmp_path / "dynamic",
            subscribe_lead_ms=60_000,
            scan_interval_seconds=0.01,
            gamma_poll_interval_seconds=0.01,
            settlement_timeout_ms=60_000,
            clob_snapshot_interval_seconds=30,
        ),
        source_client=_FakeGammaSource(record),
        websocket_factory=object(),
        recorder_factory=_RaisesAfterStopRecorder,
    )
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 60_000)
    recorder = _RaisesAfterStopRecorder.instances[0]
    await asyncio.wait_for(recorder.sink.ready.wait(), timeout=1)
    await service.scan_once(now_ms=OPEN_SECONDS * 1_000 - 57_000)

    with pytest.raises(RuntimeError, match="close had 1 failure"):
        await service.close()

    assert recorder.stopped is True
