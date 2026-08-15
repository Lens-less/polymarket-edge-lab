"""Opt-in v0.7 BTC 5m/15m shared-terminal TWAP research model.

This module is intentionally separate from the frozen v0.5/v0.6 implementation.
It models the current 60s/60s same-expiry settlement regime with one simulated
BTC path and one terminal Chainlink 60-second TWAP.  It remains paper-only and
contains no network, authentication, signing, or order-submission code.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from .btc_twap_relative_value import (
    DataHealth,
    OrderBookSnapshot,
    PairAction,
    PairDecision,
    PairSettlementState,
    SameExpiryPair,
    StrategyConfig,
    TimedPrice,
    TwapMarketContract,
)
from .data_store import canonical_json_bytes
from .execution import ExecutionFeeSchedule
from .settlement_regime import V06_SETTLEMENT_REGIME_ID

ZERO = Decimal("0")
ONE = Decimal("1")
TWO = Decimal("2")
SIZE_QUANTUM = Decimal("0.000001")
ANNUALIZATION_SECONDS = Decimal("31557600")
SETTLEMENT_MODEL_ID = "shared_terminal_twap_60.v1"
MODEL_VERSION = "btc_5m_15m_shared_terminal_twap_60.v07"
SHRINKAGE_SCHEMA_VERSION = "btc-5m-15m-v07-train-only-shrinkage.v2"
VALIDATION_VETO_SCHEMA_VERSION = "btc-5m-15m-v07-validation-veto.v2"
CANONICAL_PAIR_ID_PREFIX = "v07-expiry-pair-sha256:"
CANONICAL_EVENT_CLUSTER_PREFIX = CANONICAL_PAIR_ID_PREFIX
CANONICAL_EXPIRY_CLUSTER_PREFIX = "v07-common-expiry-sha256:"

_OUTCOME_KEYS = (
    "5up_15up",
    "5up_15down",
    "5down_15up",
    "5down_15down",
)


class V07ModelRejection(ValueError):
    """A v0.7 input cannot enter the shared-terminal evidence path."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


def _reject(code: str, detail: str) -> None:
    raise V07ModelRejection(code, detail)


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{label} must be an exact decimal value")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def _probability(value: Any, *, label: str) -> Decimal:
    parsed = _decimal(value, label=label)
    if not ZERO <= parsed <= ONE:
        raise ValueError(f"{label} must be in [0, 1]")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _hash_document(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def canonical_event_cluster_id(pair: SameExpiryPair) -> str:
    """Derive the immutable pair-integrity key, not the statistical cluster key."""

    if not isinstance(pair, SameExpiryPair):
        raise TypeError("pair must be SameExpiryPair")
    identity = {
        "schema_version": "btc-5m-15m-v07-canonical-expiry-pair.v1",
        "expiry_ms": pair.expires_at_ms,
        "market_5": {
            "market_id": pair.market_5.market_id,
            "condition_id": pair.market_5.condition_id,
        },
        "market_15": {
            "market_id": pair.market_15.market_id,
            "condition_id": pair.market_15.condition_id,
        },
    }
    return f"{CANONICAL_PAIR_ID_PREFIX}{_hash_document(identity)}"


def canonical_expiry_cluster_id(expiry_ms: int) -> str:
    """Derive the statistical cluster key from the captured common expiry only."""

    if isinstance(expiry_ms, bool) or not isinstance(expiry_ms, int) or expiry_ms <= 0:
        raise ValueError("expiry_ms must be a positive integer")
    identity = {
        "schema_version": "btc-5m-15m-v07-canonical-common-expiry.v1",
        "expiry_ms": expiry_ms,
    }
    return f"{CANONICAL_EXPIRY_CLUSTER_PREFIX}{_hash_document(identity)}"


class StrikeOrdering(str, Enum):
    FIVE_BELOW_FIFTEEN = "strike_5_below_strike_15"
    FIVE_ABOVE_FIFTEEN = "strike_5_above_strike_15"
    EQUAL = "strikes_equal"


@dataclass(frozen=True)
class SharedTerminalTwap60Scenario:
    """One terminal state from one underlying path and one shared 60s TWAP."""

    terminal_twap_60: Decimal

    def __post_init__(self) -> None:
        terminal = _decimal(self.terminal_twap_60, label="terminal_twap_60")
        if terminal <= ZERO:
            _reject("scenario_invalid", "terminal_twap_60 must be positive")
        object.__setattr__(self, "terminal_twap_60", terminal)


@dataclass(frozen=True)
class SharedTerminalTwap60Distribution:
    """Structurally constrained outcomes implied by one terminal 60s TWAP."""

    strike_5: Decimal
    strike_15: Decimal
    scenarios: tuple[SharedTerminalTwap60Scenario, ...]
    outcomes: tuple[tuple[bool, bool], ...]
    outcome_counts: Mapping[str, int]
    outcome_probabilities: Mapping[str, Decimal]
    q_5_up: Decimal
    q_15_up: Decimal
    jeffreys_alpha: Decimal
    strike_ordering: StrikeOrdering
    uncertainty_scale_outcome_probabilities: Mapping[str, Mapping[str, Decimal]] = (
        field(default_factory=lambda: MappingProxyType({}))
    )
    uncertainty_scale_path_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @classmethod
    def from_scenarios(
        cls,
        scenarios: Sequence[SharedTerminalTwap60Scenario],
        *,
        strike_5: Decimal,
        strike_15: Decimal,
        jeffreys_alpha: Decimal = Decimal("0.5"),
        uncertainty_scale_distributions: Mapping[str, SharedTerminalTwap60Distribution]
        | None = None,
    ) -> SharedTerminalTwap60Distribution:
        frozen = tuple(scenarios)
        if not frozen or any(
            not isinstance(item, SharedTerminalTwap60Scenario) for item in frozen
        ):
            _reject(
                "scenario_invalid",
                "scenarios must be non-empty shared-terminal states",
            )
        strike5 = _decimal(strike_5, label="strike_5")
        strike15 = _decimal(strike_15, label="strike_15")
        alpha = _decimal(jeffreys_alpha, label="jeffreys_alpha")
        if strike5 <= ZERO or strike15 <= ZERO:
            _reject("scenario_invalid", "strikes must be positive")
        if alpha <= ZERO:
            _reject("model_config_invalid", "jeffreys_alpha must be positive")

        ordering = (
            StrikeOrdering.FIVE_BELOW_FIFTEEN
            if strike5 < strike15
            else StrikeOrdering.FIVE_ABOVE_FIFTEEN
            if strike5 > strike15
            else StrikeOrdering.EQUAL
        )
        outcomes = tuple(
            (
                item.terminal_twap_60 >= strike5,
                item.terminal_twap_60 >= strike15,
            )
            for item in frozen
        )
        counts = {
            "5up_15up": sum(first and second for first, second in outcomes),
            "5up_15down": sum(first and not second for first, second in outcomes),
            "5down_15up": sum(not first and second for first, second in outcomes),
            "5down_15down": sum(not first and not second for first, second in outcomes),
        }
        possible = cls._possible_outcomes(ordering)
        impossible = set(_OUTCOME_KEYS) - set(possible)
        if any(counts[key] for key in impossible):
            raise RuntimeError("shared-terminal simulation emitted an impossible state")
        denominator = Decimal(len(frozen)) + alpha * Decimal(len(possible))
        probabilities = {key: ZERO for key in _OUTCOME_KEYS}
        running_probability = ZERO
        for key in possible[:-1]:
            value = (Decimal(counts[key]) + alpha) / denominator
            probabilities[key] = value
            running_probability += value
        probabilities[possible[-1]] = ONE - running_probability
        q5 = probabilities["5up_15up"] + probabilities["5up_15down"]
        q15 = probabilities["5up_15up"] + probabilities["5down_15up"]
        cls._assert_structure(ordering=ordering, q_5_up=q5, q_15_up=q15)
        if abs(sum(probabilities.values(), ZERO) - ONE) > Decimal("1e-24"):
            raise RuntimeError("shared-terminal probabilities do not sum to one")
        scale_probabilities: dict[str, Mapping[str, Decimal]] = {}
        scale_path_counts: dict[str, int] = {}
        for scale, distribution in (uncertainty_scale_distributions or {}).items():
            if not isinstance(scale, str) or not scale:
                raise ValueError("uncertainty-scale key must be non-empty")
            if not isinstance(distribution, SharedTerminalTwap60Distribution):
                raise TypeError("uncertainty-scale value must be a distribution")
            if (
                distribution.strike_5 != strike5
                or distribution.strike_15 != strike15
                or distribution.strike_ordering is not ordering
            ):
                raise ValueError("uncertainty-scale distribution has different strikes")
            scale_probabilities[scale] = MappingProxyType(
                dict(distribution.outcome_probabilities)
            )
            scale_path_counts[scale] = len(distribution.scenarios)
        return cls(
            strike_5=strike5,
            strike_15=strike15,
            scenarios=frozen,
            outcomes=outcomes,
            outcome_counts=MappingProxyType(counts),
            outcome_probabilities=MappingProxyType(probabilities),
            q_5_up=q5,
            q_15_up=q15,
            jeffreys_alpha=alpha,
            strike_ordering=ordering,
            uncertainty_scale_outcome_probabilities=MappingProxyType(
                scale_probabilities
            ),
            uncertainty_scale_path_counts=MappingProxyType(scale_path_counts),
        )

    @staticmethod
    def _possible_outcomes(ordering: StrikeOrdering) -> tuple[str, ...]:
        if ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN:
            return ("5up_15up", "5up_15down", "5down_15down")
        if ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN:
            return ("5up_15up", "5down_15up", "5down_15down")
        return ("5up_15up", "5down_15down")

    @staticmethod
    def _assert_structure(
        *,
        ordering: StrikeOrdering,
        q_5_up: Decimal,
        q_15_up: Decimal,
    ) -> None:
        if ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN and q_5_up < q_15_up:
            raise RuntimeError("strike ordering requires q_5_up >= q_15_up")
        if ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN and q_15_up < q_5_up:
            raise RuntimeError("strike ordering requires q_15_up >= q_5_up")
        if ordering is StrikeOrdering.EQUAL and q_5_up != q_15_up:
            raise RuntimeError("equal strikes require equal marginals")

    @property
    def impossible_outcomes(self) -> tuple[str, ...]:
        possible = set(self._possible_outcomes(self.strike_ordering))
        return tuple(key for key in _OUTCOME_KEYS if key not in possible)

    def projected_marginals(
        self,
        *,
        q_5_up: Decimal,
        q_15_up: Decimal,
    ) -> tuple[Decimal, Decimal, bool]:
        """L2-project two marginals onto the shared-terminal constraint."""

        q5 = _probability(q_5_up, label="q_5_up")
        q15 = _probability(q_15_up, label="q_15_up")
        if self.strike_ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN:
            if q5 >= q15:
                return q5, q15, False
            pooled = (q5 + q15) / TWO
            return pooled, pooled, True
        if self.strike_ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN:
            if q15 >= q5:
                return q5, q15, False
            pooled = (q5 + q15) / TWO
            return pooled, pooled, True
        pooled = (q5 + q15) / TWO
        return pooled, pooled, q5 != q15

    def probabilities_from_marginals(
        self,
        *,
        q_5_up: Decimal,
        q_15_up: Decimal,
    ) -> Mapping[str, Decimal]:
        q5, q15, _ = self.projected_marginals(
            q_5_up=q_5_up,
            q_15_up=q_15_up,
        )
        if self.strike_ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN:
            probabilities = {
                "5up_15up": q15,
                "5up_15down": q5 - q15,
                "5down_15up": ZERO,
                "5down_15down": ONE - q5,
            }
        elif self.strike_ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN:
            probabilities = {
                "5up_15up": q5,
                "5up_15down": ZERO,
                "5down_15up": q15 - q5,
                "5down_15down": ONE - q15,
            }
        else:
            probabilities = {
                "5up_15up": q5,
                "5up_15down": ZERO,
                "5down_15up": ZERO,
                "5down_15down": ONE - q5,
            }
        if any(value < ZERO for value in probabilities.values()):
            raise RuntimeError("shared-terminal projection produced negative mass")
        if sum(probabilities.values(), ZERO) != ONE:
            raise RuntimeError("shared-terminal projection lost probability mass")
        return MappingProxyType(probabilities)

    def payoffs(self, action: PairAction) -> tuple[Decimal, ...]:
        if action is PairAction.LONG_15_UP_LONG_5_DOWN:
            return tuple(
                Decimal(int(up_15)) + Decimal(int(not up_5))
                for up_5, up_15 in self.outcomes
            )
        if action is PairAction.LONG_5_UP_LONG_15_DOWN:
            return tuple(
                Decimal(int(up_5)) + Decimal(int(not up_15))
                for up_5, up_15 in self.outcomes
            )
        raise ValueError("NO_TRADE has no payoff distribution")

    @property
    def uncertainty_scale_marginals(
        self,
    ) -> Mapping[str, tuple[Decimal, Decimal]]:
        pairs = {
            scale: (
                probabilities["5up_15up"] + probabilities["5up_15down"],
                probabilities["5up_15up"] + probabilities["5down_15up"],
            )
            for scale, probabilities in (
                self.uncertainty_scale_outcome_probabilities.items()
            )
        }
        return MappingProxyType(pairs)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-5m-15m-shared-terminal-distribution.v1",
            "settlement_model_id": SETTLEMENT_MODEL_ID,
            "model_version": MODEL_VERSION,
            "strike_5": _decimal_text(self.strike_5),
            "strike_15": _decimal_text(self.strike_15),
            "strike_ordering": self.strike_ordering.value,
            "scenario_count": len(self.scenarios),
            "jeffreys_alpha": _decimal_text(self.jeffreys_alpha),
            "q_5_up": _decimal_text(self.q_5_up),
            "q_15_up": _decimal_text(self.q_15_up),
            "outcome_counts": dict(self.outcome_counts),
            "outcome_probabilities": {
                key: _decimal_text(self.outcome_probabilities[key])
                for key in _OUTCOME_KEYS
            },
            "impossible_outcomes": list(self.impossible_outcomes),
            "model_uncertainty_method": (
                "fixed_volatility_scale_conditional_distributions"
            ),
            "uncertainty_scale_path_counts": dict(self.uncertainty_scale_path_counts),
            "uncertainty_scale_outcome_probabilities": {
                scale: {key: _decimal_text(probabilities[key]) for key in _OUTCOME_KEYS}
                for scale, probabilities in sorted(
                    self.uncertainty_scale_outcome_probabilities.items()
                )
            },
        }


