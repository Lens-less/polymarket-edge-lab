"""
Order queries - unified interface for real and simulated orders.
"""

from typing import List, Optional
from decimal import Decimal

from src.auth import (
    PolymarketAdapterError,
    normalize_sdk_scalar,
    require_sdk_field,
    require_sdk_iter_items,
)
from src.config import DRY_RUN, LIVE_FEE_RATE_EXPONENT, has_credentials
from src.edge_lab.economics import taker_fee as _fee_formula
from src.edge_lab.models import FeeSchedule
from src.models import Order, Trade, OrderSide, OrderStatus
from src.simulator import get_simulator
from src.utils import setup_logging

logger = setup_logging()

_POSITION_BEARING_TRADE_STATUSES = frozenset(
    {
        "MATCHED",
        "MATCHED_NOT_BROADCASTED",
        "MINED",
        "CONFIRMED",
    }
)
_BASIS_POINTS = Decimal("10000")


def _timestamp_text(value: object) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _fee_from_rate_bps(
    fee_rate_bps: object, *, price: Decimal, size: Decimal
) -> Decimal:
    """Calculate a matched fee in USDC from a reported fee_rate_bps.

    Shared by both the taker leg and the maker leg of an account trade so the
    fee math is defined exactly once (see LIVE_FEE_RATE_EXPONENT for the open
    question about what fee_rate_bps actually means). A None/zero rate -- the
    documented case for maker fills today -- yields zero fee rather than
    raising, since the SDK models fee_rate_bps as optional on MakerOrder.
    """
    if fee_rate_bps is None:
        return Decimal("0")
    rate = Decimal(str(fee_rate_bps)) / _BASIS_POINTS
    if rate <= 0:
        return Decimal("0")
    schedule = FeeSchedule(rate=rate, exponent=LIVE_FEE_RATE_EXPONENT)
    return _fee_formula(size, price, schedule)


def _taker_fee(item: object, *, price: Decimal, size: Decimal) -> Decimal:
    """Calculate the matched taker fee in USDC from the reported fee rate."""
    return _fee_from_rate_bps(
        require_sdk_field(item, "fee_rate_bps"), price=price, size=size
    )


def _diagnostic_fields(subject: object, field_names: tuple[str, ...]) -> dict[str, str]:
    """Return a bounded, credential-free snapshot of SDK adapter fields."""
    return {
        field_name: str(getattr(subject, field_name, "<missing>"))
        for field_name in field_names
    }


def _owned_maker_orders(item: object) -> list[object]:
    """Select maker legs owned by the authenticated account trade record."""
    raw_maker_orders = require_sdk_field(item, "maker_orders")
    try:
        maker_orders = list(raw_maker_orders)
    except TypeError as error:
        raise PolymarketAdapterError("ClobTrade.maker_orders must be iterable") from error

    account_address = str(require_sdk_field(item, "maker_address")).casefold()
    account_owner = str(require_sdk_field(item, "owner"))
    owned = [
        maker_order
        for maker_order in maker_orders
        if str(require_sdk_field(maker_order, "maker_address")).casefold()
        == account_address
        or str(require_sdk_field(maker_order, "owner")) == account_owner
    ]
    if owned:
        return owned
    if len(maker_orders) == 1:
        # Some historical responses omit or rewrite the account identifiers,
        # but a single maker leg is still unambiguous.
        return maker_orders
    diagnostics = {
        "trade": _diagnostic_fields(
            item,
            (
                "id",
                "trader_side",
                "maker_address",
                "owner",
                "token_id",
                "side",
                "price",
                "size",
                "status",
                "fee_rate_bps",
            ),
        ),
        "maker_orders": [
            _diagnostic_fields(
                maker_order,
                (
                    "order_id",
                    "token_id",
                    "maker_address",
                    "owner",
                    "side",
                    "price",
                    "matched_amount",
                    "fee_rate_bps",
                ),
            )
            for maker_order in maker_orders
        ],
    }
    logger.error(
        "Unable to map account-owned maker legs; adapter payload=%s",
        diagnostics,
    )
    raise PolymarketAdapterError(
        "Unable to identify the authenticated account's maker order(s) "
        f"for trade {require_sdk_field(item, 'id')}"
    )


