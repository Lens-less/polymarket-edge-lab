from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.profit_system.execution import (
    ExecutionApproval,
    ExecutionEngine,
    ExecutionIntent,
    ExecutionMode,
    FillRecord,
    InMemoryQualificationRegistry,
    InterventionAction,
    InterventionKind,
    OrderLifecycleStatus,
    OrderRecord,
    OrderSide,
    QualificationLevel,
    QualificationRecord,
    RiskContext,
    RiskLimitError,
    RiskLimits,
    TimeInForce,
    VenueEventKind,
    VenueOrderSpec,
    VenueOrderState,
    VenuePreflightResult,
    VenueReconciliation,
    VenueSubmitBatch,
    VenueSubmitResult,
    VenueUserEvent,
)


def _limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("100"),
        max_market_exposure=Decimal("100"),
        max_event_exposure=Decimal("100"),
        max_strategy_exposure=Decimal("100"),
        max_total_exposure=Decimal("100"),
        max_daily_loss=Decimal("20"),
        max_drawdown=Decimal("20"),
        max_open_orders=10,
        max_unmatched_leg_seconds=30,
        max_market_data_age_ms=500,
        max_user_stream_gap_seconds=30,
        max_order_rate=5,
    )


def _qualification(mode: ExecutionMode = ExecutionMode.PAPER) -> QualificationRecord:
    allowed_modes = (ExecutionMode.PAPER, ExecutionMode.LIVE_CANARY)
    return QualificationRecord(
        strategy_id="strat-1",
        qualification=QualificationLevel.PROBE_ALLOWED,
        allowed_modes=allowed_modes,
        market_whitelist=("market-1",),
        risk_limits=_limits(),
        evidence_digest=f"digest-{mode.value.lower()}",
    )


def _order(client_order_id: str = "client-1") -> VenueOrderSpec:
    return VenueOrderSpec(
        client_order_id=client_order_id,
        strategy_id="strat-1",
        market_id="market-1",
        token_id="token-1",
        event_id="event-1",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("0.40"),
        time_in_force=TimeInForce.GTC,
    )


def _fill(
    *,
    fill_id: str = "fill-1",
    venue_order_id: str = "venue-1",
    quantity: Decimal = Decimal("1"),
    occurred_at: datetime | None = None,
) -> FillRecord:
    return FillRecord(
        fill_id=fill_id,
        venue_order_id=venue_order_id,
        market_id="market-1",
        token_id="token-1",
        side=OrderSide.BUY,
        price=Decimal("0.40"),
        quantity=quantity,
        fee=Decimal("0"),
        occurred_at=occurred_at or datetime.now(tz=UTC),
        raw_status="MATCHED",
    )


class _ScriptedVenue:
    def __init__(
        self,
        *,
        submit_batch: VenueSubmitBatch,
        reconcile_snapshot: VenueReconciliation | None = None,
    ) -> None:
        self._submit_batch = submit_batch
        self._reconcile_snapshot = reconcile_snapshot
        self.reconcile_calls: list[tuple[tuple[str, ...] | None, str | None]] = []

    async def preflight(self, request) -> VenuePreflightResult:
        return VenuePreflightResult(ok=True, checks=(), as_of=request.now)

    async def submit(self, *, orders, mode, idempotency_key) -> VenueSubmitBatch:
        del orders, mode, idempotency_key
        return self._submit_batch

    async def cancel(self, *, order_ids, idempotency_key, reason=None):
        del reason, idempotency_key
        return type(
            "CancelResult",
            (),
            {
                "canceled_ids": tuple(order_ids),
                "unresolved_ids": (),
                "canceled_at": self._submit_batch.submitted_at,
            },
        )()

    async def reconcile(self, *, open_order_ids=None, since_trade_id=None) -> VenueReconciliation:
        captured_ids = None if open_order_ids is None else tuple(open_order_ids)
        self.reconcile_calls.append((captured_ids, since_trade_id))
        if self._reconcile_snapshot is None:
            raise AssertionError("reconcile was not scripted for this test")
        return self._reconcile_snapshot

    def stream_user_events(self, *, markets=None, since_cursor=None):
        del markets, since_cursor

        async def iterate():
            if False:
                yield None

        return iterate()


def _engine(venue: _ScriptedVenue, *, mode: ExecutionMode = ExecutionMode.PAPER) -> ExecutionEngine:
    return ExecutionEngine(
        mode=mode,
        venue=venue,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(mode)}
        ),
    )


