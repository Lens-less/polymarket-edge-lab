from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import client as legacy_client
from src.profit_system import cli, readiness
from src.profit_system.execution import (
    ExecutionMode,
    OrderSide,
    QualificationLevel,
    QualificationRecord,
    RiskContext,
    RiskLimits,
    TimeInForce,
    VenueOrderSpec,
    VenuePolicyError,
    VenuePreflightRequest,
)
from src.profit_system.execution.adapters import PolymarketLiveVenueAdapter
from src.profit_system.reports import build_fault_drill_result, build_live_probe_result
from src.profit_system.web import InMemoryDeskService, create_app
from src.profit_system.web.demo import build_demo_snapshot

PRIVATE_KEY_SENTINEL = "0xSECRET_PRIVATE_KEY_SENTINEL_A1B2C3D4E5F6"
API_SECRET_SENTINEL = "pm_api_secret_SENTINEL_A1B2C3D4E5F6"
AUTH_HEADER_SENTINEL = "Bearer SECRET_AUTH_HEADER_SENTINEL_A1B2C3D4E5F6"
SENTINELS = (
    PRIVATE_KEY_SENTINEL,
    API_SECRET_SENTINEL,
    AUTH_HEADER_SENTINEL,
)


def _assert_sentinels_absent(text: str) -> None:
    for sentinel in SENTINELS:
        assert sentinel not in text


def _ready_canary_config() -> dict[str, object]:
    return {
        "schema_version": readiness.CANARY_CONFIG_SCHEMA_VERSION,
        "profile_name": "qa-canary",
        "authorization": {
            "explicit_live_authorization": True,
            "first_canary_manual_confirmation": True,
            "plan_id": "plan-001",
            "approved_by": "desk-operator",
            "approved_at": "2026-08-30T12:00:00Z",
        },
        "strategy": {
            "strategy_id": "maker.canary.demo",
            "market_whitelist": ["market-1"],
            "qualification": {
                "research_gate_passed": True,
                "shadow_gate_passed": True,
            },
        },
        "risk": {
            "max_order_notional": "1.00",
            "max_market_exposure": "2.00",
            "max_event_exposure": "2.00",
            "max_strategy_exposure": "2.00",
            "max_total_exposure": "5.00",
            "max_daily_loss": "2.00",
            "max_drawdown": "3.00",
            "max_open_orders": 2,
            "max_unmatched_leg_seconds": "15",
            "max_market_data_age_ms": 1500,
            "max_user_stream_gap_seconds": 5,
            "max_order_rate": "1",
        },
    }


def _ready_live_probe_document() -> dict[str, object]:
    return json.loads(json.dumps(build_live_probe_result(ready=True)))


def _ready_fault_drill_document() -> dict[str, object]:
    return json.loads(json.dumps(build_fault_drill_result()))


def _write_canary_config(
    tmp_path: Path,
    *,
    live_probe_result: dict[str, object],
    fault_drill_result: dict[str, object],
    extra_fields: dict[str, object] | None = None,
) -> Path:
    payload = _ready_canary_config()
    payload["live_probe_result"] = live_probe_result
    payload["fault_drill_result"] = fault_drill_result
    if extra_fields:
        payload.update(extra_fields)
    config_path = tmp_path / "canary.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def _client(tmp_path: Path) -> TestClient:
    service = InMemoryDeskService(state_dir=tmp_path / "desk-state")
    return TestClient(create_app(service=service), base_url="http://127.0.0.1")


def _limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("100"),
        max_market_exposure=Decimal("100"),
        max_event_exposure=Decimal("100"),
        max_strategy_exposure=Decimal("100"),
        max_total_exposure=Decimal("100"),
        max_daily_loss=Decimal("20"),
        max_drawdown=Decimal("20"),
        max_open_orders=10,
        max_unmatched_leg_seconds=30,
        max_market_data_age_ms=500,
        max_user_stream_gap_seconds=30,
        max_order_rate=5,
    )


def _qualification() -> QualificationRecord:
    return QualificationRecord(
        strategy_id="strat-1",
        qualification=QualificationLevel.PROBE_ALLOWED,
        allowed_modes=(ExecutionMode.LIVE_CANARY,),
        market_whitelist=("market-1",),
        risk_limits=_limits(),
        evidence_digest="digest-1",
    )


