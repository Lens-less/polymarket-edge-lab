"""Fail-closed unified-SDK and compliance preflight for live execution."""

from __future__ import annotations

from importlib import metadata
from collections.abc import Mapping
from typing import Any, Optional

import requests

from .network_safety import (
    PublicNetworkSafetyError,
    configure_public_session,
    discard_session_cookies,
    request_error_code,
    safe_error_details,
)


class LiveExecutionBlocked(RuntimeError):
    """Raised when the repository cannot safely submit current CLOB orders."""


REQUIRED_LIVE_CHECKS = (
    "legacy_v1_dependency_absent",
    "current_sdk_installed",
    "repository_v2_adapter_implemented",
    "pusd_balance_and_allowance_reconciled",
    "v2_signature_path_verified",
    "trade_terminal_status_reconciled",
    "geoblock_request_ok",
    "geoblock_allows_new_orders",
)

AUTHENTICATED_EVIDENCE_CHECKS = (
    "pusd_balance_and_allowance_reconciled",
    "v2_signature_path_verified",
    "trade_terminal_status_reconciled",
)


def installed_version(distribution: str) -> Optional[str]:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def assert_new_orders_disabled(
    audit: Mapping[str, Any] | None = None,
) -> None:
    """Reject new orders unless one complete eight-check audit is supplied.

    The historical name is retained for callers that already fail closed here.
    Passing an incomplete, malformed, or stale-in-shape audit never weakens the
    stop.  A caller that intentionally enables live submission must first run
    :func:`compatibility_audit`, attach independently verified authenticated
    evidence, and pass that exact result into this boundary.
    """
    checks = audit.get("checks") if isinstance(audit, Mapping) else None
    valid_checks = isinstance(checks, Mapping) and set(checks) == set(
        REQUIRED_LIVE_CHECKS
    )
    if valid_checks and isinstance(checks, Mapping):
        blocking = [
            label
            for label in REQUIRED_LIVE_CHECKS
            if checks.get(label) is not True
        ]
    else:
        blocking = list(REQUIRED_LIVE_CHECKS)
    if (
        valid_checks
        and not blocking
        and isinstance(audit, Mapping)
        and audit.get("new_live_orders_ready") is True
    ):
        return
    raise LiveExecutionBlocked(
        "CLOB V2/unified live readiness is blocked; all eight checks must pass. "
        f"Blocking checks: {', '.join(blocking)}"
    )


def compatibility_audit(
    timeout: float = 20,
    *,
    session: Optional[requests.Session] = None,
    authenticated_evidence: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Return a sanitized readiness audit without reading credentials.

    ``authenticated_evidence`` is deliberately explicit: its three values must
    come from a separately authorized, metered probe.  Merely having a private
    key in the environment cannot turn those checks green.
    """
    legacy_version = installed_version("py-clob-client")
    v2_version = installed_version("py-clob-client-v2")
    unified_version = installed_version("polymarket-client")

    actual_session = session or requests.Session()
    owns_session = session is None
    geoblock: dict[str, Any]
    try:
        configure_public_session(actual_session)
        response = actual_session.get(
            "https://polymarket.com/api/geoblock",
            timeout=timeout,
            allow_redirects=False,
        )
        discard_session_cookies(actual_session)
        status_code = int(response.status_code)
        if not 200 <= status_code < 300:
            geoblock = {
                "request_ok": False,
                "blocked": None,
                "error_type": (
                    "RedirectResponse"
                    if 300 <= status_code < 400
                    else "HTTPStatus"
                ),
                "error_code": (
                    "redirect_rejected"
                    if 300 <= status_code < 400
                    else "http_status_failure"
                ),
                "status_code": status_code,
            }
            raw = None
        else:
            raw = response.json()
        if raw is None:
            pass
        elif (
            not isinstance(raw, dict)
            or not isinstance(raw.get("blocked"), bool)
        ):
            geoblock = {
                "request_ok": False,
                "blocked": None,
                "error_type": "InvalidResponse",
                "error_code": "invalid_geoblock_response",
            }
        else:
            geoblock = {
                "request_ok": True,
                "blocked": raw.get("blocked"),
                # Deliberately omit country/region/IP from the persisted audit.
                "privacy": "location fields omitted",
            }
    except Exception as exc:
        details = safe_error_details(
            exc,
            code=(
                exc.code
                if isinstance(exc, PublicNetworkSafetyError)
                else request_error_code(exc)
            ),
        )
        geoblock = {
            "request_ok": False,
            "blocked": None,
            **details,
        }
    finally:
        discard_session_cookies(actual_session)
        if owns_session:
            actual_session.close()

    supplied_evidence = authenticated_evidence or {}
    unknown_evidence = set(supplied_evidence) - set(AUTHENTICATED_EVIDENCE_CHECKS)
    if unknown_evidence:
        raise ValueError(
            "unsupported authenticated evidence checks: "
            + ", ".join(sorted(unknown_evidence))
        )
    if any(type(value) is not bool for value in supplied_evidence.values()):
        raise TypeError("authenticated evidence values must be bool")

    checks = {
        "legacy_v1_dependency_absent": legacy_version is None,
        "current_sdk_installed": bool(v2_version or unified_version),
        "repository_v2_adapter_implemented": unified_version is not None,
        "pusd_balance_and_allowance_reconciled": supplied_evidence.get(
            "pusd_balance_and_allowance_reconciled", False
        ),
        "v2_signature_path_verified": supplied_evidence.get(
            "v2_signature_path_verified", False
        ),
        "trade_terminal_status_reconciled": supplied_evidence.get(
            "trade_terminal_status_reconciled", False
        ),
        "geoblock_request_ok": bool(geoblock.get("request_ok")),
        "geoblock_allows_new_orders": geoblock.get("blocked") is False,
    }
    return {
        "installed": {
            "py_clob_client_legacy": legacy_version,
            "py_clob_client_v2": v2_version,
            "polymarket_client_unified": unified_version,
        },
        "geoblock": geoblock,
        "checks": checks,
        "read_only_research_ready": True,
        "new_live_orders_ready": all(checks.values()),
        "blocking_reasons": [
            label for label, passed in checks.items() if not passed
        ],
        "warning": (
            "This audit never reads credentials and cannot verify authenticated "
            "order signing or settlement by itself. Authenticated evidence must "
            "come from a separately authorized metered probe; the double-maker "
            "strategy remains ineligible until its Gate 0 and shadow gates pass."
        ),
    }
