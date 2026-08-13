from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import src.edge_lab.capture_cli as capture_cli

from src.edge_lab.capture_cli import (
    CONFIG_SCHEMA_VERSION,
    _local_proxy_mapping,
    load_capture_config,
    main,
)


def _write_config(path: Path, data_root: Path, **overrides: object) -> Path:
    document: dict[str, object] = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "data_root": str(data_root),
        "asset_ids": ["asset-1"],
        "condition_ids": ["condition-1"],
        "rule_market_ids": ["market-1"],
        "rtds_subscriptions": [
            {
                "topic": "crypto_prices",
                "type": "update",
                "filters": "",
            }
        ],
        "snapshot_intervals": {
            "clob": 30,
            "rules": 300,
        },
        "checkpoint_every_records": 10,
        "max_records_per_batch": 100,
        "gamma_page_limit": 10,
        "gamma_max_pages": 1,
        "reward_page_limit": 100,
        "reward_max_pages": 1,
        "targets": [{"event_id": "event-1"}],
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_load_capture_config_is_explicit_and_resolves_data_root(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path / "capture.json",
        tmp_path / "data",
    )

    config = load_capture_config(config_path)

    assert config.data_root == (tmp_path / "data").resolve()
    assert config.asset_ids == ("asset-1",)
    assert config.condition_ids == ("condition-1",)
    assert config.rule_market_ids == ("market-1",)
    assert config.targets == ({"event_id": "event-1"},)
    assert config.capture_started_at_ms is None
    assert config.evidence_track_id is None


def test_load_capture_config_accepts_runtime_identity_fields(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path / "capture.json",
        tmp_path / "data",
        capture_started_at_ms=123_456,
        evidence_track_id="paper-v05",
    )

    config = load_capture_config(config_path)

    assert config.capture_started_at_ms == 123_456
    assert config.evidence_track_id == "paper-v05"


@pytest.mark.parametrize(
    ("field", "value", "pattern"),
    [
        ("capture_started_at_ms", -1, "non-negative integer"),
        ("capture_started_at_ms", True, "non-negative integer"),
        ("evidence_track_id", "", "non-empty safe token"),
        ("evidence_track_id", "unsafe/token", "non-empty safe token"),
    ],
)
def test_load_capture_config_rejects_invalid_runtime_identity_fields(
    tmp_path: Path,
    field: str,
    value: object,
    pattern: str,
) -> None:
    config_path = _write_config(
        tmp_path / "capture.json",
        tmp_path / "data",
        **{field: value},
    )

    with pytest.raises(ValueError, match=pattern):
        load_capture_config(config_path)


@pytest.mark.parametrize(
    "nested",
    [
        {"api_key": "must-not-appear"},
        {"nested": {"private-key": "must-not-appear"}},
        {"items": [{"wallet_address": "must-not-appear"}]},
    ],
)
def test_load_capture_config_rejects_credential_like_keys(
    tmp_path: Path,
    nested: dict[str, object],
) -> None:
    config_path = _write_config(
        tmp_path / "capture.json",
        tmp_path / "data",
        targets=[nested],
    )

    with pytest.raises(ValueError, match="credential-like key"):
        load_capture_config(config_path)


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://example.com:7897",
        "socks5://127.0.0.1:7897",
        "http://user:" + "password@127.0.0.1:7897",
        "http://127.0.0.1:7897/path",
        "http://127.0.0.1:7897?" + "token=forbidden",
    ],
)
def test_proxy_must_be_unauthenticated_loopback(proxy_url: str) -> None:
    with pytest.raises(
        ValueError,
        match=r"unauthenticated loopback HTTP\(S\)",
    ):
        _local_proxy_mapping(proxy_url)


def test_validate_only_checks_guard_without_creating_data(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_root = tmp_path / "must-not-be-created"
    config_path = _write_config(
        tmp_path / "capture.json",
        data_root,
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "--proxy",
            "http://127.0.0.1:7897",
            "--validate-only",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result == {
        "asset_count": 1,
        "condition_count": 1,
        "new_orders_disabled": True,
        "rule_market_count": 1,
        "schema_version": CONFIG_SCHEMA_VERSION,
        "valid": True,
    }
    assert not data_root.exists()


@pytest.mark.asyncio
async def test_forward_capture_allows_slow_finalized_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _write_config(
        tmp_path / "capture.json",
        tmp_path / "data",
    )
    captured: dict[str, Any] = {}

    class StubRecorder:
        def __init__(self, *, config: Any, **_: Any) -> None:
            captured["config"] = config

        async def run(self, *, run_for_seconds: float) -> None:
            captured["run_for_seconds"] = run_for_seconds

    monkeypatch.setattr(capture_cli, "PublicRecorder", StubRecorder)

    await capture_cli.run_forward_capture(
        load_capture_config(config_path),
        duration_seconds=1.0,
    )

    assert captured["run_for_seconds"] == 1.0
    assert captured["config"].sink_timeout_seconds == 60.0
    assert captured["config"].checkpoint_timeout_seconds == 60.0
