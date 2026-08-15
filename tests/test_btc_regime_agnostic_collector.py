from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.run_btc_regime_agnostic_collector as collector
from src.edge_lab.capture_cli import (
    ForwardCaptureFinalizationError,
    load_capture_config,
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


def _gamma_market(*, horizon: str, opens_at: int, window_seconds: int) -> dict[str, object]:
    source = f"https://data.chain.link/streams/btc-usd-twap-{window_seconds}s-streams"
    return {
        "id": f"market-{horizon}",
        "conditionId": "0x" + ("05" if horizon == "5m" else "15") * 32,
        "slug": f"btc-updown-{horizon}-{opens_at}",
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": f'["{horizon}-up", "{horizon}-down"]',
        "endDate": "2026-08-14T00:15:00Z",
        "resolutionSource": source,
        "description": (
            "This market resolves using the BTC/USD TWAP stream at "
            f"{source} with greater than or equal language."
        ),
    }


def _clob_market(*, horizon: str) -> dict[str, object]:
    return {
        "c": "0x" + ("05" if horizon == "5m" else "15") * 32,
        "t": [
            {"t": f"{horizon}-up", "o": "Up"},
            {"t": f"{horizon}-down", "o": "Down"},
        ],
        "mos": 5,
        "mts": 0.01,
        "ao": True,
        "itode": True,
        "fd": {"r": 0.07, "e": 1, "to": True},
    }


def _config(tmp_path: Path, registry_path: Path) -> collector.RawCollectorConfig:
    return collector.RawCollectorConfig(
        data_root=tmp_path / "rawcap",
        status_path=tmp_path / "status.json",
        registry_path=registry_path,
        settlement_grace_seconds=360,
        retry_seconds=30,
        status_interval_seconds=15,
        minimum_free_disk_bytes=1,
        evidence_track_id="btc-rawcap-test",
    )


def test_build_capture_plan_marks_v06_transfer_regime_and_dual_twap_topics(
    tmp_path: Path,
) -> None:
    registry = load_settlement_regime_registry(_write_registry(tmp_path))
    config = _config(tmp_path, _write_registry(tmp_path))
    discovery = collector.RawCaptureDiscovery(
        expiry_seconds=1_786_665_600,
        gamma_5=_gamma_market(horizon="5m", opens_at=1_786_665_300, window_seconds=60),
        clob_5=_clob_market(horizon="5m"),
        gamma_15=_gamma_market(horizon="15m", opens_at=1_786_664_700, window_seconds=60),
        clob_15=_clob_market(horizon="15m"),
        regime=registry.classify(
            {
                "5m": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
                "15m": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            }
        ),
    )

    plan = collector.build_capture_plan(
        config=config,
        discovery=discovery,
        registry=registry,
        now_ms=1_786_665_000_000,
        attempt_id="attempt-1",
    )

    assert {item["topic"] for item in plan.capture_config["rtds_subscriptions"]} == {
        "crypto_prices",
        "crypto_prices_twap_thirty",
        "crypto_prices_twap_sixty",
    }
    assert plan.capture_config["qualification_status"] == "eligible"
    assert plan.capture_config["regime_classification"]["destination"] == "v0.6"
    assert plan.capture_config["targets"][0]["twap_window_seconds"] == 60
    assert plan.capture_config["targets"][1]["twap_window_seconds"] == 60
    plan.capture_config_path.parent.mkdir(parents=True)
    plan.capture_config_path.write_text(
        json.dumps(plan.capture_config),
        encoding="utf-8",
    )
    loaded = load_capture_config(plan.capture_config_path)
    assert len(loaded.targets) == 2


def test_build_capture_plan_quarantines_unknown_regime_but_still_builds_capture(
    tmp_path: Path,
) -> None:
    registry = load_settlement_regime_registry(_write_registry(tmp_path))
    config = _config(tmp_path, _write_registry(tmp_path))
    discovery = collector.RawCaptureDiscovery(
        expiry_seconds=1_786_665_600,
        gamma_5=_gamma_market(horizon="5m", opens_at=1_786_665_300, window_seconds=90),
        clob_5=_clob_market(horizon="5m"),
        gamma_15=_gamma_market(horizon="15m", opens_at=1_786_664_700, window_seconds=60),
        clob_15=_clob_market(horizon="15m"),
        regime=registry.classify(
            {
                "5m": "https://data.chain.link/streams/btc-usd-twap-90s-streams",
                "15m": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            }
        ),
    )

    plan = collector.build_capture_plan(
        config=config,
        discovery=discovery,
        registry=registry,
        now_ms=1_786_665_000_000,
        attempt_id="attempt-2",
    )

    assert plan.capture_config["qualification_status"] == "quarantine"
    assert plan.capture_config["regime_classification"]["reason_codes"] == [
        "unknown_settlement_regime"
    ]
    assert plan.capture_config["targets"][1]["regime_classification"] == "quarantine"
    assert plan.duration_seconds > 0


def test_missing_resolution_source_is_quarantined_without_blocking_capture(
    tmp_path: Path,
) -> None:
    registry_path = _write_registry(tmp_path)
    registry = load_settlement_regime_registry(registry_path)
    config = _config(tmp_path, registry_path)
    gamma_5 = _gamma_market(
        horizon="5m", opens_at=1_786_665_300, window_seconds=60
    )
    gamma_5["resolutionSource"] = ""
    discovery = collector.RawCaptureDiscovery(
        expiry_seconds=1_786_665_600,
        gamma_5=gamma_5,
        clob_5=_clob_market(horizon="5m"),
        gamma_15=_gamma_market(
            horizon="15m", opens_at=1_786_664_700, window_seconds=60
        ),
        clob_15=_clob_market(horizon="15m"),
        regime=registry.classify({"5m": "", "15m": "https://data.chain.link/streams/btc-usd-twap-60s-streams"}),
    )

    plan = collector.build_capture_plan(
        config=config,
        discovery=discovery,
        registry=registry,
        now_ms=1_786_665_000_000,
        attempt_id="attempt-missing-source",
    )

    assert plan.capture_config["qualification_status"] == "quarantine"
    assert plan.capture_config["targets"][1]["resolution_source_canonical"] is None


@pytest.mark.asyncio
async def test_failed_capture_persists_structured_final_summary(
    tmp_path: Path,
) -> None:
    registry_path = _write_registry(tmp_path)
    config = _config(tmp_path, registry_path)
    capture_root = tmp_path / "capture"
    capture_root.mkdir()
    plan = collector.RawCapturePlan(
        expiry_seconds=1_786_665_600,
        duration_seconds=60,
        capture_root=capture_root,
        capture_config_path=capture_root / "capture-config.json",
        capture_config={},
        forward_config=object(),
    )
    summary = {
        "schema_version": "edge-lab-forward-capture-summary.v1",
        "capture_error": {"error_code": "capture_failed"},
    }

    async def failing_runner(_config, *, duration_seconds):
        raise ForwardCaptureFinalizationError(summary)

    with pytest.raises(ForwardCaptureFinalizationError):
        await collector._run_capture_with_heartbeat(
            config=config,
            plan=plan,
            stop_event=collector.asyncio.Event(),
            completed_capture_count=0,
            runner=failing_runner,
        )

    assert json.loads(
        (capture_root / "capture-summary.json").read_text(encoding="utf-8")
    ) == summary


def test_validate_only_reports_public_only_guards(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry_path = _write_registry(tmp_path)

    exit_code = collector.main(
        [
            "--data-root",
            str(tmp_path / "rawcap"),
            "--status-path",
            str(tmp_path / "status.json"),
            "--registry",
            str(registry_path),
            "--validate-only",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["valid"] is True
    assert payload["paper_only"] is True
    assert payload["public_only"] is True
    assert payload["orders_submitted"] == 0


def test_regime_progress_counts_quarantine_and_resets_rejection_streak(
    tmp_path: Path,
) -> None:
    registry = load_settlement_regime_registry(_write_registry(tmp_path))
    quarantined = registry.classify(
        {
            "5m": "https://data.chain.link/streams/btc-usd-twap-90s-streams",
            "15m": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        }
    )
    eligible = registry.classify(
        {
            "5m": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
            "15m": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        }
    )

    first = collector.update_regime_progress(
        {},
        classification=quarantined,
        market_slugs=("five-a", "fifteen-a"),
        observed_at="2026-08-14T16:00:00Z",
    )
    second = collector.update_regime_progress(
        first,
        classification=quarantined,
        market_slugs=("five-b", "fifteen-b"),
        observed_at="2026-08-14T16:15:00Z",
    )
    recovered = collector.update_regime_progress(
        second,
        classification=eligible,
        market_slugs=("five-c", "fifteen-c"),
        observed_at="2026-08-14T16:30:00Z",
    )

    assert second["quarantine_count"] == 2
    assert second["consecutive_rejections"] == 2
    assert second["first_seen_at"] == "2026-08-14T16:00:00Z"
    assert recovered["consecutive_rejections"] == 0
    assert recovered["last_success_at"] == "2026-08-14T16:30:00Z"
