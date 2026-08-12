"""Fail-closed CLOB V2 and compliance preflight for research experiments."""

from __future__ import annotations

from importlib import metadata
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


def installed_version(distribution: str) -> Optional[str]:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def assert_new_orders_disabled() -> None:
    """Hard stop for the legacy live adapter.

    The repository still imports the archived V1 client.  Keeping cancellation
    and read-only diagnostics available is useful for recovery, but no new live
    order may pass through this adapter after the CLOB V2 migration.
    """
    raise LiveExecutionBlocked(
        "LIVE order placement is disabled: this repository's execution adapter "
        "uses archived py-clob-client V1. Migrate and end-to-end verify a CLOB "
        "V2/unified SDK adapter, pUSD collateral, signatures, fills, and "
        "settlement before enabling new orders."
    )


def compatibility_audit(
    timeout: float = 20,
    *,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    """Return a credential-free, sanitized readiness audit."""
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

    checks = {
        "legacy_v1_dependency_absent": legacy_version is None,
        "current_sdk_installed": bool(v2_version or unified_version),
        "repository_v2_adapter_implemented": False,
        "pusd_balance_and_allowance_reconciled": False,
        "v2_signature_path_verified": False,
        "trade_terminal_status_reconciled": False,
        "geoblock_request_ok": bool(geoblock.get("request_ok")),
        "geoblock_allows_new_orders": geoblock.get("blocked") is False,
    }
    return {
        "installed": {
            "py_clob_client_legacy": legacy_version,
            "py_clob_client_v2": v2_version,
            "polymarket_client_unified_beta": unified_version,
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
            "order signing or settlement. Those require a separately authorized "
            "metered probe."
        ),
    }
