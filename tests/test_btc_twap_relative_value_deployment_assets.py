from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy" / "aws" / "paper_v04"
RESEARCH_ROOT = (
    PROJECT_ROOT / "research" / "btc_5m_15m_relative_value_paper_v04_linux_2026-08-13"
)


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v04_preregistration_freezes_linux_clock_source_and_scope() -> None:
    prereg = _load_json(RESEARCH_ROOT / "PREREGISTRATION.json")

    assert prereg["schema_version"] == ("btc-5m-15m-relative-value-preregistration.v4")
    assert prereg["repository_head"] == "3292c9bdeada074ea5e697da0b6fe8d9b37470b0"
    strategy_spec = prereg["strategy_spec"]
    assert isinstance(strategy_spec, dict)
    strategy_path = PROJECT_ROOT / str(strategy_spec["path"])
    assert strategy_path.is_file()
    actual_strategy_hash = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
    assert actual_strategy_hash == strategy_spec["sha256"]

    scope = prereg["scope"]
    assert isinstance(scope, dict)
    assert scope["paper_only"] is True
    assert scope["live_orders_disabled"] is True
    assert scope["prospective_only_after"] == prereg["frozen_at"]

    frozen_strategy = prereg["frozen_strategy"]
    assert isinstance(frozen_strategy, dict)
    assert frozen_strategy["decision_tau_seconds"] == [240, 180, 120, 60]
    assert frozen_strategy["pair_risk_usdc"] == "25"

    clock_sync = frozen_strategy["clock_sync"]
    assert isinstance(clock_sync, dict)
    assert clock_sync["source"] == ("Chrony Amazon Time Sync Service 169.254.169.123")
    assert clock_sync["measurement_command"] == "/usr/bin/chronyc -n tracking"
    assert clock_sync["measurement_attempts"] == 3
    assert clock_sync["maximum_measurement_uncertainty_ms"] == 100
    assert (
        clock_sync["uncertainty_formula"]
        == "abs(last_offset) + abs(root_delay)/2 + root_dispersion"
    )
    assert clock_sync["system_clock_mutation"] is False

    split_policy = prereg["split_policy"]
    assert isinstance(split_policy, dict)
    assert "pre_v04_data_use" in split_policy
    assert "never v0.4 PnL" in str(split_policy["pre_v04_data_use"])


def test_v04_service_config_isolated_and_public_only() -> None:
    config = _load_json(RESEARCH_ROOT / "SERVICE_CONFIG.json")

    assert config["schema_version"] == "btc-twap-relative-value-continuous-service.v1"
    assert config["data_root"] == (
        "/var/lib/poly-mm/data/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13"
    )
    assert config["research_root"] == (
        "/var/lib/poly-mm/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13"
    )
    assert config["preregistration_path"] == (
        "/opt/poly-mm/research/"
        "btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/"
        "PREREGISTRATION.json"
    )
    assert config["clock_sync_source"] == (
        "Chrony Amazon Time Sync Service 169.254.169.123"
    )
    assert config["decision_tau_seconds"] == [240, 180, 120, 60]
    assert config["seed_report_paths"] == []


def test_main_service_is_hardened_and_never_passes_proxy_or_order_flags() -> None:
    service_text = (DEPLOY_ROOT / "polymm-btc-twap-paper-v04.service").read_text(
        encoding="utf-8"
    )

    assert "Restart=always" in service_text
    assert "ExecStartPre=/opt/poly-mm/.venv/bin/python" in service_text
    assert "ExecStart=/opt/poly-mm/.venv/bin/python" in service_text
    assert "ReadWritePaths=/var/lib/poly-mm" in service_text
    assert "--validate-only" in service_text
    assert "--proxy" not in service_text
    for forbidden in ("credential", "secret", "private_key", "wallet", "sign"):
        assert forbidden not in service_text.casefold()
    assert "order" not in service_text.casefold()


def test_health_timer_and_snapshot_writer_target_monitor_output() -> None:
    timer_text = (DEPLOY_ROOT / "polymm-btc-twap-paper-v04-health.timer").read_text(
        encoding="utf-8"
    )
    health_service_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v04-health.service"
    ).read_text(encoding="utf-8")
    health_script_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v04-healthcheck.sh"
    ).read_text(encoding="utf-8")

    assert "OnUnitActiveSec=5min" in timer_text
    assert "Persistent=true" in timer_text
    assert "Unit=polymm-btc-twap-paper-v04-health.service" in timer_text
    assert (
        "ExecStart=/usr/local/bin/polymm-btc-twap-paper-v04-healthcheck"
        in health_service_text
    )
    assert "Requires=polymm-btc-twap-paper-v04.service" not in health_service_text
    assert 'OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"' in health_script_text
    assert 'HISTORY_DIR="${OUTPUT_DIR}/history"' in health_script_text
    assert (
        'HISTORY_PATH="${HISTORY_DIR}/health-${SNAPSHOT_AT}.json"'
        in health_script_text
    )
    assert 'TMP_PATH="${OUTPUT_PATH}.tmp.$$"' in health_script_text
    assert health_script_text.count("mv -f") == 2
    assert 'if [[ "${status}" -ne 0 ]]' in health_script_text
    assert health_script_text.rstrip().endswith("exit 0")
    assert "--maximum-heartbeat-age-seconds 90" in health_script_text
    assert "--proxy" not in health_script_text
    for forbidden in ("credential", "secret", "private_key", "wallet", "sign"):
        assert forbidden not in health_script_text.casefold()


def test_bootstrap_script_keeps_paper_safety_and_chrony_setup_local() -> None:
    bootstrap_text = (DEPLOY_ROOT / "bootstrap_amazon_linux.sh").read_text(
        encoding="utf-8"
    )

    assert "chronyd" in bootstrap_text
    assert "169.254.169.123" in bootstrap_text
    assert "run_btc_twap_relative_value_service.py" in bootstrap_text
    assert "--validate-only" in bootstrap_text
    assert "polymm-btc-twap-paper-v04.service" in bootstrap_text
    assert "polymm-btc-twap-paper-v04-health.timer" in bootstrap_text
    assert "iptables" not in bootstrap_text.casefold()
    assert "ufw allow" not in bootstrap_text.casefold()
    assert "--proxy" not in bootstrap_text
    assert "dnf install" in bootstrap_text
    assert "python3.11" in bootstrap_text
    assert "apt-get" not in bootstrap_text
    assert (
        'DEPLOYMENT_REVISION_PATH="${INSTALL_ROOT}/.deployment-revision"'
        in bootstrap_text
    )
    assert "neither a Git checkout nor a verified source archive" in bootstrap_text
    for forbidden in ("credential", "secret", "private_key", "wallet", "sign"):
        assert forbidden not in bootstrap_text.casefold()