@dataclass(frozen=True)
class SharedTerminalModelConfig:
    """Ex-ante v0.7 Monte Carlo configuration, never tuned on v0.6 PnL."""

    minimum_history_seconds: int = 300
    n_paths: int = 8_192
    seed: int = 712
    annualized_volatility_floor: Decimal = Decimal("0.25")
    return_intervals_seconds: tuple[int, ...] = (15, 60)
    student_t_df: int = 5
    uncertainty_scales: tuple[Decimal, ...] = (
        Decimal("0.75"),
        Decimal("1"),
        Decimal("1.5"),
    )
    uncertainty_weights: tuple[Decimal, ...] = (
        Decimal("0.2"),
        Decimal("0.6"),
        Decimal("0.2"),
    )
    jeffreys_alpha: Decimal = Decimal("0.5")
    maximum_predictor_staleness_ms: int = 5_000

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_history_seconds, bool)
            or not isinstance(self.minimum_history_seconds, int)
            or self.minimum_history_seconds < 120
        ):
            raise ValueError("minimum_history_seconds must be at least 120")
        if (
            isinstance(self.n_paths, bool)
            or not isinstance(self.n_paths, int)
            or not 100 <= self.n_paths <= 1_000_000
        ):
            raise ValueError("n_paths must be in [100, 1000000]")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("seed must be an integer")
        floor = _decimal(
            self.annualized_volatility_floor,
            label="annualized_volatility_floor",
        )
        alpha = _decimal(self.jeffreys_alpha, label="jeffreys_alpha")
        if floor <= ZERO or floor > Decimal("5"):
            raise ValueError("annualized_volatility_floor must be in (0, 5]")
        if alpha <= ZERO:
            raise ValueError("jeffreys_alpha must be positive")
        intervals = tuple(self.return_intervals_seconds)
        if not intervals or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in intervals
        ):
            raise ValueError("return intervals must be positive integers")
        if (
            isinstance(self.student_t_df, bool)
            or not isinstance(self.student_t_df, int)
            or self.student_t_df <= 2
        ):
            raise ValueError("student_t_df must exceed two")
        scales = tuple(
            _decimal(value, label="uncertainty_scale")
            for value in self.uncertainty_scales
        )
        weights = tuple(
            _decimal(value, label="uncertainty_weight")
            for value in self.uncertainty_weights
        )
        if len(scales) != len(weights) or not scales:
            raise ValueError("uncertainty scales and weights must align")
        if any(value <= ZERO for value in scales):
            raise ValueError("uncertainty scales must be positive")
        if any(value < ZERO for value in weights) or sum(weights, ZERO) != ONE:
            raise ValueError("uncertainty weights must be non-negative and sum to one")
        if (
            isinstance(self.maximum_predictor_staleness_ms, bool)
            or not isinstance(self.maximum_predictor_staleness_ms, int)
            or self.maximum_predictor_staleness_ms < 0
        ):
            raise ValueError("maximum_predictor_staleness_ms must be non-negative")
        object.__setattr__(self, "annualized_volatility_floor", floor)
        object.__setattr__(self, "return_intervals_seconds", intervals)
        object.__setattr__(self, "uncertainty_scales", scales)
        object.__setattr__(self, "uncertainty_weights", weights)
        object.__setattr__(self, "jeffreys_alpha", alpha)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-5m-15m-shared-terminal-model-config.v1",
            "settlement_model_id": SETTLEMENT_MODEL_ID,
            "model_version": MODEL_VERSION,
            "minimum_history_seconds": self.minimum_history_seconds,
            "n_paths": self.n_paths,
            "seed": self.seed,
            "annualized_volatility_floor": _decimal_text(
                self.annualized_volatility_floor
            ),
            "return_intervals_seconds": list(self.return_intervals_seconds),
            "student_t_df": self.student_t_df,
            "uncertainty_scales": [
                _decimal_text(value) for value in self.uncertainty_scales
            ],
            "uncertainty_weights": [
                _decimal_text(value) for value in self.uncertainty_weights
            ],
            "jeffreys_alpha": _decimal_text(self.jeffreys_alpha),
            "maximum_predictor_staleness_ms": self.maximum_predictor_staleness_ms,
            "empirical_oracle_residual_distribution": None,
            "residual_policy": "fixed_zero_after_causal_chainlink_minus_local_basis",
        }

    @property
    def artifact_hash(self) -> str:
        return _hash_document(self.to_document())


