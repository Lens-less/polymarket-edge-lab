from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any, Protocol, cast

from .config import StrategyConfig
from .execution import (
    ExecutionApproval,
    ExecutionEngine,
    ExecutionIntent,
    ExecutionMode,
    ExecutionPlan,
    ExecutionSnapshot,
    ExecutionTicket,
    InMemoryQualificationRegistry,
    InterventionAction,
    InterventionKind,
    OrderRecord,
    OrderSide,
    QualificationLevel,
    QualificationRecord,
    ReplayVenueAdapter,
    RiskContext,
    TimeInForce,
    VenueAdapter,
    VenueOrderSpec,
    VenueReconciliation,
    VenueUserEvent,
)
from .execution import (
    RiskLimits as ExecutionRiskLimits,
)
from .gates import GateDecision, ResearchGate, ShadowGate
from .opportunities import OpportunityEngine
from .persistence import (
    ExecutionPlanRecord,
    OpportunityRecord,
    PersistenceStore,
    PnLAttributionRecord,
    ReconciliationRunRecord,
    StrategyDecisionRecord,
    VersionStamp,
)
from .persistence import (
    FillRecord as PersistedFillRecord,
)
from .persistence import (
    OrderRecord as PersistedOrderRecord,
)
from .portfolio import PerformanceReport, PortfolioBook
from .portfolio import PortfolioSnapshot as LedgerSnapshot
from .runtime import StrategyRuntime
from .serialization import canonical_sha256, to_canonical_jsonable
from .strategies.models import (
    NormalizedOpportunity,
    RuntimeMode,
    Strategy,
    StrategyDecision,
    StrategyFeedback,
)
from .strategies.models import (
    PortfolioSnapshot as StrategyPortfolioSnapshot,
)
from .types import OpportunityCandidate, OpportunityExplanation, OpportunitySnapshot


class EvidenceStore(Protocol):
    def set_system_state(self, key: str, value: Any, *, idempotency_key: str) -> object: ...

    def get_system_state(self, key: str, default: Any = None) -> Any: ...


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ceil_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _runtime_mode(mode: ExecutionMode | RuntimeMode) -> RuntimeMode:
    if isinstance(mode, RuntimeMode):
        return mode
    return RuntimeMode(mode.value)


def _execution_mode(mode: ExecutionMode | RuntimeMode) -> ExecutionMode:
    if isinstance(mode, ExecutionMode):
        return mode
    return ExecutionMode(mode.value)


def _qualification_level(config: StrategyConfig) -> QualificationLevel:
    return QualificationLevel(config.qualification.value)


def _execution_risk_limits(config: StrategyConfig) -> ExecutionRiskLimits:
    limits = config.risk_limits
    return ExecutionRiskLimits(
        max_order_notional=limits.max_order_notional,
        max_market_exposure=limits.max_market_exposure,
        max_event_exposure=limits.max_event_exposure,
        max_strategy_exposure=limits.max_strategy_exposure,
        max_total_exposure=limits.max_total_exposure,
        max_daily_loss=(
            limits.max_daily_loss if limits.max_daily_loss > Decimal("0") else Decimal("0.000001")
        ),
        max_drawdown=(
            limits.max_drawdown if limits.max_drawdown > Decimal("0") else Decimal("0.000001")
        ),
        max_open_orders=max(int(limits.max_open_orders), 1),
        max_unmatched_leg_seconds=max(_ceil_decimal(limits.max_unmatched_leg_seconds), 1),
        max_market_data_age_ms=max(int(limits.max_market_data_age_ms), 1),
        max_user_stream_gap_seconds=max(_ceil_decimal(limits.max_user_stream_gap_seconds), 1),
        max_order_rate=max(_ceil_decimal(limits.max_order_rate), 1),
    )


def build_qualification(
    config: StrategyConfig,
    *,
    evidence_digest: str,
) -> QualificationRecord:
    allowed_modes = tuple(
        _execution_mode(RuntimeMode(mode.value))
        for mode in config.allowed_modes
        if mode.value in ExecutionMode._value2member_map_
    )
    return QualificationRecord(
        strategy_id=config.strategy_id,
        qualification=_qualification_level(config),
        allowed_modes=allowed_modes or (ExecutionMode.REPLAY,),
        market_whitelist=config.market_whitelist,
        risk_limits=_execution_risk_limits(config),
        evidence_digest=evidence_digest,
        valid_until=config.effective_until,
    )


