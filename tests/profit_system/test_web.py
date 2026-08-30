from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.profit_system.web import InMemoryDeskService, create_app


def _client(tmp_path: Path) -> TestClient:
    service = InMemoryDeskService(state_dir=tmp_path / "desk-state")
    return TestClient(create_app(service=service), base_url="http://127.0.0.1")


def _frontend_client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    static_dir = tmp_path / "frontend"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    index_file = static_dir / "index.html"
    index_file.write_text("<html>desk-shell</html>", encoding="utf-8")
    asset_file = assets_dir / "app.js"
    asset_file.write_text("console.log('desk');", encoding="utf-8")
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("do-not-serve", encoding="utf-8")
    service = InMemoryDeskService(state_dir=tmp_path / "desk-state")
    client = TestClient(
        create_app(service=service, static_dir=static_dir),
        base_url="http://127.0.0.1",
    )
    return client, index_file, secret_file


def _csrf_headers(
    client: TestClient, *, scenario: str = "paper", session: str = "default"
) -> dict[str, str]:
    snapshot = client.get(f"/api/v0.2/desk/snapshot?scenario={scenario}&session={session}").json()
    return {
        "origin": "http://127.0.0.1",
        "x-desk-csrf": snapshot["csrf_token"],
    }


def test_snapshot_serializes_authoritative_decimals_as_strings(tmp_path: Path) -> None:
    client = _client(tmp_path)

    payload = client.get("/api/v0.2/desk/snapshot?scenario=paper&session=wire").json()

    assert payload["schema_version"] == "profit-system.v0.2"
    assert isinstance(payload["status_bar"]["available_cash"], str)
    assert isinstance(payload["execution_plan"]["expected_net_profit"], str)
    assert isinstance(payload["positions"][0]["quantity"], str)
    assert payload["status_bar"]["mode"] == "PAPER"
    assert payload["csrf_token"] != "desk-csrf-paper-wire"
    assert len(payload["csrf_token"]) >= 32