@dataclass(frozen=True)
class SharedTerminalSimulationResult:
    distribution: SharedTerminalTwap60Distribution
    annualized_volatility: Decimal
    volatility_estimates: Mapping[str, Decimal]
    current_local_twap_60: Decimal
    chainlink_minus_local_basis: Decimal
    model_config_hash: str
    seed: int

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "btc-5m-15m-shared-terminal-simulation.v1",
            "settlement_model_id": SETTLEMENT_MODEL_ID,
            "model_version": MODEL_VERSION,
            "annualized_volatility": _decimal_text(self.annualized_volatility),
            "volatility_estimates": {
                key: _decimal_text(value)
                for key, value in sorted(self.volatility_estimates.items())
            },
            "current_local_twap_60": _decimal_text(self.current_local_twap_60),
            "chainlink_minus_local_basis": _decimal_text(
                self.chainlink_minus_local_basis
            ),
            "model_config_hash": self.model_config_hash,
            "seed": self.seed,
            "distribution": self.distribution.to_document(),
        }


def _annualized_rms_log_return(
    prices: Sequence[float],
    *,
    interval_seconds: int,
) -> float:
    returns = [
        math.log(prices[index] / prices[index - interval_seconds])
        for index in range(interval_seconds, len(prices), interval_seconds)
    ]
    if not returns:
        return 0.0
    mean_square = sum(value * value for value in returns) / len(returns)
    return math.sqrt(mean_square * float(ANNUALIZATION_SECONDS) / interval_seconds)


def _stratified_path_counts(
    *,
    n_paths: int,
    weights: tuple[Decimal, ...],
) -> tuple[int, ...]:
    """Allocate an exact total path count without random mixture-count noise."""

    raw = tuple(Decimal(n_paths) * weight for weight in weights)
    counts = [int(value.to_integral_value(rounding=ROUND_DOWN)) for value in raw]
    remainder = n_paths - sum(counts)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(raw[index] - Decimal(counts[index])), index),
    )
    for index in order[:remainder]:
        counts[index] += 1
    if sum(counts) != n_paths:
        raise RuntimeError("stratified uncertainty-scale allocation lost paths")
    return tuple(counts)


def _standardized_student_t(rng: random.Random, *, degrees_of_freedom: int) -> float:
    normal = rng.gauss(0.0, 1.0)
    chi_square = rng.gammavariate(degrees_of_freedom / 2.0, 2.0)
    raw = normal / math.sqrt(chi_square / degrees_of_freedom)
    return raw * math.sqrt((degrees_of_freedom - 2.0) / degrees_of_freedom)


def simulate_shared_terminal_twap_60_distribution(
    *,
    predictor_prices: Sequence[TimedPrice],
    current_terminal_twap_60: Decimal,
    decision_at_ms: int,
    expiry_ms: int,
    strike_5: Decimal,
    strike_15: Decimal,
    config: SharedTerminalModelConfig = SharedTerminalModelConfig(),
    seed_salt: str = "",
) -> SharedTerminalSimulationResult:
    """Simulate one causal BTC path and one terminal 60-second TWAP per path."""

    if not isinstance(config, SharedTerminalModelConfig):
        raise TypeError("config must be SharedTerminalModelConfig")
    if (
        isinstance(decision_at_ms, bool)
        or not isinstance(decision_at_ms, int)
        or isinstance(expiry_ms, bool)
        or not isinstance(expiry_ms, int)
        or expiry_ms <= decision_at_ms
    ):
        _reject("model_window_invalid", "expiry must follow decision time")
    horizon_ms = expiry_ms - decision_at_ms
    if horizon_ms % 1_000 != 0:
        _reject("model_window_invalid", "decision and expiry must align to seconds")
    horizon_seconds = horizon_ms // 1_000
    frozen = tuple(predictor_prices)
    if len(frozen) < config.minimum_history_seconds or any(
        not isinstance(item, TimedPrice) for item in frozen
    ):
        _reject(
            "predictor_history_insufficient",
            f"at least {config.minimum_history_seconds} one-second prices are required",
        )
    timestamps = tuple(item.timestamp_ms for item in frozen)
    if any(
        current >= following for current, following in zip(timestamps, timestamps[1:])
    ):
        _reject("predictor_time_invalid", "predictor timestamps must increase")
    if any(
        following - current != 1_000
        for current, following in zip(timestamps, timestamps[1:])
    ):
        _reject(
            "predictor_sampling_invalid",
            "predictor history must be exactly one-second sampled",
        )
    if timestamps[-1] > decision_at_ms:
        _reject("predictor_leakage", "predictor observation is after decision time")
    if decision_at_ms - timestamps[-1] > config.maximum_predictor_staleness_ms:
        _reject("predictor_stale", "last predictor observation is stale")

    current_chainlink = _decimal(
        current_terminal_twap_60,
        label="current_terminal_twap_60",
    )
    if current_chainlink <= ZERO:
        _reject("model_state_invalid", "current terminal TWAP state must be positive")
    prices = [float(item.price) for item in frozen]
    if any(not math.isfinite(value) or value <= 0 for value in prices):
        _reject("predictor_history_invalid", "predictor prices must be positive")

    volatility_estimates_float = {
        f"rms_{interval}s": _annualized_rms_log_return(
            prices,
            interval_seconds=interval,
        )
        for interval in config.return_intervals_seconds
    }
    annualized_volatility_float = max(
        float(config.annualized_volatility_floor),
        *volatility_estimates_float.values(),
    )
    if (
        not math.isfinite(annualized_volatility_float)
        or annualized_volatility_float <= 0
    ):
        _reject("predictor_variance_missing", "volatility estimate is unusable")
    per_second_sigma = annualized_volatility_float / math.sqrt(
        float(ANNUALIZATION_SECONDS)
    )

    local_twap_now_float = sum(prices[-60:]) / min(60, len(prices))
    basis_float = float(current_chainlink) - local_twap_now_float
    observed_terminal_prefix: list[float] = []
    terminal_window_start_ms = expiry_ms - 60_000
    for item in frozen:
        if terminal_window_start_ms <= item.timestamp_ms <= decision_at_ms:
            observed_terminal_prefix.append(float(item.price))
    required_future_terminal_points = min(60, horizon_seconds)
    if len(observed_terminal_prefix) + required_future_terminal_points < 60:
        _reject(
            "terminal_window_history_missing",
            "predictor history does not cover the observed part of terminal TWAP",
        )
    observed_count = 60 - required_future_terminal_points
    observed_terminal_prefix = (
        observed_terminal_prefix[-observed_count:] if observed_count else []
    )

    seed_document = {
        "base_seed": config.seed,
        "seed_salt": seed_salt,
        "decision_at_ms": decision_at_ms,
        "expiry_ms": expiry_ms,
        "strike_5": str(_decimal(strike_5, label="strike_5")),
        "strike_15": str(_decimal(strike_15, label="strike_15")),
    }
    derived_seed = int(_hash_document(seed_document)[:16], 16)
    scenarios: list[SharedTerminalTwap60Scenario] = []
    scale_scenarios: dict[str, tuple[SharedTerminalTwap60Scenario, ...]] = {}
    path_counts = _stratified_path_counts(
        n_paths=config.n_paths,
        weights=config.uncertainty_weights,
    )
    for scale, path_count in zip(config.uncertainty_scales, path_counts):
        if path_count == 0:
            continue
        # Every fixed scale starts from the same deterministic RNG stream.  This
        # supplies common random numbers across the preregistered model-uncertainty
        # conditions and nested prefixes as n_paths changes.
        rng = random.Random(derived_seed)
        volatility_scale = float(scale)
        conditional: list[SharedTerminalTwap60Scenario] = []
        for _ in range(path_count):
            price = prices[-1]
            future_terminal: list[float] = []
            for step in range(1, horizon_seconds + 1):
                innovation = _standardized_student_t(
                    rng,
                    degrees_of_freedom=config.student_t_df,
                )
                price *= math.exp(per_second_sigma * volatility_scale * innovation)
                if horizon_seconds - step < 60:
                    future_terminal.append(price)
            terminal_local_values = [*observed_terminal_prefix, *future_terminal]
            if len(terminal_local_values) != 60:
                raise RuntimeError(
                    "terminal TWAP path does not contain exactly 60 seconds"
                )
            terminal_local = sum(terminal_local_values) / 60.0
            terminal_chainlink = terminal_local + basis_float
            if not math.isfinite(terminal_chainlink) or terminal_chainlink <= 0:
                _reject("scenario_invalid", "simulated terminal TWAP is non-positive")
            conditional.append(
                SharedTerminalTwap60Scenario(
                    terminal_twap_60=Decimal(str(terminal_chainlink)),
                )
            )
        scale_key = _decimal_text(scale)
        assert scale_key is not None
        scale_scenarios[scale_key] = tuple(conditional)
        scenarios.extend(conditional)

    scale_distributions = {
        scale: SharedTerminalTwap60Distribution.from_scenarios(
            conditional,
            strike_5=strike_5,
            strike_15=strike_15,
            jeffreys_alpha=config.jeffreys_alpha,
        )
        for scale, conditional in scale_scenarios.items()
    }

    distribution = SharedTerminalTwap60Distribution.from_scenarios(
        scenarios,
        strike_5=strike_5,
        strike_15=strike_15,
        jeffreys_alpha=config.jeffreys_alpha,
        uncertainty_scale_distributions=scale_distributions,
    )
    estimates = {
        key: Decimal(str(value))
        for key, value in sorted(volatility_estimates_float.items())
    }
    return SharedTerminalSimulationResult(
        distribution=distribution,
        annualized_volatility=Decimal(str(annualized_volatility_float)),
        volatility_estimates=MappingProxyType(estimates),
        current_local_twap_60=Decimal(str(local_twap_now_float)),
        chainlink_minus_local_basis=Decimal(str(basis_float)),
        model_config_hash=config.artifact_hash,
        seed=derived_seed,
    )


