from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.profit_system.execution import (
    ExecutionApproval,
    ExecutionEngine,
    ExecutionIntent,
    ExecutionMode,
    HmacReleasePermitAuthority,
    InMemoryKillSwitchStore,
    InMemoryQualificationRegistry,
    InterventionAction,
    InterventionKind,
    OrderLifecycleStatus,
    OrderRecord,
    OrderSide,
    PersistentKillError,
    PolymarketLiveVenueAdapter,
    QualificationLevel,
    QualificationRecord,
    ReleasePermitError,
    RiskContext,
    RiskLimitError,
    RiskLimits,
    TimeInForce,
    VenueEventKind,
    VenueOrderSpec,
    VenueOrderState,
    VenueReconciliation,
    VenueUserEvent,
)
from src.profit_system.execution.adapters import ReplayVenueAdapter
from src.profit_system.readiness import REQUIRED_CANARY_CHECKS


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


def _qualification() -> QualificationRecord:
    return QualificationRecord(
        strategy_id="strat-1",
        qualification=QualificationLevel.PROBE_ALLOWED,
        allowed_modes=(ExecutionMode.PAPER, ExecutionMode.LIVE_CANARY),
        market_whitelist=("market-1",),
        risk_limits=_limits(),
        evidence_digest="digest-1",
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


class _TimeoutClient:
    def __init__(self) -> None:
        self.post_attempts = 0

    async def get_closed_only_mode(self) -> bool:
        return False

    async def get_balance_allowance(self, *, asset_type: str):
        del asset_type
        return type(
            "Allowance", (), {"balance": 25_000_000, "allowances": {"exchange": 25_000_000}}
        )()

    async def create_limit_order(self, **kwargs):
        return {"signed": kwargs}

    async def post_order(self, signed_order):
        del signed_order
        self.post_attempts += 1
        raise TimeoutError("timed out")

    async def cancel_all(self):
        return {"canceled": (), "not_canceled": {}}

    async def cancel_order(self, *, order_id: str):
        return {"canceled": (order_id,), "not_canceled": {}}

    async def cancel_orders(self, *, order_ids):
        return {"canceled": tuple(order_ids), "not_canceled": {}}

    async def list_open_orders(self):
        return _AsyncList([])

    async def list_account_trades(self, *, after=None):
        del after
        return _AsyncList([])

    async def subscribe(self, spec):
        del spec
        return _AsyncList([])


class _AsyncList:
    def __init__(self, items) -> None:
        self._items = tuple(items)

    def __aiter__(self):
        async def iterate():
            for item in self._items:
                yield item

        return iterate()

    def iter_items(self):
        return self.__aiter__()


def _arm_live_stream(engine: ExecutionEngine, *, occurred_at: datetime | None = None) -> None:
    timestamp = datetime.now(tz=UTC) if occurred_at is None else occurred_at
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.BACKFILL,
            occurred_at=timestamp,
            cursor="backfill-1",
            detail="live_backfill_complete",
        )
    )


@pytest.mark.asyncio
async def test_ambiguous_timeout_is_idempotent_and_never_blindly_resubmits() -> None:
    client = _TimeoutClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    first = await adapter.submit(
        orders=(_order(),),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-1",
    )
    second = await adapter.submit(
        orders=(_order(),),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-1",
    )

    assert first.results[0].status is OrderLifecycleStatus.AMBIGUOUS
    assert second.results[0].status is OrderLifecycleStatus.AMBIGUOUS
    assert client.post_attempts == 1


@pytest.mark.asyncio
async def test_cancel_remains_allowed_when_new_orders_are_blocked() -> None:
    from src.profit_system.execution import PaperVenueAdapter
    from src.profit_system.execution.adapters import PaperOrderBook

    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.45"),
                bid_size=Decimal("5"),
                ask_size=Decimal("5"),
                as_of=datetime.now(tz=UTC),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
    )
    plan = await engine.review(
        ExecutionIntent(
            strategy_id="strat-1",
            mode=ExecutionMode.PAPER,
            orders=(_order(),),
            risk_context=RiskContext(market_data_age_ms=100),
        ),
        actor="alice",
        idempotency_key="review-1",
    )
    ticket = await engine.confirm(
        plan.plan_id,
        ExecutionApproval(
            actor="alice",
            idempotency_key="confirm-1",
            plan_hash=plan.plan_hash,
            confirmation_phrase=plan.confirmation_phrase,
        ),
    )
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.GAP,
            occurred_at=datetime.now(tz=UTC),
            cursor="gap-1",
            gap_seconds=99,
            detail="user stream stalled",
        )
    )
    with pytest.raises(RiskLimitError):
        await engine.review(
            ExecutionIntent(
                strategy_id="strat-1",
                mode=ExecutionMode.PAPER,
                orders=(_order("client-2"),),
                risk_context=RiskContext(market_data_age_ms=100),
            ),
            actor="alice",
            idempotency_key="review-2",
        )

    result = await engine.intervene(
        InterventionAction(
            kind=InterventionKind.CANCEL,
            order_ids=(ticket.orders[0].venue_order_id,),
            reason="reduce risk",
        ),
        actor="alice",
        idempotency_key="cancel-1",
    )
    assert result.canceled_ids == (ticket.orders[0].venue_order_id,)


