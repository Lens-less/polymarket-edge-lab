from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.profit_system import readiness
from src.profit_system.execution import (
    ExecutionApproval,
    ExecutionEngine,
    ExecutionIntent,
    ExecutionMode,
    HmacReleasePermitAuthority,
    IdempotencyConflictError,
    InMemoryQualificationRegistry,
    InterventionAction,
    InterventionKind,
    OrderLifecycleStatus,
    OrderRecord,
    OrderSide,
    PaperVenueAdapter,
    QualificationError,
    QualificationLevel,
    QualificationRecord,
    ReconciliationRequiredError,
    ReleasePermitError,
    RiskContext,
    RiskLimitError,
    RiskLimits,
    TimeInForce,
    UserStreamGapError,
    VenueEventKind,
    VenueOrderSpec,
    VenuePolicyError,
    VenueUserEvent,
)
from src.profit_system.execution.adapters import PaperOrderBook


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("50"),
        max_market_exposure=Decimal("50"),
        max_event_exposure=Decimal("50"),
        max_strategy_exposure=Decimal("50"),
        max_total_exposure=Decimal("50"),
        max_daily_loss=Decimal("10"),
        max_drawdown=Decimal("10"),
        max_open_orders=5,
        max_unmatched_leg_seconds=20,
        max_market_data_age_ms=500,
        max_user_stream_gap_seconds=30,
        max_order_rate=5,
    )


def _qualification(level: QualificationLevel) -> QualificationRecord:
    return QualificationRecord(
        strategy_id="strat-1",
        qualification=level,
        allowed_modes=(
            ExecutionMode.PAPER,
            ExecutionMode.SHADOW,
            ExecutionMode.LIVE_CANARY,
            ExecutionMode.LIVE_LIMITED,
        ),
        market_whitelist=("market-1",),
        risk_limits=_risk_limits(),
        evidence_digest="canary-evidence-1",
    )


def _order(*, client_order_id: str = "client-1") -> VenueOrderSpec:
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


def _paper_engine(level: QualificationLevel = QualificationLevel.PROBE_ALLOWED) -> ExecutionEngine:
    adapter = PaperVenueAdapter(
        market_data_provider=lambda order: PaperOrderBook(
            best_bid=Decimal("0.39"),
            best_ask=Decimal("0.41"),
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
            as_of=datetime.now(tz=UTC),
        )
    )
    return ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(level)}
        ),
    )


def _arm_live_stream(engine: ExecutionEngine, *, occurred_at: datetime | None = None) -> None:
    timestamp = datetime.now(tz=UTC) if occurred_at is None else occurred_at
    engine.ingest_user_event(
        VenueUserEvent(
            kind=VenueEventKind.BACKFILL,
            occurred_at=timestamp,
            cursor="live-backfill",
            detail="live_backfill_complete",
        )
    )


@pytest.mark.asyncio
async def test_review_and_confirm_produce_execution_ticket_and_snapshot() -> None:
    engine = _paper_engine()
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.PAPER,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    plan = await engine.review(intent, actor="alice", idempotency_key="review-1")
    ticket = await engine.confirm(
        plan.plan_id,
        ExecutionApproval(
            actor="alice",
            idempotency_key="confirm-1",
            plan_hash=plan.plan_hash,
            confirmation_phrase=plan.confirmation_phrase,
        ),
    )

    assert ticket.orders[0].status is OrderLifecycleStatus.LIVE
    snapshot = engine.snapshot()
    assert snapshot.orders[0].status is OrderLifecycleStatus.LIVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("orders", "risk_context", "error_match"),
    (
        (
            (
                replace(_order(client_order_id="client-1"), quantity=Decimal("75")),
                replace(
                    _order(client_order_id="client-2"),
                    side=OrderSide.SELL,
                    quantity=Decimal("75"),
                ),
            ),
            RiskContext(market_data_age_ms=100),
            "max_market_exposure exceeded for market-1",
        ),
        (
            (
                replace(_order(client_order_id="client-1"), quantity=Decimal("75")),
                replace(
                    _order(client_order_id="client-2"),
                    market_id="market-2",
                    quantity=Decimal("75"),
                ),
            ),
            RiskContext(
                market_exposure={"market-1": Decimal("20"), "market-2": Decimal("20")},
                event_exposure={"event-1": Decimal("5")},
                market_data_age_ms=100,
            ),
            "max_event_exposure exceeded for event-1",
        ),
    ),
)
async def test_review_aggregates_new_market_and_event_exposure_before_limit_check(
    orders: tuple[VenueOrderSpec, ...],
    risk_context: RiskContext,
    error_match: str,
) -> None:
    qualification = replace(
        _qualification(QualificationLevel.PROBE_ALLOWED),
        risk_limits=replace(
            _risk_limits(),
            max_strategy_exposure=Decimal("100"),
            max_total_exposure=Decimal("100"),
        ),
    )
    if {order.market_id for order in orders} != {"market-1"}:
        qualification = replace(
            qualification,
            market_whitelist=tuple(dict.fromkeys(order.market_id for order in orders)),
        )
    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("10"),
                ask_size=Decimal("10"),
                as_of=datetime.now(tz=UTC),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(records={"strat-1": qualification}),
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.PAPER,
        orders=orders,
        risk_context=risk_context,
    )

    with pytest.raises(RiskLimitError, match=error_match):
        await engine.review(intent, actor="alice", idempotency_key=f"review-{error_match}")


