from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DEPLOY_ROOT = Path("deploy/aws/watch")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_watch_deployment_assets_exist() -> None:
    expected = {
        "README.md",
        "bootstrap_amazon_linux.sh",
        "polymm-watch.service",
        "polymm-watch.timer",
        "watch-config.json",
    }
    assert expected.issubset({path.name for path in DEPLOY_ROOT.iterdir()})


def test_watch_service_is_read_only_to_strategy_roots() -> None:
    text = (DEPLOY_ROOT / "polymm-watch.service").read_text(encoding="utf-8")

    assert "Type=oneshot" in text
    assert "User=polybotwatch" in text
    assert "Group=polybotwatch" in text
    assert "ProtectSystem=strict" in text
    assert "ReadOnlyPaths=/var/lib/poly-mm" in text
    assert "ReadOnlyPaths=/var/lib/poly-mm-v05" in text
    assert "ReadOnlyPaths=/var/lib/poly-mm-v06" in text
    assert "ReadOnlyPaths=/var/lib/poly-mm-rawcap" in text
    assert "ReadWritePaths=/var/lib/poly-mm-watch" in text
    assert "SupplementaryGroups=polybotv05 polybotv06 polybotraw" in text
    assert "ExecStart=/opt/poly-mm-watch/.venv/bin/python /opt/poly-mm-watch/scripts/watch_paper_tracks.py --config /opt/poly-mm-watch/deploy/aws/watch/watch-config.json" in text
    assert "IPAddressDeny=169.254.169.254" not in text


def test_watch_timer_and_bootstrap_match_staged_manual_start() -> None:
    timer_text = (DEPLOY_ROOT / "polymm-watch.timer").read_text(encoding="utf-8")
    bootstrap_text = (DEPLOY_ROOT / "bootstrap_amazon_linux.sh").read_text(
        encoding="utf-8"
    )

    assert "OnUnitActiveSec=60s" in timer_text
    assert "Unit=polymm-watch.service" in timer_text
    assert 'DATA_ROOT="${DATA_ROOT:-/var/lib/poly-mm-watch}"' in bootstrap_text
    assert 'SERVICE_USER="${SERVICE_USER:-polybotwatch}"' in bootstrap_text
    assert 'SERVICE_GROUP="${SERVICE_GROUP:-polybotwatch}"' in bootstrap_text
    assert 'DEPLOYMENT_REVISION_PATH="${INSTALL_ROOT}/.deployment-revision"' in bootstrap_text
    assert "verified source archive" in bootstrap_text
    assert "systemctl daemon-reload" in bootstrap_text
    assert "systemctl enable polymm-watch.timer" in bootstrap_text
    assert "systemctl start polymm-watch.timer" in bootstrap_text
    assert "polymm-btc-twap-paper-v05.service" not in bootstrap_text
    assert "polymm-btc-twap-paper-v04.service" not in bootstrap_text


def test_watch_config_tracks_v05_v06_and_rawcap() -> None:
    document = json.loads((DEPLOY_ROOT / "watch-config.json").read_text(encoding="utf-8"))

    assert document["schema_version"] == "polymm-paper-track-watch-config.v1"
    assert document["state_path"] == "/var/lib/poly-mm-watch/state/watch-state.json"
    assert document["digest_interval_seconds"] == 3600
    assert {track["name"] for track in document["tracks"]} == {"v05", "v06", "rawcap"}
    paper_tracks = {
        track["name"]: track
        for track in document["tracks"]
        if track["kind"] == "paper"
    }
    assert {
        track["regime_state_path"] for track in paper_tracks.values()
    } == {"/var/lib/poly-mm-rawcap/monitor/regime-latest.json"}
    assert document["host"]["mem_available_threshold_bytes"] == 314572800
    assert document["host"]["cpu_credit_threshold"] == 100.0


def test_watch_cli_can_start_from_outside_the_repository(tmp_path: Path) -> None:
    config_path = tmp_path / "watch-config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "polymm-paper-track-watch-config.v1",
                "state_path": str(tmp_path / "state" / "watch-state.json"),
                "alerts_path": str(tmp_path / "state" / "alerts.json"),
                "tracks": [],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str((PROJECT_ROOT / "scripts" / "watch_paper_tracks.py").resolve()),
            "--config",
            str(config_path),
            "--stdout-only",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[-1])["status"] == "ok"
