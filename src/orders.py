"""
Order queries - unified interface for real and simulated orders.
"""

from typing import List, Optional
from decimal import Decimal

from src.config import DRY_RUN, has_credentials
from src.models import Order, Trade, OrderSide, OrderStatus
from src.simulator import get_simulator
from src.utils import setup_logging

logger = setup_logging()


def get_open_orders(
    token_id: Optional[str] = None,
    raise_on_error: bool = False,
) -> List[Order]:
    """Get open orders."""
    if DRY_RUN:
        return get_simulator().get_open_orders(token_id)

    if not has_credentials():
        return []

    from src.client import get_auth_client

    try:
        response = get_auth_client().list_open_orders(token_id=token_id)
        orders = []

        for item in response.iter_items():
            if str(item.status).upper() != 'LIVE':
                continue
            order = Order(
                id=str(item.id),
                token_id=str(item.token_id),
                side=OrderSide(item.side),
                price=Decimal(str(item.price)),
                size=Decimal(str(item.original_size)),
                filled=Decimal(str(item.size_matched)),
                status=OrderStatus.LIVE,
                is_simulated=False
            )
            if token_id is None or order.token_id == token_id:
                orders.append(order)
        return orders

    except Exception as e:
        if raise_on_error:
            raise
        logger.error(f"Error getting orders: {e}")
        return []


def get_trades(
    token_id: Optional[str] = None,
    limit: Optional[int] = 50,
    raise_on_error: bool = False,
) -> List[Trade]:
    """Get recent trades."""
    if DRY_RUN:
        trades = get_simulator().get_trades(token_id)
        if limit is not None:
            trades = trades[:limit]
        return trades

    if not has_credentials():
        return []

    from src.client import get_auth_client

    try:
        response = get_auth_client().list_account_trades(token_id=token_id)
        trades = []

        for item in response.iter_items():
            trade = Trade(
                id=str(item.id),
                order_id=str(item.taker_order_id),
                token_id=str(item.token_id),
                side=OrderSide(item.side),
                price=Decimal(str(item.price)),
                size=Decimal(str(item.size)),
                is_simulated=False
            )
            if token_id is None or trade.token_id == token_id:
                trades.append(trade)
            if limit is not None and len(trades) >= limit:
                break

        return trades

    except Exception as e:
        if raise_on_error:
            raise
        logger.error(f"Error getting trades: {e}")
        return []


def get_position(token_id: str) -> Decimal:
    """Get net position for a token (positive = long)."""
    if DRY_RUN:
        return get_simulator().get_position(token_id)

    # Position checks must use the full trade history returned by the API,
    # not the UI/default preview window.
    trades = get_trades(token_id, limit=None)
    position = Decimal("0")
    for t in trades:
        if t.side == OrderSide.BUY:
            position += t.size
        else:
            position -= t.size
    return position
