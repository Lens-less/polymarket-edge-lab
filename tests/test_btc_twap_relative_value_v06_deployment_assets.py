from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from src.edge_lab.btc_twap_relative_value_service import (
    ContinuousServiceConfig,
    validate_preregistration_runtime_identity,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy" / "aws" / "paper_v06"
RESEARCH_ROOT = (
    PROJECT_ROOT / "research" / "btc_5m_15m_relative_value_paper_v06_linux_2026-08-14"
)
REGISTRY_PATH = (
    PROJECT_ROOT
    / "research"
    / "settlement_regime_break_2026-08-14"
    / "REGIME_REGISTRY.json"
)
GAP_PATH = REGISTRY_PATH.with_name("GAP_MANIFEST.json")
CLOSURE_PATH = GAP_PATH.with_name("GAP_CLOSURE.json")
V05_PREREGISTRATION = (
    PROJECT_ROOT
    / "research"
    / "btc_5m_15m_relative_value_paper_v05_linux_2026-08-13"
    / "PREREGISTRATION.json"
)
EXPECTED_DEPLOY_FILES = {
    "README.md",
    "bootstrap_amazon_linux.sh",
    "polymm-btc-twap-paper-v06-health.service",
    "polymm-btc-twap-paper-v06-health.timer",
    "polymm-btc-twap-paper-v06-healthcheck.sh",
    "polymm-btc-twap-paper-v06.service",
}


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v06_research_and_deploy_assets_exist() -> None:
    assert (RESEARCH_ROOT / "PREREGISTRATION.json").is_file()
    assert (RESEARCH_ROOT / "SERVICE_CONFIG.json").is_file()
    assert (RESEARCH_ROOT / "STRATEGY_SPEC.md").is_file()
    assert {path.name for path in DEPLOY_ROOT.iterdir()} == EXPECTED_DEPLOY_FILES
    for path in DEPLOY_ROOT.iterdir():
        assert b"\r\n" not in path.read_bytes()


def test_v06_preregistration_freezes_60s_regime_and_registry_hash() -> None:
    prereg = _load_json(RESEARCH_ROOT / "PREREGISTRATION.json")

    assert prereg["schema_version"] == "btc-5m-15m-relative-value-preregistration.v6"
    scope = prereg["scope"]
    assert isinstance(scope, dict)
    assert scope["settlement_regime"] == "chainlink_twap_60s_5m_and_60s_15m"
    assert scope["paper_only"] is True
    assert scope["live_orders_disabled"] is True
    registry = prereg["regime_registry"]
    assert isinstance(registry, dict)
    assert registry["path"] == "research/settlement_regime_break_2026-08-14/REGIME_REGISTRY.json"
    assert registry["sha256"] == hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    strategy = prereg["strategy_spec"]
    assert isinstance(strategy, dict)
    assert strategy["sha256"] == hashlib.sha256(
        (RESEARCH_ROOT / "STRATEGY_SPEC.md").read_bytes()
    ).hexdigest()
    gap = prereg["pre_v06_gap"]
    assert isinstance(gap, dict)
    assert gap["sha256"] == hashlib.sha256(GAP_PATH.read_bytes()).hexdigest()
    assert prereg["repository_head"] == (
        "5dcc3bd7e09794c578d6572181be81388fdb6661"
    )
    subprocess.run(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            str(prereg["repository_head"]),
            "HEAD",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    frozen_strategy = prereg["frozen_strategy"]
    assert isinstance(frozen_strategy, dict)
    assert frozen_strategy["transfer_test_of"] == "btc-paper-v05-20260813"
    transfer_strategy = dict(frozen_strategy)
    transfer_strategy.pop("transfer_test_of")
    v05 = _load_json(V05_PREREGISTRATION)
    assert transfer_strategy == v05["frozen_strategy"]
    assert prereg["promotion_gates"] == v05["promotion_gates"]
    assert prereg["report_policy"] == v05["report_policy"]
    split_policy = prereg["split_policy"]
    assert isinstance(split_policy, dict)
    assert "v0.5 and earlier" in str(split_policy["pre_v06_data_use"])
    sources = scope["settlement_sources"]
    assert isinstance(sources, dict)
    assert {
        source["window_seconds"]
        for source in sources.values()
        if isinstance(source, dict)
    } == {60}


def test_gap_closure_binds_without_mutating_frozen_manifest() -> None:
    preregistration = _load_json(RESEARCH_ROOT / "PREREGISTRATION.json")
    manifest = _load_json(GAP_PATH)
    closure = _load_json(CLOSURE_PATH)
    frozen_hash = hashlib.sha256(GAP_PATH.read_bytes()).hexdigest()

    assert manifest["gap_end"] is None
    assert closure["schema_version"] == "settlement-regime-gap-closure.v1"
    assert closure["immutable_base_manifest_preserved"] is True
    assert closure["base_manifest"] == {
        "path": "research/settlement_regime_break_2026-08-14/GAP_MANIFEST.json",
        "sha256": frozen_hash,
    }
    preregistered_gap = preregistration["pre_v06_gap"]
    assert isinstance(preregistered_gap, dict)
    assert preregistered_gap["sha256"] == frozen_hash
    assert closure["gap_end"] == "2026-08-14T18:49:44.097586Z"
    completed_cycle = closure["v06_first_completed_cycle"]
    assert isinstance(completed_cycle, dict)
    assert completed_cycle["verified_report_v2_count"] == 4
    assert closure["evidence_boundary"] == (
        "operational_context_only_not_qualified_strategy_evidence"
    )


def test_v06_service_config_points_to_isolated_paths_and_registry() -> None:
    config = _load_json(RESEARCH_ROOT / "SERVICE_CONFIG.json")

    assert config["data_root"] == (
        "/var/lib/poly-mm-v06/data/btc_5m_15m_relative_value_paper_v06_linux_2026-08-14"
    )
    assert config["research_root"] == (
        "/var/lib/poly-mm-v06/research/btc_5m_15m_relative_value_paper_v06_linux_2026-08-14"
    )
    assert config["regime_registry_path"] == (
        "/opt/poly-mm-v06/research/settlement_regime_break_2026-08-14/REGIME_REGISTRY.json"
    )
    assert config["settlement_regime_id"] == "chainlink_twap_60s_5m_and_60s_15m.v1"
    assert config["evidence_track_id"] == "btc-paper-v06-20260814"


def test_v06_service_unit_is_hardened_and_uses_v06_paths() -> None:
    service_text = (DEPLOY_ROOT / "polymm-btc-twap-paper-v06.service").read_text(
        encoding="utf-8"
    )

    assert "User=polybotv06" in service_text
    assert "Group=polybotv06" in service_text
    assert "WorkingDirectory=/opt/poly-mm-v06" in service_text
    assert "ReadOnlyPaths=/opt/poly-mm-v06" in service_text
    assert "ReadWritePaths=/var/lib/poly-mm-v06" in service_text
    assert "Environment=AWS_EC2_METADATA_DISABLED=true" in service_text
    assert "IPAddressDeny=169.254.169.254" in service_text
    assert "--validate-only" in service_text
    healthcheck = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v06-healthcheck.sh"
    ).read_text(encoding="utf-8")
    assert 'chmod 0640 "${TMP_PATH}"' in healthcheck


def test_v06_bootstrap_checks_release_and_implementation_markers() -> None:
    bootstrap = (DEPLOY_ROOT / "bootstrap_amazon_linux.sh").read_text(
        encoding="utf-8"
    )

    assert '.deployment-revision"' in bootstrap
    assert '.implementation-revision"' in bootstrap
    assert "merge-base --is-ancestor" in bootstrap
    assert "prereg[\"repository_head\"]" in bootstrap


def test_v06_actual_bundle_passes_runtime_identity_validation(
    tmp_path: Path,
) -> None:
    preregistration_path = RESEARCH_ROOT / "PREREGISTRATION.json"
    config = ContinuousServiceConfig(
        data_root=tmp_path / "data",
        research_root=tmp_path / "research",
        preregistration_path=preregistration_path,
        settlement_grace_seconds=360,
        retry_seconds=30,
        status_interval_seconds=15,
        minimum_free_disk_bytes=12 * 1024**3,
        max_history_roots=1,
        decision_tau_seconds=(240, 180, 120, 60),
        evidence_track_id="btc-paper-v06-20260814",
        clock_sync_source=(
            "Chrony Amazon Time Sync Service 169.254.169.123"
        ),
        settlement_regime_id=(
            "chainlink_twap_60s_5m_and_60s_15m.v1"
        ),
        regime_registry_path=REGISTRY_PATH,
    )

    validate_preregistration_runtime_identity(
        _load_json(preregistration_path),
        config=config,
    )
