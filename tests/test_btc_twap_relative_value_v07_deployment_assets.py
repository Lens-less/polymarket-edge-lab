from __future__ import annotations

import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy" / "aws" / "paper_v07"
RESEARCH_ROOT = (
    PROJECT_ROOT
    / "research"
    / "btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16"
)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v07_research_and_deploy_assets_exist() -> None:
    expected = {
        "README.md",
        "bootstrap_amazon_linux.sh",
        "polymm-btc-twap-paper-v07-performance.service",
        "polymm-btc-twap-paper-v07-performance.timer",
        "polymm-btc-twap-paper-v07-health.service",
        "polymm-btc-twap-paper-v07-health.timer",
        "polymm-btc-twap-paper-v07-healthcheck.sh",
    }
    assert {path.name for path in DEPLOY_ROOT.iterdir()} == expected
    assert (RESEARCH_ROOT / "SERVICE_CONFIG.json").is_file()
    assert (RESEARCH_ROOT / "DEPLOYMENT_SPEC.md").is_file()
    for path in DEPLOY_ROOT.iterdir():
        assert b"\r\n" not in path.read_bytes()


def test_v07_service_config_is_future_only_read_only_and_honest() -> None:
    config = _load_json(RESEARCH_ROOT / "SERVICE_CONFIG.json")

    assert config["schema_version"] == "btc-twap-relative-value-v07-shadow-service.v2"
    assert config["mode"] == "prospective_actual_market_counterfactual_shadow"
    assert config["source_runs_root"] == (
        "/var/lib/poly-mm-v06/data/"
        "btc_5m_15m_relative_value_paper_v06_linux_2026-08-14/runs"
    )
    assert config["source_status_path"] == (
        "/var/lib/poly-mm-v06/data/"
        "btc_5m_15m_relative_value_paper_v06_linux_2026-08-14/service/status.json"
    )
    assert config["data_root"] == (
        "/var/lib/poly-mm-v07/data/"
        "btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16"
    )
    assert config["research_root"] == (
        "/var/lib/poly-mm-v07/research/"
        "btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16"
    )
    assert config["preregistration_path"] == (
        "/opt/poly-mm-v07/research/"
        "btc_5m_15m_relative_value_counterfactual_v07_2026-08-15/"
        "PREREGISTRATION.json"
    )
    assert config["preregistration_sha256"] == (
        "de79f3e4d43b513a7c71e3196877ee110f04f7d31cec4b3cd060f6184f541bfe"
    )
    assert config["prospective_cutoff_iso"] == "2026-08-16T15:00:00Z"
    assert config["prospective_cutoff_ms"] == 1786892400000
    assert config["decision_tau_seconds"] == [60, 120, 180, 240]
    assert config["train_case_count"] == 1
    assert config["validation_case_count"] == 1
    assert config["maximum_cases"] == 102
    assert config["snapshot_mode"] == "reflink_required"
    assert config["minimum_free_bytes"] == 12 * 1024**3
    assert config["paper_only"] is True
    assert config["public_only"] is True
    assert config["new_orders_disabled"] is True
    assert config["live"] is False
    assert config["prelabel_lock_journal_root"] is None
    assert config["qualified_pnl_possible"] is False
    assert config["true_edge_possible"] is False


def test_v07_deployment_spec_is_honest_about_nonqualification() -> None:
    text = (RESEARCH_ROOT / "DEPLOYMENT_SPEC.md").read_text(encoding="utf-8")

    assert "prospective_actual_market_counterfactual_shadow" in text
    assert "2026-08-16T15:00:00Z" in text
    assert "1786892400000" in text
    assert "prelabel lock journal" in text
    assert "cannot satisfy true-edge gates" in text
    assert "cannot produce non-null qualified PnL" in text


