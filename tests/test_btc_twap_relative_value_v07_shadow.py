from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.build_btc_twap_relative_value_pilot_report import (
    _pair_from_capture_targets,
)
from src.edge_lab import btc_twap_relative_value_v07_shadow as shadow_module
from src.edge_lab.btc_twap_relative_value_v07 import canonical_event_cluster_id
from src.edge_lab.data_store import canonical_json_bytes

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(document) + b"\n")


def _target(expiry_ms: int, horizon: str) -> dict[str, Any]:
    opens_at_ms = expiry_ms - (300_000 if horizon == "5m" else 900_000)
    suffix = "5" if horizon == "5m" else "15"
    return {
        "horizon": horizon,
        "slug": f"btc-updown-{horizon}-{expiry_ms}",
        "market_id": f"market-{suffix}-{expiry_ms}",
        "condition_id": f"0xcondition{suffix}{expiry_ms}",
        "up_token_id": f"token-{suffix}-up-{expiry_ms}",
        "down_token_id": f"token-{suffix}-down-{expiry_ms}",
        "opens_at_ms": opens_at_ms,
        "closes_at_ms": expiry_ms,
        "twap_window_seconds": 60,
        "source_topic": "crypto_prices_twap_sixty",
        "resolution_source": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "settlement_regime": "chainlink_twap_60s_5m_and_60s_15m.v1",
        "tick_size": "0.01",
        "minimum_order_size": "5",
        "fee_schedule": {
            "rate": "0.07",
            "exponent": "1",
            "taker_only": True,
        },
        "taker_delay_ms": 250,
        "accepting_orders": True,
        "rule_hash": ("5" if horizon == "5m" else "f") * 64,
    }


def _canonical_cluster_id(expiry_ms: int) -> str:
    targets = {"5m": _target(expiry_ms, "5m"), "15m": _target(expiry_ms, "15m")}
    pair = _pair_from_capture_targets(targets)
    assert pair is not None
    return canonical_event_cluster_id(pair)