def _version_stamp(config: StrategyConfig) -> VersionStamp:
    return VersionStamp(
        strategy_id=config.strategy_id,
        strategy_config_version=config.config_hash,
        code_version=config.code_version_hash,
    )


def _primary_market(candidate: OpportunityCandidate) -> str:
    return str(candidate.market_ids[0])


def _primary_token(candidate: OpportunityCandidate) -> str:
    return str(candidate.token_ids[0])


def decision_to_intent(
    *,
    candidate: OpportunityCandidate,
    decision: StrategyDecision,
    mode: ExecutionMode,
    risk_context: RiskContext | None = None,
) -> ExecutionIntent:
    event_id = candidate.event_id
    candidate_expiry = candidate.expires_at.astimezone(UTC)
    orders: list[VenueOrderSpec] = []
    for index, leg in enumerate(decision.legs, start=1):
        tif = TimeInForce(leg.time_in_force.upper())
        expires_at = None
        if tif is TimeInForce.GTD:
            if leg.expires_at_ms is not None:
                expires_at = datetime.fromtimestamp(leg.expires_at_ms / 1000, tz=UTC)
            else:
                expires_at = candidate_expiry
        orders.append(
            VenueOrderSpec(
                client_order_id=f"{candidate.opportunity_id}:{mode.value.lower()}:{index}",
                strategy_id=decision.strategy_id,
                market_id=leg.market_id,
                token_id=leg.token_id,
                event_id=event_id,
                side=OrderSide(leg.side.upper()),
                quantity=leg.quantity,
                limit_price=leg.price,
                time_in_force=tif,
                post_only=bool(leg.post_only),
                expires_at=expires_at,
                metadata={
                    "leg_id": leg.leg_id,
                    "opportunity_id": candidate.opportunity_id,
                    "signal_signature": decision.signal_signature(),
                },
            )
        )
    return ExecutionIntent(
        strategy_id=decision.strategy_id,
        mode=mode,
        orders=tuple(orders),
        risk_context=risk_context or RiskContext(),
        note=decision.rationale,
    )


def _persist_opportunity(
    store: PersistenceStore,
    *,
    candidate: OpportunityCandidate,
    explanation: OpportunityExplanation,
    config: StrategyConfig,
    observed_at: datetime,
    cycle_id: str,
) -> None:
    try:
        store.record_opportunity(
            OpportunityRecord(
                opportunity_id=candidate.opportunity_id,
                market_id=_primary_market(candidate),
                token_id=_primary_token(candidate),
                expected_net_edge=candidate.expected_net_edge or Decimal("0"),
                status=candidate.status.value,
                observed_at=_isoformat(observed_at),
                metadata={
                    "snapshot_hash": candidate.snapshot_hash,
                    "config_hash": candidate.config_hash,
                    "code_version_hash": candidate.code_version_hash,
                    "rejection_reasons": list(candidate.rejection_reasons),
                    "gate_checks": [
                        {"name": check.name, "passed": check.passed, "detail": check.detail}
                        for check in explanation.gate_checks
                    ],
                    "evidence_refs": [
                        {"kind": ref.kind, "ref": ref.ref, "label": ref.label}
                        for ref in explanation.evidence_refs
                    ],
                },
                version=_version_stamp(config),
            ),
            idempotency_key=f"opportunity:{candidate.opportunity_id}",
        )
    except sqlite3.IntegrityError:
        pass


