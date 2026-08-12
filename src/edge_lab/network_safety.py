"""Shared fail-closed network hygiene for credential-free research clients.

The Edge Lab is allowed to read public endpoints.  It must not inherit ambient
proxy or ``.netrc`` authentication, send cookies/authentication headers, or
persist response credentials such as ``Set-Cookie``.  This module keeps that
boundary in one small, dependency-light place.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import requests


_SENSITIVE_HEADER_PARTS = (
    "api_key",
    "auth",
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "private_key",
    "proxy_auth",
    "secret",
    "signature",
    "token",
)
_SENSITIVE_PARAMETER_PARTS = (
    "api_key",
    "api_token",
    "auth",
    "authorization",
    "client_secret",
    "credential",
    "passphrase",
    "password",
    "private_key",
    "proxy_auth",
    "secret",
    "signature",
    "access_token",
    "refresh_token",
)
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "age",
        "cache-control",
        "content-length",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    }
)
_SAFE_ERROR_TYPE = re.compile(r"[^A-Za-z0-9_.-]+")
_HIGH_CONFIDENCE_SECRET_PATTERNS = (
    (
        "private_key_pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        ),
    ),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{40,})\b"
        ),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\."
            r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    (
        "basic_auth_url",
        re.compile(r"https?://[^/@\s]+:[^/@\s]+@"),
    ),
    (
        "sensitive_query",
        re.compile(
            r"(?i)[?&](?:api[_-]?key|access[_-]?token|auth|authorization|"
            r"secret|signature|private[_-]?key|passphrase|token)="
        ),
    ),
    (
        "authorization_value",
        re.compile(
            r"(?i)(?:authorization|proxy-authorization|x-api-key)"
            r"[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+"
            r"[A-Za-z0-9+/=_-]{8,}"
        ),
    ),
)
_SENSITIVE_PERSISTED_KEYS = frozenset(
    {
        "access_token",
        "api_token",
        "api_key",
        "api_secret",
        "auth",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "mnemonic",
        "passphrase",
        "password",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "secret_key",
        "seed_phrase",
        "set_cookie",
        "signature",
    }
)


class PublicNetworkSafetyError(RuntimeError):
    """A public client configuration crossed the credential-free boundary."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _normalized_name(value: Any) -> str:
    snake_case = re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1_\2",
        str(value).strip(),
    )
    return re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")


def is_sensitive_header_name(value: Any) -> bool:
    """Return whether a header name can carry authentication material."""

    normalized = _normalized_name(value)
    return any(part in normalized for part in _SENSITIVE_HEADER_PARTS)


def is_sensitive_parameter_name(value: Any) -> bool:
    """Return whether a query/parameter name suggests credential material."""

    normalized = _normalized_name(value)
    return any(part in normalized for part in _SENSITIVE_PARAMETER_PARTS)


def _assert_loopback_proxy(value: Any) -> None:
    parsed = urlsplit(str(value))
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PublicNetworkSafetyError(
            "public session proxy must be an unauthenticated loopback HTTP(S) URL",
            code="unsafe_proxy_configuration",
        )


def configure_public_session(session: Any) -> Any:
    """Disable ambient credentials and validate explicit public-session state.

    ``requests`` consults proxy environment variables and ``.netrc`` while
    ``trust_env`` is enabled.  Assigning ``False`` is therefore mandatory even
    for clients that only expose fixed public routes.
    """

    session.trust_env = False
    headers = getattr(session, "headers", {}) or {}
    if any(is_sensitive_header_name(name) for name in headers):
        raise PublicNetworkSafetyError(
            "public session contains an authentication header",
            code="session_auth_header_forbidden",
        )
    if getattr(session, "auth", None):
        raise PublicNetworkSafetyError(
            "public session contains authentication credentials",
            code="session_auth_forbidden",
        )
    cookies = getattr(session, "cookies", None)
    if cookies:
        raise PublicNetworkSafetyError(
            "public session contains cookies",
            code="session_cookie_forbidden",
        )
    proxies = getattr(session, "proxies", {}) or {}
    if not isinstance(proxies, Mapping):
        raise PublicNetworkSafetyError(
            "public session proxy configuration must be a mapping",
            code="unsafe_proxy_configuration",
        )
    for scheme, proxy_url in proxies.items():
        if str(scheme).lower() not in {"http", "https"}:
            raise PublicNetworkSafetyError(
                "public session proxy scheme is not supported",
                code="unsafe_proxy_configuration",
            )
        _assert_loopback_proxy(proxy_url)
    return session


def discard_session_cookies(session: Any) -> None:
    """Remove response cookies before any later public request can send them."""

    cookies = getattr(session, "cookies", None)
    if cookies is not None and hasattr(cookies, "clear"):
        cookies.clear()


def sanitized_response_headers(
    headers: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return a deterministic metadata allowlist with no credential headers."""

    safe: dict[str, str] = {}
    for name, value in (headers or {}).items():
        normalized = str(name).strip().lower()
        if (
            normalized in _SAFE_RESPONSE_HEADERS
            and not is_sensitive_header_name(normalized)
            and not high_confidence_secret_findings(str(value))
        ):
            safe[str(name)] = str(value)
    return safe


def safe_error_details(error: BaseException, *, code: str) -> dict[str, str]:
    """Describe an error without retaining its message, URL, query, or value."""

    error_type = _SAFE_ERROR_TYPE.sub("_", type(error).__name__)[:80]
    return {
        "error_type": error_type or "Exception",
        "error_code": code,
    }


def request_error_code(error: BaseException) -> str:
    """Map requests failures to stable, non-secret diagnostic codes."""

    if isinstance(error, requests.TooManyRedirects):
        return "redirect_rejected"
    if isinstance(error, requests.Timeout):
        return "request_timeout"
    if isinstance(error, requests.exceptions.ProxyError):
        return "proxy_unavailable"
    if isinstance(error, requests.exceptions.SSLError):
        return "tls_failure"
    if isinstance(error, requests.ConnectionError):
        return "connection_failure"
    if isinstance(error, requests.HTTPError):
        return "http_status_failure"
    if isinstance(error, requests.RequestException):
        return "request_failure"
    return "unexpected_failure"


def high_confidence_secret_findings(
    value: Any,
    *,
    path: str = "$",
) -> tuple[dict[str, str], ...]:
    """Return only finding labels/paths, never matching secret values."""

    findings: list[dict[str, str]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_name(key)
            nested_path = f"{path}.{key}"
            if (
                normalized in _SENSITIVE_PERSISTED_KEYS
                and nested not in (None, "", [], {})
            ):
                findings.append(
                    {
                        "kind": "sensitive_key_value",
                        "path": nested_path,
                    }
                )
            findings.extend(
                high_confidence_secret_findings(
                    nested,
                    path=nested_path,
                )
            )
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for index, nested in enumerate(value):
            findings.extend(
                high_confidence_secret_findings(
                    nested,
                    path=f"{path}[{index}]",
                )
            )
    elif isinstance(value, str):
        for label, pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS:
            if pattern.search(value):
                findings.append({"kind": label, "path": path})
    return tuple(findings)


__all__ = [
    "PublicNetworkSafetyError",
    "configure_public_session",
    "discard_session_cookies",
    "is_sensitive_header_name",
    "is_sensitive_parameter_name",
    "high_confidence_secret_findings",
    "request_error_code",
    "safe_error_details",
    "sanitized_response_headers",
]
