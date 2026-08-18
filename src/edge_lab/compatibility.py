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
    """Unconditionally reject new orders until Track C owns this boundary.

    A plain mapping of booleans is diagnostic evidence, not a release
    capability: it has no freshness, host, credential, adapter, or probe
    provenance.  The parameter remains only for source compatibility and is
    deliberately unable to weaken the stop.
    """
    del audit
    raise LiveExecutionBlocked(
        "CLOB V2/unified live readiness is blocked: no verified production "
        "double-maker venue adapter owns the release boundary"
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
        # Installing an SDK does not create or verify the strategy-specific
        # production ProbeVenue required by Track C.
        "repository_v2_adapter_implemented": False,
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
        "new_live_orders_ready": False,
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
