"""
Authentication utilities.

Helper functions for wallet and credential management.
"""

from collections.abc import Mapping
from typing import Any, Dict
from decimal import Decimal

from src.client import get_auth_client
from src.utils import setup_logging

logger = setup_logging()

TOKEN_DECIMALS = Decimal("1000000")


class PolymarketAdapterError(RuntimeError):
    """Raised when the installed SDK returns an unexpected contract shape."""


def require_sdk_field(subject: object, field_name: str) -> object:
    """Return a required SDK field or raise a typed adapter error."""
    if not hasattr(subject, field_name):
        raise PolymarketAdapterError(
            f"{type(subject).__module__}.{type(subject).__name__} "
            f"is missing required field '{field_name}'"
        )
    return getattr(subject, field_name)


def require_sdk_mapping(subject: object, field_name: str) -> Mapping[object, object]:
    """Return a required SDK mapping field or raise a typed adapter error."""
    value = require_sdk_field(subject, field_name)
    if not isinstance(value, Mapping):
        raise PolymarketAdapterError(
            f"{type(subject).__module__}.{type(subject).__name__}.{field_name} "
            f"must be a mapping, got {type(value).__name__}"
        )
    return value


def require_sdk_iter_items(response: object):
    """Return an SDK paginator iterator or raise a typed adapter error."""
    iter_items = require_sdk_field(response, "iter_items")
    if not callable(iter_items):
        raise PolymarketAdapterError(
            f"{type(response).__module__}.{type(response).__name__}.iter_items "
            "must be callable"
        )
    return iter_items()


def normalize_sdk_scalar(value: object, *, field_name: str) -> str:
    """Normalize enum-like SDK scalars to plain strings."""
    normalized = value.value if hasattr(value, "value") else value
    if isinstance(normalized, str):
        return normalized
    raise PolymarketAdapterError(
        f"SDK field '{field_name}' must be a string or enum-like string, "
        f"got {type(normalized).__name__}"
    )


def _balance_allowance(*, token_id: str | None = None) -> tuple[Decimal, Decimal]:
    client = get_auth_client()
    result = client.get_balance_allowance(
        asset_type="COLLATERAL" if token_id is None else "CONDITIONAL",
        token_id=token_id,
    )
    raw_balance = Decimal(str(require_sdk_field(result, "balance")))
    allowances = require_sdk_mapping(result, "allowances")
    max_allowance = max(
        (Decimal(str(value)) for value in allowances.values()),
        default=Decimal("0"),
    )
    return raw_balance / TOKEN_DECIMALS, max_allowance / TOKEN_DECIMALS


def get_wallet_address() -> str:
    """Get the wallet address associated with the authenticated client."""
    client = get_auth_client()
    return str(require_sdk_field(client, "wallet"))


def get_balances() -> Dict[str, Decimal]:
    """
    Get wallet balances.

    Returns:
        Dict with 'matic' and 'usdc' balances
    """
    try:
        balance, allowance = _balance_allowance()

        return {
            'pusd_balance': balance,
            'pusd_allowance': allowance,
            # Compatibility key retained for existing risk callers.
            'usdc_allowance': balance,
            'usdc_allowance_max': allowance,
        }
    except PolymarketAdapterError:
        raise
    except Exception as e:
        logger.error(f"Error getting balances: {e}")
        return {
            'usdc_allowance': Decimal('0'),
            'usdc_allowance_max': Decimal('0'),
        }


def check_allowances() -> Dict[str, Any]:
    """
    Check if allowances are set for trading.

    Returns:
        Dict indicating which allowances are set
    """
    try:
        _balance, allowance = _balance_allowance()
        has_allowance = allowance > Decimal("0")

        return {
            'pusd_approved': has_allowance,
            'usdc_approved': has_allowance,
            'allowance_amount': allowance,
        }
    except PolymarketAdapterError:
        raise
    except Exception as e:
        logger.error(f"Error checking allowances: {e}")
        return {
            'usdc_approved': False,
            'allowance_amount': Decimal('0'),
        }


def get_conditional_balance(
    token_id: str,
    *,
    raise_on_error: bool = False,
) -> Dict[str, Decimal]:
    """
    Get spendable balance/allowance for a specific outcome token.

    Values are normalized to token units (6 decimals).
    """
    try:
        balance, allowance = _balance_allowance(token_id=token_id)
        return {
            "balance": balance,
            "allowance": allowance,
            "sellable": min(balance, allowance),
        }
    except PolymarketAdapterError:
        raise
    except Exception as e:
        if raise_on_error:
            raise
        logger.error(f"Error getting conditional balance for {token_id[:16]}...: {e}")
        return {
            "balance": Decimal("0"),
            "allowance": Decimal("0"),
            "sellable": Decimal("0"),
        }


def set_allowances() -> bool:
    """
    Set token allowances for trading.

    Submit the official unified SDK's pUSD/conditional-token approval setup.

    Returns:
        True if successful
    """
    client = get_auth_client()

    try:
        handle = client.setup_trading_approvals()
        wait = require_sdk_field(handle, "wait")
        if not callable(wait):
            raise PolymarketAdapterError(
                f"{type(handle).__module__}.{type(handle).__name__}.wait must be callable"
            )
        wait()
        approved = bool(check_allowances().get("pusd_approved"))
        logger.info("Trading approvals confirmed: %s", approved)
        return approved
    except PolymarketAdapterError:
        raise
    except Exception as e:
        logger.error(f"Error setting allowances: {e}")
        return False


def verify_setup() -> Dict[str, Any]:
    """
    Verify the wallet is properly set up for trading.

    Returns:
        Dict with setup status and any issues found
    """
    issues = []

    try:
        # Check we can get address
        address = get_wallet_address()
        logger.info(f"Wallet address: {address}")
    except Exception as e:
        issues.append(f"Cannot get wallet address: {e}")
        return {'ok': False, 'issues': issues}

    try:
        # Check balances
        balances = get_balances()
        logger.info(f"Balances: {balances}")

        if balances.get('usdc_allowance', 0) == 0:
            issues.append("pUSD collateral balance is zero")
    except Exception as e:
        issues.append(f"Cannot check balances: {e}")

    try:
        # Check allowances
        allowances = check_allowances()
        logger.info(f"Allowances: {allowances}")

        if not allowances.get('usdc_approved', False):
            issues.append("pUSD allowance is absent")
    except Exception as e:
        issues.append(f"Cannot check allowances: {e}")

    return {
        'ok': len(issues) == 0,
        'address': address if 'address' in dir() else None,
        'issues': issues,
    }
