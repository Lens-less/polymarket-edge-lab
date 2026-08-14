from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.run_btc_regime_agnostic_collector import (
    RawCollectorConfig,
    build_raw_capture_plan,
)
from src.edge_lab.settlement_regime import load_settlement_regime_registry


def _write_registry(tmp_path: Path) -> Path:
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
    token_prefix = "5" if horizon == "5m" else "15"
    source = (
        "https://data.chain.link/streams/"
        f"btc-usd-twap-{window_seconds}s-streams"
    )
    gamma = {
        "id": f"market-{horizon}",
        "conditionId": "0x" + token_prefix * 32,
        "slug": f"btc-updown-{horizon}-{opens_at}",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": f'["{token_prefix}-up", "{token_prefix}-down"]',
        "endDate": datetime.fromtimestamp(opens_at + duration, tz=timezone.utc)
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
        "c": "0x" + token_prefix * 32,
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


def _collector_config(tmp_path: Path) -> RawCollectorConfig:
    return RawCollectorConfig(
        data_root=tmp_path / "data",
        status_interval_seconds=15,
        retry_seconds=30,
        minimum_free_disk_bytes=12 * 1024**3,
        regime_registry_path=_write_registry(tmp_path),
    )


def test_raw_capture_plan_quarantines_unknown_regime_but_keeps_dual_topics(
    tmp_path: Path,
) -> None:
    config = _collector_config(tmp_path)
    registry = load_settlement_regime_registry(config.regime_registry_path)
    gamma_15, clob_15 = _market_fixture(
        horizon="15m",
        opens_at=1_786_531_500,
        window_seconds=60,
    )
    gamma_5, clob_5 = _market_fixture(
        horizon="5m",
        opens_at=1_786_532_100,
        window_seconds=90,
    )

    plan = build_raw_capture_plan(
        config=config,
        registry=registry,
        now_ms=1_786_531_810_000,
        attempt_id="attempt-001",
        gamma_5=gamma_5,
        clob_5=clob_5,
        gamma_15=gamma_15,
        clob_15=clob_15,
    )

    assert plan.classification.qualification_status == "quarantine"
    assert plan.classification.reason_codes == ("unknown_settlement_regime",)
    assert [item["topic"] for item in plan.forward_config.rtds_subscriptions] == [
        "crypto_prices",
        "crypto_prices_twap_thirty",
        "crypto_prices_twap_sixty",
    ]
    assert plan.capture_document["qualification_status"] == "quarantine"
    assert plan.capture_document["qualified_evidence"] is False
    assert plan.capture_document["non_canonical"] is True


def test_raw_capture_plan_marks_known_v06_regime_as_observe_only_raw(
    tmp_path: Path,
) -> None:
    config = _collector_config(tmp_path)
    registry = load_settlement_regime_registry(config.regime_registry_path)
    gamma_15, clob_15 = _market_fixture(
        horizon="15m",
        opens_at=1_786_531_500,
        window_seconds=60,
    )
    gamma_5, clob_5 = _market_fixture(
        horizon="5m",
        opens_at=1_786_532_100,
        window_seconds=60,
    )

    plan = build_raw_capture_plan(
        config=config,
        registry=registry,
        now_ms=1_786_531_810_000,
        attempt_id="attempt-002",
        gamma_5=gamma_5,
        clob_5=clob_5,
        gamma_15=gamma_15,
        clob_15=clob_15,
    )

    assert plan.classification.regime_id == "chainlink_twap_60s_5m_and_60s_15m.v1"
    assert plan.capture_document["resolution_source_canonical"]["5m"].endswith(
        "btc-usd-twap-60s-streams"
    )
    assert plan.capture_document["regime_destination"] == "v0.6"
    assert plan.capture_document["qualified_evidence"] is False