@dataclass(frozen=True)
class ProbabilityPairTrainingRow:
    """One pair-integrity row assigned to a capture-derived common expiry."""

    event_cluster_id: str
    expiry_ms: int
    decision_tau_seconds: int
    decision_at_ms: int
    label_available_at_ms: int
    model_q_5_up: Decimal
    model_q_15_up: Decimal
    market_q_5_up: Decimal
    market_q_15_up: Decimal
    actual_5_up: bool
    actual_15_up: bool
    strike_5: Decimal
    strike_15: Decimal
    split: str = "train"
    raw_top_ask_q_5_up: Decimal | None = None
    raw_top_ask_q_15_up: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_cluster_id, str) or not self.event_cluster_id:
            raise ValueError("event_cluster_id must be non-empty")
        if (
            isinstance(self.decision_tau_seconds, bool)
            or not isinstance(self.decision_tau_seconds, int)
            or self.decision_tau_seconds < 0
        ):
            raise ValueError("decision_tau_seconds must be non-negative")
        for name in ("expiry_ms", "decision_at_ms", "label_available_at_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.expiry_ms <= 0:
            raise ValueError("expiry_ms must be positive")
        if self.decision_at_ms > self.expiry_ms:
            raise ValueError("decision must not occur after the common expiry")
        if self.label_available_at_ms < self.expiry_ms:
            raise ValueError("label cannot become available before the common expiry")
        if self.label_available_at_ms <= self.decision_at_ms:
            raise ValueError("label must become available after the decision")
        for name in (
            "model_q_5_up",
            "model_q_15_up",
            "market_q_5_up",
            "market_q_15_up",
        ):
            object.__setattr__(
                self,
                name,
                _probability(getattr(self, name), label=name),
            )
        for name in ("raw_top_ask_q_5_up", "raw_top_ask_q_15_up"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _probability(value, label=name))
        for name in ("strike_5", "strike_15"):
            parsed = _decimal(getattr(self, name), label=name)
            if parsed <= ZERO:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, parsed)
        if not isinstance(self.actual_5_up, bool) or not isinstance(
            self.actual_15_up, bool
        ):
            raise TypeError("actual outcomes must be bool")
        if self.split not in {"train", "validation", "test"}:
            raise ValueError("split must be train, validation, or test")
        if self.ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN:
            if self.model_q_5_up < self.model_q_15_up:
                raise ValueError("model probabilities violate strike ordering")
            if self.actual_15_up and not self.actual_5_up:
                raise ValueError("actual outcomes violate strike ordering")
        elif self.ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN:
            if self.model_q_15_up < self.model_q_5_up:
                raise ValueError("model probabilities violate strike ordering")
            if self.actual_5_up and not self.actual_15_up:
                raise ValueError("actual outcomes violate strike ordering")
        else:
            if self.model_q_5_up != self.model_q_15_up:
                raise ValueError("equal strikes require equal model probabilities")
            if self.actual_5_up != self.actual_15_up:
                raise ValueError("equal strikes require equal actual outcomes")

    @property
    def identity(self) -> tuple[str, int]:
        return self.expiry_cluster_id, self.decision_tau_seconds

    @property
    def canonical_pair_id(self) -> str:
        return self.event_cluster_id

    @property
    def expiry_cluster_id(self) -> str:
        return canonical_expiry_cluster_id(self.expiry_ms)

    @property
    def ordering(self) -> StrikeOrdering:
        return (
            StrikeOrdering.FIVE_BELOW_FIFTEEN
            if self.strike_5 < self.strike_15
            else StrikeOrdering.FIVE_ABOVE_FIFTEEN
            if self.strike_5 > self.strike_15
            else StrikeOrdering.EQUAL
        )

    def project(self, q5: Decimal, q15: Decimal) -> tuple[Decimal, Decimal]:
        q_5 = _probability(q5, label="q5")
        q_15 = _probability(q15, label="q15")
        if self.ordering is StrikeOrdering.FIVE_BELOW_FIFTEEN and q_5 < q_15:
            pooled = (q_5 + q_15) / TWO
            return pooled, pooled
        if self.ordering is StrikeOrdering.FIVE_ABOVE_FIFTEEN and q_15 < q_5:
            pooled = (q_5 + q_15) / TWO
            return pooled, pooled
        if self.ordering is StrikeOrdering.EQUAL:
            pooled = (q_5 + q_15) / TWO
            return pooled, pooled
        return q_5, q_15

    @property
    def coherent_market_marginals(self) -> tuple[Decimal, Decimal]:
        return self.project(self.market_q_5_up, self.market_q_15_up)


def _assert_one_pair_per_expiry(
    rows: Sequence[ProbabilityPairTrainingRow],
    *,
    label: str,
) -> None:
    pair_by_expiry: dict[str, str] = {}
    expiry_by_pair: dict[str, str] = {}
    for row in rows:
        expiry_cluster_id = row.expiry_cluster_id
        prior_pair = pair_by_expiry.setdefault(
            expiry_cluster_id,
            row.canonical_pair_id,
        )
        if prior_pair != row.canonical_pair_id:
            raise ValueError(f"{label} common expiry maps to multiple market pairs")
        prior_expiry = expiry_by_pair.setdefault(
            row.canonical_pair_id,
            expiry_cluster_id,
        )
        if prior_expiry != expiry_cluster_id:
            raise ValueError(f"{label} market pair maps to multiple common expiries")


