from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch
from fastapi.testclient import TestClient

from src.profit_system.execution import OrderLifecycleStatus
from src.profit_system.persistence import OrderRecord, PersistenceStore
from src.profit_system.web import create_app


def _default_client(tmp_path: Path, monkeypatch: MonkeyPatch) -> TestClient:
    state_dir = tmp_path / "desk-state"
    monkeypatch.setenv("POLYMM_DESK_STATE_DIR", str(state_dir))
    return TestClient(create_app(), base_url="http://127.0.0.1")


def _csrf_headers(
    client: TestClient,
    *,
    scenario: str = "paper",
    session: str = "default",
) -> dict[str, str]:
    snapshot = client.get(f"/api/v0.2/desk/snapshot?scenario={scenario}&session={session}").json()
    return {
        "origin": "http://127.0.0.1",
        "x-desk-csrf": snapshot["csrf_token"],
    }


def test_default_app_uses_persistence_backed_paper_pipeline(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client = _default_client(tmp_path, monkeypatch)

    initial = client.get("/api/v0.2/desk/snapshot?scenario=paper&session=truth").json()

    assert initial["status_bar"]["mode"] == "PAPER"
    assert initial["scenario"] == "paper"
    assert initial["execution_plan"]["state"] == "DRAFT"
    assert initial["orders"] == []
    assert initial["fills"] == []
    assert initial["action_log"] == ["pipeline-seeded"]
    assert "not live realized profit" in initial["status_bar"]["mode_banner"]

    headers = _csrf_headers(client, session="truth")
    reviewed = client.post(
        "/api/v0.2/desk/review?scenario=paper&session=truth",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "review-1"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["snapshot"]["execution_plan"]["state"] == "REVIEWED"

    confirmed = client.post(
        "/api/v0.2/desk/confirm?scenario=paper&session=truth",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "confirm-1"},
    )
    assert confirmed.status_code == 200
    confirmed_snapshot = confirmed.json()["snapshot"]
    assert confirmed_snapshot["execution_plan"]["state"] == "APPROVED"
    assert len(confirmed_snapshot["orders"]) == 2
    assert len(confirmed_snapshot["fills"]) == 2
    assert confirmed_snapshot["reconciliation"]["status"] == "matched"
    assert confirmed_snapshot["status_bar"]["realized_net_pnl"]["today"] == "1.07"
    assert (
        confirmed_snapshot["expected_vs_realized"][0]["expected_net_pnl"]
        == confirmed_snapshot["execution_plan"]["expected_net_profit"]
    )
    assert Decimal(confirmed_snapshot["expected_vs_realized"][0]["expected_net_pnl"]) != Decimal(
        confirmed_snapshot["opportunities"][0]["tradable_edge"]
    )

    db_path = tmp_path / "desk-state" / "paper__truth.sqlite3"
    store = PersistenceStore(db_path)
    try:
        orders = store.list_orders()
        fills = store.list_fills()
        pnl_rows = store.list_pnl_attribution()
        ledger = store.load_restart_context()
    finally:
        store.close()

    assert {order.order_id for order in orders} == {
        item["id"] for item in confirmed_snapshot["orders"]
    }
    assert {fill.fill_id for fill in fills} == {item["id"] for item in confirmed_snapshot["fills"]}
    assert len(pnl_rows) == 1
    realized_net_pnl = sum(
        (Decimal(row["realized_net_pnl"]) for row in confirmed_snapshot["fills"]),
        Decimal("0"),
    )
    assert realized_net_pnl == Decimal("1.070000")
    assert ledger["kill_state"] is None

    restarted = _default_client(tmp_path, monkeypatch)
    restarted_snapshot = restarted.get(
        "/api/v0.2/desk/snapshot?scenario=paper&session=truth"
    ).json()
    assert restarted_snapshot["csrf_token"] == confirmed_snapshot["csrf_token"]
    assert restarted_snapshot["orders"] == confirmed_snapshot["orders"]
    assert restarted_snapshot["fills"] == confirmed_snapshot["fills"]

    duplicate = restarted.post(
        "/api/v0.2/desk/confirm?scenario=paper&session=truth",
        headers=_csrf_headers(restarted, session="truth"),
        json={"actor": "desk-operator", "idempotency_key": "confirm-1"},
    )
    assert duplicate.status_code == 200
    assert (
        duplicate.json()["snapshot"]["snapshot_version"] == restarted_snapshot["snapshot_version"]
    )
    assert duplicate.json()["snapshot"]["orders"] == restarted_snapshot["orders"]

    canceled = restarted.post(
        "/api/v0.2/desk/cancel-all?scenario=paper&session=truth",
        headers=_csrf_headers(restarted, scenario="paper", session="truth"),
        json={"actor": "desk-operator", "idempotency_key": "cancel-after-fill"},
    )
    assert canceled.status_code == 200
    canceled_snapshot = canceled.json()["snapshot"]
    assert canceled.json()["message"] == "没有可取消的活动订单。"
    assert canceled_snapshot["scenario"] == "paper"
    assert canceled_snapshot["execution_plan"]["state"] == "APPROVED"
    assert [item["state"] for item in canceled_snapshot["orders"]] == ["FILLED", "FILLED"]
    assert canceled_snapshot["fills"] == restarted_snapshot["fills"]


def test_default_backend_modes_are_distinct_and_non_paper_modes_fail_closed(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client = _default_client(tmp_path, monkeypatch)

    shadow = client.get("/api/v0.2/desk/snapshot?scenario=shadow&session=modes").json()
    assert shadow["scenario"] == "shadow"
    assert shadow["status_bar"]["mode"] == "SHADOW"
    assert shadow["status_bar"]["is_live_locked"] is False
    assert shadow["status_bar"]["connections"]["data"] == "live"
    assert shadow["status_bar"]["connections"]["orders"] == "blocked"
    assert "observational only" in shadow["status_bar"]["mode_banner"]
    assert shadow["orders"] == []
    assert shadow["fills"] == []

    shadow_headers = _csrf_headers(client, scenario="shadow", session="modes")
    reviewed_shadow = client.post(
        "/api/v0.2/desk/review?scenario=shadow&session=modes",
        headers=shadow_headers,
        json={"actor": "desk-operator", "idempotency_key": "review-shadow"},
    )
    assert reviewed_shadow.status_code == 200
    assert reviewed_shadow.json()["snapshot"]["execution_plan"]["state"] == "REVIEWED"
    blocked_shadow = client.post(
        "/api/v0.2/desk/confirm?scenario=shadow&session=modes",
        headers=shadow_headers,
        json={"actor": "desk-operator", "idempotency_key": "confirm-shadow"},
    )
    assert blocked_shadow.status_code == 409
    assert "observational only" in blocked_shadow.json()["detail"]
    shadow_after = client.get("/api/v0.2/desk/snapshot?scenario=shadow&session=modes").json()
    assert shadow_after["orders"] == []
    assert shadow_after["fills"] == []

    live = client.get("/api/v0.2/desk/snapshot?scenario=live-canary&session=modes").json()
    assert live["scenario"] == "live"
    assert live["status_bar"]["mode"] == "LIVE_CANARY"
    assert live["status_bar"]["is_live_locked"] is True
    assert live["status_bar"]["connections"]["data"] == "disconnected"
    assert live["status_bar"]["connections"]["orders"] == "blocked"
    assert live["status_bar"]["connections"]["market_data_age_ms"] is None
    assert live["status_bar"]["available_cash"] == "0"
    assert "LIVE_BLOCKED" in live["status_bar"]["mode_banner"]
    assert live["execution_plan"]["live_lock_reason"].startswith("LIVE_BLOCKED")

    live_headers = _csrf_headers(client, scenario="live-canary", session="modes")
    reviewed_live = client.post(
        "/api/v0.2/desk/review?scenario=live-canary&session=modes",
        headers=live_headers,
        json={"actor": "desk-operator", "idempotency_key": "review-live"},
    )
    assert reviewed_live.status_code == 200
    blocked_live = client.post(
        "/api/v0.2/desk/confirm?scenario=live-canary&session=modes",
        headers=live_headers,
        json={"actor": "desk-operator", "idempotency_key": "confirm-live"},
    )
    assert blocked_live.status_code == 409
    assert "LIVE_BLOCKED" in blocked_live.json()["detail"]
    live_after = client.get("/api/v0.2/desk/snapshot?scenario=live&session=modes").json()
    assert live_after["orders"] == []
    assert live_after["fills"] == []

    invalid = client.get("/api/v0.2/desk/snapshot?scenario=live-limited&session=modes")
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "scenario must be one of: paper, shadow, live"


def test_default_backend_preserves_the_complete_order_lifecycle(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client = _default_client(tmp_path, monkeypatch)
    client.get("/api/v0.2/desk/snapshot?scenario=paper&session=lifecycle")
    db_path = tmp_path / "desk-state" / "paper__lifecycle.sqlite3"
    store = PersistenceStore(db_path)
    try:
        for index, status in enumerate(OrderLifecycleStatus):
            store.upsert_order(
                OrderRecord(
                    order_id=f"order-{status.value.lower()}",
                    venue_order_id=f"venue-{index}",
                    lifecycle_state=status.value,
                    market_id="track-a-m1",
                    token_id="track-a-y1",
                    side="buy",
                    price=Decimal("0.40"),
                    quantity=Decimal("1"),
                    updated_at=f"2026-08-30T00:{index:02d}:00Z",
                ),
                idempotency_key=f"seed-{status.value.lower()}",
            )
        store.upsert_order(
            OrderRecord(
                order_id="order-unknown",
                venue_order_id="venue-unknown",
                lifecycle_state="venue_extension_state",
                market_id="track-a-m1",
                token_id="track-a-y1",
                side="buy",
                price=Decimal("0.40"),
                quantity=Decimal("1"),
                updated_at="2026-08-30T00:59:00Z",
            ),
            idempotency_key="seed-unknown",
        )
    finally:
        store.close()

    snapshot = client.get("/api/v0.2/desk/snapshot?scenario=paper&session=lifecycle").json()
    orders = {order["id"]: order for order in snapshot["orders"]}
    for status in OrderLifecycleStatus:
        order = orders[f"order-{status.value.lower()}"]
        assert order["state"] == status.value
        assert order["venue_status"] == status.value.lower()
    assert orders["order-unknown"]["state"] == "ATTENTION_REQUIRED"
    assert orders["order-unknown"]["venue_status"] == "venue_extension_state"


def test_default_app_kill_is_durable_and_blocks_future_mutations(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    client = _default_client(tmp_path, monkeypatch)
    headers = _csrf_headers(client, session="kill-default")

    kill = client.post(
        "/api/v0.2/desk/kill?scenario=paper&session=kill-default",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "kill-1"},
    )
    assert kill.status_code == 200
    assert kill.json()["snapshot"]["status_bar"]["mode"] == "KILLED"
    assert kill.json()["snapshot"]["status_bar"]["kill_switch_engaged"] is True

    restarted = _default_client(tmp_path, monkeypatch)
    restarted_snapshot = restarted.get(
        "/api/v0.2/desk/snapshot?scenario=paper&session=kill-default"
    ).json()
    assert restarted_snapshot["status_bar"]["mode"] == "KILLED"
    assert restarted_snapshot["reconciliation"]["status"] == "attention"

    blocked_review = restarted.post(
        "/api/v0.2/desk/review?scenario=paper&session=kill-default",
        headers=_csrf_headers(restarted, session="kill-default"),
        json={"actor": "desk-operator", "idempotency_key": "review-after-kill"},
    )
    assert blocked_review.status_code == 409
    assert "Kill switch engaged" in blocked_review.json()["detail"]
