"""Readiness and fail-closed gate helpers for BTC 5m/15m shared-terminal work."""

from __future__ import annotations

import json
import stat
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

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
from .data_store import canonical_json_bytes

if TYPE_CHECKING:
    from .btc_twap_relative_value_v07_evaluation import V07LockedOOSEvaluation

ZERO = Decimal(0)
ONE = Decimal(1)
PROBE_ALL_IN_LIMIT = Decimal("0.99")
EXPECTED_RTDS_INTERVAL_MS = 1_000
MAXIMUM_RTDS_GAP_MS = 2_000
MINIMUM_SETTLED_CLUSTERS = 200
MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS = 200
MINIMUM_STRUCTURAL_SHADOW_EXPIRIES = 200
MINIMUM_CONSECUTIVE_PROFITABLE_UTC_DAYS = 14
MINIMUM_DAILY_NET_PNL = Decimal(20)
MAXIMUM_CAPITAL_DEPLOYED = Decimal(2_000)
MAXIMUM_SINGLE_EXPIRY_PNL_CONCENTRATION = Decimal("0.20")
CAPTURE_FAILURE_RATE_UPPER_BOUND_EXCLUSIVE = Decimal("0.05")
MINIMUM_CAPTURE_FREE_DISK_BYTES = 10 * 1024**3
MAXIMUM_PROJECTED_DAILY_CAPTURE_BYTES = 1024**3
MINIMUM_CAPTURE_MEMORY_BYTES = 2 * 1024**3
DEFAULT_PAIR_PRICING_POLICY = PairPricingPolicy(structural_only=True)
GATE_0_PAIR_PRICING_POLICY = PairPricingPolicy(
    pair_risk_usdc=None,
    structural_only=True,
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
V08_PREREGISTRATION_PATH = (
    PROJECT_ROOT
    / "research"
    / "btc_5m_15m_edge_readiness_v08_2026-08-18"
    / "PREREGISTRATION.json"
)
STRICT_GATE_0_REPORT_SCHEMA = "btc-5m-15m-gate-0-executable-upper-bound-report.v3"
STRUCTURAL_SHADOW_REPORT_SCHEMAS = frozenset({"btc_twap_structural_shadow_report.v1"})
CAPTURE_CAPACITY_ARTIFACT_SCHEMA = "btc-twap-capture-capacity-evidence.v1"
SERVICE_HEALTH_ARTIFACT_SCHEMA = "btc-twap-service-health-evidence.v1"
AUTHENTICATED_READ_ARTIFACT_SCHEMA = "btc-twap-authenticated-read-evidence.v1"
FILL_STREAM_ARTIFACT_SCHEMA = "btc-twap-fill-stream-evidence.v1"
FAULT_DRILLS_ARTIFACT_SCHEMA = "btc-twap-fault-drills-evidence.v1"
PROBE_ADAPTER_ARTIFACT_SCHEMA = "btc-twap-probe-adapter-evidence.v1"
DAILY_LEDGER_ARTIFACT_SCHEMA = "btc-twap-daily-ledger-evidence.v1"
CAPITAL_LEDGER_ARTIFACT_SCHEMA = "btc-twap-capital-ledger-evidence.v1"
EXECUTION_RECONCILIATION_ARTIFACT_SCHEMA = (
    "btc-twap-execution-reconciliation-evidence.v1"
)
STRUCTURAL_SHADOW_TRUSTED_BUILD_RECEIPT_SCHEMA = (
    "btc-twap-structural-shadow-build-receipt.v1"
)
STRUCTURAL_SHADOW_EXTERNAL_ANCHOR_SCHEMA = (
    "btc-twap-structural-shadow-external-anchor.v1"
)


def _strict_v08_preregistration() -> tuple[Path, dict[str, Any]]:
    document = json.loads(V08_PREREGISTRATION_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError("v0.8 preregistration must be a JSON object")
    return V08_PREREGISTRATION_PATH, document


def _strict_gate_0_parameters() -> dict[str, Any]:
    prereg_path, prereg = _strict_v08_preregistration()
    report_config = prereg["gate_0"]["existing_41_attempt_report"]
    scan_start, scan_end = report_config["scan_ttc_seconds_inclusive"]
    return {
        "track_id": prereg["track_id"],
        "v08_preregistration_path": str(prereg_path),
        "v08_preregistration_sha256": sha256(prereg_path.read_bytes()).hexdigest(),
        "expected_clean_attempts": report_config[
            "expected_unique_common_expiry_attempts"
        ],
        "scan_ttc_seconds_inclusive": [scan_start, scan_end],
        "candidate_ttc_count": scan_start - scan_end + 1,
        "decision_execution_mode": prereg["gate_0"]["decision_execution_mode"],
        "minimum_average_best_total_pnl_per_expiry_usdc": prereg["gate_0"][
            "minimum_average_best_total_pnl_per_expiry_usdc"
        ],
        "incomplete_existing_41_evidence_action": prereg["gate_0"][
            "incomplete_existing_41_evidence_action"
        ],
    }


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_utc_date_or_timestamp(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        parsed = _parse_utc_timestamp(value)
        return None if parsed is None else parsed.date()


def _validated_utc_now(
    value: datetime | None,
    *,
    missing_reason: str,
) -> tuple[datetime | None, tuple[str, ...]]:
    if value is None:
        return None, (missing_reason,)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None, (missing_reason,)
    return value.astimezone(timezone.utc), ()


def _validated_positive_int(
    value: int | None,
    *,
    invalid_reason: str,
) -> tuple[int | None, tuple[str, ...]]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None, (invalid_reason,)
    return value, ()


@dataclass(frozen=True)
class VerifiedJsonArtifact:
    path: str
    sha256: str
    schema_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("artifact path must be non-empty")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("artifact sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("artifact schema_version must be non-empty")


@dataclass(frozen=True)
class StructuralAuthorityTrustPolicy:
    trusted_build_receipt_path: str
    trusted_build_receipt_sha256: str
    external_anchor_reference: str
    external_anchor_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.trusted_build_receipt_path, str)
            or not self.trusted_build_receipt_path
        ):
            raise ValueError("trusted build receipt path must be non-empty")
        if (
            not isinstance(self.external_anchor_reference, str)
            or not self.external_anchor_reference
        ):
            raise ValueError("external anchor reference must be non-empty")
        for name in (
            "trusted_build_receipt_sha256",
            "external_anchor_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def load_verified_json_artifact(path: str | Path) -> VerifiedJsonArtifact:
    absolute = _artifact_absolute_path(path)
    if not _artifact_path_is_regular_file(absolute):
        raise ValueError("artifact path must be an existing regular file")
    try:
        document = json.loads(absolute.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("artifact JSON is unreadable or invalid") from exc
    if not isinstance(document, dict):
        raise TypeError("artifact JSON must be an object")
    schema_version = document.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version:
        raise ValueError("artifact schema_version must be a non-empty string")
    return VerifiedJsonArtifact(
        path=str(absolute),
        sha256=sha256(absolute.read_bytes()).hexdigest(),
        schema_version=schema_version,
    )


def _load_bound_artifact_document(
    artifact: VerifiedJsonArtifact,
    *,
    accepted_schema_versions: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    absolute = _artifact_absolute_path(artifact.path)
    if not _artifact_path_is_regular_file(absolute):
        return None
    try:
        current_bytes = absolute.read_bytes()
    except OSError:
        return None
    if sha256(current_bytes).hexdigest() != artifact.sha256:
        return None
    try:
        document = json.loads(current_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("schema_version") != artifact.schema_version:
        return None
    if (
        accepted_schema_versions is not None
        and artifact.schema_version not in accepted_schema_versions
    ):
        return None
    return document


def _artifact_path_is_regular_file(path: Path) -> bool:
    try:
        for ancestor in (path, *path.parents):
            stat_result = ancestor.lstat()
            if ancestor.is_symlink():
                return False
            if hasattr(stat_result, "st_file_attributes") and (
                stat_result.st_file_attributes
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                return False
        stat_result = path.lstat()
    except OSError:
        return False
    return path.is_file()


def _artifact_absolute_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else (Path.cwd() / candidate)


@dataclass(frozen=True)
class ExecutionProbeEvidenceBundle:
    gate_0_report_artifact: VerifiedJsonArtifact | None = None
    structural_shadow_artifact: VerifiedJsonArtifact | None = None
    capture_capacity_artifact: VerifiedJsonArtifact | None = None
    service_health_artifact: VerifiedJsonArtifact | None = None
    authenticated_read_artifact: VerifiedJsonArtifact | None = None
    fill_stream_artifact: VerifiedJsonArtifact | None = None
    fault_drills_artifact: VerifiedJsonArtifact | None = None
    probe_adapter_artifact: VerifiedJsonArtifact | None = None


@dataclass(frozen=True)
class StrategyLiveEvidenceBundle:
    gate_0_report_artifact: VerifiedJsonArtifact | None = None
    structural_shadow_artifact: VerifiedJsonArtifact | None = None
    service_health_artifact: VerifiedJsonArtifact | None = None
    daily_ledger_artifact: VerifiedJsonArtifact | None = None
    capital_ledger_artifact: VerifiedJsonArtifact | None = None
    execution_reconciliation_artifact: VerifiedJsonArtifact | None = None


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
                maker_fill_cap_by_token_id=pricing_policy.maker_fill_cap_by_token_id,
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
                    maker_fill_cap_by_token_id=(
                        attempt.pricing_policy.maker_fill_cap_by_token_id
                    ),
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


def _validated_gate_0_report_document(
    artifact: VerifiedJsonArtifact | None,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    if artifact is None:
        return None, ("gate_0_report_artifact_missing",)
    report = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(STRICT_GATE_0_REPORT_SCHEMA,),
    )
    if report is None:
        return None, ("gate_0_report_artifact_invalid",)
    unsigned = {key: value for key, value in report.items() if key != "report_sha256"}
    if report.get("report_sha256") != sha256(canonical_json_bytes(unsigned)).hexdigest():
        return None, ("gate_0_report_hash_invalid",)
    authority = report.get("authority")
    if not isinstance(authority, dict):
        return None, ("gate_0_report_authority_missing",)
    strict = _strict_gate_0_parameters()
    if authority.get("track_id") != strict["track_id"]:
        return None, ("gate_0_report_track_id_mismatch",)
    if authority.get("v08_preregistration_sha256") != strict["v08_preregistration_sha256"]:
        return None, ("gate_0_report_preregistration_sha_mismatch",)
    if authority.get("expected_clean_attempts") != strict["expected_clean_attempts"]:
        return None, ("gate_0_report_expected_clean_attempts_mismatch",)
    if authority.get("scan_ttc_seconds_inclusive") != strict["scan_ttc_seconds_inclusive"]:
        return None, ("gate_0_report_scan_window_mismatch",)
    if authority.get("candidate_ttc_count") != strict["candidate_ttc_count"]:
        return None, ("gate_0_report_candidate_ttc_count_mismatch",)
    if authority.get("decision_execution_mode") != strict["decision_execution_mode"]:
        return None, ("gate_0_report_execution_mode_mismatch",)
    if (
        authority.get("minimum_average_best_total_pnl_per_expiry_usdc")
        != strict["minimum_average_best_total_pnl_per_expiry_usdc"]
    ):
        return None, ("gate_0_report_minimum_average_pnl_mismatch",)
    strict_identity = {
        "track_id": authority.get("track_id"),
        "v08_preregistration_sha256": authority.get("v08_preregistration_sha256"),
        "expected_clean_attempts": authority.get("expected_clean_attempts"),
        "scan_ttc_seconds_inclusive": authority.get("scan_ttc_seconds_inclusive"),
        "candidate_ttc_count": authority.get("candidate_ttc_count"),
        "decision_execution_mode": authority.get("decision_execution_mode"),
        "minimum_average_best_total_pnl_per_expiry_usdc": authority.get(
            "minimum_average_best_total_pnl_per_expiry_usdc"
        ),
    }
    if authority.get("strict_identity_sha256") != sha256(
        canonical_json_bytes(strict_identity)
    ).hexdigest():
        return None, ("gate_0_report_strict_identity_hash_invalid",)
    if authority.get("requested_expected_clean_attempts") != strict["expected_clean_attempts"]:
        return None, ("gate_0_report_requested_count_mismatch",)
    if authority.get("requested_decision_tau_seconds") is not None:
        return None, ("gate_0_report_requested_tau_override_present",)
    if authority.get("strict_parameters_match") is not True:
        return None, ("gate_0_report_not_authorized_for_formal_pass",)
    if authority.get("strict_fill_volume_bound_complete") is not True:
        return None, ("gate_0_report_fill_caps_not_manifest_bound",)
    if report.get("observed_unique_common_expiry_attempts") != strict["expected_clean_attempts"]:
        return None, ("gate_0_report_observed_count_mismatch",)
    scan = report.get("scan")
    if not isinstance(scan, Mapping):
        return None, ("gate_0_report_scan_invalid",)
    if (
        scan.get("ttc_seconds_start") != strict["scan_ttc_seconds_inclusive"][0]
        or scan.get("ttc_seconds_end") != strict["scan_ttc_seconds_inclusive"][1]
        or scan.get("candidate_ttc_count") != strict["candidate_ttc_count"]
        or scan.get("all_expiries_complete") is not True
    ):
        return None, ("gate_0_report_scan_invalid",)
    maker_maker = report.get("execution_modes", {}).get("maker_maker")
    if not isinstance(maker_maker, Mapping):
        return None, ("gate_0_report_maker_maker_missing",)
    if maker_maker.get("evidence_complete") is not True:
        return None, ("gate_0_report_maker_maker_incomplete",)
    if maker_maker.get("missing_fill_volume_bound_expiry_ids") != []:
        return None, ("gate_0_report_fill_caps_missing",)
    attempts = maker_maker.get("attempts")
    if not isinstance(attempts, list):
        return None, ("gate_0_report_maker_maker_attempts_invalid",)
    strict_minimum_average = Decimal(
        str(strict["minimum_average_best_total_pnl_per_expiry_usdc"])
    )
    try:
        recomputed_aggregate = ZERO
        for item in attempts:
            if not isinstance(item, Mapping):
                return None, ("gate_0_report_maker_maker_attempts_invalid",)
            selected_level = item.get("selected_level")
            if selected_level is not None:
                if not isinstance(selected_level, Mapping):
                    return None, ("gate_0_report_maker_maker_attempts_invalid",)
                if (
                    item.get("fill_volume_bound_source")
                    != "context.future_public_trades_by_token_id"
                ):
                    return None, ("gate_0_report_fill_caps_not_manifest_bound",)
            best_total = Decimal(str(item.get("best_total_pnl", "0")))
            if not best_total.is_finite() or best_total < ZERO:
                return None, ("gate_0_report_maker_maker_economic_invalid",)
            if selected_level is None and best_total != ZERO:
                return None, ("gate_0_report_maker_maker_economic_invalid",)
            recomputed_aggregate += best_total
        reported_attempt_count = maker_maker.get("attempt_count")
        reported_aggregate = Decimal(str(maker_maker.get("aggregate_best_total_pnl")))
        reported_average = Decimal(
            str(maker_maker.get("average_best_total_pnl_per_expiry"))
        )
        reported_minimum = Decimal(str(maker_maker.get("minimum_average_pnl_per_expiry")))
    except (ArithmeticError, TypeError, ValueError):
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    if (
        reported_attempt_count != len(attempts)
        or reported_minimum != strict_minimum_average
    ):
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    recomputed_average = (
        ZERO if not attempts else (recomputed_aggregate / Decimal(len(attempts)))
    )
    if (
        reported_aggregate != recomputed_aggregate
        or reported_average != recomputed_average
    ):
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    recomputed_economic_gate = (
        recomputed_aggregate > ZERO and recomputed_average >= strict_minimum_average
    )
    recomputed_gate_passed = recomputed_economic_gate
    recomputed_stop_recommended = not recomputed_economic_gate
    if maker_maker.get("economic_gate_passed") is not recomputed_economic_gate:
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    if maker_maker.get("gate_0_passed") is not recomputed_gate_passed:
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    if maker_maker.get("stop_recommended") is not recomputed_stop_recommended:
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    if maker_maker.get("rerun_required") is not False:
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    if maker_maker.get("decision") != (
        "PASS" if recomputed_gate_passed else "STOP"
    ):
        return None, ("gate_0_report_maker_maker_economic_invalid",)
    if report.get("decision") not in {"PASS", "STOP", "RERUN_REQUIRED"}:
        return None, ("gate_0_report_decision_invalid",)
    gate_0_passed = report.get("gate_0_passed") is True
    rerun_required = report.get("rerun_required") is True
    stop_recommended = report.get("stop_recommended") is True
    if report.get("decision") == "PASS" and (not gate_0_passed or rerun_required or stop_recommended):
        return None, ("gate_0_report_decision_inconsistent",)
    if report.get("decision") == "STOP" and (gate_0_passed or rerun_required or not stop_recommended):
        return None, ("gate_0_report_decision_inconsistent",)
    if report.get("decision") == "RERUN_REQUIRED" and (
        gate_0_passed or stop_recommended or not rerun_required
    ):
        return None, ("gate_0_report_decision_inconsistent",)
    return report, ()


def _validated_structural_shadow_document(
    artifact: VerifiedJsonArtifact | None,
    *,
    trust_policy: StructuralAuthorityTrustPolicy | None,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    if artifact is None:
        return None, ("neutral_shadow_artifact_missing",)
    report = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=tuple(STRUCTURAL_SHADOW_REPORT_SCHEMAS),
    )
    if report is None:
        return None, ("neutral_shadow_artifact_invalid",)
    unsigned = {key: value for key, value in report.items() if key != "report_sha256"}
    if report.get("report_sha256") != sha256(canonical_json_bytes(unsigned)).hexdigest():
        return None, ("neutral_shadow_report_hash_invalid",)
    authority = report.get("builder_authority")
    if not isinstance(authority, Mapping):
        return None, ("neutral_shadow_builder_authority_missing",)
    if authority.get("issuer") != "btc_twap_structural_shadow.finalized_capture_store_builder":
        return None, ("neutral_shadow_builder_authority_issuer_invalid",)
    if authority.get("authority_kind") != "capture_store_finalized":
        return None, ("neutral_shadow_builder_authority_kind_invalid",)
    if authority.get("trust_model") != "local_hash_only_not_formal_readiness_authority":
        return None, ("neutral_shadow_builder_authority_trust_model_invalid",)
    if authority.get("required_trust_primitives") != [
        "trusted_build_receipt",
        "external_anchor",
    ]:
        return None, ("neutral_shadow_builder_authority_trust_primitives_invalid",)
    missing_trust_primitives = authority.get("missing_trust_primitives")
    if not isinstance(missing_trust_primitives, list) or any(
        not isinstance(item, str) for item in missing_trust_primitives
    ):
        return None, ("neutral_shadow_builder_authority_trust_primitives_invalid",)
    if trust_policy is None:
        return None, ("neutral_shadow_builder_authority_untrusted",)
    strict_track_id = _strict_gate_0_parameters()["track_id"]
    if report.get("track_id") != strict_track_id:
        return None, ("neutral_shadow_track_mismatch",)
    attempts = report.get("attempts")
    zero_cohorts = report.get("locked_zero_cohorts")
    if not isinstance(attempts, list) or not isinstance(zero_cohorts, list):
        return None, ("neutral_shadow_builder_authority_rows_invalid",)
    authority_payload = {
        "schema_version": report.get("schema_version"),
        "issuer": authority.get("issuer"),
        "trust_model": authority.get("trust_model"),
        "required_trust_primitives": authority.get("required_trust_primitives"),
        "track_id": report.get("track_id"),
        "source_input_sha256": report.get("source_input_sha256"),
        "preregistration_sha256": report.get("preregistration_sha256"),
        "rolling_audit_sha256": report.get("rolling_audit_sha256"),
        "attempts": [
            {
                "attempt_id": item.get("attempt_id"),
                "expiry_ms": item.get("expiry_ms"),
                "capture_verification_sha256": (
                    None
                    if not isinstance(item.get("capture_verification"), Mapping)
                    else sha256(
                        canonical_json_bytes(item.get("capture_verification"))
                    ).hexdigest()
                ),
                "locked_action_payload_sha256": item.get(
                    "locked_action_payload_sha256"
                ),
                "source_evidence_sha256": item.get("source_evidence_sha256"),
            }
            for item in attempts
            if isinstance(item, Mapping)
        ],
        "locked_zero_cohorts": [
            {
                "attempt_id": item.get("attempt_id"),
                "expiry_ms": item.get("expiry_ms"),
                "outcome": item.get("outcome"),
                "capture_verification_sha256": (
                    None
                    if not isinstance(item.get("capture_verification"), Mapping)
                    else sha256(
                        canonical_json_bytes(item.get("capture_verification"))
                    ).hexdigest()
                ),
                "source_evidence_sha256": item.get("source_evidence_sha256"),
                "locked_action_payload_sha256": item.get(
                    "locked_action_payload_sha256"
                ),
                "dirty_reason_code": item.get("dirty_reason_code"),
            }
            for item in zero_cohorts
            if isinstance(item, Mapping)
        ],
    }
    if len(authority_payload["attempts"]) != len(attempts) or len(
        authority_payload["locked_zero_cohorts"]
    ) != len(zero_cohorts):
        return None, ("neutral_shadow_builder_authority_rows_invalid",)
    if authority.get("authority_sha256") != sha256(
        canonical_json_bytes(authority_payload)
    ).hexdigest():
        return None, ("neutral_shadow_builder_authority_hash_invalid",)
    trust_bound_digest = _structural_shadow_trust_bound_digest(report)
    trust_evidence_refs = authority.get("trust_evidence_refs")
    if not isinstance(trust_evidence_refs, Mapping):
        return None, ("neutral_shadow_builder_authority_untrusted",)
    trusted_build_receipt_ref = trust_evidence_refs.get("trusted_build_receipt")
    external_anchor_ref = trust_evidence_refs.get("external_anchor")
    if not isinstance(trusted_build_receipt_ref, Mapping) or not isinstance(
        external_anchor_ref, Mapping
    ):
        return None, ("neutral_shadow_builder_authority_untrusted",)
    if missing_trust_primitives:
        return None, ("neutral_shadow_builder_authority_untrusted",)
    if (
        trusted_build_receipt_ref.get("status") != "bound"
        or external_anchor_ref.get("status") != "bound"
    ):
        return None, ("neutral_shadow_builder_authority_untrusted",)
    if (
        trusted_build_receipt_ref.get("path")
        != trust_policy.trusted_build_receipt_path
        or trusted_build_receipt_ref.get("sha256")
        != trust_policy.trusted_build_receipt_sha256
        or external_anchor_ref.get("reference")
        != trust_policy.external_anchor_reference
        or external_anchor_ref.get("sha256")
        != trust_policy.external_anchor_sha256
    ):
        return None, ("neutral_shadow_builder_authority_policy_mismatch",)
    trusted_receipt_artifact, trusted_receipt = _load_bound_reference_document(
        trusted_build_receipt_ref,
        accepted_schema_versions=(STRUCTURAL_SHADOW_TRUSTED_BUILD_RECEIPT_SCHEMA,),
    )
    if trusted_receipt_artifact is None or trusted_receipt is None:
        return None, ("neutral_shadow_builder_authority_trusted_build_receipt_invalid",)
    if (
        trusted_receipt.get("track_id") != strict_track_id
        or trusted_receipt.get("report_path") != artifact.path
        or trusted_receipt.get("report_digest_sha256") != trust_bound_digest
        or trusted_receipt.get("authority_sha256") != authority.get("authority_sha256")
    ):
        return None, ("neutral_shadow_builder_authority_trusted_build_receipt_invalid",)
    _anchor_artifact, anchor_document = _load_bound_reference_document(
        external_anchor_ref,
        accepted_schema_versions=(STRUCTURAL_SHADOW_EXTERNAL_ANCHOR_SCHEMA,),
        path_key="reference",
    )
    if anchor_document is None:
        return None, ("neutral_shadow_builder_authority_external_anchor_invalid",)
    if (
        anchor_document.get("track_id") != strict_track_id
        or anchor_document.get("report_digest_sha256") != trust_bound_digest
        or anchor_document.get("trusted_build_receipt_sha256")
        != trusted_receipt_artifact.sha256
        or anchor_document.get("authority_sha256") != authority.get("authority_sha256")
    ):
        return None, ("neutral_shadow_builder_authority_external_anchor_invalid",)
    return report, ()


def _artifact_flag(
    artifact: VerifiedJsonArtifact | None,
    *,
    accepted_schema: str,
    required_flag: str,
    missing_reason: str,
    invalid_reason: str,
    false_reason: str,
) -> tuple[bool, tuple[str, ...]]:
    if artifact is None:
        return False, (missing_reason,)
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(accepted_schema,),
    )
    if document is None:
        return False, (invalid_reason,)
    if document.get(required_flag) is not True:
        return False, (false_reason,)
    return True, ()


REQUIRED_FAULT_DRILLS = (
    "partial_fill",
    "disconnect",
    "reject",
    "cancel_race",
    "duplicate_and_late_fill",
    "fok_depth_shortfall",
    "emergency_unwind",
    "persistent_kill_switch",
)


def _is_sha256_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_source_binding(
    *,
    path_value: Any,
    sha_value: Any,
    accepted_schema_versions: Sequence[str] | None = None,
    require_json: bool = True,
) -> bool:
    if not isinstance(path_value, str) or not path_value or not _is_sha256_text(sha_value):
        return False
    absolute = _artifact_absolute_path(path_value)
    if not _artifact_path_is_regular_file(absolute):
        return False
    current_sha = sha256(absolute.read_bytes()).hexdigest()
    if current_sha != sha_value:
        return False
    if not require_json:
        return True
    try:
        artifact = load_verified_json_artifact(absolute)
    except ValueError:
        return False
    return not (
        accepted_schema_versions is not None
        and artifact.schema_version not in accepted_schema_versions
    )


def _load_bound_reference_document(
    reference: Any,
    *,
    accepted_schema_versions: Sequence[str],
    path_key: str = "path",
) -> tuple[VerifiedJsonArtifact | None, dict[str, Any] | None]:
    if not isinstance(reference, Mapping):
        return None, None
    path_value = reference.get(path_key)
    sha_value = reference.get("sha256")
    if not _validated_source_binding(
        path_value=path_value,
        sha_value=sha_value,
        accepted_schema_versions=accepted_schema_versions,
    ):
        return None, None
    try:
        artifact = load_verified_json_artifact(str(path_value))
    except ValueError:
        return None, None
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=accepted_schema_versions,
    )
    if document is None:
        return None, None
    return artifact, document


def _structural_shadow_trust_bound_digest(report: Mapping[str, Any]) -> str:
    unsigned = {
        key: value for key, value in report.items() if key != "report_sha256"
    }
    authority = unsigned.get("builder_authority")
    if isinstance(authority, Mapping):
        trimmed_authority = dict(authority)
        trimmed_authority.pop("trusted_build_receipt", None)
        trimmed_authority.pop("external_anchor", None)
        trimmed_authority.pop("trust_evidence_refs", None)
        trimmed_authority.pop("missing_trust_primitives", None)
        unsigned["builder_authority"] = trimmed_authority
    return sha256(canonical_json_bytes(unsigned)).hexdigest()


def _validated_service_health_artifact(
    artifact: VerifiedJsonArtifact | None,
    *,
    evaluation_utc_now: datetime | None,
    max_age_seconds: int | None,
    minimum_window_ms: int | None,
) -> tuple[bool, tuple[str, ...]]:
    if artifact is None:
        return False, ("service_health_artifact_missing",)
    validated_now, now_reasons = _validated_utc_now(
        evaluation_utc_now,
        missing_reason="service_health_evaluation_now_missing",
    )
    if now_reasons:
        return False, now_reasons
    validated_max_age, max_age_reasons = _validated_positive_int(
        max_age_seconds,
        invalid_reason="service_health_max_age_invalid",
    )
    if max_age_reasons:
        return False, max_age_reasons
    validated_minimum_window, minimum_window_reasons = _validated_positive_int(
        minimum_window_ms,
        invalid_reason="service_health_minimum_window_invalid",
    )
    if minimum_window_reasons:
        return False, minimum_window_reasons
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(SERVICE_HEALTH_ARTIFACT_SCHEMA,),
    )
    if document is None:
        return False, ("service_health_artifact_invalid",)
    if document.get("service_continuously_healthy") is not True:
        return False, ("service_continuous_health_unverified",)
    if not _is_sha256_text(document.get("heartbeat_source_sha256")):
        return False, ("service_health_heartbeat_source_hash_missing",)
    if not _validated_source_binding(
        path_value=document.get("heartbeat_source_path"),
        sha_value=document.get("heartbeat_source_sha256"),
    ):
        return False, ("service_health_heartbeat_source_invalid",)
    generated_at = _parse_utc_timestamp(document.get("generated_at"))
    if document.get("generated_at") is not None and generated_at is None:
        return False, ("service_health_generated_at_invalid",)
    started = document.get("window_started_at_ms")
    ended = document.get("window_ended_at_ms")
    if (
        isinstance(started, bool)
        or not isinstance(started, int)
        or isinstance(ended, bool)
        or not isinstance(ended, int)
        or started > ended
    ):
        return False, ("service_health_window_invalid",)
    window_duration_ms = ended - started
    if window_duration_ms < validated_minimum_window:
        return False, ("service_health_window_too_short",)
    window_ended_at = datetime.fromtimestamp(ended / 1000, tz=timezone.utc)
    if generated_at is not None:
        if generated_at > validated_now or generated_at < window_ended_at:
            return False, ("service_health_generated_at_invalid",)
        if (generated_at - window_ended_at).total_seconds() > validated_max_age:
            return False, ("service_health_artifact_stale",)
    if (validated_now - window_ended_at).total_seconds() > validated_max_age:
        return False, ("service_health_artifact_stale",)
    return True, ()


def _validated_receipt_artifact(
    artifact: VerifiedJsonArtifact | None,
    *,
    schema: str,
    required_flag: str,
    missing_reason: str,
    invalid_reason: str,
    false_reason: str,
    missing_receipt_reason: str,
) -> tuple[bool, tuple[str, ...]]:
    if artifact is None:
        return False, (missing_reason,)
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(schema,),
    )
    if document is None:
        return False, (invalid_reason,)
    if document.get(required_flag) is not True:
        return False, (false_reason,)
    if not _validated_source_binding(
        path_value=document.get("receipt_path"),
        sha_value=document.get("receipt_sha256"),
    ):
        return False, (missing_receipt_reason,)
    return True, ()


def _validated_fault_drills_artifact(
    artifact: VerifiedJsonArtifact | None,
) -> tuple[bool, tuple[str, ...]]:
    if artifact is None:
        return False, ("fault_drills_artifact_missing",)
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(FAULT_DRILLS_ARTIFACT_SCHEMA,),
    )
    if document is None:
        return False, ("fault_drills_artifact_invalid",)
    passed = document.get("passed_by_drill")
    receipts = document.get("receipts_by_drill")
    if not isinstance(passed, Mapping) or not isinstance(receipts, Mapping):
        return False, ("failure_drills_incomplete",)
    for drill in REQUIRED_FAULT_DRILLS:
        receipt = receipts.get(drill)
        if (
            passed.get(drill) is not True
            or not isinstance(receipt, Mapping)
            or not _validated_source_binding(
                path_value=receipt.get("path"),
                sha_value=receipt.get("sha256"),
            )
        ):
            return False, ("failure_drills_incomplete",)
    if set(passed) != set(REQUIRED_FAULT_DRILLS):
        return False, ("failure_drills_incomplete",)
    return True, ()


def _validated_probe_adapter_artifact(
    artifact: VerifiedJsonArtifact | None,
) -> tuple[bool, tuple[str, ...]]:
    if artifact is None:
        return False, ("probe_adapter_artifact_missing",)
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(PROBE_ADAPTER_ARTIFACT_SCHEMA,),
    )
    if document is None:
        return False, ("probe_adapter_artifact_invalid",)
    if document.get("double_maker_probe_implemented") is not True:
        return False, ("double_maker_probe_not_implemented",)
    if document.get("full_hedge_depth_verified") is not True:
        return False, ("full_hedge_depth_unverified",)
    if not _validated_source_binding(
        path_value=document.get("code_artifact_path"),
        sha_value=document.get("code_artifact_sha256"),
        require_json=False,
    ):
        return False, ("probe_adapter_code_digest_missing",)
    if not _validated_source_binding(
        path_value=document.get("test_artifact_path"),
        sha_value=document.get("test_artifact_sha256"),
        require_json=False,
    ):
        return False, ("probe_adapter_test_digest_missing",)
    for key in ("code_artifact_path", "test_artifact_path"):
        path_value = document.get(key)
        if not isinstance(path_value, str) or not str(PROJECT_ROOT) in str(
            _artifact_absolute_path(path_value)
        ):
            return False, ("probe_adapter_artifact_invalid",)
    return True, ()


def _validated_capture_capacity_artifact(
    artifact: VerifiedJsonArtifact | None,
) -> tuple[bool, tuple[str, ...]]:
    if artifact is None:
        return False, ("capture_capacity_artifact_missing",)
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(CAPTURE_CAPACITY_ARTIFACT_SCHEMA,),
    )
    if document is None:
        return False, ("capture_capacity_artifact_invalid",)
    unsigned = {key: value for key, value in document.items() if key != "artifact_sha256"}
    if document.get("artifact_sha256") != sha256(
        canonical_json_bytes(unsigned)
    ).hexdigest():
        return False, ("capture_capacity_artifact_invalid",)
    strict_track_id = _strict_gate_0_parameters()["track_id"]
    if document.get("track") != strict_track_id:
        return False, ("capture_capacity_track_mismatch",)
    try:
        verdict = evaluate_capture_capacity(
            CaptureCapacityEvidence(
                capture_attempt_count=int(document["capture_attempt_count"]),
                capture_failure_count=int(document["capture_failure_count"]),
                free_disk_bytes=int(document["free_disk_bytes"]),
                projected_daily_capture_bytes=int(
                    document["projected_daily_capture_bytes"]
                ),
                available_memory_bytes=int(document["available_memory_bytes"]),
                burstable_cpu_credit_exhausted=document["burstable_cpu_credit_exhausted"],
            )
        )
    except (KeyError, TypeError, ValueError):
        return False, ("capture_capacity_artifact_invalid",)
    if verdict.passed is not True:
        return False, ("capture_capacity_gate_not_passed",)
    receipts = document.get("attempt_receipts")
    pending_receipts = document.get("pending_attempt_receipts", [])
    if (
        not isinstance(receipts, list)
        or not receipts
        or len(receipts) != int(document["capture_attempt_count"])
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("path"), str)
            or not item.get("path")
            or not _is_sha256_text(item.get("sha256"))
            for item in receipts
        )
    ):
        return False, ("capture_capacity_attempt_receipts_missing",)
    if (
        not isinstance(pending_receipts, list)
        or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("path"), str)
            or not item.get("path")
            or not _is_sha256_text(item.get("sha256"))
            for item in pending_receipts
        )
        or len(pending_receipts) != int(document.get("pending_attempt_count", 0))
    ):
        return False, ("capture_capacity_attempt_receipts_missing",)
    seen_attempt_ids: set[str] = set()
    failure_count = 0
    pending_count = 0
    for item in receipts:
        try:
            receipt_artifact = load_verified_json_artifact(str(item["path"]))
        except ValueError:
            return False, ("capture_capacity_attempt_receipts_missing",)
        if receipt_artifact.sha256 != item["sha256"]:
            return False, ("capture_capacity_attempt_receipts_missing",)
        receipt_document = _load_bound_artifact_document(receipt_artifact)
        if receipt_document is None:
            return False, ("capture_capacity_attempt_receipts_missing",)
        attempt_id = receipt_document.get("attempt_id")
        status = receipt_document.get("status")
        if (
            receipt_document.get("schema_version")
            != "btc-twap-capture-attempt-receipt.v1"
            or not isinstance(attempt_id, str)
            or not attempt_id
            or attempt_id in seen_attempt_ids
            or receipt_document.get("track_id") != strict_track_id
            or status not in {"succeeded", "failed"}
            or not isinstance(receipt_document.get("created_at"), str)
            or _parse_utc_timestamp(receipt_document.get("created_at")) is None
            or not isinstance(receipt_document.get("terminal_at"), str)
            or _parse_utc_timestamp(receipt_document.get("terminal_at")) is None
        ):
            return False, ("capture_capacity_attempt_receipts_missing",)
        seen_attempt_ids.add(attempt_id)
        if status == "failed":
            failure_count += 1
    for item in pending_receipts:
        try:
            receipt_artifact = load_verified_json_artifact(str(item["path"]))
        except ValueError:
            return False, ("capture_capacity_attempt_receipts_missing",)
        if receipt_artifact.sha256 != item["sha256"]:
            return False, ("capture_capacity_attempt_receipts_missing",)
        receipt_document = _load_bound_artifact_document(receipt_artifact)
        if receipt_document is None:
            return False, ("capture_capacity_attempt_receipts_missing",)
        attempt_id = receipt_document.get("attempt_id")
        if (
            receipt_document.get("schema_version")
            != "btc-twap-capture-attempt-receipt.v1"
            or not isinstance(attempt_id, str)
            or not attempt_id
            or attempt_id in seen_attempt_ids
            or receipt_document.get("track_id") != strict_track_id
            or receipt_document.get("status") != "started"
            or not isinstance(receipt_document.get("created_at"), str)
            or _parse_utc_timestamp(receipt_document.get("created_at")) is None
        ):
            return False, ("capture_capacity_attempt_receipts_missing",)
        seen_attempt_ids.add(attempt_id)
        pending_count += 1
    if failure_count != int(document["capture_failure_count"]):
        return False, ("capture_capacity_artifact_invalid",)
    if pending_count != int(document.get("pending_attempt_count", 0)):
        return False, ("capture_capacity_artifact_invalid",)
    return True, ()