def _persist_decision(
    store: PersistenceStore,
    *,
    decision: StrategyDecision,
    config: StrategyConfig,
    cycle_id: str,
    decided_at: datetime,
    phase: str,
) -> str:
    decision_id = canonical_sha256(
        {
            "cycle_id": cycle_id,
            "strategy_id": decision.strategy_id,
            "opportunity_id": decision.opportunity_id,
            "phase": phase,
            "signal_signature": decision.signal_signature(),
        }
    )
    store.record_strategy_decision(
        StrategyDecisionRecord(
            decision_id=decision_id,
            opportunity_id=decision.opportunity_id,
            strategy_id=decision.strategy_id,
            decision=decision.status.value,
            expected_net_edge=decision.expected_net_edge,
            decided_at=_isoformat(decided_at),
            metadata={
                "phase": phase,
                "executable": decision.executable,
                "signal_signature": decision.signal_signature(),
                "rejection_reasons": list(decision.rejection_reasons),
                "rationale": decision.rationale,
                "legs": [leg.to_document() for leg in decision.legs],
                "attribution": dict(decision.attribution),
                "failure_plan": dict(decision.failure_plan),
                "runtime_context": dict(decision.runtime_context),
            },
            version=_version_stamp(config),
        ),
        idempotency_key=f"{cycle_id}:decision:{phase}",
    )
    return str(decision_id)


def _persist_execution_plan(
    store: PersistenceStore,
    *,
    config: StrategyConfig,
    cycle_id: str,
    decision_id: str,
    plan: ExecutionPlan,
    decision: StrategyDecision,
) -> None:
    preflight_payload = to_canonical_jsonable(
        {
            "ok": plan.preflight.ok,
            "checks": [
                {
                    "name": check.name,
                    "ok": check.ok,
                    "detail": check.detail,
                    "blocking": check.blocking,
                }
                for check in plan.preflight.checks
            ],
        }
    )
    intent_payload = to_canonical_jsonable(plan.intent.to_document())
    store.record_execution_plan(
        ExecutionPlanRecord(
            plan_id=plan.plan_id,
            decision_id=decision_id,
            strategy_id=decision.strategy_id,
            status="APPROVED" if plan.preflight.ok else "BLOCKED",
            expected_net_edge=decision.expected_net_edge,
            plan_kind=decision.metadata.get("opportunity_kind", "strategy"),
            created_at=_isoformat(plan.created_at),
            metadata={
                "cycle_id": cycle_id,
                "plan_hash": plan.plan_hash,
                "actor": plan.actor,
                "confirmation_phrase": plan.confirmation_phrase,
                "preflight": preflight_payload,
                "intent": intent_payload,
            },
            version=_version_stamp(config),
        ),
        idempotency_key=f"{cycle_id}:plan:{plan.plan_id}",
    )


def _persist_order_record(
    store: PersistenceStore,
    *,
    config: StrategyConfig,
    cycle_id: str,
    order: OrderRecord,
    idempotency_key: str,
) -> None:
    store.upsert_order(
        PersistedOrderRecord(
            order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            lifecycle_state=order.status.value,
            market_id=order.spec.market_id,
            token_id=order.spec.token_id,
            side=order.spec.side.value,
            price=order.spec.limit_price,
            quantity=order.spec.quantity,
            filled_quantity=order.filled_quantity,
            remaining_quantity=order.spec.quantity - order.filled_quantity,
            order_role=str(order.spec.metadata.get("leg_id", "entry")),
            parent_order_id=order.parent_order_id,
            updated_at=_isoformat(order.updated_at),
            metadata={
                "cycle_id": cycle_id,
                "plan_id": order.plan_id,
                "status": order.status.value,
                "reject_reason": order.reject_reason,
            },
            version=_version_stamp(config),
        ),
        idempotency_key=idempotency_key,
    )


def _persist_execution_orders(
    store: PersistenceStore,
    *,
    config: StrategyConfig,
    cycle_id: str,
    ticket: ExecutionTicket,
) -> None:
    for order in ticket.orders:
        _persist_order_record(
            store,
            config=config,
            cycle_id=cycle_id,
            order=order,
            idempotency_key=f"{cycle_id}:order:{order.client_order_id}:{order.status.value}",
        )