@pytest.mark.asyncio
async def test_qualification_gate_blocks_before_adapter_preflight() -> None:
    calls = {"preflight": 0}

    class CountingPaper(PaperVenueAdapter):
        async def preflight(self, request):
            calls["preflight"] += 1
            return await super().preflight(request)

    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=CountingPaper(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("10"),
                ask_size=Decimal("10"),
                as_of=datetime.now(tz=UTC),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.SHADOW_ONLY)}
        ),
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.LIVE_CANARY,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    with pytest.raises(QualificationError):
        await engine.review(intent, actor="alice", idempotency_key="review-1")
    assert calls["preflight"] == 0


@pytest.mark.asyncio
async def test_live_confirm_requires_exact_cryptographic_release_permit() -> None:
    adapter = PaperVenueAdapter(
        market_data_provider=lambda order: PaperOrderBook(
            best_bid=Decimal("0.39"),
            best_ask=Decimal("0.41"),
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
            as_of=datetime.now(tz=UTC),
        )
    )
    authority = HmacReleasePermitAuthority(secret=b"release-secret")
    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        permit_authority=authority,
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.LIVE_CANARY,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )
    _arm_live_stream(engine)
    plan = await engine.review(intent, actor="alice", idempotency_key="review-1")

    with pytest.raises(ReleasePermitError):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-1",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
                release_permit=None,
            ),
        )

    with pytest.raises(ReleasePermitError, match="exact required Canary checks"):
        authority.issue(
            plan=plan,
            qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
            confirmation_phrase=plan.confirmation_phrase,
            canary_checks={"ws": True, "backfill": True, "risk": True},
            mode=ExecutionMode.LIVE_CANARY,
        )

    permit = authority.issue(
        plan=plan,
        qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
        confirmation_phrase=plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )

    mode_engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        permit_authority=authority,
    )
    _arm_live_stream(mode_engine)
    mode_plan = await mode_engine.review(intent, actor="alice", idempotency_key="review-mode")
    mismatched_mode_permit = authority.issue(
        plan=mode_plan,
        qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
        confirmation_phrase=mode_plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_LIMITED,
    )
    with pytest.raises(ReleasePermitError, match="mode mismatch"):
        await mode_engine.confirm(
            mode_plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-mode-mismatch",
                plan_hash=mode_plan.plan_hash,
                confirmation_phrase=mode_plan.confirmation_phrase,
                release_permit=mismatched_mode_permit,
            ),
        )

    digest_engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        permit_authority=authority,
    )
    _arm_live_stream(digest_engine)
    digest_plan = await digest_engine.review(
        intent,
        actor="alice",
        idempotency_key="review-digest",
    )
    mismatched_digest_permit = authority.issue(
        plan=digest_plan,
        qualification=replace(
            _qualification(QualificationLevel.PROBE_ALLOWED),
            evidence_digest="different-evidence-digest",
        ),
        confirmation_phrase=digest_plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )
    with pytest.raises(ReleasePermitError, match="evidence digest mismatch"):
        await digest_engine.confirm(
            digest_plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-digest-mismatch",
                plan_hash=digest_plan.plan_hash,
                confirmation_phrase=digest_plan.confirmation_phrase,
                release_permit=mismatched_digest_permit,
            ),
        )

    ticket = await engine.confirm(
        plan.plan_id,
        ExecutionApproval(
            actor="alice",
            idempotency_key="confirm-2",
            plan_hash=plan.plan_hash,
            confirmation_phrase=plan.confirmation_phrase,
            release_permit=permit,
        ),
    )
    assert ticket.orders[0].status is OrderLifecycleStatus.LIVE


