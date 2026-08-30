from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .config import RiskLimits, StrategyConfig
from .execution import (
    ExecutionMode,
    FillRecord,
    OrderLifecycleStatus,
    OrderSide,
    PaperVenueAdapter,
    PreflightCheck,
    TimeInForce,
    VenueCancelResult,
    VenueEventKind,
    VenueOrderSpec,
    VenueOrderState,
    VenuePreflightRequest,
    VenuePreflightResult,
    VenueReconciliation,
    VenueSubmitBatch,
    VenueSubmitResult,
    VenueUserEvent,
)
from .execution.adapters import PaperOrderBook
from .gates import ResearchGate, ShadowGate
from .opportunities import OpportunityEngine
from .orchestrator import (
    FixedTrackAcceptanceResult,
    ProfitPipelineOrchestrator,
    RealizedOutcome,
)
from .persistence import PersistenceStore
from .runtime import StrategyRuntime
from .serialization import canonical_sha256
from .strategies.arbitrage import ArbitrageStrategy
from .strategies.maker import MakerStrategy
from .strategies.models import PortfolioSnapshot as StrategyPortfolioSnapshot
from .strategies.models import Strategy
from .types import (
    EvidenceRef,
    OperatingMode,
    OpportunityCostEstimate,
    OpportunitySignal,
    OpportunitySnapshot,
    OrderType,
    ProjectedRisk,
    RecommendedLeg,
    StrategyQualification,
)
from .types import (
    OrderSide as CoreOrderSide,
)

TRACK_A_PREREG = "a" * 64
TRACK_A_CONFIG = "b" * 64
TRACK_B_PREREG = "c" * 64
TRACK_B_CONFIG = "d" * 64


def _risk_limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("100"),
        max_market_exposure=Decimal("200"),
        max_event_exposure=Decimal("250"),
        max_strategy_exposure=Decimal("250"),
        max_total_exposure=Decimal("300"),
        max_daily_loss=Decimal("50"),
        max_drawdown=Decimal("60"),
        max_open_orders=20,
        max_unmatched_leg_seconds=Decimal("15"),
        max_market_data_age_ms=1_000,
        max_user_stream_gap_seconds=Decimal("5"),
        max_order_rate=Decimal("5"),
    )


def _strategy_config(
    *,
    strategy_id: str,
    config_hash_seed: str,
) -> StrategyConfig:
    return StrategyConfig(
        strategy_id=strategy_id,
        strategy_version="0.2.0",
        hypothesis_summary=f"Fixed acceptance fixture for {strategy_id}",
        operating_mode=OperatingMode.SHADOW,
        qualification=StrategyQualification.SHADOW_ONLY,
        allowed_modes=(
            OperatingMode.RESEARCH,
            OperatingMode.REPLAY,
            OperatingMode.PAPER,
            OperatingMode.SHADOW,
        ),
        min_net_edge=Decimal("0.01"),
        min_trade_size=Decimal("5"),
        risk_penalty_multiplier=Decimal("1"),
        max_candidate_ttl_seconds=Decimal("120"),
        risk_limits=_risk_limits(),
        code_version_hash=config_hash_seed,
    )


def _projected_risk(order_notional: Decimal, *, open_orders: int = 1) -> ProjectedRisk:
    return ProjectedRisk(
        order_notional=order_notional,
        projected_market_exposure=Decimal("30"),
        projected_event_exposure=Decimal("40"),
        projected_strategy_exposure=Decimal("45"),
        projected_total_exposure=Decimal("50"),
        projected_daily_loss=Decimal("3"),
        projected_drawdown=Decimal("4"),
        projected_open_orders=open_orders,
        projected_unmatched_leg_seconds=Decimal("2"),
    )