def _source_config(
    root: Path, expiry_ms: int, capture_started_at_ms: int
) -> dict[str, Any]:
    targets = [_target(expiry_ms, "5m"), _target(expiry_ms, "15m")]
    return {
        "schema_version": "edge-lab-forward-capture-config.v1",
        "data_root": str(root),
        "asset_ids": [
            targets[0]["up_token_id"],
            targets[0]["down_token_id"],
            targets[1]["up_token_id"],
            targets[1]["down_token_id"],
        ],
        "condition_ids": [targets[0]["condition_id"], targets[1]["condition_id"]],
        "rule_market_ids": [targets[0]["market_id"], targets[1]["market_id"]],
        "rtds_subscriptions": [
            {"topic": "crypto_prices", "type": "update"},
            {
                "topic": "crypto_prices_twap_thirty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            },
            {
                "topic": "crypto_prices_twap_sixty",
                "type": "update",
                "filters": '{"symbol":"btc/usd"}',
            },
        ],
        "snapshot_intervals": {"gamma": 30.0, "clob": 5.0, "rules": 30.0},
        "targets": targets,
        "clock_sync": {"offset_ms": 0},
        "capture_started_at_ms": capture_started_at_ms,
        "evidence_track_id": "btc-paper-v06-20260814",
        "settlement_regime_id": "chainlink_twap_60s_5m_and_60s_15m.v1",
    }


def _summary(
    root: Path,
    expiry_ms: int,
    *,
    failed: bool = False,
    integrity_issue: bool = False,
    resolved: bool = True,
) -> dict[str, Any]:
    del resolved  # Compact V6 summaries do not carry settlement detail.
    integrity = {
        "checksum_mismatches": [],
        "invalid_manifests": [],
        "manifest_without_raw": [],
        "orphan_partials": [],
        "raw_without_manifest": [],
    }
    if integrity_issue:
        integrity["checksum_mismatches"] = [{"raw_path": "raw/bad.jsonl"}]
    return {
        "schema_version": "btc-twap-compact-forward-capture-summary.v1",
        "generated_at": datetime.fromtimestamp(
            (expiry_ms + 360_000) / 1_000, timezone.utc
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "data_root": str(root),
        "duration_seconds": "900",
        "target_count": 2,
        "asset_count": 4,
        "recorder_leg_count": 2,
        "recorder_leg_failures": [],
        "websocket_redundancy": {"clob_market_ws": 2, "rtds_ws": 2},
        "manifest_count": 1,
        "manifest_record_count": 1,
        "integrity": integrity,
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "authenticated_endpoints_used": 0,
        "orders_submitted": 0,
        "capture_error": None if not failed else {"error_code": "capture_failed"},
    }


def _make_source_attempt(
    source_runs_root: Path,
    *,
    expiry_ms: int,
    attempt_name: str,
    capture_started_at_ms: int,
    failed: bool = False,
    integrity_issue: bool = False,
    resolved: bool = True,
) -> Path:
    root = source_runs_root / str(expiry_ms // 1000) / attempt_name
    root.mkdir(parents=True, exist_ok=False)
    _write_json(
        root / "capture-config.json",
        _source_config(root, expiry_ms, capture_started_at_ms),
    )
    raw_dir = root / "raw" / "clob_market_ws"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (root / "derived").mkdir(parents=True, exist_ok=True)
    (root / "checkpoints").mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "events.jsonl"
    raw_path.write_text('{"event":"ok"}\n', encoding="utf-8")
    _write_json(
        raw_dir / "events.manifest.json",
        {
            "checksum": {
                "algorithm": "sha256",
                "value": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            }
        },
    )
    (root / "notes.txt").write_text("shadow source\n", encoding="utf-8")
    _write_json(
        root / "capture-summary.json",
        _summary(
            root,
            expiry_ms,
            failed=failed,
            integrity_issue=integrity_issue,
            resolved=resolved,
        ),
    )
    return root


def _service_config_path(tmp_path: Path) -> Path:
    source_runs_root = tmp_path / "source" / "btc-v06" / "runs"
    source_runs_root.mkdir(parents=True, exist_ok=True)
    roots_parent = tmp_path / "runtime"
    (roots_parent / "data").mkdir(parents=True, exist_ok=True)
    (roots_parent / "research").mkdir(parents=True, exist_ok=True)
    data_root = roots_parent / "data" / "btc-v07-shadow"
    research_root = roots_parent / "research" / "btc-v07-shadow"
    status_path = data_root / "service" / "status.json"
    performance_path = roots_parent / "monitor" / "performance.json"
    history_dir = roots_parent / "monitor" / "history"
    cutoff_ms = 1_786_892_400_000
    cutoff_iso = "2026-08-16T15:00:00Z"
    deployment_spec_path = tmp_path / "DEPLOYMENT_SPEC.md"
    deployment_spec_path.write_text("test deployment spec\n", encoding="utf-8")
    source_status = {
        "schema_version": "btc-twap-relative-value-service-status.v1",
        "phase": "cycle_complete",
        "heartbeat_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "paper_only": True,
        "public_only": True,
        "new_orders_disabled": True,
        "orders_submitted": 0,
        "authenticated_endpoints_used": 0,
    }
    source_status["status_sha256"] = hashlib.sha256(
        canonical_json_bytes(source_status)
    ).hexdigest()
    _write_json(source_runs_root.parent / "service" / "status.json", source_status)
    config_path = tmp_path / "SERVICE_CONFIG.json"
    _write_json(
        config_path,
        {
            "schema_version": "btc-twap-relative-value-v07-shadow-service.v2",
            "mode": "prospective_actual_market_counterfactual_shadow",
            "track_id": "btc-paper-v07-shadow-20260816",
            "source_runs_root": str(source_runs_root),
            "source_status_path": str(
                source_runs_root.parent / "service" / "status.json"
            ),
            "data_root": str(data_root),
            "research_root": str(research_root),
            "status_path": str(status_path),
            "performance_path": str(performance_path),
            "history_dir": str(history_dir),
            "preregistration_path": str(
                PROJECT_ROOT
                / "research"
                / "btc_5m_15m_relative_value_counterfactual_v07_2026-08-15"
                / "PREREGISTRATION.json"
            ),
            "preregistration_sha256": (
                "de79f3e4d43b513a7c71e3196877ee110f04f7d31cec4b3cd060f6184f541bfe"
            ),
            "deployment_spec_path": str(deployment_spec_path),
            "prospective_cutoff_iso": cutoff_iso,
            "prospective_cutoff_ms": cutoff_ms,
            "decision_tau_seconds": [60, 120, 180, 240],
            "train_case_count": 1,
            "validation_case_count": 1,
            "maximum_cases": 102,
            "snapshot_mode": "reflink_required",
            "paper_only": True,
            "public_only": True,
            "new_orders_disabled": True,
            "maximum_status_age_seconds": 2700,
            "live": False,
            "prelabel_lock_journal_root": None,
            "qualified_pnl_possible": False,
            "true_edge_possible": False,
            "minimum_free_bytes": 12 * 1024**3,
        },
    )
    return config_path


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.fixture
def fake_copy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    copied: list[tuple[Path, Path]] = []

    def _copy(source: Path, destination: Path) -> None:
        copied.append((source, destination))
        shutil.copy2(source, destination)

    monkeypatch.setattr(shadow_module, "_copy_regular_file", _copy)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)
    return copied


def test_validate_only_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _service_config_path(tmp_path)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    exit_code = shadow_module.main(["--config", str(config_path), "--validate-only"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["valid"] is True
    assert payload["mode"] == "prospective_actual_market_counterfactual_shadow"
    assert not (tmp_path / "runtime" / "data" / "btc-v07-shadow").exists()
    assert not (tmp_path / "runtime" / "research" / "btc-v07-shadow").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("paper_only", False),
        ("public_only", False),
        ("new_orders_disabled", False),
        ("live", True),
        ("prelabel_lock_journal_root", "journal"),
        ("qualified_pnl_possible", True),
        ("true_edge_possible", True),
        ("preregistration_sha256", "bad"),
        ("maximum_cases", 101),
        ("decision_tau_seconds", [60]),
        ("prospective_cutoff_ms", 1_786_892_400_001),
        ("minimum_free_bytes", 1024),
    ),
)
def test_config_fails_closed_for_unsafe_or_unfrozen_values(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    config_path = _service_config_path(tmp_path)
    document = _load_json(config_path)
    document[field] = value
    _write_json(config_path, document)

    with pytest.raises((TypeError, ValueError)):
        shadow_module.load_shadow_config(config_path)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("paper_only", False),
        ("public_only", False),
        ("new_orders_disabled", False),
        ("orders_submitted", 1),
        ("authenticated_endpoints_used", 1),
    ),
)
def test_source_status_safety_guards_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    config = shadow_module.load_shadow_config(_service_config_path(tmp_path))
    status = _load_json(config.source_status_path)
    status[field] = value
    status.pop("status_sha256")
    status["status_sha256"] = hashlib.sha256(
        canonical_json_bytes(status)
    ).hexdigest()
    _write_json(config.source_status_path, status)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    with pytest.raises(ValueError, match="source service safety guard failed"):
        shadow_module._validate_runtime_inputs(config, validate_only=True)


def test_source_status_staleness_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = shadow_module.load_shadow_config(_service_config_path(tmp_path))
    status = _load_json(config.source_status_path)
    status["heartbeat_at"] = "2026-08-01T00:00:00Z"
    status.pop("status_sha256")
    status["status_sha256"] = hashlib.sha256(
        canonical_json_bytes(status)
    ).hexdigest()
    _write_json(config.source_status_path, status)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    with pytest.raises(ValueError, match="heartbeat is stale"):
        shadow_module._validate_runtime_inputs(config, validate_only=True)


def test_cutoff_and_latest_success_per_expiry_are_selected(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    source_runs_root = config.source_runs_root
    old_expiry = 1_786_892_400_000
    kept_expiry = 1_786_896_000_000
    _make_source_attempt(
        source_runs_root,
        expiry_ms=old_expiry,
        attempt_name="too-old",
        capture_started_at_ms=1_786_892_399_999,
    )
    _make_source_attempt(
        source_runs_root,
        expiry_ms=kept_expiry,
        attempt_name="earlier-good",
        capture_started_at_ms=kept_expiry - 960_000,
    )
    _make_source_attempt(
        source_runs_root,
        expiry_ms=kept_expiry,
        attempt_name="latest-good",
        capture_started_at_ms=kept_expiry - 950_000,
    )
    _make_source_attempt(
        source_runs_root,
        expiry_ms=old_expiry,
        attempt_name="failed-precutoff",
        capture_started_at_ms=1_786_892_399_998,
        failed=True,
    )
    monkeypatch.setattr(
        shadow_module,
        "build_counterfactual_report",
        lambda *, manifest_path: (_ for _ in ()).throw(
            AssertionError("builder should not run")
        ),
    )

    result = shadow_module.run_shadow_refresh(config)

    assert result["report_status"] == "warming_up"
    manifest = _load_json(config.research_root / "manifests" / "manifest-latest.json")
    assert len(manifest["cases"]) == 1
    assert manifest["cases"][0]["capture_root"].endswith("latest-good")
    assert result["source_summary_file_count"] == 4
    assert result["source_finalized_count"] == 2
    assert result["source_eligible_count"] == 1
    assert len(fake_copy) >= 2


@pytest.mark.parametrize(
    "failure_kind",
    (
        "capture_error",
        "integrity",
        "missing_config",
        "malformed_config",
        "malformed_summary",
        "config_schema",
        "summary_schema",
    ),
)
def test_post_cutoff_source_failures_fail_the_refresh(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
    failure_kind: str,
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    source_root = _make_source_attempt(
        config.source_runs_root,
        expiry_ms=1_786_896_000_000,
        attempt_name=f"bad-{failure_kind}",
        capture_started_at_ms=1_786_895_000_000,
        failed=failure_kind == "capture_error",
        integrity_issue=failure_kind == "integrity",
    )
    capture_config_path = source_root / "capture-config.json"
    summary_path = source_root / "capture-summary.json"
    if failure_kind == "missing_config":
        capture_config_path.unlink()
    elif failure_kind == "malformed_config":
        capture_config_path.write_text("{", encoding="utf-8")
    elif failure_kind == "malformed_summary":
        summary_path.write_text("{", encoding="utf-8")
    elif failure_kind == "config_schema":
        document = _load_json(capture_config_path)
        document["schema_version"] = "unexpected"
        _write_json(capture_config_path, document)
    elif failure_kind == "summary_schema":
        document = _load_json(summary_path)
        document["schema_version"] = "unexpected"
        _write_json(summary_path, document)

    exit_code = shadow_module.main(["--config", str(config_path)])

    assert exit_code == 1
    status = _load_json(config.status_path)
    assert status["phase"] == "failed"
    assert status["evaluation_status"] == "failed"
    assert status["source_finalized_count"] == 0
    assert status["last_error"] == {
        "error_type": "ValueError",
        "error_code": "v07_shadow_refresh_failed",
    }


def test_projection_creates_independent_snapshot_and_preserves_source(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    source_root = _make_source_attempt(
        config.source_runs_root,
        expiry_ms=1_800_000_000_000,
        attempt_name="attempt-a",
        capture_started_at_ms=1_800_000_000_000 - 960_000,
    )
    monkeypatch.setattr(
        shadow_module,
        "build_counterfactual_report",
        lambda *, manifest_path: (_ for _ in ()).throw(
            AssertionError("builder should not run")
        ),
    )
    source_before = (source_root / "raw" / "clob_market_ws" / "events.jsonl").read_text(
        encoding="utf-8"
    )

    shadow_module.run_shadow_refresh(config)

    projected_root = next((config.data_root / "captures").glob("*/*"))
    projected_config = _load_json(projected_root / "capture-config.json")
    assert projected_config["schema_version"] == (
        "btc-5m-15m-relative-value-capture.v1"
    )
    assert projected_config["paper_only"] is True
    assert projected_config["public_only"] is True
    assert projected_config["new_orders_disabled"] is True
    assert projected_config["generated_fixture"] is False
    assert (
        projected_config["safety_flags_derived_from_frozen_v06_public_service"] is True
    )
    assert source_before == (
        source_root / "raw" / "clob_market_ws" / "events.jsonl"
    ).read_text(encoding="utf-8")
    assert not projected_root.samefile(source_root)
    assert fake_copy


def test_three_cases_call_builder_and_write_status(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    expiries = [
        1_800_000_000_000,
        1_800_003_600_000,
        1_800_007_200_000,
        1_800_010_800_000,
    ]
    for index, expiry_ms in enumerate(expiries):
        _make_source_attempt(
            config.source_runs_root,
            expiry_ms=expiry_ms,
            attempt_name=f"attempt-{index}",
            capture_started_at_ms=expiry_ms - 540_000,
        )

    seen: dict[str, Any] = {}

    def fake_builder(*, manifest_path: Path) -> dict[str, Any]:
        manifest = _load_json(manifest_path)
        seen["manifest"] = manifest
        return {
            "schema_version": "btc-5m-15m-v07-counterfactual-report.v8",
            "primary": {
                "edge_basis_breakdown": {
                    "predictive": {
                        "action_reconciliation_count": 3,
                        "net_pnl": "1.25",
                    },
                    "structural": {
                        "action_reconciliation_count": 0,
                        "net_pnl": "0",
                    },
                }
            },
            "evaluation": {
                # Even an impossible/malformed authority claim from upstream must
                # stay fail-closed on this no-journal deployment track.
                "status": "true_edge_gate_satisfied",
                "true_edge_gate_satisfied": True,
                "positive_net_pnl_user_check_passed": True,
                "qualified_net_pnl": "1.25",
                "edge_basis": {
                    "predictive": {"settled_expiry_clusters": 3},
                    "structural": {"settled_expiry_clusters": 0},
                },
                "net_pnl": "1.25",
            },
        }

    monkeypatch.setattr(shadow_module, "build_counterfactual_report", fake_builder)

    result = shadow_module.run_shadow_refresh(config)

    assert result["report_status"] == "report_built"
    manifest = seen["manifest"]
    assert [case["split"] for case in manifest["cases"]] == [
        "train",
        "validation",
        "test",
    ]
    assert manifest["cases"][0]["capture_root"].endswith("attempt-1")
    assert len(manifest["cases"][0]["history_roots"]) == 1
    assert manifest["cases"][0]["history_roots"][0].endswith("attempt-0")
    assert all(len(case["history_roots"]) <= 1 for case in manifest["cases"])
    assert manifest["cases"][1]["history_roots"][0].endswith("attempt-1")
    assert manifest["cases"][2]["history_roots"][0].endswith("attempt-2")
    status = _load_json(config.status_path)
    assert status["evaluation_status"] == "counterfactual_insufficient"
    assert status["builder_evaluation_status"] == "suppressed_no_prelabel_journal"
    assert status["true_edge_gate"] is False
    assert status["qualified_net_pnl"] is None
    assert status["live_execution"] is False
    assert status["prelabel_lock_journal"] is False
    assert status["orders_submitted"] == 0
    performance = _load_json(config.performance_path)
    assert performance["predictive_explainable_economic_attempt_count"] == 3
    assert performance["positive_100_trade_check"] is False


def test_repeat_runs_are_idempotent(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    for index, expiry_ms in enumerate(
        [
            1_800_000_000_000,
            1_800_003_600_000,
            1_800_007_200_000,
            1_800_010_800_000,
        ]
    ):
        _make_source_attempt(
            config.source_runs_root,
            expiry_ms=expiry_ms,
            attempt_name=f"attempt-{index}",
            capture_started_at_ms=expiry_ms - 540_000,
        )
    monkeypatch.setattr(
        shadow_module,
        "build_counterfactual_report",
        lambda *, manifest_path: {
            "schema_version": "btc-5m-15m-v07-counterfactual-report.v8",
            "evaluation": {
                "status": "counterfactual_insufficient",
                "true_edge_gate_satisfied": False,
                "qualified_net_pnl": None,
                "predictive_explainable_economic_attempt_count": 3,
                "structural_explainable_economic_attempt_count": 0,
                "predictive_settled_expiry_cluster_count": 3,
                "structural_settled_expiry_cluster_count": 0,
                "predictive_net_pnl": "1.25",
                "structural_net_pnl": "0",
                "net_pnl": "1.25",
            },
        },
    )

    shadow_module.run_shadow_refresh(config)
    first_projected = sorted((config.data_root / "captures").glob("*/*"))
    shadow_module.run_shadow_refresh(config)
    second_projected = sorted((config.data_root / "captures").glob("*/*"))

    assert [path.as_posix() for path in first_projected] == [
        path.as_posix() for path in second_projected
    ]


def test_symlink_rejection_writes_safe_failure_status(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    source_root = _make_source_attempt(
        config.source_runs_root,
        expiry_ms=1_800_000_000_000,
        attempt_name="attempt-a",
        capture_started_at_ms=1_800_000_000_000 - 960_000,
    )
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (source_root / "symlink.txt").symlink_to(outside)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    exit_code = shadow_module.main(["--config", str(config_path)])

    assert exit_code == 1
    status = _load_json(config.status_path)
    assert status["phase"] == "failed"
    assert status["last_error"] == {
        "error_type": "ValueError",
        "error_code": "v07_shadow_refresh_failed",
    }


def test_source_status_hash_corruption_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = shadow_module.load_shadow_config(_service_config_path(tmp_path))
    status = _load_json(config.source_status_path)
    status["paper_only"] = False
    _write_json(config.source_status_path, status)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    with pytest.raises(ValueError, match="status hash is invalid"):
        shadow_module._validate_runtime_inputs(config, validate_only=True)


def test_source_status_unhealthy_phase_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = shadow_module.load_shadow_config(_service_config_path(tmp_path))
    status = _load_json(config.source_status_path)
    status["phase"] = "stopped"
    status.pop("status_sha256")
    status["status_sha256"] = hashlib.sha256(
        canonical_json_bytes(status)
    ).hexdigest()
    _write_json(config.source_status_path, status)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    with pytest.raises(ValueError, match="phase is unhealthy"):
        shadow_module._validate_runtime_inputs(config, validate_only=True)


def test_unknown_source_status_phase_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = shadow_module.load_shadow_config(_service_config_path(tmp_path))
    status = _load_json(config.source_status_path)
    status["phase"] = "future_unknown_phase"
    status.pop("status_sha256")
    status["status_sha256"] = hashlib.sha256(
        canonical_json_bytes(status)
    ).hexdigest()
    _write_json(config.source_status_path, status)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    with pytest.raises(ValueError, match="phase is unhealthy"):
        shadow_module._validate_runtime_inputs(config, validate_only=True)


def test_claimed_clean_summary_cannot_hide_missing_raw_manifest(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    expiry_ms = 1_800_000_000_000
    source_root = _make_source_attempt(
        config.source_runs_root,
        expiry_ms=expiry_ms,
        attempt_name="attempt-a",
        capture_started_at_ms=expiry_ms - 960_000,
    )
    (source_root / "raw" / "clob_market_ws" / "events.manifest.json").unlink()

    exit_code = shadow_module.main(["--config", str(config_path)])

    assert exit_code == 1
    assert _load_json(config.status_path)["phase"] == "failed"
    assert fake_copy == []


def test_source_mutation_during_projection_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = shadow_module.load_shadow_config(_service_config_path(tmp_path))
    expiry_ms = 1_800_000_000_000
    source_root = _make_source_attempt(
        config.source_runs_root,
        expiry_ms=expiry_ms,
        attempt_name="attempt-a",
        capture_started_at_ms=expiry_ms - 960_000,
    )
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)
    mutated = False

    def copy_and_mutate(source: Path, destination: Path) -> None:
        nonlocal mutated
        shutil.copy2(source, destination)
        if not mutated:
            mutated = True
            (source_root / "notes.txt").write_text(
                "changed during projection\n", encoding="utf-8"
            )

    monkeypatch.setattr(shadow_module, "_copy_regular_file", copy_and_mutate)

    with pytest.raises(ValueError, match="changed during projection"):
        shadow_module.run_shadow_refresh(config)

    assert not (
        config.data_root / "captures" / str(expiry_ms) / "attempt-a"
    ).exists()


def test_source_capture_hardlink_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    expiry_ms = 1_800_000_000_000
    source_root = _make_source_attempt(
        config.source_runs_root,
        expiry_ms=expiry_ms,
        attempt_name="attempt-a",
        capture_started_at_ms=expiry_ms - 960_000,
    )
    outside = tmp_path / "outside-hardlink.txt"
    outside.hardlink_to(source_root / "notes.txt")
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    exit_code = shadow_module.main(["--config", str(config_path)])

    assert exit_code == 1
    status = _load_json(config.status_path)
    assert status["phase"] == "failed"
    assert status["orders_submitted"] == 0
    assert status["true_edge"] is False
    assert status["qualified_net_pnl"] is None


def test_symlinked_expiry_directory_cannot_escape_source_root(
    tmp_path: Path,
    fake_copy: list[tuple[Path, Path]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _service_config_path(tmp_path)
    config = shadow_module.load_shadow_config(config_path)
    expiry_ms = 1_800_000_000_000
    outside_runs = tmp_path / "outside-runs"
    outside_root = _make_source_attempt(
        outside_runs,
        expiry_ms=expiry_ms,
        attempt_name="attempt-a",
        capture_started_at_ms=expiry_ms - 960_000,
    )
    expiry_link = config.source_runs_root / str(expiry_ms // 1_000)
    expiry_link.symlink_to(outside_root.parent, target_is_directory=True)
    monkeypatch.setattr(shadow_module, "_ensure_reflink_available", lambda: None)

    exit_code = shadow_module.main(["--config", str(config_path)])

    assert exit_code == 1
    assert _load_json(config.status_path)["phase"] == "failed"
    assert fake_copy == []