async def _review_and_confirm(engine: ExecutionEngine, orders: tuple[VenueOrderSpec, ...]):
    plan = await engine.review(
        ExecutionIntent(
            strategy_id="strat-1",
            mode=engine.mode,
            orders=orders,
            risk_context=RiskContext(market_data_age_ms=100),
        ),
        actor="alice",
        idempotency_key="review-1",
    )
    return await engine.confirm(
        plan.plan_id,
        ExecutionApproval(
            actor="alice",
            idempotency_key="confirm-1",
            plan_hash=plan.plan_hash,
            confirmation_phrase=plan.confirmation_phrase,
        ),
    )


@pytest.mark.asyncio
async def test_confirm_keeps_mixed_batch_results_and_blocks_further_reviews_until_reconcile() -> (
    None
):
    submitted_at = datetime.now(tz=UTC)
    venue = _ScriptedVenue(
        submit_batch=VenueSubmitBatch(
            results=(
                VenueSubmitResult(
                    client_order_id="client-1",
                    status=OrderLifecycleStatus.LIVE,
                    venue_order_id="venue-1",
                ),
                VenueSubmitResult(
                    client_order_id="client-2",
                    status=OrderLifecycleStatus.REJECTED,
                    reject_reason="crossed",
                ),
                VenueSubmitResult(
                    client_order_id="client-3",
                    status=OrderLifecycleStatus.AMBIGUOUS,
                ),
            ),
            submitted_at=submitted_at,
        )
    )
    engine = _engine(venue)

    ticket = await _review_and_confirm(
        engine,
        (_order("client-1"), _order("client-2"), _order("client-3")),
    )

    assert [result.status for result in ticket.results] == [
        OrderLifecycleStatus.LIVE,
        OrderLifecycleStatus.REJECTED,
        OrderLifecycleStatus.AMBIGUOUS,
    ]
    assert "unresolved_ambiguous_submission" in engine.snapshot().blocked_reasons

    with pytest.raises(RiskLimitError):
        await engine.review(
            ExecutionIntent(
                strategy_id="strat-1",
                mode=ExecutionMode.PAPER,
                orders=(_order("client-4"),),
                risk_context=RiskContext(market_data_age_ms=100),
            ),
            actor="alice",
            idempotency_key="review-2",
        )


@pytest.mark.asyncio
async def test_reconcile_resolves_ambiguous_submit_and_reopens_reviews() -> None:
    submitted_at = datetime.now(tz=UTC)
    venue = _ScriptedVenue(
        submit_batch=VenueSubmitBatch(
            results=(
                VenueSubmitResult(
                    client_order_id="client-1",
                    status=OrderLifecycleStatus.LIVE,
                    venue_order_id="venue-1",
                ),
                VenueSubmitResult(
                    client_order_id="client-2",
                    status=OrderLifecycleStatus.REJECTED,
                    reject_reason="crossed",
                ),
                VenueSubmitResult(
                    client_order_id="client-3",
                    status=OrderLifecycleStatus.AMBIGUOUS,
                ),
            ),
            submitted_at=submitted_at,
        ),
        reconcile_snapshot=VenueReconciliation(
            open_orders=(
                VenueOrderState(
                    venue_order_id="venue-1",
                    client_order_id="client-1",
                    market_id="market-1",
                    token_id="token-1",
                    side=OrderSide.BUY,
                    quantity=Decimal("2"),
                    limit_price=Decimal("0.40"),
                    filled_quantity=Decimal("0"),
                    status=OrderLifecycleStatus.LIVE,
                    time_in_force=TimeInForce.GTC,
                    updated_at=submitted_at,
                ),
                VenueOrderState(
                    venue_order_id="venue-3",
                    client_order_id="client-3",
                    market_id="market-1",
                    token_id="token-1",
                    side=OrderSide.BUY,
                    quantity=Decimal("2"),
                    limit_price=Decimal("0.40"),
                    filled_quantity=Decimal("0"),
                    status=OrderLifecycleStatus.LIVE,
                    time_in_force=TimeInForce.GTC,
                    updated_at=submitted_at,
                ),
            ),
            fills=(),
            as_of=submitted_at,
            backfill_complete=True,
        ),
    )
    engine = _engine(venue)

    await _review_and_confirm(
        engine,
        (_order("client-1"), _order("client-2"), _order("client-3")),
    )

    result = await engine.intervene(
        InterventionAction(kind=InterventionKind.RECONCILE),
        actor="alice",
        idempotency_key="reconcile-1",
    )

    assert result.reconciliation is not None
    assert "unresolved_ambiguous_submission" not in engine.snapshot().blocked_reasons
    follow_on_plan = await engine.review(
        ExecutionIntent(
            strategy_id="strat-1",
            mode=ExecutionMode.PAPER,
            orders=(_order("client-4"),),
            risk_context=RiskContext(market_data_age_ms=100),
        ),
        actor="alice",
        idempotency_key="review-2",
    )
    assert follow_on_plan.intent.orders[0].client_order_id == "client-4"