def _track_a_snapshot() -> OpportunitySnapshot:
    detected_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    signal = OpportunitySignal(
        strategy_id="track_a.complete_set.arbitrage.v0_2",
        detected_at=detected_at,
        expires_at=detected_at + timedelta(seconds=45),
        market_ids=("track-a-m1", "track-a-m2"),
        token_ids=("track-a-y1", "track-a-y2"),
        event_id="track-a-event",
        recommended_legs=(
            RecommendedLeg(
                market_id="track-a-m1",
                token_id="track-a-y1",
                side=CoreOrderSide.BUY,
                price=Decimal("0.41"),
                size=Decimal("10"),
                order_type=OrderType.LIMIT,
                post_only=False,
            ),
            RecommendedLeg(
                market_id="track-a-m2",
                token_id="track-a-y2",
                side=CoreOrderSide.BUY,
                price=Decimal("0.47"),
                size=Decimal("10"),
                order_type=OrderType.LIMIT,
                post_only=False,
            ),
        ),
        expected_gross_edge=Decimal("1.90"),
        expected_incentive=Decimal("0.20"),
        costs=OpportunityCostEstimate(
            expected_taker_fee=Decimal("0.10"),
            expected_builder_fee=Decimal("0.05"),
            expected_slippage=Decimal("0.03"),
            expected_adverse_selection=Decimal("0.02"),
            expected_inventory_cost=Decimal("0.01"),
            expected_unwind_cost=Decimal("0.04"),
            uncertainty_buffer=Decimal("0.05"),
        ),
        estimated_capacity=Decimal("10"),
        max_loss=Decimal("0.60"),
        confidence=Decimal("0.93"),
        lower_confidence_margin=Decimal("0.10"),
        projected_risk=_projected_risk(Decimal("8.80")),
        evidence_refs=(
            EvidenceRef(kind="research", ref="track-a/baseline"),
            EvidenceRef(kind="shadow", ref="track-a/day17"),
        ),
    )
    return OpportunitySnapshot(
        captured_at=detected_at + timedelta(seconds=5),
        signals=(signal,),
        market_data_age_ms=200,
        user_stream_gap_seconds=Decimal("1"),
        open_order_count=0,
        current_order_rate=Decimal("0.2"),
    )


def _track_b_snapshot() -> OpportunitySnapshot:
    detected_at = datetime(2026, 8, 30, 12, 5, tzinfo=UTC)
    signal = OpportunitySignal(
        strategy_id="track_b.maker.quote.v0_2",
        detected_at=detected_at,
        expires_at=detected_at + timedelta(seconds=90),
        market_ids=("track-b-m1",),
        token_ids=("track-b-y1",),
        event_id="track-b-event",
        recommended_legs=(
            RecommendedLeg(
                market_id="track-b-m1",
                token_id="track-b-y1",
                side=CoreOrderSide.BUY,
                price=Decimal("0.48"),
                size=Decimal("12"),
                order_type=OrderType.LIMIT,
                post_only=True,
            ),
        ),
        expected_gross_edge=Decimal("0.90"),
        expected_incentive=Decimal("0.06"),
        costs=OpportunityCostEstimate(
            expected_taker_fee=Decimal("0.04"),
            expected_builder_fee=Decimal("0.02"),
            expected_slippage=Decimal("0.01"),
            expected_adverse_selection=Decimal("0.08"),
            expected_inventory_cost=Decimal("0.03"),
            expected_unwind_cost=Decimal("0.00"),
            uncertainty_buffer=Decimal("0.02"),
        ),
        estimated_capacity=Decimal("12"),
        max_loss=Decimal("0.90"),
        confidence=Decimal("0.88"),
        lower_confidence_margin=Decimal("0.06"),
        projected_risk=_projected_risk(Decimal("5.76"), open_orders=2),
        evidence_refs=(
            EvidenceRef(kind="research", ref="track-b/baseline"),
            EvidenceRef(kind="shadow", ref="track-b/day14"),
        ),
    )
    return OpportunitySnapshot(
        captured_at=detected_at + timedelta(seconds=5),
        signals=(signal,),
        market_data_age_ms=250,
        user_stream_gap_seconds=Decimal("1"),
        open_order_count=0,
        current_order_rate=Decimal("0.25"),
    )


