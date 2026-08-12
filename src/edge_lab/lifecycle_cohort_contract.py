"""Shared exact contract for prospective lifecycle cohort commitments."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .data_store import canonical_json_bytes


ROOT_EVENT_TYPE = "lifecycle_cohort_committed"
REMEDIATION_EVENT_TYPE = "lifecycle_remediation_cohort_committed"
ROOT_COHORT_SCHEMA = (
    "edge-lab.phase2-lifecycle-prospective-cohort.v1"
)
REMEDIATION_COHORT_SCHEMA = (
    "edge-lab.phase2-lifecycle-remediation-cohort.v1"
)
REMEDIATION_PLAN_SCHEMA = (
    "edge-lab.phase2-lifecycle-remediation-plan.v1"
)
REMEDIATION_REASON_CODE = "scheduler_starved_by_gamma_sweep"
SELECTION_RULE = "first_mature_targets_opening_at_or_after_boundary"
_SELECTION_ORDER = ("opens_at_ms", "closes_at_ms", "slug")
_ASSETS = ("BTC", "ETH")
_HORIZONS = ("5m", "15m")
CONTENT_HASH = re.compile(r"[0-9a-f]{64}")

_COMMON_FIELDS = frozenset({
    "scheduler_now_ms",
    "schema_version",
    "selection_rule",
    "eligibility_start_ms",
    "sample_size",
    "threshold",
    "settlement_timeout_ms",
    "assets",
    "horizons",
    "selection_order",
    "must_be_durable_before_public_network",
    "actual_fill",
    "authenticated_fill",
    "orders_submitted",
    "authenticated_endpoints_used",
})
_ROOT_FIELDS = _COMMON_FIELDS | frozenset({
    "earliest_valid_commitment_wins",
})
_REMEDIATION_FIELDS = _COMMON_FIELDS | frozenset({
    "predecessor_commitment_record_id",
    "remediation_reason_code",
    "remediation_plan_id",
    "append_only",
    "prior_failure_retained",
})


class LifecycleCohortContractError(ValueError):
    """A commitment or remediation plan is not the exact frozen contract."""


def is_content_hash(value: Any) -> bool:
    return type(value) is str and CONTENT_HASH.fullmatch(value) is not None


def remediation_plan_id(predecessor_record_id: str) -> str:
    if not is_content_hash(predecessor_record_id):
        raise LifecycleCohortContractError(
            "remediation predecessor must be an exact content hash"
        )
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema_version": REMEDIATION_PLAN_SCHEMA,
                "predecessor_commitment_record_id": (
                    predecessor_record_id
                ),
                "remediation_reason_code": REMEDIATION_REASON_CODE,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class LifecycleRemediationPlan:
    """One immutable request to append a cohort after a failed chain tip."""

    predecessor_commitment_record_id: str
    remediation_reason_code: str
    plan_id: str

    SCHEMA_VERSION = REMEDIATION_PLAN_SCHEMA

    @classmethod
    def create(
        cls,
        predecessor_commitment_record_id: str,
        *,
        remediation_reason_code: str = REMEDIATION_REASON_CODE,
    ) -> "LifecycleRemediationPlan":
        if (
            type(predecessor_commitment_record_id) is not str
            or type(remediation_reason_code) is not str
        ):
            raise ValueError(
                "remediation plan fields must be native exact strings"
            )
        if remediation_reason_code != REMEDIATION_REASON_CODE:
            raise ValueError("remediation reason is not approved")
        return cls(
            predecessor_commitment_record_id=(
                predecessor_commitment_record_id
            ),
            remediation_reason_code=remediation_reason_code,
            plan_id=remediation_plan_id(
                predecessor_commitment_record_id
            ),
        )

    @classmethod
    def from_document(
        cls,
        value: Mapping[str, Any],
    ) -> "LifecycleRemediationPlan":
        expected_fields = {
            "schema_version",
            "predecessor_commitment_record_id",
            "remediation_reason_code",
            "plan_id",
        }
        if (
            set(value) != expected_fields
            or any(
                type(value.get(field)) is not str
                for field in expected_fields
            )
        ):
            raise ValueError(
                "remediation plan fields must be native exact strings"
            )
        plan = cls(
            predecessor_commitment_record_id=value[
                "predecessor_commitment_record_id"
            ],
            remediation_reason_code=value["remediation_reason_code"],
            plan_id=value["plan_id"],
        )
        if (
            value["schema_version"] != cls.SCHEMA_VERSION
            or plan
            != cls.create(
                plan.predecessor_commitment_record_id,
                remediation_reason_code=plan.remediation_reason_code,
            )
        ):
            raise ValueError("remediation plan is not canonical")
        return plan

    def __post_init__(self) -> None:
        if any(
            type(value) is not str
            for value in (
                self.predecessor_commitment_record_id,
                self.remediation_reason_code,
                self.plan_id,
            )
        ):
            raise ValueError(
                "remediation plan fields must be native exact strings"
            )
        if not is_content_hash(self.predecessor_commitment_record_id):
            raise ValueError(
                "remediation predecessor must be an exact content hash"
            )
        if self.remediation_reason_code != REMEDIATION_REASON_CODE:
            raise ValueError("remediation reason is not approved")
        if not is_content_hash(self.plan_id):
            raise ValueError("remediation plan_id must be a content hash")
        if self.plan_id != remediation_plan_id(
            self.predecessor_commitment_record_id
        ):
            raise ValueError("remediation plan_id is not canonical")

    def to_document(self) -> dict[str, str]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "predecessor_commitment_record_id": (
                self.predecessor_commitment_record_id
            ),
            "remediation_reason_code": self.remediation_reason_code,
            "plan_id": self.plan_id,
        }


def validate_cohort_payload(
    event_type: str,
    value: Any,
    *,
    outer_kind: Any,
    record_id: str,
    received_at_ms: int,
    settlement_timeout_ms: int,
    sample_size: int = 20,
    threshold: Decimal = Decimal("0.8"),
) -> dict[str, Any]:
    """Return one exact safe payload or raise without coercing any field."""

    if outer_kind != "command" or type(outer_kind) is not str:
        raise LifecycleCohortContractError(
            f"{record_id} commitment outer kind is not command"
        )
    if not is_content_hash(record_id):
        raise LifecycleCohortContractError(
            "commitment record_id is not an exact content hash"
        )
    if not isinstance(value, Mapping):
        raise LifecycleCohortContractError(
            f"{record_id} commitment payload is not an object"
        )
    if event_type == ROOT_EVENT_TYPE:
        expected_fields = _ROOT_FIELDS
        expected_schema = ROOT_COHORT_SCHEMA
    elif event_type == REMEDIATION_EVENT_TYPE:
        expected_fields = _REMEDIATION_FIELDS
        expected_schema = REMEDIATION_COHORT_SCHEMA
    else:
        raise LifecycleCohortContractError(
            f"unsupported cohort event type: {event_type}"
        )
    scheduler_now_ms = value.get("scheduler_now_ms")
    eligibility_start_ms = value.get("eligibility_start_ms")
    valid = (
        set(value) == expected_fields
        and value.get("schema_version") == expected_schema
        and type(value.get("schema_version")) is str
        and value.get("selection_rule") == SELECTION_RULE
        and type(value.get("selection_rule")) is str
        and type(scheduler_now_ms) is int
        and 0 <= scheduler_now_ms <= received_at_ms
        and type(eligibility_start_ms) is int
        and eligibility_start_ms > received_at_ms
        and eligibility_start_ms % (5 * 60 * 1_000) == 0
        and type(value.get("sample_size")) is int
        and value.get("sample_size") == sample_size
        and type(value.get("threshold")) is str
        and value.get("threshold") == format(threshold.normalize(), "f")
        and type(value.get("settlement_timeout_ms")) is int
        and value.get("settlement_timeout_ms") == settlement_timeout_ms
        and type(value.get("assets")) is list
        and value.get("assets") == list(_ASSETS)
        and type(value.get("horizons")) is list
        and value.get("horizons") == list(_HORIZONS)
        and type(value.get("selection_order")) is list
        and value.get("selection_order") == list(_SELECTION_ORDER)
        and value.get("must_be_durable_before_public_network") is True
        and value.get("actual_fill") is False
        and value.get("authenticated_fill") is False
        and type(value.get("orders_submitted")) is int
        and value.get("orders_submitted") == 0
        and type(value.get("authenticated_endpoints_used")) is int
        and value.get("authenticated_endpoints_used") == 0
    )
    if event_type == ROOT_EVENT_TYPE:
        valid = (
            valid
            and value.get("earliest_valid_commitment_wins") is True
        )
    else:
        predecessor = value.get("predecessor_commitment_record_id")
        plan_id = value.get("remediation_plan_id")
        valid = (
            valid
            and is_content_hash(predecessor)
            and value.get("remediation_reason_code")
            == REMEDIATION_REASON_CODE
            and type(value.get("remediation_reason_code")) is str
            and is_content_hash(plan_id)
            and (
                plan_id == remediation_plan_id(predecessor)
                if is_content_hash(predecessor)
                else False
            )
            and value.get("append_only") is True
            and value.get("prior_failure_retained") is True
        )
    if not valid:
        raise LifecycleCohortContractError(
            f"{record_id} is not an exact {event_type} commitment"
        )
    return deepcopy(dict(value))