def _persist_reconciliation(
    store: PersistenceStore,
    *,
    config: StrategyConfig,
    cycle_id: str,
    reconciliation: VenueReconciliation,
    phase: str,
) -> None:
    venue_snapshot = to_canonical_jsonable(
        {
            "as_of": _isoformat(reconciliation.as_of),
            "open_orders": [state.to_document() for state in reconciliation.open_orders],
            "fills": [
                {
                    "fill_id": fill.fill_id,
                    "venue_order_id": fill.venue_order_id,
                    "market_id": fill.market_id,
                    "token_id": fill.token_id,
                    "side": fill.side.value,
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                    "fee": str(fill.fee),
                    "occurred_at": _isoformat(fill.occurred_at),
                }
                for fill in reconciliation.fills
            ],
            "cash_balance": str(reconciliation.cash_balance),
            "allowance_available": str(reconciliation.allowance_available),
        }
    )
    report = {
        "open_order_count": len(reconciliation.open_orders),
        "fill_count": len(reconciliation.fills),
        "close_only": reconciliation.close_only,
        "geoblocked": reconciliation.geoblocked,
        "backfill_complete": reconciliation.backfill_complete,
        "discrepancies": list(reconciliation.discrepancies),
    }
    store.record_reconciliation_run(
        ReconciliationRunRecord(
            run_id=f"{cycle_id}:{phase}:reconcile",
            scope=phase,
            status="matched" if not reconciliation.discrepancies else "mismatch",
            venue_snapshot=cast(dict[str, Any], venue_snapshot),
            report=report,
            unresolved_count=len(reconciliation.discrepancies),
            created_at=_isoformat(reconciliation.as_of),
            version=_version_stamp(config),
        ),
        idempotency_key=f"{cycle_id}:reconcile:{phase}",
    )
    store.save_cash_snapshot(
        {
            "balances": [
                {
                    "currency": "USDC",
                    "total": str(reconciliation.cash_balance),
                    "available": str(reconciliation.cash_balance),
                    "reserved": "0",
                }
            ]
        },
        idempotency_key=f"{cycle_id}:cash:{phase}",
    )
    store.save_user_stream_snapshot(
        cast(
            dict[str, Any],
            to_canonical_jsonable(
                {
                    "as_of": _isoformat(reconciliation.as_of),
                    "open_orders": [state.to_document() for state in reconciliation.open_orders],
                }
            ),
        ),
        idempotency_key=f"{cycle_id}:user_stream:{phase}",
    )


def _persist_reconciliation_orders_and_fills(
    store: PersistenceStore,
    *,
    config: StrategyConfig,
    cycle_id: str,
    snapshot: ExecutionSnapshot,
    reconciliation: VenueReconciliation,
) -> None:
    for order in snapshot.orders:
        store.upsert_order(
            PersistedOrderRecord(
                order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
                lifecycle_state=order.status.value,
                market_id=order.spec.market_id,
                token_id=order.spec.token_id,
                side=order.spec.side.value,
                price=order.spec.limit_price,
                quantity=order.spec.quantity,
                filled_quantity=order.filled_quantity,
                remaining_quantity=order.spec.quantity - order.filled_quantity,
                order_role=str(order.spec.metadata.get("leg_id", "entry")),
                parent_order_id=order.parent_order_id,
                updated_at=_isoformat(order.updated_at),
                metadata={
                    "cycle_id": cycle_id,
                    "plan_id": order.plan_id,
                    "status": order.status.value,
                    "reject_reason": order.reject_reason,
                },
                version=_version_stamp(config),
            ),
            idempotency_key=f"{cycle_id}:order:{order.client_order_id}:snapshot",
        )
    for fill in reconciliation.fills:
        client_order_id = next(
            (
                order.client_order_id
                for order in snapshot.orders
                if order.venue_order_id == fill.venue_order_id
            ),
            fill.venue_order_id,
        )
        store.record_fill(
            PersistedFillRecord(
                fill_id=fill.fill_id,
                venue_fill_id=fill.fill_id,
                order_id=client_order_id,
                token_id=fill.token_id,
                side=fill.side.value,
                price=fill.price,
                quantity=fill.quantity,
                occurred_at=_isoformat(fill.occurred_at),
                fee_amount=fill.fee,
                liquidity_role="maker" if fill.side.value == "SELL" else "taker",
                metadata={
                    "cycle_id": cycle_id,
                    "market_id": fill.market_id,
                    "raw_status": fill.raw_status,
                },
                version=_version_stamp(config),
            ),
            idempotency_key=f"{cycle_id}:fill:{fill.fill_id}",
        )


