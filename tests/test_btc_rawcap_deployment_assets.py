from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy" / "aws" / "rawcap"
EXPECTED_FILES = {
    "README.md",
    "bootstrap_amazon_linux.sh",
    "polymm-btc-rawcap-health.service",
    "polymm-btc-rawcap-health.timer",
    "polymm-btc-rawcap-healthcheck.sh",
    "polymm-btc-rawcap.service",
}


def test_rawcap_deployment_assets_exist_and_use_lf_only() -> None:
    names = {path.name for path in DEPLOY_ROOT.iterdir() if path.is_file()}
    assert names == EXPECTED_FILES
    for path in DEPLOY_ROOT.iterdir():
        if path.is_file():
            assert b"\r\n" not in path.read_bytes()


def test_rawcap_service_is_hardened_and_public_only() -> None:
    service_text = (DEPLOY_ROOT / "polymm-btc-rawcap.service").read_text(
        encoding="utf-8"
    )

    assert "User=polybotraw" in service_text
    assert "Group=polybotraw" in service_text
    assert "WorkingDirectory=/opt/poly-mm-rawcap" in service_text
    assert "ReadOnlyPaths=/opt/poly-mm-rawcap" in service_text
    assert "ReadWritePaths=/var/lib/poly-mm-rawcap" in service_text
    assert "Environment=AWS_EC2_METADATA_DISABLED=true" in service_text
    assert "IPAddressDeny=169.254.169.254" in service_text
    assert "ExecStartPre=/opt/poly-mm-rawcap/.venv/bin/python" in service_text
    assert "--validate-only" in service_text
    assert "--registry /opt/poly-mm-rawcap/research/settlement_regime_break_2026-08-14/REGIME_REGISTRY.json" in service_text
    assert "--proxy" not in service_text


def test_rawcap_health_assets_snapshot_latest_atomically() -> None:
    timer_text = (DEPLOY_ROOT / "polymm-btc-rawcap-health.timer").read_text(
        encoding="utf-8"
    )
    health_service_text = (
        DEPLOY_ROOT / "polymm-btc-rawcap-health.service"
    ).read_text(encoding="utf-8")
    health_script_text = (
        DEPLOY_ROOT / "polymm-btc-rawcap-healthcheck.sh"
    ).read_text(encoding="utf-8")

    assert "OnUnitActiveSec=5min" in timer_text
    assert "Unit=polymm-btc-rawcap-health.service" in timer_text
    assert "ExecStart=/usr/local/bin/polymm-btc-rawcap-healthcheck" in health_service_text
    assert 'OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"' in health_script_text
    assert health_script_text.count("mv -f") == 2


def test_rawcap_bootstrap_stages_install_and_does_not_touch_v05() -> None:
    bootstrap_text = (DEPLOY_ROOT / "bootstrap_amazon_linux.sh").read_text(
        encoding="utf-8"
    )

    assert 'INSTALL_ROOT="${INSTALL_ROOT:-/opt/poly-mm-rawcap}"' in bootstrap_text
    assert 'DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-rawcap}"' in bootstrap_text
    assert 'SERVICE_USER="${SERVICE_USER:-polybotraw}"' in bootstrap_text
    assert "run_btc_regime_agnostic_collector.py" in bootstrap_text
    assert "polymm-btc-rawcap.service" in bootstrap_text
    assert "polymm-btc-twap-paper-v05.service" not in bootstrap_text