def test_v07_performance_service_and_timer_are_hardened() -> None:
    service_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v07-performance.service"
    ).read_text(encoding="utf-8")
    timer_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v07-performance.timer"
    ).read_text(encoding="utf-8")

    assert "Type=oneshot" in service_text
    assert "Wants=polymm-btc-twap-paper-v06.service" not in service_text
    assert "User=polybotv07" in service_text
    assert "Group=polybotv07" in service_text
    assert "SupplementaryGroups=polybotv06" in service_text
    assert "WorkingDirectory=/opt/poly-mm-v07" in service_text
    assert "ReadOnlyPaths=/opt/poly-mm-v07" in service_text
    assert "ReadOnlyPaths=/var/lib/poly-mm-v06" in service_text
    assert "ReadWritePaths=/var/lib/poly-mm-v07" in service_text
    assert "Environment=AWS_EC2_METADATA_DISABLED=true" in service_text
    assert "IPAddressDeny=any" in service_text
    assert "RestrictAddressFamilies=AF_UNIX" in service_text
    assert "NoNewPrivileges=true" in service_text
    assert "PrivateTmp=true" in service_text
    assert "PrivateDevices=true" in service_text
    assert "ProtectSystem=strict" in service_text
    assert "ProtectHome=true" in service_text
    assert "Nice=19" in service_text
    assert "IOSchedulingClass=idle" in service_text
    assert "CPUQuota=15%" in service_text
    assert "MemoryMax=500M" in service_text
    assert "TimeoutStartSec=25min" in service_text
    assert "EnvironmentFile=" not in service_text
    assert "--validate-only" in service_text
    assert (
        "/opt/poly-mm-v07/research/"
        "btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/"
        "SERVICE_CONFIG.json"
    ) in service_text
    assert "OnCalendar=*:0/30" in timer_text
    assert "Persistent=true" in timer_text
    assert "RandomizedDelaySec=30" in timer_text
    assert "Unit=polymm-btc-twap-paper-v07-performance.service" in timer_text


def test_v07_health_service_timer_and_script_are_read_only() -> None:
    service_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v07-health.service"
    ).read_text(encoding="utf-8")
    timer_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v07-health.timer"
    ).read_text(encoding="utf-8")
    script_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v07-healthcheck.sh"
    ).read_text(encoding="utf-8")

    assert "Type=oneshot" in service_text
    assert (
        "ExecStart=/usr/local/bin/polymm-btc-twap-paper-v07-healthcheck"
        in service_text
    )
    assert "ReadOnlyPaths=/var/lib/poly-mm-v06" in service_text
    assert "ReadWritePaths=/var/lib/poly-mm-v07" in service_text
    assert "IPAddressDeny=any" in service_text
    assert "RestrictAddressFamilies=AF_UNIX" in service_text
    assert "OnCalendar=*:0/5" in timer_text
    assert "Persistent=true" in timer_text
    assert 'OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"' in script_text
    assert 'HISTORY_DIR="${OUTPUT_DIR}/history"' in script_text
    assert (
        'STATUS_PATH="/var/lib/poly-mm-v07/data/'
        'btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/'
        'service/status.json"'
    ) in script_text
    assert "systemctl\", \"show\", unit" in script_text
    assert "source_v06_active" in script_text
    assert "disk_below_minimum" in script_text
    assert "mode_mismatch" in script_text
    assert "orders_submitted_nonzero" in script_text
    assert "authenticated_endpoints_used_nonzero" in script_text
    assert "shadow_validate_failed" in script_text
    assert "status_phase_unhealthy" in script_text
    assert "source_accounting_invalid" in script_text
    assert "source_accounting_inconsistent" in script_text
    assert "source_attempt_capture_error_present" in script_text
    assert "no_clean_post_cutoff_source_attempts_yet" in script_text
    assert '"warnings": warnings' in script_text
    assert "qualified_pnl_must_be_null" in script_text
    assert "true_edge_guard_invalid" in script_text
    assert "positive_100_trade_guard_invalid" in script_text
    assert "prelabel_guard_invalid" in script_text
    assert "heartbeat_age_seconds < -60" in script_text
    assert "status_stale" in script_text
    assert "mv -f \"${TMP_PATH}\" \"${OUTPUT_PATH}\"" in script_text
    assert "cp \"${OUTPUT_PATH}\" \"${HISTORY_TMP}\"" in script_text