@dataclass(frozen=True, slots=True)
class RealizedOutcome:
    source_id: str
    token_id: str
    gross_trading_pnl: Decimal
    fee_amount: Decimal = Decimal("0")
    slippage_amount: Decimal = Decimal("0")
    unwind_amount: Decimal = Decimal("0")
    onchain_amount: Decimal = Decimal("0")
    incentive_confirmed: Decimal = Decimal("0")
    incentive_pending: Decimal = Decimal("0")
    markout_bps: Decimal = Decimal("0")
    adverse_selection_bps: Decimal = Decimal("0")

    @property
    def realized_net_pnl(self) -> Decimal:
        return (
            self.gross_trading_pnl
            - self.fee_amount
            - self.slippage_amount
            - self.unwind_amount
            - self.onchain_amount
            + self.incentive_confirmed
        )

    def feedback_document(self, *, opportunity_id: str) -> dict[str, Any]:
        return {
            "opportunity_id": opportunity_id,
            "realized_net_edge": str(self.realized_net_pnl),
            "realized_pnl": str(self.realized_net_pnl),
            "markout_bps": str(self.markout_bps),
            "adverse_selection_bps": str(self.adverse_selection_bps),
            "fees": str(self.fee_amount),
            "spread_capture": str(self.gross_trading_pnl),
            "maker_rebates": "0",
            "liquidity_rewards": str(self.incentive_confirmed),
            "inventory_mark_to_market": str(self.unwind_amount),
            "realized_unwind_cost": str(self.unwind_amount),
        }


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    decision: GateDecision
    evidence: dict[str, Any]
    evidence_digest: str
    state_key: str


@dataclass(frozen=True, slots=True)
class StageResult:
    cycle_id: str
    phase: str
    runtime_mode: RuntimeMode
    execution_mode: ExecutionMode | None
    candidate: OpportunityCandidate
    explanation: OpportunityExplanation
    normalized_opportunity: NormalizedOpportunity
    decision: StrategyDecision
    decision_id: str
    signal_signature: str
    evidence_digest: str
    intent: ExecutionIntent | None = None
    execution_engine: ExecutionEngine | None = None
    ticket: ExecutionTicket | None = None
    execution_snapshot: ExecutionSnapshot | None = None
    reconciliation: VenueReconciliation | None = None
    ledger_snapshot: LedgerSnapshot | None = None
    performance: PerformanceReport | None = None
    feedback: StrategyFeedback | None = None
    realized_outcome: RealizedOutcome | None = None