def _validated_daily_ledger_artifact(
    artifact: VerifiedJsonArtifact | None,
    *,
    evaluation_utc_now: datetime | None,
    max_age_days: int | None,
) -> tuple[dict[str, Decimal | int] | None, tuple[str, ...]]:
    if artifact is None:
        return None, ("daily_ledger_artifact_missing",)
    validated_now, now_reasons = _validated_utc_now(
        evaluation_utc_now,
        missing_reason="daily_ledger_evaluation_now_missing",
    )
    if now_reasons:
        return None, now_reasons
    validated_max_age_days, max_age_reasons = _validated_positive_int(
        max_age_days,
        invalid_reason="daily_ledger_max_age_invalid",
    )
    if max_age_reasons:
        return None, max_age_reasons
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(DAILY_LEDGER_ARTIFACT_SCHEMA,),
    )
    if document is None:
        return None, ("daily_ledger_artifact_invalid",)
    rows = document.get("rows")
    generated_at = document.get("generated_at")
    if not isinstance(rows, list) or not rows or not isinstance(generated_at, str):
        return None, ("daily_ledger_artifact_invalid",)
    try:
        parsed = sorted(
            (
                date.fromisoformat(str(row["utc_date"])),
                Decimal(str(row["net_pnl"])),
            )
            for row in rows
            if isinstance(row, Mapping)
        )
    except (KeyError, ValueError, TypeError):
        return None, ("daily_ledger_artifact_invalid",)
    if len(parsed) != len(rows):
        return None, ("daily_ledger_artifact_invalid",)
    if len({day for day, _pnl in parsed}) != len(parsed):
        return None, ("daily_ledger_artifact_invalid",)
    latest_day = _parse_utc_date_or_timestamp(generated_at)
    if latest_day is None:
        return None, ("daily_ledger_generated_at_invalid",)
    if latest_day > validated_now.date():
        return None, ("daily_ledger_generated_at_invalid",)
    if (validated_now.date() - latest_day).days > validated_max_age_days:
        return None, ("daily_ledger_artifact_stale",)
    tail = parsed[-MINIMUM_CONSECUTIVE_PROFITABLE_UTC_DAYS :]
    if len(tail) < MINIMUM_CONSECUTIVE_PROFITABLE_UTC_DAYS:
        return None, ("fewer_than_14_consecutive_profitable_utc_days",)
    expected_days = [
        latest_day.toordinal() - offset
        for offset in range(MINIMUM_CONSECUTIVE_PROFITABLE_UTC_DAYS - 1, -1, -1)
    ]
    if [day.toordinal() for day, _pnl in tail] != expected_days:
        return None, ("fewer_than_14_consecutive_profitable_utc_days",)
    minimum = min(pnl for _day, pnl in tail)
    if minimum < MINIMUM_DAILY_NET_PNL:
        return None, ("minimum_daily_net_pnl_below_20",)
    return {
        "consecutive_days": MINIMUM_CONSECUTIVE_PROFITABLE_UTC_DAYS,
        "minimum_daily_net_pnl": minimum,
    }, ()


