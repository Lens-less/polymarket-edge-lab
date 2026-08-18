"""
Pricing and order book data from Polymarket CLOB API.
"""

from typing import List, Optional, Dict, Any
from src.client import get_client
from src.models import OrderBook, PriceLevel
from src.utils import setup_logging

logger = setup_logging()


def get_midpoint(token_id: str) -> Optional[float]:
    """
    Get current midpoint price for a token.

    Args:
        token_id: The token ID

    Returns:
        Midpoint price or None if unavailable
    """
    client = get_client()

    try:
        result = client.get_midpoint(token_id=token_id)
        if result is not None:
            return float(result)
        return None
    except Exception as e:
        logger.error(f"Error fetching midpoint for {token_id}: {e}")
        return None


def get_price(token_id: str, side: str = "BUY") -> Optional[float]:
    """
    Get current best price for a token.

    Args:
        token_id: The token ID
        side: "BUY" or "SELL"

    Returns:
        Best price for the given side or None
    """
    client = get_client()

    try:
        result = client.get_price(token_id=token_id, side=side)
        if result is not None:
            return float(result)
        return None
    except Exception as e:
        logger.error(f"Error fetching price for {token_id}: {e}")
        return None


def get_order_book(token_id: str) -> Optional[OrderBook]:
    """
    Get full order book for a token.

    Args:
        token_id: The token ID

    Returns:
        OrderBook object or None if unavailable
    """
    from src.rate_limiter import get_market_data_limiter
    get_market_data_limiter().wait_sync()

    client = get_client()

    try:
        result = client.get_order_book(token_id=token_id)
        return _parse_order_book(token_id, result)
    except Exception as e:
        error_str = str(e)
        if "404" in error_str or "No orderbook exists" in error_str:
            logger.warning(f"No CLOB order book for {token_id[:16]}... - market may not be active")
        else:
            logger.error(f"Error fetching order book for {token_id}: {e}")
        return None


def get_order_books(token_ids: List[str]) -> Dict[str, OrderBook]:
    """
    Get order books for multiple tokens in one call.

    Args:
        token_ids: List of token IDs

    Returns:
        Dictionary mapping token_id to OrderBook
    """
    client = get_client()

    try:
        results = client.get_order_books(token_ids=token_ids)

        books = {}
        for i, result in enumerate(results):
            if result:
                token_id = str(getattr(result, "token_id", token_ids[i]))
                books[token_id] = _parse_order_book(token_id, result)

        return books
    except Exception as e:
        logger.error(f"Error fetching order books: {e}")
        return {}


def get_spread(token_id: str) -> Optional[float]:
    """
    Get current bid-ask spread for a token.

    Args:
        token_id: The token ID

    Returns:
        Spread (ask - bid) or None if unavailable
    """
    book = get_order_book(token_id)
    if book:
        return book.spread
    return None


def get_spread_percentage(token_id: str) -> Optional[float]:
    """
    Get current bid-ask spread as percentage of midpoint.

    Args:
        token_id: The token ID

    Returns:
        Spread percentage or None if unavailable
    """
    book = get_order_book(token_id)
    if book and book.spread and book.midpoint and book.midpoint > 0:
        return (book.spread / book.midpoint) * 100
    return None


def _parse_order_book(token_id: str, data: Any) -> OrderBook:
    """Parse raw order book response into OrderBook object"""

    bids = []
    asks = []
    timestamp = None

    # Handle the official unified SDK OrderBook model.
    if hasattr(data, 'bids') and hasattr(data, 'asks'):
        # Parse bids (buy orders)
        for bid in data.bids:
            if hasattr(bid, 'price') and hasattr(bid, 'size'):
                bids.append(PriceLevel(
                    price=float(bid.price),
                    size=float(bid.size)
                ))

        # Parse asks (sell orders)
        for ask in data.asks:
            if hasattr(ask, 'price') and hasattr(ask, 'size'):
                asks.append(PriceLevel(
                    price=float(ask.price),
                    size=float(ask.size)
                ))

        timestamp_value = getattr(data, 'timestamp', None)
        isoformat = getattr(timestamp_value, "isoformat", None)
        timestamp = isoformat() if callable(isoformat) else timestamp_value

    # Handle dictionary format (for compatibility)
    elif isinstance(data, dict):
        raw_bids = data.get("bids", [])
        for bid in raw_bids:
            if isinstance(bid, dict):
                bids.append(PriceLevel(
                    price=float(bid.get("price", 0)),
                    size=float(bid.get("size", 0))
                ))
            elif isinstance(bid, (list, tuple)) and len(bid) >= 2:
                bids.append(PriceLevel(price=float(bid[0]), size=float(bid[1])))

        raw_asks = data.get("asks", [])
        for ask in raw_asks:
            if isinstance(ask, dict):
                asks.append(PriceLevel(
                    price=float(ask.get("price", 0)),
                    size=float(ask.get("size", 0))
                ))
            elif isinstance(ask, (list, tuple)) and len(ask) >= 2:
                asks.append(PriceLevel(price=float(ask[0]), size=float(ask[1])))

        timestamp = data.get("timestamp")

    # Sort: bids descending (highest first), asks ascending (lowest first)
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)

    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        timestamp=timestamp
    )