@pytest.mark.asyncio
async def test_live_release_permit_enforces_expiry_future_clock_and_single_use() -> None:
    current = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return current[0]

    authority = HmacReleasePermitAuthority(secret=b"release-secret", clock=clock)
    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("10"),
                ask_size=Decimal("10"),
                as_of=clock(),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        permit_authority=authority,
        clock=clock,
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.LIVE_CANARY,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )
    _arm_live_stream(engine, occurred_at=clock())
    plan = await engine.review(intent, actor="alice", idempotency_key="review-time")
    permit = authority.issue(
        plan=plan,
        qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
        confirmation_phrase=plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )

    current[0] = current[0] + timedelta(minutes=6)
    with pytest.raises(ReleasePermitError, match="has expired"):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-expired",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
                release_permit=permit,
            ),
        )

    future_authority = HmacReleasePermitAuthority(
        secret=b"release-secret",
        clock=lambda: clock() + timedelta(minutes=1),
    )
    future_permit = future_authority.issue(
        plan=plan,
        qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
        confirmation_phrase=plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )
    current[0] = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    with pytest.raises(ReleasePermitError, match="issued in the future"):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-future",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
                release_permit=future_permit,
            ),
        )

    valid_permit = authority.issue(
        plan=plan,
        qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
        confirmation_phrase=plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )
    approval = ExecutionApproval(
        actor="alice",
        idempotency_key="confirm-valid",
        plan_hash=plan.plan_hash,
        confirmation_phrase=plan.confirmation_phrase,
        release_permit=valid_permit,
    )
    ticket = await engine.confirm(plan.plan_id, approval)
    replayed_ticket = await engine.confirm(plan.plan_id, approval)

    assert ticket == replayed_ticket
    with pytest.raises(ReleasePermitError, match="already been used"):
        authority.reserve(valid_permit, now=clock())


@pytest.mark.asyncio
async def test_live_release_permit_rejects_validity_window_longer_than_verifier_policy() -> None:
    current = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return current[0]

    verifier_authority = HmacReleasePermitAuthority(secret=b"release-secret", clock=clock)
    issuing_authority = HmacReleasePermitAuthority(
        secret=b"release-secret",
        clock=clock,
        max_age=timedelta(minutes=10),
    )
    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("10"),
                ask_size=Decimal("10"),
                as_of=clock(),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        permit_authority=verifier_authority,
        clock=clock,
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.LIVE_CANARY,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )
    _arm_live_stream(engine, occurred_at=clock())
    plan = await engine.review(intent, actor="alice", idempotency_key="review-window")
    long_window_permit = issuing_authority.issue(
        plan=plan,
        qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
        confirmation_phrase=plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )

    with pytest.raises(ReleasePermitError, match="validity window exceeds verifier policy"):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-window",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
                release_permit=long_window_permit,
            ),
        )


@pytest.mark.asyncio
async def test_confirm_fails_closed_when_gtd_order_has_expired_since_review() -> None:
    current = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return current[0]

    class CountingPaper(PaperVenueAdapter):
        def __init__(self) -> None:
            super().__init__(
                market_data_provider=lambda order: PaperOrderBook(
                    best_bid=Decimal("0.39"),
                    best_ask=Decimal("0.41"),
                    bid_size=Decimal("10"),
                    ask_size=Decimal("10"),
                    as_of=clock(),
                )
            )
            self.submit_calls = 0

        async def submit(self, *, orders, mode, idempotency_key):
            self.submit_calls += 1
            return await super().submit(
                orders=orders,
                mode=mode,
                idempotency_key=idempotency_key,
            )

    adapter = CountingPaper()
    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        clock=clock,
    )
    expires_at = clock() + timedelta(seconds=5)
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.PAPER,
        orders=(
            replace(
                _order(),
                time_in_force=TimeInForce.GTD,
                expires_at=expires_at,
            ),
        ),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    plan = await engine.review(intent, actor="alice", idempotency_key="review-gtd-expiry")
    current[0] = expires_at

    with pytest.raises(RiskLimitError, match="expired before confirm"):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-gtd-expiry",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
            ),
        )

    assert adapter.submit_calls == 0