@pytest.mark.asyncio
async def test_fill_dedupe_and_persistent_kill_survive_restart() -> None:
    from src.profit_system.execution import FillRecord, PaperVenueAdapter
    from src.profit_system.execution.adapters import PaperOrderBook

    store = InMemoryKillSwitchStore()
    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.45"),
                bid_size=Decimal("5"),
                ask_size=Decimal("5"),
                as_of=datetime.now(tz=UTC),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
        kill_store=store,
    )
    plan = await engine.review(
        ExecutionIntent(
            strategy_id="strat-1",
            mode=ExecutionMode.PAPER,
            orders=(_order(),),
            risk_context=RiskContext(market_data_age_ms=100),
        ),
        actor="alice",
        idempotency_key="review-1",
    )
    ticket = await engine.confirm(
        plan.plan_id,
        ExecutionApproval(
            actor="alice",
            idempotency_key="confirm-1",
            plan_hash=plan.plan_hash,
            confirmation_phrase=plan.confirmation_phrase,
        ),
    )
    fill = FillRecord(
        fill_id="fill-1",
        venue_order_id=ticket.orders[0].venue_order_id or "venue-1",
        market_id="market-1",
        token_id="token-1",
        side=OrderSide.BUY,
        price=Decimal("0.40"),
        quantity=Decimal("1"),
        fee=Decimal("0"),
        occurred_at=datetime.now(tz=UTC),
        raw_status="MATCHED",
    )
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.TRADE,
            occurred_at=fill.occurred_at,
            cursor="fill-cursor-1",
            fill=fill,
        )
    )
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.TRADE,
            occurred_at=fill.occurred_at,
            cursor="fill-cursor-2",
            fill=fill,
        )
    )
    assert engine.snapshot().orders[0].filled_quantity == Decimal("1")

    await engine.intervene(
        InterventionAction(kind=InterventionKind.KILL, reason="manual kill"),
        actor="alice",
        idempotency_key="kill-1",
    )
    restarted = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.45"),
                bid_size=Decimal("5"),
                ask_size=Decimal("5"),
                as_of=datetime.now(tz=UTC),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
        kill_store=store,
        restored_orders=engine.snapshot().orders,
    )

    assert restarted.snapshot().killed is True
    with pytest.raises(PersistentKillError):
        await restarted.review(
            ExecutionIntent(
                strategy_id="strat-1",
                mode=ExecutionMode.PAPER,
                orders=(_order("client-2"),),
                risk_context=RiskContext(market_data_age_ms=100),
            ),
            actor="alice",
            idempotency_key="review-2",
        )


@pytest.mark.asyncio
async def test_reconcile_keeps_blocks_until_backfill_and_discrepancies_clear() -> None:
    live_state = VenueOrderState(
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
        updated_at=datetime.now(tz=UTC),
    )
    restored = OrderRecord(
        client_order_id="client-1",
        spec=_order(),
        status=OrderLifecycleStatus.AMBIGUOUS,
        plan_id="plan-1",
        venue_order_id="venue-1",
        updated_at=datetime.now(tz=UTC),
    )
    adapter = ReplayVenueAdapter(
        scripted_reconciliation=VenueReconciliation(
            open_orders=(live_state,),
            fills=(),
            as_of=datetime.now(tz=UTC),
            backfill_complete=False,
            discrepancies=("cash_mismatch",),
        )
    )
    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
        restored_orders=(restored,),
    )

    await engine.intervene(
        InterventionAction(kind=InterventionKind.RECONCILE, reason="startup"),
        actor="alice",
        idempotency_key="reconcile-1",
    )
    snapshot = engine.snapshot()
    assert "restart_reconcile_required" in snapshot.blocked_reasons
    assert "user_stream_backfill_pending" in snapshot.blocked_reasons
    assert "reconciliation_mismatch" in snapshot.blocked_reasons

    adapter._scripted_reconciliation = VenueReconciliation(  # noqa: SLF001
        open_orders=(live_state,),
        fills=(),
        as_of=datetime.now(tz=UTC),
        backfill_complete=True,
        discrepancies=(),
    )
    await engine.intervene(
        InterventionAction(kind=InterventionKind.RECONCILE, reason="startup"),
        actor="alice",
        idempotency_key="reconcile-2",
    )
    snapshot = engine.snapshot()
    assert "restart_reconcile_required" not in snapshot.blocked_reasons
    assert "user_stream_backfill_pending" not in snapshot.blocked_reasons
    assert "reconciliation_mismatch" not in snapshot.blocked_reasons


@pytest.mark.asyncio
async def test_live_replace_fails_closed_without_new_review_permit() -> None:
    from src.profit_system.execution import PaperVenueAdapter
    from src.profit_system.execution.adapters import PaperOrderBook

    authority = HmacReleasePermitAuthority(secret=b"release-secret")
    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("5"),
                ask_size=Decimal("5"),
                as_of=datetime.now(tz=UTC),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": _qualification()}),
        permit_authority=authority,
    )
    _arm_live_stream(engine)
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.LIVE_CANARY,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )
    plan = await engine.review(intent, actor="alice", idempotency_key="review-live")
    permit = authority.issue(
        plan=plan,
        qualification=_qualification(),
        confirmation_phrase=plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )
    ticket = await engine.confirm(
        plan.plan_id,
        ExecutionApproval(
            actor="alice",
            idempotency_key="confirm-live",
            plan_hash=plan.plan_hash,
            confirmation_phrase=plan.confirmation_phrase,
            release_permit=permit,
        ),
    )

    with pytest.raises(ReleasePermitError, match="new explicit review/confirm cycle"):
        await engine.intervene(
            InterventionAction(
                kind=InterventionKind.REPLACE,
                order_ids=(ticket.orders[0].venue_order_id or "",),
                replacement_orders=(_order("client-2"),),
                reason="quote moved",
            ),
            actor="alice",
            idempotency_key="replace-live-1",
        )
    assert engine.snapshot().orders[0].status is OrderLifecycleStatus.LIVE
