from __future__ import annotations

from typing import Any, cast

from ..acceptance import run_fixed_acceptance_suite_in_tempdir
from ..orchestrator import FixedTrackAcceptanceResult, StageResult

ACCEPTANCE_REPORT_VERSION = "profit-system-v0.2.acceptance.v2"


def _stage_summary(stage: StageResult) -> dict[str, Any]:
    return {
        "status": stage.decision.status.value,
        "signal_signature": stage.signal_signature,
        "evidence_digest": stage.evidence_digest,
    }


def _execution_summary(stage: StageResult) -> dict[str, Any]:
    return {
        "confirm_status": "CONFIRMED" if stage.ticket is not None else "BLOCKED",
        "blocked_reasons": []
        if stage.execution_snapshot is None
        else list(stage.execution_snapshot.blocked_reasons),
    }


def _track_summary(result: FixedTrackAcceptanceResult) -> dict[str, Any]:
    return {
        "track": result.track,
        "strategy_id": result.strategy_id,
        "final_status": result.final_status,
        "research": result.research.decision.to_document(),
        "replay": _stage_summary(result.replay),
        "paper": _stage_summary(result.paper),
        "shadow": _stage_summary(result.shadow),
        "shadow_gate": result.shadow_gate.decision.to_document(),
        "replay_execution": _execution_summary(result.replay),
        "paper_execution": _execution_summary(result.paper),
        "shadow_execution": _execution_summary(result.shadow),
    }


def run_track_a_walkthrough() -> dict[str, Any]:
    return _track_summary(run_fixed_acceptance_suite_in_tempdir()["A"])


def run_track_b_walkthrough() -> dict[str, Any]:
    return _track_summary(run_fixed_acceptance_suite_in_tempdir()["B"])


def run_acceptance_walkthroughs() -> dict[str, Any]:
    results = run_fixed_acceptance_suite_in_tempdir()
    return cast(
        dict[str, Any],
        {
            "report_version": ACCEPTANCE_REPORT_VERSION,
            "tracks": [_track_summary(results["A"]), _track_summary(results["B"])],
        },
    )


__all__ = [
    "ACCEPTANCE_REPORT_VERSION",
    "run_acceptance_walkthroughs",
    "run_track_a_walkthrough",
    "run_track_b_walkthrough",
]