@pytest.mark.asyncio
async def test_live_review_requires_authenticated_backfill_and_fresh_stream() -> None:
    current = [datetime(2026, 8, 30, 12, 0, tzinfo=UTC)]

    def clock() -> datetime:
        return current[0]

    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("10"),
                ask_size=Decimal("10"),
                as_of=clock(),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        permit_authority=HmacReleasePermitAuthority(secret=b"release-secret"),
        clock=clock,
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.LIVE_CANARY,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    with pytest.raises(ReconciliationRequiredError, match="user-stream backfill"):
        await engine.review(intent, actor="alice", idempotency_key="review-missing")

    _arm_live_stream(engine, occurred_at=clock())
    plan = await engine.review(intent, actor="alice", idempotency_key="review-fresh")
    assert plan.intent.mode is ExecutionMode.LIVE_CANARY

    current[0] = current[0] + timedelta(seconds=31)
    with pytest.raises(UserStreamGapError, match="authenticated user stream is stale"):
        await engine.review(intent, actor="alice", idempotency_key="review-stale")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocked_status",
    (
        OrderLifecycleStatus.SUBMITTING,
        OrderLifecycleStatus.AMBIGUOUS,
        OrderLifecycleStatus.CANCEL_REQUESTED,
        OrderLifecycleStatus.RECONCILING,
    ),
)
async def test_review_blocks_when_local_orders_need_resolution(
    blocked_status: OrderLifecycleStatus,
) -> None:
    blocked_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    engine = _paper_engine()
    engine._orders["stuck-order"] = OrderRecord(
        client_order_id="stuck-order",
        spec=_order(client_order_id="stuck-order"),
        status=blocked_status,
        plan_id="plan-stuck",
        venue_order_id="venue-stuck",
        updated_at=blocked_at,
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.PAPER,
        orders=(_order(client_order_id=f"candidate-{blocked_status.value.lower()}"),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    with pytest.raises(RiskLimitError, match="unresolved local order state blocks new orders"):
        await engine.review(intent, actor="alice", idempotency_key=f"review-{blocked_status.value}")


@pytest.mark.asyncio
async def test_cancel_and_replace_flow_creates_child_order() -> None:
    engine = _paper_engine()
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.PAPER,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )
    plan = await engine.review(intent, actor="alice", idempotency_key="review-1")
    ticket = await engine.confirm(
        plan.plan_id,
        ExecutionApproval(
            actor="alice",
            idempotency_key="confirm-1",
            plan_hash=plan.plan_hash,
            confirmation_phrase=plan.confirmation_phrase,
        ),
    )
    venue_order_id = ticket.orders[0].venue_order_id
    assert venue_order_id is not None

    replacement = _order(client_order_id="client-2")
    result = await engine.intervene(
        InterventionAction(
            kind=InterventionKind.REPLACE,
            order_ids=(venue_order_id,),
            replacement_orders=(replacement,),
            reason="quote moved",
        ),
        actor="alice",
        idempotency_key="replace-1",
    )

    assert any(order.client_order_id == "client-2" for order in result.orders)
    assert engine.snapshot().orders[-1].client_order_id == "client-2"


@pytest.mark.asyncio
async def test_confirm_journals_before_submit_and_reuses_original_key() -> None:
    class CountingPaper(PaperVenueAdapter):
        def __init__(self) -> None:
            super().__init__(
                market_data_provider=lambda order: PaperOrderBook(
                    best_bid=Decimal("0.39"),
                    best_ask=Decimal("0.41"),
                    bid_size=Decimal("10"),
                    ask_size=Decimal("10"),
                    as_of=datetime.now(tz=UTC),
                )
            )
            self.submit_calls = 0

        async def submit(self, *, orders, mode, idempotency_key):
            del orders, mode, idempotency_key
            self.submit_calls += 1
            return await super().submit(
                orders=(_order(),),
                mode=ExecutionMode.PAPER,
                idempotency_key="adapter-submit",
            )

    adapter = CountingPaper()
    journaled_statuses: list[tuple[OrderLifecycleStatus, ...]] = []

    def journal(*, plan, approval, orders) -> None:
        del plan, approval
        assert adapter.submit_calls == 0
        journaled_statuses.append(tuple(order.status for order in orders))

    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        submit_journal=journal,
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.PAPER,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    plan = await engine.review(intent, actor="alice", idempotency_key="review-submit-journal")
    approval = ExecutionApproval(
        actor="alice",
        idempotency_key="confirm-submit-journal",
        plan_hash=plan.plan_hash,
        confirmation_phrase=plan.confirmation_phrase,
    )
    ticket = await engine.confirm(plan.plan_id, approval)
    replayed_ticket = await engine.confirm(plan.plan_id, approval)

    assert adapter.submit_calls == 1
    assert ticket == replayed_ticket
    assert journaled_statuses == [(OrderLifecycleStatus.SUBMITTING,)]

    with pytest.raises(IdempotencyConflictError, match="already been confirmed"):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-submit-journal-2",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
            ),
        )
    assert adapter.submit_calls == 1