@pytest.mark.asyncio
async def test_gap_then_backfill_recovers_live_state_without_duplicate_order_or_fill_records() -> (
    None
):
    order = OrderRecord(
        client_order_id="client-1",
        spec=_order("client-1"),
        status=OrderLifecycleStatus.LIVE,
        plan_id="plan-1",
        venue_order_id="venue-1",
        submitted_at=datetime.now(tz=UTC),
    )
    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=_ScriptedVenue(
            submit_batch=VenueSubmitBatch(results=(), submitted_at=datetime.now(tz=UTC))
        ),
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(ExecutionMode.LIVE_CANARY)}
        ),
        restored_orders=(order,),
    )
    occurred_at = datetime.now(tz=UTC)
    state = VenueOrderState(
        venue_order_id="venue-1",
        client_order_id="client-1",
        market_id="market-1",
        token_id="token-1",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("0.40"),
        filled_quantity=Decimal("1"),
        status=OrderLifecycleStatus.PARTIALLY_FILLED,
        time_in_force=TimeInForce.GTC,
        updated_at=occurred_at,
    )
    fill = _fill(venue_order_id="venue-1", occurred_at=occurred_at)

    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.GAP,
            occurred_at=occurred_at,
            cursor="gap-1",
            gap_seconds=45,
            detail="user stream stalled",
        )
    )
    assert "user_stream_gap" in engine.snapshot().blocked_reasons

    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.BACKFILL,
            occurred_at=occurred_at,
            cursor="backfill-1",
            detail="backfill complete",
        )
    )
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.ORDER,
            occurred_at=occurred_at,
            cursor="order-1",
            order=state,
        )
    )
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.ORDER,
            occurred_at=occurred_at,
            cursor="order-2",
            order=state,
        )
    )
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.TRADE,
            occurred_at=occurred_at,
            cursor="trade-1",
            fill=fill,
        )
    )
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.TRADE,
            occurred_at=occurred_at,
            cursor="trade-2",
            fill=fill,
        )
    )

    snapshot = engine.snapshot()
    assert "user_stream_gap" not in snapshot.blocked_reasons
    assert len(snapshot.orders) == 1
    assert snapshot.orders[0].filled_quantity == Decimal("1")
    assert len(snapshot.orders[0].fills) == 1


@pytest.mark.asyncio
async def test_reconcile_threads_restored_live_order_ids_and_fails_closed_when_missing() -> None:
    restored = OrderRecord(
        client_order_id="client-1",
        spec=_order(),
        status=OrderLifecycleStatus.LIVE,
        plan_id="plan-1",
        venue_order_id="venue-1",
        updated_at=datetime.now(tz=UTC),
    )
    venue = _ScriptedVenue(
        submit_batch=VenueSubmitBatch(results=(), submitted_at=datetime.now(tz=UTC))
    )
    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=venue,
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
        restored_orders=(restored,),
    )

    expected_message = (
        "locally tracked live order venue-1 is missing from venue open orders "
        "and has no terminal evidence"
    )
    venue._reconcile_snapshot = VenueReconciliation(  # noqa: SLF001
        open_orders=(),
        fills=(),
        as_of=datetime.now(tz=UTC),
        backfill_complete=True,
        discrepancies=(expected_message,),
    )

    result = await engine.intervene(
        InterventionAction(kind=InterventionKind.RECONCILE, reason="restart"),
        actor="alice",
        idempotency_key="reconcile-restored-live",
    )

    assert venue.reconcile_calls == [(("venue-1",), None)]
    assert result.reconciliation is not None
    assert result.reconciliation.discrepancies == (expected_message,)
    assert "reconciliation_mismatch" in engine.snapshot().blocked_reasons


