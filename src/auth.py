"""
Authentication utilities.

Helper functions for wallet and credential management.
"""

from typing import Optional, Dict, Any
from decimal import Decimal

from src.client import get_auth_client
from src.utils import setup_logging

logger = setup_logging()

TOKEN_DECIMALS = Decimal("1000000")


def _balance_allowance(*, token_id: str | None = None) -> tuple[Decimal, Decimal]:
    client = get_auth_client()
    result = client.get_balance_allowance(
        asset_type="COLLATERAL" if token_id is None else "CONDITIONAL",
        token_id=token_id,
    )
    raw_balance = Decimal(str(getattr(result, "balance", 0)))
    allowances = getattr(result, "allowances", {}) or {}
    max_allowance = max(
        (Decimal(str(value)) for value in allowances.values()),
        default=Decimal("0"),
    )
    return raw_balance / TOKEN_DECIMALS, max_allowance / TOKEN_DECIMALS


def get_wallet_address() -> str:
    """Get the wallet address associated with the authenticated client."""
    client = get_auth_client()
    return str(client.wallet)


def get_balances() -> Dict[str, Decimal]:
    """
    Get wallet balances.

    Returns:
        Dict with 'matic' and 'usdc' balances
    """
    client = get_auth_client()

    try:
        balance, allowance = _balance_allowance()

        return {
            'pusd_balance': balance,
            'pusd_allowance': allowance,
            # Compatibility key retained for existing risk callers.
            'usdc_allowance': balance,
            'usdc_allowance_max': allowance,
        }
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
    client = get_auth_client()

    try:
        _balance, allowance = _balance_allowance()
        has_allowance = allowance > Decimal("0")

        return {
            'pusd_approved': has_allowance,
            'usdc_approved': has_allowance,
            'allowance_amount': allowance,
        }
    except Exception as e:
        logger.error(f"Error checking allowances: {e}")
        return {
            'usdc_approved': False,
            'allowance_amount': Decimal('0'),
        }


def get_conditional_balance(token_id: str) -> Dict[str, Decimal]:
    """
    Get spendable balance/allowance for a specific outcome token.

    Values are normalized to token units (6 decimals).
    """
    client = get_auth_client()

    try:
        balance, allowance = _balance_allowance(token_id=token_id)
        return {
            "balance": balance,
            "allowance": allowance,
            "sellable": min(balance, allowance),
        }
    except Exception as e:
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
        handle.wait()
        approved = bool(check_allowances().get("pusd_approved"))
        logger.info("Trading approvals confirmed: %s", approved)
        return approved
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
