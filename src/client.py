"""
CLOB Client wrapper with authentication support.

Provides both read-only and authenticated client access.
"""

from typing import Optional
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from src.config import (
    CLOB_API_URL,
    CHAIN_ID,
    POLY_PRIVATE_KEY,
    POLY_API_KEY,
    POLY_API_SECRET,
    POLY_PASSPHRASE,
    POLY_SIGNATURE_TYPE,
    POLY_FUNDER,
    has_credentials,
)
from src.utils import setup_logging

logger = setup_logging()

# Module-level clients (singletons)
_read_client: Optional[ClobClient] = None
_auth_client: Optional[ClobClient] = None


def get_client() -> ClobClient:
    """
    Get read-only CLOB client.

    Use this for operations that don't require authentication:
    - Fetching order books
    - Getting market info
    - Price queries
    """
    global _read_client

    if _read_client is None:
        _read_client = ClobClient(
            host=CLOB_API_URL,
            chain_id=CHAIN_ID
        )
        logger.debug("Created read-only CLOB client")

    return _read_client


def get_auth_client() -> ClobClient:
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
                "Set POLY_PRIVATE_KEY and, for signature_type=1, POLY_FUNDER. "
                "Optional POLY_API_KEY/POLY_API_SECRET/POLY_PASSPHRASE can also be provided."
            )

        # Create client with signing material. Some accounts also require funder/signature_type.
        _auth_client = ClobClient(
            host=CLOB_API_URL,
            chain_id=CHAIN_ID,
            key=POLY_PRIVATE_KEY,
            signature_type=POLY_SIGNATURE_TYPE,
            funder=POLY_FUNDER,
        )

        if all([POLY_API_KEY, POLY_API_SECRET, POLY_PASSPHRASE]):
            _auth_client.set_api_creds(ApiCreds(
                api_key=POLY_API_KEY,
                api_secret=POLY_API_SECRET,
                api_passphrase=POLY_PASSPHRASE
            ))
        else:
            creds = _auth_client.create_or_derive_api_creds()
            _auth_client.set_api_creds(creds)

        logger.info("Created authenticated CLOB client")

    return _auth_client


def reset_clients():
    """Reset client singletons. Useful for testing."""
    global _read_client, _auth_client
    _read_client = None
    _auth_client = None