def _request() -> VenuePreflightRequest:
    return VenuePreflightRequest(
        mode=ExecutionMode.LIVE_CANARY,
        qualification=_qualification(),
        orders=(
            VenueOrderSpec(
                client_order_id="client-1",
                strategy_id="strat-1",
                market_id="market-1",
                token_id="token-1",
                event_id="event-1",
                side=OrderSide.BUY,
                quantity=Decimal("2"),
                limit_price=Decimal("0.40"),
                time_in_force=TimeInForce.GTC,
            ),
        ),
        risk_context=RiskContext(market_data_age_ms=100),
        idempotency_key="preflight-1",
        now=readiness._utc_now(),
    )


def test_doctor_omits_secret_like_unused_config_fields(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config_path = _write_canary_config(
        tmp_path,
        live_probe_result=_ready_live_probe_document(),
        fault_drill_result=_ready_fault_drill_document(),
        extra_fields={
            "credentials": {
                "private_key": PRIVATE_KEY_SENTINEL,
                "api_secret": API_SECRET_SENTINEL,
            },
            "headers": {"Authorization": AUTH_HEADER_SENTINEL},
        },
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    _assert_sentinels_absent(output)
    _assert_sentinels_absent("\n".join(caplog.messages))


def test_doctor_omits_secret_like_probe_detail_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _ready_live_probe_document()
    first_check = probe["checks"][0]
    assert isinstance(first_check, dict)
    first_check["detail"] = f"verified with {AUTH_HEADER_SENTINEL}"
    config_path = _write_canary_config(
        tmp_path,
        live_probe_result=probe,
        fault_drill_result=_ready_fault_drill_document(),
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    assert exit_code == 0
    _assert_sentinels_absent(capsys.readouterr().out)


def test_doctor_omits_secret_like_probe_evidence_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _ready_live_probe_document()
    first_check = probe["checks"][0]
    assert isinstance(first_check, dict)
    first_check["evidence"] = {
        "authorization_header": AUTH_HEADER_SENTINEL,
        "api_secret": API_SECRET_SENTINEL,
        "private_key": PRIVATE_KEY_SENTINEL,
    }
    config_path = _write_canary_config(
        tmp_path,
        live_probe_result=probe,
        fault_drill_result=_ready_fault_drill_document(),
    )

    exit_code = cli.main(["doctor", "--config", str(config_path)])

    assert exit_code == 0
    _assert_sentinels_absent(capsys.readouterr().out)


def test_snapshot_omits_unknown_secret_fields_from_seed_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "desk-state"
    state_dir.mkdir()
    seeded = build_demo_snapshot(scenario="paper", session="wire").to_wire()
    seeded["credentials"] = {
        "private_key": PRIVATE_KEY_SENTINEL,
        "api_secret": API_SECRET_SENTINEL,
    }
    seeded["headers"] = {"Authorization": AUTH_HEADER_SENTINEL}
    (state_dir / "paper__wire.json").write_text(
        json.dumps(seeded, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    client = _client(tmp_path)

    response = client.get("/api/v0.2/desk/snapshot?scenario=paper&session=wire")

    assert response.status_code == 200
    _assert_sentinels_absent(json.dumps(response.json(), sort_keys=True))


@pytest.mark.asyncio
async def test_live_adapter_constructor_does_not_touch_legacy_authenticated_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"count": 0}

    def fake_get_auth_client() -> object:
        calls["count"] += 1
        raise AssertionError("legacy authenticated client should stay lazy")

    monkeypatch.setattr(legacy_client, "get_auth_client", fake_get_auth_client)

    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: legacy_client.get_auth_client())

    assert adapter is not None
    assert calls["count"] == 0


@pytest.mark.asyncio
async def test_live_adapter_omits_sdk_exception_text_from_signing_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)

    class BrokenClient:
        async def create_limit_order(self, **kwargs):
            raise RuntimeError(f"live signing failed {PRIVATE_KEY_SENTINEL} {API_SECRET_SENTINEL}")

    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: BrokenClient())

    with pytest.raises(VenuePolicyError) as exc_info:
        await adapter.submit(
            orders=_request().orders,
            mode=ExecutionMode.LIVE_CANARY,
            idempotency_key="submit-1",
        )

    assert str(exc_info.value) == "venue order signing failed before submission"
    _assert_sentinels_absent(repr(exc_info.value))
    _assert_sentinels_absent("\n".join(caplog.messages))