def _track_a_research_evidence() -> dict[str, Any]:
    return {
        "strategy_id": "track_a.complete_set.arbitrage.v0_2",
        "strategy_version": "0.2.0",
        "preregistration_sha256": TRACK_A_PREREG,
        "config_sha256": TRACK_A_CONFIG,
        "preregistered": True,
        "chronological_oos": True,
        "no_future_leakage": True,
        "baseline_present": True,
        "ablation_present": True,
        "thresholds_frozen": True,
        "stress_cost_multiplier": "1.25",
        "stress_slippage_multiplier": "1.25",
        "stress_fill_rate_multiplier": "0.75",
        "baseline_expected_net_edge": "1.90",
        "ablation_expected_net_edge": "0.40",
        "stressed_expected_net_edge": "0.75",
        "candidate_count": 64,
        "executable_count": 28,
        "evidence_refs": ("research://track-a/prereg", "research://track-a/stress"),
    }


def _track_b_research_evidence() -> dict[str, Any]:
    return {
        "strategy_id": "track_b.maker.quote.v0_2",
        "strategy_version": "0.2.0",
        "preregistration_sha256": TRACK_B_PREREG,
        "config_sha256": TRACK_B_CONFIG,
        "preregistered": True,
        "chronological_oos": True,
        "no_future_leakage": True,
        "baseline_present": True,
        "ablation_present": True,
        "thresholds_frozen": True,
        "stress_cost_multiplier": "1.25",
        "stress_slippage_multiplier": "1.25",
        "stress_fill_rate_multiplier": "0.75",
        "baseline_expected_net_edge": "0.41",
        "ablation_expected_net_edge": "0.05",
        "stressed_expected_net_edge": "0.08",
        "candidate_count": 57,
        "executable_count": 21,
        "evidence_refs": ("research://track-b/prereg", "research://track-b/stress"),
    }


def _track_a_shadow_evidence() -> dict[str, Any]:
    return {
        "strategy_id": "track_a.complete_set.arbitrage.v0_2",
        "strategy_version": "0.2.0",
        "preregistration_sha256": TRACK_A_PREREG,
        "config_sha256": TRACK_A_CONFIG,
        "observation_days": 17,
        "candidate_count": 58,
        "executable_count": 24,
        "all_decisions_recorded": True,
        "expected_net_edge": "1.35",
        "data_integrity_ok": True,
        "state_drift_ok": True,
        "thresholds_frozen": True,
        "evidence_refs": ("shadow://track-a/day01", "shadow://track-a/day17"),
    }


def _track_b_shadow_evidence() -> dict[str, Any]:
    return {
        "strategy_id": "track_b.maker.quote.v0_2",
        "strategy_version": "0.2.0",
        "preregistration_sha256": TRACK_B_PREREG,
        "config_sha256": TRACK_B_CONFIG,
        "observation_days": 14,
        "candidate_count": 53,
        "executable_count": 20,
        "all_decisions_recorded": True,
        "expected_net_edge": "-0.12",
        "data_integrity_ok": True,
        "state_drift_ok": True,
        "thresholds_frozen": True,
        "evidence_refs": ("shadow://track-b/day01", "shadow://track-b/day14"),
    }


def _track_a_replay_adapter(snapshot: OpportunitySnapshot) -> tuple[Any, ...]:
    signal = snapshot.signals[0]
    submitted_at = snapshot.captured_at + timedelta(seconds=1)
    results = VenueSubmitBatch(
        results=(
            VenueSubmitResult(
                client_order_id="pending",
                status=OrderLifecycleStatus.FILLED,
                venue_order_id="pending",
                filled_quantity=Decimal("10"),
            ),
            VenueSubmitResult(
                client_order_id="pending-2",
                status=OrderLifecycleStatus.FILLED,
                venue_order_id="pending-2",
                filled_quantity=Decimal("10"),
            ),
        ),
        submitted_at=submitted_at,
    )
    reconciliation = VenueReconciliation(
        open_orders=(),
        fills=(
            FillRecord(
                fill_id="track-a-replay-fill-1",
                venue_order_id="pending",
                market_id=signal.market_ids[0],
                token_id=signal.token_ids[0],
                side=OrderSide.BUY,
                price=Decimal("0.41"),
                quantity=Decimal("10"),
                fee=Decimal("0"),
                occurred_at=submitted_at,
                raw_status="FILLED",
            ),
            FillRecord(
                fill_id="track-a-replay-fill-2",
                venue_order_id="pending-2",
                market_id=signal.market_ids[1],
                token_id=signal.token_ids[1],
                side=OrderSide.BUY,
                price=Decimal("0.47"),
                quantity=Decimal("10"),
                fee=Decimal("0"),
                occurred_at=submitted_at,
                raw_status="FILLED",
            ),
        ),
        as_of=submitted_at + timedelta(seconds=1),
        cash_balance=Decimal("991.00"),
        allowance_available=Decimal("991.00"),
    )
    return results, reconciliation


