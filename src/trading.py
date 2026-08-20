"""
Order placement and management.

Handles both DRY_RUN (simulated) and LIVE (real) modes.
"""

import time
from datetime import datetime, timezone
from typing import Optional
from decimal import Decimal, ROUND_DOWN

from src.auth import (
    PolymarketAdapterError,
    require_sdk_field,
    require_sdk_mapping,
)
from src.config import (
    DRY_RUN,
    MAX_POSITION_PER_MARKET,
    MAX_ORDER_SIZE,
    MIN_ORDER_SIZE,
    has_credentials
)
from src.models import Order, OrderSide, OrderStatus
from src.simulator import get_simulator
from src.utils import setup_logging

logger = setup_logging()


class OrderError(Exception):
    """Order placement error."""
    pass


class OrderCancellationError(OrderError):
    """Raised when live order cancellation cannot be verified cleanly."""


# Balance check state (cached to reduce API calls)
_last_balance_check: float = 0
_cached_balance: Optional[Decimal] = None
BALANCE_CACHE_SECONDS = 30
MIN_BALANCE_FOR_ORDER = Decimal("1.0")  # Don't trade below $1
_tick_size_cache: dict[str, str] = {}
_neg_risk_cache: dict[str, bool] = {}
_CANCEL_VERIFY_RETRY_SECONDS = 0.05
_MAX_BULK_CANCEL_ATTEMPTS = 2


def check_balance_for_order(price: Decimal, size: Decimal) -> None:
    """
    Ensure sufficient balance for order.

    Raises OrderError if:
    - Balance is below minimum threshold
    - Order cost exceeds 50% of available balance

    Uses cached balance to reduce API calls.
    """
    global _last_balance_check, _cached_balance

    if DRY_RUN:
        return  # Skip balance checks in simulation

    now = time.time()
    if now - _last_balance_check > BALANCE_CACHE_SECONDS or _cached_balance is None:
        try:
            from src.auth import get_balances
            balances = get_balances()
            _cached_balance = balances.get('usdc_allowance', Decimal('0'))
            _last_balance_check = now
            logger.debug(f"[BALANCE] Updated cache: ${_cached_balance:.2f}")
        except Exception as e:
            logger.error(f"Balance check failed closed: {e}")
            raise OrderError("Unable to verify pUSD balance for live order") from e

    order_cost = price * size
    if _cached_balance < MIN_BALANCE_FOR_ORDER:
        raise OrderError(f"Balance too low: ${_cached_balance:.2f} < ${MIN_BALANCE_FOR_ORDER}")

    if order_cost > _cached_balance * Decimal("0.5"):
        raise OrderError(
            f"Order cost ${order_cost:.2f} exceeds 50% of balance ${_cached_balance:.2f}"
        )


def check_sell_inventory(token_id: str, size: Decimal) -> None:
    """Ensure we actually hold enough outcome tokens to place a SELL order."""
    if DRY_RUN:
        return

    from src.auth import get_conditional_balance

    token_state = get_conditional_balance(token_id)
    sellable = token_state["sellable"]
    if sellable < size:
        raise OrderError(
            f"Insufficient outcome token balance/allowance for SELL: "
            f"need {size}, sellable {sellable}"
        )


def get_tick_size(token_id: str) -> Decimal:
    """
    Get tick size for a token.

    In live mode require the exchange-provided tick size.
    """
    if token_id in _tick_size_cache:
        return Decimal(_tick_size_cache[token_id])

    if DRY_RUN or not has_credentials():
        return Decimal("0.01")

    try:
        from src.client import get_auth_client

        tick_size = str(get_auth_client().get_order_book(token_id=token_id).tick_size)
        _tick_size_cache[token_id] = tick_size
        return Decimal(tick_size)
    except Exception as e:
        raise OrderError(
            f"Unable to verify exchange tick size for {token_id[:16]}..."
        ) from e


def get_order_creation_options(token_id: str) -> dict[str, object]:
    """Fetch per-market order creation options expected by the SDK."""
    tick_size = str(get_tick_size(token_id))

    if token_id not in _neg_risk_cache and not DRY_RUN and has_credentials():
        try:
            from src.client import get_auth_client

            _neg_risk_cache[token_id] = bool(
                get_auth_client().get_order_book(token_id=token_id).neg_risk
            )
        except Exception as e:
            logger.warning(f"Falling back to neg_risk=False for {token_id[:16]}...: {e}")
            _neg_risk_cache[token_id] = False

    return {
        "tick_size": tick_size,
        "neg_risk": _neg_risk_cache.get(token_id, False),
    }


def round_to_tick(
    price: Decimal,
    tick_size: Decimal,
    *,
    rounding: str = ROUND_DOWN,
) -> Decimal:
    """Round a price to an exchange tick with an explicit direction."""
    return (price / tick_size).quantize(Decimal("1"), rounding=rounding) * tick_size


