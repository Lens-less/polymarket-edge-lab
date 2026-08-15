"""Public-contract tests for settlement-regime routing and binding."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.edge_lab.btc_twap_relative_value import (
    RelativeValueRejection,
    TwapMarketContract,
)
from src.edge_lab.btc_twap_relative_value_service import (
    ClockSyncMeasurement,
    build_capture_plan,
    load_service_config,
    validate_preregistration_runtime_identity,
)
from src.edge_lab.capture_cli import load_capture_config
from src.edge_lab.settlement_regime import load_settlement_regime_registry


def _write_registry(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "REGIME_REGISTRY.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "btc-twap-settlement-regime-registry.v1",
                "regimes": {
                    "chainlink_twap_30s_5m_and_60s_15m.v1": {
                        "5m": {
                            "provider": "chainlink",
                            "symbol": "btc/usd",
                            "product": "twap",
                            "window_seconds": 30,
                            "stream_slug": "btc-usd-twap-30s-streams",
                        },
                        "15m": {
                            "provider": "chainlink",
                            "symbol": "btc/usd",
                            "product": "twap",
                            "window_seconds": 60,
                            "stream_slug": "btc-usd-twap-60s-streams",
                        },
                    },
                    "chainlink_twap_60s_5m_and_60s_15m.v1": {
                        "destination": "v0.6",
                        "5m": {
                            "provider": "chainlink",
                            "symbol": "btc/usd",
                            "product": "twap",
                            "window_seconds": 60,
                            "stream_slug": "btc-usd-twap-60s-streams",
                        },
                        "15m": {
                            "provider": "chainlink",
                            "symbol": "btc/usd",
                            "product": "twap",
                            "window_seconds": 60,
                            "stream_slug": "btc-usd-twap-60s-streams",
                        },
                    },
                },
                "unknown_regime": {"destination": "quarantine"},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _market_fixture(
    *,
    horizon: str,
    opens_at: int,
    window_seconds: int,
) -> tuple[dict[str, object], dict[str, object]]:
    duration = {"5m": 300, "15m": 900}[horizon]
    source = (
        "https://data.chain.link/streams/"
        f"btc-usd-twap-{window_seconds}s-streams"
    )
    condition_id = "0x" + ("05" if horizon == "5m" else "15") * 32
    token_prefix = "5" if horizon == "5m" else "15"
    gamma = {
        "id": f"market-{horizon}",
        "conditionId": condition_id,
        "slug": f"btc-updown-{horizon}-{opens_at}",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": f'["{token_prefix}-up", "{token_prefix}-down"]',
        "endDate": datetime.fromtimestamp(
            opens_at + duration,
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "resolutionSource": source,
        "description": (
            "This market resolves Up when the closing TWAP is greater than or "
            "equal to the opening TWAP, using the BTC/USD TWAP data stream "
            f"available at {source}."
        ),
    }
    clob = {
        "c": condition_id,
        "t": [
            {"t": f"{token_prefix}-up", "o": "Up"},
            {"t": f"{token_prefix}-down", "o": "Down"},
        ],
        "mos": 5,
        "mts": 0.01,
        "ao": True,
        "itode": True,
        "fd": {"r": 0.07, "e": 1, "to": True},
    }
    return gamma, clob


def test_registry_classifies_normalized_pair_identity(tmp_path: Path) -> None:
    registry = load_settlement_regime_registry(_write_registry(tmp_path))

    classification = registry.classify(
        {
            "5m": (
                "HTTPS://DATA.CHAIN.LINK/streams/"
                "BTC-USD-TWAP-60S-STREAMS/?display=1"
            ),
            "15m": (
                "https://data.chain.link/streams/"
                "btc-usd-twap-60s-streams"
            ),
        }
    )

    assert classification.regime_id == (
        "chainlink_twap_60s_5m_and_60s_15m.v1"
    )
    assert classification.qualification_status == "eligible"
    assert classification.destination == "v0.6"


def test_registry_quarantines_unknown_pair_without_rejecting_capture(
    tmp_path: Path,
) -> None:
    registry = load_settlement_regime_registry(_write_registry(tmp_path))

    classification = registry.classify(
        {
            "5m": "https://data.chain.link/streams/btc-usd-twap-90s-streams",
            "15m": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        }
    )

    assert classification.regime_id is None
    assert classification.qualification_status == "quarantine"
    assert classification.destination == "quarantine"
    assert classification.reason_codes == ("unknown_settlement_regime",)


def test_v06_contract_binds_5m_to_frozen_60_second_source(
    tmp_path: Path,
) -> None:
    registry = load_settlement_regime_registry(_write_registry(tmp_path))
    regime = registry.require("chainlink_twap_60s_5m_and_60s_15m.v1")
    gamma, clob = _market_fixture(
        horizon="5m",
        opens_at=1_786_529_400,
        window_seconds=60,
    )

    contract = TwapMarketContract.from_public_metadata(
        gamma,
        clob,
        settlement_regime=regime,
    )

    assert contract.twap_window_seconds == 60
    assert contract.source_topic == "crypto_prices_twap_sixty"
    assert contract.settlement_regime == regime.regime_id


def test_v06_contract_rejects_legacy_30_second_5m_market(
    tmp_path: Path,
) -> None:
    registry = load_settlement_regime_registry(_write_registry(tmp_path))
    regime = registry.require("chainlink_twap_60s_5m_and_60s_15m.v1")
    gamma, clob = _market_fixture(
        horizon="5m",
        opens_at=1_786_529_400,
        window_seconds=30,
    )

    with pytest.raises(RelativeValueRejection) as caught:
        TwapMarketContract.from_public_metadata(
            gamma,
            clob,
            settlement_regime=regime,
        )

    assert caught.value.code == "resolution_source_mismatch"


def _write_v06_runtime(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    registry_path = _write_registry(tmp_path / "registry")
    strategy_path = tmp_path / "research" / "paper" / "STRATEGY_SPEC.md"
    strategy_path.parent.mkdir(parents=True, exist_ok=True)
    strategy_path.write_text("# frozen 60s/60s transfer test\n", encoding="utf-8")
    preregistration_path = strategy_path.with_name("PREREGISTRATION.json")
    preregistration = {
        "schema_version": "btc-5m-15m-relative-value-preregistration.v6",
        "repository_head": "a" * 40,
        "strategy_spec": {
            "path": "research/paper/STRATEGY_SPEC.md",
            "sha256": hashlib.sha256(strategy_path.read_bytes()).hexdigest(),
        },
        "regime_registry": {
            "path": str(registry_path.relative_to(tmp_path)).replace("\\", "/"),
            "sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        },
        "scope": {
            "paper_only": True,
            "live_orders_disabled": True,
            "prospective_only_after": "2026-08-14T15:00:00Z",
            "evidence_track_id": "btc-paper-v06-test",
            "settlement_regime": (
                "chainlink_twap_60s_5m_and_60s_15m.v1"
            ),
        },
        "frozen_strategy": {
            "decision_tau_seconds": [240, 180, 120, 60],
            "clock_sync": {
                "source": "Chrony Amazon Time Sync Service 169.254.169.123"
            },
        },
    }
    preregistration_path.write_text(
        json.dumps(preregistration),
        encoding="utf-8",
    )
    service_config_path = tmp_path / "SERVICE_CONFIG.json"
    service_config_path.write_text(
        json.dumps(
            {
                "schema_version": "btc-twap-relative-value-continuous-service.v1",
                "data_root": str(tmp_path / "data"),
                "research_root": str(tmp_path / "reports"),
                "preregistration_path": str(preregistration_path),
                "regime_registry_path": str(registry_path),
                "settlement_regime_id": (
                    "chainlink_twap_60s_5m_and_60s_15m.v1"
                ),
                "evidence_track_id": "btc-paper-v06-test",
                "settlement_grace_seconds": 360,
                "retry_seconds": 30,
                "status_interval_seconds": 15,
                "minimum_free_disk_bytes": 12 * 1024**3,
                "max_history_roots": 1,
                "decision_tau_seconds": [240, 180, 120, 60],
                "clock_sync_source": (
                    "Chrony Amazon Time Sync Service 169.254.169.123"
                ),
                "seed_report_paths": [],
            }
        ),
        encoding="utf-8",
    )
    return service_config_path, registry_path, preregistration


def test_v06_service_config_and_preregistration_bind_the_same_registry(
    tmp_path: Path,
) -> None:
    config_path, registry_path, preregistration = _write_v06_runtime(tmp_path)

    config = load_service_config(config_path)
    validate_preregistration_runtime_identity(preregistration, config=config)

    assert config.regime_registry_path == registry_path.resolve()
    assert config.settlement_regime_id == (
        "chainlink_twap_60s_5m_and_60s_15m.v1"
    )


def test_v06_runtime_rejects_registry_hash_drift(tmp_path: Path) -> None:
    config_path, registry_path, preregistration = _write_v06_runtime(tmp_path)
    config = load_service_config(config_path)
    registry_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="registry sha256"):
        validate_preregistration_runtime_identity(preregistration, config=config)


def test_v06_capture_plan_persists_top_level_regime_identity(
    tmp_path: Path,
) -> None:
    config_path, _registry_path, _preregistration = _write_v06_runtime(tmp_path)
    config = load_service_config(config_path)
    expiry_seconds = 1_786_532_400
    gamma_5, clob_5 = _market_fixture(
        horizon="5m",
        opens_at=expiry_seconds - 300,
        window_seconds=60,
    )
    gamma_15, clob_15 = _market_fixture(
        horizon="15m",
        opens_at=expiry_seconds - 900,
        window_seconds=60,
    )

    plan = build_capture_plan(
        config=config,
        now_ms=1_786_531_810_000,
        attempt_id="v06-regime-plan",
        clock_sync=ClockSyncMeasurement(
            measured_at_raw_ms=1_786_531_809_000,
            offset_seconds="0.0001",
            uncertainty_seconds="0.001",
            source="Chrony Amazon Time Sync Service 169.254.169.123",
        ),
        gamma_5=gamma_5,
        clob_5=clob_5,
        gamma_15=gamma_15,
        clob_15=clob_15,
    )

    assert plan.capture_config["settlement_regime_id"] == (
        "chainlink_twap_60s_5m_and_60s_15m.v1"
    )
    assert {
        target["source_topic"] for target in plan.capture_config["targets"]
    } == {"crypto_prices_twap_sixty"}
    assert {
        target["settlement_regime"] for target in plan.capture_config["targets"]
    } == {"chainlink_twap_60s_5m_and_60s_15m.v1"}
    plan.capture_config_path.parent.mkdir(parents=True)
    plan.capture_config_path.write_text(
        json.dumps(plan.capture_config),
        encoding="utf-8",
    )
    loaded = load_capture_config(plan.capture_config_path)
    assert {target["source_topic"] for target in loaded.targets} == {
        "crypto_prices_twap_sixty"
    }
