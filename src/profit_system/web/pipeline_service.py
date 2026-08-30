from __future__ import annotations

import asyncio
import json
import secrets
import threading
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from ..acceptance import build_fixed_paper_venue, build_track_a_fixture
from ..execution import ExecutionMode
from ..opportunities import OpportunityEngine
from ..orchestrator import ProfitPipelineOrchestrator
from ..persistence import PersistenceStore
from ..portfolio import PortfolioBook
from ..portfolio import PortfolioSnapshot as LedgerSnapshot
from ..runtime import StrategyRuntime
from ..strategies.models import NormalizedOpportunity, RuntimeMode, StrategyDecision
from ..types import OpportunityCandidate, OpportunityExplanation
from .models import (
    SCHEMA_VERSION,
    ConnectionState,
    CostItem,
    DepthItem,
    DeskConnectionStatus,
    DeskMode,
    DeskSnapshot,
    DeskStatusBar,
    ExecutionPlanSnapshot,
    ExpectedVsRealizedSnapshot,
    ExplanationSnapshot,
    FillSnapshot,
    LadderLevel,
    MutationRequest,
    MutationResult,
    OpportunityState,
    OpportunitySummary,
    OrderSnapshot,
    OrderState,
    PlanLeg,
    PlanState,
    PnlWindow,
    PositionSnapshot,
    RecentTrade,
    ReconciliationDiff,
    ReconciliationSnapshot,
    RelatedMarket,
    ScenarioItem,
    StrategyPnlSnapshot,
    isoformat,
    utc_now,
)
from .service import DeskConflictError

ZERO = Decimal("0")
_MARKET_TITLES = {
    "track-a-m1": "Track A Complete Set Leg 1",
    "track-a-m2": "Track A Complete Set Leg 2",
}
_TOKEN_LABELS = {
    "track-a-y1": "Track A YES 1",
    "track-a-y2": "Track A YES 2",
}


@dataclass
class _SessionState:
    snapshot_version: int
    csrf_token: str
    selected_opportunity_id: str
    plan_state: str
    action_log: list[str]


@dataclass(frozen=True)
class _Context:
    candidate: OpportunityCandidate
    explanation: OpportunityExplanation
    decision: StrategyDecision
    market_prices: dict[str, Decimal]
    available_capital: Decimal