def _validated_capital_ledger_artifact(
    artifact: VerifiedJsonArtifact | None,
) -> tuple[Decimal | None, tuple[str, ...]]:
    if artifact is None:
        return None, ("capital_ledger_artifact_missing",)
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(CAPITAL_LEDGER_ARTIFACT_SCHEMA,),
    )
    if document is None:
        return None, ("capital_ledger_artifact_invalid",)
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        return None, ("capital_ledger_artifact_invalid",)
    try:
        parsed = [
            Decimal(str(row["capital_deployed"])) for row in rows if isinstance(row, Mapping)
        ]
    except (KeyError, ValueError, TypeError):
        return None, ("capital_ledger_artifact_invalid",)
    if len(parsed) != len(rows) or any(not value.is_finite() or value < ZERO for value in parsed):
        return None, ("capital_ledger_artifact_invalid",)
    peak = max(parsed)
    if peak <= ZERO or peak > MAXIMUM_CAPITAL_DEPLOYED:
        return None, ("peak_capital_deployed_outside_0_to_2000",)
    return peak, ()


def _validated_execution_reconciliation_artifact(
    artifact: VerifiedJsonArtifact | None,
    *,
    expected_cohort_ids: set[str] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    if artifact is None:
        return False, ("execution_reconciliation_artifact_missing",)
    document = _load_bound_artifact_document(
        artifact,
        accepted_schema_versions=(EXECUTION_RECONCILIATION_ARTIFACT_SCHEMA,),
    )
    if document is None:
        return False, ("execution_reconciliation_artifact_invalid",)
    rows = document.get("terminal_rows")
    if not isinstance(rows, list) or not rows:
        return False, ("execution_reconciliation_artifact_invalid",)
    if not all(isinstance(row, Mapping) for row in rows):
        return False, ("execution_reconciliation_artifact_invalid",)
    cohort_ids = []
    for row in rows:
        cohort_id = row.get("cohort_id")
        if not isinstance(cohort_id, str) or not cohort_id:
            return False, ("execution_reconciliation_artifact_invalid",)
        cohort_ids.append(cohort_id)
    if len(cohort_ids) != len(set(cohort_ids)):
        return False, ("execution_reconciliation_artifact_invalid",)
    if expected_cohort_ids is not None and set(cohort_ids) != expected_cohort_ids:
        return False, ("execution_reconciliation_shadow_mismatch",)
    if not all(row.get("execution_complete") is True for row in rows):
        return False, ("complete_real_execution_evidence_missing",)
    if not all(row.get("included_in_pnl_distribution") is True for row in rows):
        return False, ("locked_no_trade_no_fill_cohorts_missing_from_distribution",)
    return True, ()


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
    verified_evidence_bundle: ExecutionProbeEvidenceBundle | None = None
    structural_authority_trust_policy: StructuralAuthorityTrustPolicy | None = None
    service_health_evaluation_utc_now: datetime | None = None
    service_health_max_age_seconds: int | None = None
    service_health_min_window_ms: int | None = None


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
            "capture_failure_rate_upper_bound_exclusive": str(
                CAPTURE_FAILURE_RATE_UPPER_BOUND_EXCLUSIVE
            ),
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
    if failure_rate >= CAPTURE_FAILURE_RATE_UPPER_BOUND_EXCLUSIVE:
        reasons.append("capture_failure_rate_not_below_5pct")
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


def _coerce_neutral_shadow_evidence(
    structural_shadow_report: object | None,
) -> tuple[NeutralShadowEvidence | None, tuple[str, ...]]:
    if structural_shadow_report is None:
        return None, ("neutral_shadow_evidence_unverified",)
    if hasattr(structural_shadow_report, "neutral_shadow_evidence") and hasattr(
        structural_shadow_report,
        "neutral_robustness",
    ):
        report_document = structural_shadow_report.to_document()
        unsigned_report = {
            key: value for key, value in report_document.items() if key != "report_sha256"
        }
        if report_document.get("report_sha256") != sha256(
            canonical_json_bytes(unsigned_report)
        ).hexdigest():
            return None, ("neutral_shadow_report_hash_invalid",)
        if getattr(structural_shadow_report.neutral_robustness, "passed", False) is not True:
            return None, ("neutral_shadow_report_robustness_failed",)
        evidence = structural_shadow_report.neutral_shadow_evidence
        if not isinstance(evidence, NeutralShadowEvidence):
            return None, ("neutral_shadow_report_unverified",)
        return evidence, ()
    if isinstance(structural_shadow_report, Mapping):
        document = dict(structural_shadow_report)
        if document.get("schema_version") not in STRUCTURAL_SHADOW_REPORT_SCHEMAS:
            return None, ("neutral_shadow_report_unverified",)
        unsigned = {
            key: value for key, value in document.items() if key != "report_sha256"
        }
        if document.get("report_sha256") != sha256(
            canonical_json_bytes(unsigned)
        ).hexdigest():
            return None, ("neutral_shadow_report_hash_invalid",)
        robustness = document.get("neutral_robustness")
        if not isinstance(robustness, Mapping) or robustness.get("passed") is not True:
            return None, ("neutral_shadow_report_robustness_failed",)
        evidence_document = document.get("neutral_shadow_evidence")
        if not isinstance(evidence_document, Mapping):
            return None, ("neutral_shadow_report_unverified",)
        try:
            return (
                NeutralShadowEvidence(
                    realized_net_pnl=evidence_document.get("realized_net_pnl"),
                    receipt_chain_verified=bool(
                        evidence_document.get("receipt_chain_verified")
                    ),
                    all_admitted_cohorts_included=bool(
                        evidence_document.get("all_admitted_cohorts_included")
                    ),
                    scenario=str(evidence_document.get("scenario", "neutral")),
                    expiry_count=int(evidence_document.get("expiry_count", 0)),
                    without_best_expiry_net_pnl=evidence_document.get(
                        "without_best_expiry_net_pnl"
                    ),
                    without_best_direction_net_pnl=evidence_document.get(
                        "without_best_direction_net_pnl"
                    ),
                    minimum_rolling_window_net_pnl=evidence_document.get(
                        "minimum_rolling_window_net_pnl"
                    ),
                    max_single_expiry_pnl_concentration=evidence_document.get(
                        "max_single_expiry_pnl_concentration"
                    ),
                ),
                (),
            )
        except (TypeError, ValueError):
            return None, ("neutral_shadow_report_unverified",)
    return None, ("neutral_shadow_report_unverified",)


def evaluate_execution_probe_readiness(
    *,
    structural_shadow_report: object | None,
    clean_common_terminal_cohort_count: int,
    coverage_results: Sequence[CohortCoverageResult],
    structural_floor: StructuralFloorVerdict | None,
    prerequisites: ExecutionProbePrerequisites = DEFAULT_PROBE_PREREQUISITES,
) -> ExecutionProbeReadiness:
    reasons: list[str] = []
    unique_complete_cohort_count = len(
        {result.cohort_id for result in coverage_results if result.complete}
    )
    bundle = prerequisites.verified_evidence_bundle
    neutral_shadow_evidence: NeutralShadowEvidence | None = None
    if bundle is None:
        reasons.append("probe_verified_evidence_bundle_missing")
    gate_0_report, gate_0_reasons = _validated_gate_0_report_document(
        None if bundle is None else bundle.gate_0_report_artifact
    )
    reasons.extend(gate_0_reasons)
    shadow_document = None
    if bundle is not None and bundle.structural_shadow_artifact is not None:
        shadow_document, shadow_reasons = _validated_structural_shadow_document(
            bundle.structural_shadow_artifact,
            trust_policy=prerequisites.structural_authority_trust_policy,
        )
        reasons.extend(shadow_reasons)
    else:
        reasons.append("neutral_shadow_artifact_missing")
        if structural_shadow_report is not None:
            reasons.append("neutral_shadow_object_fallback_rejected")
    if shadow_document is not None:
        neutral_shadow_evidence, shadow_reasons = _coerce_neutral_shadow_evidence(
            shadow_document
        )
        reasons.extend(shadow_reasons)
    if neutral_shadow_evidence is not None:
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
    _service_ok, service_reasons = _validated_service_health_artifact(
        None if bundle is None else bundle.service_health_artifact,
        evaluation_utc_now=prerequisites.service_health_evaluation_utc_now,
        max_age_seconds=prerequisites.service_health_max_age_seconds,
        minimum_window_ms=prerequisites.service_health_min_window_ms,
    )
    reasons.extend(service_reasons)
    _auth_ok, auth_reasons = _validated_receipt_artifact(
        None if bundle is None else bundle.authenticated_read_artifact,
        schema=AUTHENTICATED_READ_ARTIFACT_SCHEMA,
        required_flag="authenticated_read_verified",
        missing_reason="authenticated_read_artifact_missing",
        invalid_reason="authenticated_read_artifact_invalid",
        false_reason="authenticated_read_unverified",
        missing_receipt_reason="authenticated_read_receipt_missing",
    )
    reasons.extend(auth_reasons)
    _fill_ok, fill_reasons = _validated_receipt_artifact(
        None if bundle is None else bundle.fill_stream_artifact,
        schema=FILL_STREAM_ARTIFACT_SCHEMA,
        required_flag="fill_stream_verified",
        missing_reason="fill_stream_artifact_missing",
        invalid_reason="fill_stream_artifact_invalid",
        false_reason="fill_stream_unverified",
        missing_receipt_reason="fill_stream_receipt_missing",
    )
    reasons.extend(fill_reasons)
    _drill_ok, drill_reasons = _validated_fault_drills_artifact(
        None if bundle is None else bundle.fault_drills_artifact
    )
    reasons.extend(drill_reasons)
    if not prerequisites.immutable_probe_preregistration_present:
        reasons.append("immutable_probe_preregistration_missing")
    _adapter_ok, adapter_reasons = _validated_probe_adapter_artifact(
        None if bundle is None else bundle.probe_adapter_artifact
    )
    reasons.extend(adapter_reasons)
    _capacity_ok, capacity_reasons = _validated_capture_capacity_artifact(
        None if bundle is None else bundle.capture_capacity_artifact
    )
    reasons.extend(capacity_reasons)
    if gate_0_report is None:
        reasons.append("gate_0_not_passed")
    else:
        if gate_0_report.get("decision") != "PASS" or gate_0_report.get("gate_0_passed") is not True:
            reasons.append("gate_0_not_passed")
        if gate_0_report.get("rerun_required") is True:
            reasons.append("gate_0_rerun_required")
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
    consecutive_profitable_utc_days: int
    minimum_daily_net_pnl: Decimal | None
    peak_capital_deployed: Decimal | None
    verified_evidence_bundle: StrategyLiveEvidenceBundle | None = None
    structural_authority_trust_policy: StructuralAuthorityTrustPolicy | None = None
    service_health_evaluation_utc_now: datetime | None = None
    service_health_max_age_seconds: int | None = None
    service_health_min_window_ms: int | None = None
    daily_ledger_evaluation_utc_now: datetime | None = None
    daily_ledger_max_age_days: int | None = None


def evaluate_strategy_live_readiness_inputs(
    inputs: StrategyLiveInputs,
) -> StrategyLiveReadiness:
    reasons: list[str] = []
    bundle = inputs.verified_evidence_bundle
    if bundle is None:
        reasons.append("strategy_live_verified_evidence_bundle_missing")
    if not inputs.builder_verified_evidence_chain:
        reasons.append("builder_verified_evidence_chain_missing")
    if not inputs.auditable_prelabel_lock_evidence:
        reasons.append("prelabel_lock_evidence_missing")
    if inputs.clean_prelabeled_common_terminal_cohort_count < MINIMUM_SETTLED_CLUSTERS:
        reasons.append("fewer_than_200_clean_prelabeled_common_terminal_cohorts")
    if inputs.structural_settled_expiry_cluster_count < MINIMUM_SETTLED_CLUSTERS:
        reasons.append("structural_fewer_than_200_distinct_settled_expiry_clusters")
    if (
        inputs.structural_explainable_economic_attempt_count
        < MINIMUM_EXPLAINABLE_ECONOMIC_ATTEMPTS
    ):
        reasons.append(
            "structural_fewer_than_200_explainable_locked_oos_economic_attempts"
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
    gate_0_report, gate_0_reasons = _validated_gate_0_report_document(
        None if bundle is None else bundle.gate_0_report_artifact
    )
    reasons.extend(gate_0_reasons)
    if inputs.structural_gate_0_passed and (
        gate_0_report is None or gate_0_report.get("decision") != "PASS"
    ):
        reasons.append("structural_gate_0_not_passed")
    shadow_document, shadow_reasons = _validated_structural_shadow_document(
        None if bundle is None else bundle.structural_shadow_artifact,
        trust_policy=inputs.structural_authority_trust_policy,
    )
    reasons.extend(shadow_reasons)
    expected_shadow_cohort_ids: set[str] | None = None
    if shadow_document is None:
        reasons.append("structural_shadow_artifact_unverified")
    else:
        evidence = shadow_document.get("neutral_shadow_evidence")
        robustness = shadow_document.get("neutral_robustness")
        if not isinstance(evidence, Mapping) or not isinstance(robustness, Mapping):
            reasons.append("structural_shadow_artifact_unverified")
        else:
            if evidence.get("expiry_count", 0) < MINIMUM_STRUCTURAL_SHADOW_EXPIRIES:
                reasons.append("fewer_than_200_neutral_shadow_expiries")
            if robustness.get("passed") is not True:
                reasons.append("neutral_shadow_report_robustness_failed")
            if (
                evidence.get("max_single_expiry_pnl_concentration") is None
                or Decimal(str(evidence["max_single_expiry_pnl_concentration"]))
                > MAXIMUM_SINGLE_EXPIRY_PNL_CONCENTRATION
            ):
                reasons.append("structural_single_expiry_pnl_concentration_above_20_percent")
            if (
                evidence.get("realized_net_pnl") is None
                or Decimal(str(evidence["realized_net_pnl"])) <= ZERO
            ):
                reasons.append("structural_qualified_net_pnl_not_positive")
        expected_shadow_cohort_ids = {
            str(item.get("attempt_id"))
            for key in ("attempts", "locked_zero_cohorts")
            for item in shadow_document.get(key, [])
            if isinstance(item, Mapping) and isinstance(item.get("attempt_id"), str)
        }
    if not inputs.service_continuously_healthy:
        reasons.append("service_continuous_health_unverified")
    if (
        inputs.consecutive_profitable_utc_days
        < MINIMUM_CONSECUTIVE_PROFITABLE_UTC_DAYS
    ):
        reasons.append("fewer_than_14_consecutive_profitable_utc_days")
    if (
        inputs.minimum_daily_net_pnl is None
        or inputs.minimum_daily_net_pnl < MINIMUM_DAILY_NET_PNL
    ):
        reasons.append("minimum_daily_net_pnl_below_20")
    if (
        inputs.peak_capital_deployed is None
        or inputs.peak_capital_deployed <= ZERO
        or inputs.peak_capital_deployed > MAXIMUM_CAPITAL_DEPLOYED
    ):
        reasons.append("peak_capital_deployed_outside_0_to_2000")
    if not inputs.complete_real_execution_evidence:
        reasons.append("complete_real_execution_evidence_missing")
    if not inputs.all_locked_cohorts_in_pnl_distribution:
        reasons.append("locked_no_trade_no_fill_cohorts_missing_from_distribution")
    _service_ok, service_reasons = _validated_service_health_artifact(
        None if bundle is None else bundle.service_health_artifact,
        evaluation_utc_now=inputs.service_health_evaluation_utc_now,
        max_age_seconds=inputs.service_health_max_age_seconds,
        minimum_window_ms=inputs.service_health_min_window_ms,
    )
    reasons.extend(service_reasons)
    _daily_metrics, daily_reasons = _validated_daily_ledger_artifact(
        None if bundle is None else bundle.daily_ledger_artifact,
        evaluation_utc_now=inputs.daily_ledger_evaluation_utc_now,
        max_age_days=inputs.daily_ledger_max_age_days,
    )
    reasons.extend(daily_reasons)
    _peak_capital, capital_reasons = _validated_capital_ledger_artifact(
        None if bundle is None else bundle.capital_ledger_artifact
    )
    reasons.extend(capital_reasons)
    _reconciliation_ok, reconciliation_reasons = _validated_execution_reconciliation_artifact(
        None if bundle is None else bundle.execution_reconciliation_artifact,
        expected_cohort_ids=expected_shadow_cohort_ids,
    )
    reasons.extend(reconciliation_reasons)
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
            consecutive_profitable_utc_days=0,
            minimum_daily_net_pnl=None,
            peak_capital_deployed=None,
        )
    )


__all__ = [
    "EXPECTED_RTDS_INTERVAL_MS",
    "MAXIMUM_RTDS_GAP_MS",
    "MINIMUM_STRUCTURAL_SHADOW_EXPIRIES",
    "PROBE_ALL_IN_LIMIT",
    "CaptureCapacityEvidence",
    "CaptureCapacityVerdict",
    "CohortCoverageInput",
    "CohortCoverageResult",
    "ExecutionProbePrerequisites",
    "ExecutionProbeReadiness",
    "NeutralShadowEvidence",
    "PerfectInformationAttempt",
    "PerfectInformationAttemptUpperBound",
    "PerfectInformationBreakpoint",
    "PerfectInformationUpperBoundReport",
    "StrategyLiveInputs",
    "StrategyLiveReadiness",
    "StructuralAuthorityTrustPolicy",
    "StructuralFloorLevel",
    "StructuralFloorVerdict",
    "evaluate_capture_capacity",
    "evaluate_execution_probe_readiness",
    "evaluate_perfect_information_upper_bound",
    "evaluate_strategy_live_readiness",
    "evaluate_strategy_live_readiness_inputs",
    "validate_cohort_data_coverage",
    "validate_structural_floor",
]
