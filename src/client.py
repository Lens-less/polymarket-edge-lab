"""
CLOB Client wrapper with authentication support.

Provides both read-only and authenticated client access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from src.config import (
    POLY_PRIVATE_KEY,
    POLY_API_KEY,
    POLY_API_SECRET,
    POLY_PASSPHRASE,
    POLY_FUNDER,
    has_credentials,
)
from src.utils import setup_logging

logger = setup_logging()

if TYPE_CHECKING:
    from polymarket import ApiKeyCreds, PublicClient, SecureClient
else:
    ApiKeyCreds = Any
    PublicClient = Any
    SecureClient = Any

# Module-level clients (singletons)
_read_client: Optional[PublicClient] = None
_auth_client: Optional[SecureClient] = None


def _load_polymarket_clients() -> tuple[
    type[PublicClient],
    type[SecureClient],
    type[ApiKeyCreds],
]:
    try:
        from polymarket import ApiKeyCreds as loaded_creds
        from polymarket import PublicClient as loaded_public
        from polymarket import SecureClient as loaded_secure
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "polymarket-client 0.6.0 is required for CLOB access."
        ) from exc
    return loaded_public, loaded_secure, loaded_creds


def get_client() -> PublicClient:
    """
    Get read-only CLOB client.

    Use this for operations that don't require authentication:
    - Fetching order books
    - Getting market info
    - Price queries
    """
    global _read_client

    if _read_client is None:
        public_type, _secure_type, _api_creds_type = _load_polymarket_clients()
        _read_client = public_type()
        logger.debug("Created official read-only Polymarket client")

    return _read_client


def get_auth_client() -> SecureClient:
    """
    Get authenticated CLOB client.

    Use this for operations that require authentication:
    - Placing orders
    - Canceling orders
    - Checking balances
    - Managing positions

    Raises:
        ValueError: If credentials are not configured
    """
    global _auth_client

    if _auth_client is None:
        if not has_credentials():
            raise ValueError(
                "Authentication credentials not configured. "
                "Set POLY_PRIVATE_KEY and the deposit wallet in POLY_FUNDER when "
                "the signer does not directly hold collateral. Optional API key "
                "credentials must be provided as a complete triple."
            )
        public_type, secure_type, api_creds_type = _load_polymarket_clients()
        del public_type
        supplied = (POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE)
        if any(supplied) and not all(supplied):
            raise ValueError(
                "POLY_API_KEY, POLY_API_SECRET, and POLY_PASSPHRASE must be "
                "provided together"
            )
        credentials = (
            api_creds_type(
                apiKey=POLY_API_KEY,
                secret=POLY_API_SECRET,
                passphrase=POLY_PASSPHRASE,
            )
            if all(supplied)
            else None
        )
        _auth_client = secure_type.create(
            private_key=POLY_PRIVATE_KEY,
            wallet=POLY_FUNDER,
            credentials=credentials,
        )
        logger.info("Created official authenticated Polymarket client")

    return _auth_client


def reset_clients() -> None:
    """Reset client singletons. Useful for testing."""
    global _read_client, _auth_client
    for client in (_read_client, _auth_client):
        close = getattr(client, "close", None)
        if callable(close):
            close()
    _read_client = None
    _auth_client = None
