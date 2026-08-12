"""Machine-recomputable evidence and deterministic report-core structures.

This module is the final offline seam between a strategy experiment and any
profitability claim.  It deliberately has no network, credential, order, or
filesystem-writing dependencies.

The design is intentionally conservative:

* every economic amount is a finite :class:`~decimal.Decimal`;
* binary floats are rejected rather than silently rounded;
* a trade's claimed PnL must exactly equal its explicit decomposition;
* validation bootstraps independently settled *events*, never individual fills;
* rewards are removed and a pessimistic cost is applied for the validation PnL;
* every failed or unavailable gate emits a deterministic failure reason; and
* ``validated_profitable`` is impossible unless every required gate passes.

The builders return JSON/CSV-ready in-memory data only.  Callers remain
responsible for deciding where artifacts are written.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence


ZERO = Decimal("0")
ONE = Decimal("1")
MAX_EVENT_PROFIT_CONTRIBUTION = Decimal("0.20")
MAX_ONE_LEG_CVAR_FRACTION = Decimal("0.20")
MINIMUM_INDEPENDENT_SETTLED_EVENTS = 100
MINIMUM_EXPLAINABLE_FILLS = 100
DEFAULT_BOOTSTRAP_SEED = 20_260_724
DEFAULT_BOOTSTRAP_RESAMPLES = 5_000
CALCULATION_PRECISION = 28
CALCULATION_CONTEXT = Context(
    prec=CALCULATION_PRECISION,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    capitals=1,
    clamp=0,
)
BACKTEST_RESULTS_SCHEMA = "polymarket-edge-lab/backtest-results-v1"
EXPERIMENT_SUMMARY_SCHEMA = "polymarket-edge-lab/experiment-summary-v1"


class DataLevel(str, Enum):
    """Evidence depth, from metadata-only to live shadow verification."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"

    @property
    def rank(self) -> int:
        return int(self.value[1:])


class EvidenceStatus(str, Enum):
    """The only classifications emitted by the evidence gate."""

    VALIDATED_PROFITABLE = "validated_profitable"
    PROMISING_NOT_VALIDATED = "promising_not_validated"
    INSUFFICIENT_DATA = "insufficient_data"
    REJECTED = "rejected"


class StrategyKind(str, Enum):
    """Validation family; reward strategies receive additional hard gates."""

    PREDICTIVE = "predictive"
    STRUCTURAL = "structural"
    REWARD = "reward"
    LATENCY = "latency"
    OTHER = "other"


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("artifact Decimal values must be finite")
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_ready(item)
            for key, item in sorted(
                value.items(), key=lambda pair: str(pair[0])
            )
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    raise TypeError(f"unsupported artifact value: {type(value).__name__}")