def validate_price(price: Decimal, token_id: str) -> Decimal:
    """
    Validate and round price to tick size.

    Returns rounded price or raises OrderError.
    """
    if price <= Decimal("0") or price >= Decimal("1"):
        raise OrderError(f"Price must be between 0 and 1, got {price}")

    tick = get_tick_size(token_id)
    rounded = round_to_tick(price, tick)

    # Ensure still in valid range after rounding
    if rounded <= Decimal("0"):
        rounded = tick
    if rounded >= Decimal("1"):
        rounded = Decimal("1") - tick

    return rounded


def validate_size(size: Decimal) -> None:
    """Validate order size. Raises OrderError if invalid."""
    if size < MIN_ORDER_SIZE:
        raise OrderError(f"Size {size} below minimum {MIN_ORDER_SIZE}")

    if size > MAX_ORDER_SIZE:
        raise OrderError(f"Size {size} exceeds maximum {MAX_ORDER_SIZE}")


def check_position_limit(token_id: str, side: OrderSide, size: Decimal) -> None:
    """Check if order would exceed position limit. Raises OrderError if exceeded."""
    from src.orders import get_position

    current = get_position(token_id)

    if side == OrderSide.BUY:
        new_position = current + size
    else:
        new_position = current - size

    if abs(new_position) > MAX_POSITION_PER_MARKET:
        raise OrderError(
            f"Would exceed position limit. "
            f"Current: {current}, After: {new_position}, Limit: ±{MAX_POSITION_PER_MARKET}"
        )


