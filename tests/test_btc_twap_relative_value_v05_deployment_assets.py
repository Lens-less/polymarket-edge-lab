from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PROJECT_ROOT / "deploy" / "aws" / "paper_v05"
EXPECTED_FILES = {
    "README.md",
    "bootstrap_amazon_linux.sh",
    "polymm-btc-twap-paper-v05-health.service",
    "polymm-btc-twap-paper-v05-health.timer",
    "polymm-btc-twap-paper-v05-healthcheck.sh",
    "polymm-btc-twap-paper-v05.service",
}
EXPECTED_V04_REPORT_HASH = (
    "321f1e797038f4445f1bcc96529119c676f472d3bdda27b3a2cb9b75e7874b98"
)
V05_RESEARCH_PATH = (
    "/opt/poly-mm-v05/research/btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/"
)


def test_v05_deployment_assets_exist_and_use_lf_only() -> None:
    names = {path.name for path in DEPLOY_ROOT.iterdir() if path.is_file()}
    assert names == EXPECTED_FILES

    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.sh", "*.service", "*.timer", "*.py", "*.json", "*.md"):
        assert f"{pattern} text eol=lf" in attributes

    for path in DEPLOY_ROOT.iterdir():
        if path.is_file():
            assert b"\r\n" not in path.read_bytes()


def test_v05_service_is_isolated_to_v05_paths_and_account() -> None:
    service_text = (DEPLOY_ROOT / "polymm-btc-twap-paper-v05.service").read_text(
        encoding="utf-8"
    )

    assert "User=polybotv05" in service_text
    assert "Group=polybotv05" in service_text
    assert "WorkingDirectory=/opt/poly-mm-v05" in service_text
    assert "ReadOnlyPaths=/opt/poly-mm-v05" in service_text
    assert "ReadWritePaths=/var/lib/poly-mm-v05" in service_text
    assert "Environment=AWS_EC2_METADATA_DISABLED=true" in service_text
    assert "IPAddressDeny=169.254.169.254" in service_text
    assert "IPAddressDeny=fd00:ec2::254" in service_text
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6" in service_text
    assert "ProtectClock=true" in service_text
    assert V05_RESEARCH_PATH in service_text
    assert "--validate-only" in service_text
    assert "Restart=always" in service_text
    assert "/opt/poly-mm/" not in service_text
    assert "/var/lib/poly-mm/" not in service_text
    assert "paper-v04" not in service_text
    assert "--proxy" not in service_text
    for forbidden in ("credential", "secret", "private_key", "wallet"):
        assert forbidden not in service_text.casefold()
    for forbidden in ("--order", "submit_order", "place_order", "auth_token"):
        assert forbidden not in service_text.casefold()


def test_v05_health_assets_use_atomic_latest_and_unique_history() -> None:
    timer_text = (DEPLOY_ROOT / "polymm-btc-twap-paper-v05-health.timer").read_text(
        encoding="utf-8"
    )
    health_service_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v05-health.service"
    ).read_text(encoding="utf-8")
    health_script_text = (
        DEPLOY_ROOT / "polymm-btc-twap-paper-v05-healthcheck.sh"
    ).read_text(encoding="utf-8")

    assert "Unit=polymm-btc-twap-paper-v05-health.service" in timer_text
    assert "OnUnitActiveSec=5min" in timer_text
    assert "Persistent=true" in timer_text
    assert (
        "ExecStart=/usr/local/bin/polymm-btc-twap-paper-v05-healthcheck"
        in health_service_text
    )
    assert "User=polybotv05" in health_service_text
    assert "Group=polybotv05" in health_service_text
    assert "ReadWritePaths=/var/lib/poly-mm-v05" in health_service_text
    assert "IPAddressDeny=any" in health_service_text
    assert "RestrictAddressFamilies=AF_UNIX" in health_service_text
    assert 'OUTPUT_DIR="/var/lib/poly-mm-v05/monitor"' in health_script_text
    assert 'OUTPUT_PATH="${OUTPUT_DIR}/health-latest.json"' in health_script_text
    assert 'SNAPSHOT_AT="$(date -u +%Y%m%dT%H%M%S.%NZ)"' in health_script_text
    assert (
        'TMP_PATH="$(mktemp "${OUTPUT_DIR}/health-latest.json.tmp.XXXXXX")"'
        in health_script_text
    )
    assert (
        'HISTORY_TMP="$(mktemp "${HISTORY_DIR}/health-${SNAPSHOT_AT}-XXXXXX.json.tmp")"'
        in health_script_text
    )
    assert 'HISTORY_PATH="${HISTORY_TMP%.tmp}"' in health_script_text
    assert 'chmod 0640 "${TMP_PATH}"' in health_script_text
    assert 'chmod 0640 "${HISTORY_TMP}"' in health_script_text
    assert health_script_text.count("mv -f") == 2
    assert "--maximum-heartbeat-age-seconds 90" in health_script_text
    assert "--proxy" not in health_script_text
    for forbidden in ("credential", "secret", "private_key", "wallet"):
        assert forbidden not in health_script_text.casefold()
    for forbidden in ("--order", "submit_order", "place_order", "auth_token"):
        assert forbidden not in health_script_text.casefold()