class ProfitPipelineOrchestrator:
    def __init__(
        self,
        *,
        config: StrategyConfig,
        strategy: Strategy,
        store: PersistenceStore,
        opportunity_engine: OpportunityEngine | None = None,
        runtime: StrategyRuntime | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.strategy = strategy
        self.store = store
        self.opportunity_engine = opportunity_engine or OpportunityEngine()
        self.runtime = runtime or StrategyRuntime(strategy)
        self.now = now or (lambda: datetime.now(UTC))

    def scan_snapshot(
        self, snapshot: OpportunitySnapshot
    ) -> tuple[OpportunityCandidate, OpportunityExplanation]:
        ranked = self.opportunity_engine.scan(snapshot, (self.config,))
        for candidate in ranked.opportunities:
            if candidate.strategy_id == self.config.strategy_id:
                return candidate, self.opportunity_engine.explain(candidate.opportunity_id)
        raise KeyError(f"no candidate for {self.config.strategy_id}")

    def _cycle_id(
        self,
        *,
        phase: str,
        candidate: OpportunityCandidate,
        signal_signature: str,
    ) -> str:
        return str(
            canonical_sha256(
                {
                    "phase": phase,
                    "strategy_id": self.config.strategy_id,
                    "opportunity_id": candidate.opportunity_id,
                    "snapshot_hash": candidate.snapshot_hash,
                    "config_hash": self.config.config_hash,
                    "signal_signature": signal_signature,
                }
            )
        )

    async def run_shadow_stage(
        self,
        *,
        snapshot: OpportunitySnapshot,
        portfolio: StrategyPortfolioSnapshot,
        phase: str = "shadow",
        submit_probe: VenueAdapter | None = None,
    ) -> StageResult:
        candidate, explanation = self.scan_snapshot(snapshot)
        normalized = NormalizedOpportunity.from_candidate(candidate)
        decision = self.runtime.evaluate(normalized, portfolio, RuntimeMode.SHADOW)
        signal_signature = decision.signal_signature()
        cycle_id = self._cycle_id(
            phase=phase, candidate=candidate, signal_signature=signal_signature
        )
        observed_at = self.now()
        _persist_opportunity(
            self.store,
            candidate=candidate,
            explanation=explanation,
            config=self.config,
            observed_at=observed_at,
            cycle_id=cycle_id,
        )
        decision_id = _persist_decision(
            self.store,
            decision=decision,
            config=self.config,
            cycle_id=cycle_id,
            decided_at=observed_at,
            phase=phase,
        )
        if submit_probe is not None and hasattr(submit_probe, "submit"):
            # Shadow must not mutate the venue. We keep the dependency injectable so tests can
            # prove the branch never calls through to adapter.submit.
            pass
        evidence_digest = canonical_sha256(
            {
                "cycle_id": cycle_id,
                "candidate": candidate,
                "decision_signature": signal_signature,
                "phase": phase,
            }
        )
        return StageResult(
            cycle_id=cycle_id,
            phase=phase,
            runtime_mode=RuntimeMode.SHADOW,
            execution_mode=None,
            candidate=candidate,
            explanation=explanation,
            normalized_opportunity=normalized,
            decision=decision,
            decision_id=decision_id,
            signal_signature=signal_signature,
            evidence_digest=evidence_digest,
        )

    async def run_execution_stage(
        self,
        *,
        snapshot: OpportunitySnapshot,
        portfolio: StrategyPortfolioSnapshot,
        mode: ExecutionMode,
        venue: VenueAdapter,
        phase: str,
        actor: str,
        realized_outcome: RealizedOutcome | None = None,
        risk_context: RiskContext | None = None,
    ) -> StageResult:
        candidate, explanation = self.scan_snapshot(snapshot)
        normalized = NormalizedOpportunity.from_candidate(candidate)
        runtime_mode = _runtime_mode(mode)
        decision = self.runtime.evaluate(normalized, portfolio, runtime_mode)
        signal_signature = decision.signal_signature()
        cycle_id = self._cycle_id(
            phase=phase, candidate=candidate, signal_signature=signal_signature
        )
        observed_at = self.now()
        _persist_opportunity(
            self.store,
            candidate=candidate,
            explanation=explanation,
            config=self.config,
            observed_at=observed_at,
            cycle_id=cycle_id,
        )
        decision_id = _persist_decision(
            self.store,
            decision=decision,
            config=self.config,
            cycle_id=cycle_id,
            decided_at=observed_at,
            phase=phase,
        )
        if not decision.executable:
            evidence_digest = canonical_sha256(
                {
                    "cycle_id": cycle_id,
                    "candidate": candidate,
                    "decision_signature": signal_signature,
                    "phase": phase,
                    "executable": False,
                }
            )
            return StageResult(
                cycle_id=cycle_id,
                phase=phase,
                runtime_mode=runtime_mode,
                execution_mode=mode,
                candidate=candidate,
                explanation=explanation,
                normalized_opportunity=normalized,
                decision=decision,
                decision_id=decision_id,
                signal_signature=signal_signature,
                evidence_digest=evidence_digest,
            )

        evidence_digest = canonical_sha256(
            {
                "strategy_id": self.config.strategy_id,
                "phase": phase,
                "opportunity_id": candidate.opportunity_id,
                "signal_signature": signal_signature,
            }
        )
        qualification = build_qualification(self.config, evidence_digest=evidence_digest)

        def persist_submit_journal(
            *,
            plan: ExecutionPlan,
            approval: ExecutionApproval,
            orders: Sequence[OrderRecord],
        ) -> object:
            del plan
            for order in orders:
                _persist_order_record(
                    self.store,
                    config=self.config,
                    cycle_id=cycle_id,
                    order=order,
                    idempotency_key=(
                        f"{cycle_id}:order:{order.client_order_id}:"
                        f"{order.status.value}:{approval.idempotency_key}"
                    ),
                )
            return None

        engine = ExecutionEngine(
            mode=mode,
            venue=venue,
            qualification_registry=InMemoryQualificationRegistry(
                records={self.config.strategy_id: qualification}
            ),
            submit_journal=persist_submit_journal,
        )
        intent = decision_to_intent(
            candidate=candidate,
            decision=decision,
            mode=mode,
            risk_context=risk_context,
        )
        plan = await engine.review(
            intent,
            actor=actor,
            idempotency_key=f"{cycle_id}:review",
        )
        _persist_execution_plan(
            self.store,
            config=self.config,
            cycle_id=cycle_id,
            decision_id=decision_id,
            plan=plan,
            decision=decision,
        )
        ticket = await engine.confirm(
            plan.plan_id,
            ExecutionApproval(
                actor=actor,
                idempotency_key=f"{cycle_id}:confirm",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
            ),
        )
        _persist_execution_orders(
            self.store,
            config=self.config,
            cycle_id=cycle_id,
            ticket=ticket,
        )
        reconcile_result = await engine.intervene(
            InterventionAction(
                kind=InterventionKind.RECONCILE,
                reason=f"{phase}_reconcile",
            ),
            actor=actor,
            idempotency_key=f"{cycle_id}:reconcile",
        )
        reconciliation = reconcile_result.reconciliation
        if reconciliation is None:
            raise RuntimeError("reconcile action did not return a venue reconciliation snapshot")
        execution_snapshot = engine.snapshot()
        _persist_reconciliation(
            self.store,
            config=self.config,
            cycle_id=cycle_id,
            reconciliation=reconciliation,
            phase=phase,
        )
        _persist_reconciliation_orders_and_fills(
            self.store,
            config=self.config,
            cycle_id=cycle_id,
            snapshot=execution_snapshot,
            reconciliation=reconciliation,
        )
        feedback = None
        if realized_outcome is not None:
            self.store.record_pnl_attribution(
                PnLAttributionRecord(
                    attribution_id=f"{cycle_id}:realized",
                    source_type=phase,
                    source_id=realized_outcome.source_id,
                    token_id=realized_outcome.token_id,
                    strategy_id=self.config.strategy_id,
                    occurred_at=_isoformat(self.now()),
                    realized_trading_pnl=realized_outcome.gross_trading_pnl,
                    fee_pnl=-realized_outcome.fee_amount,
                    slippage_pnl=-realized_outcome.slippage_amount,
                    unwind_pnl=-realized_outcome.unwind_amount,
                    onchain_pnl=-realized_outcome.onchain_amount,
                    incentive_pnl_confirmed=realized_outcome.incentive_confirmed,
                    incentive_pnl_pending=realized_outcome.incentive_pending,
                    metadata={
                        "cycle_id": cycle_id,
                        "explicit_realized_net_pnl": str(realized_outcome.realized_net_pnl),
                    },
                    version=_version_stamp(self.config),
                ),
                idempotency_key=f"{cycle_id}:pnl",
            )
            feedback = self.runtime.observe(
                realized_outcome.feedback_document(opportunity_id=decision.opportunity_id)
            )
        book = PortfolioBook(self.store)
        ledger_snapshot = book.rebuild_read_model()
        performance = book.performance({"allocated_capital": portfolio.available_capital})
        return StageResult(
            cycle_id=cycle_id,
            phase=phase,
            runtime_mode=runtime_mode,
            execution_mode=mode,
            candidate=candidate,
            explanation=explanation,
            normalized_opportunity=normalized,
            decision=decision,
            decision_id=decision_id,
            signal_signature=signal_signature,
            evidence_digest=evidence_digest,
            intent=intent,
            execution_engine=engine,
            ticket=ticket,
            execution_snapshot=execution_snapshot,
            reconciliation=reconciliation,
            ledger_snapshot=ledger_snapshot,
            performance=performance,
            feedback=feedback,
            realized_outcome=realized_outcome,
        )

    async def run_monitor_cycle(
        self,
        *,
        session: StageResult,
        venue: VenueAdapter,
        phase: str,
        actor: str,
        event_limit: int = 8,
    ) -> StageResult:
        if session.execution_engine is None:
            raise ValueError("monitor cycle requires an execution-backed session")
        engine = session.execution_engine
        consumed_events: list[VenueUserEvent] = []
        async for event in venue.stream_user_events(
            markets=session.candidate.market_ids,
            since_cursor=None,
        ):
            consumed_events.append(event)
            engine.ingest_user_event(event)
            if len(consumed_events) >= event_limit:
                break
        reconcile_result = await engine.intervene(
            InterventionAction(
                kind=InterventionKind.RECONCILE,
                reason=f"{phase}_monitor",
            ),
            actor=actor,
            idempotency_key=f"{session.cycle_id}:{phase}:monitor",
        )
        reconciliation = reconcile_result.reconciliation
        if reconciliation is None:
            raise RuntimeError("monitor cycle expected reconciliation output")
        execution_snapshot = engine.snapshot()
        monitor_phase = f"{phase}_monitor"
        _persist_reconciliation(
            self.store,
            config=self.config,
            cycle_id=session.cycle_id,
            reconciliation=reconciliation,
            phase=monitor_phase,
        )
        _persist_reconciliation_orders_and_fills(
            self.store,
            config=self.config,
            cycle_id=session.cycle_id,
            snapshot=execution_snapshot,
            reconciliation=reconciliation,
        )
        self.store.save_user_stream_snapshot(
            {
                "as_of": _isoformat(reconciliation.as_of),
                "consumed_events": [
                    {
                        "cursor": event.cursor,
                        "kind": event.kind.value,
                        "occurred_at": _isoformat(event.occurred_at),
                        "detail": event.detail,
                    }
                    for event in consumed_events
                ],
                "last_event_at": None
                if not consumed_events
                else _isoformat(consumed_events[-1].occurred_at),
            },
            idempotency_key=f"{session.cycle_id}:{monitor_phase}:user_stream_snapshot",
        )
        book = PortfolioBook(self.store)
        ledger_snapshot = book.rebuild_read_model()
        performance = book.performance(
            {"allocated_capital": session.decision.capital_limit or Decimal("0")}
        )
        return StageResult(
            cycle_id=session.cycle_id,
            phase=phase,
            runtime_mode=session.runtime_mode,
            execution_mode=session.execution_mode,
            candidate=session.candidate,
            explanation=session.explanation,
            normalized_opportunity=session.normalized_opportunity,
            decision=session.decision,
            decision_id=session.decision_id,
            signal_signature=session.signal_signature,
            evidence_digest=session.evidence_digest,
            intent=session.intent,
            execution_engine=engine,
            ticket=session.ticket,
            execution_snapshot=execution_snapshot,
            reconciliation=reconciliation,
            ledger_snapshot=ledger_snapshot,
            performance=performance,
            feedback=session.feedback,
            realized_outcome=session.realized_outcome,
        )

    def evaluate_gate(
        self,
        *,
        gate: ResearchGate | ShadowGate,
        evidence: dict[str, Any],
        state_key: str,
        phase: str,
    ) -> GateEvaluation:
        evidence_digest = canonical_sha256(
            {
                "strategy_id": self.config.strategy_id,
                "phase": phase,
                "state_key": state_key,
                "evidence": evidence,
            }
        )
        self.store.set_system_state(
            state_key,
            {
                "evidence": evidence,
                "evidence_digest": evidence_digest,
            },
            idempotency_key=f"{self.config.strategy_id}:{phase}:{state_key}",
        )
        persisted = cast(dict[str, Any], self.store.get_system_state(state_key))
        decision = gate.evaluate(dict(persisted["evidence"]))
        return GateEvaluation(
            decision=decision,
            evidence=dict(persisted["evidence"]),
            evidence_digest=str(persisted["evidence_digest"]),
            state_key=state_key,
        )


@dataclass(frozen=True, slots=True)
class FixedTrackAcceptanceResult:
    track: str
    strategy_id: str
    research: GateEvaluation
    replay: StageResult
    paper: StageResult
    shadow: StageResult
    shadow_gate: GateEvaluation

    @property
    def final_status(self) -> str:
        return "GO" if self.research.decision.go and self.shadow_gate.decision.go else "NO_GO"


@dataclass(frozen=True, slots=True)
class ReplayScenario:
    adapter: ReplayVenueAdapter
    realized_outcome: RealizedOutcome | None = None
