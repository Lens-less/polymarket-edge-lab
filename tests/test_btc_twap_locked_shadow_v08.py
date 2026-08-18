from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.edge_lab.btc_twap_locked_shadow_v08 import (
    RollingCohortAdmission,
    RollingCohortOutcome,
    RollingDecisionReceipt,
    RollingInclusionPolicy,
    admit_rolling_cohort,
    append_rolling_decision,
    audit_rolling_shadow,
    finalize_rolling_cohort,
    initialize_rolling_shadow,
)
from src.edge_lab.btc_twap_relative_value_v07 import canonical_expiry_cluster_id

START_MS = 1_800_000_000_000


def _policy() -> RollingInclusionPolicy:
    return RollingInclusionPolicy(
        preregistration_sha256="a" * 64,
        track_starts_at_ms=START_MS,
        decision_tau_seconds=(240, 180, 120, 60),
        locked_at_ms=START_MS - 2_000,
        received_at_ms=START_MS - 1_000,
        monotonic_ns=1,
    )


def _admission(index: int, *, attempt: str | None = None) -> RollingCohortAdmission:
    expiry_ms = START_MS + index * 900_000
    return RollingCohortAdmission(
        common_expiry_id=canonical_expiry_cluster_id(expiry_ms),
        canonical_pair_id=f"pair-{index}",
        expiry_ms=expiry_ms,
        capture_attempt_id=attempt or f"attempt-{index}-first",
        market_5_id=f"market-5-{index}",
        market_15_id=f"market-15-{index}",
        condition_5_id=f"condition-5-{index}",
        condition_15_id=f"condition-15-{index}",
        discovered_at_ms=expiry_ms - 300_000,
        received_at_ms=expiry_ms - 299_000,
        monotonic_ns=10 + index,
    )


def test_rolling_policy_admits_future_cohorts_without_a_fixed_universe_cap(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    initialize_rolling_shadow(root, _policy())

    for index in range(1, 105):
        admit_rolling_cohort(root, _admission(index))

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is True
    assert audit["admitted_common_expiry_count"] == 104
    assert audit["unfinished_common_expiry_count"] == 104
    assert audit["policy"]["rolling_without_fixed_universe_cap"] is True


def test_first_attempt_is_permanent_and_cannot_be_posthoc_replaced(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    initialize_rolling_shadow(root, _policy())
    first = _admission(1)
    admit_rolling_cohort(root, first)

    with pytest.raises(ValueError, match="replacement is forbidden"):
        admit_rolling_cohort(
            root,
            RollingCohortAdmission(
                **{**first.__dict__, "capture_attempt_id": "attempt-1-later-clean"}
            ),
        )

    audit = audit_rolling_shadow(root)
    assert audit["admissions"][first.common_expiry_id]["capture_attempt_id"] == (
        "attempt-1-first"
    )


def test_no_trade_no_fill_and_dirty_outcomes_remain_in_denominator(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    initialize_rolling_shadow(root, _policy())
    admission = _admission(1)
    admit_rolling_cohort(root, admission)
    decision_at_ms = admission.expiry_ms - 60_000
    append_rolling_decision(
        root,
        RollingDecisionReceipt(
            common_expiry_id=admission.common_expiry_id,
            decision_tau_seconds=60,
            decision_at_ms=decision_at_ms,
            receipt_received_at_ms=decision_at_ms + 500,
            monotonic_ns=20,
            action="no_trade",
            action_payload_sha256="b" * 64,
        ),
    )
    finalize_rolling_cohort(
        root,
        RollingCohortOutcome(
            common_expiry_id=admission.common_expiry_id,
            outcome="rtds_gap_dirty",
            clean=False,
            finalized_at_ms=admission.expiry_ms + 1_000,
            received_at_ms=admission.expiry_ms + 1_100,
            monotonic_ns=30,
            realized_net_pnl=None,
            evidence_sha256="c" * 64,
        ),
    )

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is True
    assert audit["admitted_common_expiry_count"] == 1
    assert audit["finalized_common_expiry_count"] == 1
    assert audit["clean_common_expiry_count"] == 0
    assert audit["dirty_common_expiry_count"] == 1
    assert audit["outcome_counts"]["rtds_gap_dirty"] == 1
    assert audit["denominator_includes_no_trade_no_fill_and_dirty"] is True


def test_receipt_tampering_breaks_the_audit_chain(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    initialize_rolling_shadow(root, _policy())
    admit_rolling_cohort(root, _admission(1))
    receipt = sorted((root / "receipts").glob("*.json"))[1]
    os.chmod(receipt, 0o600)
    document = json.loads(receipt.read_text(encoding="utf-8"))
    document["body"]["admission"]["capture_attempt_id"] = "posthoc-replacement"
    receipt.write_text(json.dumps(document), encoding="utf-8")

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is False
    assert "hash_mismatch_at_2" in audit["errors"]
