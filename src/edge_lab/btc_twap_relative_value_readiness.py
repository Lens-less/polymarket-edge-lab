"""Readiness and fail-closed gate helpers for BTC 5m/15m shared-terminal work."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING

from .btc_twap_pair_pricing import (
    PairExecutionMode,
    PairPricingPolicy,
    joint_quantity_breakpoints,
    quote_pair_buy,
    select_healthy_pair_books,
)
from .btc_twap_relative_value import (
    OrderBookSnapshot,
    PairAction,
    PairSettlementState,
    SameExpiryPair,
)
from .btc_twap_relative_value_v07 import (
    SharedTerminalPayoffStructure,
    StrikeOrdering,
)

if TYPE_CHECKING:
    from .btc_twap_relative_value_v07_evaluation import V07LockedOOSEvaluation

ZERO = Decimal(0)
ONE = Decimal(1)
PROBE_ALL_IN_LIMIT = Decimal("0.99")
EXPECTED_RTDS_INTERVAL_MS = 1_000
MAXIMUM_RTDS_GAP_MS = 2_000
MINIMUM_SETTLED_CLUSTERS = 100
MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS = 100
MINIMUM_STRUCTURAL_SHADOW_EXPIRIES = 200
MAXIMUM_SINGLE_EXPIRY_PNL_CONCENTRATION = Decimal("0.20")
MAXIMUM_CAPTURE_FAILURE_RATE = Decimal("0.05")
MINIMUM_CAPTURE_FREE_DISK_BYTES = 10 * 1024**3
MAXIMUM_PROJECTED_DAILY_CAPTURE_BYTES = 1024**3
MINIMUM_CAPTURE_MEMORY_BYTES = 2 * 1024**3
DEFAULT_PAIR_PRICING_POLICY = PairPricingPolicy(structural_only=True)
GATE_0_PAIR_PRICING_POLICY = PairPricingPolicy(
    pair_risk_usdc=None,
    structural_only=True,
)


def _outcome_key(*, actual_5_up: bool, actual_15_up: bool) -> str:
    if actual_5_up and actual_15_up:
        return "5up_15up"
    if actual_5_up and not actual_15_up:
        return "5up_15down"
    if (not actual_5_up) and actual_15_up:
        return "5down_15up"
    return "5down_15down"


def _pair_action_specs(
    pair: SameExpiryPair,
) -> Mapping[PairAction, tuple[str, str]]:
    return MappingProxyType(
        {
            PairAction.LONG_15_UP_LONG_5_DOWN: (
                pair.market_15.up_token_id,
                pair.market_5.down_token_id,
            ),
            PairAction.LONG_5_UP_LONG_15_DOWN: (
                pair.market_5.up_token_id,
                pair.market_15.down_token_id,
            ),
        }
    )


def _structural_floor_actions(
    strike_ordering: StrikeOrdering,
) -> tuple[PairAction, ...]:
    if strike_ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN:
        return (PairAction.LONG_5_UP_LONG_15_DOWN,)
    if strike_ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN:
        return (PairAction.LONG_15_UP_LONG_5_DOWN,)
    return (
        PairAction.LONG_15_UP_LONG_5_DOWN,
        PairAction.LONG_5_UP_LONG_15_DOWN,
    )


@dataclass(frozen=True)
class StructuralFloorLevel:
    quantity: Decimal
    action: PairAction
    first_token_id: str
    second_token_id: str
    notional_per_pair: Decimal
    fee_per_pair: Decimal
    all_in_cost_per_pair: Decimal
    worst_case_payoff_per_pair: Decimal
    structural_net_floor_per_pair: Decimal
    guaranteed_total_pnl: Decimal
    deterministic_floor_exists: bool
    positive_edge_after_cost: bool
    probe_buffer_ok: bool
    execution_mode: PairExecutionMode = PairExecutionMode.TAKER_TAKER
    maker_token_ids: tuple[str, ...] = ()

    def to_document(self) -> dict[str, object]:
        return {
            "quantity": format(self.quantity.normalize(), "f"),
            "action": self.action.value,
            "first_token_id": self.first_token_id,
            "second_token_id": self.second_token_id,
            "notional_per_pair": format(self.notional_per_pair.normalize(), "f"),
            "fee_per_pair": format(self.fee_per_pair.normalize(), "f"),
            "all_in_cost_per_pair": format(self.all_in_cost_per_pair.normalize(), "f"),
            "worst_case_payoff_per_pair": format(
                self.worst_case_payoff_per_pair.normalize(), "f"
            ),
            "structural_net_floor_per_pair": format(
                self.structural_net_floor_per_pair.normalize(), "f"
            ),
            "guaranteed_total_pnl": format(self.guaranteed_total_pnl.normalize(), "f"),
            "deterministic_floor_exists": self.deterministic_floor_exists,
            "positive_edge_after_cost": self.positive_edge_after_cost,
            "probe_buffer_ok": self.probe_buffer_ok,
            "execution_mode": self.execution_mode.value,
            "maker_token_ids": list(self.maker_token_ids),
        }


@dataclass(frozen=True)
class StructuralFloorVerdict:
    strike_ordering: StrikeOrdering
    market_5_rule_hash: str
    market_15_rule_hash: str
    allowed_actions: tuple[PairAction, ...]
    selected_action: PairAction | None
    selected_first_token_id: str | None
    selected_second_token_id: str | None
    depth_ladder: tuple[StructuralFloorLevel, ...]
    split_probability_live_fallback_allowed: bool = False

    @property
    def selected_level(self) -> StructuralFloorLevel | None:
        if not self.depth_ladder:
            return None
        return min(
            self.depth_ladder,
            key=lambda item: (
                -item.guaranteed_total_pnl,
                -item.structural_net_floor_per_pair,
                item.quantity,
                item.action.value,
            ),
        )

    @property
    def deterministic_floor_exists(self) -> bool:
        selected = self.selected_level
        return selected is not None and selected.deterministic_floor_exists

    @property
    def positive_edge_after_cost(self) -> bool:
        selected = self.selected_level
        return selected is not None and selected.positive_edge_after_cost

    @property
    def probe_buffer_ok(self) -> bool:
        selected = self.selected_level
        return selected is not None and selected.probe_buffer_ok

    def to_document(self) -> dict[str, object]:
        selected = self.selected_level
        return {
            "strike_ordering": self.strike_ordering.value,
            "market_5_rule_hash": self.market_5_rule_hash,
            "market_15_rule_hash": self.market_15_rule_hash,
            "allowed_actions": [action.value for action in self.allowed_actions],
            "selected_action": (
                None if self.selected_action is None else self.selected_action.value
            ),
            "selected_first_token_id": self.selected_first_token_id,
            "selected_second_token_id": self.selected_second_token_id,
            "deterministic_floor_exists": self.deterministic_floor_exists,
            "probe_buffer_ok": self.probe_buffer_ok,
            "selected_level": (None if selected is None else selected.to_document()),
            "depth_ladder": [level.to_document() for level in self.depth_ladder],
            "split_probability_live_fallback_allowed": (
                self.split_probability_live_fallback_allowed
            ),
            "positive_edge_after_cost": self.positive_edge_after_cost,
            "all_in_limit_per_pair": format(ONE, "f"),
            "probe_all_in_limit_per_pair": format(PROBE_ALL_IN_LIMIT, "f"),
        }


def validate_structural_floor(
    *,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    books: Mapping[str, OrderBookSnapshot],
    pricing_policy: PairPricingPolicy = DEFAULT_PAIR_PRICING_POLICY,
    probe_all_in_limit: Decimal = PROBE_ALL_IN_LIMIT,
    execution_mode: PairExecutionMode = PairExecutionMode.TAKER_TAKER,
) -> StructuralFloorVerdict:
    if (
        settlement_state.market_5_rule_hash != pair.market_5.rule_hash
        or settlement_state.market_15_rule_hash != pair.market_15.rule_hash
    ):
        raise ValueError("co-terminal validator rule hashes do not match the pair")
    if (
        settlement_state.market_5_open_timestamp_ms != pair.market_5.opens_at_ms
        or settlement_state.market_15_open_timestamp_ms != pair.market_15.opens_at_ms
    ):
        raise ValueError(
            "co-terminal validator opening timestamps do not match the pair"
        )
    structure = SharedTerminalPayoffStructure.from_strikes(
        strike_5=settlement_state.strike_5,
        strike_15=settlement_state.strike_15,
    )
    all_specs = _pair_action_specs(pair)
    allowed_actions = _structural_floor_actions(structure.strike_ordering)

    ladder: list[StructuralFloorLevel] = []
    for action in allowed_actions:
        first_token_id, second_token_id = all_specs[action]
        first_contract = (
            pair.market_15
            if first_token_id
            in {
                pair.market_15.up_token_id,
                pair.market_15.down_token_id,
            }
            else pair.market_5
        )
        second_contract = (
            pair.market_15
            if second_token_id
            in {
                pair.market_15.up_token_id,
                pair.market_15.down_token_id,
            }
            else pair.market_5
        )
        selected_books = select_healthy_pair_books(
            first_token_id=first_token_id,
            second_token_id=second_token_id,
            books=books,
            policy=pricing_policy,
            first_contract=first_contract,
            second_contract=second_contract,
            execution_mode=execution_mode,
        )
        if selected_books is None:
            continue
        first_book, second_book = selected_books
        quantities = joint_quantity_breakpoints(
            first_book=first_book,
            second_book=second_book,
            first_contract=first_contract,
            second_contract=second_contract,
            policy=pricing_policy,
            execution_mode=execution_mode,
        )
        worst_case_payoff = structure.worst_case_payoff(action)
        for quantity in quantities:
            quote = quote_pair_buy(
                quantity=quantity,
                first_book=first_book,
                second_book=second_book,
                first_contract=first_contract,
                second_contract=second_contract,
                execution_mode=execution_mode,
            )
            if quote is None:
                continue
            floor = worst_case_payoff - quote.cost_per_pair
            all_in = quote.cost_per_pair
            ladder.append(
                StructuralFloorLevel(
                    quantity=quantity,
                    action=action,
                    first_token_id=first_token_id,
                    second_token_id=second_token_id,
                    notional_per_pair=quote.notional_per_pair,
                    fee_per_pair=quote.fee_per_pair,
                    all_in_cost_per_pair=all_in,
                    worst_case_payoff_per_pair=worst_case_payoff,
                    structural_net_floor_per_pair=floor,
                    guaranteed_total_pnl=quantity * floor,
                    deterministic_floor_exists=all_in <= ONE and floor >= ZERO,
                    positive_edge_after_cost=floor > ZERO,
                    probe_buffer_ok=all_in <= probe_all_in_limit and floor > ZERO,
                    execution_mode=quote.execution_mode,
                    maker_token_ids=quote.maker_token_ids,
                )
            )
    selected = (
        None
        if not ladder
        else min(
            ladder,
            key=lambda item: (
                -item.guaranteed_total_pnl,
                -item.structural_net_floor_per_pair,
                item.quantity,
                item.action.value,
            ),
        )
    )
    return StructuralFloorVerdict(
        strike_ordering=structure.strike_ordering,
        market_5_rule_hash=settlement_state.market_5_rule_hash,
        market_15_rule_hash=settlement_state.market_15_rule_hash,
        allowed_actions=allowed_actions,
        selected_action=None if selected is None else selected.action,
        selected_first_token_id=None if selected is None else selected.first_token_id,
        selected_second_token_id=(
            None if selected is None else selected.second_token_id
        ),
        depth_ladder=tuple(ladder),
    )


@dataclass(frozen=True)
class PerfectInformationAttempt:
    attempt_id: str
    pair: SameExpiryPair
    settlement_state: PairSettlementState
    books: Mapping[str, OrderBookSnapshot]
    actual_5_up: bool
    actual_15_up: bool
    pricing_policy: PairPricingPolicy = GATE_0_PAIR_PRICING_POLICY
    execution_mode: PairExecutionMode = PairExecutionMode.TAKER_TAKER


@dataclass(frozen=True)
class PerfectInformationBreakpoint:
    quantity: Decimal
    action: PairAction
    execution_mode: PairExecutionMode
    maker_token_ids: tuple[str, ...]
    realized_payoff_per_pair: Decimal
    all_in_cost_per_pair: Decimal
    realized_net_pnl_per_pair: Decimal
    realized_total_pnl: Decimal

    def to_document(self) -> dict[str, object]:
        return {
            "quantity": str(self.quantity),
            "action": self.action.value,
            "execution_mode": self.execution_mode.value,
            "maker_token_ids": list(self.maker_token_ids),
            "realized_payoff_per_pair": str(self.realized_payoff_per_pair),
            "all_in_cost_per_pair": str(self.all_in_cost_per_pair),
            "realized_net_pnl_per_pair": str(self.realized_net_pnl_per_pair),
            "realized_total_pnl": str(self.realized_total_pnl),
        }


@dataclass(frozen=True)
class PerfectInformationAttemptUpperBound:
    attempt_id: str
    strike_ordering: StrikeOrdering
    breakpoints: tuple[PerfectInformationBreakpoint, ...]
    per_action_best_total_pnl: Mapping[str, Decimal]
    best_action: PairAction | None
    best_quantity: Decimal | None
    best_total_pnl: Decimal
    unrestricted_best_action: PairAction | None
    unrestricted_best_quantity: Decimal | None
    unrestricted_best_total_pnl: Decimal
    gate_0_passed: bool

    def to_document(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "strike_ordering": self.strike_ordering.value,
            "breakpoints": [item.to_document() for item in self.breakpoints],
            "per_action_best_total_pnl": {
                key: str(value) for key, value in self.per_action_best_total_pnl.items()
            },
            "best_action": None if self.best_action is None else self.best_action.value,
            "best_quantity": (
                None if self.best_quantity is None else str(self.best_quantity)
            ),
            "best_total_pnl": str(self.best_total_pnl),
            "unrestricted_best_action": (
                None
                if self.unrestricted_best_action is None
                else self.unrestricted_best_action.value
            ),
            "unrestricted_best_quantity": (
                None
                if self.unrestricted_best_quantity is None
                else str(self.unrestricted_best_quantity)
            ),
            "unrestricted_best_total_pnl": str(
                self.unrestricted_best_total_pnl
            ),
            "gate_0_passed": self.gate_0_passed,
            "no_trade_available": True,
        }


@dataclass(frozen=True)
class PerfectInformationUpperBoundReport:
    attempts: tuple[PerfectInformationAttemptUpperBound, ...]
    aggregate_best_total_pnl: Decimal
    unrestricted_hindsight_aggregate_best_total_pnl: Decimal
    per_action_total_pnl: Mapping[str, Decimal]
    gate_0_passed: bool
    stop_recommended: bool

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": "btc-5m-15m-perfect-information-upper-bound.v1",
            "diagnostic_only": True,
            "counts_as_locked_oos_evidence": False,
            "attempt_count": len(self.attempts),
            "attempts": [attempt.to_document() for attempt in self.attempts],
            "aggregate_best_total_pnl": str(self.aggregate_best_total_pnl),
            "unrestricted_hindsight_aggregate_best_total_pnl": str(
                self.unrestricted_hindsight_aggregate_best_total_pnl
            ),
            "per_action_total_pnl": {
                key: str(value) for key, value in self.per_action_total_pnl.items()
            },
            "per_action_gate_0_passed": {
                key: value > ZERO for key, value in self.per_action_total_pnl.items()
            },
            "per_action_stop_recommended": {
                key: value <= ZERO for key, value in self.per_action_total_pnl.items()
            },
            "gate_0_passed": self.gate_0_passed,
            "stop_recommended": self.stop_recommended,
            "gate_0_route": "structural_floor_only",
        }


def evaluate_perfect_information_upper_bound(
    attempts: Sequence[PerfectInformationAttempt],
) -> PerfectInformationUpperBoundReport:
    attempt_ids = [attempt.attempt_id for attempt in attempts]
    if any(
        not isinstance(attempt_id, str) or not attempt_id for attempt_id in attempt_ids
    ):
        raise ValueError("perfect-information attempt ids must be non-empty")
    if len(set(attempt_ids)) != len(attempt_ids):
        raise ValueError(
            "perfect-information attempt ids must be unique common expiries"
        )
    per_attempt: list[PerfectInformationAttemptUpperBound] = []
    per_action: defaultdict[str, Decimal] = defaultdict(lambda: ZERO)
    structural_aggregate = ZERO
    unrestricted_aggregate = ZERO
    for attempt in attempts:
        structure = SharedTerminalPayoffStructure.from_strikes(
            strike_5=attempt.settlement_state.strike_5,
            strike_15=attempt.settlement_state.strike_15,
        )
        outcome = _outcome_key(
            actual_5_up=attempt.actual_5_up,
            actual_15_up=attempt.actual_15_up,
        )
        if outcome not in structure.feasible_outcomes:
            raise ValueError(
                f"attempt {attempt.attempt_id} label violates shared-terminal strike ordering"
            )
        structural_actions = set(_structural_floor_actions(structure.strike_ordering))
        candidates: list[PerfectInformationBreakpoint] = []
        per_attempt_best_by_action: defaultdict[str, Decimal] = defaultdict(
            lambda: Decimal("-Infinity")
        )
        for action, token_ids in _pair_action_specs(attempt.pair).items():
            first_token_id, second_token_id = token_ids
            first_contract = (
                attempt.pair.market_15
                if first_token_id
                in {
                    attempt.pair.market_15.up_token_id,
                    attempt.pair.market_15.down_token_id,
                }
                else attempt.pair.market_5
            )
            second_contract = (
                attempt.pair.market_15
                if second_token_id
                in {
                    attempt.pair.market_15.up_token_id,
                    attempt.pair.market_15.down_token_id,
                }
                else attempt.pair.market_5
            )
            selected_books = select_healthy_pair_books(
                first_token_id=first_token_id,
                second_token_id=second_token_id,
                books=attempt.books,
                policy=attempt.pricing_policy,
                first_contract=first_contract,
                second_contract=second_contract,
                execution_mode=attempt.execution_mode,
            )
            if selected_books is None:
                per_attempt_best_by_action[action.value] = ZERO
                continue
            first_book, second_book = selected_books
            quantities = joint_quantity_breakpoints(
                first_book=first_book,
                second_book=second_book,
                first_contract=first_contract,
                second_contract=second_contract,
                policy=attempt.pricing_policy,
                execution_mode=attempt.execution_mode,
            )
            realized_payoff = structure.payoff_by_outcome(action)[outcome]
            for quantity in quantities:
                quote = quote_pair_buy(
                    quantity=quantity,
                    first_book=first_book,
                    second_book=second_book,
                    first_contract=first_contract,
                    second_contract=second_contract,
                    execution_mode=attempt.execution_mode,
                )
                if quote is None:
                    continue
                realized_net = realized_payoff - quote.cost_per_pair
                breakpoint = PerfectInformationBreakpoint(
                    quantity=quantity,
                    action=action,
                    execution_mode=quote.execution_mode,
                    maker_token_ids=quote.maker_token_ids,
                    realized_payoff_per_pair=realized_payoff,
                    all_in_cost_per_pair=quote.cost_per_pair,
                    realized_net_pnl_per_pair=realized_net,
                    realized_total_pnl=quantity * realized_net,
                )
                candidates.append(breakpoint)
                per_attempt_best_by_action[action.value] = max(
                    per_attempt_best_by_action[action.value],
                    breakpoint.realized_total_pnl,
                )
            per_attempt_best_by_action[action.value] = max(
                ZERO,
                per_attempt_best_by_action[action.value],
            )
        if not candidates:
            per_action_best = dict(sorted(per_attempt_best_by_action.items()))
            for action_name, total_pnl in per_action_best.items():
                per_action[action_name] += total_pnl
            per_attempt.append(
                PerfectInformationAttemptUpperBound(
                    attempt_id=attempt.attempt_id,
                    strike_ordering=structure.strike_ordering,
                    breakpoints=(),
                    per_action_best_total_pnl=MappingProxyType(per_action_best),
                    best_action=None,
                    best_quantity=None,
                    best_total_pnl=ZERO,
                    unrestricted_best_action=None,
                    unrestricted_best_quantity=None,
                    unrestricted_best_total_pnl=ZERO,
                    gate_0_passed=False,
                )
            )
            continue
        per_action_best = dict(sorted(per_attempt_best_by_action.items()))
        for action_name, total_pnl in per_action_best.items():
            per_action[action_name] += total_pnl
        unrestricted_positive_candidates = [
            candidate for candidate in candidates if candidate.realized_total_pnl > ZERO
        ]
        unrestricted_best = (
            None
            if not unrestricted_positive_candidates
            else max(
                unrestricted_positive_candidates,
                key=lambda item: (
                    item.realized_total_pnl,
                    item.realized_net_pnl_per_pair,
                    -item.quantity,
                    item.action.value,
                ),
            )
        )
        structural_non_negative_candidates = [
            candidate
            for candidate in candidates
            if candidate.action in structural_actions
            and candidate.realized_total_pnl >= ZERO
        ]
        best = (
            None
            if not structural_non_negative_candidates
            else max(
                structural_non_negative_candidates,
                key=lambda item: (
                    item.realized_total_pnl,
                    item.realized_net_pnl_per_pair,
                    -item.quantity,
                    item.action.value,
                ),
            )
        )
        best_total = ZERO if best is None else best.realized_total_pnl
        unrestricted_best_total = (
            ZERO
            if unrestricted_best is None
            else unrestricted_best.realized_total_pnl
        )
        structural_aggregate += best_total
        unrestricted_aggregate += unrestricted_best_total
        per_attempt.append(
            PerfectInformationAttemptUpperBound(
                attempt_id=attempt.attempt_id,
                strike_ordering=structure.strike_ordering,
                breakpoints=tuple(candidates),
                per_action_best_total_pnl=MappingProxyType(per_action_best),
                best_action=None if best is None else best.action,
                best_quantity=None if best is None else best.quantity,
                best_total_pnl=best_total,
                unrestricted_best_action=(
                    None if unrestricted_best is None else unrestricted_best.action
                ),
                unrestricted_best_quantity=(
                    None if unrestricted_best is None else unrestricted_best.quantity
                ),
                unrestricted_best_total_pnl=unrestricted_best_total,
                gate_0_passed=best_total > ZERO,
            )
        )
    return PerfectInformationUpperBoundReport(
        attempts=tuple(per_attempt),
        aggregate_best_total_pnl=structural_aggregate,
        unrestricted_hindsight_aggregate_best_total_pnl=unrestricted_aggregate,
        per_action_total_pnl=MappingProxyType(dict(sorted(per_action.items()))),
        gate_0_passed=structural_aggregate > ZERO,
        stop_recommended=structural_aggregate <= ZERO,
    )


@dataclass(frozen=True)
class CohortCoverageInput:
    cohort_id: str
    market_15_open_ms: int
    common_expiry_ms: int
    rtds_observed_at_ms: tuple[int, ...]
    rtds_received_at_ms: tuple[int, ...]
    market_5_l2_complete: bool
    market_15_l2_complete: bool
    market_5_trades_complete: bool
    market_15_trades_complete: bool
    fee_complete: bool
    rule_complete: bool
    source_timestamps_complete: bool
    receipt_timestamps_complete: bool
    disconnect_count: int = 0
    error_count: int = 0
    clock_sync_valid: bool = True


@dataclass(frozen=True)
class CohortCoverageResult:
    cohort_id: str
    complete: bool
    reason_codes: tuple[str, ...]


def validate_cohort_data_coverage(
    cohort: CohortCoverageInput,
    *,
    maximum_rtds_gap_ms: int = MAXIMUM_RTDS_GAP_MS,
    maximum_clock_drift_ms: int = 5_000,
) -> CohortCoverageResult:
    reasons: list[str] = []
    observed = cohort.rtds_observed_at_ms
    received = cohort.rtds_received_at_ms
    if not observed or not received:
        reasons.append("official_rtds_missing")
    else:
        if observed[0] > cohort.market_15_open_ms:
            reasons.append("official_rtds_starts_after_15m_open")
        if observed[-1] < cohort.common_expiry_ms:
            reasons.append("official_rtds_ends_before_common_expiry")
        if len(observed) != len(received):
            reasons.append("official_rtds_timestamp_count_mismatch")
        if any(current < previous for previous, current in pairwise(observed)):
            reasons.append("official_rtds_source_timestamps_not_monotonic")
        for previous, current in pairwise(observed):
            if current - previous > maximum_rtds_gap_ms:
                reasons.append("official_rtds_gap_detected")
                break
        for observed_at, received_at in zip(observed, received):
            if received_at + maximum_clock_drift_ms < observed_at:
                reasons.append("receipt_timestamp_precedes_source_timestamp")
                break
        if any(current < previous for previous, current in pairwise(received)):
            reasons.append("receipt_timestamps_not_monotonic")
    if cohort.disconnect_count > 0:
        reasons.append("official_rtds_disconnect_observed")
    if cohort.error_count > 0:
        reasons.append("official_rtds_error_observed")
    if not cohort.clock_sync_valid:
        reasons.append("clock_sync_invalid")
    if not cohort.market_5_l2_complete:
        reasons.append("market_5_l2_incomplete")
    if not cohort.market_15_l2_complete:
        reasons.append("market_15_l2_incomplete")
    if not cohort.market_5_trades_complete:
        reasons.append("market_5_trades_incomplete")
    if not cohort.market_15_trades_complete:
        reasons.append("market_15_trades_incomplete")
    if not cohort.fee_complete:
        reasons.append("fee_metadata_incomplete")
    if not cohort.rule_complete:
        reasons.append("rule_metadata_incomplete")
    if not cohort.source_timestamps_complete:
        reasons.append("source_timestamps_incomplete")
    if not cohort.receipt_timestamps_complete:
        reasons.append("receipt_timestamps_incomplete")
    return CohortCoverageResult(
        cohort_id=cohort.cohort_id,
        complete=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True)
class ExecutionProbePrerequisites:
    service_continuously_healthy: bool = False
    authenticated_read_verified: bool = False
    fill_stream_verified: bool = False
    failure_drills_complete: bool = False
    immutable_probe_preregistration_present: bool = False
    full_hedge_depth_verified: bool = False
    gate_0_passed: bool = False
    double_maker_probe_implemented: bool = False
    capture_capacity_gate_passed: bool = False


DEFAULT_PROBE_PREREQUISITES = ExecutionProbePrerequisites()


@dataclass(frozen=True)
class CaptureCapacityEvidence:
    capture_attempt_count: int
    capture_failure_count: int
    free_disk_bytes: int
    projected_daily_capture_bytes: int
    available_memory_bytes: int
    burstable_cpu_credit_exhausted: bool

    def __post_init__(self) -> None:
        for name in (
            "capture_attempt_count",
            "capture_failure_count",
            "free_disk_bytes",
            "projected_daily_capture_bytes",
            "available_memory_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.capture_attempt_count == 0:
            raise ValueError("capture_attempt_count must be positive")
        if self.capture_failure_count > self.capture_attempt_count:
            raise ValueError("capture failures cannot exceed attempts")
        if not isinstance(self.burstable_cpu_credit_exhausted, bool):
            raise TypeError("burstable_cpu_credit_exhausted must be bool")


@dataclass(frozen=True)
class CaptureCapacityVerdict:
    passed: bool
    failure_rate: Decimal
    reason_codes: tuple[str, ...]

    def to_document(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "failure_rate": str(self.failure_rate),
            "reason_codes": list(self.reason_codes),
            "maximum_capture_failure_rate": str(MAXIMUM_CAPTURE_FAILURE_RATE),
            "minimum_free_disk_bytes": MINIMUM_CAPTURE_FREE_DISK_BYTES,
            "maximum_projected_daily_capture_bytes": (
                MAXIMUM_PROJECTED_DAILY_CAPTURE_BYTES
            ),
            "minimum_available_memory_bytes": MINIMUM_CAPTURE_MEMORY_BYTES,
            "burstable_cpu_credit_exhaustion_allowed": False,
        }


def evaluate_capture_capacity(
    evidence: CaptureCapacityEvidence,
) -> CaptureCapacityVerdict:
    if not isinstance(evidence, CaptureCapacityEvidence):
        raise TypeError("evidence must be CaptureCapacityEvidence")
    failure_rate = Decimal(evidence.capture_failure_count) / Decimal(
        evidence.capture_attempt_count
    )
    reasons: list[str] = []
    if failure_rate > MAXIMUM_CAPTURE_FAILURE_RATE:
        reasons.append("capture_failure_rate_above_5pct")
    if evidence.free_disk_bytes < MINIMUM_CAPTURE_FREE_DISK_BYTES:
        reasons.append("capture_free_disk_below_10gib")
    if (
        evidence.projected_daily_capture_bytes
        > MAXIMUM_PROJECTED_DAILY_CAPTURE_BYTES
    ):
        reasons.append("projected_daily_capture_above_1gib")
    if evidence.available_memory_bytes < MINIMUM_CAPTURE_MEMORY_BYTES:
        reasons.append("capture_memory_below_2gib")
    if evidence.burstable_cpu_credit_exhausted:
        reasons.append("burstable_cpu_credit_exhausted")
    return CaptureCapacityVerdict(
        passed=not reasons,
        failure_rate=failure_rate,
        reason_codes=tuple(reasons),
    )


@dataclass(frozen=True)
class NeutralShadowEvidence:
    realized_net_pnl: Decimal | None
    receipt_chain_verified: bool
    all_admitted_cohorts_included: bool
    scenario: str = "neutral"
    expiry_count: int = 0
    without_best_expiry_net_pnl: Decimal | None = None
    without_best_direction_net_pnl: Decimal | None = None
    minimum_rolling_window_net_pnl: Decimal | None = None
    max_single_expiry_pnl_concentration: Decimal | None = None

    def __post_init__(self) -> None:
        if self.scenario != "neutral":
            raise ValueError("execution-probe shadow evidence must be neutral")
        if (
            isinstance(self.expiry_count, bool)
            or not isinstance(self.expiry_count, int)
            or self.expiry_count < 0
        ):
            raise ValueError("neutral shadow expiry_count must be non-negative")
        for name in (
            "realized_net_pnl",
            "without_best_expiry_net_pnl",
            "without_best_direction_net_pnl",
            "minimum_rolling_window_net_pnl",
            "max_single_expiry_pnl_concentration",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, (bool, float)):
                raise TypeError(f"{name} must be an exact decimal")
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
            if not parsed.is_finite():
                raise ValueError(f"{name} must be finite")
            if name == "max_single_expiry_pnl_concentration" and parsed < ZERO:
                raise ValueError("shadow concentration cannot be negative")
            object.__setattr__(self, name, parsed)
        for name in ("receipt_chain_verified", "all_admitted_cohorts_included"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")


@dataclass(frozen=True)
class ExecutionProbeReadiness:
    eligible: bool
    clean_common_terminal_cohort_count: int
    reason_codes: tuple[str, ...]


def evaluate_execution_probe_readiness(
    *,
    neutral_shadow_evidence: NeutralShadowEvidence | None,
    clean_common_terminal_cohort_count: int,
    coverage_results: Sequence[CohortCoverageResult],
    structural_floor: StructuralFloorVerdict | None,
    prerequisites: ExecutionProbePrerequisites = DEFAULT_PROBE_PREREQUISITES,
) -> ExecutionProbeReadiness:
    reasons: list[str] = []
    unique_complete_cohort_count = len(
        {result.cohort_id for result in coverage_results if result.complete}
    )
    if neutral_shadow_evidence is None:
        reasons.append("neutral_shadow_evidence_unverified")
    else:
        if (
            neutral_shadow_evidence.realized_net_pnl is None
            or neutral_shadow_evidence.realized_net_pnl <= ZERO
        ):
            reasons.append("neutral_shadow_net_pnl_not_positive")
        if not neutral_shadow_evidence.receipt_chain_verified:
            reasons.append("neutral_shadow_receipt_chain_unverified")
        if not neutral_shadow_evidence.all_admitted_cohorts_included:
            reasons.append("neutral_shadow_denominator_incomplete")
        if (
            neutral_shadow_evidence.expiry_count
            < MINIMUM_STRUCTURAL_SHADOW_EXPIRIES
        ):
            reasons.append("fewer_than_200_neutral_shadow_expiries")
        if (
            neutral_shadow_evidence.without_best_expiry_net_pnl is None
            or neutral_shadow_evidence.without_best_expiry_net_pnl <= ZERO
        ):
            reasons.append("neutral_shadow_not_positive_without_best_expiry")
        if (
            neutral_shadow_evidence.without_best_direction_net_pnl is None
            or neutral_shadow_evidence.without_best_direction_net_pnl <= ZERO
        ):
            reasons.append("neutral_shadow_not_positive_without_best_direction")
        if (
            neutral_shadow_evidence.minimum_rolling_window_net_pnl is None
            or neutral_shadow_evidence.minimum_rolling_window_net_pnl <= ZERO
        ):
            reasons.append("neutral_shadow_rolling_window_not_positive")
        if neutral_shadow_evidence.max_single_expiry_pnl_concentration is None:
            reasons.append("neutral_shadow_concentration_unverified")
        elif (
            neutral_shadow_evidence.max_single_expiry_pnl_concentration
            > MAXIMUM_SINGLE_EXPIRY_PNL_CONCENTRATION
        ):
            reasons.append("neutral_shadow_concentration_above_20pct")
    if clean_common_terminal_cohort_count < 4:
        reasons.append("fewer_than_4_candidate_common_terminal_cohorts")
    if unique_complete_cohort_count < 4:
        reasons.append("fewer_than_4_unique_complete_common_terminal_cohorts")
    if clean_common_terminal_cohort_count != unique_complete_cohort_count:
        reasons.append("clean_common_terminal_cohort_count_mismatch")
    if not coverage_results:
        reasons.append("cohort_coverage_unverified")
    elif any(not result.complete for result in coverage_results):
        reasons.append("cohort_coverage_incomplete")
    if structural_floor is None or not structural_floor.deterministic_floor_exists:
        reasons.append("structural_floor_unavailable")
    elif not structural_floor.positive_edge_after_cost:
        reasons.append("structural_floor_not_positive_after_cost")
    elif not structural_floor.probe_buffer_ok:
        reasons.append("all_in_cost_exceeds_0_99_probe_buffer")
    elif (
        structural_floor.selected_level is None
        or structural_floor.selected_level.execution_mode
        is not PairExecutionMode.MAKER_MAKER
    ):
        reasons.append("structural_floor_not_double_maker")
    if not prerequisites.service_continuously_healthy:
        reasons.append("service_continuous_health_unverified")
    if not prerequisites.authenticated_read_verified:
        reasons.append("authenticated_read_unverified")
    if not prerequisites.fill_stream_verified:
        reasons.append("fill_stream_unverified")
    if not prerequisites.failure_drills_complete:
        reasons.append("failure_drills_incomplete")
    if not prerequisites.immutable_probe_preregistration_present:
        reasons.append("immutable_probe_preregistration_missing")
    if not prerequisites.full_hedge_depth_verified:
        reasons.append("full_hedge_depth_unverified")
    if not prerequisites.gate_0_passed:
        reasons.append("gate_0_not_passed")
    if not prerequisites.double_maker_probe_implemented:
        reasons.append("double_maker_probe_not_implemented")
    if not prerequisites.capture_capacity_gate_passed:
        reasons.append("capture_capacity_gate_not_passed")
    return ExecutionProbeReadiness(
        eligible=not reasons,
        clean_common_terminal_cohort_count=unique_complete_cohort_count,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True)
class StrategyLiveReadiness:
    eligible: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class StrategyLiveInputs:
    builder_verified_evidence_chain: bool
    auditable_prelabel_lock_evidence: bool
    clean_prelabeled_common_terminal_cohort_count: int
    structural_settled_expiry_cluster_count: int
    structural_explainable_economic_attempt_count: int
    structural_bootstrap_cluster_mean_lower_95: Decimal | None
    structural_true_edge_gate_satisfied: bool
    structural_qualified_net_pnl: Decimal | None
    structural_gate_0_passed: bool
    structural_max_single_expiry_pnl_concentration: Decimal | None
    complete_real_execution_evidence: bool
    all_locked_cohorts_in_pnl_distribution: bool
    service_continuously_healthy: bool


def evaluate_strategy_live_readiness_inputs(
    inputs: StrategyLiveInputs,
) -> StrategyLiveReadiness:
    reasons: list[str] = []
    if not inputs.builder_verified_evidence_chain:
        reasons.append("builder_verified_evidence_chain_missing")
    if not inputs.auditable_prelabel_lock_evidence:
        reasons.append("prelabel_lock_evidence_missing")
    if inputs.clean_prelabeled_common_terminal_cohort_count < MINIMUM_SETTLED_CLUSTERS:
        reasons.append("fewer_than_100_clean_prelabeled_common_terminal_cohorts")
    if inputs.structural_settled_expiry_cluster_count < MINIMUM_SETTLED_CLUSTERS:
        reasons.append("structural_fewer_than_100_distinct_settled_expiry_clusters")
    if (
        inputs.structural_explainable_economic_attempt_count
        < MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
    ):
        reasons.append(
            "structural_fewer_than_100_explainable_locked_oos_economic_attempts"
        )
    if inputs.structural_bootstrap_cluster_mean_lower_95 is None or (
        inputs.structural_bootstrap_cluster_mean_lower_95 <= ZERO
    ):
        reasons.append("structural_expiry_cluster_bootstrap_lower_95_not_above_zero")
    if not inputs.structural_true_edge_gate_satisfied:
        reasons.append("structural_true_edge_gate_not_satisfied")
    if (
        inputs.structural_qualified_net_pnl is None
        or inputs.structural_qualified_net_pnl <= ZERO
    ):
        reasons.append("structural_qualified_net_pnl_not_positive")
    if not inputs.structural_gate_0_passed:
        reasons.append("structural_gate_0_not_passed")
    concentration = inputs.structural_max_single_expiry_pnl_concentration
    if concentration is None or concentration > MAXIMUM_SINGLE_EXPIRY_PNL_CONCENTRATION:
        reasons.append("structural_single_expiry_pnl_concentration_above_20_percent")
    if not inputs.complete_real_execution_evidence:
        reasons.append("complete_real_execution_evidence_missing")
    if not inputs.all_locked_cohorts_in_pnl_distribution:
        reasons.append("locked_no_trade_no_fill_cohorts_missing_from_distribution")
    if not inputs.service_continuously_healthy:
        reasons.append("service_continuous_health_unverified")
    return StrategyLiveReadiness(
        eligible=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def evaluate_strategy_live_readiness(
    evaluation: V07LockedOOSEvaluation,
    *,
    clean_prelabeled_common_terminal_cohort_count: int = 0,
    structural_gate_0_passed: bool = False,
) -> StrategyLiveReadiness:
    return evaluate_strategy_live_readiness_inputs(
        StrategyLiveInputs(
            builder_verified_evidence_chain=evaluation.builder_verified_evidence_chain,
            auditable_prelabel_lock_evidence=(
                evaluation.auditable_prelabel_lock_evidence
            ),
            clean_prelabeled_common_terminal_cohort_count=(
                clean_prelabeled_common_terminal_cohort_count
            ),
            structural_settled_expiry_cluster_count=(
                evaluation.structural_settled_expiry_cluster_count
            ),
            structural_explainable_economic_attempt_count=(
                evaluation.structural_explainable_economic_attempt_count
            ),
            structural_bootstrap_cluster_mean_lower_95=(
                evaluation.structural_bootstrap_cluster_mean_lower_95
            ),
            structural_true_edge_gate_satisfied=(
                evaluation.structural_true_edge_gate_satisfied
            ),
            structural_qualified_net_pnl=evaluation.structural_qualified_net_pnl,
            structural_gate_0_passed=structural_gate_0_passed,
            structural_max_single_expiry_pnl_concentration=None,
            complete_real_execution_evidence=False,
            all_locked_cohorts_in_pnl_distribution=False,
            service_continuously_healthy=False,
        )
    )


__all__ = [
    "CaptureCapacityEvidence",
    "CaptureCapacityVerdict",
    "EXPECTED_RTDS_INTERVAL_MS",
    "MAXIMUM_RTDS_GAP_MS",
    "MINIMUM_STRUCTURAL_SHADOW_EXPIRIES",
    "NeutralShadowEvidence",
    "PROBE_ALL_IN_LIMIT",
    "CohortCoverageInput",
    "CohortCoverageResult",
    "ExecutionProbePrerequisites",
    "ExecutionProbeReadiness",
    "PerfectInformationAttempt",
    "PerfectInformationAttemptUpperBound",
    "PerfectInformationBreakpoint",
    "PerfectInformationUpperBoundReport",
    "StrategyLiveInputs",
    "StrategyLiveReadiness",
    "StructuralFloorLevel",
    "StructuralFloorVerdict",
    "evaluate_execution_probe_readiness",
    "evaluate_capture_capacity",
    "evaluate_perfect_information_upper_bound",
    "evaluate_strategy_live_readiness",
    "evaluate_strategy_live_readiness_inputs",
    "validate_cohort_data_coverage",
    "validate_structural_floor",
]
