from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from src import auth, orders, pricing, trading
from src.models import OrderSide


class _Paginator:
    def __init__(self, items) -> None:
        self._items = tuple(items)

    def iter_items(self):
        yield from self._items


def test_auth_normalizes_official_base_units_and_approvals(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Approval:
        @staticmethod
        def wait() -> None:
            calls.append(("wait", None))

    class Client:
        wallet = "0xwallet"

        @staticmethod
        def get_balance_allowance(**kwargs):
            calls.append(("balance", kwargs))
            return SimpleNamespace(
                balance=12_500_000,
                allowances={"exchange": 10_000_000},
            )

        @staticmethod
        def setup_trading_approvals():
            calls.append(("approve", None))
            return Approval()

    monkeypatch.setattr(auth, "get_auth_client", lambda: Client())

    assert auth.get_wallet_address() == "0xwallet"
    assert auth.get_balances()["pusd_balance"] == Decimal("12.5")
    assert auth.check_allowances()["allowance_amount"] == Decimal("10")
    assert auth.set_allowances() is True
    assert ("wait", None) in calls


def test_order_queries_map_official_models_and_paginators(monkeypatch) -> None:
    order_item = SimpleNamespace(
        id="order-1",
        token_id="token-1",
        side="BUY",
        price=Decimal("0.4"),
        original_size=Decimal("8"),
        size_matched=Decimal("3"),
        status="LIVE",
    )
    trade_item = SimpleNamespace(
        id="trade-1",
        taker_order_id="order-1",
        token_id="token-1",
        side="BUY",
        price=Decimal("0.4"),
        size=Decimal("3"),
    )

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            return _Paginator((order_item,))

        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator((trade_item,))

    monkeypatch.setattr(orders, "DRY_RUN", False)
    monkeypatch.setattr(orders, "has_credentials", lambda: True)
    monkeypatch.setattr("src.client.get_auth_client", lambda: Client())

    open_orders = orders.get_open_orders("token-1", raise_on_error=True)
    trades = orders.get_trades("token-1", raise_on_error=True)

    assert open_orders[0].filled == Decimal("3")
    assert trades[0].side is OrderSide.BUY


def test_pricing_maps_batch_books_by_returned_token_id(monkeypatch) -> None:
    def book(token_id: str):
        return SimpleNamespace(
            token_id=token_id,
            bids=(SimpleNamespace(price=Decimal("0.4"), size=Decimal("2")),),
            asks=(SimpleNamespace(price=Decimal("0.6"), size=Decimal("2")),),
            timestamp=None,
        )

    class Client:
        @staticmethod
        def get_order_books(**kwargs):
            return (book("token-b"), book("token-a"))

    monkeypatch.setattr(pricing, "get_client", lambda: Client())

    result = pricing.get_order_books(["token-a", "token-b"])

    assert set(result) == {"token-a", "token-b"}
    assert result["token-a"].best_bid == 0.4


def test_live_cancel_uses_authoritative_sdk_response(monkeypatch) -> None:
    class Client:
        @staticmethod
        def cancel_order(**kwargs):
            return SimpleNamespace(canceled=("other-order",))

        @staticmethod
        def cancel_market_orders(**kwargs):
            return SimpleNamespace(canceled=("a", "b"))

        @staticmethod
        def cancel_all():
            return SimpleNamespace(canceled=("a", "b", "c"))

    monkeypatch.setattr(trading, "DRY_RUN", False)
    monkeypatch.setattr(trading, "has_credentials", lambda: True)
    monkeypatch.setattr("src.client.get_auth_client", lambda: Client())
    monkeypatch.setattr("src.rate_limiter.RateLimiter.wait_sync", lambda self: None)

    assert trading.cancel_order("order-1") is False
    assert trading.cancel_all_orders("token-1") == 2
    assert trading.cancel_all_orders() == 3