def test_mutations_require_same_origin_and_csrf(tmp_path: Path) -> None:
    client = _client(tmp_path)

    response = client.post(
        "/api/v0.2/desk/review?scenario=paper&session=authz",
        json={"actor": "desk-operator", "idempotency_key": "review-1"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing Origin header."

    response = client.post(
        "/api/v0.2/desk/review?scenario=paper&session=authz",
        headers={"origin": "https://evil.example", "x-desk-csrf": "wrong"},
        json={"actor": "desk-operator", "idempotency_key": "review-1"},
    )
    assert response.status_code == 403

    guessed = client.post(
        "/api/v0.2/desk/review?scenario=paper&session=authz",
        headers={"origin": "http://127.0.0.1", "x-desk-csrf": "desk-csrf-paper-authz"},
        json={"actor": "desk-operator", "idempotency_key": "review-2"},
    )
    assert guessed.status_code == 403
    assert guessed.json()["detail"] == "Invalid desk CSRF token."


def test_non_loopback_host_is_rejected(tmp_path: Path) -> None:
    service = InMemoryDeskService(state_dir=tmp_path / "desk-state")
    client = TestClient(create_app(service=service), base_url="http://example.com")

    response = client.get("/api/v0.2/desk/snapshot?scenario=paper&session=remote")

    assert response.status_code == 403
    assert response.json()["detail"] == "Desk API is restricted to loopback hosts."


def test_frontend_catchall_only_serves_files_within_static_root(tmp_path: Path) -> None:
    client, index_file, secret_file = _frontend_client(tmp_path)

    asset = client.get("/assets/app.js")
    assert asset.status_code == 200
    assert asset.text == "console.log('desk');"

    traversal = client.get("/..%2Fsecret.txt")
    assert traversal.status_code == 200
    assert traversal.text == index_file.read_text(encoding="utf-8")
    assert traversal.text != secret_file.read_text(encoding="utf-8")

    absolute = client.get("/" + secret_file.resolve().as_posix())
    assert absolute.status_code == 200
    assert absolute.text == index_file.read_text(encoding="utf-8")


def test_frontend_catchall_requires_loopback_host(tmp_path: Path) -> None:
    static_dir = tmp_path / "frontend"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>desk-shell</html>", encoding="utf-8")
    service = InMemoryDeskService(state_dir=tmp_path / "desk-state")
    client = TestClient(
        create_app(service=service, static_dir=static_dir),
        base_url="http://example.com",
    )

    response = client.get("/")

    assert response.status_code == 403
    assert response.json()["detail"] == "Desk API is restricted to loopback hosts."


def test_review_confirm_and_restart_persist_state(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = _csrf_headers(client, session="persist")

    reviewed = client.post(
        "/api/v0.2/desk/review?scenario=paper&session=persist",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "review-1"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["snapshot"]["execution_plan"]["state"] == "REVIEWED"

    confirmed = client.post(
        "/api/v0.2/desk/confirm?scenario=paper&session=persist",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "confirm-1"},
    )
    assert confirmed.status_code == 200
    confirmed_snapshot = confirmed.json()["snapshot"]
    assert confirmed_snapshot["execution_plan"]["state"] == "APPROVED"
    assert confirmed_snapshot["orders"][0]["state"] == "FILLED"
    assert confirmed_snapshot["fills"][0]["realized_net_pnl"] == "14.250000"

    duplicate = client.post(
        "/api/v0.2/desk/confirm?scenario=paper&session=persist",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "confirm-1"},
    )
    assert duplicate.status_code == 200
    duplicate_snapshot = duplicate.json()["snapshot"]
    assert duplicate_snapshot["snapshot_version"] == confirmed_snapshot["snapshot_version"]
    assert len(duplicate_snapshot["fills"]) == len(confirmed_snapshot["fills"])

    restarted_client = _client(tmp_path)
    restarted_snapshot = restarted_client.get(
        "/api/v0.2/desk/snapshot?scenario=paper&session=persist"
    ).json()
    assert restarted_snapshot["csrf_token"] == confirmed_snapshot["csrf_token"]
    assert restarted_snapshot["execution_plan"]["state"] == "APPROVED"
    assert restarted_snapshot["orders"][0]["id"] == confirmed_snapshot["orders"][0]["id"]
    assert restarted_snapshot["fills"][0]["id"] == confirmed_snapshot["fills"][0]["id"]

    restarted_duplicate = restarted_client.post(
        "/api/v0.2/desk/confirm?scenario=paper&session=persist",
        headers=_csrf_headers(restarted_client, session="persist"),
        json={"actor": "desk-operator", "idempotency_key": "confirm-1"},
    )
    assert restarted_duplicate.status_code == 200
    assert (
        restarted_duplicate.json()["snapshot"]["snapshot_version"]
        == restarted_snapshot["snapshot_version"]
    )
    assert len(restarted_duplicate.json()["snapshot"]["fills"]) == len(restarted_snapshot["fills"])


def test_live_lock_blocks_confirm_until_health_recovers(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = _csrf_headers(client, scenario="fake-live-lock", session="lock")

    review_response = client.post(
        "/api/v0.2/desk/review?scenario=fake-live-lock&session=lock",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "review-lock"},
    )
    assert review_response.status_code == 200
    assert review_response.json()["snapshot"]["status_bar"]["mode"] == "LIVE_CANARY"

    confirm_response = client.post(
        "/api/v0.2/desk/confirm?scenario=fake-live-lock&session=lock",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "confirm-lock"},
    )
    assert confirm_response.status_code == 409
    assert "confirm disabled" in confirm_response.json()["detail"]


def test_kill_is_persistent_and_cancel_all_marks_reconciliation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    headers = _csrf_headers(client, session="kill-session")

    cancel_all = client.post(
        "/api/v0.2/desk/cancel-all?scenario=paper&session=kill-session",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "cancel-all-1"},
    )
    assert cancel_all.status_code == 200
    assert cancel_all.json()["snapshot"]["reconciliation"]["status"] == "attention"
    assert any(
        item["state"] == "CANCELED"
        for item in cancel_all.json()["snapshot"]["orders"]
        if item["id"] == "ord-prev-live"
    )

    kill = client.post(
        "/api/v0.2/desk/kill?scenario=paper&session=kill-session",
        headers=headers,
        json={"actor": "desk-operator", "idempotency_key": "kill-1"},
    )
    assert kill.status_code == 200
    killed_snapshot = kill.json()["snapshot"]
    assert killed_snapshot["status_bar"]["mode"] == "KILLED"
    assert killed_snapshot["status_bar"]["kill_switch_engaged"] is True

    restarted_client = _client(tmp_path)
    restarted_headers = _csrf_headers(restarted_client, session="kill-session")
    restarted_snapshot = restarted_client.get(
        "/api/v0.2/desk/snapshot?scenario=paper&session=kill-session"
    ).json()
    assert restarted_snapshot["status_bar"]["mode"] == "KILLED"

    blocked_review = restarted_client.post(
        "/api/v0.2/desk/review?scenario=paper&session=kill-session",
        headers=restarted_headers,
        json={"actor": "desk-operator", "idempotency_key": "review-after-kill"},
    )
    assert blocked_review.status_code == 409
    assert "Kill switch engaged" in blocked_review.json()["detail"]