@pytest.mark.asyncio
async def test_submit_journal_failure_blocks_adapter_submit_and_restores_reviewed_state() -> None:
    class CountingPaper(PaperVenueAdapter):
        def __init__(self) -> None:
            super().__init__(
                market_data_provider=lambda order: PaperOrderBook(
                    best_bid=Decimal("0.39"),
                    best_ask=Decimal("0.41"),
                    bid_size=Decimal("10"),
                    ask_size=Decimal("10"),
                    as_of=datetime.now(tz=UTC),
                )
            )
            self.submit_calls = 0

        async def submit(self, *, orders, mode, idempotency_key):
            del orders, mode, idempotency_key
            self.submit_calls += 1
            return await super().submit(
                orders=(_order(),),
                mode=ExecutionMode.PAPER,
                idempotency_key="adapter-submit",
            )

    adapter = CountingPaper()

    def failing_journal(*, plan, approval, orders) -> None:
        del plan, approval
        assert tuple(order.status for order in orders) == (OrderLifecycleStatus.SUBMITTING,)
        raise RuntimeError("journal write failed")

    engine = ExecutionEngine(
        mode=ExecutionMode.PAPER,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        submit_journal=failing_journal,
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.PAPER,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    plan = await engine.review(intent, actor="alice", idempotency_key="review-journal-failure")
    with pytest.raises(RuntimeError, match="journal write failed"):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-journal-failure",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
            ),
        )

    assert adapter.submit_calls == 0
    assert engine.snapshot().orders[0].status is OrderLifecycleStatus.REVIEWED


@pytest.mark.asyncio
async def test_live_definitive_submit_failure_rolls_back_and_releases_permit() -> None:
    current = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

    class RetryOncePaper(PaperVenueAdapter):
        def __init__(self) -> None:
            super().__init__(
                market_data_provider=lambda order: PaperOrderBook(
                    best_bid=Decimal("0.39"),
                    best_ask=Decimal("0.41"),
                    bid_size=Decimal("10"),
                    ask_size=Decimal("10"),
                    as_of=current,
                )
            )
            self.submit_calls = 0

        async def submit(self, *, orders, mode, idempotency_key):
            self.submit_calls += 1
            if self.submit_calls == 1:
                raise VenuePolicyError(
                    "venue rejected submission with a retryable policy response",
                    retry_after_seconds=3,
                )
            return await super().submit(
                orders=orders,
                mode=mode,
                idempotency_key=idempotency_key,
            )

    adapter = RetryOncePaper()
    authority = HmacReleasePermitAuthority(secret=b"release-secret", clock=lambda: current)
    journaled_statuses: list[tuple[OrderLifecycleStatus, ...]] = []

    def journal(*, plan, approval, orders) -> None:
        del plan, approval
        journaled_statuses.append(tuple(order.status for order in orders))

    engine = ExecutionEngine(
        mode=ExecutionMode.LIVE_CANARY,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
        permit_authority=authority,
        clock=lambda: current,
        submit_journal=journal,
    )
    _arm_live_stream(engine, occurred_at=current)
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.LIVE_CANARY,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )
    plan = await engine.review(intent, actor="alice", idempotency_key="review-retry")
    permit = authority.issue(
        plan=plan,
        qualification=_qualification(QualificationLevel.PROBE_ALLOWED),
        confirmation_phrase=plan.confirmation_phrase,
        canary_checks={check_id: True for check_id in readiness.REQUIRED_CANARY_CHECKS},
        mode=ExecutionMode.LIVE_CANARY,
    )
    approval = ExecutionApproval(
        actor="alice",
        idempotency_key="confirm-retry",
        plan_hash=plan.plan_hash,
        confirmation_phrase=plan.confirmation_phrase,
        release_permit=permit,
    )

    with pytest.raises(VenuePolicyError, match="retryable policy response"):
        await engine.confirm(plan.plan_id, approval)

    assert engine.snapshot().orders[0].status is OrderLifecycleStatus.REVIEWED
    ticket = await engine.confirm(plan.plan_id, approval)

    assert ticket.orders[0].status is OrderLifecycleStatus.LIVE
    assert adapter.submit_calls == 2
    assert journaled_statuses == [
        (OrderLifecycleStatus.SUBMITTING,),
        (OrderLifecycleStatus.REVIEWED,),
        (OrderLifecycleStatus.SUBMITTING,),
    ]


