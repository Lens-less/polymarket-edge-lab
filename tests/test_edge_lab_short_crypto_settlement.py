"""Fail-closed settlement reconciliation for short Chainlink markets."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.edge_lab.chainlink_ptb import (
    FinalizedChainlinkBoundaries,
    FinalizedChainlinkBoundary,
)
from src.edge_lab.data_store import canonical_record_id
from src.edge_lab.short_crypto_catalog import ShortCryptoTarget
from src.edge_lab.short_crypto_settlement import (
    AtomicSettlementGate,
    ChainlinkBoundaryObservation,
    CloseLivenessProof,
    ClosePongObservation,
    SettlementCommitReceipt,
    SettlementRejection,
    reconcile_short_crypto_settlement,
)


OPEN_MS = 1_784_899_000_000
CLOSE_MS = OPEN_MS + 300_000


def _iso(timestamp_ms: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_ms / 1_000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def target() -> ShortCryptoTarget:
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
        announced_at=_iso(OPEN_MS - 60_000),
        announcement_record_id="a" * 64,
        rule_hash="b" * 64,
    )


def gamma_market(
    *,
    closed: bool = True,
    outcome_prices: str = '["1", "0"]',
) -> dict[str, object]:
    expected = target()
    return {
        "id": expected.market_id,
        "slug": expected.slug,
        "conditionId": expected.condition_id,
        "outcomes": '["Up", "Down"]',
        "outcomePrices": outcome_prices,
        "clobTokenIds": (
            f'["{expected.up_token_id}", "{expected.down_token_id}"]'
        ),
        "closed": closed,
        "updatedAt": _iso(CLOSE_MS + 5_000),
    }


def gamma_record(
    markets: list[dict[str, object]],
    *,
    received_at_ms: int = CLOSE_MS + 10_000,
) -> dict[str, object]:
    recorder_payload = {
        "schema_version": "gamma-http.snapshot.v1",
        "source": "gamma_http",
        "kind": "data",
        "event_type": "snapshot",
        "session_id": "session-a",
        "connection_id": "connection-a",
        "received_at": _iso(received_at_ms),
        "event_at": None,
        "sequence": None,
        "payload": {
            "snapshot_kind": "periodic",
            "responses": [
                {
                    "resource": "gamma_markets",
                    "raw_json": {
                        "$schema": "GammaMarkets",
                        "markets": markets,
                        "next_cursor": "cursor",
                    },
                    "provenance": {"status_code": 200},
                }
            ],
        },
    }
    record: dict[str, object] = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "gamma_http",
        "received_at": _iso(received_at_ms),
        "event_at": None,
        "sequence": None,
        "payload": recorder_payload,
    }
    record["record_id"] = canonical_record_id(record)
    return record


def gamma_lifecycle_record(
    *,
    kind: str = "lifecycle",
    event_type: str = "snapshot_error",
) -> dict[str, object]:
    received_at_ms = CLOSE_MS + 3_000
    recorder_payload = {
        "schema_version": "gamma-http.lifecycle.v1",
        "source": "gamma_http",
        "kind": kind,
        "event_type": event_type,
        "session_id": "session-a",
        "connection_id": "connection-a",
        "received_at": _iso(received_at_ms),
        "event_at": None,
        "sequence": None,
        "payload": None,
    }
    record: dict[str, object] = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "gamma_http",
        "received_at": _iso(received_at_ms),
        "event_at": None,
        "sequence": None,
        "payload": recorder_payload,
    }
    record["record_id"] = canonical_record_id(record)
    return record


def chainlink_pair(
    *,
    open_price: str = "100",
    close_price: str = "101",
) -> tuple[ChainlinkBoundaryObservation, ChainlinkBoundaryObservation]:
    return (
        ChainlinkBoundaryObservation(
            record_id="c" * 64,
            symbol="btc/usd",
            event_at_ms=OPEN_MS,
            received_at_ms=OPEN_MS + 1_500,
            price=Decimal(open_price),
        ),
        ChainlinkBoundaryObservation(
            record_id="d" * 64,
            symbol="btc/usd",
            event_at_ms=CLOSE_MS,
            received_at_ms=CLOSE_MS + 2_000,
            price=Decimal(close_price),
        ),
    )


def finalized_boundaries(
    *,
    open_price: str = "100",
    close_price: str = "101",
    open_retrospective: bool = False,
) -> FinalizedChainlinkBoundaries:
    common = {
        "source_topic": "crypto_prices_chainlink",
        "source_symbol": "btc/usd",
        "rule_url": "https://data.chain.link/streams/btc-usd",
        "rule_hash": "b" * 64,
        "display_encoding": "exact_decimal",
        "session_id": "rtds-session",
        "connection_id": "rtds-connection",
        "source_manifest_sha256": "3" * 64,
        "raw_sha256": "4" * 64,
        "line_number": 1,
    }
    return FinalizedChainlinkBoundaries(
        open_boundary=FinalizedChainlinkBoundary(
            boundary_role="open",
            inner_timestamp_ms=OPEN_MS,
            price=Decimal(open_price),
            display_value=open_price,
            full_accuracy_value=(
                str(int(Decimal(open_price) * Decimal(10**18)))
            ),
            record_id="c" * 64,
            received_at=_iso(OPEN_MS + 1_500),
            source_manifest_path=(
                "/capture/raw/rtds_ws/open.manifest.json"
            ),
            raw_path="/capture/raw/rtds_ws/open.jsonl",
            retrospective=open_retrospective,
            available_at_first_decision=not open_retrospective,
            **common,
        ),
        close_boundary=FinalizedChainlinkBoundary(
            boundary_role="close",
            inner_timestamp_ms=CLOSE_MS,
            price=Decimal(close_price),
            display_value=close_price,
            full_accuracy_value=(
                str(int(Decimal(close_price) * Decimal(10**18)))
            ),
            record_id="d" * 64,
            received_at=_iso(CLOSE_MS + 2_000),
            source_manifest_path=(
                "/capture/raw/rtds_ws/close.manifest.json"
            ),
            raw_path="/capture/raw/rtds_ws/close.jsonl",
            retrospective=True,
            available_at_first_decision=False,
            **common,
        ),
    )


def close_liveness(
    *,
    before_connection_id: str = "clob-connection",
    after_connection_id: str = "clob-connection",
    before_raw_frame: str = "PONG",
    after_raw_frame: str = "PONG",
    before_received_at_ms: int = CLOSE_MS - 5_000,
    after_received_at_ms: int = CLOSE_MS + 5_000,
) -> CloseLivenessProof:
    return CloseLivenessProof(
        before=ClosePongObservation(
            record_id="e" * 64,
            session_id="clob-session",
            connection_id=before_connection_id,
            received_at_ms=before_received_at_ms,
            raw_frame=before_raw_frame,
        ),
        after=ClosePongObservation(
            record_id="f" * 64,
            session_id="clob-session",
            connection_id=after_connection_id,
            received_at_ms=after_received_at_ms,
            raw_frame=after_raw_frame,
        ),
        tolerance_ms=30_000,
    )


@pytest.mark.asyncio
async def test_gamma_only_winner_remains_pending_without_boundary_evidence() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("incomplete settlement must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=None,
        close_liveness=None,
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "PENDING"
    assert result.reason_codes == ("chainlink_boundary_evidence_missing",)
    assert result.label is None
    assert gate.snapshot == result
    assert persisted is False


@pytest.mark.parametrize(
    ("now_ms", "expected_state", "expected_reasons"),
    [
        (
            CLOSE_MS + 10_000,
            "PENDING",
            ("clob_close_liveness_missing",),
        ),
        (
            CLOSE_MS + 60_000,
            "EXCLUDED",
            (
                "clob_close_liveness_missing",
                "settlement_deadline_elapsed",
            ),
        ),
    ],
)
@pytest.mark.asyncio
async def test_missing_close_pong_stays_pending_then_excludes_at_timeout(
    now_ms: int,
    expected_state: str,
    expected_reasons: tuple[str, ...],
) -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("missing liveness must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=None,
        now_ms=now_ms,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == expected_state
    assert result.reason_codes == expected_reasons
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_all_complete_durable_evidence_commits_then_becomes_settled() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted_payload: dict[str, object] | None = None

    async def persist(
        payload: dict[str, object],
    ) -> SettlementCommitReceipt:
        nonlocal persisted_payload
        assert gate.snapshot.state == "PENDING"
        persisted_payload = payload
        required = payload["required_record_ids"]
        assert isinstance(required, list)
        commit_id = "9" * 64
        return SettlementCommitReceipt(
            record_id=commit_id,
            durable_finalized_record_ids=frozenset(
                [commit_id, *(str(item) for item in required)]
            ),
        )

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=close_liveness(),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "SETTLED"
    assert result.reason_codes == ()
    assert result.label is not None
    assert result.label.outcome == "Up"
    assert result.commit_record_id == "9" * 64
    assert set(result.required_record_ids) == {
        "a" * 64,
        reconciliation.label.gamma_record_id,
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "f" * 64,
    }
    assert persisted_payload is not None
    assert persisted_payload["transition"] == "commit_settlement"
    assert persisted_payload["outcome"] == "Up"


@pytest.mark.asyncio
async def test_cross_connection_close_pongs_are_settlement_conflict() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("cross-connection PONGs must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=close_liveness(
            before_connection_id="connection-before",
            after_connection_id="connection-after",
        ),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "settlement_conflict"
    assert result.reason_codes == ("clob_close_pong_connection_conflict",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_empty_pong_session_or_connection_cannot_satisfy_liveness() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    proof = close_liveness()
    proof = replace(
        proof,
        before=replace(
            proof.before,
            session_id="",
            connection_id="",
        ),
        after=replace(
            proof.after,
            session_id="",
            connection_id="",
        ),
    )
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("empty provenance must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=proof,
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "settlement_conflict"
    assert result.reason_codes == ("clob_close_pong_connection_conflict",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_non_pong_frame_cannot_satisfy_close_liveness() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("non-PONG liveness must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=close_liveness(after_raw_frame="PING"),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "PENDING"
    assert result.reason_codes == ("clob_close_liveness_missing",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_pongs_that_do_not_bracket_close_are_not_close_liveness() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("non-bracketing PONGs must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=close_liveness(
            after_received_at_ms=CLOSE_MS - 1,
        ),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "PENDING"
    assert result.reason_codes == ("clob_close_liveness_missing",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_strict_gate_revalidates_boundary_rule_and_timestamp_identity() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    pair = finalized_boundaries()
    pair = replace(
        pair,
        close_boundary=replace(
            pair.close_boundary,
            inner_timestamp_ms=CLOSE_MS - 1,
            rule_hash="0" * 64,
        ),
    )
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("boundary identity conflict must not persist")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=pair,
        close_liveness=close_liveness(),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "settlement_conflict"
    assert result.reason_codes == ("chainlink_boundary_identity_conflict",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_retrospective_open_boundary_cannot_reach_settled() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("retrospective PTB must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(open_retrospective=True),
        close_liveness=close_liveness(),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "PENDING"
    assert result.reason_codes == ("chainlink_open_retrospective",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_future_gamma_boundary_or_pong_cannot_settle_early() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("future evidence must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=close_liveness(),
        now_ms=CLOSE_MS + 1_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "PENDING"
    assert result.reason_codes == ("settlement_evidence_not_yet_available",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_strict_gate_rebinds_reconciliation_and_winner_token_identity() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    assert reconciliation.label is not None
    reconciliation = replace(
        reconciliation,
        target_condition_id="0x" + "cd" * 32,
        label=replace(
            reconciliation.label,
            winning_token_id=target().down_token_id,
        ),
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("identity drift must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(),
        close_liveness=close_liveness(),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "settlement_conflict"
    assert result.reason_codes == ("settlement_target_identity_conflict",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_strict_gate_rejects_gamma_chainlink_winner_conflict() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market(outcome_prices='["1", "0"]')])],
    )
    gate = AtomicSettlementGate(target())
    persisted = False

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        nonlocal persisted
        persisted = True
        raise AssertionError("winner conflict must not be persisted")

    result = await gate.reconcile_and_commit(
        reconciliation=reconciliation,
        boundaries=finalized_boundaries(
            open_price="101",
            close_price="100",
        ),
        close_liveness=close_liveness(),
        now_ms=CLOSE_MS + 10_000,
        settlement_deadline_ms=CLOSE_MS + 60_000,
        persist=persist,
    )

    assert result.state == "settlement_conflict"
    assert result.reason_codes == ("gamma_chainlink_label_conflict",)
    assert result.label is None
    assert persisted is False


@pytest.mark.asyncio
async def test_persistence_failure_never_exposes_settled_state() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        assert gate.snapshot.state == "PENDING"
        raise OSError("injected persistence failure")

    with pytest.raises(OSError, match="injected persistence failure"):
        await gate.reconcile_and_commit(
            reconciliation=reconciliation,
            boundaries=finalized_boundaries(),
            close_liveness=close_liveness(),
            now_ms=CLOSE_MS + 10_000,
            settlement_deadline_ms=CLOSE_MS + 60_000,
            persist=persist,
        )

    assert gate.snapshot.state == "PENDING"
    assert gate.snapshot.reason_codes == ("settlement_persistence_failed",)
    assert gate.snapshot.label is None
    assert gate.snapshot.commit_record_id is None


@pytest.mark.asyncio
async def test_cancellation_during_persistence_never_exposes_settled_state() -> None:
    reconciliation = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )
    gate = AtomicSettlementGate(target())
    persistence_started = asyncio.Event()
    never_release = asyncio.Event()

    async def persist(_: dict[str, object]) -> SettlementCommitReceipt:
        persistence_started.set()
        await never_release.wait()
        raise AssertionError("cancelled persistence must not return a receipt")

    task = asyncio.create_task(
        gate.reconcile_and_commit(
            reconciliation=reconciliation,
            boundaries=finalized_boundaries(),
            close_liveness=close_liveness(),
            now_ms=CLOSE_MS + 10_000,
            settlement_deadline_ms=CLOSE_MS + 60_000,
            persist=persist,
        )
    )
    await persistence_started.wait()

    assert gate.snapshot.state == "PENDING"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gate.snapshot.state == "PENDING"
    assert gate.snapshot.reason_codes == ("settlement_persistence_cancelled",)
    assert gate.snapshot.label is None
    assert gate.snapshot.commit_record_id is None


def test_resolves_only_closed_one_hot_gamma_and_matching_chainlink() -> None:
    open_value, close_value = chainlink_pair()
    record = gamma_record([gamma_market()])

    result = reconcile_short_crypto_settlement(
        target(),
        [record],
        chainlink_open=open_value,
        chainlink_close=close_value,
    )

    assert result.status == "resolved"
    assert result.reason_codes == ()
    assert result.label is not None
    assert result.label.outcome == "Up"
    assert result.label.winning_token_id == target().up_token_id
    assert result.label.gamma_record_id == record["record_id"]
    assert result.label.gamma_updated_at_ms == CLOSE_MS + 5_000
    assert result.label.gamma_received_at_ms == CLOSE_MS + 10_000
    assert result.label.chainlink_open_record_id == "c" * 64
    assert result.label.chainlink_close_record_id == "d" * 64
    assert result.label.available_at_ms == CLOSE_MS + 10_000


def test_chainlink_tie_resolves_up_by_contract() -> None:
    open_value, close_value = chainlink_pair(
        open_price="100.000",
        close_price="100.000",
    )

    result = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
        chainlink_open=open_value,
        chainlink_close=close_value,
    )

    assert result.status == "resolved"
    assert result.label is not None
    assert result.label.outcome == "Up"


def test_distinguishes_missing_from_pending() -> None:
    unrelated = gamma_market()
    unrelated["id"] = "999"
    unrelated["slug"] = "eth-updown-5m-1784899000"
    unrelated["conditionId"] = "0x" + "cd" * 32
    unrelated["clobTokenIds"] = '["3", "4"]'

    missing = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([unrelated])],
    )
    pending = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market(closed=False, outcome_prices='["0.5", "0.5"]')])],
    )

    assert missing.status == "missing"
    assert missing.reason_codes == ("gamma_not_found",)
    assert missing.label is None
    assert pending.status == "pending"
    assert pending.reason_codes == ("gamma_not_closed",)
    assert pending.label is None


@pytest.mark.parametrize(
    "non_data_record",
    [
        gamma_lifecycle_record(),
        gamma_lifecycle_record(
            kind="diagnostic",
            event_type="anomaly.config_change",
        ),
    ],
)
def test_ignores_content_addressed_gamma_non_data_records(
    non_data_record: dict[str, object],
) -> None:
    result = reconcile_short_crypto_settlement(
        target(),
        [non_data_record, gamma_record([gamma_market()])],
    )

    assert result.status == "resolved"
    assert result.label is not None
    assert result.label.outcome == "Up"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("id", "999"),
        ("conditionId", "0x" + "cd" * 32),
        ("outcomes", '["Down", "Up"]'),
        ("clobTokenIds", '["2", "1"]'),
    ],
)
def test_matching_identity_fails_closed_on_conflict(
    field: str,
    replacement: str,
) -> None:
    market = gamma_market()
    market[field] = replacement

    result = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([market])],
    )

    assert result.status == "conflict"
    assert "gamma_identity_conflict" in result.reason_codes
    assert result.label is None


@pytest.mark.parametrize(
    "outcome_prices",
    ['["0.99", "0.01"]', '["1", "1"]', '["0", "0"]', '["bogus", "0"]'],
)
def test_closed_market_requires_exact_one_hot_prices(
    outcome_prices: str,
) -> None:
    result = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market(outcome_prices=outcome_prices)])],
    )

    assert result.status == "conflict"
    assert "gamma_terminal_prices_not_one_hot" in result.reason_codes
    assert result.label is None


def test_chainlink_cross_check_detects_label_conflict() -> None:
    open_value, close_value = chainlink_pair(
        open_price="101",
        close_price="100",
    )

    result = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market(outcome_prices='["1", "0"]')])],
        chainlink_open=open_value,
        chainlink_close=close_value,
    )

    assert result.status == "conflict"
    assert result.reason_codes == ("gamma_chainlink_label_conflict",)
    assert result.label is None


@pytest.mark.parametrize(
    "mutation",
    ["incomplete", "symbol", "open_time", "close_time"],
)
def test_chainlink_pair_is_strictly_bound_to_target(
    mutation: str,
) -> None:
    open_value, close_value = chainlink_pair()
    if mutation == "incomplete":
        close_value = None
    elif mutation == "symbol":
        close_value = ChainlinkBoundaryObservation(
            record_id=close_value.record_id,
            symbol="eth/usd",
            event_at_ms=close_value.event_at_ms,
            received_at_ms=close_value.received_at_ms,
            price=close_value.price,
        )
    elif mutation == "open_time":
        open_value = ChainlinkBoundaryObservation(
            record_id=open_value.record_id,
            symbol=open_value.symbol,
            event_at_ms=open_value.event_at_ms + 1,
            received_at_ms=open_value.received_at_ms,
            price=open_value.price,
        )
    else:
        close_value = ChainlinkBoundaryObservation(
            record_id=close_value.record_id,
            symbol=close_value.symbol,
            event_at_ms=close_value.event_at_ms - 1,
            received_at_ms=close_value.received_at_ms,
            price=close_value.price,
        )

    result = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
        chainlink_open=open_value,
        chainlink_close=close_value,
    )

    assert result.status == "conflict"
    assert result.label is None
    assert result.reason_codes[0].startswith("chainlink_")


def test_conflicting_terminal_gamma_observations_never_choose_latest() -> None:
    up = gamma_record(
        [gamma_market(outcome_prices='["1", "0"]')],
        received_at_ms=CLOSE_MS + 10_000,
    )
    down = gamma_record(
        [gamma_market(outcome_prices='["0", "1"]')],
        received_at_ms=CLOSE_MS + 20_000,
    )

    result = reconcile_short_crypto_settlement(target(), [up, down])

    assert result.status == "conflict"
    assert result.reason_codes == ("gamma_terminal_label_conflict",)
    assert result.label is None
    assert result.gamma_record_ids == (up["record_id"], down["record_id"])


def test_label_is_evaluation_only_and_unavailable_before_observation() -> None:
    result = reconcile_short_crypto_settlement(
        target(),
        [gamma_record([gamma_market()])],
    )

    assert result.evaluation_label_as_of(CLOSE_MS + 9_999) is None
    assert result.evaluation_label_as_of(CLOSE_MS + 10_000) == "Up"
    assert result.decision_safe_identity() == {
        "slug": target().slug,
        "condition_id": target().condition_id,
        "rule_hash": target().rule_hash,
    }
    assert "outcome" not in result.decision_safe_identity()
    assert "winning_token_id" not in result.decision_safe_identity()


def test_rejects_noncanonical_raw_record_before_using_settlement() -> None:
    corrupt = deepcopy(gamma_record([gamma_market()]))
    corrupt["record_id"] = "0" * 64

    with pytest.raises(SettlementRejection) as caught:
        reconcile_short_crypto_settlement(target(), [corrupt])

    assert caught.value.code == "record_id_mismatch"


def test_closed_observation_received_before_market_close_is_conflict() -> None:
    result = reconcile_short_crypto_settlement(
        target(),
        [
            gamma_record(
                [gamma_market()],
                received_at_ms=CLOSE_MS - 1,
            )
        ],
    )

    assert result.status == "conflict"
    assert result.reason_codes == ("gamma_terminal_time_conflict",)
    assert result.label is None