def _track_b_replay_adapter(snapshot: OpportunitySnapshot) -> tuple[Any, ...]:
    signal = snapshot.signals[0]
    submitted_at = snapshot.captured_at + timedelta(seconds=1)
    results = VenueSubmitBatch(
        results=(
            VenueSubmitResult(
                client_order_id="pending",
                status=OrderLifecycleStatus.LIVE,
                venue_order_id="pending",
            ),
            VenueSubmitResult(
                client_order_id="pending-2",
                status=OrderLifecycleStatus.LIVE,
                venue_order_id="pending-2",
            ),
        ),
        submitted_at=submitted_at,
    )
    reconciliation = VenueReconciliation(
        open_orders=(
            VenueOrderState(
                venue_order_id="pending",
                client_order_id="pending",
                market_id=signal.market_ids[0],
                token_id=signal.token_ids[0],
                side=OrderSide.BUY,
                quantity=Decimal("25"),
                limit_price=Decimal("0.465"),
                filled_quantity=Decimal("0"),
                status=OrderLifecycleStatus.LIVE,
                time_in_force=TimeInForce.GTD,
                expires_at=signal.expires_at,
                updated_at=submitted_at,
            ),
            VenueOrderState(
                venue_order_id="pending-2",
                client_order_id="pending-2",
                market_id=signal.market_ids[0],
                token_id=signal.token_ids[0],
                side=OrderSide.SELL,
                quantity=Decimal("25"),
                limit_price=Decimal("0.505"),
                filled_quantity=Decimal("0"),
                status=OrderLifecycleStatus.LIVE,
                time_in_force=TimeInForce.GTD,
                expires_at=signal.expires_at,
                updated_at=submitted_at,
            ),
        ),
        fills=(),
        as_of=submitted_at + timedelta(seconds=1),
        cash_balance=Decimal("1000.00"),
        allowance_available=Decimal("1000.00"),
    )
    return results, reconciliation


@dataclass(frozen=True, slots=True)
class FixedReplayScript:
    batch: VenueSubmitBatch
    reconciliation: VenueReconciliation


def _fixed_replay_script(fixture: FixedTrackFixture) -> FixedReplayScript:
    if fixture.track == "A":
        batch, reconciliation = _track_a_replay_adapter(fixture.snapshot)
    else:
        batch, reconciliation = _track_b_replay_adapter(fixture.snapshot)
    return FixedReplayScript(batch=batch, reconciliation=reconciliation)


def _rewrite_submit_batch(
    batch: VenueSubmitBatch,
    *,
    client_order_ids: tuple[str, ...],
) -> VenueSubmitBatch:
    results = []
    for client_order_id, result in zip(client_order_ids, batch.results, strict=True):
        venue_order_id = result.venue_order_id
        if venue_order_id == "pending":
            venue_order_id = f"replay-{client_order_id}"
        elif venue_order_id == "pending-2":
            venue_order_id = f"replay-{client_order_id}"
        results.append(
            VenueSubmitResult(
                client_order_id=client_order_id,
                status=result.status,
                venue_order_id=venue_order_id,
                filled_quantity=result.filled_quantity,
                reject_reason=result.reject_reason,
                retry_after_seconds=result.retry_after_seconds,
            )
        )
    return VenueSubmitBatch(results=tuple(results), submitted_at=batch.submitted_at)


