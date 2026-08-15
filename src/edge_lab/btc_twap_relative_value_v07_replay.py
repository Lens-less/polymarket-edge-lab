"""Conservative causal replay for the opt-in v0.7 shared-terminal model."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from .btc_twap_relative_value import (
    OrderBookSnapshot,
    PairAction,
    PairExecutionStatus,
    PairPaperExecution,
    PairPaperSettlement,
    PairSettlementState,
    SameExpiryPair,
    TwapMarketContract,
    execute_pair_paper,
    settle_pair_paper_execution,
)
from .btc_twap_relative_value_replay import (
    CausalBookObservation,
    CausalBookReplay,
    PairPaperCycleEvaluation,
    paper_execution_diagnostics,
)
from .btc_twap_relative_value_v07 import (
    SharedTerminalDataHealth,
    SharedTerminalSimulationResult,
    TrainOnlyShrinkageArtifact,
    V07EdgeBasis,
    V07ForecastAvailabilityBasis,
    V07PairDecision,
    V07StrategyConfig,
    ValidationVetoEvidence,
    decide_shared_terminal_pair_trade,
    executable_binary_ask_probability,
    raw_top_ask_probability,
)


def _contract_for_token(pair: SameExpiryPair, token_id: str) -> TwapMarketContract:
    for contract in (pair.market_5, pair.market_15):
        if token_id in {contract.up_token_id, contract.down_token_id}:
            return contract
    raise ValueError(f"token is not in same-expiry pair: {token_id}")


def _observable_snapshot(observation: CausalBookObservation) -> OrderBookSnapshot:
    snapshot = observation.snapshot
    return OrderBookSnapshot.from_tuples(
        snapshot.token_id,
        bids=tuple((level.price, level.size) for level in snapshot.bids),
        asks=tuple((level.price, level.size) for level in snapshot.asks),
        timestamp_ms=max(observation.source_at_ms, observation.received_at_ms),
        tick_size=snapshot.tick_size,
        minimum_order_size=snapshot.minimum_order_size,
    )


@dataclass(frozen=True)
class V07PaperCycleEvaluation:
    """One non-promotional locked-OOS counterfactual decision and replay."""

    event_cluster_id: str
    event_cluster_alias: str
    decision_tau_seconds: int
    cycle: PairPaperCycleEvaluation
    simulation: SharedTerminalSimulationResult | None
    market_q_5_up: Decimal | None
    market_q_15_up: Decimal | None
    raw_top_ask_q_5_up: Decimal | None
    raw_top_ask_q_15_up: Decimal | None
    signal_source_event_ids: tuple[str, ...]
    shrinkage: TrainOnlyShrinkageArtifact | None
    validation_veto: ValidationVetoEvidence | None
    locked_oos: bool
    forecast_available_at_ms: int
    forecast_availability_basis: V07ForecastAvailabilityBasis
    first_execution_not_before_ms: int | None = None
    first_execution_observed_at_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_cluster_id, str) or not self.event_cluster_id:
            raise ValueError("event_cluster_id must be non-empty")
        if (
            not isinstance(self.event_cluster_alias, str)
            or not self.event_cluster_alias
        ):
            raise ValueError("event_cluster_alias must be non-empty")
        if (
            isinstance(self.decision_tau_seconds, bool)
            or not isinstance(self.decision_tau_seconds, int)
            or self.decision_tau_seconds < 0
        ):
            raise ValueError("decision_tau_seconds must be non-negative")
        if (
            isinstance(self.forecast_available_at_ms, bool)
            or not isinstance(self.forecast_available_at_ms, int)
            or self.forecast_available_at_ms < self.decision.decision_at_ms
        ):
            raise ValueError("forecast_available_at_ms is invalid")
        if not isinstance(
            self.forecast_availability_basis,
            V07ForecastAvailabilityBasis,
        ):
            raise TypeError(
                "forecast_availability_basis must be V07ForecastAvailabilityBasis"
            )
        if self.decision.forecast_available_at_ms != self.forecast_available_at_ms:
            raise ValueError("cycle and decision forecast availability differ")
        if (
            self.decision.forecast_availability_basis
            is not self.forecast_availability_basis
        ):
            raise ValueError("cycle and decision availability basis differ")
        if self.first_execution_not_before_ms is not None:
            if (
                isinstance(self.first_execution_not_before_ms, bool)
                or not isinstance(self.first_execution_not_before_ms, int)
                or self.first_execution_not_before_ms < self.forecast_available_at_ms
            ):
                raise ValueError("first execution eligibility timestamp is invalid")
        if self.first_execution_observed_at_ms is not None:
            if self.first_execution_not_before_ms is None:
                raise ValueError(
                    "observed execution surface requires an eligibility time"
                )
            if (
                isinstance(self.first_execution_observed_at_ms, bool)
                or not isinstance(self.first_execution_observed_at_ms, int)
                or self.first_execution_observed_at_ms
                < self.first_execution_not_before_ms
            ):
                raise ValueError("first execution surface predates eligibility")

    @property
    def decision(self) -> V07PairDecision:
        decision = self.cycle.decision
        if not isinstance(decision, V07PairDecision):
            raise TypeError("v0.7 cycle requires V07PairDecision")
        return decision

    @property
    def execution(self) -> PairPaperExecution | None:
        return self.cycle.execution

    @property
    def settlement(self) -> PairPaperSettlement | None:
        return self.cycle.settlement

    @property
    def economic_attempt(self) -> bool:
        if self.execution is None:
            return False
        diagnostics = paper_execution_diagnostics(
            self.execution,
            expiry_ms=self.cycle.expiry_ms or 0,
        )
        return diagnostics["economic_attempt"] is True

    @property
    def effective_signal_to_execution_latency_ms(self) -> int | None:
        endpoint = (
            self.first_execution_observed_at_ms
            if self.first_execution_observed_at_ms is not None
            else self.first_execution_not_before_ms
        )
        if endpoint is None:
            return None
        return endpoint - self.decision.decision_at_ms

    @property
    def immutable_receipt_timing_verified(self) -> bool:
        return (
            self.forecast_availability_basis
            is V07ForecastAvailabilityBasis.VERIFIED_IMMUTABLE_RECEIPT
        )

    def to_document(self) -> dict[str, Any]:
        document = self.cycle.to_document()
        document["schema_version"] = (
            "btc-5m-15m-shared-terminal-counterfactual-cycle.v2"
        )
        document["track"] = (
            "v07_locked_oos_counterfactual"
            if self.locked_oos
            else "v07_invariant_fixture"
        )
        document["event_cluster_id"] = self.event_cluster_id
        document["event_cluster_alias"] = self.event_cluster_alias
        document["decision_tau_seconds"] = self.decision_tau_seconds
        decision = self.decision
        document["decision"].update(
            {
                "edge_basis": decision.edge_basis.value,
                "edge_evaluation_quantity": (
                    None
                    if decision.edge_evaluation_quantity is None
                    else str(decision.edge_evaluation_quantity)
                ),
                "structural_worst_case_payoff_per_pair": (
                    None
                    if decision.structural_worst_case_payoff_per_pair is None
                    else str(decision.structural_worst_case_payoff_per_pair)
                ),
                "structural_net_floor_per_pair": (
                    None
                    if decision.structural_net_floor_per_pair is None
                    else str(decision.structural_net_floor_per_pair)
                ),
                "structural_quantity_executable": (
                    decision.structural_quantity_executable
                ),
                "qualification_edge_per_pair": (
                    None
                    if decision.qualification_edge_per_pair is None
                    else str(decision.qualification_edge_per_pair)
                ),
                "quantity_selection_basis": decision.quantity_selection_basis.value,
                "quantity_candidate_breakpoint_count": (
                    decision.quantity_candidate_breakpoint_count
                ),
                "selected_guaranteed_total_pnl": (
                    None
                    if decision.selected_guaranteed_total_pnl is None
                    else str(decision.selected_guaranteed_total_pnl)
                ),
                "selected_uncertainty_adjusted_total_pnl": (
                    None
                    if decision.selected_uncertainty_adjusted_total_pnl is None
                    else str(decision.selected_uncertainty_adjusted_total_pnl)
                ),
                "probability_diagnostics_applicable": (
                    decision.probability_diagnostics_applicable
                ),
                "forecast_available_at_ms": decision.forecast_available_at_ms,
                "forecast_availability_basis": (
                    decision.forecast_availability_basis.value
                ),
            }
        )
        document["edge_classification"] = {
            "edge_basis": decision.edge_basis.value,
            "edge_evaluation_quantity": (
                None
                if decision.edge_evaluation_quantity is None
                else str(decision.edge_evaluation_quantity)
            ),
            "structural_worst_case_payoff_per_pair": (
                None
                if decision.structural_worst_case_payoff_per_pair is None
                else str(decision.structural_worst_case_payoff_per_pair)
            ),
            "structural_net_floor_per_pair": (
                None
                if decision.structural_net_floor_per_pair is None
                else str(decision.structural_net_floor_per_pair)
            ),
            "structural_quantity_executable": (decision.structural_quantity_executable),
            "qualification_edge_per_pair": (
                None
                if decision.qualification_edge_per_pair is None
                else str(decision.qualification_edge_per_pair)
            ),
            "quantity_selection_basis": decision.quantity_selection_basis.value,
            "quantity_candidate_breakpoint_count": (
                decision.quantity_candidate_breakpoint_count
            ),
            "selected_guaranteed_total_pnl": (
                None
                if decision.selected_guaranteed_total_pnl is None
                else str(decision.selected_guaranteed_total_pnl)
            ),
            "selected_uncertainty_adjusted_total_pnl": (
                None
                if decision.selected_uncertainty_adjusted_total_pnl is None
                else str(decision.selected_uncertainty_adjusted_total_pnl)
            ),
            "probability_diagnostics_applicable": (
                decision.probability_diagnostics_applicable
            ),
        }
        document["timing"] = {
            "forecast_available_at_ms": self.forecast_available_at_ms,
            "forecast_availability_basis": self.forecast_availability_basis.value,
            "forecast_computation_availability_delay_ms": (
                self.forecast_available_at_ms - decision.decision_at_ms
            ),
            "first_execution_not_before_ms": self.first_execution_not_before_ms,
            "first_execution_observed_at_ms": self.first_execution_observed_at_ms,
            "effective_signal_to_execution_latency_ms": (
                self.effective_signal_to_execution_latency_ms
            ),
            "immutable_receipt_timing_verified": (
                self.immutable_receipt_timing_verified
            ),
            "counterfactual_delay_is_production_runtime_validation": False,
            "paper_replay_only": True,
        }
        document["settlement_model"] = (
            None if self.simulation is None else self.simulation.to_document()
        )
        if decision.edge_basis is V07EdgeBasis.STRUCTURAL:
            model_status = "not_applicable_structural_primitive"
        elif self.simulation is None:
            model_status = "unavailable_after_structural_scan"
        else:
            model_status = "available_predictive_diagnostic"
        document["settlement_model_diagnostic_status"] = model_status
        document["executable_market_probability_up"] = {
            "5m": (None if self.market_q_5_up is None else str(self.market_q_5_up)),
            "15m": (None if self.market_q_15_up is None else str(self.market_q_15_up)),
        }
        document["raw_top_ask_probability_up_diagnostic"] = {
            "5m": (
                None
                if self.raw_top_ask_q_5_up is None
                else str(self.raw_top_ask_q_5_up)
            ),
            "15m": (
                None
                if self.raw_top_ask_q_15_up is None
                else str(self.raw_top_ask_q_15_up)
            ),
        }
        document["signal_source_event_ids"] = list(self.signal_source_event_ids)
        document["shrinkage"] = (
            None if self.shrinkage is None else self.shrinkage.to_document()
        )
        document["validation_veto"] = (
            None if self.validation_veto is None else self.validation_veto.to_document()
        )
        document["locked_oos"] = self.locked_oos
        document["non_promotional"] = True
        document["qualified_sample_for_v05_v06"] = False
        document["paper_only"] = True
        document["orders_submitted"] = 0
        document["authenticated_endpoints_used"] = 0
        document["proxy_urls_used"] = 0
        return document


def evaluate_shared_terminal_paper_cycle(
    *,
    event_cluster_id: str,
    event_cluster_alias: str,
    decision_tau_seconds: int,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    simulation: SharedTerminalSimulationResult | None,
    replay: CausalBookReplay,
    health: SharedTerminalDataHealth,
    config: V07StrategyConfig,
    market_5_up: bool | None,
    market_15_up: bool | None,
    locked_oos: bool = True,
) -> V07PaperCycleEvaluation:
    """Replay delayed taker legs, partial fills, unwind, and settlement costs."""

    required_tokens = (
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    )
    signal = replay.signal_books(
        token_ids=required_tokens,
        decision_at_ms=health.decision_at_ms,
        maximum_age_ms=config.maximum_book_staleness_ms,
    )
    signal_books = {
        token_id: observation.snapshot for token_id, observation in signal.items()
    }
    decision = decide_shared_terminal_pair_trade(
        pair=pair,
        settlement_state=settlement_state,
        distribution=(None if simulation is None else simulation.distribution),
        books=signal_books,
        health=health,
        config=config,
        locked_oos=locked_oos,
    )
    if decision.probability_diagnostics_applicable:
        market5 = executable_binary_ask_probability(
            signal_books,
            up_token_id=pair.market_5.up_token_id,
            down_token_id=pair.market_5.down_token_id,
            target_quantity=config.market_baseline_quantity,
            fee_schedule=pair.market_5.fee_schedule,
        )
        market15 = executable_binary_ask_probability(
            signal_books,
            up_token_id=pair.market_15.up_token_id,
            down_token_id=pair.market_15.down_token_id,
            target_quantity=config.market_baseline_quantity,
            fee_schedule=pair.market_15.fee_schedule,
        )
        raw_market5 = raw_top_ask_probability(
            signal_books,
            up_token_id=pair.market_5.up_token_id,
            down_token_id=pair.market_5.down_token_id,
        )
        raw_market15 = raw_top_ask_probability(
            signal_books,
            up_token_id=pair.market_15.up_token_id,
            down_token_id=pair.market_15.down_token_id,
        )
    else:
        market5 = None
        market15 = None
        raw_market5 = None
        raw_market15 = None
    source_ids = tuple(signal[token_id].source_event_id for token_id in sorted(signal))

    def wrap(
        cycle: PairPaperCycleEvaluation,
        *,
        first_not_before_ms: int | None = None,
        first_observed_at_ms: int | None = None,
    ) -> V07PaperCycleEvaluation:
        return V07PaperCycleEvaluation(
            event_cluster_id=event_cluster_id,
            event_cluster_alias=event_cluster_alias,
            decision_tau_seconds=decision_tau_seconds,
            cycle=cycle,
            simulation=simulation,
            market_q_5_up=market5,
            market_q_15_up=market15,
            raw_top_ask_q_5_up=raw_market5,
            raw_top_ask_q_15_up=raw_market15,
            signal_source_event_ids=source_ids,
            shrinkage=(
                health.shrinkage
                if decision.probability_diagnostics_applicable
                else None
            ),
            validation_veto=(
                health.validation_veto
                if decision.probability_diagnostics_applicable
                else None
            ),
            locked_oos=locked_oos,
            forecast_available_at_ms=health.forecast_available_at_ms,
            forecast_availability_basis=health.forecast_availability_basis,
            first_execution_not_before_ms=first_not_before_ms,
            first_execution_observed_at_ms=first_observed_at_ms,
        )

    if decision.action is PairAction.NO_TRADE:
        return wrap(
            PairPaperCycleEvaluation(
                decision=decision,
                execution=None,
                settlement=None,
                reason_codes=(),
                expiry_ms=pair.expires_at_ms,
            )
        )

    assert decision.first_token_id is not None
    assert decision.second_token_id is not None
    first_contract = _contract_for_token(pair, decision.first_token_id)
    second_contract = _contract_for_token(pair, decision.second_token_id)
    first_not_before_ms = (
        health.forecast_available_at_ms + first_contract.taker_delay_ms
    )
    first = replay.first_executable_book(
        token_id=decision.first_token_id,
        not_before_ms=first_not_before_ms,
        maximum_wait_ms=config.maximum_book_staleness_ms,
    )
    if first is None:
        return wrap(
            PairPaperCycleEvaluation(
                decision=decision,
                execution=None,
                settlement=None,
                reason_codes=("first_leg_execution_book_missing",),
                expiry_ms=pair.expires_at_ms,
            ),
            first_not_before_ms=first_not_before_ms,
        )

    first_observable_ms = max(first.source_at_ms, first.received_at_ms)
    second_deadline_ms = first_observable_ms + config.max_leg_delay_ms
    second_not_before_ms = first_observable_ms + second_contract.taker_delay_ms
    second = (
        None
        if second_not_before_ms > second_deadline_ms
        else replay.first_executable_book(
            token_id=decision.second_token_id,
            not_before_ms=second_not_before_ms,
            maximum_wait_ms=second_deadline_ms - second_not_before_ms,
        )
    )
    if second is None:
        second = replay.first_executable_book(
            token_id=decision.second_token_id,
            not_before_ms=second_deadline_ms + 1,
            maximum_wait_ms=config.maximum_book_staleness_ms,
        )
        if second is None:
            return wrap(
                PairPaperCycleEvaluation(
                    decision=decision,
                    execution=None,
                    settlement=None,
                    reason_codes=("second_leg_timeout_surface_missing",),
                    expiry_ms=pair.expires_at_ms,
                ),
                first_not_before_ms=first_not_before_ms,
                first_observed_at_ms=first_observable_ms,
            )

    second_is_timely = (
        max(second.source_at_ms, second.received_at_ms) <= second_deadline_ms
    )
    hedge_result_observable_ms = (
        max(second.source_at_ms, second.received_at_ms)
        if second_is_timely
        else second_deadline_ms
    )
    unwind_not_before_ms = hedge_result_observable_ms + first_contract.taker_delay_ms
    unwind = replay.first_executable_book(
        token_id=decision.first_token_id,
        not_before_ms=unwind_not_before_ms,
        maximum_wait_ms=config.maximum_book_staleness_ms,
    )
    execution_clock_decision = replace(
        decision,
        decision_at_ms=health.forecast_available_at_ms,
    )
    execution = execute_pair_paper(
        decision=execution_clock_decision,
        pair=pair,
        first_book=_observable_snapshot(first),
        second_book=_observable_snapshot(second),
        # The placeholder only determines whether residual first-leg inventory
        # would need an unwind.  An absent causal unwind surface still invalidates
        # that replay below rather than receiving the placeholder price.
        unwind_book=_observable_snapshot(unwind or first),
        first_source_event_id=first.source_event_id,
        second_source_event_id=second.source_event_id,
        unwind_source_event_id=(unwind or first).source_event_id,
        initial_cash=config.initial_cash,
        max_leg_delay_ms=config.max_leg_delay_ms,
        max_book_age_ms=config.maximum_book_staleness_ms,
    )
    if unwind is None and execution.unwind_leg is not None:
        return wrap(
            PairPaperCycleEvaluation(
                decision=decision,
                execution=None,
                settlement=None,
                reason_codes=("unwind_execution_book_missing",),
                expiry_ms=pair.expires_at_ms,
            ),
            first_not_before_ms=first_not_before_ms,
            first_observed_at_ms=first_observable_ms,
        )
    settlement = None
    if market_5_up is not None and market_15_up is not None:
        settlement = settle_pair_paper_execution(
            decision=decision,
            pair=pair,
            execution=execution,
            market_5_up=market_5_up,
            market_15_up=market_15_up,
        )
    reasons: tuple[str, ...] = ()
    if execution.status is PairExecutionStatus.NO_FILL:
        reasons = ("first_leg_unfilled",)
    elif execution.status is PairExecutionStatus.FAILED_UNHEDGED:
        reasons = ("failed_unhedged",)
    elif settlement is None:
        reasons = ("settlement_missing",)
    return wrap(
        PairPaperCycleEvaluation(
            decision=decision,
            execution=execution,
            settlement=settlement,
            reason_codes=reasons,
            expiry_ms=pair.expires_at_ms,
        ),
        first_not_before_ms=first_not_before_ms,
        first_observed_at_ms=first_observable_ms,
    )


__all__ = [
    "V07PaperCycleEvaluation",
    "evaluate_shared_terminal_paper_cycle",
]
