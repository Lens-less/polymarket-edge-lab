from __future__ import annotations

from pathlib import Path

from src.profit_system.acceptance import (
    acceptance_summary,
    run_fixed_acceptance_suite,
)
from src.profit_system.gates import ResearchGate, ShadowGate
from src.profit_system.persistence import PersistenceStore


def test_fixed_acceptance_suite_produces_track_a_go_and_track_b_no_go(tmp_path: Path) -> None:
    results = run_fixed_acceptance_suite(tmp_path)
    summary = acceptance_summary(results)

    assert results["A"].final_status == "GO"
    assert results["A"].research.decision.status == "GO"
    assert results["A"].shadow_gate.decision.status == "GO"
    assert results["B"].final_status == "NO_GO"
    assert results["B"].research.decision.status == "GO"
    assert results["B"].shadow_gate.decision.status == "NO_GO"
    assert summary["report_version"] == "profit-system-v0.2.acceptance.v2"
    assert [track["track"] for track in summary["tracks"]] == ["A", "B"]
    assert (
        results["A"].replay.signal_signature
        == results["A"].paper.signal_signature
        == results["A"].shadow.signal_signature
    )
    assert (
        results["B"].replay.signal_signature
        == results["B"].paper.signal_signature
        == results["B"].shadow.signal_signature
    )


def test_gate_evidence_is_persisted_and_recomputable_from_fixed_store(tmp_path: Path) -> None:
    results = run_fixed_acceptance_suite(tmp_path)

    track_a_store = PersistenceStore(tmp_path / "track-a.db")
    track_b_store = PersistenceStore(tmp_path / "track-b.db")
    try:
        track_a_research = track_a_store.get_system_state("acceptance:track_a:research")
        track_b_shadow = track_b_store.get_system_state("acceptance:track_b:shadow")

        assert track_a_research["evidence_digest"] == results["A"].research.evidence_digest
        assert track_b_shadow["evidence_digest"] == results["B"].shadow_gate.evidence_digest
        assert (
            ResearchGate().evaluate(track_a_research["evidence"]).report_sha256
            == results["A"].research.decision.report_sha256
        )
        assert (
            ShadowGate().evaluate(track_b_shadow["evidence"]).report_sha256
            == results["B"].shadow_gate.decision.report_sha256
        )
        assert track_a_store.journal_count() > 0
        assert track_b_store.journal_count() > 0
    finally:
        track_a_store.close()
        track_b_store.close()