def _rewrite_reconciliation(
    reconciliation: VenueReconciliation,
    *,
    client_order_ids: tuple[str, ...],
) -> VenueReconciliation:
    mapping = {
        "pending": f"replay-{client_order_ids[0]}",
        "pending-2": f"replay-{client_order_ids[1]}",
    }
    open_orders = []
    for state in reconciliation.open_orders:
        open_orders.append(
            VenueOrderState(
                venue_order_id=mapping.get(state.venue_order_id, state.venue_order_id),
                client_order_id=client_order_ids[0]
                if state.client_order_id == "pending"
                else client_order_ids[1],
                market_id=state.market_id,
                token_id=state.token_id,
                side=state.side,
                quantity=state.quantity,
                limit_price=state.limit_price,
                filled_quantity=state.filled_quantity,
                status=state.status,
                time_in_force=state.time_in_force,
                post_only=state.post_only,
                expires_at=state.expires_at,
                reject_reason=state.reject_reason,
                updated_at=state.updated_at,
            )
        )
    resolved_orders = []
    for state in reconciliation.resolved_orders:
        resolved_orders.append(
            VenueOrderState(
                venue_order_id=mapping.get(state.venue_order_id, state.venue_order_id),
                client_order_id=client_order_ids[0]
                if state.client_order_id == "pending"
                else client_order_ids[1],
                market_id=state.market_id,
                token_id=state.token_id,
                side=state.side,
                quantity=state.quantity,
                limit_price=state.limit_price,
                filled_quantity=state.filled_quantity,
                status=state.status,
                time_in_force=state.time_in_force,
                post_only=state.post_only,
                expires_at=state.expires_at,
                reject_reason=state.reject_reason,
                updated_at=state.updated_at,
            )
        )
    fills = []
    for fill in reconciliation.fills:
        fills.append(
            FillRecord(
                fill_id=fill.fill_id,
                venue_order_id=mapping.get(fill.venue_order_id, fill.venue_order_id),
                market_id=fill.market_id,
                token_id=fill.token_id,
                side=fill.side,
                price=fill.price,
                quantity=fill.quantity,
                fee=fill.fee,
                occurred_at=fill.occurred_at,
                raw_status=fill.raw_status,
            )
        )
    return VenueReconciliation(
        open_orders=tuple(open_orders),
        resolved_orders=tuple(resolved_orders),
        fills=tuple(fills),
        as_of=reconciliation.as_of,
        cash_balance=reconciliation.cash_balance,
        allowance_available=reconciliation.allowance_available,
        close_only=reconciliation.close_only,
        geoblocked=reconciliation.geoblocked,
        backfill_complete=reconciliation.backfill_complete,
        discrepancies=reconciliation.discrepancies,
    )


class FixedReplayVenueAdapter:
    def __init__(self, script: FixedReplayScript) -> None:
        self._script = script
        self._submissions: dict[str, VenueSubmitBatch] = {}
        self._reconciliation: VenueReconciliation | None = None

    async def preflight(self, request: VenuePreflightRequest) -> VenuePreflightResult:
        del request
        as_of = self._script.reconciliation.as_of
        return VenuePreflightResult(
            ok=True,
            checks=(
                PreflightCheck(
                    name="fixed_replay_fixture",
                    ok=True,
                    detail="deterministic replay fixture is ready",
                ),
            ),
            as_of=as_of,
            allowance_available=self._script.reconciliation.allowance_available,
            cash_available=self._script.reconciliation.cash_balance,
        )

    async def submit(
        self,
        *,
        orders: Sequence[VenueOrderSpec],
        mode: ExecutionMode,
        idempotency_key: str,
    ) -> VenueSubmitBatch:
        del mode
        client_order_ids = tuple(order.client_order_id for order in orders)
        batch = _rewrite_submit_batch(
            self._script.batch,
            client_order_ids=client_order_ids,
        )
        self._submissions[idempotency_key] = batch
        self._reconciliation = _rewrite_reconciliation(
            self._script.reconciliation,
            client_order_ids=client_order_ids,
        )
        return batch

    async def cancel(
        self,
        *,
        order_ids: Sequence[str],
        idempotency_key: str,
        reason: str | None = None,
    ) -> VenueCancelResult:
        del idempotency_key, reason
        return VenueCancelResult(
            canceled_ids=tuple(order_ids),
            unresolved_ids=(),
            canceled_at=self._script.reconciliation.as_of,
        )

    async def reconcile(
        self,
        *,
        open_order_ids: Sequence[str] | None = None,
        since_trade_id: str | None = None,
    ) -> VenueReconciliation:
        del open_order_ids, since_trade_id
        if self._reconciliation is not None:
            return self._reconciliation
        return self._script.reconciliation

    async def stream_user_events(
        self,
        *,
        markets: Sequence[str] | None = None,
        since_cursor: str | None = None,
    ) -> AsyncIterator[VenueUserEvent]:
        del markets
        if since_cursor == "__fixture_sentinel__":
            yield VenueUserEvent(
                kind=VenueEventKind.STATUS,
                occurred_at=self._script.reconciliation.as_of,
                cursor="fixed-replay-sentinel",
                detail="unused fixture sentinel",
            )