@pytest.mark.asyncio
async def test_shadow_confirm_fails_closed_before_adapter_submit() -> None:
    class CountingPaper(PaperVenueAdapter):
        def __init__(self) -> None:
            super().__init__(
                market_data_provider=lambda order: PaperOrderBook(
                    best_bid=Decimal("0.39"),
                    best_ask=Decimal("0.41"),
                    bid_size=Decimal("10"),
                    ask_size=Decimal("10"),
                    as_of=datetime.now(tz=UTC),
                )
            )
            self.submit_calls = 0

        async def submit(self, *, orders, mode, idempotency_key):
            self.submit_calls += 1
            return await super().submit(
                orders=orders,
                mode=mode,
                idempotency_key=idempotency_key,
            )

    adapter = CountingPaper()
    engine = ExecutionEngine(
        mode=ExecutionMode.SHADOW,
        venue=adapter,
        qualification_registry=InMemoryQualificationRegistry(
            records={"strat-1": _qualification(QualificationLevel.PROBE_ALLOWED)}
        ),
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=ExecutionMode.SHADOW,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    plan = await engine.review(intent, actor="alice", idempotency_key="review-shadow")
    with pytest.raises(RiskLimitError, match="shadow mode is observational only"):
        await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor="alice",
                idempotency_key="confirm-shadow",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
            ),
        )
    assert adapter.submit_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    (
        ExecutionMode.REPLAY,
        ExecutionMode.PAPER,
        ExecutionMode.SHADOW,
        ExecutionMode.LIVE_CANARY,
        ExecutionMode.LIVE_LIMITED,
    ),
)
async def test_stopped_qualification_blocks_every_new_order_mode(mode: ExecutionMode) -> None:
    engine = ExecutionEngine(
        mode=mode,
        venue=PaperVenueAdapter(
            market_data_provider=lambda order: PaperOrderBook(
                best_bid=Decimal("0.39"),
                best_ask=Decimal("0.41"),
                bid_size=Decimal("10"),
                ask_size=Decimal("10"),
                as_of=datetime.now(tz=UTC),
            )
        ),
        qualification_registry=InMemoryQualificationRegistry(
            records={
                "strat-1": QualificationRecord(
                    strategy_id="strat-1",
                    qualification=QualificationLevel.STOPPED,
                    allowed_modes=(
                        ExecutionMode.REPLAY,
                        ExecutionMode.PAPER,
                        ExecutionMode.SHADOW,
                        ExecutionMode.LIVE_CANARY,
                        ExecutionMode.LIVE_LIMITED,
                    ),
                    market_whitelist=("market-1",),
                    risk_limits=_risk_limits(),
                    evidence_digest="stopped-digest",
                )
            }
        ),
    )
    intent = ExecutionIntent(
        strategy_id="strat-1",
        mode=mode,
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
    )

    with pytest.raises(QualificationError, match="STOPPED"):
        await engine.review(intent, actor="alice", idempotency_key=f"review-{mode.value}")
