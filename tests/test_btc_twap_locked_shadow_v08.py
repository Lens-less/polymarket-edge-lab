from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import src.edge_lab.btc_twap_locked_shadow_v08 as locked_shadow
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
from src.edge_lab.data_store import canonical_json_bytes

START_MS = 1_800_000_000_000


def _policy() -> RollingInclusionPolicy:
    return RollingInclusionPolicy(
        preregistration_sha256="a" * 64,
        track_starts_at_ms=START_MS,
        decision_tau_seconds=(240, 180, 120, 60),
        locked_at_ms=START_MS - 2_000,
        received_at_ms=START_MS - 1_000,
        monotonic_ns=1,
        settlement_grace_seconds=360,
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


def _install_writer_clock(monkeypatch, *samples: tuple[int, int]) -> None:
    iterator = iter(samples)
    monkeypatch.setattr(
        locked_shadow,
        "_sample_writer_clock",
        lambda: next(iterator),
    )


@pytest.mark.timeout(300)
def test_rolling_policy_admits_future_cohorts_without_a_fixed_universe_cap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    # This test covers policy cardinality, not storage latency. Real fsync on
    # every receipt can dominate the suite on Windows; production code and the
    # integrity-focused tests below retain the real call.
    monkeypatch.setattr(locked_shadow.os, "fsync", lambda _descriptor: None)
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        *[
            (_admission(index).received_at_ms + 1, 1_000 + index)
            for index in range(1, 105)
        ],
    )
    initialize_rolling_shadow(root, policy)

    for index in range(1, 105):
        admit_rolling_cohort(root, _admission(index))

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is True
    assert audit["admitted_common_expiry_count"] == 104
    assert audit["unfinished_common_expiry_count"] == 104
    assert audit["policy"]["rolling_without_fixed_universe_cap"] is True


def test_first_attempt_is_permanent_and_cannot_be_posthoc_replaced(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    first = _admission(1)
    _install_writer_clock(
        monkeypatch,
        (_policy().received_at_ms + 1, 1_000),
        (first.received_at_ms + 1, 1_001),
    )
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
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    decision_at_ms = admission.expiry_ms - 60_000
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
        (decision_at_ms + 501, 1_002),
        (admission.expiry_ms + 1_100, 1_003),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)
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


def test_receipt_tampering_breaks_the_audit_chain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)
    receipt = sorted((root / "receipts").glob("*.json"))[1]
    os.chmod(receipt, 0o600)
    document = json.loads(receipt.read_text(encoding="utf-8"))
    document["body"]["admission"]["capture_attempt_id"] = "posthoc-replacement"
    receipt.write_text(json.dumps(document), encoding="utf-8")

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is False
    assert "hash_mismatch_at_2" in audit["errors"]


def test_decision_receipt_cannot_predate_decision_time(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)
    decision_at_ms = admission.expiry_ms - 60_000

    with pytest.raises(ValueError, match="decision receipt cannot precede decision time"):
        append_rolling_decision(
            root,
            RollingDecisionReceipt(
                common_expiry_id=admission.common_expiry_id,
                decision_tau_seconds=60,
                decision_at_ms=decision_at_ms,
                receipt_received_at_ms=decision_at_ms - 1,
                monotonic_ns=20,
                action="no_trade",
                action_payload_sha256="b" * 64,
            ),
        )


def test_audit_rejects_tampered_decision_timeline_even_with_recomputed_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    decision_at_ms = admission.expiry_ms - 60_000
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
        (decision_at_ms + 1, 1_002),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)
    append_rolling_decision(
        root,
        RollingDecisionReceipt(
            common_expiry_id=admission.common_expiry_id,
            decision_tau_seconds=60,
            decision_at_ms=decision_at_ms,
            receipt_received_at_ms=decision_at_ms + 1,
            monotonic_ns=20,
            action="no_trade",
            action_payload_sha256="b" * 64,
        ),
    )

    decision_receipt = min((root / "receipts").glob("*decision_locked.json"))
    os.chmod(decision_receipt, 0o600)
    document = json.loads(decision_receipt.read_text(encoding="utf-8"))
    document["body"]["decision"]["receipt_received_at_ms"] = decision_at_ms - 1
    unsigned = {key: value for key, value in document.items() if key != "receipt_sha256"}
    document["receipt_sha256"] = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    decision_receipt.write_text(json.dumps(document), encoding="utf-8")

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is False
    assert "decision_invalid_at_3" in audit["errors"]