class PipelineDeskService:
    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir or (Path.cwd() / ".desk-state")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def get_snapshot(self, *, scenario: str = "paper", session: str = "default") -> DeskSnapshot:
        normalized_scenario = self._normalize_scenario(scenario)
        with self._lock:
            context = self._build_context()
            state = self._load_or_seed_state(
                scenario=normalized_scenario,
                session=session,
                context=context,
            )
            with self._store_for(scenario=normalized_scenario, session=session) as store:
                ledger = PortfolioBook(store).snapshot()
                return self._build_snapshot(
                    scenario=normalized_scenario,
                    session=session,
                    state=state,
                    context=context,
                    ledger=ledger,
                    store=store,
                )

    def review(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate(
            "review",
            mutation,
            scenario=self._normalize_scenario(scenario),
            session=session,
        )

    def confirm(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate(
            "confirm",
            mutation,
            scenario=self._normalize_scenario(scenario),
            session=session,
        )

    def cancel(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate(
            "cancel",
            mutation,
            scenario=self._normalize_scenario(scenario),
            session=session,
        )

    def cancel_all(
        self, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult:
        return self._mutate(
            "cancel-all",
            mutation,
            scenario=self._normalize_scenario(scenario),
            session=session,
        )

    def kill(self, mutation: MutationRequest, *, scenario: str, session: str) -> MutationResult:
        return self._mutate(
            "kill",
            mutation,
            scenario=self._normalize_scenario(scenario),
            session=session,
        )

    def _mutate(
        self, action: str, mutation: MutationRequest, *, scenario: str, session: str
    ) -> MutationResult:
        if not mutation.actor.strip():
            raise ValueError("actor must be non-empty")
        if not mutation.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        receipt_key = self._receipt_key(scenario, session, action, mutation.idempotency_key)
        with self._lock:
            receipts = self._load_receipts(scenario=scenario, session=session)
            cached = receipts.get(receipt_key)
            if cached is not None:
                return MutationResult(
                    action=action,
                    accepted=bool(cached["accepted"]),
                    message=str(cached["message"]),
                    snapshot=self.get_snapshot(scenario=scenario, session=session),
                )
            context = self._build_context()
            state = self._load_or_seed_state(scenario=scenario, session=session, context=context)
            with self._store_for(scenario=scenario, session=session) as store:
                state, message = self._apply_mutation(
                    action=action,
                    mutation=mutation,
                    scenario=scenario,
                    session=session,
                    state=state,
                    context=context,
                    store=store,
                )
                self._save_state(scenario=scenario, session=session, state=state)
                snapshot = self._build_snapshot(
                    scenario=scenario,
                    session=session,
                    state=state,
                    context=context,
                    ledger=PortfolioBook(store).snapshot(),
                    store=store,
                )
            receipts[receipt_key] = {"accepted": True, "message": message}
            self._save_receipts(scenario=scenario, session=session, receipts=receipts)
            return MutationResult(action=action, accepted=True, message=message, snapshot=snapshot)

    def _apply_mutation(
        self,
        *,
        action: str,
        mutation: MutationRequest,
        scenario: str,
        session: str,
        state: _SessionState,
        context: _Context,
        store: PersistenceStore,
    ) -> tuple[_SessionState, str]:
        self._ensure_not_killed(store)
        if action == "confirm" and scenario == "shadow":
            raise DeskConflictError(
                "Shadow mode is observational only; confirm cannot create venue orders."
            )
        if action == "confirm" and scenario == "live":
            raise DeskConflictError(
                "LIVE_BLOCKED: this desk session has no current-environment release permit, "
                "risk budget, live venue preflight, or reconciliation evidence."
            )
        if action in {"cancel", "cancel-all"} and scenario != "paper" and store.list_open_orders():
            raise DeskConflictError(
                f"{scenario.upper()} desk view is not attached to an authorized venue "
                "cancel path; use the fail-closed operator recovery flow."
            )
        if action == "review":
            if state.plan_state != PlanState.APPROVED.value:
                state.plan_state = PlanState.REVIEWED.value
            self._append_action(state, f"review:{mutation.actor}")
            return state, f"Plan reviewed by {mutation.actor}."
        if action == "confirm":
            if state.plan_state == PlanState.APPROVED.value:
                self._append_action(state, f"confirm-replayed:{mutation.actor}")
                return state, f"Plan already confirmed for {mutation.actor}."
            if state.plan_state != PlanState.REVIEWED.value:
                raise DeskConflictError("Execution plan must be reviewed before confirm.")
            self._execute_confirm(
                scenario=scenario,
                session=session,
                actor=mutation.actor,
                store=store,
            )
            state.plan_state = PlanState.APPROVED.value
            self._append_action(state, f"confirm:{mutation.actor}")
            return state, f"Plan confirmed by {mutation.actor}."
        if action in {"cancel", "cancel-all"}:
            if not store.list_open_orders():
                self._append_action(state, f"{action}-noop:{mutation.actor}")
                return state, "没有可取消的活动订单。"
            if state.plan_state != PlanState.APPROVED.value:
                state.plan_state = PlanState.CANCELED.value
            self._append_action(state, f"{action}:{mutation.actor}")
            return state, f"Cancel requested for active orders by {mutation.actor}."
        store.save_kill_state(
            reason="desk kill switch engaged",
            actor=mutation.actor,
            recovery={
                "scenario": scenario,
                "session": session,
                "restart_context": store.load_restart_context(),
            },
            idempotency_key=f"{scenario}:{session}:kill-state",
        )
        state.plan_state = PlanState.KILLED.value
        self._append_action(state, f"kill:{mutation.actor}")
        return state, f"Kill switch engaged by {mutation.actor}."

    def _execute_confirm(
        self,
        *,
        scenario: str,
        session: str,
        actor: str,
        store: PersistenceStore,
    ) -> None:
        del scenario, session
        fixture = build_track_a_fixture()
        orchestrator = ProfitPipelineOrchestrator(
            config=fixture.config,
            strategy=fixture.strategy,
            store=store,
            opportunity_engine=OpportunityEngine(),
            runtime=StrategyRuntime(fixture.strategy),
            now=lambda: fixture.snapshot.captured_at,
        )
        asyncio.run(
            orchestrator.run_execution_stage(
                snapshot=fixture.snapshot,
                portfolio=fixture.portfolio,
                mode=ExecutionMode.PAPER,
                venue=build_fixed_paper_venue(),
                phase="paper",
                actor=actor,
                realized_outcome=fixture.paper_outcome,
            )
        )

    def _build_context(self) -> _Context:
        fixture = build_track_a_fixture()
        engine = OpportunityEngine()
        candidate = engine.scan(fixture.snapshot, (fixture.config,)).opportunities[0]
        explanation = engine.explain(candidate.opportunity_id)
        decision = StrategyRuntime(fixture.strategy).evaluate(
            NormalizedOpportunity.from_candidate(candidate),
            fixture.portfolio,
            RuntimeMode.PAPER,
        )
        return _Context(
            candidate=candidate,
            explanation=explanation,
            decision=decision,
            market_prices={"track-a-y1": Decimal("0.40"), "track-a-y2": Decimal("0.40")},
            available_capital=fixture.portfolio.available_capital,
        )

    def _build_snapshot(
        self,
        *,
        scenario: str,
        session: str,
        state: _SessionState,
        context: _Context,
        ledger: LedgerSnapshot,
        store: PersistenceStore,
    ) -> DeskSnapshot:
        kill_state = store.get_system_state("kill_state")
        orders = self._order_snapshots(store)
        fills = self._fill_snapshots(
            store=store,
            market_names=self._market_names(context),
            realized_net_pnl=ledger.realized_net_pnl,
        )
        reconciliation = self._reconciliation_snapshot(
            store,
            scenario=scenario,
            kill_state=kill_state,
        )
        positions = self._position_snapshots(ledger)
        expected_realized = ledger.realized_net_pnl
        expected_plan_pnl = self._expected_plan_pnl(context)
        risk_budget_change = (
            context.decision.max_loss / Decimal("50") if Decimal("50") > ZERO else ZERO
        )
        if kill_state:
            mode = DeskMode.KILLED
            connections = DeskConnectionStatus(
                data=ConnectionState.LIVE,
                orders=ConnectionState.BLOCKED,
                market_data_age_ms=200,
                blocked_reason="kill switch engaged",
            )
            is_live_locked = True
            mode_banner = (
                "KILLED: new orders blocked until a manual operator reset completes reconciliation."
            )
            plan_lock_reason = "kill switch engaged"
            venue_label = "blocked"
            execution_notes = [
                "Kill is durable across restart; only recovery and reconciliation are allowed.",
                "No PnL shown in this state is evidence of live profitability.",
            ]
        elif scenario == "shadow":
            mode = DeskMode.SHADOW
            connections = DeskConnectionStatus(
                data=ConnectionState.LIVE,
                orders=ConnectionState.BLOCKED,
                market_data_age_ms=200,
                blocked_reason="shadow mode never submits venue orders",
            )
            is_live_locked = False
            mode_banner = (
                "SHADOW observational only: decisions are persisted, venue submission is "
                "disabled, and displayed PnL is not live realized profit."
            )
            plan_lock_reason = "shadow mode is observational only"
            venue_label = "shadow-no-submit"
            execution_notes = [
                "Review persists the operator decision; confirm cannot create venue orders.",
                "Shadow expected values are evidence only and never realized profit.",
            ]
        elif scenario == "live":
            mode = DeskMode.LIVE_CANARY
            connections = DeskConnectionStatus(
                data=ConnectionState.DISCONNECTED,
                orders=ConnectionState.BLOCKED,
                market_data_age_ms=None,
                blocked_reason=(
                    "no authorized live venue, market-data, or user-stream session is attached"
                ),
            )
            is_live_locked = True
            mode_banner = (
                "LIVE_BLOCKED: Canary preview only; current-environment authorization, risk "
                "budget, release permit, venue preflight, and reconciliation are absent."
            )
            plan_lock_reason = "LIVE_BLOCKED: Canary Gate prerequisites are not attached"
            venue_label = "polymarket-live-locked"
            execution_notes = [
                "Review is local only; confirm remains fail-closed without the full Canary Gate.",
                "No displayed balance, edge, or expected value is live profit evidence.",
            ]
        else:
            mode = DeskMode.PAPER
            connections = DeskConnectionStatus(
                data=ConnectionState.LIVE,
                orders=ConnectionState.LIVE,
                market_data_age_ms=200,
                blocked_reason=None,
            )
            is_live_locked = False
            mode_banner = (
                "PAPER fixed scenario only: persisted paper fills and PnL are simulation "
                "evidence, not live realized profit."
            )
            plan_lock_reason = None
            venue_label = "paper"
            execution_notes = [
                "Confirm routes through the paper execution engine and persists every order "
                "and fill.",
                "Paper numbers are simulation evidence and never claim live realized profit.",
            ]
        now = utc_now()
        return DeskSnapshot(
            schema_version=SCHEMA_VERSION,
            snapshot_version=state.snapshot_version,
            generated_at=now,
            scenario=scenario,
            session=session,
            status_bar=DeskStatusBar(
                mode=mode,
                available_cash=(
                    ZERO
                    if scenario == "live"
                    else self._available_cash(ledger, fallback=context.available_capital)
                ),
                realized_net_pnl=PnlWindow(
                    today=ledger.realized_net_pnl,
                    seven_day=ledger.realized_net_pnl,
                    thirty_day=ledger.realized_net_pnl,
                ),
                current_drawdown=-abs(self._performance_drawdown(store)),
                risk_budget_used=(
                    risk_budget_change
                    if scenario == "paper" and state.plan_state == PlanState.APPROVED.value
                    else ZERO
                ),
                connections=connections,
                is_live_locked=is_live_locked,
                kill_switch_engaged=bool(kill_state),
                mode_banner=mode_banner,
            ),
            selected_opportunity_id=state.selected_opportunity_id,
            opportunities=[
                OpportunitySummary(
                    id=context.candidate.opportunity_id,
                    rank=1,
                    strategy=context.candidate.strategy_id,
                    market=" / ".join(self._market_names(context)),
                    tradable_edge=context.candidate.tradable_edge or ZERO,
                    expected_net_profit=expected_plan_pnl,
                    max_loss=context.candidate.max_loss,
                    expected_capacity=context.candidate.estimated_capacity,
                    confidence=context.candidate.confidence,
                    ttl_seconds=int(context.candidate.remaining_ttl_seconds),
                    state=(
                        OpportunityState.REVIEW
                        if scenario == "live"
                        else OpportunityState(context.candidate.status.value)
                    ),
                    note=context.decision.rationale,
                )
            ],
            explanation=self._explanation_snapshot(context),
            execution_plan=ExecutionPlanSnapshot(
                id=f"plan-{context.candidate.opportunity_id[:12]}",
                opportunity_id=context.candidate.opportunity_id,
                state=PlanState(state.plan_state),
                review_required=True,
                live_lock_reason=plan_lock_reason,
                estimated_fee=(context.candidate.expected_fees or ZERO),
                estimated_slippage=self._estimated_slippage(context.candidate),
                expected_net_profit=expected_plan_pnl,
                worst_case_loss=context.decision.max_loss,
                risk_budget_change=risk_budget_change,
                execution_notes=execution_notes,
                failure_path=(
                    "Kill or reconciliation attention keeps new confirms "
                    "fail-closed until manual reset."
                ),
                legs=[
                    PlanLeg(
                        id=leg.leg_id,
                        venue=venue_label,
                        side=leg.side.upper(),
                        instrument=_TOKEN_LABELS.get(leg.token_id, leg.token_id),
                        price=leg.price,
                        size=leg.quantity,
                        order_type=leg.order_type.upper(),
                        post_only=leg.post_only,
                    )
                    for leg in context.decision.legs
                ],
            ),
            orders=orders,
            fills=fills,
            positions=positions,
            strategy_pnl=[
                StrategyPnlSnapshot(
                    strategy=context.decision.strategy_id,
                    realized_today=ledger.realized_net_pnl,
                    realized_seven_day=ledger.realized_net_pnl,
                    realized_thirty_day=ledger.realized_net_pnl,
                    mark_to_market=ledger.unrealized_pnl,
                )
            ],
            expected_vs_realized=[
                ExpectedVsRealizedSnapshot(
                    strategy=context.decision.strategy_id,
                    expected_net_pnl=expected_plan_pnl,
                    realized_net_pnl=expected_realized,
                    variance=expected_realized - expected_plan_pnl,
                )
            ],
            reconciliation=reconciliation,
            csrf_token=state.csrf_token,
            action_log=list(state.action_log),
        )

    def _available_cash(self, ledger: LedgerSnapshot, *, fallback: Decimal) -> Decimal:
        for balance in ledger.cash_balances:
            if balance.currency == "USDC":
                return Decimal(str(balance.available))
        return fallback

    def _expected_plan_pnl(self, context: _Context) -> Decimal:
        return Decimal(str(context.decision.expected_net_edge))

    def _performance_drawdown(self, store: PersistenceStore) -> Decimal:
        allocated = store.get_system_state("allocated_capital", Decimal("100"))
        performance = PortfolioBook(store).performance({"allocated_capital": allocated})
        value = performance.metrics.get("max_drawdown")
        return ZERO if value is None else Decimal(str(value))

    def _market_names(self, context: _Context) -> list[str]:
        return [
            _MARKET_TITLES.get(market_id, market_id) for market_id in context.candidate.market_ids
        ]

    def _estimated_slippage(self, candidate: OpportunityCandidate) -> Decimal:
        values = (
            candidate.expected_slippage,
            candidate.expected_adverse_selection,
            candidate.expected_inventory_cost,
            candidate.expected_unwind_cost,
            candidate.uncertainty_buffer,
        )
        return sum(((value or ZERO) for value in values), ZERO)

    def _explanation_snapshot(self, context: _Context) -> ExplanationSnapshot:
        recent_time = isoformat(utc_now())
        return ExplanationSnapshot(
            opportunity_id=context.candidate.opportunity_id,
            market_title="Track A Complete Set Arbitrage",
            settlement=f"Expires {isoformat(context.candidate.expires_at)}",
            ladder=[
                LadderLevel(side="bid", price=Decimal("0.39"), size=Decimal("20")),
                LadderLevel(side="ask", price=Decimal("0.40"), size=Decimal("20")),
                LadderLevel(side="bid", price=Decimal("0.46"), size=Decimal("20")),
                LadderLevel(side="ask", price=Decimal("0.47"), size=Decimal("20")),
            ],
            recent_trades=[
                RecentTrade(time=recent_time, side="BUY", price=leg.price, size=leg.quantity)
                for leg in context.decision.legs
            ],
            related_markets=[
                RelatedMarket(label=name, relationship="paired_leg", mark=Decimal("0.40"))
                for name in self._market_names(context)
            ],
            reason_summary=[check.detail for check in context.explanation.gate_checks[:4]],
            cost_breakdown=[
                CostItem(
                    label="Fees",
                    amount=context.candidate.expected_fees or ZERO,
                    note="modeled fees",
                ),
                CostItem(
                    label="Slippage and adverse",
                    amount=self._estimated_slippage(context.candidate),
                    note="slippage, adverse selection, inventory, unwind, uncertainty",
                ),
                CostItem(
                    label="Confirmed incentive",
                    amount=-(context.candidate.expected_incentive or ZERO),
                    note="incentive offsets net trading costs",
                ),
            ],
            scenarios=[
                ScenarioItem(
                    name=name,
                    net_profit=value or ZERO,
                    worst_case_loss=context.candidate.max_loss,
                    confidence=context.candidate.confidence,
                )
                for name, value in context.explanation.scenarios.items()
            ],
            executable_depth=[
                DepthItem(level="leg-1", price=Decimal("0.40"), executable_size=Decimal("20")),
                DepthItem(level="leg-2", price=Decimal("0.47"), executable_size=Decimal("20")),
            ],
        )

    def _order_snapshots(self, store: PersistenceStore) -> list[OrderSnapshot]:
        market_names = _MARKET_TITLES
        orders = sorted(store.list_orders(), key=lambda order: order.updated_at, reverse=True)
        return [
            OrderSnapshot(
                id=order.order_id,
                plan_id=str(order.metadata.get("plan_id", "paper-plan")),
                market=market_names.get(order.market_id, order.market_id),
                state=self._order_state(order.lifecycle_state),
                side=order.side.upper(),
                price=order.price,
                size=order.quantity,
                filled_size=order.filled_quantity,
                venue_status=order.lifecycle_state,
                updated_at=order.updated_at,
            )
            for order in orders
        ]

    def _fill_snapshots(
        self,
        *,
        store: PersistenceStore,
        market_names: list[str],
        realized_net_pnl: Decimal,
    ) -> list[FillSnapshot]:
        fills = sorted(store.list_fills(), key=lambda fill: fill.occurred_at, reverse=True)
        if not fills:
            return []
        notional_total = sum((fill.price * fill.quantity for fill in fills), ZERO)
        remaining = realized_net_pnl
        snapshots: list[FillSnapshot] = []
        for index, fill in enumerate(fills):
            market_name = market_names[index] if index < len(market_names) else fill.token_id
            if index == len(fills) - 1 or notional_total == ZERO:
                allocated_pnl = remaining
            else:
                weight = (fill.price * fill.quantity) / notional_total
                allocated_pnl = (realized_net_pnl * weight).quantize(Decimal("0.000001"))
                remaining -= allocated_pnl
            snapshots.append(
                FillSnapshot(
                    id=fill.fill_id,
                    order_id=fill.order_id,
                    market=market_name,
                    side=fill.side.upper(),
                    price=fill.price,
                    size=fill.quantity,
                    fee=fill.fee_amount,
                    realized_net_pnl=allocated_pnl,
                    occurred_at=fill.occurred_at,
                )
            )
        return snapshots

    def _position_snapshots(self, ledger: LedgerSnapshot) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(
                id=f"pos-{position.token_id}",
                market=_TOKEN_LABELS.get(position.token_id, position.token_id),
                side="LONG" if position.net_quantity >= ZERO else "SHORT",
                quantity=abs(position.net_quantity),
                avg_price=position.average_cost or ZERO,
                mark_price=position.market_price or ZERO,
                realized_net_pnl=(
                    position.realized_trading_pnl
                    + position.fee_pnl
                    + position.slippage_pnl
                    + position.unwind_pnl
                    + position.onchain_pnl
                    + position.incentive_pnl_confirmed
                ),
                unrealized_net_pnl=position.unrealized_pnl,
            )
            for position in ledger.positions
        ]

    def _reconciliation_snapshot(
        self,
        store: PersistenceStore,
        *,
        scenario: str,
        kill_state: object,
    ) -> ReconciliationSnapshot:
        runs = store.list_reconciliation_runs()
        if runs:
            latest = runs[-1]
            differences = [
                ReconciliationDiff(
                    scope="reconciliation",
                    severity="high",
                    message=str(item),
                )
                for item in latest.report.get("discrepancies", [])
            ]
            if kill_state:
                differences.append(
                    ReconciliationDiff(
                        scope="system",
                        severity="high",
                        message="Kill switch is durable; confirm remains disabled after restart.",
                    )
                )
            summary = (
                f"Authoritative {scenario} reconciliation matches the persisted journal."
                if latest.status == "matched"
                else "Reconciliation requires operator attention before more confirms."
            )
            return ReconciliationSnapshot(
                status="attention" if kill_state else latest.status,
                last_run_at=latest.created_at,
                summary=summary,
                differences=differences,
            )
        if kill_state:
            status = "attention"
            summary = "Kill switch is durable; confirm remains disabled after restart."
            differences = [
                ReconciliationDiff(
                    scope="system",
                    severity="high",
                    message="Kill switch is durable; confirm remains disabled after restart.",
                )
            ]
        elif scenario == "live":
            status = "attention"
            summary = (
                "No authorized live account or user-stream reconciliation snapshot is attached."
            )
            differences = [
                ReconciliationDiff(
                    scope="live-readiness",
                    severity="high",
                    message="LIVE_BLOCKED until the complete Canary Gate is verified.",
                )
            ]
        elif scenario == "shadow":
            status = "not_applicable"
            summary = "Shadow is observational only and creates no venue orders to reconcile."
            differences = []
        else:
            status = "pending"
            summary = "Awaiting the first paper execution reconciliation."
            differences = []
        return ReconciliationSnapshot(
            status=status,
            last_run_at=isoformat(utc_now()),
            summary=summary,
            differences=differences,
        )

    def _ensure_not_killed(self, store: PersistenceStore) -> None:
        if store.get_system_state("kill_state") is not None:
            raise DeskConflictError(
                "Kill switch engaged; manual reset required before more mutations."
            )

    def _load_or_seed_state(
        self,
        *,
        scenario: str,
        session: str,
        context: _Context,
    ) -> _SessionState:
        path = self._state_path(scenario=scenario, session=session)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return _SessionState(
                snapshot_version=int(payload["snapshot_version"]),
                csrf_token=str(payload["csrf_token"]),
                selected_opportunity_id=str(payload["selected_opportunity_id"]),
                plan_state=str(payload["plan_state"]),
                action_log=[str(item) for item in payload.get("action_log", [])],
            )
        with self._store_for(scenario=scenario, session=session) as store:
            store.save_cash_snapshot(
                {
                    "balances": [
                        {
                            "currency": "USDC",
                            "total": str(context.available_capital),
                            "available": str(context.available_capital),
                            "reserved": "0",
                        }
                    ]
                },
                idempotency_key=f"{scenario}:{session}:seed-cash",
            )
            store.save_user_stream_snapshot(
                {"as_of": isoformat(utc_now()), "open_orders": []},
                idempotency_key=f"{scenario}:{session}:seed-user-stream",
            )
            store.set_system_state(
                "allocated_capital",
                str(context.available_capital),
                idempotency_key=f"{scenario}:{session}:allocated-capital",
            )
            store.set_system_state(
                "latest_market_prices",
                {token_id: str(price) for token_id, price in context.market_prices.items()},
                idempotency_key=f"{scenario}:{session}:market-prices",
            )
        state = _SessionState(
            snapshot_version=1,
            csrf_token=secrets.token_urlsafe(32),
            selected_opportunity_id=context.candidate.opportunity_id,
            plan_state=PlanState.DRAFT.value,
            action_log=["pipeline-seeded"],
        )
        self._save_state(scenario=scenario, session=session, state=state)
        return state

    def _append_action(self, state: _SessionState, entry: str) -> None:
        state.snapshot_version += 1
        state.action_log.append(entry)

    def _state_path(self, *, scenario: str, session: str) -> Path:
        return self._state_dir / f"{self._slug(scenario)}__{self._slug(session)}.state.json"

    def _db_path(self, *, scenario: str, session: str) -> Path:
        return self._state_dir / f"{self._slug(scenario)}__{self._slug(session)}.sqlite3"

    def _receipt_path(self, *, scenario: str, session: str) -> Path:
        return self._state_dir / f"{self._slug(scenario)}__{self._slug(session)}.receipts.json"

    def _receipt_key(self, scenario: str, session: str, action: str, idempotency_key: str) -> str:
        return "::".join((scenario, session, action, idempotency_key))

    def _save_state(self, *, scenario: str, session: str, state: _SessionState) -> None:
        self._state_path(scenario=scenario, session=session).write_text(
            json.dumps(asdict(state), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _load_receipts(self, *, scenario: str, session: str) -> dict[str, dict[str, object]]:
        path = self._receipt_path(scenario=scenario, session=session)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("receipt store must be a JSON object")
        return {str(key): dict(value) for key, value in payload.items()}

    def _save_receipts(
        self,
        *,
        scenario: str,
        session: str,
        receipts: dict[str, dict[str, object]],
    ) -> None:
        self._receipt_path(scenario=scenario, session=session).write_text(
            json.dumps(receipts, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _slug(self, value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)

    def _store_for(self, *, scenario: str, session: str) -> _StoreContext:
        return _StoreContext(self._db_path(scenario=scenario, session=session))

    def _normalize_scenario(self, scenario: str) -> str:
        normalized = scenario.strip().lower().replace("_", "-")
        aliases = {
            "paper": "paper",
            "shadow": "shadow",
            "live": "live",
            "live-canary": "live",
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError("scenario must be one of: paper, shadow, live") from exc

    def _order_state(self, lifecycle_state: str) -> OrderState:
        normalized = lifecycle_state.strip().upper()
        if normalized == "CANCELLED":
            normalized = OrderState.CANCELED.value
        try:
            return OrderState(normalized)
        except ValueError:
            return OrderState.ATTENTION_REQUIRED


class _StoreContext:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._store: PersistenceStore | None = None

    def __enter__(self) -> PersistenceStore:
        self._store = PersistenceStore(self._path)
        return self._store

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        if self._store is not None:
            self._store.close()
            self._store = None