def test_v05_bootstrap_is_stage_only_and_does_not_manage_v04() -> None:
    bootstrap_text = (DEPLOY_ROOT / "bootstrap_amazon_linux.sh").read_text(
        encoding="utf-8"
    )

    assert 'INSTALL_ROOT="${INSTALL_ROOT:-/opt/poly-mm-v05}"' in bootstrap_text
    assert 'DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-v05}"' in bootstrap_text
    assert 'SERVICE_USER="${SERVICE_USER:-polybotv05}"' in bootstrap_text
    assert 'SERVICE_GROUP="${SERVICE_GROUP:-polybotv05}"' in bootstrap_text
    assert "chronyd" in bootstrap_text
    assert "169.254.169.123" in bootstrap_text
    assert "run_btc_twap_relative_value_service.py" in bootstrap_text
    assert "--validate-only" in bootstrap_text
    assert "polymm-btc-twap-paper-v05.service" in bootstrap_text
    assert "polymm-btc-twap-paper-v05-health.timer" in bootstrap_text
    assert "systemctl daemon-reload" in bootstrap_text
    assert "Validated only. Start manually when ready:" in bootstrap_text
    assert "systemctl enable polymm-btc-twap-paper-v05.service" in bootstrap_text
    assert "systemctl start polymm-btc-twap-paper-v05.service" in bootstrap_text
    assert (
        "systemctl enable --now polymm-btc-twap-paper-v05.service" not in bootstrap_text
    )
    assert (
        "systemctl enable --now polymm-btc-twap-paper-v05-health.timer"
        not in bootstrap_text
    )
    assert "systemctl start polymm-btc-twap-paper-v04.service" not in bootstrap_text
    assert "systemctl stop polymm-btc-twap-paper-v04.service" not in bootstrap_text
    assert "systemctl disable polymm-btc-twap-paper-v04.service" not in bootstrap_text
    assert "/opt/poly-mm-v05" in bootstrap_text
    assert "/var/lib/poly-mm-v05" in bootstrap_text
    assert (
        "/opt/poly-mm/research/btc_5m_15m_relative_value_paper_v04"
        not in bootstrap_text
    )
    assert (
        "/var/lib/poly-mm/data/btc_5m_15m_relative_value_paper_v04"
        not in bootstrap_text
    )
    assert "--proxy" not in bootstrap_text
    assert "apt-get" not in bootstrap_text
    assert (
        'DEPLOYMENT_REVISION_PATH="${INSTALL_ROOT}/.deployment-revision"'
        in bootstrap_text
    )
    assert (
        'IMPLEMENTATION_REVISION_PATH="${INSTALL_ROOT}/.implementation-revision"'
        in bootstrap_text
    )
    assert 'git -C "${INSTALL_ROOT}" merge-base --is-ancestor' in bootstrap_text
    assert 'prereg["repository_head"]' in bootstrap_text
    assert "neither a Git checkout nor a verified source archive" in bootstrap_text
    assert (
        "printf '%s\\n' \"${DEPLOY_REF}\" >\"${DEPLOYMENT_REVISION_PATH}\""
        in bootstrap_text
    )
    for forbidden in ("credential", "secret", "private_key", "wallet"):
        assert forbidden not in bootstrap_text.casefold()
    for forbidden in ("--order", "submit_order", "place_order", "auth_token"):
        assert forbidden not in bootstrap_text.casefold()


def test_v05_readme_documents_stage_start_verify_and_v04_preservation() -> None:
    readme_text = (DEPLOY_ROOT / "README.md").read_text(encoding="utf-8")

    assert ".deployment-revision" in readme_text
    assert ".implementation-revision" in readme_text
    assert "content-addressed archive" in readme_text
    assert "sudo systemctl enable polymm-btc-twap-paper-v05.service" in readme_text
    assert "sudo systemctl start polymm-btc-twap-paper-v05.service" in readme_text
    assert "sudo systemctl enable polymm-btc-twap-paper-v05-health.timer" in readme_text
    assert "sudo systemctl start polymm-btc-twap-paper-v05-health.timer" in readme_text
    assert (
        "sudo /opt/poly-mm-v05/.venv/bin/python /opt/poly-mm-v05/scripts/"
        "run_btc_twap_relative_value_service.py --config "
        "/opt/poly-mm-v05/research/"
        "btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/"
        "SERVICE_CONFIG.json --validate-only"
    ) in readme_text
    assert (
        "sha256sum /opt/poly-mm/research/"
        "btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/"
        "DEPLOYMENT_REPORT.md"
        in readme_text
    )
    assert EXPECTED_V04_REPORT_HASH in readme_text
    assert "do not move, rewrite, or delete `/opt/poly-mm`" in readme_text
    assert "do not move, rewrite, or delete `/var/lib/poly-mm`" in readme_text
    assert (
        "do not edit `/opt/poly-mm/research/"
        "btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/`"
        in readme_text
    )