def build_fixed_replay_venue(fixture: FixedTrackFixture) -> FixedReplayVenueAdapter:
    return FixedReplayVenueAdapter(_fixed_replay_script(fixture))


def build_fixed_paper_venue() -> PaperVenueAdapter:
    return PaperVenueAdapter(
        market_data_provider=_paper_book_provider,
        cash_available=Decimal("1000"),
        allowance_available=Decimal("1000"),
    )


@dataclass(frozen=True, slots=True)
class FixedTrackFixture:
    track: str
    config: StrategyConfig
    strategy: Strategy
    snapshot: OpportunitySnapshot
    portfolio: StrategyPortfolioSnapshot
    research_evidence: dict[str, Any]
    shadow_evidence: dict[str, Any]
    replay_outcome: RealizedOutcome | None
    paper_outcome: RealizedOutcome | None


def build_track_a_fixture() -> FixedTrackFixture:
    return FixedTrackFixture(
        track="A",
        config=_strategy_config(
            strategy_id="track_a.complete_set.arbitrage.v0_2",
            config_hash_seed="a" * 64,
        ),
        strategy=ArbitrageStrategy(),
        snapshot=_track_a_snapshot(),
        portfolio=StrategyPortfolioSnapshot(available_capital=Decimal("100")),
        research_evidence=_track_a_research_evidence(),
        shadow_evidence=_track_a_shadow_evidence(),
        replay_outcome=RealizedOutcome(
            source_id="track-a-replay-realized",
            token_id="track-a-y1",
            gross_trading_pnl=Decimal("1.40"),
            fee_amount=Decimal("0.10"),
            slippage_amount=Decimal("0.03"),
            unwind_amount=Decimal("0.04"),
            incentive_confirmed=Decimal("0.20"),
        ),
        paper_outcome=RealizedOutcome(
            source_id="track-a-paper-realized",
            token_id="track-a-y1",
            gross_trading_pnl=Decimal("1.10"),
            fee_amount=Decimal("0.08"),
            slippage_amount=Decimal("0.02"),
            unwind_amount=Decimal("0.03"),
            incentive_confirmed=Decimal("0.10"),
        ),
    )


def build_track_b_fixture() -> FixedTrackFixture:
    return FixedTrackFixture(
        track="B",
        config=_strategy_config(
            strategy_id="track_b.maker.quote.v0_2",
            config_hash_seed="b" * 64,
        ),
        strategy=MakerStrategy(),
        snapshot=_track_b_snapshot(),
        portfolio=StrategyPortfolioSnapshot(
            available_capital=Decimal("100"),
            positions={"track-b-y1": Decimal("8")},
        ),
        research_evidence=_track_b_research_evidence(),
        shadow_evidence=_track_b_shadow_evidence(),
        replay_outcome=None,
        paper_outcome=None,
    )


def _paper_book_provider(order: VenueOrderSpec) -> PaperOrderBook | None:
    now = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    if order.strategy_id == "track_a.complete_set.arbitrage.v0_2":
        return PaperOrderBook(
            best_bid=Decimal("0.39"),
            best_ask=Decimal("0.40"),
            bid_size=Decimal("20"),
            ask_size=Decimal("20"),
            as_of=now,
        )
    return PaperOrderBook(
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.51"),
        bid_size=Decimal("50"),
        ask_size=Decimal("50"),
        as_of=now,
    )


