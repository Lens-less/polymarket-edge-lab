from __future__ import annotations

from pathlib import Path

import pytest

from scripts import build_btc_twap_executable_upper_bound as gate0
from scripts import build_btc_twap_relative_value_v07_counterfactual as builder
from src.edge_lab.data_store import canonical_json_bytes
from tests.test_btc_twap_relative_value_v07_counterfactual import (
    PREREGISTRATION_PATH,
    _build_case,
)


def _manifest(tmp_path: Path) -> Path:
    case, _manifest_dir = _build_case(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": builder.MANIFEST_SCHEMA_VERSION,
                "preregistration_path": str(PREREGISTRATION_PATH),
                "cases": [case],
            }
        )
        + b"\n"
    )
    return path


def test_gate_zero_builder_emits_one_depth_ladder_per_common_expiry(
    tmp_path: Path,
) -> None:
    report = gate0.build_upper_bound_report(
        manifest_path=_manifest(tmp_path),
        decision_tau_seconds=60,
        expected_clean_attempts=1,
    )

    assert report["schema_version"] == gate0.REPORT_SCHEMA
    assert report["observed_unique_common_expiry_attempts"] == 1
    assert report["diagnostic"]["attempt_count"] == 1
    assert report["diagnostic"]["attempts"][0]["breakpoints"]
    assert report["diagnostic"]["counts_as_locked_oos_evidence"] is False
    assert report["policy"]["can_authorize_live"] is False
    assert report["safety"]["orders_submitted"] == 0
    assert report["report_sha256"]


def test_gate_zero_builder_refuses_to_silently_misstate_41_attempts(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="expected 41.*observed 1"):
        gate0.build_upper_bound_report(
            manifest_path=_manifest(tmp_path),
            decision_tau_seconds=60,
            expected_clean_attempts=41,
        )