def _normalize_account_trade(item: object) -> list[Trade]:
    """Map one SDK account trade into one or more account-owned fills."""
    trade_id = str(require_sdk_field(item, "id"))
    trader_side = normalize_sdk_scalar(
        require_sdk_field(item, "trader_side"),
        field_name="trader_side",
    ).upper()
    timestamp = _timestamp_text(require_sdk_field(item, "matched_at"))

    if trader_side == "TAKER":
        price = Decimal(str(require_sdk_field(item, "price")))
        size = Decimal(str(require_sdk_field(item, "size")))
        return [
            Trade(
                id=trade_id,
                order_id=str(require_sdk_field(item, "taker_order_id")),
                token_id=str(require_sdk_field(item, "token_id")),
                side=OrderSide(
                    normalize_sdk_scalar(
                        require_sdk_field(item, "side"),
                        field_name="side",
                    ).upper()
                ),
                price=price,
                size=size,
                is_simulated=False,
                timestamp=timestamp,
                fee=_taker_fee(item, price=price, size=size),
            )
        ]

    if trader_side != "MAKER":
        raise PolymarketAdapterError(
            f"Unsupported ClobTrade.trader_side {trader_side!r} for trade {trade_id}"
        )

    fills: list[Trade] = []
    for maker_order in _owned_maker_orders(item):
        order_id = str(require_sdk_field(maker_order, "order_id"))
        price = Decimal(str(require_sdk_field(maker_order, "price")))
        size = Decimal(str(require_sdk_field(maker_order, "matched_amount")))
        fills.append(
            Trade(
                # One taker trade can fill multiple maker orders owned by the
                # same account.  Composite IDs keep the local dedupe exact.
                id=f"{trade_id}:{order_id}",
                order_id=order_id,
                token_id=str(require_sdk_field(maker_order, "token_id")),
                side=OrderSide(
                    normalize_sdk_scalar(
                        require_sdk_field(maker_order, "side"),
                        field_name="maker_order.side",
                    ).upper()
                ),
                price=price,
                size=size,
                is_simulated=False,
                timestamp=timestamp,
                fee=_fee_from_rate_bps(
                    require_sdk_field(maker_order, "fee_rate_bps"),
                    price=price,
                    size=size,
                ),
            )
        )
    return fills


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

        for item in require_sdk_iter_items(response):
            status = normalize_sdk_scalar(
                require_sdk_field(item, "status"),
                field_name="status",
            )
            if status.upper() != 'LIVE':
                continue
            order = Order(
                id=str(require_sdk_field(item, "id")),
                token_id=str(require_sdk_field(item, "token_id")),
                side=OrderSide(
                    normalize_sdk_scalar(
                        require_sdk_field(item, "side"),
                        field_name="side",
                    )
                ),
                price=Decimal(str(require_sdk_field(item, "price"))),
                size=Decimal(str(require_sdk_field(item, "original_size"))),
                filled=Decimal(str(require_sdk_field(item, "size_matched"))),
                status=OrderStatus.LIVE,
                is_simulated=False
            )
            if token_id is None or order.token_id == token_id:
                orders.append(order)
        return orders

    except PolymarketAdapterError:
        raise
    except Exception as e:
        if raise_on_error:
            raise
        logger.error(f"Error getting orders: {e}")
        return []


def get_trades(
    token_id: Optional[str] = None,
    limit: Optional[int] = 50,
    raise_on_error: bool = False,
    *,
    after: Optional[str] = None,
) -> List[Trade]:
    """Get recent trades."""
    if DRY_RUN:
        trades = get_simulator().get_trades(token_id)
        if limit is not None:
            trades = [] if limit <= 0 else trades[-limit:]
        return trades

    if not has_credentials():
        return []

    from src.client import get_auth_client

    try:
        filters: dict[str, str | None] = {"token_id": token_id}
        if after is not None:
            filters["after"] = after
        response = get_auth_client().list_account_trades(**filters)
        trades = []

        for item in require_sdk_iter_items(response):
            status = normalize_sdk_scalar(
                require_sdk_field(item, "status"),
                field_name="status",
            ).upper()
            if status not in _POSITION_BEARING_TRADE_STATUSES:
                continue
            for trade in _normalize_account_trade(item):
                if token_id is None or trade.token_id == token_id:
                    trades.append(trade)

        # The SDK paginator does not promise endpoint order.  Normalize to
        # chronological order before applying the caller's recent-item limit.
        trades.sort(key=lambda trade: (trade.timestamp or "", trade.id))
        if limit is not None:
            return [] if limit <= 0 else trades[-limit:]
        return trades

    except PolymarketAdapterError:
        raise
    except Exception as e:
        if raise_on_error:
            raise
        logger.error(f"Error getting trades: {e}")
        return []


def get_position(token_id: str) -> Decimal:
    """Get the authoritative current outcome-token balance."""
    if DRY_RUN:
        return get_simulator().get_position(token_id)

    # Trade history cannot account for transfers, splits, merges, redemptions,
    # or failed settlement.  The conditional-token balance is the live source
    # of truth and must raise rather than silently turning an API failure into
    # a zero position.
    from src.auth import get_conditional_balance

    state = get_conditional_balance(token_id, raise_on_error=True)
    return Decimal(str(state["balance"]))