async def run_fixed_track(base_dir: Path, fixture: FixedTrackFixture) -> FixedTrackAcceptanceResult:
    store = PersistenceStore(base_dir / f"track-{fixture.track.lower()}.db")
    orchestrator = ProfitPipelineOrchestrator(
        config=fixture.config,
        strategy=fixture.strategy,
        store=store,
        opportunity_engine=OpportunityEngine(),
        runtime=StrategyRuntime(fixture.strategy),
        now=lambda: fixture.snapshot.captured_at,
    )
    try:
        research = orchestrator.evaluate_gate(
            gate=ResearchGate(),
            evidence=fixture.research_evidence,
            state_key=f"acceptance:track_{fixture.track.lower()}:research",
            phase="research",
        )
        replay = await orchestrator.run_execution_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            mode=ExecutionMode.REPLAY,
            venue=build_fixed_replay_venue(fixture),
            phase="replay",
            actor="acceptance",
            realized_outcome=fixture.replay_outcome,
        )
        paper = await orchestrator.run_execution_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            mode=ExecutionMode.PAPER,
            venue=build_fixed_paper_venue(),
            phase="paper",
            actor="acceptance",
            realized_outcome=fixture.paper_outcome,
        )
        shadow = await orchestrator.run_shadow_stage(
            snapshot=fixture.snapshot,
            portfolio=fixture.portfolio,
            phase="shadow",
            submit_probe=build_fixed_paper_venue(),
        )
        shadow_gate = orchestrator.evaluate_gate(
            gate=ShadowGate(),
            evidence=fixture.shadow_evidence,
            state_key=f"acceptance:track_{fixture.track.lower()}:shadow",
            phase="shadow",
        )
        return FixedTrackAcceptanceResult(
            track=fixture.track,
            strategy_id=fixture.config.strategy_id,
            research=research,
            replay=replay,
            paper=paper,
            shadow=shadow,
            shadow_gate=shadow_gate,
        )
    finally:
        store.close()


def run_fixed_track_sync(base_dir: Path, fixture: FixedTrackFixture) -> FixedTrackAcceptanceResult:
    return asyncio.run(run_fixed_track(base_dir, fixture))


def run_fixed_acceptance_suite(base_dir: Path) -> dict[str, FixedTrackAcceptanceResult]:
    return {
        "A": run_fixed_track_sync(base_dir, build_track_a_fixture()),
        "B": run_fixed_track_sync(base_dir, build_track_b_fixture()),
    }


def run_fixed_acceptance_suite_in_tempdir() -> dict[str, FixedTrackAcceptanceResult]:
    with TemporaryDirectory(prefix="profit-system-acceptance-") as raw_dir:
        return run_fixed_acceptance_suite(Path(raw_dir))


def acceptance_summary(results: dict[str, FixedTrackAcceptanceResult]) -> dict[str, Any]:
    ordered_results = (results["A"], results["B"])
    return {
        "report_version": "profit-system-v0.2.acceptance.v2",
        "tracks": [
            {
                "track": result.track,
                "strategy_id": result.strategy_id,
                "final_status": result.final_status,
                "research": result.research.decision.to_document(),
                "replay_signal_signature": result.replay.signal_signature,
                "paper_signal_signature": result.paper.signal_signature,
                "shadow_signal_signature": result.shadow.signal_signature,
                "shadow_gate": result.shadow_gate.decision.to_document(),
                "replay_evidence_digest": result.replay.evidence_digest,
                "paper_evidence_digest": result.paper.evidence_digest,
                "shadow_evidence_digest": result.shadow.evidence_digest,
                "research_evidence_digest": result.research.evidence_digest,
                "shadow_gate_evidence_digest": result.shadow_gate.evidence_digest,
                "paper_realized_net_pnl": _paper_realized_net_pnl(result),
            }
            for result in ordered_results
        ],
        "bundle_digest": canonical_sha256(
            {
                key: {
                    "track": value.track,
                    "final_status": value.final_status,
                    "research": value.research.decision.to_document(),
                    "shadow_gate": value.shadow_gate.decision.to_document(),
                    "replay_signal_signature": value.replay.signal_signature,
                    "paper_signal_signature": value.paper.signal_signature,
                    "shadow_signal_signature": value.shadow.signal_signature,
                }
                for key, value in results.items()
            }
        ),
    }


def _paper_realized_net_pnl(result: FixedTrackAcceptanceResult) -> str | None:
    if result.paper.ledger_snapshot is None:
        return None
    return str(result.paper.ledger_snapshot.realized_net_pnl)