def _artifact_bytes(value: Any) -> bytes:
    return json.dumps(
        _json_ready(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _artifact_hash(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_artifact_bytes(value)).hexdigest()}"


def _decimal(value: Any, *, name: str, optional: bool = False) -> Optional[Decimal]:
    """Validate a financial scalar without admitting binary floats."""

    if value is None:
        if optional:
            return None
        raise TypeError(f"{name} must be Decimal, int, or decimal string")
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must be Decimal, int, or decimal string")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, str)):
        try:
            result = Decimal(value)
        except Exception as exc:  # pragma: no cover - Decimal exceptions vary
            raise ValueError(f"{name} must be a finite decimal") from exc
    else:
        raise TypeError(f"{name} must be Decimal, int, or decimal string")
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _required_decimal(value: Any, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    assert result is not None
    return result


def _optional_decimal(value: Any, *, name: str) -> Optional[Decimal]:
    return _decimal(value, name=name, optional=True)


def _optional_bool(value: Any, *, name: str) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(f"{name} must be bool or None")


def _nonempty(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_nonempty(value: Any, *, name: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty(value, name=name)


def _aware_iso8601(value: Any, *, name: str, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    text = _nonempty(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return text


def _decimal_string(value: Decimal) -> str:
    if not value.is_finite():  # defensive: construction already enforces this
        raise ValueError("cannot serialize a non-finite Decimal")
    return str(value)


def _optional_decimal_string(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else _decimal_string(value)


def _bool_text(value: Optional[bool]) -> str:
    if value is None:
        return ""
    return "true" if value else "false"


def _sum_decimal(values: Iterable[Decimal]) -> Decimal:
    """Add finite Decimals exactly, independent of the process context.

    Decimal's ordinary ``+`` is context-sensitive and can silently erase small
    PnL components when a caller has lowered global precision.  Aligning the
    base-ten integer coefficients avoids rounding entirely.
    """

    materialized = tuple(values)
    if not materialized:
        return ZERO
    exponents = [int(value.as_tuple().exponent) for value in materialized]
    common_exponent = min(exponents)
    total_coefficient = 0
    for value, exponent in zip(materialized, exponents):
        decimal_tuple = value.as_tuple()
        coefficient = 0
        for digit in decimal_tuple.digits:
            coefficient = coefficient * 10 + digit
        if decimal_tuple.sign:
            coefficient = -coefficient
        total_coefficient += coefficient * (10 ** (exponent - common_exponent))
    sign = 1 if total_coefficient < 0 else 0
    absolute = abs(total_coefficient)
    digits = (
        tuple(int(character) for character in str(absolute))
        if absolute
        else (0,)
    )
    return Decimal((sign, digits, common_exponent))


def _negated(value: Decimal) -> Decimal:
    return value.copy_negate()


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == ZERO:
        raise ZeroDivisionError("Decimal denominator cannot be zero")
    with localcontext(CALCULATION_CONTEXT):
        return numerator / denominator


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    """Multiply finite Decimals exactly, independent of the process context."""

    left_tuple = left.as_tuple()
    right_tuple = right.as_tuple()
    left_coefficient = 0
    for digit in left_tuple.digits:
        left_coefficient = left_coefficient * 10 + digit
    right_coefficient = 0
    for digit in right_tuple.digits:
        right_coefficient = right_coefficient * 10 + digit
    product = left_coefficient * right_coefficient
    sign = left_tuple.sign ^ right_tuple.sign
    digits = (
        tuple(int(character) for character in str(product))
        if product
        else (0,)
    )
    exponent = int(left_tuple.exponent) + int(right_tuple.exponent)
    return Decimal((sign, digits, exponent))


def _tri_state_all(values: Sequence[Optional[bool]]) -> Optional[bool]:
    """Return False on known failure, None on unknown, and True only if all true."""

    if not values:
        return None
    if any(value is False for value in values):
        return False
    if any(value is None for value in values):
        return None
    return True


def _settlement_provenance_consistency(
    trades: Sequence["TradeEvidence"],
) -> Optional[bool]:
    """Verify canonical settlement evidence is complete and non-conflicting."""

    settled = [trade for trade in trades if trade.settled is True]
    if not settled or any(
        trade.settlement_source_event_id is None for trade in settled
    ):
        return None
    event_sources: dict[str, str] = {}
    source_groups: dict[str, str] = {}
    for trade in settled:
        source_id = trade.settlement_source_event_id
        group_id = trade.independence_group
        assert source_id is not None
        if group_id is None:
            return None
        previous_source = event_sources.setdefault(trade.event_id, source_id)
        if previous_source != source_id:
            return False
        previous_group = source_groups.setdefault(source_id, group_id)
        if previous_group != group_id:
            return False
    return True


@dataclass(frozen=True, init=False)
class VerifiedEvidenceContext:
    """Content-bound proof that upstream data and accounting audits passed.

    The constructor is intentionally disabled.  The only supported creation
    path validates immutable raw-record IDs, disjoint OOS splits, execution
    decision coverage, a reconciled pessimistic ledger, and sourced fee terms.
    ``StrategyEvidence`` then checks every trade against this context.
    """

    capture_audit_hash: str
    raw_records_hash: str
    experiment_record_hash: str
    split_manifest_hash: str
    execution_trace_hash: str
    ledger_snapshot_hash: str
    fee_config_hash: str
    experiment_id: str
    raw_record_ids: frozenset[str]
    oos_event_ids: frozenset[str]
    decision_by_trade_id: Mapping[str, str]
    ledger_net_pnl_by_trade_id: Mapping[str, Decimal]
    experiment_trade_ids: frozenset[str]
    parameter_neighbor_reward_zero_pnls: Mapping[str, Decimal]

    def __init__(self) -> None:
        raise TypeError(
            "use VerifiedEvidenceContext.verify() with audited artifacts"
        )

    @classmethod
    def verify(
        cls,
        *,
        capture_audit: Mapping[str, Any],
        raw_records: Sequence[Mapping[str, Any]],
        experiment_record: Mapping[str, Any],
        split_manifest: Mapping[str, Sequence[str]],
        execution_trace: Sequence[Mapping[str, Any]],
        ledger_snapshot: Mapping[str, Any],
        fee_config: Mapping[str, Any],
    ) -> "VerifiedEvidenceContext":
        if not isinstance(capture_audit, Mapping):
            raise TypeError("capture_audit must be a mapping")
        expected_audit_keys = {
            "orphan_partials",
            "raw_without_manifest",
            "manifest_without_raw",
            "checksum_mismatches",
            "invalid_manifests",
        }
        if not expected_audit_keys <= set(capture_audit):
            raise ValueError("capture_audit is missing integrity categories")
        if any(capture_audit[key] for key in expected_audit_keys):
            raise ValueError("capture_audit contains integrity failures")

        from .data_store import canonical_record_id

        if isinstance(raw_records, (str, bytes)) or not raw_records:
            raise ValueError("raw_records must contain verified raw envelopes")
        raw_ids: set[str] = set()
        materialized_raw: list[Mapping[str, Any]] = []
        for raw_record in raw_records:
            if not isinstance(raw_record, Mapping):
                raise TypeError("raw_records must contain mappings")
            record_id = _nonempty(
                raw_record.get("record_id"), name="raw record_id"
            )
            if canonical_record_id(raw_record) != record_id:
                raise ValueError(
                    f"raw record checksum mismatch: {record_id}"
                )
            if record_id in raw_ids:
                raise ValueError(f"duplicate raw record_id: {record_id}")
            raw_ids.add(record_id)
            materialized_raw.append(raw_record)

        if not isinstance(experiment_record, Mapping):
            raise TypeError("experiment_record must be a mapping")
        experiment_id = _nonempty(
            experiment_record.get("experiment_id"),
            name="experiment_record.experiment_id",
        )
        experiment_trade_ids_raw = experiment_record.get("trade_ids")
        if isinstance(experiment_trade_ids_raw, (str, bytes)) or not isinstance(
            experiment_trade_ids_raw, Sequence
        ):
            raise ValueError("experiment_record.trade_ids must be a sequence")
        experiment_trade_ids = frozenset(
            _nonempty(str(value), name="experiment trade_id")
            for value in experiment_trade_ids_raw
        )
        if len(experiment_trade_ids) != len(experiment_trade_ids_raw):
            raise ValueError("experiment_record.trade_ids contains duplicates")
        raw_neighbors = experiment_record.get(
            "parameter_neighbor_reward_zero_pnls"
        )
        if not isinstance(raw_neighbors, Mapping):
            raise ValueError(
                "experiment record is missing parameter-neighbor results"
            )
        neighbors: dict[str, Decimal] = {}
        for raw_name, raw_value in sorted(
            raw_neighbors.items(), key=lambda item: str(item[0])
        ):
            name = _nonempty(str(raw_name), name="parameter neighbor")
            neighbors[name] = _required_decimal(
                raw_value, name=f"experiment neighbor {name}"
            )

        required_splits = ("train", "validation", "test")
        if not isinstance(split_manifest, Mapping) or any(
            name not in split_manifest for name in required_splits
        ):
            raise ValueError(
                "split_manifest requires train, validation, and test"
            )
        split_sets: dict[str, set[str]] = {}
        for split_name in required_splits:
            raw_ids_for_split = split_manifest[split_name]
            if isinstance(raw_ids_for_split, (str, bytes)):
                raise TypeError(f"{split_name} split must be a sequence")
            values = [
                _nonempty(str(value), name=f"{split_name} event ID")
                for value in raw_ids_for_split
            ]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate event in {split_name} split")
            split_sets[split_name] = set(values)
        if (
            split_sets["train"] & split_sets["validation"]
            or split_sets["train"] & split_sets["test"]
            or split_sets["validation"] & split_sets["test"]
        ):
            raise ValueError("split_manifest contains event leakage")

        if isinstance(execution_trace, (str, bytes)) or not execution_trace:
            raise ValueError("execution_trace must contain decisions")
        decision_by_trade: dict[str, str] = {}
        materialized_trace: list[Mapping[str, Any]] = []
        for entry in execution_trace:
            if not isinstance(entry, Mapping):
                raise TypeError("execution_trace entries must be mappings")
            trade_id = _nonempty(
                entry.get("trade_id"), name="execution trace trade_id"
            )
            decision_id = _nonempty(
                entry.get("decision_id"), name="execution trace decision_id"
            )
            previous = decision_by_trade.setdefault(trade_id, decision_id)
            if previous != decision_id:
                raise ValueError(
                    f"trade has conflicting decisions: {trade_id}"
                )
            materialized_trace.append(entry)

        if not isinstance(ledger_snapshot, Mapping):
            raise TypeError("ledger_snapshot must be a mapping")
        for required_flag in (
            "reconciled",
            "pessimistic_execution",
            "queue_bounded",
            "all_costs_included",
        ):
            if ledger_snapshot.get(required_flag) is not True:
                raise ValueError(
                    f"ledger_snapshot requires {required_flag}=true"
                )
        raw_ledger_pnls = ledger_snapshot.get("trade_net_pnls")
        if not isinstance(raw_ledger_pnls, Mapping):
            raise ValueError(
                "ledger_snapshot.trade_net_pnls must be a mapping"
            )
        ledger_pnls = {
            _nonempty(str(trade_id), name="ledger trade_id"): (
                _required_decimal(value, name=f"ledger PnL {trade_id}")
            )
            for trade_id, value in raw_ledger_pnls.items()
        }
        if set(ledger_pnls) != set(decision_by_trade):
            raise ValueError(
                "ledger and execution trace trade identities differ"
            )

        if not isinstance(fee_config, Mapping):
            raise TypeError("fee_config must be a mapping")
        if fee_config.get("verified") is not True:
            raise ValueError("fee_config must be verified")
        fee_sources = fee_config.get("source_record_ids")
        if isinstance(fee_sources, (str, bytes)) or not fee_sources:
            raise ValueError("fee_config requires source_record_ids")
        normalized_fee_sources = {
            _nonempty(str(value), name="fee source record ID")
            for value in fee_sources
        }
        if not normalized_fee_sources <= raw_ids:
            raise ValueError("fee_config references unknown raw records")

        instance = object.__new__(cls)
        attributes: dict[str, Any] = {
            "capture_audit_hash": _artifact_hash(capture_audit),
            "raw_records_hash": _artifact_hash(materialized_raw),
            "experiment_record_hash": _artifact_hash(experiment_record),
            "split_manifest_hash": _artifact_hash(split_manifest),
            "execution_trace_hash": _artifact_hash(materialized_trace),
            "ledger_snapshot_hash": _artifact_hash(ledger_snapshot),
            "fee_config_hash": _artifact_hash(fee_config),
            "experiment_id": experiment_id,
            "raw_record_ids": frozenset(raw_ids),
            "oos_event_ids": frozenset(split_sets["test"]),
            "decision_by_trade_id": MappingProxyType(decision_by_trade),
            "ledger_net_pnl_by_trade_id": MappingProxyType(ledger_pnls),
            "experiment_trade_ids": experiment_trade_ids,
            "parameter_neighbor_reward_zero_pnls": MappingProxyType(neighbors),
        }
        for name, value in attributes.items():
            object.__setattr__(instance, name, value)
        return instance

    def coverage_failures(
        self,
        *,
        experiment_id: str,
        trades: Sequence["TradeEvidence"],
        parameter_neighbors: Optional[Mapping[str, Decimal]],
        reward_scoring_evidence_ids: Sequence[str],
        reward_payout_epoch_sources: Mapping[str, str],
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if experiment_id != self.experiment_id:
            failures.append("experiment_id_not_bound")
        trade_ids = {trade.trade_id for trade in trades}
        if trade_ids != set(self.experiment_trade_ids):
            failures.append("experiment_trade_ids_mismatch")
        if any(trade.event_id not in self.oos_event_ids for trade in trades):
            failures.append("trade_outside_oos_split")
        for trade in trades:
            raw_refs = (
                trade.source_event_id,
                trade.settlement_source_event_id,
                trade.reward_theoretical_source_event_id,
                trade.reward_scoring_source_event_id,
                trade.reward_payout_source_event_id,
            )
            if any(
                reference is not None
                and reference not in self.raw_record_ids
                for reference in raw_refs
            ):
                failures.append("unknown_raw_source_reference")
                break
        if any(
            self.decision_by_trade_id.get(trade.trade_id)
            != trade.decision_id
            for trade in trades
        ):
            failures.append("decision_trace_mismatch")
        if any(
            self.ledger_net_pnl_by_trade_id.get(trade.trade_id)
            != trade.recomputed_net_pnl
            for trade in trades
        ):
            failures.append("ledger_pnl_mismatch")
        if any(
            source_id not in self.raw_record_ids
            for source_id in reward_scoring_evidence_ids
        ):
            failures.append("unknown_reward_scoring_source")
        if any(
            source_id not in self.raw_record_ids
            for source_id in reward_payout_epoch_sources.values()
        ):
            failures.append("unknown_reward_payout_source")
        normalized_neighbors = (
            None if parameter_neighbors is None else dict(parameter_neighbors)
        )
        if normalized_neighbors != dict(
            self.parameter_neighbor_reward_zero_pnls
        ):
            failures.append("parameter_neighbors_not_registry_bound")
        return tuple(sorted(set(failures)))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "capture_audit_hash": self.capture_audit_hash,
            "raw_records_hash": self.raw_records_hash,
            "experiment_record_hash": self.experiment_record_hash,
            "split_manifest_hash": self.split_manifest_hash,
            "execution_trace_hash": self.execution_trace_hash,
            "ledger_snapshot_hash": self.ledger_snapshot_hash,
            "fee_config_hash": self.fee_config_hash,
            "experiment_id": self.experiment_id,
            "raw_record_count": len(self.raw_record_ids),
            "oos_event_count": len(self.oos_event_ids),
            "execution_trade_count": len(self.decision_by_trade_id),
        }


@dataclass(frozen=True)
class TradeEvidence:
    """One economically complete, provenance-linked fill or conversion.

    Fees, the three reward evidence levels, other costs, and pessimistic costs
    are non-negative magnitudes.  Spread, forecast, adverse-selection, and
    hedge fields are signed PnL components.  Only confirmed reward payout is
    realized:

    ``net = spread + forecast + adverse + hedge + confirmed_payout - costs``

    The pessimistic validation scenario additionally removes all rewards and
    subtracts ``pessimistic_cost``.
    """

    trade_id: str
    event_id: str
    independence_group: Optional[str]
    market_id: str
    source_event_id: Optional[str]
    settlement_source_event_id: Optional[str]
    decision_id: Optional[str]
    one_leg_episode_id: Optional[str]
    filled_at: str
    settled_at: Optional[str]
    settled: Optional[bool]
    is_real: Optional[bool]
    is_oos: Optional[bool]
    leakage_free: Optional[bool]
    all_costs_included: Optional[bool]
    fill_explainable: Optional[bool]
    notional: Decimal
    capacity_notional: Decimal
    spread_pnl: Decimal
    forecast_pnl: Decimal
    adverse_selection_pnl: Decimal
    hedge_pnl: Decimal
    fees: Decimal
    theoretical_rewards: Decimal
    scoring_rewards: Decimal
    confirmed_reward_payout: Decimal
    reward_theoretical_source_event_id: Optional[str]
    reward_scoring_source_event_id: Optional[str]
    reward_payout_epoch_id: Optional[str]
    reward_payout_source_event_id: Optional[str]
    other_costs: Decimal
    pessimistic_cost: Decimal
    claimed_net_pnl: Optional[Decimal]
    one_leg_pnl: Optional[Decimal]

    def __post_init__(self) -> None:
        for name in ("trade_id", "event_id", "market_id"):
            object.__setattr__(
                self, name, _nonempty(getattr(self, name), name=name)
            )
        object.__setattr__(
            self,
            "independence_group",
            _optional_nonempty(
                self.independence_group, name="independence_group"
            ),
        )
        object.__setattr__(
            self,
            "source_event_id",
            _optional_nonempty(self.source_event_id, name="source_event_id"),
        )
        object.__setattr__(
            self,
            "settlement_source_event_id",
            _optional_nonempty(
                self.settlement_source_event_id,
                name="settlement_source_event_id",
            ),
        )
        object.__setattr__(
            self,
            "decision_id",
            _optional_nonempty(self.decision_id, name="decision_id"),
        )
        object.__setattr__(
            self,
            "one_leg_episode_id",
            _optional_nonempty(
                self.one_leg_episode_id, name="one_leg_episode_id"
            ),
        )
        object.__setattr__(
            self,
            "filled_at",
            _aware_iso8601(self.filled_at, name="filled_at"),
        )
        object.__setattr__(
            self,
            "settled_at",
            _aware_iso8601(
                self.settled_at, name="settled_at", optional=True
            ),
        )
        for name in (
            "settled",
            "is_real",
            "is_oos",
            "leakage_free",
            "all_costs_included",
            "fill_explainable",
        ):
            object.__setattr__(
                self,
                name,
                _optional_bool(getattr(self, name), name=name),
            )
        for name in (
            "notional",
            "capacity_notional",
            "spread_pnl",
            "forecast_pnl",
            "adverse_selection_pnl",
            "hedge_pnl",
            "fees",
            "other_costs",
            "pessimistic_cost",
        ):
            object.__setattr__(
                self,
                name,
                _required_decimal(getattr(self, name), name=name),
            )
        for name in (
            "theoretical_rewards",
            "scoring_rewards",
            "confirmed_reward_payout",
        ):
            object.__setattr__(
                self,
                name,
                _required_decimal(getattr(self, name), name=name),
            )
        for name in (
            "reward_theoretical_source_event_id",
            "reward_scoring_source_event_id",
            "reward_payout_epoch_id",
            "reward_payout_source_event_id",
        ):
            object.__setattr__(
                self,
                name,
                _optional_nonempty(getattr(self, name), name=name),
            )
        for name in ("claimed_net_pnl", "one_leg_pnl"):
            object.__setattr__(
                self,
                name,
                _optional_decimal(getattr(self, name), name=name),
            )

        if self.notional <= ZERO:
            raise ValueError("notional must be positive")
        if self.capacity_notional < ZERO:
            raise ValueError("capacity_notional cannot be negative")
        if self.capacity_notional < self.notional:
            raise ValueError("capacity_notional cannot be below filled notional")
        for name in (
            "fees",
            "theoretical_rewards",
            "scoring_rewards",
            "confirmed_reward_payout",
            "other_costs",
            "pessimistic_cost",
        ):
            if getattr(self, name) < ZERO:
                raise ValueError(f"{name} cannot be negative")
        if self.theoretical_rewards > ZERO and (
            self.reward_theoretical_source_event_id is None
        ):
            raise ValueError(
                "positive theoretical_rewards require source provenance"
            )
        if self.scoring_rewards > ZERO and (
            self.reward_scoring_source_event_id is None
        ):
            raise ValueError("positive scoring_rewards require source provenance")
        if self.confirmed_reward_payout > ZERO and (
            self.reward_payout_epoch_id is None
            or self.reward_payout_source_event_id is None
        ):
            raise ValueError(
                "confirmed_reward_payout requires epoch and source provenance"
            )
        if self.settled is True and self.settled_at is None:
            raise ValueError("settled trades require settled_at")
        if self.settled is False and self.settled_at is not None:
            raise ValueError("unsettled trades cannot have settled_at")
        if self.settled_at is not None:
            filled_instant = datetime.fromisoformat(
                self.filled_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            settled_instant = datetime.fromisoformat(
                self.settled_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if settled_instant < filled_instant:
                raise ValueError("settled_at cannot precede filled_at")
        if self.fill_explainable is True and (
            self.source_event_id is None or self.decision_id is None
        ):
            raise ValueError(
                "explainable fills require source_event_id and decision_id"
            )

    @property
    def recomputed_net_pnl(self) -> Decimal:
        return _sum_decimal(
            (
                self.spread_pnl,
                self.forecast_pnl,
                self.adverse_selection_pnl,
                self.hedge_pnl,
                self.confirmed_reward_payout,
                _negated(self.fees),
                _negated(self.other_costs),
            )
        )

    @property
    def pessimistic_reward_zero_pnl(self) -> Decimal:
        return _sum_decimal(
            (
                self.recomputed_net_pnl,
                _negated(self.confirmed_reward_payout),
                _negated(self.pessimistic_cost),
            )
        )

    @property
    def pnl_recomputable(self) -> bool:
        return (
            self.claimed_net_pnl is not None
            and self.claimed_net_pnl == self.recomputed_net_pnl
        )

    @property
    def is_explainable_fill(self) -> bool:
        return (
            self.fill_explainable is True
            and self.source_event_id is not None
            and self.decision_id is not None
        )

    def to_mapping(self) -> dict[str, Any]:
        """Return deterministic JSON-ready content with Decimal strings."""

        return {
            "trade_id": self.trade_id,
            "event_id": self.event_id,
            "independence_group": self.independence_group,
            "market_id": self.market_id,
            "source_event_id": self.source_event_id,
            "settlement_source_event_id": (
                self.settlement_source_event_id
            ),
            "decision_id": self.decision_id,
            "one_leg_episode_id": self.one_leg_episode_id,
            "filled_at": self.filled_at,
            "settled_at": self.settled_at,
            "settled": self.settled,
            "is_real": self.is_real,
            "is_oos": self.is_oos,
            "leakage_free": self.leakage_free,
            "all_costs_included": self.all_costs_included,
            "fill_explainable": self.fill_explainable,
            "notional": _decimal_string(self.notional),
            "capacity_notional": _decimal_string(self.capacity_notional),
            "spread_pnl": _decimal_string(self.spread_pnl),
            "forecast_pnl": _decimal_string(self.forecast_pnl),
            "adverse_selection_pnl": _decimal_string(
                self.adverse_selection_pnl
            ),
            "hedge_pnl": _decimal_string(self.hedge_pnl),
            "fees": _decimal_string(self.fees),
            "theoretical_rewards": _decimal_string(
                self.theoretical_rewards
            ),
            "scoring_rewards": _decimal_string(self.scoring_rewards),
            "confirmed_reward_payout": _decimal_string(
                self.confirmed_reward_payout
            ),
            "reward_theoretical_source_event_id": (
                self.reward_theoretical_source_event_id
            ),
            "reward_scoring_source_event_id": (
                self.reward_scoring_source_event_id
            ),
            "reward_payout_epoch_id": self.reward_payout_epoch_id,
            "reward_payout_source_event_id": (
                self.reward_payout_source_event_id
            ),
            "other_costs": _decimal_string(self.other_costs),
            "pessimistic_cost": _decimal_string(self.pessimistic_cost),
            "claimed_net_pnl": _optional_decimal_string(
                self.claimed_net_pnl
            ),
            "recomputed_net_pnl": _decimal_string(
                self.recomputed_net_pnl
            ),
            "pessimistic_reward_zero_pnl": _decimal_string(
                self.pessimistic_reward_zero_pnl
            ),
            "one_leg_pnl": _optional_decimal_string(self.one_leg_pnl),
            "pnl_recomputable": self.pnl_recomputable,
        }


@dataclass(frozen=True)
class StrategyMetrics:
    """All report metrics recomputed from :class:`TradeEvidence`."""

    initial_capital: Decimal
    final_capital: Decimal
    marked_final_capital: Decimal
    net_pnl: Decimal
    unsettled_marked_pnl: Decimal
    pessimistic_reward_zero_net_pnl: Decimal
    return_on_initial_capital: Decimal
    max_drawdown: Decimal
    max_drawdown_fraction: Decimal
    pessimistic_max_drawdown: Decimal
    pessimistic_max_drawdown_fraction: Decimal
    hit_rate: Optional[Decimal]
    turnover: Decimal
    capacity: Optional[Decimal]
    one_leg_empirical_cvar_95: Optional[Decimal]
    one_leg_tail_loss_total: Optional[Decimal]
    one_leg_tail_episode_count: int
    independent_settled_events: int
    explainable_fills: int
    bootstrap_95_lower: Optional[Decimal]
    bootstrap_95_upper: Optional[Decimal]
    max_event_profit_contribution: Optional[Decimal]
    max_event_positive_pnl: Optional[Decimal]
    total_positive_event_pnl: Optional[Decimal]
    max_event_profit_contribution_acceptable: Optional[bool]
    independence_labels_complete: Optional[bool]
    settlement_status_complete: Optional[bool]
    settlement_provenance_consistent: Optional[bool]
    all_trades_settled: Optional[bool]
    real_data: Optional[bool]
    out_of_sample: Optional[bool]
    no_leakage: Optional[bool]
    all_costs_included: Optional[bool]
    parameter_neighborhood_stable: Optional[bool]
    parameter_neighbor_count: int
    one_leg_cvar_limit_conservative: Optional[bool]
    one_leg_tail_risk_acceptable: Optional[bool]
    pnl_recomputable: Optional[bool]
    decomposition: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decomposition",
            MappingProxyType(dict(self.decomposition)),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "initial_capital": _decimal_string(self.initial_capital),
            "final_capital": _decimal_string(self.final_capital),
            "marked_final_capital": _decimal_string(
                self.marked_final_capital
            ),
            "net_pnl": _decimal_string(self.net_pnl),
            "unsettled_marked_pnl": _decimal_string(
                self.unsettled_marked_pnl
            ),
            "pessimistic_reward_zero_net_pnl": _decimal_string(
                self.pessimistic_reward_zero_net_pnl
            ),
            "return_on_initial_capital": _decimal_string(
                self.return_on_initial_capital
            ),
            "max_drawdown": _decimal_string(self.max_drawdown),
            "max_drawdown_fraction": _decimal_string(
                self.max_drawdown_fraction
            ),
            "max_drawdown_definition": (
                "realized settled-PnL curve ordered by settlement timestamp; "
                "not an intraperiod mark-to-market estimate"
            ),
            "pessimistic_max_drawdown": _decimal_string(
                self.pessimistic_max_drawdown
            ),
            "pessimistic_max_drawdown_fraction": _decimal_string(
                self.pessimistic_max_drawdown_fraction
            ),
            "hit_rate": _optional_decimal_string(self.hit_rate),
            "hit_rate_definition": (
                "profitable independent settled-event-group net PnL divided "
                "by independent settled-event-group count"
            ),
            "turnover": _decimal_string(self.turnover),
            "turnover_definition": "sum of filled trade notional",
            "capacity": _optional_decimal_string(self.capacity),
            "capacity_definition": (
                "minimum observed pessimistic executable notional per trade"
            ),
            "one_leg_empirical_cvar_95": _optional_decimal_string(
                self.one_leg_empirical_cvar_95
            ),
            "one_leg_tail_loss_total": _optional_decimal_string(
                self.one_leg_tail_loss_total
            ),
            "one_leg_tail_episode_count": self.one_leg_tail_episode_count,
            "one_leg_cvar_definition": (
                "positive loss magnitude of the worst ceil(5%) one-leg PnLs"
            ),
            "independent_settled_events": self.independent_settled_events,
            "explainable_fills": self.explainable_fills,
            "bootstrap_95_lower": _optional_decimal_string(
                self.bootstrap_95_lower
            ),
            "bootstrap_95_upper": _optional_decimal_string(
                self.bootstrap_95_upper
            ),
            "bootstrap_unit": (
                "mean pessimistic reward-zero PnL per independently "
                "settled event"
            ),
            "max_event_profit_contribution": _optional_decimal_string(
                self.max_event_profit_contribution
            ),
            "max_event_positive_pnl": _optional_decimal_string(
                self.max_event_positive_pnl
            ),
            "total_positive_event_pnl": _optional_decimal_string(
                self.total_positive_event_pnl
            ),
            "max_event_profit_contribution_acceptable": (
                self.max_event_profit_contribution_acceptable
            ),
            "independence_labels_complete": (
                self.independence_labels_complete
            ),
            "settlement_status_complete": self.settlement_status_complete,
            "settlement_provenance_consistent": (
                self.settlement_provenance_consistent
            ),
            "all_trades_settled": self.all_trades_settled,
            "real_data": self.real_data,
            "out_of_sample": self.out_of_sample,
            "no_leakage": self.no_leakage,
            "all_costs_included": self.all_costs_included,
            "parameter_neighborhood_stable": (
                self.parameter_neighborhood_stable
            ),
            "parameter_neighbor_count": self.parameter_neighbor_count,
            "one_leg_cvar_limit_conservative": (
                self.one_leg_cvar_limit_conservative
            ),
            "one_leg_tail_risk_acceptable": (
                self.one_leg_tail_risk_acceptable
            ),
            "pnl_recomputable": self.pnl_recomputable,
            "decomposition": {
                key: _decimal_string(value)
                for key, value in sorted(self.decomposition.items())
            },
        }


@dataclass(frozen=True)
class GateEvaluation:
    name: str
    passed: bool
    observed: str
    failure_reason: Optional[str]
    failure_kind: Optional[str]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "observed": self.observed,
            "failure_reason": self.failure_reason,
            "failure_kind": self.failure_kind,
        }


@dataclass(frozen=True)
class ValidationResult:
    status: EvidenceStatus
    gates: tuple[GateEvaluation, ...]

    @property
    def passed_gates(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self.gates if gate.passed)

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(gate.name for gate in self.gates if not gate.passed)

    @property
    def failure_reasons(self) -> tuple[str, ...]:
        return tuple(
            gate.failure_reason
            for gate in self.gates
            if not gate.passed and gate.failure_reason is not None
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "passed_gates": list(self.passed_gates),
            "failed_gates": list(self.failed_gates),
            "failure_reasons": list(self.failure_reasons),
            "gates": [gate.to_mapping() for gate in self.gates],
        }


def _trade_order_key(
    trade: TradeEvidence,
) -> tuple[datetime, str, str]:
    instant = datetime.fromisoformat(
        trade.filled_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    return (instant, trade.event_id, trade.trade_id)


def _aggregate_independent_event_pnl(
    trades: Sequence[TradeEvidence],
    *,
    pessimistic_reward_zero: bool,
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for trade in trades:
        if trade.settled is not True:
            continue
        key = trade.independence_group
        if key is None:
            continue
        value = (
            trade.pessimistic_reward_zero_pnl
            if pessimistic_reward_zero
            else trade.recomputed_net_pnl
        )
        result[key] = _sum_decimal((result.get(key, ZERO), value))
    return result


def _percentile(samples: Sequence[Decimal], probability: Decimal) -> Decimal:
    """Deterministic linear percentile over already materialized samples."""

    if not samples:
        raise ValueError("percentile requires at least one sample")
    if not ZERO <= probability <= ONE:
        raise ValueError("probability must be between zero and one")
    ordered = sorted(samples)
    if len(ordered) == 1:
        return ordered[0]
    position = _multiply(probability, Decimal(len(ordered) - 1))
    lower_index = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = _sum_decimal((position, _negated(Decimal(lower_index))))
    interval = _sum_decimal(
        (ordered[upper_index], _negated(ordered[lower_index]))
    )
    return _sum_decimal(
        (ordered[lower_index], _multiply(interval, weight))
    )


def _event_bootstrap_ci(
    event_pnls: Mapping[str, Decimal],
    *,
    seed: int,
    n_resamples: int,
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    if not event_pnls:
        return None, None
    # Bootstrap draws depend only on the empirical distribution, never on
    # arbitrary event-label assignment.
    values = sorted(event_pnls.values())
    if values[0] == values[-1]:
        return values[0], values[0]
    count = len(values)
    denominator = Decimal(count)
    rng = random.Random(seed)
    samples: list[Decimal] = []
    for _ in range(n_resamples):
        total = _sum_decimal(values[rng.randrange(count)] for _ in range(count))
        samples.append(_divide(total, denominator))
    return (
        _percentile(samples, Decimal("0.025")),
        _percentile(samples, Decimal("0.975")),
    )


def _empirical_one_leg_cvar_95(
    trades: Sequence[TradeEvidence],
) -> tuple[Optional[Decimal], Optional[Decimal], int]:
    settled = [trade for trade in trades if trade.settled is True]
    if not settled or any(
        trade.one_leg_pnl is None
        or trade.independence_group is None
        or trade.one_leg_episode_id is None
        for trade in settled
    ):
        return None, None, 0
    episode_pnls: dict[str, Decimal] = {}
    episode_groups: dict[str, str] = {}
    for trade in settled:
        assert trade.independence_group is not None
        assert trade.one_leg_episode_id is not None
        assert trade.one_leg_pnl is not None
        previous_group = episode_groups.setdefault(
            trade.one_leg_episode_id, trade.independence_group
        )
        if previous_group != trade.independence_group:
            return None, None, 0
        episode_pnls[trade.one_leg_episode_id] = _sum_decimal(
            (
                episode_pnls.get(trade.one_leg_episode_id, ZERO),
                trade.one_leg_pnl,
            )
        )
    pnls = sorted(episode_pnls.values())
    tail_count = max(1, (len(pnls) * 5 + 99) // 100)
    tail_total = _sum_decimal(pnls[:tail_count])
    tail_loss_total = max(ZERO, _negated(tail_total))
    return (
        _divide(tail_loss_total, Decimal(tail_count)),
        tail_loss_total,
        tail_count,
    )


def _settlement_order_key(
    trade: TradeEvidence,
) -> tuple[datetime, str, str]:
    assert trade.settled_at is not None
    instant = datetime.fromisoformat(
        trade.settled_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    return (instant, trade.event_id, trade.trade_id)


def _max_drawdown(
    initial_capital: Decimal,
    trades: Sequence[TradeEvidence],
    *,
    pessimistic_reward_zero: bool = False,
) -> tuple[Decimal, Decimal]:
    equity = initial_capital
    peak = initial_capital
    maximum = ZERO
    maximum_fraction = ZERO
    settled = [trade for trade in trades if trade.settled is True]
    for trade in sorted(settled, key=_settlement_order_key):
        pnl = (
            trade.pessimistic_reward_zero_pnl
            if pessimistic_reward_zero
            else trade.recomputed_net_pnl
        )
        equity = _sum_decimal((equity, pnl))
        if equity > peak:
            peak = equity
            continue
        drawdown = _sum_decimal((peak, _negated(equity)))
        fraction = _divide(drawdown, peak) if peak > ZERO else ZERO
        if drawdown > maximum:
            maximum = drawdown
        if fraction > maximum_fraction:
            maximum_fraction = fraction
    return maximum, maximum_fraction


def _parameter_stability(
    neighbors: Optional[Mapping[str, Decimal]],
) -> tuple[Optional[bool], int]:
    if neighbors is None or len(neighbors) < 3:
        return None, 0 if neighbors is None else len(neighbors)
    positive = sum(value > ZERO for value in neighbors.values())
    return positive * 3 >= len(neighbors) * 2, len(neighbors)


def _event_contribution_metrics(
    event_pnls: Mapping[str, Decimal],
) -> tuple[
    Optional[Decimal],
    Optional[bool],
    Optional[Decimal],
    Optional[Decimal],
]:
    if not event_pnls:
        return None, None, None, None
    positive = [value for value in event_pnls.values() if value > ZERO]
    total_positive = _sum_decimal(positive)
    if not positive or total_positive <= ZERO:
        # With observed events but no positive contribution, concentration is
        # mathematically zero.  Profitability fails at the net-PnL gate rather
        # than being mislabeled as missing concentration data.
        return ZERO, True, ZERO, ZERO
    maximum = max(positive)
    return (
        _divide(maximum, total_positive),
        maximum
        <= _multiply(
            total_positive, MAX_EVENT_PROFIT_CONTRIBUTION
        ),
        maximum,
        total_positive,
    )


def _compute_metrics(
    *,
    initial_capital: Decimal,
    trades: Sequence[TradeEvidence],
    parameter_neighbors: Optional[Mapping[str, Decimal]],
    one_leg_cvar_limit: Optional[Decimal],
    bootstrap_seed: int,
    bootstrap_resamples: int,
) -> StrategyMetrics:
    settled_trades = tuple(trade for trade in trades if trade.settled is True)
    unsettled_trades = tuple(
        trade for trade in trades if trade.settled is not True
    )
    net_pnl = _sum_decimal(
        trade.recomputed_net_pnl for trade in settled_trades
    )
    unsettled_marked_pnl = _sum_decimal(
        trade.recomputed_net_pnl for trade in unsettled_trades
    )
    pessimistic_net = _sum_decimal(
        trade.pessimistic_reward_zero_pnl
        for trade in settled_trades
    )
    final_capital = _sum_decimal((initial_capital, net_pnl))
    marked_final_capital = _sum_decimal(
        (final_capital, unsettled_marked_pnl)
    )
    max_drawdown, max_drawdown_fraction = _max_drawdown(
        initial_capital, trades
    )
    (
        pessimistic_max_drawdown,
        pessimistic_max_drawdown_fraction,
    ) = _max_drawdown(
        initial_capital,
        trades,
        pessimistic_reward_zero=True,
    )
    independent_settled_pnls = _aggregate_independent_event_pnl(
        trades,
        pessimistic_reward_zero=False,
    )
    independent_pessimistic_pnls = _aggregate_independent_event_pnl(
        trades,
        pessimistic_reward_zero=True,
    )
    bootstrap_lower, bootstrap_upper = _event_bootstrap_ci(
        independent_pessimistic_pnls,
        seed=bootstrap_seed,
        n_resamples=bootstrap_resamples,
    )
    profitable_events = sum(
        value > ZERO for value in independent_settled_pnls.values()
    )
    hit_rate = (
        _divide(
            Decimal(profitable_events),
            Decimal(len(independent_settled_pnls)),
        )
        if independent_settled_pnls
        else None
    )
    cvar, cvar_tail_loss, cvar_tail_count = (
        _empirical_one_leg_cvar_95(trades)
    )
    (
        event_contribution,
        event_contribution_acceptable,
        max_event_positive_pnl,
        total_positive_event_pnl,
    ) = _event_contribution_metrics(independent_pessimistic_pnls)
    parameter_stable, neighbor_count = _parameter_stability(
        parameter_neighbors
    )
    tail_acceptable = (
        None
        if cvar_tail_loss is None
        or cvar_tail_count == 0
        or one_leg_cvar_limit is None
        else cvar_tail_loss
        <= _multiply(one_leg_cvar_limit, Decimal(cvar_tail_count))
    )
    cvar_limit_conservative = (
        None
        if one_leg_cvar_limit is None
        else one_leg_cvar_limit
        <= _multiply(initial_capital, MAX_ONE_LEG_CVAR_FRACTION)
    )
    decomposition = {
        "spread_pnl": _sum_decimal(
            trade.spread_pnl for trade in settled_trades
        ),
        "forecast_pnl": _sum_decimal(
            trade.forecast_pnl for trade in settled_trades
        ),
        "adverse_selection_pnl": _sum_decimal(
            trade.adverse_selection_pnl for trade in settled_trades
        ),
        "hedge_pnl": _sum_decimal(
            trade.hedge_pnl for trade in settled_trades
        ),
        "fees": _sum_decimal(trade.fees for trade in settled_trades),
        "theoretical_rewards": _sum_decimal(
            trade.theoretical_rewards for trade in trades
        ),
        "scoring_rewards": _sum_decimal(
            trade.scoring_rewards for trade in trades
        ),
        "confirmed_reward_payout": _sum_decimal(
            trade.confirmed_reward_payout for trade in settled_trades
        ),
        "other_costs": _sum_decimal(
            trade.other_costs for trade in settled_trades
        ),
        "pessimistic_cost": _sum_decimal(
            trade.pessimistic_cost for trade in settled_trades
        ),
    }
    return StrategyMetrics(
        initial_capital=initial_capital,
        final_capital=final_capital,
        marked_final_capital=marked_final_capital,
        net_pnl=net_pnl,
        unsettled_marked_pnl=unsettled_marked_pnl,
        pessimistic_reward_zero_net_pnl=pessimistic_net,
        return_on_initial_capital=_divide(net_pnl, initial_capital),
        max_drawdown=max_drawdown,
        max_drawdown_fraction=max_drawdown_fraction,
        pessimistic_max_drawdown=pessimistic_max_drawdown,
        pessimistic_max_drawdown_fraction=(
            pessimistic_max_drawdown_fraction
        ),
        hit_rate=hit_rate,
        turnover=_sum_decimal(trade.notional for trade in trades),
        capacity=(
            min((trade.capacity_notional for trade in trades), default=None)
        ),
        one_leg_empirical_cvar_95=cvar,
        one_leg_tail_loss_total=cvar_tail_loss,
        one_leg_tail_episode_count=cvar_tail_count,
        independent_settled_events=len(independent_pessimistic_pnls),
        explainable_fills=len(
            {
                (trade.source_event_id, trade.decision_id)
                for trade in trades
                if trade.is_explainable_fill
            }
        ),
        bootstrap_95_lower=bootstrap_lower,
        bootstrap_95_upper=bootstrap_upper,
        max_event_profit_contribution=event_contribution,
        max_event_positive_pnl=max_event_positive_pnl,
        total_positive_event_pnl=total_positive_event_pnl,
        max_event_profit_contribution_acceptable=(
            event_contribution_acceptable
        ),
        independence_labels_complete=(
            None
            if not any(trade.settled is True for trade in trades)
            or any(
                trade.independence_group is None
                for trade in trades
                if trade.settled is True
            )
            else True
        ),
        settlement_status_complete=(
            None
            if not trades or any(trade.settled is None for trade in trades)
            else True
        ),
        settlement_provenance_consistent=(
            _settlement_provenance_consistency(trades)
        ),
        all_trades_settled=(
            None
            if not trades or any(trade.settled is None for trade in trades)
            else all(trade.settled is True for trade in trades)
        ),
        real_data=_tri_state_all([trade.is_real for trade in trades]),
        out_of_sample=_tri_state_all([trade.is_oos for trade in trades]),
        no_leakage=_tri_state_all(
            [trade.leakage_free for trade in trades]
        ),
        all_costs_included=_tri_state_all(
            [trade.all_costs_included for trade in trades]
        ),
        parameter_neighborhood_stable=parameter_stable,
        parameter_neighbor_count=neighbor_count,
        one_leg_cvar_limit_conservative=cvar_limit_conservative,
        one_leg_tail_risk_acceptable=tail_acceptable,
        pnl_recomputable=(
            None
            if not trades
            or any(trade.claimed_net_pnl is None for trade in trades)
            else all(trade.pnl_recomputable for trade in trades)
        ),
        decomposition=decomposition,
    )


def _gate(
    *,
    name: str,
    passed: bool,
    observed: str,
    failure_kind: Optional[str],
    missing: bool = False,
) -> GateEvaluation:
    reason = None
    kind = None
    if not passed:
        reason = f"{name}: {'missing' if missing else f'observed={observed}'}"
        kind = "missing" if missing else failure_kind
    return GateEvaluation(
        name=name,
        passed=passed,
        observed=observed,
        failure_reason=reason,
        failure_kind=kind,
    )


def _bool_gate(
    *,
    name: str,
    value: Optional[bool],
    failure_kind: str,
) -> GateEvaluation:
    return _gate(
        name=name,
        passed=value is True,
        observed="missing" if value is None else str(value).lower(),
        failure_kind=failure_kind,
        missing=value is None,
    )


def _validate(
    *,
    data_level: DataLevel,
    metrics: StrategyMetrics,
    strategy_kind: StrategyKind,
    reward_scoring_evidence_count: int,
    reward_payout_epoch_count: int,
    evidence_context_failures: Sequence[str],
    evidence_context_missing: bool,
    bootstrap_resamples: int,
) -> ValidationResult:
    common_gates = (
        _gate(
            name="verified_evidence_context",
            passed=not evidence_context_failures,
            observed=(
                "verified"
                if not evidence_context_failures
                else ",".join(evidence_context_failures)
            ),
            failure_kind="sample",
            missing=evidence_context_missing,
        ),
        _gate(
            name="data_level>=L2",
            passed=data_level.rank >= DataLevel.L2.rank,
            observed=data_level.value,
            failure_kind="sample",
        ),
        _bool_gate(
            name="real_data",
            value=metrics.real_data,
            failure_kind="methodology",
        ),
        _bool_gate(
            name="out_of_sample",
            value=metrics.out_of_sample,
            failure_kind="methodology",
        ),
        _bool_gate(
            name="no_leakage",
            value=metrics.no_leakage,
            failure_kind="hard",
        ),
        _gate(
            name="pessimistic_reward_zero_net_pnl>0",
            passed=metrics.pessimistic_reward_zero_net_pnl > ZERO,
            observed=_decimal_string(
                metrics.pessimistic_reward_zero_net_pnl
            ),
            failure_kind="hard",
        ),
        _bool_gate(
            name="all_costs_included",
            value=metrics.all_costs_included,
            failure_kind="hard",
        ),
        _bool_gate(
            name="independence_labels_complete",
            value=metrics.independence_labels_complete,
            failure_kind="hard",
        ),
        _bool_gate(
            name="settlement_status_complete",
            value=metrics.settlement_status_complete,
            failure_kind="hard",
        ),
        _bool_gate(
            name="settlement_provenance_consistent",
            value=metrics.settlement_provenance_consistent,
            failure_kind="hard",
        ),
        _bool_gate(
            name="all_trades_settled",
            value=metrics.all_trades_settled,
            failure_kind="sample",
        ),
        _gate(
            name=(
                "independent_settled_events>="
                f"{MINIMUM_INDEPENDENT_SETTLED_EVENTS}"
            ),
            passed=(
                metrics.independent_settled_events
                >= MINIMUM_INDEPENDENT_SETTLED_EVENTS
            ),
            observed=str(metrics.independent_settled_events),
            failure_kind="sample",
        ),
        _gate(
            name=f"explainable_fills>={MINIMUM_EXPLAINABLE_FILLS}",
            passed=metrics.explainable_fills >= MINIMUM_EXPLAINABLE_FILLS,
            observed=str(metrics.explainable_fills),
            failure_kind="sample",
        ),
        _gate(
            name="bootstrap_95_lower>0",
            passed=(
                metrics.bootstrap_95_lower is not None
                and metrics.bootstrap_95_lower > ZERO
            ),
            observed=(
                "missing"
                if metrics.bootstrap_95_lower is None
                else _decimal_string(metrics.bootstrap_95_lower)
            ),
            failure_kind="hard",
            missing=metrics.bootstrap_95_lower is None,
        ),
        _gate(
            name=f"bootstrap_resamples>={DEFAULT_BOOTSTRAP_RESAMPLES}",
            passed=bootstrap_resamples >= DEFAULT_BOOTSTRAP_RESAMPLES,
            observed=str(bootstrap_resamples),
            failure_kind="sample",
        ),
        _gate(
            name="max_event_profit_contribution<=0.20",
            passed=(
                metrics.max_event_profit_contribution is not None
                and metrics.max_event_profit_contribution_acceptable is True
            ),
            observed=(
                "missing"
                if metrics.max_event_profit_contribution is None
                else _decimal_string(
                    metrics.max_event_profit_contribution
                )
            ),
            failure_kind="hard",
            missing=metrics.max_event_profit_contribution is None,
        ),
        _bool_gate(
            name="parameter_neighborhood_stable",
            value=metrics.parameter_neighborhood_stable,
            failure_kind="methodology",
        ),
        _bool_gate(
            name="one_leg_cvar_limit<=20%_initial_capital",
            value=metrics.one_leg_cvar_limit_conservative,
            failure_kind="hard",
        ),
        _bool_gate(
            name="one_leg_tail_risk",
            value=metrics.one_leg_tail_risk_acceptable,
            failure_kind="hard",
        ),
        _bool_gate(
            name="pnl_recomputable",
            value=metrics.pnl_recomputable,
            failure_kind="hard",
        ),
    )
    reward_gates: tuple[GateEvaluation, ...] = ()
    if strategy_kind is StrategyKind.REWARD:
        reward_gates = (
            _gate(
                name="reward_strategy_data_level>=L3",
                passed=data_level.rank >= DataLevel.L3.rank,
                observed=data_level.value,
                failure_kind="methodology",
            ),
            _gate(
                name="real_reward_scoring_observations>=1",
                passed=reward_scoring_evidence_count >= 1,
                observed=str(reward_scoring_evidence_count),
                failure_kind="methodology",
            ),
            _gate(
                name="confirmed_reward_payout_epochs>=2",
                passed=reward_payout_epoch_count >= 2,
                observed=str(reward_payout_epoch_count),
                failure_kind="methodology",
            ),
        )
    gates = common_gates + reward_gates
    failed_kinds = {
        gate.failure_kind for gate in gates if gate.failure_kind is not None
    }
    if not failed_kinds:
        status = EvidenceStatus.VALIDATED_PROFITABLE
    elif "missing" in failed_kinds or "sample" in failed_kinds:
        status = EvidenceStatus.INSUFFICIENT_DATA
    elif "hard" in failed_kinds:
        status = EvidenceStatus.REJECTED
    else:
        status = EvidenceStatus.PROMISING_NOT_VALIDATED
    return ValidationResult(status=status, gates=gates)


@dataclass(frozen=True)
class StrategyEvidence:
    """One strategy experiment plus all evidence needed for strict validation."""

    strategy_id: str
    experiment_id: str
    strategy_kind: StrategyKind
    data_level: DataLevel
    initial_capital: Decimal
    trades: Sequence[TradeEvidence]
    parameter_neighbor_reward_zero_pnls: Optional[Mapping[str, Decimal]]
    one_leg_cvar_limit: Optional[Decimal]
    verified_context: Optional[VerifiedEvidenceContext]
    reward_scoring_evidence_ids: Sequence[str] = ()
    reward_payout_epoch_sources: Mapping[str, str] = field(
        default_factory=dict
    )
    bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED
    metrics: StrategyMetrics = field(init=False)
    validation: ValidationResult = field(init=False)
    evidence_context_failures: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "strategy_id",
            _nonempty(self.strategy_id, name="strategy_id"),
        )
        object.__setattr__(
            self,
            "experiment_id",
            _nonempty(self.experiment_id, name="experiment_id"),
        )
        if not isinstance(self.strategy_kind, StrategyKind):
            try:
                object.__setattr__(
                    self,
                    "strategy_kind",
                    StrategyKind(str(self.strategy_kind).lower()),
                )
            except ValueError as exc:
                raise ValueError(
                    "strategy_kind must be a supported StrategyKind"
                ) from exc
        if not isinstance(self.data_level, DataLevel):
            try:
                object.__setattr__(
                    self, "data_level", DataLevel(str(self.data_level).upper())
                )
            except ValueError as exc:
                raise ValueError("data_level must be one of L0-L4") from exc
        capital = _required_decimal(
            self.initial_capital, name="initial_capital"
        )
        if capital <= ZERO:
            raise ValueError("initial_capital must be positive")
        object.__setattr__(self, "initial_capital", capital)

        if isinstance(self.trades, (str, bytes)) or not isinstance(
            self.trades, Sequence
        ):
            raise TypeError("trades must be a sequence of TradeEvidence")
        trades = tuple(self.trades)
        if any(not isinstance(trade, TradeEvidence) for trade in trades):
            raise TypeError("trades must contain only TradeEvidence")
        trade_ids = [trade.trade_id for trade in trades]
        if len(trade_ids) != len(set(trade_ids)):
            raise ValueError("duplicate trade_id within strategy evidence")
        event_groups: dict[str, str] = {}
        for trade in trades:
            if trade.independence_group is None:
                continue
            previous_group = event_groups.setdefault(
                trade.event_id, trade.independence_group
            )
            if previous_group != trade.independence_group:
                raise ValueError(
                    "one event_id cannot span multiple independence groups: "
                    f"{trade.event_id}"
                )
        object.__setattr__(self, "trades", trades)

        if isinstance(self.reward_scoring_evidence_ids, (str, bytes)):
            raise TypeError(
                "reward_scoring_evidence_ids must be a sequence of IDs"
            )
        scoring_ids = tuple(
            sorted(
                {
                    _nonempty(str(value), name="reward scoring evidence ID")
                    for value in self.reward_scoring_evidence_ids
                }
            )
        )
        object.__setattr__(
            self, "reward_scoring_evidence_ids", scoring_ids
        )
        if not isinstance(self.reward_payout_epoch_sources, Mapping):
            raise TypeError(
                "reward_payout_epoch_sources must map epoch IDs to source IDs"
            )
        payout_sources: dict[str, str] = {}
        for raw_epoch, raw_source in sorted(
            self.reward_payout_epoch_sources.items(),
            key=lambda item: str(item[0]),
        ):
            epoch = _nonempty(str(raw_epoch), name="reward payout epoch")
            source = _nonempty(
                str(raw_source), name="reward payout source event ID"
            )
            payout_sources[epoch] = source
        if len(set(payout_sources.values())) != len(payout_sources):
            raise ValueError(
                "independent reward payout epochs need distinct source records"
            )
        for trade in trades:
            if trade.confirmed_reward_payout <= ZERO:
                continue
            assert trade.reward_payout_epoch_id is not None
            assert trade.reward_payout_source_event_id is not None
            if (
                payout_sources.get(trade.reward_payout_epoch_id)
                != trade.reward_payout_source_event_id
            ):
                raise ValueError(
                    "confirmed reward payout is not bound to the strategy "
                    f"epoch evidence: {trade.reward_payout_epoch_id}"
                )
        object.__setattr__(
            self,
            "reward_payout_epoch_sources",
            MappingProxyType(payout_sources),
        )

        neighbors: Optional[Mapping[str, Decimal]]
        if self.parameter_neighbor_reward_zero_pnls is None:
            neighbors = None
        else:
            if not isinstance(
                self.parameter_neighbor_reward_zero_pnls, Mapping
            ):
                raise TypeError(
                    "parameter_neighbor_reward_zero_pnls must be a mapping"
                )
            normalized: dict[str, Decimal] = {}
            for raw_name, raw_value in sorted(
                self.parameter_neighbor_reward_zero_pnls.items(),
                key=lambda item: str(item[0]),
            ):
                name = _nonempty(str(raw_name), name="parameter neighbor")
                if name in normalized:
                    raise ValueError(f"duplicate parameter neighbor: {name}")
                normalized[name] = _required_decimal(
                    raw_value,
                    name=f"parameter_neighbor_reward_zero_pnls[{name}]",
                )
            neighbors = MappingProxyType(normalized)
        object.__setattr__(
            self, "parameter_neighbor_reward_zero_pnls", neighbors
        )

        cvar_limit = _optional_decimal(
            self.one_leg_cvar_limit, name="one_leg_cvar_limit"
        )
        if cvar_limit is not None and cvar_limit < ZERO:
            raise ValueError("one_leg_cvar_limit cannot be negative")
        object.__setattr__(self, "one_leg_cvar_limit", cvar_limit)

        if (
            not isinstance(self.bootstrap_resamples, int)
            or isinstance(self.bootstrap_resamples, bool)
            or self.bootstrap_resamples < 100
        ):
            raise ValueError("bootstrap_resamples must be an integer >= 100")
        if self.bootstrap_seed != DEFAULT_BOOTSTRAP_SEED:
            raise ValueError(
                f"bootstrap_seed is fixed at {DEFAULT_BOOTSTRAP_SEED}"
            )

        if self.verified_context is None:
            context_failures = ("missing_verified_evidence_context",)
        elif not isinstance(
            self.verified_context, VerifiedEvidenceContext
        ):
            raise TypeError(
                "verified_context must be VerifiedEvidenceContext or None"
            )
        else:
            context_failures = self.verified_context.coverage_failures(
                experiment_id=self.experiment_id,
                trades=trades,
                parameter_neighbors=neighbors,
                reward_scoring_evidence_ids=scoring_ids,
                reward_payout_epoch_sources=payout_sources,
            )
        object.__setattr__(
            self, "evidence_context_failures", context_failures
        )

        metrics = _compute_metrics(
            initial_capital=capital,
            trades=trades,
            parameter_neighbors=neighbors,
            one_leg_cvar_limit=cvar_limit,
            bootstrap_seed=self.bootstrap_seed,
            bootstrap_resamples=self.bootstrap_resamples,
        )
        validation = _validate(
            data_level=self.data_level,
            metrics=metrics,
            strategy_kind=self.strategy_kind,
            reward_scoring_evidence_count=len(scoring_ids),
            reward_payout_epoch_count=len(payout_sources),
            evidence_context_failures=context_failures,
            evidence_context_missing=self.verified_context is None,
            bootstrap_resamples=self.bootstrap_resamples,
        )
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "validation", validation)

    def to_backtest_result(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "experiment_id": self.experiment_id,
            "strategy_kind": self.strategy_kind.value,
            "data_level": self.data_level.value,
            "status": self.validation.status.value,
            "bootstrap_seed": self.bootstrap_seed,
            "bootstrap_resamples": self.bootstrap_resamples,
            "one_leg_cvar_limit": _optional_decimal_string(
                self.one_leg_cvar_limit
            ),
            "maximum_allowed_one_leg_cvar_fraction_of_initial_capital": (
                _decimal_string(MAX_ONE_LEG_CVAR_FRACTION)
            ),
            "parameter_neighbor_reward_zero_pnls": (
                None
                if self.parameter_neighbor_reward_zero_pnls is None
                else {
                    key: _decimal_string(value)
                    for key, value in (
                        self.parameter_neighbor_reward_zero_pnls.items()
                    )
                }
            ),
            "reward_scoring_evidence_ids": list(
                self.reward_scoring_evidence_ids
            ),
            "reward_payout_epoch_sources": dict(
                self.reward_payout_epoch_sources
            ),
            "verified_evidence_context": (
                None
                if self.verified_context is None
                else self.verified_context.to_mapping()
            ),
            "evidence_context_failures": list(
                self.evidence_context_failures
            ),
            "metrics": self.metrics.to_mapping(),
            "validation": self.validation.to_mapping(),
            "trade_count": len(self.trades),
            "trade_ids": [
                trade.trade_id
                for trade in sorted(self.trades, key=_trade_order_key)
            ],
        }

    def to_experiment_summary(self) -> dict[str, Any]:
        return {
            "schema_version": EXPERIMENT_SUMMARY_SCHEMA,
            "strategy_id": self.strategy_id,
            "experiment_id": self.experiment_id,
            "strategy_kind": self.strategy_kind.value,
            "data_level": self.data_level.value,
            "status": self.validation.status.value,
            "net_pnl": _decimal_string(self.metrics.net_pnl),
            "pessimistic_reward_zero_net_pnl": _decimal_string(
                self.metrics.pessimistic_reward_zero_net_pnl
            ),
            "independent_settled_events": (
                self.metrics.independent_settled_events
            ),
            "explainable_fills": self.metrics.explainable_fills,
            "reward_scoring_evidence_count": len(
                self.reward_scoring_evidence_ids
            ),
            "confirmed_reward_payout_epochs": len(
                self.reward_payout_epoch_sources
            ),
            "bootstrap_95_lower": _optional_decimal_string(
                self.metrics.bootstrap_95_lower
            ),
            "max_event_profit_contribution": _optional_decimal_string(
                self.metrics.max_event_profit_contribution
            ),
            "failure_reasons": list(self.validation.failure_reasons),
        }


def _strategy_key(strategy: StrategyEvidence) -> tuple[str, str]:
    return (strategy.strategy_id, strategy.experiment_id)


def _sorted_unique_strategies(
    strategies: Iterable[StrategyEvidence],
) -> tuple[StrategyEvidence, ...]:
    materialized = tuple(strategies)
    if any(
        not isinstance(strategy, StrategyEvidence)
        for strategy in materialized
    ):
        raise TypeError("strategies must contain only StrategyEvidence")
    keys = [_strategy_key(strategy) for strategy in materialized]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate strategy evidence key")
    return tuple(sorted(materialized, key=_strategy_key))


def build_backtest_results(
    strategies: Iterable[StrategyEvidence],
) -> dict[str, Any]:
    """Build deterministic ``BACKTEST_RESULTS.json`` content."""

    ordered = _sorted_unique_strategies(strategies)
    return {
        "schema_version": BACKTEST_RESULTS_SCHEMA,
        "bootstrap_seed": DEFAULT_BOOTSTRAP_SEED,
        "strategies": [
            strategy.to_backtest_result() for strategy in ordered
        ],
    }


def backtest_results_json(
    strategies: Iterable[StrategyEvidence],
    *,
    indent: Optional[int] = 2,
) -> str:
    """Serialize deterministic ``BACKTEST_RESULTS.json`` content."""

    return (
        json.dumps(
            build_backtest_results(strategies),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":") if indent is None else None,
            indent=indent,
            allow_nan=False,
        )
        + ("\n" if indent is not None else "")
    )


def build_experiment_summaries(
    strategies: Iterable[StrategyEvidence],
) -> tuple[dict[str, Any], ...]:
    """Build deterministic experiment-summary rows."""

    return tuple(
        strategy.to_experiment_summary()
        for strategy in _sorted_unique_strategies(strategies)
    )


def experiment_summaries_jsonl(
    strategies: Iterable[StrategyEvidence],
) -> str:
    """Serialize deterministic, append-friendly experiment-summary JSONL."""

    return "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in build_experiment_summaries(strategies)
    )


TRADES_CSV_COLUMNS = (
    "strategy_id",
    "experiment_id",
    "trade_id",
    "event_id",
    "independence_group",
    "market_id",
    "source_event_id",
    "settlement_source_event_id",
    "decision_id",
    "one_leg_episode_id",
    "filled_at",
    "settled_at",
    "settled",
    "is_real",
    "is_oos",
    "leakage_free",
    "all_costs_included",
    "fill_explainable",
    "notional",
    "capacity_notional",
    "spread_pnl",
    "forecast_pnl",
    "adverse_selection_pnl",
    "hedge_pnl",
    "fees",
    "theoretical_rewards",
    "scoring_rewards",
    "confirmed_reward_payout",
    "reward_theoretical_source_event_id",
    "reward_scoring_source_event_id",
    "reward_payout_epoch_id",
    "reward_payout_source_event_id",
    "other_costs",
    "pessimistic_cost",
    "claimed_net_pnl",
    "recomputed_net_pnl",
    "pessimistic_reward_zero_pnl",
    "one_leg_pnl",
    "pnl_recomputable",
)


def _trade_csv_row(
    strategy: StrategyEvidence, trade: TradeEvidence
) -> dict[str, str]:
    return {
        "strategy_id": strategy.strategy_id,
        "experiment_id": strategy.experiment_id,
        "trade_id": trade.trade_id,
        "event_id": trade.event_id,
        "independence_group": trade.independence_group or "",
        "market_id": trade.market_id,
        "source_event_id": trade.source_event_id or "",
        "settlement_source_event_id": (
            trade.settlement_source_event_id or ""
        ),
        "decision_id": trade.decision_id or "",
        "one_leg_episode_id": trade.one_leg_episode_id or "",
        "filled_at": trade.filled_at,
        "settled_at": trade.settled_at or "",
        "settled": _bool_text(trade.settled),
        "is_real": _bool_text(trade.is_real),
        "is_oos": _bool_text(trade.is_oos),
        "leakage_free": _bool_text(trade.leakage_free),
        "all_costs_included": _bool_text(trade.all_costs_included),
        "fill_explainable": _bool_text(trade.fill_explainable),
        "notional": _decimal_string(trade.notional),
        "capacity_notional": _decimal_string(trade.capacity_notional),
        "spread_pnl": _decimal_string(trade.spread_pnl),
        "forecast_pnl": _decimal_string(trade.forecast_pnl),
        "adverse_selection_pnl": _decimal_string(
            trade.adverse_selection_pnl
        ),
        "hedge_pnl": _decimal_string(trade.hedge_pnl),
        "fees": _decimal_string(trade.fees),
        "theoretical_rewards": _decimal_string(
            trade.theoretical_rewards
        ),
        "scoring_rewards": _decimal_string(trade.scoring_rewards),
        "confirmed_reward_payout": _decimal_string(
            trade.confirmed_reward_payout
        ),
        "reward_theoretical_source_event_id": (
            trade.reward_theoretical_source_event_id or ""
        ),
        "reward_scoring_source_event_id": (
            trade.reward_scoring_source_event_id or ""
        ),
        "reward_payout_epoch_id": trade.reward_payout_epoch_id or "",
        "reward_payout_source_event_id": (
            trade.reward_payout_source_event_id or ""
        ),
        "other_costs": _decimal_string(trade.other_costs),
        "pessimistic_cost": _decimal_string(trade.pessimistic_cost),
        "claimed_net_pnl": (
            ""
            if trade.claimed_net_pnl is None
            else _decimal_string(trade.claimed_net_pnl)
        ),
        "recomputed_net_pnl": _decimal_string(
            trade.recomputed_net_pnl
        ),
        "pessimistic_reward_zero_pnl": _decimal_string(
            trade.pessimistic_reward_zero_pnl
        ),
        "one_leg_pnl": (
            ""
            if trade.one_leg_pnl is None
            else _decimal_string(trade.one_leg_pnl)
        ),
        "pnl_recomputable": _bool_text(trade.pnl_recomputable),
    }


def build_trades_csv_rows(
    strategies: Iterable[StrategyEvidence],
) -> tuple[dict[str, str], ...]:
    """Build deterministic, string-only ``TRADES.csv`` rows."""

    rows: list[dict[str, str]] = []
    for strategy in _sorted_unique_strategies(strategies):
        rows.extend(
            _trade_csv_row(strategy, trade)
            for trade in sorted(strategy.trades, key=_trade_order_key)
        )
    return tuple(rows)


def trades_csv_text(strategies: Iterable[StrategyEvidence]) -> str:
    """Serialize deterministic ``TRADES.csv`` content without writing it."""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=TRADES_CSV_COLUMNS,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(build_trades_csv_rows(strategies))
    return output.getvalue()


__all__ = [
    "BACKTEST_RESULTS_SCHEMA",
    "DEFAULT_BOOTSTRAP_RESAMPLES",
    "DEFAULT_BOOTSTRAP_SEED",
    "MAX_ONE_LEG_CVAR_FRACTION",
    "DataLevel",
    "EvidenceStatus",
    "GateEvaluation",
    "StrategyEvidence",
    "StrategyKind",
    "StrategyMetrics",
    "TRADES_CSV_COLUMNS",
    "TradeEvidence",
    "ValidationResult",
    "VerifiedEvidenceContext",
    "backtest_results_json",
    "build_backtest_results",
    "build_experiment_summaries",
    "build_trades_csv_rows",
    "experiment_summaries_jsonl",
    "trades_csv_text",
]
