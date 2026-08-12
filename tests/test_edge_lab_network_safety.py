"""Cross-module tests for the Edge Lab's credential-free network boundary."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests

from src.edge_lab.compatibility import compatibility_audit
from src.edge_lab.network_safety import (
    PublicNetworkSafetyError,
    configure_public_session,
    high_confidence_secret_findings,
    is_sensitive_parameter_name,
    safe_error_details,
    sanitized_response_headers,
)
from src.edge_lab.public_api import PublicAPIError, PublicPolymarketAPI
from src.edge_lab.recorder import SnapshotEnvelope
from src.edge_lab.sources import PublicSourceError, PublicSourcesClient


class StubResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = json.dumps(payload).encode("utf-8")

    def json(self) -> Any:
        return self._payload


class StubSession:
    def __init__(
        self,
        *responses: StubResponse | BaseException,
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}
        self.auth = None
        self.cookies = requests.cookies.RequestsCookieJar()
        self.proxies: dict[str, str] = {}
        self.trust_env = True
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        return self.request("GET", url, **kwargs)

    def close(self) -> None:
        self.closed = True


def test_public_session_disables_ambient_auth_and_allows_only_loopback_proxy() -> None:
    session = StubSession()
    session.proxies = {
        "http": "http://127.0.0.1:7897",
        "https": "http://localhost:7897",
    }

    assert configure_public_session(session) is session
    assert session.trust_env is False

    session.proxies["https"] = "https://proxy.example:443"
    with pytest.raises(
        PublicNetworkSafetyError,
        match="unauthenticated loopback",
    ):
        configure_public_session(session)

    session.proxies["https"] = (
        "http://user:" + "password@127.0.0.1:7897"
    )
    with pytest.raises(PublicNetworkSafetyError):
        configure_public_session(session)


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "api-key",
        "apiKey",
        "apiToken",
        "auth",
        "password",
        "privateKey",
        "accessToken",
        "proxyAuthorization",
    ],
)
def test_sensitive_parameter_names_cover_snake_kebab_and_camel_case(
    name: str,
) -> None:
    assert is_sensitive_parameter_name(name)


def test_public_session_rejects_headers_auth_and_cookie_jar_without_echo() -> None:
    session = StubSession()
    session.headers["Authorization"] = "sentinel-authorization-value"
    with pytest.raises(PublicNetworkSafetyError) as caught:
        configure_public_session(session)
    assert "sentinel-authorization-value" not in str(caught.value)

    session.headers.clear()
    session.headers["X-Api-Key"] = "sentinel-api-key"
    with pytest.raises(PublicNetworkSafetyError) as caught:
        configure_public_session(session)
    assert "sentinel-api-key" not in str(caught.value)

    session.headers.clear()
    session.auth = ("sentinel-user", "sentinel-password")
    with pytest.raises(PublicNetworkSafetyError) as caught:
        configure_public_session(session)
    assert "sentinel-password" not in str(caught.value)

    session.auth = None
    session.cookies.set("session", "sentinel-cookie")
    with pytest.raises(PublicNetworkSafetyError) as caught:
        configure_public_session(session)
    assert "sentinel-cookie" not in str(caught.value)


def test_response_header_allowlist_drops_all_credential_carriers() -> None:
    safe = sanitized_response_headers(
        {
            "Content-Type": "application/json",
            "ETag": "public-etag",
            "Last-Modified": (
                "eyJabcdefghijk"
                + ".abcdefghijk"
                + ".abcdefghijk"
            ),
            "Set-Cookie": "__cf" + "_bm=sentinel-cookie",
            "Cookie": "sentinel-cookie",
            "Authorization": "Bearer sentinel-authorization",
            "Proxy-Authorization": "Basic sentinel-proxy-auth",
            "X-Access-Token": "sentinel-token",
            "X-Signature": "sentinel-signature",
            "X-Credential": "sentinel-credential",
        }
    )

    assert safe == {
        "Content-Type": "application/json",
        "ETag": "public-etag",
    }
    assert "sentinel" not in json.dumps(safe)


def test_snapshot_envelope_applies_the_same_strict_header_allowlist() -> None:
    envelope = SnapshotEnvelope(
        payload={},
        raw_body=b"{}",
        status_code=200,
        request_url="https://clob.polymarket.com/book",
        response_headers={
            "Content-Type": "application/json",
            "X-Debug-Header": "sentinel-debug-value",
        },
    )

    assert dict(envelope.response_headers) == {
        "Content-Type": "application/json"
    }
    assert "sentinel" not in json.dumps(dict(envelope.response_headers))


def test_public_sources_sanitize_headers_and_network_error_messages() -> None:
    response = StubResponse(
        {"markets": [], "next_cursor": None},
        headers={
            "Content-Type": "application/json",
            "Set-Cookie": "__cf" + "_bm=sentinel-cookie",
            "Authorization": "Bearer sentinel-authorization",
        },
    )
    session = StubSession(response)
    fetched = PublicSourcesClient(session=session).gamma_markets()

    assert session.trust_env is False
    assert fetched.raw.metadata.response_headers == {
        "Content-Type": "application/json"
    }

    poisoned = StubSession(
        requests.exceptions.ProxyError(
            "proxy http://user:" + "sentinel-password@evil.invalid/"
            + "?" + "api_key=sentinel-query"
        )
    )
    with pytest.raises(PublicSourceError) as caught:
        PublicSourcesClient(session=poisoned, retries=0).gamma_markets()
    rendered = str(caught.value)
    assert "sentinel" not in rendered
    assert caught.value.code == "proxy_unavailable"


def test_public_sources_sanitize_unexpected_transport_errors_and_clear_cookies() -> None:
    class UnexpectedFailureSession(StubSession):
        def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
            self.cookies.set("session", "sentinel-cookie")
            raise RuntimeError(
                "https://evil.invalid/?" + "api_key=sentinel-query"
            )

    session = UnexpectedFailureSession()
    with pytest.raises(PublicSourceError) as caught:
        PublicSourcesClient(session=session, retries=0).gamma_markets()

    assert caught.value.code == "unexpected_failure"
    assert "sentinel" not in str(caught.value)
    assert not session.cookies


def test_public_api_quotes_identifiers_and_never_persists_raw_request_errors() -> None:
    session = StubSession(StubResponse({"ok": True}))
    api = PublicPolymarketAPI(session=session, retries=0)
    result = api.reward_market("condition?" + "api_key=sentinel-query")

    assert result == {"ok": True}
    assert session.trust_env is False
    dispatched = session.calls[0]["url"]
    assert urlsplit(dispatched).query == ""
    assert "%3Fapi_key%3Dsentinel-query" in dispatched
    assert session.calls[0]["allow_redirects"] is False

    poisoned = StubSession(
        requests.exceptions.ProxyError(
            "proxy http://user:" + "sentinel-password@evil.invalid/"
            + "?" + "api_key=sentinel-query"
        )
    )
    with pytest.raises(PublicAPIError) as caught:
        PublicPolymarketAPI(session=poisoned, retries=0).reward_markets()
    rendered = str(caught.value)
    assert "sentinel" not in rendered
    assert caught.value.error_code == "proxy_unavailable"


def test_public_api_rejects_credential_parameters_before_dispatch() -> None:
    session = StubSession(StubResponse({"ok": True}))
    api = PublicPolymarketAPI(session=session, retries=0)

    with pytest.raises(PublicAPIError) as caught:
        api._json(
            "GET",
            "https://clob.polymarket.com/book",
            params={"apiToken": "sentinel-token"},
        )

    assert caught.value.error_code == "sensitive_request_parameter"
    assert session.calls == []
    assert "sentinel" not in str(caught.value)


def test_compatibility_audit_uses_safe_error_type_and_code_only() -> None:
    session = StubSession(
        requests.exceptions.ProxyError(
            "proxy http://user:" + "sentinel-password@evil.invalid/"
            + "?" + "api_key=sentinel-query"
        )
    )

    result = compatibility_audit(session=session)
    rendered = json.dumps(result, sort_keys=True)

    assert session.trust_env is False
    assert "sentinel" not in rendered
    assert result["geoblock"]["error_code"] == "proxy_unavailable"
    assert "error" not in result["geoblock"]


def test_compatibility_audit_rejects_non_boolean_geoblock_values() -> None:
    session = StubSession(
        StubResponse({"blocked": "api_key=sentinel-query"})
    )

    result = compatibility_audit(session=session)
    rendered = json.dumps(result, sort_keys=True)

    assert result["geoblock"] == {
        "request_ok": False,
        "blocked": None,
        "error_type": "InvalidResponse",
        "error_code": "invalid_geoblock_response",
    }
    assert "sentinel" not in rendered


def test_high_confidence_scan_reports_only_labels_and_paths() -> None:
    artifact = {
        "safe": "public market data",
        "nested": {
            "private_key": "sentinel-private-key",
            "url": "https://public.invalid/data?" + "api_key=sentinel-query",
            "header": "Authorization: Bearer abcdefghijklmnop",
        },
    }

    findings = high_confidence_secret_findings(artifact)
    rendered = json.dumps(findings, sort_keys=True)

    assert {finding["kind"] for finding in findings} == {
        "authorization_value",
        "sensitive_key_value",
        "sensitive_query",
    }
    assert "sentinel" not in rendered


def test_safe_error_details_never_retains_exception_text() -> None:
    details = safe_error_details(
        RuntimeError(
            "https://user:" + "sentinel-password@evil.invalid/"
            + "?" + "api_key=sentinel-query"
        ),
        code="operation_failed",
    )

    assert details == {
        "error_type": "RuntimeError",
        "error_code": "operation_failed",
    }
    assert "sentinel" not in json.dumps(details)