def test_v07_bootstrap_uses_release_markers_reflink_probe_and_manual_start() -> None:
    bootstrap_text = (DEPLOY_ROOT / "bootstrap_amazon_linux.sh").read_text(
        encoding="utf-8"
    )
    readme_text = (DEPLOY_ROOT / "README.md").read_text(encoding="utf-8")

    assert 'DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-v07}"' in bootstrap_text
    assert 'SERVICE_USER="${SERVICE_USER:-polybotv07}"' in bootstrap_text
    assert 'SERVICE_GROUP="${SERVICE_GROUP:-polybotv07}"' in bootstrap_text
    assert (
        'DEPLOYMENT_REVISION_PATH="${INSTALL_ROOT}/.deployment-revision"'
        in bootstrap_text
    )
    assert (
        'IMPLEMENTATION_REVISION_PATH="${INSTALL_ROOT}/.implementation-revision"'
        in bootstrap_text
    )
    assert "checkout -f --detach" in bootstrap_text
    assert 'git -C "${INSTALL_ROOT}" clean -ffdx' in bootstrap_text
    assert "status --porcelain=v1 --untracked-files=all" in bootstrap_text
    assert 'release_marker in "/.deployment-revision"' in bootstrap_text
    assert '"${INSTALL_ROOT}/.git/info/exclude"' in bootstrap_text
    assert "--shell /sbin/nologin" in bootstrap_text
    assert (
        "preregistration source_baseline must be a 40-character commit"
        in bootstrap_text
    )
    assert "merge-base --is-ancestor" in bootstrap_text
    assert "release-marker-only archives are forbidden" in bootstrap_text
    assert 'SOURCE_RUNS_ROOT="$(' in bootstrap_text
    assert 'document.get("source_runs_root")' in bootstrap_text
    assert 'stat -c %d "${DATA_ROOT}"' in bootstrap_text
    assert 'stat -c %d "${SOURCE_RUNS_ROOT}"' in bootstrap_text
    assert "must share one filesystem" in bootstrap_text
    assert 'stat -f -c %T "${xfs_path}"' in bootstrap_text
    assert '!= "xfs"' in bootstrap_text
    assert 'find "${SOURCE_RUNS_ROOT}"' in bootstrap_text
    assert '-name capture-config.json' in bootstrap_text
    assert "cp --reflink=always" in bootstrap_text
    assert "distinct inode" in bootstrap_text
    assert "systemctl daemon-reload" in bootstrap_text
    assert "run_btc_twap_relative_value_v07_shadow.py" in bootstrap_text
    assert (
        "systemctl enable polymm-btc-twap-paper-v07-performance.timer"
        in bootstrap_text
    )
    assert (
        "systemctl start polymm-btc-twap-paper-v07-performance.timer"
        in bootstrap_text
    )
    assert "systemctl enable polymm-btc-twap-paper-v07-health.timer" in bootstrap_text
    assert "systemctl start polymm-btc-twap-paper-v07-health.timer" in bootstrap_text
    assert "enable --now" not in bootstrap_text
    assert "future-only v0.7 counterfactual shadow track" in readme_text
    assert "never submits orders" in readme_text
    assert "does not prove true edge or real trading profitability" in readme_text


def test_release_checkout_commands_remove_dirty_and_untracked_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "review@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Deployment Review"],
        check=True,
    )
    tracked = repo / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked.write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.py").write_text("print('stale')\n", encoding="utf-8")

    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-f", "--detach", revision],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "clean", "-ffdx"], check=True)

    assert tracked.read_text(encoding="utf-8") == "committed\n"
    assert not (repo / "untracked.py").exists()
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
