"""Auditable semantic constraints and depth-priced bundle opportunities.

The module is deliberately self contained and read-only.  It turns verified
event rules into enumerated binary payoff states; execution code is outside its
scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from hashlib import sha256
from itertools import product
import json
from math import gcd, lcm
from types import MappingProxyType
from typing import Mapping, Sequence

from .models import FeeSchedule, OrderBook


FEE_QUANTUM = Decimal("0.00001")
SHARE_SCALE = Decimal("1000000")
SHARE_SCALE_INT = 1_000_000


def _text_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical_decimal(value: Decimal) -> str:
    """Represent equal finite Decimal values identically for audit hashes."""
    if not value.is_finite():
        return str(value)
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


def _is_nonnegative_int(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _audit_integer(value: object) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return repr(value)


def _audit_text(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return repr(value)


def _evidence_present(values: Sequence[str]) -> bool:
    return bool(values) and all(
        isinstance(value, str) and bool(value.strip()) for value in values
    )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _snapshot_evidence_record(
    evidence: object,
) -> dict[str, int | str | None]:
    if not isinstance(evidence, BookSnapshotEvidence):
        return {
            "book_hash": None,
            "received_at_ms": None,
            "rtt_ms": None,
        }
    return {
        "book_hash": _audit_text(evidence.book_hash),
        "received_at_ms": _audit_integer(evidence.received_at_ms),
        "rtt_ms": _audit_integer(evidence.rtt_ms),
    }


def _minimum_executable_quantity_step(units: Decimal) -> Fraction:
    """Return the smallest terminating q whose leg order is one microshare."""
    units_fraction = Fraction(units)
    numerator = abs(units_fraction.numerator)
    denominator = units_fraction.denominator
    smooth_numerator = 1
    for factor in (2, 5):
        while numerator % factor == 0:
            numerator //= factor
            smooth_numerator *= factor
    return Fraction(
        denominator,
        SHARE_SCALE_INT * smooth_numerator,
    )


def _fraction_lcm(left: Fraction, right: Fraction) -> Fraction:
    """Least positive value that is an integer multiple of both rationals."""
    return Fraction(
        lcm(left.numerator, right.numerator),
        gcd(left.denominator, right.denominator),
    )


def _terminating_fraction_to_decimal(value: Fraction) -> Decimal:
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("fraction has no terminating decimal representation")
    scale = max(twos, fives)
    coefficient = (
        abs(value.numerator)
        * (2 ** (scale - twos))
        * (5 ** (scale - fives))
    )
    digits = tuple(int(character) for character in str(coefficient))
    return Decimal(
        (
            int(value.numerator < 0),
            digits or (0,),
            -scale,
        )
    )


def _quantity_at_step(
    bound: Fraction,
    step: Fraction,
    *,
    round_up: bool,
) -> Decimal:
    ratio = bound / step
    if round_up:
        multiple = -(-ratio.numerator // ratio.denominator)
    else:
        multiple = ratio.numerator // ratio.denominator
    return _terminating_fraction_to_decimal(step * multiple)


@dataclass(frozen=True)
class PredicateNode:
    """A binary market predicate together with its source evidence."""

    node_id: str
    event_id: str
    market_id: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    question: str
    rules: str
    resolution_source: str
    role: str = "named"
    evidence: tuple[str, ...] = field(default_factory=tuple)
    payoff_domain: tuple[Decimal, ...] = (
        Decimal("0"),
        Decimal("1"),
    )

    @property
    def rules_hash(self) -> str:
        return _text_hash(self.rules)

    @property
    def resolution_source_hash(self) -> str:
        return _text_hash(self.resolution_source)

    def _canonical_record(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "question": self.question,
            "rules_hash": self.rules_hash,
            "resolution_source_hash": self.resolution_source_hash,
            "role": self.role,
            "evidence": list(self.evidence),
            "payoff_domain": [
                _canonical_decimal(value) for value in self.payoff_domain
            ],
        }


@dataclass(frozen=True)
class Constraint:
    """A rule-derived relationship between predicate nodes."""

    constraint_id: str
    kind: str
    node_ids: tuple[str, ...]
    verified: bool = False
    evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def normalized_kind(self) -> str:
        return self.kind.lower().replace("-", "_")

    def accepts(self, state: Mapping[str, int]) -> bool:
        values = tuple(state[node_id] for node_id in self.node_ids)
        kind = self.normalized_kind
        if kind == "complement":
            return len(values) == 2 and sum(values) == 1
        if kind == "implication":
            return len(values) == 2 and values[0] <= values[1]
        if kind == "equivalence":
            return len(values) == 2 and values[0] == values[1]
        if kind == "mutex":
            return sum(values) <= 1
        if kind == "exhaustive":
            return sum(values) >= 1
        if kind in {"exactly_one", "exactlyone"}:
            return sum(values) == 1
        return False

    def _canonical_record(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "kind": self.normalized_kind,
            "node_ids": list(self.node_ids),
            "verified": self.verified,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class PayoffStates:
    states: tuple[dict[str, int], ...]
    failure_codes: tuple[str, ...]
    graph_hash: str

    @property
    def blocked(self) -> bool:
        return bool(self.failure_codes)


@dataclass(frozen=True)
class TransformResult:
    failure_codes: tuple[str, ...]
    index_set: int | None
    amount_out: Decimal
    collateral_delta: Decimal
    token_deltas: Mapping[str, Decimal]

    @property
    def blocked(self) -> bool:
        return bool(self.failure_codes)


@dataclass(frozen=True)
class InventoryTransform:
    """An explicit inventory conversion, never inferred from Gamma ordering."""

    transform_id: str
    kind: str
    quantity: Decimal
    selected_market_ids: tuple[str, ...] = field(default_factory=tuple)
    fee_bips: int = 0
    chain_index_map: Mapping[str, int] | None = None
    expected_graph_hash: str | None = None
    collateral_decimals: int = 6

    @classmethod
    def neg_risk_convert(
        cls,
        *,
        transform_id: str,
        quantity: Decimal,
        selected_market_ids: tuple[str, ...],
        fee_bips: int,
        chain_index_map: Mapping[str, int],
        expected_graph_hash: str,
        collateral_decimals: int = 6,
    ) -> "InventoryTransform":
        return cls(
            transform_id=transform_id,
            kind="neg_risk_convert",
            quantity=quantity,
            selected_market_ids=selected_market_ids,
            fee_bips=fee_bips,
            chain_index_map=dict(chain_index_map),
            expected_graph_hash=expected_graph_hash,
            collateral_decimals=collateral_decimals,
        )

    def evaluate(
        self,
        graph: "ConstraintGraph",
        *,
        inventory: Mapping[str, Decimal],
    ) -> TransformResult:
        if not self.expected_graph_hash:
            return TransformResult(
                failure_codes=("graph_hash_unpinned",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        graph_result = graph.enumerate_payoff_states(
            expected_graph_hash=self.expected_graph_hash
        )
        if graph_result.blocked:
            return TransformResult(
                failure_codes=graph_result.failure_codes,
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if not graph.neg_risk:
            return TransformResult(
                failure_codes=("neg_risk_required",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if self.kind != "neg_risk_convert":
            return TransformResult(
                failure_codes=("transform_kind_unsupported",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if self.chain_index_map is None:
            return TransformResult(
                failure_codes=("index_mapping_missing",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if dict(self.chain_index_map) != dict(graph.chain_index_map or {}):
            return TransformResult(
                failure_codes=("index_mapping_mismatch",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if self.fee_bips != graph.adapter_fee_bips:
            return TransformResult(
                failure_codes=("adapter_fee_mismatch",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if self.collateral_decimals != graph.collateral_decimals:
            return TransformResult(
                failure_codes=("collateral_decimals_mismatch",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )

        nodes_by_market = {node.market_id: node for node in graph.nodes}
        selected = tuple(dict.fromkeys(self.selected_market_ids))
        if (
            not selected
            or any(market_id not in nodes_by_market for market_id in selected)
        ):
            return TransformResult(
                failure_codes=("transform_market_invalid",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if (
            not self.quantity.is_finite()
            or self.quantity <= 0
            or not 0 <= self.fee_bips <= 10_000
            or not 0 <= self.collateral_decimals <= 18
        ):
            return TransformResult(
                failure_codes=("transform_terms_invalid",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        collateral_scale = Decimal(10) ** self.collateral_decimals
        quantity_base_units = self.quantity * collateral_scale
        if quantity_base_units != quantity_base_units.to_integral_value():
            return TransformResult(
                failure_codes=("transform_precision_invalid",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )
        if any(
            (
                not inventory.get(
                    nodes_by_market[market_id].no_token_id,
                    Decimal("0"),
                ).is_finite()
                or inventory.get(
                    nodes_by_market[market_id].no_token_id,
                    Decimal("0"),
                )
                < self.quantity
            )
            for market_id in selected
        ):
            return TransformResult(
                failure_codes=("transform_input_missing",),
                index_set=None,
                amount_out=Decimal("0"),
                collateral_delta=Decimal("0"),
                token_deltas={},
            )

        quantity_units = int(quantity_base_units)
        fee_units = quantity_units * self.fee_bips // 10_000
        amount_out = (
            Decimal(quantity_units - fee_units) / collateral_scale
        )
        selected_set = set(selected)
        token_deltas: dict[str, Decimal] = {}
        index_set = 0
        for market_id in selected:
            node = nodes_by_market[market_id]
            token_deltas[node.no_token_id] = -self.quantity
            index_set |= 1 << graph.chain_index_for(market_id)
        for node in graph.nodes:
            if node.market_id not in selected_set:
                token_deltas[node.yes_token_id] = amount_out

        return TransformResult(
            failure_codes=(),
            index_set=index_set,
            amount_out=amount_out,
            collateral_delta=Decimal(len(selected) - 1) * amount_out,
            token_deltas=token_deltas,
        )


@dataclass(frozen=True)
class BundleLeg:
    """One per-bundle outcome-token trade."""

    node_id: str
    outcome: str
    side: str
    units: Decimal = Decimal("1")


@dataclass(frozen=True)
class BookSnapshotEvidence:
    """Local receipt evidence not present in the public OrderBook schema."""

    book_hash: str
    received_at_ms: int
    rtt_ms: int


@dataclass(frozen=True)
class LegFill:
    token_id: str
    side: str
    requested_size: Decimal
    filled_size: Decimal
    notional: Decimal
    fee: Decimal
    complete: bool
    levels: tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True)
class Opportunity:
    """An opportunity or rejection; both are retained for audit."""

    opportunity_id: str
    family: str
    event_id: str
    constraint_ids: tuple[str, ...]
    fee_source: str | Mapping[str, str] | None
    quantity: Decimal
    accepted: bool
    failure_code: str
    failure_codes: tuple[str, ...]
    graph_hash: str
    gross_cash_flow: Decimal
    taker_fees: Decimal
    gas: Decimal
    latency_buffer: Decimal
    worst_case_payoff: Decimal
    diagnostic_edge: Decimal
    rounding_uncertainty: Decimal
    rounding_coverage_guard: Decimal
    net_edge: Decimal | None
    leg_fills: tuple[LegFill, ...]
    audit_inputs_json: str

    def to_record(self) -> dict[str, object]:
        """Return a JSON-safe record without converting Decimal to float."""
        fee_source = (
            dict(self.fee_source)
            if isinstance(self.fee_source, Mapping)
            else self.fee_source
        )
        return {
            "opportunity_id": self.opportunity_id,
            "family": self.family,
            "event_id": self.event_id,
            "constraint_ids": list(self.constraint_ids),
            "fee_source": fee_source,
            "quantity": str(self.quantity),
            "accepted": self.accepted,
            "failure_code": self.failure_code,
            "failure_codes": list(self.failure_codes),
            "graph_hash": self.graph_hash,
            "gross_cash_flow": str(self.gross_cash_flow),
            "taker_fees": str(self.taker_fees),
            "gas": str(self.gas),
            "latency_buffer": str(self.latency_buffer),
            "worst_case_payoff": str(self.worst_case_payoff),
            "diagnostic_edge": str(self.diagnostic_edge),
            "rounding_uncertainty": str(self.rounding_uncertainty),
            "rounding_coverage_guard": str(
                self.rounding_coverage_guard
            ),
            "net_edge": (
                str(self.net_edge) if self.net_edge is not None else None
            ),
            "audit_inputs": json.loads(self.audit_inputs_json),
            "leg_fills": [
                {
                    "token_id": fill.token_id,
                    "side": fill.side,
                    "requested_size": str(fill.requested_size),
                    "filled_size": str(fill.filled_size),
                    "notional": str(fill.notional),
                    "fee": str(fill.fee),
                    "complete": fill.complete,
                    "levels": [
                        {"price": str(price), "size": str(size)}
                        for price, size in fill.levels
                    ],
                }
                for fill in self.leg_fills
            ],
        }


def _append_failure(failures: list[str], code: str) -> None:
    if code not in failures:
        failures.append(code)


def _token_for_leg(node: PredicateNode, leg: BundleLeg) -> str | None:
    outcome = leg.outcome.upper()
    if outcome == "YES":
        return node.yes_token_id
    if outcome == "NO":
        return node.no_token_id
    return None


def _fee_terms_for_token(
    fee_schedule: FeeSchedule | Mapping[str, FeeSchedule] | None,
    fee_source: str | Mapping[str, str] | None,
    token_id: str,
) -> tuple[FeeSchedule | None, str | None]:
    schedule = (
        fee_schedule.get(token_id)
        if isinstance(fee_schedule, Mapping)
        else fee_schedule
    )
    source = (
        fee_source.get(token_id)
        if isinstance(fee_source, Mapping)
        else fee_source
    )
    return schedule, source


def _fee_terms_valid(schedule: FeeSchedule) -> bool:
    decimals = (schedule.rate, schedule.exponent, schedule.rebate_rate)
    return (
        all(value.is_finite() for value in decimals)
        and Decimal("0") <= schedule.rate <= Decimal("1")
        and schedule.exponent > 0
        and Decimal("0") <= schedule.rebate_rate <= Decimal("1")
        and isinstance(schedule.taker_only, bool)
    )


def _level_taker_fee(
    shares: Decimal,
    price: Decimal,
    schedule: FeeSchedule,
) -> Decimal:
    if shares <= 0 or schedule.rate <= 0:
        return Decimal("0")
    probability_term = price * (Decimal("1") - price)
    if probability_term <= 0:
        return Decimal("0")
    raw = (
        shares
        * schedule.rate
        * (probability_term**schedule.exponent)
    )
    if raw < FEE_QUANTUM:
        return Decimal("0")
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def _book_levels_valid(book: OrderBook) -> bool:
    if (
        not book.tick_size.is_finite()
        or book.tick_size <= 0
        or not book.min_order_size.is_finite()
        or book.min_order_size <= 0
    ):
        return False
    return all(
        level.price.is_finite()
        and level.size.is_finite()
        and Decimal("0") < level.price < Decimal("1")
        and level.size > 0
        and level.price % book.tick_size == 0
        for level in (*book.bids, *book.asks)
    )


def _candidate_quantities(
    legs: Sequence[BundleLeg],
    nodes_by_id: Mapping[str, PredicateNode],
    books: Mapping[str, OrderBook],
    inventory: Mapping[str, Decimal],
) -> tuple[Decimal, ...]:
    candidates: set[Decimal] = set()
    minimums: list[Decimal] = []
    sell_units_by_token: dict[str, Decimal] = {}
    quantity_step: Fraction | None = None
    for leg in legs:
        if not leg.units.is_finite() or leg.units <= 0:
            continue
        leg_step = _minimum_executable_quantity_step(leg.units)
        quantity_step = (
            leg_step
            if quantity_step is None
            else _fraction_lcm(quantity_step, leg_step)
        )
    if quantity_step is None:
        return ()

    for leg in legs:
        node = nodes_by_id.get(leg.node_id)
        if (
            node is None
            or not leg.units.is_finite()
            or leg.units <= 0
        ):
            continue
        token_id = _token_for_leg(node, leg)
        book = books.get(token_id or "")
        if book is None:
            continue
        if book.min_order_size.is_finite() and book.min_order_size > 0:
            minimums.append(
                _quantity_at_step(
                    Fraction(book.min_order_size) / Fraction(leg.units),
                    quantity_step,
                    round_up=True,
                )
            )
        side = leg.side.upper()
        raw_levels = book.asks if side == "BUY" else book.bids
        levels = sorted(
            (
                level
                for level in raw_levels
                if level.price.is_finite()
                and level.size.is_finite()
                and level.size > 0
            ),
            key=lambda level: level.price,
            reverse=side == "SELL",
        )
        cumulative = Fraction(0)
        for level in levels:
            cumulative += Fraction(level.size)
            executable_quantity = _quantity_at_step(
                cumulative / Fraction(leg.units),
                quantity_step,
                round_up=False,
            )
            if executable_quantity > 0:
                candidates.add(executable_quantity)
        if leg.side.upper() == "SELL":
            sell_units_by_token[token_id or ""] = (
                sell_units_by_token.get(token_id or "", Decimal("0"))
                + leg.units
            )
    for token_id, units in sell_units_by_token.items():
        available = inventory.get(token_id, Decimal("0"))
        if available.is_finite() and available > 0 and units > 0:
            executable_quantity = _quantity_at_step(
                Fraction(available) / Fraction(units),
                quantity_step,
                round_up=False,
            )
            if executable_quantity > 0:
                candidates.add(executable_quantity)
    if minimums:
        candidates.add(max(minimums))
    return tuple(sorted(quantity for quantity in candidates if quantity > 0))


def _evaluate_bundle_quantity(
    *,
    graph: "ConstraintGraph",
    graph_states: PayoffStates,
    family: str,
    legs: Sequence[BundleLeg],
    books: Mapping[str, OrderBook],
    fee_schedule: FeeSchedule | Mapping[str, FeeSchedule] | None,
    fee_source: str | Mapping[str, str] | None,
    quantity: Decimal,
    gas: Decimal,
    latency_buffer: Decimal,
    inventory: Mapping[str, Decimal],
    expected_graph_hash: str,
    observed_at_ms: int,
    max_book_age_ms: int,
    max_book_skew_ms: int,
    max_book_rtt_ms: int,
    book_evidence: Mapping[str, BookSnapshotEvidence],
) -> Opportunity:
    failures = list(graph_states.failure_codes)
    nodes_by_id = {node.node_id: node for node in graph.nodes}
    quantity_is_valid = quantity.is_finite() and quantity > 0
    if not quantity_is_valid:
        _append_failure(failures, "quantity_invalid")
    pricing_quantity = quantity if quantity_is_valid else Decimal("0")
    buffers_are_valid = (
        gas.is_finite()
        and latency_buffer.is_finite()
        and gas >= 0
        and latency_buffer >= 0
    )
    if not buffers_are_valid:
        _append_failure(failures, "buffer_invalid")
    pricing_gas = gas if gas.is_finite() and gas >= 0 else Decimal("0")
    pricing_latency_buffer = (
        latency_buffer
        if latency_buffer.is_finite() and latency_buffer >= 0
        else Decimal("0")
    )
    snapshot_policy_is_valid = all(
        _is_nonnegative_int(value)
        for value in (
            observed_at_ms,
            max_book_age_ms,
            max_book_skew_ms,
            max_book_rtt_ms,
        )
    )
    if not snapshot_policy_is_valid:
        _append_failure(failures, "snapshot_policy_invalid")

    resolved: list[
        tuple[BundleLeg, PredicateNode, str, OrderBook, FeeSchedule | None]
    ] = []
    sell_requirements: dict[str, Decimal] = {}
    seen_tokens: set[str] = set()
    for leg in legs:
        node = nodes_by_id.get(leg.node_id)
        if node is None:
            _append_failure(failures, "bundle_node_unknown")
            continue
        token_id = _token_for_leg(node, leg)
        if token_id is None or leg.side.upper() not in {"BUY", "SELL"}:
            _append_failure(failures, "bundle_leg_invalid")
            continue
        if not leg.units.is_finite() or leg.units <= 0:
            _append_failure(failures, "bundle_leg_invalid")
            continue
        if token_id in seen_tokens:
            _append_failure(failures, "duplicate_bundle_token")
            continue
        seen_tokens.add(token_id)
        book = books.get(token_id)
        if book is None:
            _append_failure(failures, "book_missing")
            continue
        if book.token_id != token_id:
            _append_failure(failures, "book_token_mismatch")
            continue
        if book.condition_id != node.condition_id:
            _append_failure(failures, "book_condition_mismatch")
            continue
        if (
            not isinstance(book.neg_risk, bool)
            or book.neg_risk != graph.neg_risk
        ):
            _append_failure(failures, "book_neg_risk_mismatch")
            continue
        if not _book_levels_valid(book):
            _append_failure(failures, "book_level_invalid")
            continue
        leg_fee_schedule, leg_fee_source = _fee_terms_for_token(
            fee_schedule, fee_source, token_id
        )
        if (
            leg_fee_schedule is None
            or not isinstance(leg_fee_source, str)
            or not leg_fee_source.strip()
        ):
            _append_failure(failures, "fee_unknown")
        elif (
            not isinstance(leg_fee_schedule, FeeSchedule)
            or not _fee_terms_valid(leg_fee_schedule)
        ):
            _append_failure(failures, "fee_terms_invalid")
        requested = pricing_quantity * leg.units
        scaled_requested = requested * SHARE_SCALE
        if scaled_requested != scaled_requested.to_integral_value():
            _append_failure(failures, "quantity_precision_invalid")
        if requested < book.min_order_size:
            _append_failure(failures, "below_min_size")
        if book.tick_size <= 0:
            _append_failure(failures, "tick_invalid")
        if leg.side.upper() == "SELL":
            sell_requirements[token_id] = (
                sell_requirements.get(token_id, Decimal("0")) + requested
            )
        resolved.append((leg, node, token_id, book, leg_fee_schedule))

    book_timestamps = tuple(
        book.timestamp_ms for _, _, _, book, _ in resolved
    )
    relevant_evidence = tuple(
        book_evidence.get(token_id)
        for _, _, token_id, _, _ in resolved
    )
    if resolved and snapshot_policy_is_valid:
        snapshots_are_trusted = True
        for timestamp, evidence in zip(
            book_timestamps,
            relevant_evidence,
            strict=True,
        ):
            if (
                not _is_nonnegative_int(timestamp)
                or not isinstance(evidence, BookSnapshotEvidence)
                or not _is_sha256(evidence.book_hash)
                or not _is_nonnegative_int(evidence.received_at_ms)
                or not _is_nonnegative_int(evidence.rtt_ms)
            ):
                snapshots_are_trusted = False
                break
            if (
                timestamp > observed_at_ms
                or observed_at_ms - timestamp > max_book_age_ms
                or evidence.received_at_ms > observed_at_ms
                or observed_at_ms - evidence.received_at_ms
                > max_book_age_ms
                or evidence.received_at_ms < timestamp
                or evidence.rtt_ms > max_book_rtt_ms
            ):
                snapshots_are_trusted = False
                break
        if snapshots_are_trusted and len(book_timestamps) > 1:
            snapshots_are_trusted = (
                max(book_timestamps) - min(book_timestamps)
                <= max_book_skew_ms
                and max(
                    evidence.received_at_ms
                    for evidence in relevant_evidence
                    if isinstance(evidence, BookSnapshotEvidence)
                )
                - min(
                    evidence.received_at_ms
                    for evidence in relevant_evidence
                    if isinstance(evidence, BookSnapshotEvidence)
                )
                <= max_book_skew_ms
            )
        if not snapshots_are_trusted:
            _append_failure(failures, "stale_or_unsynced_books")

    for token_id, required in sell_requirements.items():
        available = inventory.get(token_id, Decimal("0"))
        if not available.is_finite():
            _append_failure(failures, "inventory_invalid")
        elif available < required:
            _append_failure(failures, "naked_sell")

    fills: list[LegFill] = []
    gross_cash_flow = Decimal("0")
    total_fees = Decimal("0")
    rounding_uncertainty = Decimal("0")
    payoff_positions: list[tuple[PredicateNode, str, str, Decimal]] = []
    for leg, node, token_id, book, leg_fee_schedule in resolved:
        requested = pricing_quantity * leg.units
        side = leg.side.upper()
        levels = sorted(
            book.asks if side == "BUY" else book.bids,
            key=lambda level: level.price,
            reverse=side == "SELL",
        )
        remaining = requested
        filled = Decimal("0")
        notional = Decimal("0")
        leg_fee = Decimal("0")
        consumed: list[tuple[Decimal, Decimal]] = []
        for level in levels:
            if remaining <= 0:
                break
            taken = min(remaining, level.size)
            if taken <= 0:
                continue
            if (
                book.tick_size <= 0
                or level.price <= 0
                or level.price >= 1
                or level.price % book.tick_size != 0
            ):
                _append_failure(failures, "tick_invalid")
            consumed.append((level.price, taken))
            notional += level.price * taken
            if (
                isinstance(leg_fee_schedule, FeeSchedule)
                and _fee_terms_valid(leg_fee_schedule)
            ):
                leg_fee += _level_taker_fee(
                    taken, level.price, leg_fee_schedule
                )
                if leg_fee_schedule.rate > 0:
                    rounding_uncertainty += FEE_QUANTUM
            filled += taken
            remaining -= taken
        complete = remaining <= 0
        if not complete:
            _append_failure(failures, "depth_insufficient")
        sign = Decimal("-1") if side == "BUY" else Decimal("1")
        gross_cash_flow += sign * notional
        total_fees += leg_fee
        payoff_positions.append((node, leg.outcome.upper(), side, filled))
        fills.append(
            LegFill(
                token_id=token_id,
                side=side,
                requested_size=requested,
                filled_size=filled,
                notional=notional,
                fee=leg_fee,
                complete=complete,
                levels=tuple(consumed),
            )
        )

    state_payoffs: list[Decimal] = []
    for state in graph_states.states:
        payoff = Decimal("0")
        for node, outcome, side, filled in payoff_positions:
            outcome_payoff = Decimal(
                state[node.node_id]
                if outcome == "YES"
                else 1 - state[node.node_id]
            )
            payoff += (
                outcome_payoff * filled
                if side == "BUY"
                else -outcome_payoff * filled
            )
        state_payoffs.append(payoff)
    worst_case_payoff = (
        min(state_payoffs) if state_payoffs else Decimal("0")
    )
    diagnostic_edge = (
        gross_cash_flow
        - total_fees
        - pricing_gas
        - pricing_latency_buffer
        + worst_case_payoff
    )
    conservative_edge = diagnostic_edge - rounding_uncertainty
    # The rounded fee can differ from its continuous line by at most 1.5
    # quanta per consumed level.  A two-quanta guard ensures an accepted
    # interior point implies a positive enumerated depth endpoint, so an
    # unenumerated fee jump cannot be the sole profit evidence.
    rounding_coverage_guard = rounding_uncertainty * Decimal("2")
    if not failures:
        if diagnostic_edge <= 0:
            _append_failure(failures, "edge_nonpositive")
        elif conservative_edge <= rounding_coverage_guard:
            _append_failure(
                failures, "edge_below_rounding_uncertainty"
            )
    accepted = not failures
    failure_code = "accepted" if accepted else failures[0]
    identity = {
        "graph_hash": graph.graph_hash,
        "expected_graph_hash": expected_graph_hash,
        "family": family,
        "quantity": _canonical_decimal(quantity),
        "legs": [
            {
                "node_id": leg.node_id,
                "outcome": leg.outcome.upper(),
                "side": leg.side.upper(),
                "units": _canonical_decimal(leg.units),
            }
            for leg in legs
        ],
        "fills": [
            {
                "token_id": fill.token_id,
                "side": fill.side,
                "levels": [
                    [
                        _canonical_decimal(price),
                        _canonical_decimal(size),
                    ]
                    for price, size in fill.levels
                ],
            }
            for fill in fills
        ],
        "fee_sources": {
            token_id: _audit_text(source)
            for token_id in sorted(seen_tokens)
            for _, source in [
                _fee_terms_for_token(fee_schedule, fee_source, token_id)
            ]
        },
        "fee_schedules": {
            token_id: (
                {
                    "rate": _canonical_decimal(schedule.rate),
                    "exponent": _canonical_decimal(schedule.exponent),
                    "taker_only": schedule.taker_only,
                    "rebate_rate": _canonical_decimal(
                        schedule.rebate_rate
                    ),
                }
                if isinstance(schedule, FeeSchedule)
                else None
            )
            for token_id in sorted(seen_tokens)
            for schedule, _ in [
                _fee_terms_for_token(fee_schedule, fee_source, token_id)
            ]
        },
        "gas": _canonical_decimal(gas),
        "latency_buffer": _canonical_decimal(latency_buffer),
        "fee_rounding_policy": "subquantum-zero-half-up-v1",
        "rounding_uncertainty": _canonical_decimal(rounding_uncertainty),
        "rounding_coverage_guard": _canonical_decimal(
            rounding_coverage_guard
        ),
        "observed_at_ms": _audit_integer(observed_at_ms),
        "max_book_age_ms": _audit_integer(max_book_age_ms),
        "max_book_skew_ms": _audit_integer(max_book_skew_ms),
        "max_book_rtt_ms": _audit_integer(max_book_rtt_ms),
        "inventory": {
            token_id: _canonical_decimal(
                inventory.get(token_id, Decimal("0"))
            )
            for token_id in sorted(sell_requirements)
        },
        "books": {
            token_id: {
                "token_id": book.token_id,
                "condition_id": book.condition_id,
                "timestamp_ms": _audit_integer(book.timestamp_ms),
                **_snapshot_evidence_record(
                    book_evidence.get(token_id)
                ),
                "tick_size": _canonical_decimal(book.tick_size),
                "min_order_size": _canonical_decimal(
                    book.min_order_size
                ),
                "neg_risk": (
                    book.neg_risk
                    if isinstance(book.neg_risk, bool)
                    else repr(book.neg_risk)
                ),
                "bids": [
                    [
                        _canonical_decimal(level.price),
                        _canonical_decimal(level.size),
                    ]
                    for level in book.bids
                ],
                "asks": [
                    [
                        _canonical_decimal(level.price),
                        _canonical_decimal(level.size),
                    ]
                    for level in book.asks
                ],
            }
            for token_id, book in sorted(books.items())
            if token_id in seen_tokens
        },
    }
    audit_inputs_json = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    opportunity_id = _text_hash(audit_inputs_json)
    locked_fee_source = (
        MappingProxyType(dict(identity["fee_sources"]))
        if isinstance(fee_source, Mapping)
        else _audit_text(fee_source)
    )
    locked_net_edge = (
        conservative_edge
        if accepted or failures == ["edge_nonpositive"]
        else None
    )
    return Opportunity(
        opportunity_id=opportunity_id,
        family=family,
        event_id=graph.event_id,
        constraint_ids=tuple(
            constraint.constraint_id for constraint in graph.constraints
        ),
        fee_source=locked_fee_source,
        quantity=quantity,
        accepted=accepted,
        failure_code=failure_code,
        failure_codes=tuple(failures),
        graph_hash=graph.graph_hash,
        gross_cash_flow=gross_cash_flow,
        taker_fees=total_fees,
        gas=gas,
        latency_buffer=latency_buffer,
        worst_case_payoff=worst_case_payoff,
        diagnostic_edge=diagnostic_edge,
        rounding_uncertainty=rounding_uncertainty,
        rounding_coverage_guard=rounding_coverage_guard,
        net_edge=locked_net_edge,
        leg_fills=tuple(fills),
        audit_inputs_json=audit_inputs_json,
    )


def enumerate_bundle_opportunities(
    *,
    graph: "ConstraintGraph",
    family: str,
    legs: Sequence[BundleLeg],
    books: Mapping[str, OrderBook],
    fee_schedule: FeeSchedule | Mapping[str, FeeSchedule] | None,
    fee_source: str | Mapping[str, str] | None,
    gas: Decimal = Decimal("0"),
    latency_buffer: Decimal = Decimal("0"),
    inventory: Mapping[str, Decimal] | None = None,
    quantities: Sequence[Decimal] | None = None,
    expected_graph_hash: str,
    observed_at_ms: int,
    max_book_age_ms: int,
    max_book_skew_ms: int,
    max_book_rtt_ms: int,
    book_evidence: Mapping[str, BookSnapshotEvidence],
) -> tuple[Opportunity, ...]:
    """Price an auditable bundle at every supplied or visible-depth breakpoint."""
    graph_states = graph.enumerate_payoff_states(
        expected_graph_hash=expected_graph_hash
    )
    normalized_legs = tuple(legs)
    nodes_by_id = {node.node_id: node for node in graph.nodes}
    holdings = inventory or {}
    if quantities is None:
        candidate_quantities = _candidate_quantities(
            normalized_legs, nodes_by_id, books, holdings
        )
    else:
        supplied_quantities = tuple(
            Decimal(quantity) for quantity in quantities
        )
        finite_quantities = tuple(
            sorted(
                {
                    quantity
                    for quantity in supplied_quantities
                    if quantity.is_finite()
                }
            )
        )
        nonfinite_by_text = {
            str(quantity): quantity
            for quantity in supplied_quantities
            if not quantity.is_finite()
        }
        candidate_quantities = finite_quantities + tuple(
            nonfinite_by_text[key] for key in sorted(nonfinite_by_text)
        )
    if not candidate_quantities:
        candidate_quantities = (Decimal("0"),)
    return tuple(
        _evaluate_bundle_quantity(
            graph=graph,
            graph_states=graph_states,
            family=family,
            legs=normalized_legs,
            books=books,
            fee_schedule=fee_schedule,
            fee_source=fee_source,
            quantity=quantity,
            gas=gas,
            latency_buffer=latency_buffer,
            inventory=holdings,
            expected_graph_hash=expected_graph_hash,
            observed_at_ms=observed_at_ms,
            max_book_age_ms=max_book_age_ms,
            max_book_skew_ms=max_book_skew_ms,
            max_book_rtt_ms=max_book_rtt_ms,
            book_evidence=book_evidence,
        )
        for quantity in candidate_quantities
    )


@dataclass(frozen=True)
class ConstraintGraph:
    """A versioned, evidence-backed semantic graph for one event."""

    event_id: str
    nodes: tuple[PredicateNode, ...]
    constraints: tuple[Constraint, ...] = field(default_factory=tuple)
    augmented_neg_risk: bool = False
    neg_risk: bool = False
    onchain_question_count: int | None = None
    chain_index_map: Mapping[str, int] | None = None
    adapter_address: str | None = None
    adapter_block_number: int | None = None
    adapter_fee_bips: int | None = None
    collateral_decimals: int | None = None
    chain_index_evidence: tuple[str, ...] = field(default_factory=tuple)

    @property
    def graph_hash(self) -> str:
        payload = {
            "event_id": self.event_id,
            "augmented_neg_risk": self.augmented_neg_risk,
            "neg_risk": self.neg_risk,
            "onchain_question_count": _audit_integer(
                self.onchain_question_count
            ),
            "chain_index_map": (
                sorted(
                    (
                        market_id,
                        _audit_integer(index),
                    )
                    for market_id, index in self.chain_index_map.items()
                )
                if self.chain_index_map is not None
                else None
            ),
            "adapter_address": _audit_text(self.adapter_address),
            "adapter_block_number": _audit_integer(
                self.adapter_block_number
            ),
            "adapter_fee_bips": _audit_integer(self.adapter_fee_bips),
            "collateral_decimals": _audit_integer(
                self.collateral_decimals
            ),
            "chain_index_evidence": list(self.chain_index_evidence),
            "nodes": [
                node._canonical_record()
                for node in sorted(self.nodes, key=lambda item: item.node_id)
            ],
            "constraints": [
                constraint._canonical_record()
                for constraint in sorted(
                    self.constraints, key=lambda item: item.constraint_id
                )
            ],
        }
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return _text_hash(serialized)

    def chain_index_for(self, market_id: str) -> int:
        """Return the explicit on-chain index; node/Gamma order is never used."""
        if self.chain_index_map is None or market_id not in self.chain_index_map:
            raise ValueError(f"missing explicit chain index for {market_id}")
        return int(self.chain_index_map[market_id])

    def enumerate_payoff_states(
        self, *, expected_graph_hash: str | None = None
    ) -> PayoffStates:
        if (
            expected_graph_hash is not None
            and expected_graph_hash != self.graph_hash
        ):
            return PayoffStates(
                states=(),
                failure_codes=("graph_hash_changed",),
                graph_hash=self.graph_hash,
            )
        if not self.nodes:
            return PayoffStates(
                states=(),
                failure_codes=("empty_graph",),
                graph_hash=self.graph_hash,
            )
        node_ids_in_order = tuple(node.node_id for node in self.nodes)
        if len(set(node_ids_in_order)) != len(node_ids_in_order):
            return PayoffStates(
                states=(),
                failure_codes=("node_id_duplicate",),
                graph_hash=self.graph_hash,
            )
        if any(node.event_id != self.event_id for node in self.nodes):
            return PayoffStates(
                states=(),
                failure_codes=("node_event_mismatch",),
                graph_hash=self.graph_hash,
            )
        identity_values = (
            self.event_id,
            *(
                value
                for node in self.nodes
                for value in (
                    node.node_id,
                    node.market_id,
                    node.condition_id,
                    node.yes_token_id,
                    node.no_token_id,
                )
            ),
        )
        if any(not value.strip() for value in identity_values):
            return PayoffStates(
                states=(),
                failure_codes=("node_identity_missing",),
                graph_hash=self.graph_hash,
            )
        market_ids = tuple(node.market_id for node in self.nodes)
        if len(set(market_ids)) != len(market_ids):
            return PayoffStates(
                states=(),
                failure_codes=("market_id_duplicate",),
                graph_hash=self.graph_hash,
            )
        condition_ids = tuple(node.condition_id for node in self.nodes)
        if len(set(condition_ids)) != len(condition_ids):
            return PayoffStates(
                states=(),
                failure_codes=("condition_id_duplicate",),
                graph_hash=self.graph_hash,
            )
        token_ids = tuple(
            token_id
            for node in self.nodes
            for token_id in (node.yes_token_id, node.no_token_id)
        )
        if len(set(token_ids)) != len(token_ids):
            return PayoffStates(
                states=(),
                failure_codes=("token_id_duplicate",),
                graph_hash=self.graph_hash,
            )
        if any(
            tuple(node.payoff_domain)
            != (Decimal("0"), Decimal("1"))
            for node in self.nodes
        ):
            return PayoffStates(
                states=(),
                failure_codes=("non_binary_payoff_domain",),
                graph_hash=self.graph_hash,
            )
        provenance_failures: list[str] = []
        if any(not node.rules.strip() for node in self.nodes):
            provenance_failures.append("rules_missing")
        if any(not node.resolution_source.strip() for node in self.nodes):
            provenance_failures.append("resolution_source_missing")
        if any(
            not _evidence_present(node.evidence) for node in self.nodes
        ):
            provenance_failures.append("node_evidence_missing")
        if provenance_failures:
            return PayoffStates(
                states=(),
                failure_codes=tuple(provenance_failures),
                graph_hash=self.graph_hash,
            )
        if self.augmented_neg_risk:
            return PayoffStates(
                states=(),
                failure_codes=("augmented_neg_risk_blocked",),
                graph_hash=self.graph_hash,
            )
        if self.neg_risk and self.chain_index_map is None:
            return PayoffStates(
                states=(),
                failure_codes=("index_mapping_missing",),
                graph_hash=self.graph_hash,
            )
        if self.neg_risk:
            mapping_failures: list[str] = []
            if self.onchain_question_count is None:
                mapping_failures.append("question_count_unknown")
            elif not _is_nonnegative_int(self.onchain_question_count):
                mapping_failures.append("question_count_invalid")
            else:
                if self.onchain_question_count <= 1:
                    return PayoffStates(
                        states=(),
                        failure_codes=("no_convertible_positions",),
                        graph_hash=self.graph_hash,
                    )
                if (
                    len(self.nodes) != self.onchain_question_count
                    or len(self.chain_index_map or {})
                    != self.onchain_question_count
                ):
                    mapping_failures.append("question_count_mismatch")
                expected_market_ids = {node.market_id for node in self.nodes}
                expected_indices = set(range(self.onchain_question_count))
                actual_mapping = self.chain_index_map or {}
                if (
                    set(actual_mapping) != expected_market_ids
                    or any(
                        not _is_nonnegative_int(index)
                        for index in actual_mapping.values()
                    )
                    or set(actual_mapping.values()) != expected_indices
                ):
                    mapping_failures.append("index_mapping_mismatch")
            if mapping_failures:
                return PayoffStates(
                    states=(),
                    failure_codes=tuple(mapping_failures),
                    graph_hash=self.graph_hash,
                )
            graph_node_ids = {node.node_id for node in self.nodes}
            has_verified_mutex = any(
                constraint.verified
                and constraint.evidence
                and constraint.normalized_kind
                in {"mutex", "exactly_one", "exactlyone"}
                and set(constraint.node_ids) == graph_node_ids
                and len(set(constraint.node_ids)) == len(constraint.node_ids)
                for constraint in self.constraints
            )
            if not has_verified_mutex:
                return PayoffStates(
                    states=(),
                    failure_codes=("neg_risk_mutex_missing",),
                    graph_hash=self.graph_hash,
                )
            if (
                not isinstance(self.adapter_address, str)
                or not self.adapter_address.strip()
                or self.adapter_block_number is None
                or self.adapter_fee_bips is None
                or self.collateral_decimals is None
                or not _evidence_present(self.chain_index_evidence)
            ):
                return PayoffStates(
                    states=(),
                    failure_codes=("adapter_provenance_missing",),
                    graph_hash=self.graph_hash,
                )
            if (
                not _is_nonnegative_int(self.adapter_block_number)
                or not _is_nonnegative_int(self.adapter_fee_bips)
                or self.adapter_fee_bips > 10_000
                or not _is_nonnegative_int(self.collateral_decimals)
                or self.collateral_decimals > 18
            ):
                return PayoffStates(
                    states=(),
                    failure_codes=("adapter_provenance_invalid",),
                    graph_hash=self.graph_hash,
                )
        roles = {node.role.lower() for node in self.nodes}
        if "other" in roles:
            return PayoffStates(
                states=(),
                failure_codes=("other_mutable",),
                graph_hash=self.graph_hash,
            )
        if "placeholder" in roles:
            return PayoffStates(
                states=(),
                failure_codes=("placeholder",),
                graph_hash=self.graph_hash,
            )
        if "ambiguous" in roles:
            return PayoffStates(
                states=(),
                failure_codes=("semantic_ambiguous",),
                graph_hash=self.graph_hash,
            )
        if roles - {"named", "other", "placeholder", "ambiguous"}:
            return PayoffStates(
                states=(),
                failure_codes=("node_role_unknown",),
                graph_hash=self.graph_hash,
            )
        constraint_ids = tuple(
            constraint.constraint_id for constraint in self.constraints
        )
        if any(not constraint_id.strip() for constraint_id in constraint_ids):
            return PayoffStates(
                states=(),
                failure_codes=("constraint_identity_missing",),
                graph_hash=self.graph_hash,
            )
        if len(set(constraint_ids)) != len(constraint_ids):
            return PayoffStates(
                states=(),
                failure_codes=("constraint_id_duplicate",),
                graph_hash=self.graph_hash,
            )
        if any(not constraint.verified for constraint in self.constraints):
            return PayoffStates(
                states=(),
                failure_codes=("semantic_unverified",),
                graph_hash=self.graph_hash,
            )
        if any(
            not _evidence_present(constraint.evidence)
            for constraint in self.constraints
        ):
            return PayoffStates(
                states=(),
                failure_codes=("constraint_evidence_missing",),
                graph_hash=self.graph_hash,
            )
        node_ids = tuple(sorted(node.node_id for node in self.nodes))
        node_id_set = set(node_ids)
        binary_kinds = {"complement", "implication", "equivalence"}
        group_kinds = {"mutex", "exhaustive", "exactly_one", "exactlyone"}
        for constraint in self.constraints:
            if len(set(constraint.node_ids)) != len(constraint.node_ids):
                return PayoffStates(
                    states=(),
                    failure_codes=("constraint_node_duplicate",),
                    graph_hash=self.graph_hash,
                )
            if any(node_id not in node_id_set for node_id in constraint.node_ids):
                return PayoffStates(
                    states=(),
                    failure_codes=("constraint_node_unknown",),
                    graph_hash=self.graph_hash,
                )
            kind = constraint.normalized_kind
            if kind not in binary_kinds | group_kinds:
                return PayoffStates(
                    states=(),
                    failure_codes=("constraint_kind_unsupported",),
                    graph_hash=self.graph_hash,
                )
            if (
                kind in binary_kinds
                and len(constraint.node_ids) != 2
                or kind in group_kinds
                and len(constraint.node_ids) < 2
            ):
                return PayoffStates(
                    states=(),
                    failure_codes=("constraint_arity_invalid",),
                    graph_hash=self.graph_hash,
                )
        candidate_states: Sequence[Mapping[str, int]]
        if len(node_ids) > 16:
            full_set_kinds = {
                constraint.normalized_kind
                for constraint in self.constraints
                if set(constraint.node_ids) == node_id_set
            }
            if full_set_kinds & {"exactly_one", "exactlyone"}:
                candidate_states = tuple(
                    {
                        current: int(current == true_node)
                        for current in node_ids
                    }
                    for true_node in node_ids
                )
            elif "mutex" in full_set_kinds:
                candidate_states = (
                    {node_id: 0 for node_id in node_ids},
                    *(
                        {
                            current: int(current == true_node)
                            for current in node_ids
                        }
                        for true_node in node_ids
                    ),
                )
            else:
                return PayoffStates(
                    states=(),
                    failure_codes=("state_space_too_large",),
                    graph_hash=self.graph_hash,
                )
        else:
            candidate_states = tuple(
                dict(zip(node_ids, values, strict=True))
                for values in product((0, 1), repeat=len(node_ids))
            )
        states: list[dict[str, int]] = []
        for candidate in candidate_states:
            state = dict(candidate)
            if all(constraint.accepts(state) for constraint in self.constraints):
                states.append(state)
        if not states:
            return PayoffStates(
                states=(),
                failure_codes=("semantic_contradiction",),
                graph_hash=self.graph_hash,
            )
        return PayoffStates(
            states=tuple(states),
            failure_codes=(),
            graph_hash=self.graph_hash,
        )