@dataclass(frozen=True)
class TrainOnlyShrinkageArtifact:
    """One model-weight selected on cluster-equal training Brier only."""

    model_weight: Decimal
    train_selected_model_weight: Decimal
    selection_overridden_for_preregistered_sensitivity: bool
    diagnostic_setting_id: str | None
    candidate_model_weights: tuple[Decimal, ...]
    cluster_equal_brier_by_weight: Mapping[str, Decimal]
    fit_at_ms: int
    maximum_label_available_at_ms: int
    training_expiry_cluster_ids: tuple[str, ...]
    training_pair_ids: tuple[str, ...]
    training_row_count: int
    training_rows_sha256: str
    base_train_artifact_hash: str | None
    settlement_regime: str
    settlement_model_id: str
    model_version: str
    artifact_hash: str

    @classmethod
    def fit(
        cls,
        rows: Sequence[ProbabilityPairTrainingRow],
        *,
        fit_at_ms: int,
        candidate_model_weights: Sequence[Decimal] = (
            Decimal("0"),
            Decimal("0.25"),
            Decimal("0.5"),
            Decimal("0.75"),
            Decimal("1"),
        ),
    ) -> TrainOnlyShrinkageArtifact:
        frozen = tuple(rows)
        if not frozen or any(
            not isinstance(row, ProbabilityPairTrainingRow) for row in frozen
        ):
            raise ValueError("at least one training probability pair is required")
        if (
            isinstance(fit_at_ms, bool)
            or not isinstance(fit_at_ms, int)
            or fit_at_ms < 0
        ):
            raise ValueError("fit_at_ms must be non-negative")
        if any(row.split != "train" for row in frozen):
            raise ValueError("shrinkage fitting accepts train rows only")
        if any(row.label_available_at_ms >= fit_at_ms for row in frozen):
            raise ValueError("all training labels must predate fit_at_ms")
        _assert_one_pair_per_expiry(frozen, label="training")
        identities = tuple(row.identity for row in frozen)
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate training expiry/tau identity")
        weights = tuple(
            _probability(value, label="candidate_model_weight")
            for value in candidate_model_weights
        )
        if not weights or len(set(weights)) != len(weights):
            raise ValueError("candidate model weights must be unique and non-empty")
        weights = tuple(sorted(weights))

        scores = {
            weight: _cluster_equal_pair_brier(frozen, model_weight=weight)
            for weight in weights
        }
        selected = min(weights, key=lambda weight: (scores[weight], weight))
        expiry_cluster_ids = tuple(sorted({row.expiry_cluster_id for row in frozen}))
        pair_ids = tuple(sorted({row.canonical_pair_id for row in frozen}))
        training_rows_document = [
            {
                "canonical_pair_id": row.canonical_pair_id,
                "expiry_ms": row.expiry_ms,
                "expiry_cluster_id": row.expiry_cluster_id,
                "decision_tau_seconds": row.decision_tau_seconds,
                "decision_at_ms": row.decision_at_ms,
                "label_available_at_ms": row.label_available_at_ms,
                "model_q_5_up": str(row.model_q_5_up),
                "model_q_15_up": str(row.model_q_15_up),
                "market_q_5_up": str(row.market_q_5_up),
                "market_q_15_up": str(row.market_q_15_up),
                "coherent_market_q_5_up": str(row.coherent_market_marginals[0]),
                "coherent_market_q_15_up": str(row.coherent_market_marginals[1]),
                "raw_top_ask_q_5_up": (
                    None
                    if row.raw_top_ask_q_5_up is None
                    else str(row.raw_top_ask_q_5_up)
                ),
                "raw_top_ask_q_15_up": (
                    None
                    if row.raw_top_ask_q_15_up is None
                    else str(row.raw_top_ask_q_15_up)
                ),
                "actual_5_up": row.actual_5_up,
                "actual_15_up": row.actual_15_up,
                "strike_5": str(row.strike_5),
                "strike_15": str(row.strike_15),
            }
            for row in sorted(
                frozen,
                key=lambda item: (
                    item.expiry_ms,
                    item.canonical_pair_id,
                    item.decision_tau_seconds,
                ),
            )
        ]
        training_rows_sha256 = _hash_document({"training_rows": training_rows_document})
        payload = {
            "schema_version": SHRINKAGE_SCHEMA_VERSION,
            "selection_metric": "cluster_equal_mean_of_two_horizon_brier",
            "tie_break": "lowest_model_weight",
            "model_weight": _decimal_text(selected),
            "train_selected_model_weight": _decimal_text(selected),
            "selection_overridden_for_preregistered_sensitivity": False,
            "diagnostic_setting_id": None,
            "candidate_model_weights": [_decimal_text(value) for value in weights],
            "cluster_equal_brier_by_weight": {
                str(weight): _decimal_text(scores[weight]) for weight in weights
            },
            "fit_at_ms": fit_at_ms,
            "maximum_label_available_at_ms": max(
                row.label_available_at_ms for row in frozen
            ),
            "training_expiry_cluster_ids": list(expiry_cluster_ids),
            "training_pair_ids": list(pair_ids),
            "training_row_count": len(frozen),
            "training_rows_sha256": training_rows_sha256,
            "base_train_artifact_hash": None,
            "settlement_regime": V06_SETTLEMENT_REGIME_ID,
            "settlement_model_id": SETTLEMENT_MODEL_ID,
            "model_version": MODEL_VERSION,
        }
        return cls(
            model_weight=selected,
            train_selected_model_weight=selected,
            selection_overridden_for_preregistered_sensitivity=False,
            diagnostic_setting_id=None,
            candidate_model_weights=weights,
            cluster_equal_brier_by_weight=MappingProxyType(
                {str(weight): scores[weight] for weight in weights}
            ),
            fit_at_ms=fit_at_ms,
            maximum_label_available_at_ms=max(
                row.label_available_at_ms for row in frozen
            ),
            training_expiry_cluster_ids=expiry_cluster_ids,
            training_pair_ids=pair_ids,
            training_row_count=len(frozen),
            training_rows_sha256=training_rows_sha256,
            base_train_artifact_hash=None,
            settlement_regime=V06_SETTLEMENT_REGIME_ID,
            settlement_model_id=SETTLEMENT_MODEL_ID,
            model_version=MODEL_VERSION,
            artifact_hash=_hash_document(payload),
        )

    def with_diagnostic_model_weight(
        self,
        *,
        model_weight: Decimal,
        setting_id: str,
    ) -> TrainOnlyShrinkageArtifact:
        """Return a traceable sensitivity-only override of the trained weight."""

        forced = _probability(model_weight, label="diagnostic model_weight")
        if not isinstance(setting_id, str) or not setting_id:
            raise ValueError("diagnostic setting_id must be non-empty")
        payload = self.to_document()
        payload.pop("artifact_hash")
        payload.update(
            {
                "model_weight": _decimal_text(forced),
                "train_selected_model_weight": _decimal_text(
                    self.train_selected_model_weight
                ),
                "selection_overridden_for_preregistered_sensitivity": True,
                "diagnostic_setting_id": setting_id,
                "base_train_artifact_hash": self.artifact_hash,
            }
        )
        return replace(
            self,
            model_weight=forced,
            selection_overridden_for_preregistered_sensitivity=True,
            diagnostic_setting_id=setting_id,
            base_train_artifact_hash=self.artifact_hash,
            artifact_hash=_hash_document(payload),
        )

    def transform(
        self,
        distribution: SharedTerminalTwap60Distribution,
        *,
        market_q_5_up: Decimal,
        market_q_15_up: Decimal,
    ) -> tuple[Decimal, Decimal, bool]:
        if not isinstance(distribution, SharedTerminalTwap60Distribution):
            raise TypeError("distribution must be shared-terminal v0.7")
        return self.transform_model_marginals(
            distribution,
            model_q_5_up=distribution.q_5_up,
            model_q_15_up=distribution.q_15_up,
            market_q_5_up=market_q_5_up,
            market_q_15_up=market_q_15_up,
        )

    def transform_model_marginals(
        self,
        distribution: SharedTerminalTwap60Distribution,
        *,
        model_q_5_up: Decimal,
        model_q_15_up: Decimal,
        market_q_5_up: Decimal,
        market_q_15_up: Decimal,
    ) -> tuple[Decimal, Decimal, bool]:
        if not isinstance(distribution, SharedTerminalTwap60Distribution):
            raise TypeError("distribution must be shared-terminal v0.7")
        model5 = _probability(model_q_5_up, label="model_q_5_up")
        model15 = _probability(model_q_15_up, label="model_q_15_up")
        market5 = _probability(market_q_5_up, label="market_q_5_up")
        market15 = _probability(market_q_15_up, label="market_q_15_up")
        q5 = self.model_weight * model5 + (ONE - self.model_weight) * market5
        q15 = self.model_weight * model15 + (ONE - self.model_weight) * market15
        return distribution.projected_marginals(q_5_up=q5, q_15_up=q15)

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": SHRINKAGE_SCHEMA_VERSION,
            "selection_metric": "cluster_equal_mean_of_two_horizon_brier",
            "tie_break": "lowest_model_weight",
            "model_weight": _decimal_text(self.model_weight),
            "train_selected_model_weight": _decimal_text(
                self.train_selected_model_weight
            ),
            "selection_overridden_for_preregistered_sensitivity": (
                self.selection_overridden_for_preregistered_sensitivity
            ),
            "diagnostic_setting_id": self.diagnostic_setting_id,
            "candidate_model_weights": [
                _decimal_text(value) for value in self.candidate_model_weights
            ],
            "cluster_equal_brier_by_weight": {
                key: _decimal_text(value)
                for key, value in self.cluster_equal_brier_by_weight.items()
            },
            "fit_at_ms": self.fit_at_ms,
            "maximum_label_available_at_ms": self.maximum_label_available_at_ms,
            "training_expiry_cluster_ids": list(self.training_expiry_cluster_ids),
            "training_pair_ids": list(self.training_pair_ids),
            "training_row_count": self.training_row_count,
            "training_rows_sha256": self.training_rows_sha256,
            "base_train_artifact_hash": self.base_train_artifact_hash,
            "settlement_regime": self.settlement_regime,
            "settlement_model_id": self.settlement_model_id,
            "model_version": self.model_version,
            "artifact_hash": self.artifact_hash,
        }


def _cluster_equal_pair_brier(
    rows: Sequence[ProbabilityPairTrainingRow],
    *,
    model_weight: Decimal,
) -> Decimal:
    by_cluster: dict[str, list[Decimal]] = {}
    for row in rows:
        q5 = model_weight * row.model_q_5_up + (ONE - model_weight) * row.market_q_5_up
        q15 = (
            model_weight * row.model_q_15_up + (ONE - model_weight) * row.market_q_15_up
        )
        q5, q15 = row.project(q5, q15)
        actual5 = ONE if row.actual_5_up else ZERO
        actual15 = ONE if row.actual_15_up else ZERO
        pair_brier = ((q5 - actual5) ** 2 + (q15 - actual15) ** 2) / TWO
        by_cluster.setdefault(row.expiry_cluster_id, []).append(pair_brier)
    cluster_scores = [
        sum(values, ZERO) / Decimal(len(values)) for values in by_cluster.values()
    ]
    return sum(cluster_scores, ZERO) / Decimal(len(cluster_scores))


def _cluster_equal_horizon_brier(
    rows: Sequence[ProbabilityPairTrainingRow],
    *,
    horizon: str,
    model_weight: Decimal,
) -> Decimal:
    by_cluster: dict[str, list[Decimal]] = {}
    for row in rows:
        q5 = model_weight * row.model_q_5_up + (ONE - model_weight) * row.market_q_5_up
        q15 = (
            model_weight * row.model_q_15_up + (ONE - model_weight) * row.market_q_15_up
        )
        q5, q15 = row.project(q5, q15)
        probability = q5 if horizon == "5m" else q15
        actual = (
            ONE if (row.actual_5_up if horizon == "5m" else row.actual_15_up) else ZERO
        )
        by_cluster.setdefault(row.expiry_cluster_id, []).append(
            (probability - actual) ** 2
        )
    cluster_scores = [
        sum(values, ZERO) / Decimal(len(values)) for values in by_cluster.values()
    ]
    return sum(cluster_scores, ZERO) / Decimal(len(cluster_scores))