def test_supporting_history_capture_attempt_ids_are_locked_on_first_admission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1, attempt="attempt-1-primary")
    admission = RollingCohortAdmission(
        **{
            **admission.__dict__,
            "supporting_history_capture_attempt_ids": (
                "history-1",
                "history-2",
            ),
        }
    )
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)

    with pytest.raises(ValueError, match="replacement is forbidden"):
        admit_rolling_cohort(
            root,
            RollingCohortAdmission(
                **{
                    **admission.__dict__,
                    "supporting_history_capture_attempt_ids": ("history-3",),
                }
            ),
        )

    audit = audit_rolling_shadow(root)
    assert audit["admissions"][admission.common_expiry_id][
        "supporting_history_capture_attempt_ids"
    ] == ["history-1", "history-2"]


def test_append_lock_rejects_concurrent_writer(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    root.mkdir()
    (root / ".append.lock").write_text("held", encoding="utf-8")

    with pytest.raises(ValueError, match="append lock is already held"):
        initialize_rolling_shadow(root, _policy())


def test_writer_rejects_wall_clock_regression_without_clamping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = RollingInclusionPolicy(
        preregistration_sha256="a" * 64,
        track_starts_at_ms=100_000,
        decision_tau_seconds=(60,),
        locked_at_ms=1_000,
        received_at_ms=2_000,
        monotonic_ns=1,
        settlement_grace_seconds=360,
    )
    admission = RollingCohortAdmission(
        common_expiry_id=canonical_expiry_cluster_id(180_000),
        canonical_pair_id="pair-1",
        expiry_ms=180_000,
        capture_attempt_id="attempt-1",
        market_5_id="market-5-1",
        market_15_id="market-15-1",
        condition_5_id="condition-5-1",
        condition_15_id="condition-15-1",
        discovered_at_ms=10_000,
        received_at_ms=20_000,
        monotonic_ns=2,
    )
    _install_writer_clock(
        monkeypatch,
        (30_000, 9_000),
        (25_000, 9_001),
    )
    initialize_rolling_shadow(root, policy)

    with pytest.raises(ValueError, match="writer wall clock regressed"):
        admit_rolling_cohort(root, admission)


def test_writer_rejects_monotonic_regression_without_clamping(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = RollingInclusionPolicy(
        preregistration_sha256="a" * 64,
        track_starts_at_ms=100_000,
        decision_tau_seconds=(60,),
        locked_at_ms=1_000,
        received_at_ms=2_000,
        monotonic_ns=1,
        settlement_grace_seconds=360,
    )
    admission = RollingCohortAdmission(
        common_expiry_id=canonical_expiry_cluster_id(180_000),
        canonical_pair_id="pair-1",
        expiry_ms=180_000,
        capture_attempt_id="attempt-1",
        market_5_id="market-5-1",
        market_15_id="market-15-1",
        condition_5_id="condition-5-1",
        condition_15_id="condition-15-1",
        discovered_at_ms=10_000,
        received_at_ms=20_000,
        monotonic_ns=2,
    )
    _install_writer_clock(
        monkeypatch,
        (30_000, 9_000),
        (30_001, 8_999),
    )
    initialize_rolling_shadow(root, policy)

    with pytest.raises(ValueError, match="writer monotonic clock regressed"):
        admit_rolling_cohort(root, admission)


def test_decision_writer_clock_forward_jump_is_rejected(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = RollingInclusionPolicy(
        preregistration_sha256="a" * 64,
        track_starts_at_ms=100_000,
        decision_tau_seconds=(60,),
        locked_at_ms=1_000,
        received_at_ms=2_000,
        monotonic_ns=1,
        settlement_grace_seconds=360,
    )
    admission = RollingCohortAdmission(
        common_expiry_id=canonical_expiry_cluster_id(180_000),
        canonical_pair_id="pair-1",
        expiry_ms=180_000,
        capture_attempt_id="attempt-1",
        market_5_id="market-5-1",
        market_15_id="market-15-1",
        condition_5_id="condition-5-1",
        condition_15_id="condition-15-1",
        discovered_at_ms=10_000,
        received_at_ms=20_000,
        monotonic_ns=2,
    )
    decision = RollingDecisionReceipt(
        common_expiry_id=admission.common_expiry_id,
        decision_tau_seconds=60,
        decision_at_ms=120_000,
        receipt_received_at_ms=120_001,
        monotonic_ns=3,
        action="no_trade",
        action_payload_sha256="b" * 64,
    )
    _install_writer_clock(
        monkeypatch,
        (5_000, 9_000),
        (30_000, 9_001),
        (180_000, 9_002),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)

    with pytest.raises(ValueError, match="writer-recorded strictly pre-label"):
        append_rolling_decision(root, decision)


def test_late_admission_source_receipt_is_rejected_without_poisoning_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    earliest_decision_ms = (
        admission.expiry_ms - max(policy.decision_tau_seconds) * 1_000
    )
    late_admission = RollingCohortAdmission(
        **{
            **admission.__dict__,
            "discovered_at_ms": earliest_decision_ms,
            "received_at_ms": earliest_decision_ms + 1_000,
        }
    )
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (earliest_decision_ms - 1, 1_001),
    )
    initialize_rolling_shadow(root, policy)

    with pytest.raises(ValueError, match="source receipt must precede"):
        admit_rolling_cohort(root, late_admission)

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is True
    assert audit["receipt_count"] == 1
    assert audit["admissions"] == {}


def test_decision_writer_clock_cannot_precede_decision_without_poisoning_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    decision_at_ms = admission.expiry_ms - 60_000
    decision = RollingDecisionReceipt(
        common_expiry_id=admission.common_expiry_id,
        decision_tau_seconds=60,
        decision_at_ms=decision_at_ms,
        receipt_received_at_ms=decision_at_ms + 1_000,
        monotonic_ns=3,
        action="no_trade",
        action_payload_sha256="b" * 64,
    )
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
        (decision_at_ms - 1, 1_002),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)

    with pytest.raises(ValueError, match="writer-recorded before decision time"):
        append_rolling_decision(root, decision)

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is True
    assert audit["receipt_count"] == 2
    assert audit["decisions"] == {}


def test_post_label_decision_source_receipt_is_rejected_without_poisoning_journal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    decision_at_ms = admission.expiry_ms - 60_000
    decision = RollingDecisionReceipt(
        common_expiry_id=admission.common_expiry_id,
        decision_tau_seconds=60,
        decision_at_ms=decision_at_ms,
        receipt_received_at_ms=admission.expiry_ms + 1_000,
        monotonic_ns=3,
        action="no_trade",
        action_payload_sha256="b" * 64,
    )
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
        (admission.expiry_ms - 1, 1_002),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)

    with pytest.raises(ValueError, match="source receipt must be strictly pre-label"):
        append_rolling_decision(root, decision)

    audit = audit_rolling_shadow(root)
    assert audit["valid"] is True
    assert audit["receipt_count"] == 2
    assert audit["decisions"] == {}


def test_finalize_rejects_outcome_beyond_locked_settlement_grace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "journal"
    policy = _policy()
    admission = _admission(1)
    _install_writer_clock(
        monkeypatch,
        (policy.received_at_ms + 1, 1_000),
        (admission.received_at_ms + 1, 1_001),
    )
    initialize_rolling_shadow(root, policy)
    admit_rolling_cohort(root, admission)

    with pytest.raises(ValueError, match="locked settlement grace"):
        finalize_rolling_cohort(
            root,
            RollingCohortOutcome(
                common_expiry_id=admission.common_expiry_id,
                outcome="complete",
                clean=True,
                finalized_at_ms=admission.expiry_ms + 360_001,
                received_at_ms=admission.expiry_ms + 360_001,
                monotonic_ns=30,
                realized_net_pnl="1.25",
                evidence_sha256="c" * 64,
            ),
        )
