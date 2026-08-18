from __future__ import annotations

from pathlib import Path
import re

import pytest
import requests

import src.client as client_module
from src.edge_lab import compatibility

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dependencies_use_the_official_unified_sdk_only() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.in").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert '"polymarket-client==0.6.0"' in pyproject
    assert "polymarket-client==0.6.0" in requirements
    assert "py-clob-client" not in pyproject
    assert "py-clob-client" not in requirements
    assert "pip install py-clob-client" not in dockerfile
    legacy_imports = [
        path
        for path in (ROOT / "src").rglob("*.py")
        if re.search(
            r"(?:from|import)\s+py_clob_client",
            path.read_text(encoding="utf-8"),
        )
    ]
    assert legacy_imports == []


def test_client_factory_constructs_official_public_and_secure_clients(
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class FakePublicClient:
        pass

    class FakeCreds:
        def __init__(self, **kwargs) -> None:
            calls["credentials"] = kwargs

    class FakeSecureClient:
        @classmethod
        def create(cls, **kwargs):
            calls["secure"] = kwargs
            return cls()

    monkeypatch.setattr(
        client_module,
        "_load_polymarket_clients",
        lambda: (FakePublicClient, FakeSecureClient, FakeCreds),
    )
    monkeypatch.setattr(client_module, "has_credentials", lambda: True)
    monkeypatch.setattr(client_module, "POLY_PRIVATE_KEY", "private-key")
    monkeypatch.setattr(client_module, "POLY_FUNDER", "0xdeposit")
    monkeypatch.setattr(client_module, "POLY_API_KEY", "api-key")
    monkeypatch.setattr(client_module, "POLY_API_SECRET", "api-secret")
    monkeypatch.setattr(client_module, "POLY_PASSPHRASE", "passphrase")
    client_module.reset_clients()

    assert isinstance(client_module.get_client(), FakePublicClient)
    assert isinstance(client_module.get_auth_client(), FakeSecureClient)
    assert calls["credentials"] == {
        "apiKey": "api-key",
        "secret": "api-secret",
        "passphrase": "passphrase",
    }
    secure_args = calls["secure"]
    assert isinstance(secure_args, dict)
    assert secure_args["private_key"] == "private-key"
    assert secure_args["wallet"] == "0xdeposit"
    assert isinstance(secure_args["credentials"], FakeCreds)


def test_live_order_boundary_cannot_be_released_by_a_boolean_mapping() -> None:
    complete = {label: True for label in compatibility.REQUIRED_LIVE_CHECKS}
    with pytest.raises(
        compatibility.LiveExecutionBlocked,
        match="no verified production double-maker venue adapter",
    ):
        compatibility.assert_new_orders_disabled(
            {"checks": complete, "new_live_orders_ready": True}
        )


def test_compatibility_audit_accepts_only_explicit_authenticated_evidence(
    monkeypatch,
) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, bool]:
            return {"blocked": False}

    versions = {
        "py-clob-client": None,
        "py-clob-client-v2": None,
        "polymarket-client": "0.6.0",
    }
    monkeypatch.setattr(
        compatibility,
        "installed_version",
        lambda distribution: versions[distribution],
    )
    session = requests.Session()
    monkeypatch.setattr(session, "get", lambda *args, **kwargs: Response())
    audit = compatibility.compatibility_audit(
        session=session,
        authenticated_evidence={
            label: True for label in compatibility.AUTHENTICATED_EVIDENCE_CHECKS
        },
    )

    assert audit["new_live_orders_ready"] is False
    assert audit["checks"]["current_sdk_installed"] is True
    assert audit["checks"]["repository_v2_adapter_implemented"] is False
    assert set(audit["blocking_reasons"]) == {"repository_v2_adapter_implemented"}

    with pytest.raises(ValueError, match="unsupported authenticated evidence"):
        compatibility.compatibility_audit(
            session=session, authenticated_evidence={"invented_check": True}
        )