def _cluster_equal_executable_market_horizon_brier(
    rows: Sequence[ProbabilityPairTrainingRow],
    *,
    horizon: str,
) -> Decimal:
    """Score the binding coherent executable baseline."""

    by_cluster: dict[str, list[Decimal]] = {}
    for row in rows:
        market5, market15 = row.coherent_market_marginals
        probability = market5 if horizon == "5m" else market15
        actual = (
            ONE if (row.actual_5_up if horizon == "5m" else row.actual_15_up) else ZERO
        )
        by_cluster.setdefault(row.expiry_cluster_id, []).append(
            (probability - actual) ** 2
        )
    cluster_scores = [
        sum(values, ZERO) / Decimal(len(values)) for values in by_cluster.values()
    ]
    return sum(cluster_scores, ZERO) / Decimal(len(cluster_scores))


@dataclass(frozen=True)
class ValidationVetoEvidence:
    """A veto-only validation result; it never changes the selected weight."""

    passed: bool
    evaluated_at_ms: int
    maximum_label_available_at_ms: int
    validation_expiry_cluster_ids: tuple[str, ...]
    validation_pair_ids: tuple[str, ...]
    selected_model_weight: Decimal
    blended_brier_5m: Decimal
    market_brier_5m: Decimal
    blended_brier_15m: Decimal
    market_brier_15m: Decimal
    reason_codes: tuple[str, ...]
    shrinkage_artifact_hash: str
    artifact_hash: str

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": VALIDATION_VETO_SCHEMA_VERSION,
            "passed": self.passed,
            "evaluated_at_ms": self.evaluated_at_ms,
            "maximum_label_available_at_ms": self.maximum_label_available_at_ms,
            "validation_expiry_cluster_ids": list(
                self.validation_expiry_cluster_ids
            ),
            "validation_pair_ids": list(self.validation_pair_ids),
            "selected_model_weight": _decimal_text(self.selected_model_weight),
            "blended_brier": {
                "5m": _decimal_text(self.blended_brier_5m),
                "15m": _decimal_text(self.blended_brier_15m),
            },
            "market_brier": {
                "5m": _decimal_text(self.market_brier_5m),
                "15m": _decimal_text(self.market_brier_15m),
            },
            "reason_codes": list(self.reason_codes),
            "shrinkage_artifact_hash": self.shrinkage_artifact_hash,
            "artifact_hash": self.artifact_hash,
        }


def evaluate_validation_veto(
    rows: Sequence[ProbabilityPairTrainingRow],
    *,
    shrinkage: TrainOnlyShrinkageArtifact,
    evaluated_at_ms: int,
) -> ValidationVetoEvidence:
    frozen = tuple(rows)
    if not frozen or any(row.split != "validation" for row in frozen):
        raise ValueError("validation veto requires non-empty validation-only rows")
    if (
        isinstance(evaluated_at_ms, bool)
        or not isinstance(evaluated_at_ms, int)
        or evaluated_at_ms < 0
    ):
        raise ValueError("evaluated_at_ms must be non-negative")
    if any(row.label_available_at_ms >= evaluated_at_ms for row in frozen):
        raise ValueError("all validation labels must predate veto evaluation")
    _assert_one_pair_per_expiry(frozen, label="validation")
    identities = tuple(row.identity for row in frozen)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate validation expiry/tau identity")
    if set(row.expiry_cluster_id for row in frozen) & set(
        shrinkage.training_expiry_cluster_ids
    ):
        raise ValueError("validation common expiries overlap shrinkage training")
    selected = shrinkage.model_weight
    blended5 = _cluster_equal_horizon_brier(
        frozen,
        horizon="5m",
        model_weight=selected,
    )
    market5 = _cluster_equal_executable_market_horizon_brier(
        frozen,
        horizon="5m",
    )
    blended15 = _cluster_equal_horizon_brier(
        frozen,
        horizon="15m",
        model_weight=selected,
    )
    market15 = _cluster_equal_executable_market_horizon_brier(
        frozen,
        horizon="15m",
    )
    reasons: list[str] = []
    if blended5 >= market5:
        reasons.append("validation_5m_brier_does_not_beat_coherent_market")
    if blended15 >= market15:
        reasons.append("validation_15m_brier_does_not_beat_coherent_market")
    expiry_cluster_ids = tuple(sorted({row.expiry_cluster_id for row in frozen}))
    pair_ids = tuple(sorted({row.canonical_pair_id for row in frozen}))
    payload = {
        "schema_version": VALIDATION_VETO_SCHEMA_VERSION,
        "passed": not reasons,
        "evaluated_at_ms": evaluated_at_ms,
        "maximum_label_available_at_ms": max(
            row.label_available_at_ms for row in frozen
        ),
        "validation_expiry_cluster_ids": list(expiry_cluster_ids),
        "validation_pair_ids": list(pair_ids),
        "selected_model_weight": _decimal_text(selected),
        "blended_brier": {
            "5m": _decimal_text(blended5),
            "15m": _decimal_text(blended15),
        },
        "market_brier": {
            "5m": _decimal_text(market5),
            "15m": _decimal_text(market15),
        },
        "reason_codes": reasons,
        "shrinkage_artifact_hash": shrinkage.artifact_hash,
    }
    return ValidationVetoEvidence(
        passed=not reasons,
        evaluated_at_ms=evaluated_at_ms,
        maximum_label_available_at_ms=max(row.label_available_at_ms for row in frozen),
        validation_expiry_cluster_ids=expiry_cluster_ids,
        validation_pair_ids=pair_ids,
        selected_model_weight=selected,
        blended_brier_5m=blended5,
        market_brier_5m=market5,
        blended_brier_15m=blended15,
        market_brier_15m=market15,
        reason_codes=tuple(reasons),
        shrinkage_artifact_hash=shrinkage.artifact_hash,
        artifact_hash=_hash_document(payload),
    )


@dataclass(frozen=True)
class SharedTerminalDataHealth:
    decision_at_ms: int
    terminal_twap_60_observed_at_ms: int
    terminal_twap_60_received_at_ms: int
    absolute_clock_drift_ms: int
    shrinkage: TrainOnlyShrinkageArtifact | None
    validation_veto: ValidationVetoEvidence | None

    def __post_init__(self) -> None:
        for name in (
            "decision_at_ms",
            "terminal_twap_60_observed_at_ms",
            "terminal_twap_60_received_at_ms",
            "absolute_clock_drift_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.shrinkage is not None and not isinstance(
            self.shrinkage,
            TrainOnlyShrinkageArtifact,
        ):
            raise TypeError("shrinkage must be a train-only artifact or None")
        if self.validation_veto is not None and not isinstance(
            self.validation_veto,
            ValidationVetoEvidence,
        ):
            raise TypeError("validation_veto must be veto evidence or None")

    def legacy_execution_health(self) -> DataHealth:
        """Map one explicit 60s state into the legacy execution health carrier.

        This is not a settlement-model conversion.  The duplicated timestamps
        only let the already-conservative v0.6 book/execution machinery consume
        one v0.7 health record without altering frozen code.
        """

        return DataHealth(
            decision_at_ms=self.decision_at_ms,
            twap_30_observed_at_ms=self.terminal_twap_60_observed_at_ms,
            twap_60_observed_at_ms=self.terminal_twap_60_observed_at_ms,
            twap_30_received_at_ms=self.terminal_twap_60_received_at_ms,
            twap_60_received_at_ms=self.terminal_twap_60_received_at_ms,
            absolute_clock_drift_ms=self.absolute_clock_drift_ms,
            calibration_5=None,
            calibration_15=None,
        )


