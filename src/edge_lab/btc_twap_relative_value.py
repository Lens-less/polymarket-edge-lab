"""Fail-closed BTC 5m/15m Chainlink-TWAP relative-value research.

The module is paper-only.  Its public seams turn current public market
metadata into immutable contracts, price both horizons from the same scenario
set, and (in later sections) evaluate conservative execution evidence.  It
does not import authentication or order-submission code.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from .data_store import canonical_json_bytes
from .execution import (
    DepthBook,
    ExecutionFeeSchedule,
    ExecutionResult,
    ExecutionStatus,
    Ledger,
    OrderSide,
    TimeInForce,
    TraceRef,
    execute_taker,
)
from .settlement_regime import (
    LEGACY_SETTLEMENT_REGIME_ID,
    V06_SETTLEMENT_REGIME_ID,
    SettlementRegimeSpec,
    canonicalize_resolution_source,
    legacy_settlement_regime,
)

_SLUG = re.compile(r"^btc-updown-(5m|15m)-([0-9]{10})$")
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_DURATION_SECONDS = {"5m": 300, "15m": 900}
_ZERO = Decimal("0")
_ONE = Decimal("1")
_SIZE_QUANTUM = Decimal("0.000001")
_MODEL_VERSION = "btc_5m_15m_twap_relative_value.v1"
_SETTLEMENT_REGIME = LEGACY_SETTLEMENT_REGIME_ID
_SUPPORTED_SETTLEMENT_REGIMES = {
    LEGACY_SETTLEMENT_REGIME_ID,
    V06_SETTLEMENT_REGIME_ID,
}


class RelativeValueRejection(ValueError):
    """A public input cannot enter the frozen strategy universe."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


def _reject(code: str, detail: str) -> None:
    raise RelativeValueRejection(code, detail)


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _reject("metadata_shape_invalid", f"{label} must be an object")
    return value


def _string(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _reject("metadata_shape_invalid", f"{label} must be a non-empty string")
    return value.strip()


def _json_string_list(value: Any, *, label: str) -> tuple[str, ...]:
    decoded = value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RelativeValueRejection(
                "metadata_shape_invalid", f"{label} is not valid JSON"
            ) from exc
    if (
        not isinstance(decoded, Sequence)
        or isinstance(decoded, (str, bytes, bytearray))
        or any(not isinstance(item, str) or not item for item in decoded)
    ):
        _reject("metadata_shape_invalid", f"{label} must contain strings")
    return tuple(decoded)


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool):
        _reject("metadata_shape_invalid", f"{label} must be decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RelativeValueRejection(
            "metadata_shape_invalid", f"{label} must be decimal"
        ) from exc
    if not result.is_finite():
        _reject("metadata_shape_invalid", f"{label} must be finite")
    return result