def place_order(
    token_id: str,
    side: OrderSide,
    price: Decimal,
    size: Decimal
) -> Order:
    """
    Place an order.

    In DRY_RUN mode: Creates simulated order
    In LIVE mode: Places real order on exchange

    Args:
        token_id: The token to trade
        side: BUY or SELL
        price: Limit price (0 < price < 1)
        size: Order size in contracts

    Returns:
        Order object

    Raises:
        OrderError: If validation fails or order rejected
    """
    # Pure local validation still runs first so callers receive the normal
    # domain errors without touching credentials or the network.
    if price <= Decimal("0") or price >= Decimal("1"):
        raise OrderError(f"Price must be between 0 and 1, got {price}")
    validate_size(size)

    if not DRY_RUN:
        # The runtime now uses the official unified SDK, but ordinary strategy
        # callers do not carry the separately authorized eight-check audit.
        # Keep them fail-closed before credentials, balances, or signing are
        # touched. Cancellation remains available for recovery.
        from src.edge_lab.compatibility import assert_new_orders_disabled

        assert_new_orders_disabled()

    # Validate
    price = validate_price(price, token_id)
    check_position_limit(token_id, side, size)
    if side == OrderSide.BUY:
        check_balance_for_order(price, size)  # SAFETY: Verify collateral balance
    else:
        check_sell_inventory(token_id, size)

    if DRY_RUN:
        return get_simulator().create_order(token_id, side, price, size)

    # === LIVE MODE ===
    from src.rate_limiter import get_order_limiter
    get_order_limiter().wait_sync()

    if not has_credentials():
        raise OrderError("No credentials configured for live trading")

    from src.client import get_auth_client
    client = get_auth_client()

    try:
        logger.info(f"[LIVE] Placing: {side.value} {size} @ {price}")

        response = client.place_limit_order(
            token_id=token_id,
            price=price,
            size=size,
            side=side.value,
            post_only=True,
        )
        if not response.ok:
            raise OrderError(f"Order rejected ({response.code}): {response.message}")
        order_id = str(response.order_id)

        logger.info(f"[LIVE] Order placed: {order_id}")

        return Order(
            id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            filled=Decimal("0"),
            status=OrderStatus.LIVE,
            is_simulated=False,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    except OrderError:
        raise
    except Exception as e:
        raise OrderError(f"Order failed: {e}")


def cancel_order(order_id: str) -> bool:
    """
    Cancel an order by ID.

    Returns True if cancelled, False otherwise.
    """
    if DRY_RUN:
        return get_simulator().cancel_order(order_id)

    from src.rate_limiter import get_order_limiter
    get_order_limiter().wait_sync()

    if not has_credentials():
        logger.warning("No credentials for live trading")
        return False

    from src.client import get_auth_client

    try:
        logger.info(f"[LIVE] Cancelling: {order_id}")
        response = get_auth_client().cancel_order(order_id=order_id)
        canceled = {str(item) for item in require_sdk_field(response, "canceled")}
        return order_id in canceled
    except PolymarketAdapterError:
        raise
    except Exception as e:
        logger.error(f"Cancel failed for {order_id}: {e}")
        return False


def _live_open_order_ids(token_id: Optional[str]) -> set[str]:
    from src.orders import get_open_orders

    return {
        order.id
        for order in get_open_orders(token_id, raise_on_error=True)
        if order.is_live
    }


def _parse_cancel_response(response: object) -> tuple[set[str], dict[str, str]]:
    canceled = {str(item) for item in require_sdk_field(response, "canceled")}
    not_canceled = {
        str(order_id): str(reason)
        for order_id, reason in require_sdk_mapping(response, "not_canceled").items()
    }
    return canceled, not_canceled


def cancel_all_orders(
    token_id: Optional[str] = None,
    *,
    verify: bool = False,
    raise_on_failure: bool = False,
) -> int:
    """
    Cancel all open orders, optionally filtered by token.

    Returns count of cancelled orders.
    """
    if DRY_RUN:
        return get_simulator().cancel_all(token_id)

    if not has_credentials():
        if verify or raise_on_failure:
            raise OrderCancellationError(
                "Unable to verify live order cancellation without credentials"
            )
        return 0

    from src.client import get_auth_client
    client = get_auth_client()

    # Listing orders is useful for counting and targeted retries, but it must
    # never gate the emergency bulk-cancel request.  A disconnect can take the
    # read path down before the write path, and an eventually-consistent read
    # can briefly report an empty book while orders are still live.
    initial_list_error: Exception | None = None
    try:
        target_ids: set[str] | None = _live_open_order_ids(token_id)
    except Exception as error:
        target_ids = None
        initial_list_error = error
        logger.error(
            "Unable to list live orders before cancel; sending blind bulk cancel: %s",
            error,
        )

    remaining_ids = set(target_ids or ())
    canceled_ids: set[str] = set()
    reported_canceled_ids: set[str] = set()
    failure_reasons: dict[str, str] = {}
    verification_error: Exception | None = initial_list_error
    bulk_succeeded = False

    for attempt in range(_MAX_BULK_CANCEL_ATTEMPTS):
        try:
            response = (
                client.cancel_all()
                if token_id is None
                else client.cancel_market_orders(token_id=token_id)
            )
            bulk_succeeded = True
            batch_canceled, batch_failures = _parse_cancel_response(response)
            reported_canceled_ids |= batch_canceled
            failure_reasons.update(batch_failures)
        except Exception as error:
            logger.error(f"Cancel-all failed on attempt {attempt + 1}: {error}")

        try:
            current_ids = _live_open_order_ids(token_id)
            verification_error = None
            if target_ids is not None:
                canceled_ids |= target_ids - current_ids
            canceled_ids |= reported_canceled_ids
            # Reconcile against everything currently visible in scope, not
            # only the preflight snapshot; this also closes placement races.
            remaining_ids = current_ids
        except Exception as error:
            verification_error = error
            logger.error(
                "Unable to verify cancel-all attempt %s: %s",
                attempt + 1,
                error,
            )
            if target_ids is not None:
                remaining_ids = target_ids - reported_canceled_ids

        if verification_error is None and not remaining_ids and bulk_succeeded:
            break
        if attempt + 1 < _MAX_BULK_CANCEL_ATTEMPTS:
            time.sleep(_CANCEL_VERIFY_RETRY_SECONDS)

    if verification_error is None and remaining_ids:
        for order_id in tuple(remaining_ids):
            if cancel_order(order_id):
                reported_canceled_ids.add(order_id)

        try:
            current_ids = _live_open_order_ids(token_id)
            if target_ids is not None:
                canceled_ids |= target_ids - current_ids
            canceled_ids |= reported_canceled_ids
            remaining_ids = current_ids
        except Exception as error:
            verification_error = error
            logger.error(f"Unable to verify individual cancel retries: {error}")

    canceled_ids |= reported_canceled_ids

    uncertainty: str | None = None
    if not bulk_succeeded:
        uncertainty = "no bulk cancellation request completed successfully"
    elif verification_error is not None:
        uncertainty = f"post-cancel order listing failed: {verification_error}"

    if remaining_ids:
        failure_summary = ", ".join(
            f"{order_id}={failure_reasons.get(order_id, 'still-open-after-verify')}"
            for order_id in sorted(remaining_ids)
        )
        preflight_count = (
            str(len(target_ids)) if target_ids is not None else "unknown"
        )
        message = (
            f"Unable to verify live order cancellation; {len(remaining_ids)} "
            f"order(s) remain open (preflight count: {preflight_count}): "
            f"{failure_summary}"
        )
        logger.error(message)
        if verify or raise_on_failure:
            raise OrderCancellationError(message)
    elif uncertainty is not None:
        message = f"Unable to verify live order cancellation: {uncertainty}"
        logger.error(message)
        if verify or raise_on_failure:
            raise OrderCancellationError(message)
    elif canceled_ids:
        logger.info(f"[LIVE] Cancelled {len(canceled_ids)} orders")

    return len(canceled_ids)