@dataclass(frozen=True)
class V07StrategyConfig:
    """Opt-in v0.7 action thresholds; v0.6 promotion policy is untouched."""

    tau_min_seconds: int = 45
    tau_max_seconds: int = 240
    maximum_spread_each_leg: Decimal = Decimal("0.03")
    maximum_chainlink_staleness_ms: int = 5_000
    maximum_book_staleness_ms: int = 750
    maximum_clock_drift_ms: int = 100
    minimum_market_price: Decimal = Decimal("0.05")
    maximum_market_price: Decimal = Decimal("0.95")
    pair_risk_usdc: Decimal = Decimal("25")
    market_baseline_quantity: Decimal = Decimal("5")
    minimum_net_expected_pnl_per_pair: Decimal = Decimal("0.015")
    uncertainty_multiplier: Decimal = Decimal("1.25")
    cvar_tail_probability: Decimal = Decimal("0.05")
    max_leg_delay_ms: int = 750
    initial_cash: Decimal = Decimal("10000")
    settlement_regime: str = V06_SETTLEMENT_REGIME_ID

    def __post_init__(self) -> None:
        if (
            isinstance(self.tau_min_seconds, bool)
            or not isinstance(self.tau_min_seconds, int)
            or isinstance(self.tau_max_seconds, bool)
            or not isinstance(self.tau_max_seconds, int)
            or self.tau_min_seconds < 0
            or self.tau_max_seconds < self.tau_min_seconds
        ):
            raise ValueError("tau bounds are invalid")
        for name in (
            "maximum_spread_each_leg",
            "minimum_market_price",
            "maximum_market_price",
            "pair_risk_usdc",
            "market_baseline_quantity",
            "minimum_net_expected_pnl_per_pair",
            "uncertainty_multiplier",
            "cvar_tail_probability",
            "initial_cash",
        ):
            parsed = _decimal(getattr(self, name), label=name)
            if parsed < ZERO:
                raise ValueError(f"{name} cannot be negative")
            object.__setattr__(self, name, parsed)
        if not ZERO < self.cvar_tail_probability <= ONE:
            raise ValueError("cvar_tail_probability must be in (0, 1]")
        if not ZERO <= self.minimum_market_price < self.maximum_market_price <= ONE:
            raise ValueError("market price bounds are invalid")
        if (
            self.pair_risk_usdc <= ZERO
            or self.market_baseline_quantity <= ZERO
            or self.initial_cash <= ZERO
        ):
            raise ValueError(
                "pair_risk_usdc, market_baseline_quantity, and initial_cash "
                "must be positive"
            )
        if self.settlement_regime != V06_SETTLEMENT_REGIME_ID:
            raise ValueError("v0.7 requires the shared 60s/60s settlement regime")
        for name in (
            "maximum_chainlink_staleness_ms",
            "maximum_book_staleness_ms",
            "maximum_clock_drift_ms",
            "max_leg_delay_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be non-negative")

    def execution_config(self) -> StrategyConfig:
        return StrategyConfig(
            tau_min_seconds=self.tau_min_seconds,
            tau_max_seconds=self.tau_max_seconds,
            maximum_spread_each_leg=self.maximum_spread_each_leg,
            maximum_chainlink_staleness_ms=self.maximum_chainlink_staleness_ms,
            maximum_book_staleness_ms=self.maximum_book_staleness_ms,
            maximum_clock_drift_ms=self.maximum_clock_drift_ms,
            minimum_market_price=self.minimum_market_price,
            maximum_market_price=self.maximum_market_price,
            pair_risk_usdc=self.pair_risk_usdc,
            minimum_net_expected_pnl_per_pair=self.minimum_net_expected_pnl_per_pair,
            uncertainty_multiplier=self.uncertainty_multiplier,
            cvar_tail_probability=self.cvar_tail_probability,
            settlement_regime=self.settlement_regime,
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
    model_uncertainty_downside_per_pair: Decimal
    adjusted_pnl_per_pair: Decimal
    cvar_per_pair: Decimal
    loss_probability: Decimal


def raw_top_ask_probability(
    books: Mapping[str, OrderBookSnapshot],
    *,
    up_token_id: str,
    down_token_id: str,
) -> Decimal | None:
    """Normalize top asks for diagnostics only; never use this as a gate."""

    up = books.get(up_token_id)
    down = books.get(down_token_id)
    if up is None or down is None or up.best_ask is None or down.best_ask is None:
        return None
    denominator = up.best_ask + down.best_ask
    if denominator <= ZERO:
        return None
    return up.best_ask / denominator


def executable_binary_ask_probability(
    books: Mapping[str, OrderBookSnapshot],
    *,
    up_token_id: str,
    down_token_id: str,
    target_quantity: Decimal,
    fee_schedule: ExecutionFeeSchedule,
) -> Decimal | None:
    """Normalize fixed-size, depth-walked, fee-inclusive Up/Down costs."""

    quantity = _decimal(target_quantity, label="target_quantity")
    if quantity <= ZERO:
        raise ValueError("target_quantity must be positive")
    if not isinstance(fee_schedule, ExecutionFeeSchedule):
        raise TypeError("fee_schedule must be ExecutionFeeSchedule")
    up = books.get(up_token_id)
    down = books.get(down_token_id)
    if up is None or down is None:
        return None
    if up.token_id != up_token_id or down.token_id != down_token_id:
        return None
    if quantity < max(up.minimum_order_size, down.minimum_order_size):
        return None
    up_quote = _quote_buy(up, quantity=quantity, fees=fee_schedule)
    down_quote = _quote_buy(down, quantity=quantity, fees=fee_schedule)
    if up_quote is None or down_quote is None:
        return None
    up_cost = (up_quote.notional + up_quote.fee) / quantity
    down_cost = (down_quote.notional + down_quote.fee) / quantity
    denominator = up_cost + down_cost
    if denominator <= ZERO:
        return None
    return up_cost / denominator


def _quote_buy(
    book: OrderBookSnapshot,
    *,
    quantity: Decimal,
    fees: ExecutionFeeSchedule,
) -> _BuyQuote | None:
    remaining = quantity
    notional = ZERO
    fee = ZERO
    for level in book.asks:
        if remaining <= ZERO:
            break
        filled = min(remaining, level.size)
        notional += filled * level.price
        fee += fees.fee(filled, level.price, maker=False)
        remaining -= filled
    if remaining > ZERO:
        return None
    return _BuyQuote(notional=notional, fee=fee)


def _candidate(
    *,
    action: PairAction,
    first_token_id: str,
    second_token_id: str,
    first_contract: TwapMarketContract,
    second_contract: TwapMarketContract,
    books: Mapping[str, OrderBookSnapshot],
    distribution: SharedTerminalTwap60Distribution,
    q_5_up: Decimal,
    q_15_up: Decimal,
    uncertainty_probability_pairs: Sequence[tuple[Decimal, Decimal]],
    config: V07StrategyConfig,
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
            or book.spread < ZERO
            or book.spread > config.maximum_spread_each_leg
            or not config.minimum_market_price
            <= book.best_ask
            <= config.maximum_market_price
        ):
            return None
    first_unit_fee = first_contract.fee_schedule.raw_fee(
        ONE,
        first_book.best_ask,
        maker=False,
    )
    second_unit_fee = second_contract.fee_schedule.raw_fee(
        ONE,
        second_book.best_ask,
        maker=False,
    )
    indicative_unit_cost = (
        first_book.best_ask + second_book.best_ask + first_unit_fee + second_unit_fee
    )
    if indicative_unit_cost <= ZERO:
        return None
    upper_quantity = (config.pair_risk_usdc / indicative_unit_cost).quantize(
        SIZE_QUANTUM,
        rounding=ROUND_DOWN,
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
        (minimum_quantity / SIZE_QUANTUM).to_integral_value(rounding=ROUND_CEILING)
    )
    maximum_units = int(upper_quantity / SIZE_QUANTUM)
    affordable: tuple[Decimal, _BuyQuote, _BuyQuote] | None = None
    while minimum_units <= maximum_units:
        middle_units = (minimum_units + maximum_units) // 2
        quantity = Decimal(middle_units) * SIZE_QUANTUM
        first_quote = _quote_buy(
            first_book,
            quantity=quantity,
            fees=first_contract.fee_schedule,
        )
        second_quote = _quote_buy(
            second_book,
            quantity=quantity,
            fees=second_contract.fee_schedule,
        )
        total = (
            first_quote.notional
            + first_quote.fee
            + second_quote.notional
            + second_quote.fee
            if first_quote is not None and second_quote is not None
            else None
        )
        if total is not None and total <= config.pair_risk_usdc:
            affordable = quantity, first_quote, second_quote
            minimum_units = middle_units + 1
        else:
            maximum_units = middle_units - 1
    if affordable is None:
        return None
    quantity, first_quote, second_quote = affordable
    notional_per_pair = (first_quote.notional + second_quote.notional) / quantity
    fee_per_pair = (first_quote.fee + second_quote.fee) / quantity
    cost_per_pair = notional_per_pair + fee_per_pair
    probabilities = distribution.probabilities_from_marginals(
        q_5_up=q_5_up,
        q_15_up=q_15_up,
    )
    payoff_by_outcome = (
        {
            "5up_15up": ONE,
            "5up_15down": ZERO,
            "5down_15up": TWO,
            "5down_15down": ONE,
        }
        if action is PairAction.LONG_15_UP_LONG_5_DOWN
        else {
            "5up_15up": ONE,
            "5up_15down": TWO,
            "5down_15up": ZERO,
            "5down_15down": ONE,
        }
    )
    mean_payoff = sum(
        probabilities[outcome] * payoff for outcome, payoff in payoff_by_outcome.items()
    )
    gross = mean_payoff - notional_per_pair
    net = mean_payoff - cost_per_pair
    conditional_net = []
    for conditional_q5, conditional_q15 in uncertainty_probability_pairs:
        conditional_probabilities = distribution.probabilities_from_marginals(
            q_5_up=conditional_q5,
            q_15_up=conditional_q15,
        )
        conditional_mean_payoff = sum(
            conditional_probabilities[outcome] * payoff
            for outcome, payoff in payoff_by_outcome.items()
        )
        conditional_net.append(conditional_mean_payoff - cost_per_pair)
    model_uncertainty_downside = (
        max(ZERO, net - min(conditional_net)) if conditional_net else ZERO
    )
    adjusted = net - config.uncertainty_multiplier * model_uncertainty_downside
    weighted_pnl = sorted(
        (payoff - cost_per_pair, probabilities[outcome])
        for outcome, payoff in payoff_by_outcome.items()
    )
    remaining = config.cvar_tail_probability
    tail_total = ZERO
    for pnl, probability in weighted_pnl:
        included = min(probability, remaining)
        tail_total += pnl * included
        remaining -= included
        if remaining == ZERO:
            break
    cvar = tail_total / config.cvar_tail_probability
    loss_probability = sum(
        probability for pnl, probability in weighted_pnl if pnl < ZERO
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
        model_uncertainty_downside_per_pair=model_uncertainty_downside,
        adjusted_pnl_per_pair=adjusted,
        cvar_per_pair=cvar,
        loss_probability=loss_probability,
    )


def _no_trade(
    *,
    distribution: SharedTerminalTwap60Distribution,
    decision_at_ms: int,
    reasons: Sequence[str],
    q_5_up: Decimal | None = None,
    q_15_up: Decimal | None = None,
) -> PairDecision:
    return PairDecision(
        action=PairAction.NO_TRADE,
        decision_at_ms=decision_at_ms,
        quantity=ZERO,
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
        q_5_calibrated=q_5_up,
        q_15_calibrated=q_15_up,
        reason_codes=tuple(reasons),
        qualification="development_shadow",
    )


def _base_reasons(
    *,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    distribution: SharedTerminalTwap60Distribution,
    books: Mapping[str, OrderBookSnapshot],
    health: SharedTerminalDataHealth,
    config: V07StrategyConfig,
    locked_oos: bool,
) -> list[str]:
    reasons: list[str] = []
    if pair.market_5.settlement_regime != V06_SETTLEMENT_REGIME_ID or (
        pair.market_15.settlement_regime != V06_SETTLEMENT_REGIME_ID
    ):
        reasons.append("shared_terminal_settlement_regime_required")
    if (
        pair.market_5.twap_window_seconds != 60
        or pair.market_15.twap_window_seconds != 60
    ):
        reasons.append("both_markets_must_use_terminal_twap_60")
    if pair.market_5.source_topic != pair.market_15.source_topic:
        reasons.append("paired_markets_do_not_share_one_terminal_source")
    if settlement_state.market_5_rule_hash != pair.market_5.rule_hash or (
        settlement_state.market_15_rule_hash != pair.market_15.rule_hash
    ):
        reasons.append("settlement_rule_hash_mismatch")
    if (
        settlement_state.market_5_open_timestamp_ms != pair.market_5.opens_at_ms
        or settlement_state.market_15_open_timestamp_ms != pair.market_15.opens_at_ms
    ):
        reasons.append("settlement_state_window_mismatch")
    if settlement_state.strike_5 != distribution.strike_5 or (
        settlement_state.strike_15 != distribution.strike_15
    ):
        reasons.append("settlement_state_strike_mismatch")
    tau_ms = pair.expires_at_ms - health.decision_at_ms
    if not config.tau_min_seconds * 1_000 <= tau_ms <= config.tau_max_seconds * 1_000:
        reasons.append("tau_outside_preregistered_window")
    observation_age = health.decision_at_ms - health.terminal_twap_60_observed_at_ms
    receipt_age = health.decision_at_ms - health.terminal_twap_60_received_at_ms
    if (
        observation_age < 0
        or observation_age > config.maximum_chainlink_staleness_ms
        or receipt_age < 0
        or receipt_age > config.maximum_chainlink_staleness_ms
        or health.terminal_twap_60_received_at_ms
        < health.terminal_twap_60_observed_at_ms - config.maximum_clock_drift_ms
    ):
        reasons.append("terminal_twap_60_stale_or_future")
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
    if locked_oos:
        shrinkage = health.shrinkage
        veto = health.validation_veto
        if shrinkage is None:
            reasons.append("train_only_shrinkage_missing")
        elif (
            shrinkage.fit_at_ms >= health.decision_at_ms
            or shrinkage.maximum_label_available_at_ms >= shrinkage.fit_at_ms
            or shrinkage.settlement_regime != V06_SETTLEMENT_REGIME_ID
            or shrinkage.settlement_model_id != SETTLEMENT_MODEL_ID
            or shrinkage.model_version != MODEL_VERSION
        ):
            reasons.append("train_only_shrinkage_invalid_or_leaky")
        if veto is None:
            reasons.append("validation_veto_missing")
        elif (
            not veto.passed
            or veto.evaluated_at_ms >= health.decision_at_ms
            or veto.maximum_label_available_at_ms >= veto.evaluated_at_ms
            or shrinkage is None
            or veto.shrinkage_artifact_hash != shrinkage.artifact_hash
        ):
            reasons.append("validation_veto_failed_or_leaky")
    return reasons


def decide_shared_terminal_pair_trade(
    *,
    pair: SameExpiryPair,
    settlement_state: PairSettlementState,
    distribution: SharedTerminalTwap60Distribution,
    books: Mapping[str, OrderBookSnapshot],
    health: SharedTerminalDataHealth,
    config: V07StrategyConfig = V07StrategyConfig(),
    locked_oos: bool = True,
) -> PairDecision:
    """Choose one non-promotional v0.7 paper action from executable asks."""

    reasons = _base_reasons(
        pair=pair,
        settlement_state=settlement_state,
        distribution=distribution,
        books=books,
        health=health,
        config=config,
        locked_oos=locked_oos,
    )
    market5 = executable_binary_ask_probability(
        books,
        up_token_id=pair.market_5.up_token_id,
        down_token_id=pair.market_5.down_token_id,
        target_quantity=config.market_baseline_quantity,
        fee_schedule=pair.market_5.fee_schedule,
    )
    market15 = executable_binary_ask_probability(
        books,
        up_token_id=pair.market_15.up_token_id,
        down_token_id=pair.market_15.down_token_id,
        target_quantity=config.market_baseline_quantity,
        fee_schedule=pair.market_15.fee_schedule,
    )
    if market5 is None or market15 is None:
        reasons.append("executable_market_probability_missing")
    if reasons:
        return _no_trade(
            distribution=distribution,
            decision_at_ms=health.decision_at_ms,
            reasons=(*reasons, "v07_counterfactual_non_promotional"),
        )
    assert market5 is not None and market15 is not None
    projected = False
    if locked_oos:
        assert health.shrinkage is not None
        q5, q15, projected = health.shrinkage.transform(
            distribution,
            market_q_5_up=market5,
            market_q_15_up=market15,
        )
        uncertainty_probability_pairs = tuple(
            health.shrinkage.transform_model_marginals(
                distribution,
                model_q_5_up=conditional_q5,
                model_q_15_up=conditional_q15,
                market_q_5_up=market5,
                market_q_15_up=market15,
            )[:2]
            for conditional_q5, conditional_q15 in (
                distribution.uncertainty_scale_marginals.values()
            )
        )
    else:
        q5, q15, projected = distribution.projected_marginals(
            q_5_up=distribution.q_5_up,
            q_15_up=distribution.q_15_up,
        )
        uncertainty_probability_pairs = tuple(
            distribution.projected_marginals(
                q_5_up=conditional_q5,
                q_15_up=conditional_q15,
            )[:2]
            for conditional_q5, conditional_q15 in (
                distribution.uncertainty_scale_marginals.values()
            )
        )
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
                q_5_up=q5,
                q_15_up=q15,
                uncertainty_probability_pairs=uncertainty_probability_pairs,
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
                q_5_up=q5,
                q_15_up=q15,
                uncertainty_probability_pairs=uncertainty_probability_pairs,
                config=config,
            ),
        )
        if item is not None
    )
    audit_reasons = ["v07_counterfactual_non_promotional"]
    if projected:
        audit_reasons.append("marginals_projected_to_shared_terminal_structure")
    if not candidates:
        return _no_trade(
            distribution=distribution,
            decision_at_ms=health.decision_at_ms,
            reasons=("no_candidate_has_healthy_executable_depth", *audit_reasons),
            q_5_up=q5,
            q_15_up=q15,
        )
    best = max(candidates, key=lambda item: item.adjusted_pnl_per_pair)
    action = best.action
    if best.adjusted_pnl_per_pair <= config.minimum_net_expected_pnl_per_pair:
        action = PairAction.NO_TRADE
        audit_reasons.insert(0, "net_edge_below_preregistered_threshold")
    return PairDecision(
        action=action,
        decision_at_ms=health.decision_at_ms,
        quantity=best.quantity if action is not PairAction.NO_TRADE else ZERO,
        first_token_id=best.first_token_id
        if action is not PairAction.NO_TRADE
        else None,
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
        q_5_calibrated=q5,
        q_15_calibrated=q15,
        reason_codes=tuple(audit_reasons),
        qualification="development_shadow",
    )


__all__ = [
    "CANONICAL_EXPIRY_CLUSTER_PREFIX",
    "CANONICAL_EVENT_CLUSTER_PREFIX",
    "CANONICAL_PAIR_ID_PREFIX",
    "MODEL_VERSION",
    "SETTLEMENT_MODEL_ID",
    "ProbabilityPairTrainingRow",
    "SharedTerminalDataHealth",
    "SharedTerminalModelConfig",
    "SharedTerminalSimulationResult",
    "SharedTerminalTwap60Distribution",
    "SharedTerminalTwap60Scenario",
    "StrikeOrdering",
    "TrainOnlyShrinkageArtifact",
    "V07ModelRejection",
    "V07StrategyConfig",
    "ValidationVetoEvidence",
    "canonical_expiry_cluster_id",
    "canonical_event_cluster_id",
    "decide_shared_terminal_pair_trade",
    "evaluate_validation_veto",
    "executable_binary_ask_probability",
    "raw_top_ask_probability",
    "simulate_shared_terminal_twap_60_distribution",
]
