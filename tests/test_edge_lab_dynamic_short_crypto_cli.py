from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from src.edge_lab.data_store import canonical_json_bytes
from src.edge_lab.dynamic_short_crypto_cli import (
    RESTART_RECOVERY,
    STATUS_SCHEMA_VERSION,
    _exclusive_service_lock,
    _isolated_run_config,
    _service_config,
    build_parser,
    main,
)
from src.edge_lab.dynamic_short_crypto_service import (
    DynamicShortCryptoServiceConfig,
    LifecycleRemediationPlan,
)


def _finalized_discovery_file(directory: Path) -> None:
    raw_path = directory / "batch.jsonl"
    content = b"{}\n"
    raw_path.write_bytes(content)
    raw_path.with_suffix(".manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": "capture-manifest.v1",
                "schema_version": "edge-lab-recorder.raw.v1",
                "source": directory.name,
                "batch_id": "batch",
                "raw_path": f"raw/{directory.name}/batch.jsonl",
                "record_count": 1,
                "checksum": {
                    "algorithm": "sha256",
                    "value": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                    "lines": 1,
                },
            }
        ),
        encoding="utf-8",
    )


def test_validate_only_reads_manifests_without_network_or_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    _finalized_discovery_file(discovery)
    output = tmp_path / "must-not-exist"

    exit_code = main(
        [
            "--discovery-dir",
            str(discovery),
            "--data-root",
            str(output),
            "--proxy",
            "http://127.0.0.1:7897",
            "--validate-only",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "finalized_discovery_file_count": 1,
        "input_output_separate": True,
        "manifest_validation_enabled": True,
        "network_requests": False,
        "new_orders_disabled": True,
        "public_only": True,
        "restart_recovery": RESTART_RECOVERY,
        "run_isolation": "unique_append_only_run_root",
        "schema_version": STATUS_SCHEMA_VERSION,
        "valid": True,
        "writes_performed": False,
    }
    assert not output.exists()


def test_normal_runs_receive_unique_append_only_roots(tmp_path: Path) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    base = tmp_path / "dynamic"

    config = DynamicShortCryptoServiceConfig(
        discovery_dir=discovery,
        output_root=base,
    )
    first = _isolated_run_config(config)
    second = _isolated_run_config(config)

    assert first.output_root.parent == base / "runs"
    assert second.output_root.parent == base / "runs"
    assert first.output_root != second.output_root
    assert not first.output_root.exists()
    assert not second.output_root.exists()


def test_new_run_replays_all_existing_run_roots_in_stable_order(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    base = tmp_path / "dynamic"
    second_prior = base / "runs" / "run-b"
    first_prior = base / "runs" / "run-a"
    second_prior.mkdir(parents=True)
    first_prior.mkdir()
    (base / "runs" / "not-a-directory").write_text(
        "ignored",
        encoding="utf-8",
    )
    config = DynamicShortCryptoServiceConfig(
        discovery_dir=discovery,
        output_root=base,
    )

    isolated = _isolated_run_config(config)

    assert isolated.recovery_run_roots == (
        first_prior.resolve(),
        second_prior.resolve(),
    )
    assert isolated.output_root not in isolated.recovery_run_roots
    assert isolated.recovery_snapshot_path == (
        base / "service" / "restart-recovery-snapshot-v2.json"
    ).resolve()


def test_cli_defaults_background_recovery_to_bounded_cpu_ratio(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    base = tmp_path / "dynamic"
    args = build_parser().parse_args(
        [
            "--discovery-dir",
            str(discovery),
            "--data-root",
            str(base),
        ]
    )

    config = _service_config(args)
    isolated = _isolated_run_config(config)

    assert isolated.recovery_cpu_ratio == 0.4
    assert isolated.recovery_snapshot_path == (
        base / "service" / "restart-recovery-snapshot-v2.json"
    ).resolve()


def test_service_config_infers_sibling_finalized_rtds_evidence(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    discovery = raw_root / "clob_market_ws"
    rtds = raw_root / "rtds_ws"
    discovery.mkdir(parents=True)
    rtds.mkdir()
    output = tmp_path / "dynamic"
    args = build_parser().parse_args(
        [
            "--discovery-dir",
            str(discovery),
            "--data-root",
            str(output),
        ]
    )

    config = _service_config(args)

    assert config.rtds_manifest_dir == rtds.resolve()


def test_service_config_bounds_default_worker_scope_to_one_binary_market(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    args = build_parser().parse_args(
        [
            "--discovery-dir",
            str(discovery),
            "--data-root",
            str(tmp_path / "dynamic"),
        ]
    )

    config = _service_config(args)

    assert config.max_assets_per_group == 2


def test_cli_loads_one_content_addressed_read_only_remediation_plan(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    plan = LifecycleRemediationPlan.create("c" * 64)
    plan_path = tmp_path / (
        f"lifecycle-remediation-plan-{plan.plan_id}.json"
    )
    plan_path.write_bytes(canonical_json_bytes(plan.to_document()) + b"\n")
    plan_path.chmod(0o444)
    args = build_parser().parse_args(
        [
            "--discovery-dir",
            str(discovery),
            "--data-root",
            str(tmp_path / "dynamic"),
            "--lifecycle-remediation-plan",
            str(plan_path),
        ]
    )

    config = _service_config(args)

    assert config.lifecycle_remediation_plan == plan


def test_cli_rejects_mutable_remediation_plan(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    plan = LifecycleRemediationPlan.create("c" * 64)
    plan_path = tmp_path / (
        f"lifecycle-remediation-plan-{plan.plan_id}.json"
    )
    plan_path.write_bytes(canonical_json_bytes(plan.to_document()) + b"\n")
    args = build_parser().parse_args(
        [
            "--discovery-dir",
            str(discovery),
            "--data-root",
            str(tmp_path / "dynamic"),
            "--lifecycle-remediation-plan",
            str(plan_path),
        ]
    )

    with pytest.raises(ValueError, match="0444"):
        _service_config(args)


@pytest.mark.parametrize("forged_mode", (0o4644, 0o2444, 0o1444))
def test_cli_rejects_special_bits_on_read_only_remediation_plan(
    tmp_path: Path,
    forged_mode: int,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()
    plan = LifecycleRemediationPlan.create("c" * 64)
    plan_path = tmp_path / (
        f"lifecycle-remediation-plan-{plan.plan_id}.json"
    )
    plan_path.write_bytes(canonical_json_bytes(plan.to_document()) + b"\n")
    plan_path.chmod(forged_mode)
    if stat.S_IMODE(plan_path.stat().st_mode) != forged_mode:
        pytest.skip("filesystem did not retain special permission bits")
    args = build_parser().parse_args(
        [
            "--discovery-dir",
            str(discovery),
            "--data-root",
            str(tmp_path / "dynamic"),
            "--lifecycle-remediation-plan",
            str(plan_path),
        ]
    )

    with pytest.raises(ValueError, match="0444"):
        _service_config(args)


def test_validate_only_rejects_overlapping_input_and_output(
    tmp_path: Path,
) -> None:
    discovery = tmp_path / "clob_market_ws"
    discovery.mkdir()

    with pytest.raises(ValueError, match="separate"):
        main(
            [
                "--discovery-dir",
                str(discovery),
                "--data-root",
                str(discovery / "output"),
                "--validate-only",
            ]
        )


def test_exclusive_lock_rejects_second_process_scope(tmp_path: Path) -> None:
    output = tmp_path / "dynamic"

    with _exclusive_service_lock(output):
        with pytest.raises(RuntimeError, match="another"):
            with _exclusive_service_lock(output):
                pass