def _epoch_ms(value: Any, *, label: str) -> int:
    text = _string(value, label=label)
    try:
        parsed = datetime.fromisoformat(
            f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as exc:
        raise RelativeValueRejection(
            "metadata_shape_invalid", f"{label} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _reject("metadata_shape_invalid", f"{label} must include a timezone")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000)


@dataclass(frozen=True)
class TwapMarketContract:
    """One current-regime BTC Up/Down contract with executable parameters."""

    horizon: str
    slug: str
    market_id: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    opens_at_ms: int
    closes_at_ms: int
    twap_window_seconds: int
    source_topic: str
    resolution_source: str
    tick_size: Decimal
    minimum_order_size: Decimal
    fee_schedule: ExecutionFeeSchedule
    taker_delay_ms: int
    accepting_orders: bool
    rule_hash: str
    settlement_regime: str = _SETTLEMENT_REGIME

    @classmethod
    def from_public_metadata(
        cls,
        gamma_market: Mapping[str, Any],
        clob_market: Mapping[str, Any],
        *,
        settlement_regime: SettlementRegimeSpec | None = None,
    ) -> "TwapMarketContract":
        """Bind Gamma rules to the matching compact CLOB market payload."""

        gamma = _mapping(gamma_market, label="gamma_market")
        clob = _mapping(clob_market, label="clob_market")
        slug = _string(gamma.get("slug"), label="gamma_market.slug")
        match = _SLUG.fullmatch(slug)
        if match is None:
            _reject("unsupported_market", "slug is not a BTC 5m/15m market")
        horizon, opens_at_text = match.groups()
        opens_at_ms = int(opens_at_text) * 1_000
        closes_at_ms = opens_at_ms + _DURATION_SECONDS[horizon] * 1_000
        if (
            _epoch_ms(gamma.get("endDate"), label="gamma_market.endDate")
            != closes_at_ms
        ):
            _reject("window_mismatch", "Gamma endDate does not match slug horizon")

        regime = settlement_regime or legacy_settlement_regime()
        source_spec = regime.source_for(horizon)
        window = source_spec.window_seconds
        expected_source = source_spec.resolution_source
        source = _string(
            gamma.get("resolutionSource"),
            label="gamma_market.resolutionSource",
        )
        description = _string(
            gamma.get("description"), label="gamma_market.description"
        )
        normalized_description = " ".join(description.casefold().split())
        try:
            source_matches = (
                canonicalize_resolution_source(source)
                == canonicalize_resolution_source(expected_source)
            )
        except ValueError:
            source_matches = False
        if (
            not source_matches
            or source_spec.stream_slug not in normalized_description
        ):
            _reject(
                "resolution_source_mismatch",
                f"{horizon} must bind the exact {window}s Chainlink TWAP source",
            )
        if "greater than or equal" not in normalized_description:
            _reject(
                "resolution_rule_mismatch",
                "current Up rule must explicitly use greater-than-or-equal",
            )

        condition_id = _string(
            gamma.get("conditionId"), label="gamma_market.conditionId"
        ).lower()
        if _CONDITION_ID.fullmatch(condition_id) is None:
            _reject("condition_mapping_mismatch", "condition ID is malformed")
        if str(clob.get("c", "")).lower() != condition_id:
            _reject(
                "condition_mapping_mismatch",
                "Gamma and CLOB condition IDs differ",
            )

        outcomes = _json_string_list(
            gamma.get("outcomes"), label="gamma_market.outcomes"
        )
        token_ids = _json_string_list(
            gamma.get("clobTokenIds"), label="gamma_market.clobTokenIds"
        )
        if outcomes != ("Up", "Down") or len(token_ids) != 2:
            _reject("token_mapping_mismatch", "outcomes must be exactly Up, Down")
        clob_tokens = clob.get("t")
        if not isinstance(clob_tokens, Sequence) or isinstance(
            clob_tokens, (str, bytes, bytearray)
        ):
            _reject("token_mapping_mismatch", "CLOB tokens must be an array")
        compact_tokens: list[tuple[str, str]] = []
        for item in clob_tokens:
            token = _mapping(item, label="clob_market.t[]")
            compact_tokens.append(
                (
                    _string(token.get("t"), label="clob token id"),
                    _string(token.get("o"), label="clob outcome"),
                )
            )
        if tuple(compact_tokens) != (
            (token_ids[0], "Up"),
            (token_ids[1], "Down"),
        ):
            _reject("token_mapping_mismatch", "Gamma and CLOB token order differs")

        accepting_orders = clob.get("ao")
        if not isinstance(accepting_orders, bool):
            _reject("metadata_shape_invalid", "CLOB ao must be boolean")
        delay_enabled = clob.get("itode", False)
        if not isinstance(delay_enabled, bool):
            _reject("metadata_shape_invalid", "CLOB itode must be boolean")
        tick_size = _decimal(clob.get("mts"), label="clob_market.mts")
        minimum_order_size = _decimal(clob.get("mos"), label="clob_market.mos")
        if tick_size <= 0 or minimum_order_size <= 0:
            _reject("metadata_shape_invalid", "tick and minimum size must be positive")
        try:
            fee_schedule = ExecutionFeeSchedule.from_market(clob)
        except ValueError as exc:
            raise RelativeValueRejection("fee_schedule_invalid", str(exc)) from exc

        rule_identity = {
            "schema_version": "btc-twap-market-contract.v1",
            "horizon": horizon,
            "slug": slug,
            "market_id": _string(gamma.get("id"), label="gamma_market.id"),
            "condition_id": condition_id,
            "token_ids": token_ids,
            "opens_at_ms": opens_at_ms,
            "closes_at_ms": closes_at_ms,
            "twap_window_seconds": window,
            "source_topic": source_spec.source_topic,
            "resolution_source": source,
            "settlement_regime": regime.regime_id,
            "description_sha256": hashlib.sha256(
                description.encode("utf-8")
            ).hexdigest(),
            "tick_size": str(tick_size),
            "minimum_order_size": str(minimum_order_size),
            "fee_rate": str(fee_schedule.rate),
            "fee_exponent": str(fee_schedule.exponent),
            "fee_taker_only": fee_schedule.taker_only,
            "taker_delay_ms": 250 if delay_enabled else 0,
        }
        return cls(
            horizon=horizon,
            slug=slug,
            market_id=rule_identity["market_id"],
            condition_id=condition_id,
            up_token_id=token_ids[0],
            down_token_id=token_ids[1],
            opens_at_ms=opens_at_ms,
            closes_at_ms=closes_at_ms,
            twap_window_seconds=window,
            source_topic=source_spec.source_topic,
            resolution_source=source,
            tick_size=tick_size,
            minimum_order_size=minimum_order_size,
            fee_schedule=fee_schedule,
            taker_delay_ms=250 if delay_enabled else 0,
            accepting_orders=accepting_orders,
            rule_hash=hashlib.sha256(
                canonical_json_bytes(rule_identity)
            ).hexdigest(),
            settlement_regime=regime.regime_id,
        )


@dataclass(frozen=True)
class SameExpiryPair:
    """The final 5m window nested inside one 15m contract."""

    market_5: TwapMarketContract
    market_15: TwapMarketContract
    expires_at_ms: int

    @classmethod
    def from_contracts(
        cls,
        first: TwapMarketContract,
        second: TwapMarketContract,
    ) -> "SameExpiryPair":
        contracts = {first.horizon: first, second.horizon: second}
        if set(contracts) != {"5m", "15m"}:
            _reject("pair_horizon_mismatch", "pair needs one 5m and one 15m market")
        market_5 = contracts["5m"]
        market_15 = contracts["15m"]
        if market_5.settlement_regime != market_15.settlement_regime:
            _reject(
                "settlement_regime_mismatch",
                "paired markets must share one frozen settlement regime",
            )
        if (
            market_5.closes_at_ms != market_15.closes_at_ms
            or market_5.opens_at_ms != market_15.closes_at_ms - 300_000
        ):
            _reject(
                "same_expiry_mismatch",
                "5m market must be the final window of the 15m market",
            )
        return cls(
            market_5=market_5,
            market_15=market_15,
            expires_at_ms=market_5.closes_at_ms,
        )


@dataclass(frozen=True)
class PairSettlementState:
    """Exact opening TWAP evidence bound to one immutable market-rule pair."""

    market_5_rule_hash: str
    market_15_rule_hash: str
    market_5_open_timestamp_ms: int
    market_15_open_timestamp_ms: int
    strike_5: Decimal
    strike_15: Decimal
    opening_5_source_event_id: str
    opening_15_source_event_id: str

    def __post_init__(self) -> None:
        for name in ("market_5_rule_hash", "market_15_rule_hash"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{64}", value) is None
            ):
                raise ValueError(f"{name} must be a SHA-256 hex digest")
        for name in ("market_5_open_timestamp_ms", "market_15_open_timestamp_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("strike_5", "strike_15"):
            value = _decimal(getattr(self, name), label=name)
            if value <= _ZERO:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        for name in ("opening_5_source_event_id", "opening_15_source_event_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class SettlementScenario:
    """One joint terminal state produced by a shared underlying price path."""

    twap_30: Decimal
    twap_60: Decimal

    def __post_init__(self) -> None:
        for name in ("twap_30", "twap_60"):
            value = _decimal(getattr(self, name), label=name)
            if value <= 0:
                _reject("scenario_invalid", f"{name} must be positive")
            object.__setattr__(self, name, value)


@dataclass(frozen=True)
class JointDistribution:
    """Settlement outcomes for both horizons from exactly one scenario set."""

    strike_5: Decimal
    strike_15: Decimal
    scenarios: tuple[SettlementScenario, ...]
    outcomes: tuple[tuple[bool, bool], ...]
    q_5_up: Decimal
    q_15_up: Decimal
    outcome_counts: Mapping[str, int]

    @classmethod
    def from_scenarios(
        cls,
        scenarios: Sequence[SettlementScenario],
        *,
        strike_5: Decimal,
        strike_15: Decimal,
    ) -> "JointDistribution":
        frozen = tuple(scenarios)
        if not frozen or any(
            not isinstance(item, SettlementScenario) for item in frozen
        ):
            _reject("scenario_invalid", "scenarios must be non-empty settlement states")
        strike5 = _decimal(strike_5, label="strike_5")
        strike15 = _decimal(strike_15, label="strike_15")
        if strike5 <= 0 or strike15 <= 0:
            _reject("scenario_invalid", "strikes must be positive")
        outcomes = tuple(
            (item.twap_30 >= strike5, item.twap_60 >= strike15)
            for item in frozen
        )
        count = Decimal(len(outcomes))
        counts = {
            "5up_15up": sum(first and second for first, second in outcomes),
            "5up_15down": sum(first and not second for first, second in outcomes),
            "5down_15up": sum(not first and second for first, second in outcomes),
            "5down_15down": sum(not first and not second for first, second in outcomes),
        }
        return cls(
            strike_5=strike5,
            strike_15=strike15,
            scenarios=frozen,
            outcomes=outcomes,
            q_5_up=Decimal(sum(first for first, _ in outcomes)) / count,
            q_15_up=Decimal(sum(second for _, second in outcomes)) / count,
            outcome_counts=MappingProxyType(counts),
        )

    def payoffs(self, action: str) -> tuple[Decimal, ...]:
        if action == "long_15_up_long_5_down":
            return tuple(
                Decimal(int(up_15)) + Decimal(int(not up_5))
                for up_5, up_15 in self.outcomes
            )
        if action == "long_5_up_long_15_down":
            return tuple(
                Decimal(int(up_5)) + Decimal(int(not up_15))
                for up_5, up_15 in self.outcomes
            )
        raise ValueError(f"unsupported relative-value action: {action}")

    def calibrated_outcome_probabilities(
        self,
        *,
        q_5_up: Decimal,
        q_15_up: Decimal,
    ) -> Mapping[str, Decimal]:
        """Project calibrated marginals onto one coherent joint distribution.

        The one free joint probability is chosen as the feasible value nearest
        (in squared distance across all four cells) to the raw Monte Carlo
        joint.  This retains as much sampled dependence as possible while
        making both calibrated marginals exact.
        """

        q5 = _decimal(q_5_up, label="q_5_up")
        q15 = _decimal(q_15_up, label="q_15_up")
        if not _ZERO <= q5 <= _ONE or not _ZERO <= q15 <= _ONE:
            raise ValueError("calibrated marginals must be in [0, 1]")
        count = Decimal(len(self.outcomes))
        raw_11 = Decimal(self.outcome_counts["5up_15up"]) / count
        raw_10 = Decimal(self.outcome_counts["5up_15down"]) / count
        raw_01 = Decimal(self.outcome_counts["5down_15up"]) / count
        raw_00 = Decimal(self.outcome_counts["5down_15down"]) / count
        unconstrained_11 = (
            Decimal(2) * q5
            + Decimal(2) * q15
            - _ONE
            + raw_11
            - raw_10
            - raw_01
            + raw_00
        ) / Decimal(4)
        lower_11 = max(_ZERO, q5 + q15 - _ONE)
        upper_11 = min(q5, q15)
        p_11 = min(upper_11, max(lower_11, unconstrained_11))
        probabilities = {
            "5up_15up": p_11,
            "5up_15down": q5 - p_11,
            "5down_15up": q15 - p_11,
            "5down_15down": _ONE - q5 - q15 + p_11,
        }
        if any(value < _ZERO for value in probabilities.values()):
            raise RuntimeError("joint calibration produced negative probability")
        if sum(probabilities.values(), _ZERO) != _ONE:
            raise RuntimeError("joint calibration did not preserve total probability")
        return MappingProxyType(probabilities)


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal

    def __post_init__(self) -> None:
        price = _decimal(self.price, label="book price")
        size = _decimal(self.size, label="book size")
        if not _ZERO <= price <= _ONE or size <= _ZERO:
            _reject("book_invalid", "book levels need probability prices and size")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "size", size)


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Immutable executable L2 at one captured exchange timestamp."""

    token_id: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    timestamp_ms: int
    tick_size: Decimal
    minimum_order_size: Decimal

    @classmethod
    def from_tuples(
        cls,
        token_id: str,
        *,
        bids: Sequence[tuple[Decimal, Decimal]],
        asks: Sequence[tuple[Decimal, Decimal]],
        timestamp_ms: int,
        tick_size: Decimal,
        minimum_order_size: Decimal,
    ) -> "OrderBookSnapshot":
        if not isinstance(token_id, str) or not token_id:
            _reject("book_invalid", "token_id must be non-empty")
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
        ):
            _reject("book_invalid", "timestamp_ms must be non-negative")
        tick = _decimal(tick_size, label="tick_size")
        minimum = _decimal(minimum_order_size, label="minimum_order_size")
        if tick <= _ZERO or minimum <= _ZERO:
            _reject("book_invalid", "tick and minimum size must be positive")
        return cls(
            token_id=token_id,
            bids=tuple(
                sorted(
                    (BookLevel(price, size) for price, size in bids),
                    key=lambda item: item.price,
                    reverse=True,
                )
            ),
            asks=tuple(
                sorted(
                    (BookLevel(price, size) for price, size in asks),
                    key=lambda item: item.price,
                )
            ),
            timestamp_ms=timestamp_ms,
            tick_size=tick,
            minimum_order_size=minimum,
        )

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class DataHealth:
    decision_at_ms: int
    twap_30_observed_at_ms: int
    twap_60_observed_at_ms: int
    twap_30_received_at_ms: int
    twap_60_received_at_ms: int
    absolute_clock_drift_ms: int
    calibration_5: IsotonicProbabilityCalibrator | None
    calibration_15: IsotonicProbabilityCalibrator | None

    def __post_init__(self) -> None:
        for name in (
            "decision_at_ms",
            "twap_30_observed_at_ms",
            "twap_60_observed_at_ms",
            "twap_30_received_at_ms",
            "twap_60_received_at_ms",
            "absolute_clock_drift_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("calibration_5", "calibration_15"):
            calibration = getattr(self, name)
            if calibration is not None and not isinstance(
                calibration, IsotonicProbabilityCalibrator
            ):
                raise TypeError(f"{name} must be an isotonic artifact or None")


@dataclass(frozen=True)
class StrategyConfig:
    tau_min_seconds: int = 45
    tau_max_seconds: int = 240
    maximum_spread_each_leg: Decimal = Decimal("0.03")
    maximum_chainlink_staleness_ms: int = 1_500
    maximum_book_staleness_ms: int = 750
    maximum_clock_drift_ms: int = 100
    minimum_market_price: Decimal = Decimal("0.05")
    maximum_market_price: Decimal = Decimal("0.95")
    pair_risk_usdc: Decimal = Decimal("25")
    minimum_net_expected_pnl_per_pair: Decimal = Decimal("0.015")
    uncertainty_multiplier: Decimal = Decimal("1.25")
    cvar_tail_probability: Decimal = Decimal("0.05")
    settlement_regime: str = _SETTLEMENT_REGIME

    def __post_init__(self) -> None:
        if self.tau_min_seconds < 0 or self.tau_max_seconds < self.tau_min_seconds:
            raise ValueError("tau bounds are invalid")
        for name in (
            "maximum_spread_each_leg",
            "minimum_market_price",
            "maximum_market_price",
            "pair_risk_usdc",
            "minimum_net_expected_pnl_per_pair",
            "uncertainty_multiplier",
            "cvar_tail_probability",
        ):
            value = _decimal(getattr(self, name), label=name)
            if value < _ZERO:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, value)
        if not _ZERO < self.cvar_tail_probability <= _ONE:
            raise ValueError("cvar_tail_probability must be in (0, 1]")
        if not _ZERO <= self.minimum_market_price < self.maximum_market_price <= _ONE:
            raise ValueError("market price bounds are invalid")
        if self.settlement_regime not in _SUPPORTED_SETTLEMENT_REGIMES:
            raise ValueError("unsupported strategy settlement regime")


class PairAction(str, Enum):
    NO_TRADE = "no_trade"
    LONG_15_UP_LONG_5_DOWN = "long_15_up_long_5_down"
    LONG_5_UP_LONG_15_DOWN = "long_5_up_long_15_down"


@dataclass(frozen=True)
class PairDecision:
    action: PairAction
    decision_at_ms: int
    quantity: Decimal
    first_token_id: str | None
    second_token_id: str | None
    execution_notional_per_pair: Decimal | None
    fee_per_pair: Decimal | None
    execution_cost_per_pair: Decimal | None
    expected_gross_pnl_per_pair: Decimal | None
    expected_net_pnl_per_pair: Decimal | None
    uncertainty_adjusted_pnl_per_pair: Decimal | None
    cvar_per_pair: Decimal | None
    loss_probability: Decimal | None
    q_5_raw: Decimal
    q_15_raw: Decimal
    q_5_calibrated: Decimal | None
    q_15_calibrated: Decimal | None
    reason_codes: tuple[str, ...]
    qualification: str = "qualified"

    def __post_init__(self) -> None:
        if self.qualification not in {"qualified", "development_shadow"}:
            raise ValueError("unsupported pair-decision qualification")


class PairExecutionStatus(str, Enum):
    NO_FILL = "no_fill"
    COMPLETE = "complete"
    UNWOUND_TO_MATCHED = "unwound_to_matched"
    FAILED_UNHEDGED = "failed_unhedged"


@dataclass(frozen=True)
class PairPaperExecution:
    """Auditable outcome of two non-atomic taker-like paper legs."""

    status: PairExecutionStatus
    first_leg: ExecutionResult
    second_leg: ExecutionResult
    unwind_leg: ExecutionResult | None
    matched_quantity: Decimal
    unhedged_quantity: Decimal
    fees_paid: Decimal
    legging_cost: Decimal
    cashflow_after_execution: Decimal


@dataclass(frozen=True)
class PairPaperSettlement:
    """Hold-to-settlement ledger result for one explainable paper execution."""

    market_5_up: bool
    market_15_up: bool
    first_token_wins: bool
    second_token_wins: bool
    payout: Decimal
    net_pnl: Decimal
    explainable: bool
    qualified_sample: bool


def settle_pair_paper_execution(
    *,
    decision: PairDecision,
    pair: SameExpiryPair,
    execution: PairPaperExecution,
    market_5_up: bool,
    market_15_up: bool,
) -> PairPaperSettlement:
    """Settle recorded paper inventory without substituting mark prices."""

    if not isinstance(market_5_up, bool) or not isinstance(market_15_up, bool):
        raise TypeError("settlement outcomes must be bool")
    if decision.action is PairAction.NO_TRADE:
        raise ValueError("a no-trade decision has no execution to settle")
    if not decision.first_token_id or not decision.second_token_id:
        raise ValueError("actionable decision token IDs are required")
    if not isinstance(execution, PairPaperExecution):
        raise TypeError("execution must be PairPaperExecution")
    winning_tokens = {
        pair.market_5.up_token_id if market_5_up else pair.market_5.down_token_id,
        pair.market_15.up_token_id if market_15_up else pair.market_15.down_token_id,
    }
    first_wins = decision.first_token_id in winning_tokens
    second_wins = decision.second_token_id in winning_tokens
    first_position = execution.matched_quantity + execution.unhedged_quantity
    second_position = execution.matched_quantity
    payout = (
        first_position if first_wins else _ZERO
    ) + (second_position if second_wins else _ZERO)
    net_pnl = execution.cashflow_after_execution + payout
    # A fully unwound failed pair can finish with zero inventory while still
    # realizing spread, fee, and legging losses.  It remains an economic trade
    # and must not disappear from the paper PnL sample merely because payout is
    # zero at settlement.
    explainable = (
        execution.first_leg.filled_quantity > _ZERO
        or execution.second_leg.filled_quantity > _ZERO
        or (
            execution.unwind_leg is not None
            and execution.unwind_leg.filled_quantity > _ZERO
        )
    )
    qualified_sample = (
        explainable
        and decision.qualification == "qualified"
        and execution.status
        in {PairExecutionStatus.COMPLETE, PairExecutionStatus.UNWOUND_TO_MATCHED}
        and execution.unhedged_quantity == _ZERO
    )
    return PairPaperSettlement(
        market_5_up=market_5_up,
        market_15_up=market_15_up,
        first_token_wins=first_wins,
        second_token_wins=second_wins,
        payout=payout,
        net_pnl=net_pnl,
        explainable=explainable,
        qualified_sample=qualified_sample,
    )


def _mutable_book(snapshot: OrderBookSnapshot) -> DepthBook:
    return DepthBook.from_tuples(
        snapshot.token_id,
        bids=((level.price, level.size) for level in snapshot.bids),
        asks=((level.price, level.size) for level in snapshot.asks),
        tick_size=snapshot.tick_size,
        min_order_size=snapshot.minimum_order_size,
        timestamp_ms=snapshot.timestamp_ms,
    )


def _killed_execution(
    *,
    quantity: Decimal,
    source_event_id: str,
    decision_id: str,
    reason: str,
) -> ExecutionResult:
    return ExecutionResult(
        status=ExecutionStatus.KILLED,
        requested_quantity=quantity,
        fills=(),
        source_event_id=source_event_id,
        decision_id=decision_id,
        reason=reason,
    )


def _contract_for_token(
    pair: SameExpiryPair, token_id: str
) -> TwapMarketContract:
    for contract in (pair.market_5, pair.market_15):
        if token_id in {contract.up_token_id, contract.down_token_id}:
            return contract
    _reject("execution_token_mismatch", f"token {token_id} is not in the pair")


def execute_pair_paper(
    *,
    decision: PairDecision,
    pair: SameExpiryPair,
    first_book: OrderBookSnapshot,
    second_book: OrderBookSnapshot,
    unwind_book: OrderBookSnapshot,
    first_source_event_id: str,
    second_source_event_id: str,
    unwind_source_event_id: str,
    initial_cash: Decimal,
    max_leg_delay_ms: int,
    max_book_age_ms: int,
) -> PairPaperExecution:
    """Replay delayed FAK legs and sell excess first-leg inventory on failure.

    Each supplied snapshot is treated as the first captured executable book at
    its timestamp.  It must occur after the market's taker delay and within the
    frozen age/legging windows.  A failed unwind is retained as unhedged
    inventory and can never enter a qualified PnL sample.
    """

    if decision.action is PairAction.NO_TRADE or decision.quantity <= _ZERO:
        raise ValueError("only an actionable positive-quantity decision can execute")
    if not decision.first_token_id or not decision.second_token_id:
        raise ValueError("actionable decisions require both token IDs")
    expected_tokens = {
        PairAction.LONG_15_UP_LONG_5_DOWN: (
            pair.market_15.up_token_id,
            pair.market_5.down_token_id,
        ),
        PairAction.LONG_5_UP_LONG_15_DOWN: (
            pair.market_5.up_token_id,
            pair.market_15.down_token_id,
        ),
    }
    if (decision.first_token_id, decision.second_token_id) != expected_tokens.get(
        decision.action
    ):
        _reject(
            "execution_action_mismatch",
            "decision action does not match its ordered token pair",
        )
    if max_leg_delay_ms < 0 or max_book_age_ms < 0:
        raise ValueError("execution timing bounds cannot be negative")
    if first_book.token_id != decision.first_token_id:
        _reject("execution_token_mismatch", "first book does not match decision")
    if second_book.token_id != decision.second_token_id:
        _reject("execution_token_mismatch", "second book does not match decision")
    if unwind_book.token_id != decision.first_token_id:
        _reject("execution_token_mismatch", "unwind book must sell the first leg")

    first_contract = _contract_for_token(pair, decision.first_token_id)
    second_contract = _contract_for_token(pair, decision.second_token_id)
    first_earliest_ms = decision.decision_at_ms + first_contract.taker_delay_ms
    if not (
        first_earliest_ms
        <= first_book.timestamp_ms
        <= first_earliest_ms + max_book_age_ms
    ):
        _reject("execution_timing_invalid", "first executable book misses delay window")
    # This is a sequential hedge: the second quantity is the observed first
    # fill, so its own market delay cannot start before that fill is observable.
    second_earliest_ms = first_book.timestamp_ms + second_contract.taker_delay_ms
    second_is_timely = (
        second_earliest_ms
        <= second_book.timestamp_ms
        <= first_book.timestamp_ms + max_leg_delay_ms
    )

    ledger = Ledger(initial_cash)
    for contract in (pair.market_5, pair.market_15):
        ledger.register_binary_market(
            contract.condition_id,
            yes_token_id=contract.up_token_id,
            no_token_id=contract.down_token_id,
        )
    decision_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "action": decision.action.value,
                "decision_at_ms": decision.decision_at_ms,
                "first_token_id": decision.first_token_id,
                "second_token_id": decision.second_token_id,
                "quantity": str(decision.quantity),
            }
        )
    ).hexdigest()

    first = execute_taker(
        ledger,
        _mutable_book(first_book),
        side=OrderSide.BUY,
        quantity=decision.quantity,
        limit_price=_ONE,
        time_in_force=TimeInForce.FAK,
        fee_schedule=first_contract.fee_schedule,
        trace=TraceRef(
            source_event_id=first_source_event_id,
            decision_id=decision_id,
            timestamp_ms=decision.decision_at_ms,
        ),
        execution_latency_ms=first_book.timestamp_ms - decision.decision_at_ms,
        max_book_age_ms=max_book_age_ms,
    )
    if first.filled_quantity == _ZERO:
        second = _killed_execution(
            quantity=_ZERO,
            source_event_id=second_source_event_id,
            decision_id=decision_id,
            reason="first_leg_unfilled",
        )
        return PairPaperExecution(
            status=PairExecutionStatus.NO_FILL,
            first_leg=first,
            second_leg=second,
            unwind_leg=None,
            matched_quantity=_ZERO,
            unhedged_quantity=_ZERO,
            fees_paid=ledger.fees_paid,
            legging_cost=_ZERO,
            cashflow_after_execution=ledger.cash - ledger.initial_cash,
        )

    if second_is_timely:
        second = execute_taker(
            ledger,
            _mutable_book(second_book),
            side=OrderSide.BUY,
            quantity=first.filled_quantity,
            limit_price=_ONE,
            time_in_force=TimeInForce.FAK,
            fee_schedule=second_contract.fee_schedule,
            trace=TraceRef(
                source_event_id=second_source_event_id,
                decision_id=decision_id,
                timestamp_ms=decision.decision_at_ms,
            ),
            execution_latency_ms=(
                second_book.timestamp_ms - decision.decision_at_ms
            ),
            max_book_age_ms=max_book_age_ms,
        )
    else:
        second = _killed_execution(
            quantity=first.filled_quantity,
            source_event_id=second_source_event_id,
            decision_id=decision_id,
            reason="maximum_leg_delay_exceeded",
        )

    excess = first.filled_quantity - second.filled_quantity
    unwind: ExecutionResult | None = None
    legging_cost = _ZERO
    if excess > _ZERO:
        second_failure_observable_ms = (
            second_book.timestamp_ms
            if second_is_timely
            else first_book.timestamp_ms + max_leg_delay_ms
        )
        # The unwind is another marketable order and is subject to the first
        # contract's delay after the hedge failure becomes observable.
        unwind_earliest_ms = (
            second_failure_observable_ms + first_contract.taker_delay_ms
        )
        if not (
            unwind_earliest_ms
            <= unwind_book.timestamp_ms
            <= unwind_earliest_ms + max_book_age_ms
        ):
            unwind = _killed_execution(
                quantity=excess,
                source_event_id=unwind_source_event_id,
                decision_id=decision_id,
                reason="unwind_book_outside_frozen_window",
            )
        else:
            unwind = execute_taker(
                ledger,
                _mutable_book(unwind_book),
                side=OrderSide.SELL,
                quantity=excess,
                limit_price=_ZERO,
                time_in_force=TimeInForce.FAK,
                fee_schedule=first_contract.fee_schedule,
                trace=TraceRef(
                    source_event_id=unwind_source_event_id,
                    decision_id=decision_id,
                    timestamp_ms=decision.decision_at_ms,
                ),
                execution_latency_ms=(
                    unwind_book.timestamp_ms - decision.decision_at_ms
                ),
                max_book_age_ms=max_book_age_ms,
            )
        if unwind.filled_quantity > _ZERO:
            first_cost_per_share = (first.notional + first.fee) / first.filled_quantity
            unwind_proceeds = unwind.notional - unwind.fee
            legging_cost = (
                first_cost_per_share * unwind.filled_quantity - unwind_proceeds
            )

    unwound = unwind.filled_quantity if unwind is not None else _ZERO
    unhedged = excess - unwound
    status = (
        PairExecutionStatus.FAILED_UNHEDGED
        if unhedged > _ZERO
        else PairExecutionStatus.UNWOUND_TO_MATCHED
        if excess > _ZERO
        else PairExecutionStatus.COMPLETE
    )
    return PairPaperExecution(
        status=status,
        first_leg=first,
        second_leg=second,
        unwind_leg=unwind,
        matched_quantity=min(
            ledger.position(decision.first_token_id),
            ledger.position(decision.second_token_id),
        ),
        unhedged_quantity=unhedged,
        fees_paid=ledger.fees_paid,
        legging_cost=legging_cost,
        cashflow_after_execution=ledger.cash - ledger.initial_cash,
    )


@dataclass(frozen=True)
class _BuyQuote:
    notional: Decimal
    fee: Decimal


@dataclass(frozen=True)
class _Candidate:
    action: PairAction
    quantity: Decimal
    first_token_id: str
    second_token_id: str
    notional_per_pair: Decimal
    fee_per_pair: Decimal
    cost_per_pair: Decimal
    gross_pnl_per_pair: Decimal
    net_pnl_per_pair: Decimal
    adjusted_pnl_per_pair: Decimal
    cvar_per_pair: Decimal
    loss_probability: Decimal


def _quote_buy(
    book: OrderBookSnapshot,
    *,
    quantity: Decimal,
    fees: ExecutionFeeSchedule,
) -> _BuyQuote | None:
    remaining = quantity
    notional = _ZERO
    fee = _ZERO
    for level in book.asks:
        if remaining <= _ZERO:
            break
        filled = min(remaining, level.size)
        notional += filled * level.price
        fee += fees.fee(filled, level.price, maker=False)
        remaining -= filled
    if remaining > _ZERO:
        return None
    return _BuyQuote(notional=notional, fee=fee)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values))


def _candidate(
    *,
    action: PairAction,
    first_token_id: str,
    second_token_id: str,
    first_contract: TwapMarketContract,
    second_contract: TwapMarketContract,
    books: Mapping[str, OrderBookSnapshot],
    distribution: JointDistribution,
    q_5_calibrated: Decimal,
    q_15_calibrated: Decimal,
    config: StrategyConfig,
) -> _Candidate | None:
    first_book = books.get(first_token_id)
    second_book = books.get(second_token_id)
    if first_book is None or second_book is None:
        return None
    if first_book.token_id != first_token_id or second_book.token_id != second_token_id:
        return None
    for book in (first_book, second_book):
        if (
            book.best_bid is None
            or book.best_ask is None
            or book.spread is None
            or book.spread < _ZERO
            or book.spread > config.maximum_spread_each_leg
            or not (
                config.minimum_market_price
                <= book.best_ask
                <= config.maximum_market_price
            )
        ):
            return None
    first_unit_fee = first_contract.fee_schedule.raw_fee(
        _ONE, first_book.best_ask, maker=False
    )
    second_unit_fee = second_contract.fee_schedule.raw_fee(
        _ONE, second_book.best_ask, maker=False
    )
    indicative_unit_cost = (
        first_book.best_ask
        + second_book.best_ask
        + first_unit_fee
        + second_unit_fee
    )
    if indicative_unit_cost <= _ZERO:
        return None
    upper_quantity = (config.pair_risk_usdc / indicative_unit_cost).quantize(
        _SIZE_QUANTUM, rounding=ROUND_DOWN
    )
    minimum_quantity = max(
        first_book.minimum_order_size,
        second_book.minimum_order_size,
        first_contract.minimum_order_size,
        second_contract.minimum_order_size,
    )
    if upper_quantity < minimum_quantity:
        return None

    minimum_units = int(
        (minimum_quantity / _SIZE_QUANTUM).to_integral_value(
            rounding="ROUND_CEILING"
        )
    )
    maximum_units = int(upper_quantity / _SIZE_QUANTUM)
    affordable: tuple[Decimal, _BuyQuote, _BuyQuote] | None = None
    while minimum_units <= maximum_units:
        middle_units = (minimum_units + maximum_units) // 2
        trial_quantity = Decimal(middle_units) * _SIZE_QUANTUM
        trial_first = _quote_buy(
            first_book,
            quantity=trial_quantity,
            fees=first_contract.fee_schedule,
        )
        trial_second = _quote_buy(
            second_book,
            quantity=trial_quantity,
            fees=second_contract.fee_schedule,
        )
        trial_cost = (
            trial_first.notional
            + trial_first.fee
            + trial_second.notional
            + trial_second.fee
            if trial_first is not None and trial_second is not None
            else None
        )
        if trial_cost is not None and trial_cost <= config.pair_risk_usdc:
            affordable = (trial_quantity, trial_first, trial_second)
            minimum_units = middle_units + 1
        else:
            maximum_units = middle_units - 1
    if affordable is None:
        return None
    quantity, first_quote, second_quote = affordable
    notional_per_pair = (first_quote.notional + second_quote.notional) / quantity
    fee_per_pair = (first_quote.fee + second_quote.fee) / quantity
    cost_per_pair = notional_per_pair + fee_per_pair
    probabilities = distribution.calibrated_outcome_probabilities(
        q_5_up=q_5_calibrated,
        q_15_up=q_15_calibrated,
    )
    payoff_by_outcome = (
        {
            "5up_15up": _ONE,
            "5up_15down": _ZERO,
            "5down_15up": Decimal(2),
            "5down_15down": _ONE,
        }
        if action is PairAction.LONG_15_UP_LONG_5_DOWN
        else {
            "5up_15up": _ONE,
            "5up_15down": Decimal(2),
            "5down_15up": _ZERO,
            "5down_15down": _ONE,
        }
    )
    mean_payoff = sum(
        probabilities[outcome] * payoff
        for outcome, payoff in payoff_by_outcome.items()
    )
    gross = mean_payoff - notional_per_pair
    net = mean_payoff - cost_per_pair
    variance = sum(
        probabilities[outcome] * (payoff - mean_payoff) ** 2
        for outcome, payoff in payoff_by_outcome.items()
    )
    standard_error = (variance / Decimal(len(distribution.outcomes))).sqrt()
    adjusted = net - config.uncertainty_multiplier * standard_error
    weighted_pnl = sorted(
        (
            payoff - cost_per_pair,
            probabilities[outcome],
        )
        for outcome, payoff in payoff_by_outcome.items()
    )
    tail_remaining = config.cvar_tail_probability
    tail_total = _ZERO
    for pnl, probability in weighted_pnl:
        included = min(probability, tail_remaining)
        tail_total += pnl * included
        tail_remaining -= included
        if tail_remaining == _ZERO:
            break
    cvar = tail_total / config.cvar_tail_probability
    loss_probability = sum(
        probability for pnl, probability in weighted_pnl if pnl < _ZERO
    )
    return _Candidate(
        action=action,
        quantity=quantity,
        first_token_id=first_token_id,
        second_token_id=second_token_id,
        notional_per_pair=notional_per_pair,
        fee_per_pair=fee_per_pair,
        cost_per_pair=cost_per_pair,
        gross_pnl_per_pair=gross,
        net_pnl_per_pair=net,
        adjusted_pnl_per_pair=adjusted,
        cvar_per_pair=cvar,
        loss_probability=loss_probability,
    )


def _base_decision_reasons(
    *,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    distribution: JointDistribution,
    books: Mapping[str, OrderBookSnapshot],
    health: DataHealth,
    config: StrategyConfig,
) -> list[str]:
    reasons: list[str] = []
    if (
        settlement_state.market_5_rule_hash != pair.market_5.rule_hash
        or settlement_state.market_15_rule_hash != pair.market_15.rule_hash
    ):
        reasons.append("settlement_state_rule_mismatch")
    if (
        settlement_state.market_5_open_timestamp_ms != pair.market_5.opens_at_ms
        or settlement_state.market_15_open_timestamp_ms != pair.market_15.opens_at_ms
    ):
        reasons.append("settlement_state_window_mismatch")
    if (
        settlement_state.strike_5 != distribution.strike_5
        or settlement_state.strike_15 != distribution.strike_15
    ):
        reasons.append("settlement_state_strike_mismatch")
    tau_ms = pair.expires_at_ms - health.decision_at_ms
    if not config.tau_min_seconds * 1_000 <= tau_ms <= config.tau_max_seconds * 1_000:
        reasons.append("tau_outside_frozen_window")
    for label, observed_at, received_at in (
        (
            "twap_30",
            health.twap_30_observed_at_ms,
            health.twap_30_received_at_ms,
        ),
        (
            "twap_60",
            health.twap_60_observed_at_ms,
            health.twap_60_received_at_ms,
        ),
    ):
        observation_age = health.decision_at_ms - observed_at
        receipt_age = health.decision_at_ms - received_at
        if (
            observation_age < 0
            or observation_age > config.maximum_chainlink_staleness_ms
            or receipt_age < 0
            or receipt_age > config.maximum_chainlink_staleness_ms
            or received_at < observed_at - config.maximum_clock_drift_ms
        ):
            reasons.append(f"{label}_stale_or_future")
    if health.absolute_clock_drift_ms > config.maximum_clock_drift_ms:
        reasons.append("clock_drift_exceeded")
    if not pair.market_5.accepting_orders or not pair.market_15.accepting_orders:
        reasons.append("market_not_accepting_orders")
    required_tokens = {
        pair.market_5.up_token_id,
        pair.market_5.down_token_id,
        pair.market_15.up_token_id,
        pair.market_15.down_token_id,
    }
    if set(books) != required_tokens:
        reasons.append("complete_four_outcome_books_missing")
    else:
        for token_id in sorted(required_tokens):
            book = books[token_id]
            age = health.decision_at_ms - book.timestamp_ms
            if age < 0 or age > config.maximum_book_staleness_ms:
                reasons.append(f"book_stale_or_future:{token_id}")
    return reasons


def _no_trade_decision(
    *,
    distribution: JointDistribution,
    decision_at_ms: int,
    reasons: Sequence[str],
    q_5_calibrated: Decimal | None = None,
    q_15_calibrated: Decimal | None = None,
    qualification: str = "qualified",
) -> PairDecision:
    return PairDecision(
        action=PairAction.NO_TRADE,
        decision_at_ms=decision_at_ms,
        quantity=_ZERO,
        first_token_id=None,
        second_token_id=None,
        execution_notional_per_pair=None,
        fee_per_pair=None,
        execution_cost_per_pair=None,
        expected_gross_pnl_per_pair=None,
        expected_net_pnl_per_pair=None,
        uncertainty_adjusted_pnl_per_pair=None,
        cvar_per_pair=None,
        loss_probability=None,
        q_5_raw=distribution.q_5_up,
        q_15_raw=distribution.q_15_up,
        q_5_calibrated=q_5_calibrated,
        q_15_calibrated=q_15_calibrated,
        reason_codes=tuple(reasons),
        qualification=qualification,
    )


def _decision_from_probabilities(
    *,
    pair: SameExpiryPair,
    distribution: JointDistribution,
    books: Mapping[str, OrderBookSnapshot],
    health: DataHealth,
    config: StrategyConfig,
    q_5_calibrated: Decimal,
    q_15_calibrated: Decimal,
    qualification: str,
    audit_reason_codes: tuple[str, ...],
) -> PairDecision:
    candidates = tuple(
        item
        for item in (
            _candidate(
                action=PairAction.LONG_15_UP_LONG_5_DOWN,
                first_token_id=pair.market_15.up_token_id,
                second_token_id=pair.market_5.down_token_id,
                first_contract=pair.market_15,
                second_contract=pair.market_5,
                books=books,
                distribution=distribution,
                q_5_calibrated=q_5_calibrated,
                q_15_calibrated=q_15_calibrated,
                config=config,
            ),
            _candidate(
                action=PairAction.LONG_5_UP_LONG_15_DOWN,
                first_token_id=pair.market_5.up_token_id,
                second_token_id=pair.market_15.down_token_id,
                first_contract=pair.market_5,
                second_contract=pair.market_15,
                books=books,
                distribution=distribution,
                q_5_calibrated=q_5_calibrated,
                q_15_calibrated=q_15_calibrated,
                config=config,
            ),
        )
        if item is not None
    )
    if not candidates:
        return _no_trade_decision(
            distribution=distribution,
            decision_at_ms=health.decision_at_ms,
            reasons=(
                "no_candidate_has_healthy_executable_depth",
                *audit_reason_codes,
            ),
            q_5_calibrated=q_5_calibrated,
            q_15_calibrated=q_15_calibrated,
            qualification=qualification,
        )
    best = max(candidates, key=lambda item: item.adjusted_pnl_per_pair)
    action = best.action
    reason_codes = audit_reason_codes
    if best.adjusted_pnl_per_pair <= config.minimum_net_expected_pnl_per_pair:
        action = PairAction.NO_TRADE
        reason_codes = ("net_edge_below_frozen_threshold", *audit_reason_codes)
    return PairDecision(
        action=action,
        decision_at_ms=health.decision_at_ms,
        quantity=best.quantity if action is not PairAction.NO_TRADE else _ZERO,
        first_token_id=(
            best.first_token_id if action is not PairAction.NO_TRADE else None
        ),
        second_token_id=(
            best.second_token_id if action is not PairAction.NO_TRADE else None
        ),
        execution_notional_per_pair=best.notional_per_pair,
        fee_per_pair=best.fee_per_pair,
        execution_cost_per_pair=best.cost_per_pair,
        expected_gross_pnl_per_pair=best.gross_pnl_per_pair,
        expected_net_pnl_per_pair=best.net_pnl_per_pair,
        uncertainty_adjusted_pnl_per_pair=best.adjusted_pnl_per_pair,
        cvar_per_pair=best.cvar_per_pair,
        loss_probability=best.loss_probability,
        q_5_raw=distribution.q_5_up,
        q_15_raw=distribution.q_15_up,
        q_5_calibrated=q_5_calibrated,
        q_15_calibrated=q_15_calibrated,
        reason_codes=reason_codes,
        qualification=qualification,
    )


def decide_pair_trade(
    *,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    distribution: JointDistribution,
    books: Mapping[str, OrderBookSnapshot],
    health: DataHealth,
    config: StrategyConfig,
) -> PairDecision:
    """Choose at most one calibrated fee/depth-aware pair trade."""

    reasons = _base_decision_reasons(
        pair=pair,
        settlement_state=settlement_state,
        distribution=distribution,
        books=books,
        health=health,
        config=config,
    )
    calibrations = (health.calibration_5, health.calibration_15)
    if any(calibration is None for calibration in calibrations):
        reasons.append("past_only_calibration_missing")
    elif any(
        calibration.fit_at_ms >= health.decision_at_ms
        or calibration.maximum_label_available_at_ms >= calibration.fit_at_ms
        for calibration in calibrations
        if calibration is not None
    ):
        reasons.append("past_only_calibration_invalid")
    elif (
        health.calibration_5.horizon != "5m"
        or health.calibration_15.horizon != "15m"
        or health.calibration_5.settlement_regime != config.settlement_regime
        or health.calibration_15.settlement_regime != config.settlement_regime
        or health.calibration_5.model_version != _MODEL_VERSION
        or health.calibration_15.model_version != _MODEL_VERSION
    ):
        reasons.append("calibration_scope_mismatch")
    if reasons:
        return _no_trade_decision(
            distribution=distribution,
            decision_at_ms=health.decision_at_ms,
            reasons=reasons,
        )

    assert health.calibration_5 is not None
    assert health.calibration_15 is not None
    q_5_calibrated = health.calibration_5.transform(distribution.q_5_up)
    q_15_calibrated = health.calibration_15.transform(distribution.q_15_up)
    return _decision_from_probabilities(
        pair=pair,
        distribution=distribution,
        books=books,
        health=health,
        config=config,
        q_5_calibrated=q_5_calibrated,
        q_15_calibrated=q_15_calibrated,
        qualification="qualified",
        audit_reason_codes=(),
    )


def decide_pair_trade_shadow(
    *,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    distribution: JointDistribution,
    books: Mapping[str, OrderBookSnapshot],
    health: DataHealth,
    config: StrategyConfig,
) -> PairDecision:
    """Evaluate raw-model paper execution without claiming calibration/OOS."""

    reasons = _base_decision_reasons(
        pair=pair,
        settlement_state=settlement_state,
        distribution=distribution,
        books=books,
        health=health,
        config=config,
    )
    if reasons:
        return _no_trade_decision(
            distribution=distribution,
            decision_at_ms=health.decision_at_ms,
            reasons=(*reasons, "uncalibrated_shadow_only"),
            qualification="development_shadow",
        )
    return _decision_from_probabilities(
        pair=pair,
        distribution=distribution,
        books=books,
        health=health,
        config=config,
        q_5_calibrated=distribution.q_5_up,
        q_15_calibrated=distribution.q_15_up,
        qualification="development_shadow",
        audit_reason_codes=("uncalibrated_shadow_only",),
    )


@dataclass(frozen=True)
class TimedPrice:
    """One predictor price with its source observation timestamp."""

    timestamp_ms: int
    price: Decimal

    def __post_init__(self) -> None:
        if (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("timestamp_ms must be a non-negative integer")
        price = _decimal(self.price, label="predictor price")
        if price <= _ZERO:
            raise ValueError("predictor price must be positive")
        object.__setattr__(self, "price", price)


@dataclass(frozen=True)
class CalibrationPoint:
    """One resolved training prediction available before calibrator fitting."""

    event_id: str
    prediction: Decimal
    outcome: bool
    split: str
    label_available_at_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be non-empty")
        prediction = _decimal(self.prediction, label="calibration prediction")
        if not _ZERO <= prediction <= _ONE:
            raise ValueError("calibration prediction must be in [0, 1]")
        object.__setattr__(self, "prediction", prediction)
        if not isinstance(self.outcome, bool):
            raise TypeError("outcome must be bool")
        if not isinstance(self.split, str) or not self.split:
            raise ValueError("split must be non-empty")
        if (
            isinstance(self.label_available_at_ms, bool)
            or not isinstance(self.label_available_at_ms, int)
            or self.label_available_at_ms < 0
        ):
            raise ValueError("label_available_at_ms must be non-negative")


@dataclass
class _IsotonicBlock:
    lower: Decimal
    upper: Decimal
    successes: int
    weight: int

    @property
    def value(self) -> Decimal:
        return Decimal(self.successes) / Decimal(self.weight)


@dataclass(frozen=True)
class IsotonicProbabilityCalibrator:
    """Deterministic PAVA calibration artifact fitted on train labels only."""

    knots: tuple[tuple[Decimal, Decimal], ...]
    training_event_ids: tuple[str, ...]
    fit_at_ms: int
    maximum_label_available_at_ms: int
    horizon: str
    settlement_regime: str
    model_version: str
    artifact_hash: str

    @classmethod
    def fit(
        cls,
        points: Sequence[CalibrationPoint],
        *,
        fit_at_ms: int,
        horizon: str,
        settlement_regime: str = _SETTLEMENT_REGIME,
        model_version: str = _MODEL_VERSION,
    ) -> "IsotonicProbabilityCalibrator":
        frozen = tuple(points)
        if len(frozen) < 2 or any(
            not isinstance(point, CalibrationPoint) for point in frozen
        ):
            _reject("calibration_data_insufficient", "at least two points are required")
        if (
            isinstance(fit_at_ms, bool)
            or not isinstance(fit_at_ms, int)
            or fit_at_ms < 0
        ):
            _reject("calibration_time_invalid", "fit_at_ms must be non-negative")
        if any(point.split != "train" for point in frozen):
            _reject("calibration_split_invalid", "only train rows may fit calibration")
        if horizon not in {"5m", "15m"}:
            _reject("calibration_scope_invalid", "horizon must be 5m or 15m")
        if (
            settlement_regime not in _SUPPORTED_SETTLEMENT_REGIMES
            or model_version != _MODEL_VERSION
        ):
            _reject(
                "calibration_scope_invalid",
                "calibrator regime and model version must match the frozen strategy",
            )
        if any(point.label_available_at_ms >= fit_at_ms for point in frozen):
            _reject(
                "calibration_leakage",
                "every training label must be available before fit time",
            )
        event_ids = tuple(point.event_id for point in frozen)
        if len(set(event_ids)) != len(event_ids):
            _reject("calibration_duplicate_event", "training event IDs must be unique")

        grouped: list[_IsotonicBlock] = []
        for point in sorted(frozen, key=lambda item: (item.prediction, item.event_id)):
            if grouped and grouped[-1].upper == point.prediction:
                grouped[-1].successes += int(point.outcome)
                grouped[-1].weight += 1
            else:
                grouped.append(
                    _IsotonicBlock(
                        lower=point.prediction,
                        upper=point.prediction,
                        successes=int(point.outcome),
                        weight=1,
                    )
                )
        blocks: list[_IsotonicBlock] = []
        for block in grouped:
            blocks.append(block)
            while len(blocks) >= 2 and blocks[-2].value > blocks[-1].value:
                right = blocks.pop()
                left = blocks.pop()
                blocks.append(
                    _IsotonicBlock(
                        lower=left.lower,
                        upper=right.upper,
                        successes=left.successes + right.successes,
                        weight=left.weight + right.weight,
                    )
                )
        knots: list[tuple[Decimal, Decimal]] = []
        for block in blocks:
            knots.append((block.lower, block.value))
            if block.upper != block.lower:
                knots.append((block.upper, block.value))
        payload = {
            "schema_version": "btc-twap-isotonic-calibration.v1",
            "fit_at_ms": fit_at_ms,
            "horizon": horizon,
            "settlement_regime": settlement_regime,
            "model_version": model_version,
            "training_points": [
                {
                    "event_id": point.event_id,
                    "prediction": str(point.prediction),
                    "outcome": point.outcome,
                    "split": point.split,
                    "label_available_at_ms": point.label_available_at_ms,
                }
                for point in frozen
            ],
            "knots": [[str(x), str(y)] for x, y in knots],
        }
        return cls(
            knots=tuple(knots),
            training_event_ids=event_ids,
            fit_at_ms=fit_at_ms,
            maximum_label_available_at_ms=max(
                point.label_available_at_ms for point in frozen
            ),
            horizon=horizon,
            settlement_regime=settlement_regime,
            model_version=model_version,
            artifact_hash=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        )

    def transform(self, probability: Decimal) -> Decimal:
        value = _decimal(probability, label="probability")
        if not _ZERO <= value <= _ONE:
            raise ValueError("probability must be in [0, 1]")
        if value <= self.knots[0][0]:
            return self.knots[0][1]
        if value >= self.knots[-1][0]:
            return self.knots[-1][1]
        for (left_x, left_y), (right_x, right_y) in zip(
            self.knots, self.knots[1:]
        ):
            if left_x <= value <= right_x:
                if right_x == left_x:
                    return max(left_y, right_y)
                fraction = (value - left_x) / (right_x - left_x)
                return left_y + fraction * (right_y - left_y)
        raise RuntimeError("calibration knot search failed")


def simulate_ewma_joint_distribution(
    *,
    predictor_prices: Sequence[TimedPrice],
    current_twap_30: Decimal,
    current_twap_60: Decimal,
    decision_at_ms: int,
    expiry_ms: int,
    strike_5: Decimal,
    strike_15: Decimal,
    n_paths: int = 20_000,
    seed: int = 712,
    ewma_lambda: Decimal = Decimal("0.94"),
    oracle_residual_pairs: Sequence[tuple[Decimal, Decimal]] = (),
) -> JointDistribution:
    """Build a deterministic joint terminal distribution from past ticks only.

    Standardized one-step residuals are resampled with a fixed seed.  Both
    terminal TWAP proxies are evaluated on every generated underlying path;
    the current Chainlink-minus-local-proxy basis is carried forward.  Optional
    30s/60s oracle residual pairs must come from a separately frozen training
    split and are sampled jointly.
    """

    if (
        isinstance(decision_at_ms, bool)
        or not isinstance(decision_at_ms, int)
        or isinstance(expiry_ms, bool)
        or not isinstance(expiry_ms, int)
        or expiry_ms <= decision_at_ms
    ):
        _reject("model_window_invalid", "expiry must follow decision time")
    if (
        isinstance(n_paths, bool)
        or not isinstance(n_paths, int)
        or n_paths <= 0
        or n_paths > 1_000_000
    ):
        _reject("model_config_invalid", "n_paths must be in [1, 1000000]")
    if isinstance(seed, bool) or not isinstance(seed, int):
        _reject("model_config_invalid", "seed must be an integer")
    lam = _decimal(ewma_lambda, label="ewma_lambda")
    if not _ZERO < lam < _ONE:
        _reject("model_config_invalid", "ewma_lambda must be in (0, 1)")
    frozen_prices = tuple(predictor_prices)
    if len(frozen_prices) < 60 or any(
        not isinstance(item, TimedPrice) for item in frozen_prices
    ):
        _reject("predictor_history_insufficient", "at least 60 prices are required")
    timestamps = tuple(item.timestamp_ms for item in frozen_prices)
    if any(
        current >= following
        for current, following in zip(timestamps, timestamps[1:])
    ):
        _reject("predictor_time_invalid", "predictor timestamps must increase")
    if timestamps[-1] > decision_at_ms:
        _reject("predictor_leakage", "predictor tick occurs after decision time")
    if any(
        following - current != 1_000
        for current, following in zip(timestamps, timestamps[1:])
    ):
        _reject(
            "predictor_sampling_invalid",
            "predictor history must be exactly one-second sampled",
        )
    if decision_at_ms - timestamps[-1] > 5_000:
        _reject("predictor_stale", "last predictor tick is more than five seconds old")

    current30 = _decimal(current_twap_30, label="current_twap_30")
    current60 = _decimal(current_twap_60, label="current_twap_60")
    if current30 <= _ZERO or current60 <= _ZERO:
        _reject("model_state_invalid", "current TWAP values must be positive")
    history = [float(item.price) for item in frozen_prices]
    log_returns = [
        math.log(current / previous)
        for previous, current in zip(history, history[1:])
    ]
    initial_variance = sum(value * value for value in log_returns[:30]) / 30
    variance_path: list[float] = []
    variance = initial_variance
    lambda_float = float(lam)
    for value in log_returns:
        variance = lambda_float * variance + (1 - lambda_float) * value * value
        variance_path.append(variance)
    if not math.isfinite(variance) or variance <= 1e-20:
        _reject("predictor_variance_missing", "past returns contain no usable variance")
    residuals = [
        value / math.sqrt(max(current_variance, 1e-20))
        for value, current_variance in zip(log_returns, variance_path)
    ]
    residual_mean = sum(residuals) / len(residuals)
    residuals = [max(-5.0, min(5.0, value - residual_mean)) for value in residuals]
    if max(residuals) - min(residuals) <= 1e-12:
        _reject(
            "predictor_residuals_missing",
            "standardized residuals have no variation",
        )

    fast_rv = sum(value * value for value in log_returns[-15:])
    slow_rv = sum(value * value for value in log_returns[-60:])
    ratio = fast_rv / max(slow_rv * (15 / 60), 1e-20)
    regime_multiplier = 1.5 if ratio > 2 else 0.75 if ratio < 0.5 else 1.0
    variance *= regime_multiplier

    proxy_30_now = sum(history[-30:]) / min(30, len(history))
    proxy_60_now = sum(history[-60:]) / min(60, len(history))
    basis_30 = float(current30) - proxy_30_now
    basis_60 = float(current60) - proxy_60_now
    horizon_seconds = max(1, math.ceil((expiry_ms - decision_at_ms) / 1_000))
    residual_pairs: tuple[tuple[float, float], ...]
    if oracle_residual_pairs:
        parsed_pairs: list[tuple[float, float]] = []
        for residual_30, residual_60 in oracle_residual_pairs:
            first = _decimal(residual_30, label="oracle residual 30s")
            second = _decimal(residual_60, label="oracle residual 60s")
            parsed_pairs.append((float(first), float(second)))
        residual_pairs = tuple(parsed_pairs)
    else:
        residual_pairs = ((0.0, 0.0),)

    rng = random.Random(seed)
    scenarios: list[SettlementScenario] = []
    for _ in range(n_paths):
        path_variance = variance
        last_price = history[-1]
        future: list[float] = []
        for _step in range(horizon_seconds):
            innovation = residuals[rng.randrange(len(residuals))]
            modeled_return = math.sqrt(max(path_variance, 1e-20)) * innovation
            last_price *= math.exp(modeled_return)
            future.append(last_price)
            path_variance = (
                lambda_float * path_variance
                + (1 - lambda_float) * modeled_return * modeled_return
            )
        combined = [*history[-60:], *future]
        proxy_30_terminal = sum(combined[-30:]) / min(30, len(combined))
        proxy_60_terminal = sum(combined[-60:]) / min(60, len(combined))
        oracle_30, oracle_60 = residual_pairs[rng.randrange(len(residual_pairs))]
        terminal_30 = proxy_30_terminal + basis_30 + oracle_30
        terminal_60 = proxy_60_terminal + basis_60 + oracle_60
        if terminal_30 <= 0 or terminal_60 <= 0:
            _reject("scenario_invalid", "simulated terminal TWAP is non-positive")
        scenarios.append(
            SettlementScenario(
                twap_30=Decimal(str(terminal_30)),
                twap_60=Decimal(str(terminal_60)),
            )
        )
    return JointDistribution.from_scenarios(
        scenarios,
        strike_5=strike_5,
        strike_15=strike_15,
    )


class ValidationStatus(str, Enum):
    VALIDATED_PROFITABLE = "validated_profitable"
    PROMISING_NOT_VALIDATED = "promising_not_validated"
    INSUFFICIENT_DATA = "insufficient_data"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ValidationEvidence:
    """Frozen, current-regime evidence inputs to the promotion gates."""

    resolved_current_regime_markets: int
    expected_current_regime_markets: int
    markets_with_complete_capture: int
    unknown_resolution_mapping_count: int
    explainable_simulated_trades: int
    explainable_fills: int
    explainable_net_pnls: tuple[Decimal, ...]
    chronological_oos_complete: bool
    complete_taker_cost_model: bool
    delay_depth_and_legging_replay_complete: bool
    bootstrap_net_pnl_lower_95: Decimal | None
    oos_brier_5: Decimal | None
    oos_brier_15: Decimal | None
    market_brier_5: Decimal | None
    market_brier_15: Decimal | None
    oos_expected_calibration_error_5: Decimal | None
    oos_expected_calibration_error_15: Decimal | None
    maximum_single_event_pnl_share: Decimal | None
    direction_exposure_below_single_leg: bool | None
    signal_strength_net_ev_monotonic: bool | None

    def __post_init__(self) -> None:
        for name in (
            "resolved_current_regime_markets",
            "expected_current_regime_markets",
            "markets_with_complete_capture",
            "unknown_resolution_mapping_count",
            "explainable_simulated_trades",
            "explainable_fills",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.resolved_current_regime_markets > self.expected_current_regime_markets:
            raise ValueError("resolved markets cannot exceed expected markets")
        if self.markets_with_complete_capture > self.expected_current_regime_markets:
            raise ValueError("complete captures cannot exceed expected markets")
        if len(self.explainable_net_pnls) != self.explainable_simulated_trades:
            raise ValueError("every simulated trade needs one explainable net PnL")
        for value in self.explainable_net_pnls:
            _decimal(value, label="explainable net PnL")
        for name in (
            "chronological_oos_complete",
            "complete_taker_cost_model",
            "delay_depth_and_legging_replay_complete",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        for name in (
            "direction_exposure_below_single_leg",
            "signal_strength_net_ev_monotonic",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise TypeError(f"{name} must be bool or None")
        for name in (
            "bootstrap_net_pnl_lower_95",
            "oos_brier_5",
            "oos_brier_15",
            "market_brier_5",
            "market_brier_15",
            "oos_expected_calibration_error_5",
            "oos_expected_calibration_error_15",
            "maximum_single_event_pnl_share",
        ):
            value = getattr(self, name)
            if value is not None:
                parsed = _decimal(value, label=name)
                object.__setattr__(self, name, parsed)
        for name in (
            "oos_brier_5",
            "oos_brier_15",
            "market_brier_5",
            "market_brier_15",
            "oos_expected_calibration_error_5",
            "oos_expected_calibration_error_15",
            "maximum_single_event_pnl_share",
        ):
            value = getattr(self, name)
            if value is not None and not _ZERO <= value <= _ONE:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True)
class ValidationReport:
    status: ValidationStatus
    reason_codes: tuple[str, ...]
    market_coverage: Decimal | None
    observed_explainable_net_pnl: Decimal | None
    qualified_net_pnl: Decimal | None
    minimum_resolved_markets: int = 2_000
    minimum_simulated_trades: int = 500
    minimum_explainable_fills: int = 500


def evaluate_validation(evidence: ValidationEvidence) -> ValidationReport:
    """Apply the preregistered gates without turning missing PnL into zero."""

    if not isinstance(evidence, ValidationEvidence):
        raise TypeError("evidence must be ValidationEvidence")
    coverage = (
        Decimal(evidence.markets_with_complete_capture)
        / Decimal(evidence.expected_current_regime_markets)
        if evidence.expected_current_regime_markets
        else None
    )
    observed_net = (
        sum(evidence.explainable_net_pnls, _ZERO)
        if evidence.explainable_net_pnls
        else None
    )
    sufficiency_reasons: list[str] = []
    if evidence.resolved_current_regime_markets < 2_000:
        sufficiency_reasons.append("resolved_markets_below_2000")
    if evidence.explainable_simulated_trades < 500:
        sufficiency_reasons.append("simulated_trades_below_500")
    if evidence.explainable_fills < 500:
        sufficiency_reasons.append("explainable_fills_below_500")
    if coverage is None or coverage < Decimal("0.995"):
        sufficiency_reasons.append("market_coverage_below_0_995")
    if evidence.unknown_resolution_mapping_count:
        sufficiency_reasons.append("unknown_resolution_mapping_present")
    if not evidence.chronological_oos_complete:
        sufficiency_reasons.append("chronological_oos_incomplete")
    if not evidence.complete_taker_cost_model:
        sufficiency_reasons.append("complete_taker_cost_model_missing")
    if not evidence.delay_depth_and_legging_replay_complete:
        sufficiency_reasons.append("delay_depth_legging_replay_incomplete")
    required_metrics = (
        evidence.bootstrap_net_pnl_lower_95,
        evidence.oos_brier_5,
        evidence.oos_brier_15,
        evidence.market_brier_5,
        evidence.market_brier_15,
        evidence.oos_expected_calibration_error_5,
        evidence.oos_expected_calibration_error_15,
        evidence.maximum_single_event_pnl_share,
        evidence.direction_exposure_below_single_leg,
        evidence.signal_strength_net_ev_monotonic,
    )
    if any(value is None for value in required_metrics):
        sufficiency_reasons.append("required_oos_metrics_missing")
    if sufficiency_reasons:
        return ValidationReport(
            status=ValidationStatus.INSUFFICIENT_DATA,
            reason_codes=tuple(sufficiency_reasons),
            market_coverage=coverage,
            observed_explainable_net_pnl=observed_net,
            qualified_net_pnl=None,
        )

    assert observed_net is not None
    rejection_reasons: list[str] = []
    if observed_net <= _ZERO:
        rejection_reasons.append("oos_net_pnl_not_positive")
    if evidence.bootstrap_net_pnl_lower_95 <= _ZERO:
        rejection_reasons.append("bootstrap_lower_bound_not_positive")
    if evidence.oos_brier_5 >= evidence.market_brier_5:
        rejection_reasons.append("oos_brier_5_did_not_beat_market")
    if evidence.oos_brier_15 >= evidence.market_brier_15:
        rejection_reasons.append("oos_brier_15_did_not_beat_market")
    if max(
        evidence.oos_expected_calibration_error_5,
        evidence.oos_expected_calibration_error_15,
    ) > Decimal("0.05"):
        rejection_reasons.append("oos_calibration_error_above_0_05")
    if evidence.maximum_single_event_pnl_share > Decimal("0.20"):
        rejection_reasons.append("single_event_pnl_share_above_0_20")
    if not evidence.direction_exposure_below_single_leg:
        rejection_reasons.append("direction_exposure_not_reduced")
    if not evidence.signal_strength_net_ev_monotonic:
        rejection_reasons.append("signal_strength_net_ev_not_monotonic")
    return ValidationReport(
        status=(
            ValidationStatus.REJECTED
            if rejection_reasons
            else ValidationStatus.VALIDATED_PROFITABLE
        ),
        reason_codes=tuple(rejection_reasons),
        market_coverage=coverage,
        observed_explainable_net_pnl=observed_net,
        qualified_net_pnl=observed_net if not rejection_reasons else None,
    )


__all__ = [
    "CalibrationPoint",
    "DataHealth",
    "IsotonicProbabilityCalibrator",
    "JointDistribution",
    "OrderBookSnapshot",
    "PairAction",
    "PairDecision",
    "PairExecutionStatus",
    "PairPaperExecution",
    "RelativeValueRejection",
    "SameExpiryPair",
    "SettlementScenario",
    "StrategyConfig",
    "TimedPrice",
    "TwapMarketContract",
    "ValidationEvidence",
    "ValidationReport",
    "ValidationStatus",
    "decide_pair_trade",
    "evaluate_validation",
    "execute_pair_paper",
    "simulate_ewma_joint_distribution",
]