@pytest.mark.asyncio
async def test_reconcile_applies_authoritative_canceled_state_without_invalid_live_transition() -> (
    None
):
    restored = OrderRecord(
        client_order_id="client-1",
        spec=_order(),
        status=OrderLifecycleStatus.LIVE,
        plan_id="plan-1",
        venue_order_id="venue-1",
        updated_at=datetime.now(tz=UTC),
    )
    canceled_state = VenueOrderState(
        venue_order_id="venue-1",
        client_order_id="client-1",
        market_id="market-1",
        token_id="token-1",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("0.40"),
        filled_quantity=Decimal("0"),
        status=OrderLifecycleStatus.CANCELED,
        time_in_force=TimeInForce.GTC,
        updated_at=datetime.now(tz=UTC),
    )
    venue = _ScriptedVenue(
        submit_batch=VenueSubmitBatch(results=(), submitted_at=datetime.now(tz=UTC)),
        reconcile_snapshot=VenueReconciliation(
            open_orders=(),
            resolved_orders=(canceled_state,),
            fills=(),
            as_of=datetime.now(tz=UTC),
            backfill_complete=True,
            discrepancies=("cash_mismatch",),
        ),
    )
    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=venue,
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
        restored_orders=(restored,),
    )

    first = await engine.intervene(
        InterventionAction(kind=InterventionKind.RECONCILE, reason="restart"),
        actor="alice",
        idempotency_key="reconcile-canceled-1",
    )

    assert first.reconciliation is not None
    assert engine.snapshot().orders[0].status is OrderLifecycleStatus.CANCELED
    assert "reconciliation_mismatch" in engine.snapshot().blocked_reasons

    venue._reconcile_snapshot = VenueReconciliation(  # noqa: SLF001
        open_orders=(),
        resolved_orders=(canceled_state,),
        fills=(),
        as_of=datetime.now(tz=UTC),
        backfill_complete=True,
        discrepancies=(),
    )
    second = await engine.intervene(
        InterventionAction(kind=InterventionKind.RECONCILE, reason="restart"),
        actor="alice",
        idempotency_key="reconcile-canceled-2",
    )

    assert second.reconciliation is not None
    assert engine.snapshot().orders[0].status is OrderLifecycleStatus.CANCELED
    assert "reconciliation_mismatch" not in engine.snapshot().blocked_reasons
    assert "restart_reconcile_required" not in engine.snapshot().blocked_reasons


@pytest.mark.asyncio
async def test_late_fill_after_cancel_is_retained_for_reconciliation_instead_of_crashing() -> None:
    submitted_at = datetime.now(tz=UTC)
    venue = _ScriptedVenue(
        submit_batch=VenueSubmitBatch(
            results=(
                VenueSubmitResult(
                    client_order_id="client-1",
                    status=OrderLifecycleStatus.LIVE,
                    venue_order_id="venue-1",
                ),
            ),
            submitted_at=submitted_at,
        )
    )
    engine = _engine(venue)

    ticket = await _review_and_confirm(engine, (_order("client-1"),))
    await engine.intervene(
        InterventionAction(
            kind=InterventionKind.CANCEL,
            order_ids=(ticket.orders[0].venue_order_id or "venue-1",),
            reason="reduce risk",
        ),
        actor="alice",
        idempotency_key="cancel-1",
    )

    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.TRADE,
            occurred_at=submitted_at,
            cursor="late-fill-1",
            fill=_fill(venue_order_id="venue-1", occurred_at=submitted_at),
        )
    )

    snapshot = engine.snapshot()
    assert snapshot.orders[0].filled_quantity == Decimal("1")
    assert snapshot.orders[0].status in {
        OrderLifecycleStatus.PARTIALLY_FILLED,
        OrderLifecycleStatus.FILLED,
        OrderLifecycleStatus.RECONCILING,
    }


@pytest.mark.asyncio
async def test_restored_orders_dedupe_duplicate_fill_on_replayed_trade_event() -> None:
    occurred_at = datetime.now(tz=UTC)
    restored_fill = _fill(occurred_at=occurred_at)
    restored_order = OrderRecord(
        client_order_id="client-1",
        spec=_order("client-1"),
        status=OrderLifecycleStatus.PARTIALLY_FILLED,
        plan_id="plan-1",
        venue_order_id="venue-1",
        fills=(restored_fill,),
        submitted_at=occurred_at,
        updated_at=occurred_at,
    )
    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=_ScriptedVenue(submit_batch=VenueSubmitBatch(results=(), submitted_at=occurred_at)),
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
        restored_orders=(restored_order,),
    )

    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.TRADE,
            occurred_at=occurred_at,
            cursor="replayed-fill-1",
            fill=restored_fill,
        )
    )

    snapshot = engine.snapshot()
    assert snapshot.orders[0].filled_quantity == Decimal("1")
    assert len(snapshot.orders[0].fills) == 1
