from __future__ import annotations

from decimal import Decimal
from enum import Enum
from importlib.metadata import version as installed_version
from types import SimpleNamespace

import pytest
from polymarket.models.clob.account import BalanceAllowance, ClobTrade, OpenOrder
from polymarket.models.clob.cancel import CancelOrdersResponse

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
            return BalanceAllowance.model_validate(
                {
                    "balance": "12500000",
                    "allowances": {"exchange": "10000000"},
                }
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


def test_auth_adapter_raises_clear_error_when_balance_allowance_shape_is_incomplete(
    monkeypatch,
) -> None:
    class Client:
        @staticmethod
        def get_balance_allowance(**kwargs):
            return SimpleNamespace(balance=12_500_000)

    monkeypatch.setattr(auth, "get_auth_client", lambda: Client())

    with pytest.raises(auth.PolymarketAdapterError, match="allowances"):
        auth.get_balances()


def test_order_queries_map_official_models_and_paginators(monkeypatch) -> None:
    order_item = OpenOrder.model_validate(
        {
            "id": "order-1",
            "market": "0x" + "1" * 64,
            "asset_id": "token-1",
            "owner": "0xowner",
            "maker_address": "0xmaker",
            "side": "BUY",
            "price": "0.4",
            "original_size": "8",
            "size_matched": "3",
            "outcome": "YES",
            "order_type": "GTC",
            "status": "LIVE",
            "associate_trades": [],
            "created_at": "2026-08-19T00:00:00Z",
            "expiration": None,
        }
    )
    trade_item = ClobTrade.model_validate(
        {
            "id": "trade-1",
            "market": "0x" + "1" * 64,
            "asset_id": "token-1",
            "owner": "0xowner",
            "maker_address": "0xmaker",
            "taker_order_id": "order-1",
            "side": "BUY",
            "trader_side": "TAKER",
            "price": "0.4",
            "size": "3",
            "outcome": "YES",
            "status": "CONFIRMED",
            "fee_rate_bps": "0",
            "bucket_index": 0,
            "transaction_hash": "0x" + "2" * 64,
            "maker_orders": [],
            "match_time": "2026-08-19T00:00:00Z",
            "last_update": "2026-08-19T00:00:00Z",
        }
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


def test_order_queries_normalize_enum_like_status_values(monkeypatch) -> None:
    class Status(Enum):
        LIVE = "LIVE"

    order_item = SimpleNamespace(
        id="order-1",
        token_id="token-1",
        side="BUY",
        price=Decimal("0.4"),
        original_size=Decimal("8"),
        size_matched=Decimal("3"),
        status=Status.LIVE,
    )

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            return _Paginator((order_item,))

    monkeypatch.setattr(orders, "DRY_RUN", False)
    monkeypatch.setattr(orders, "has_credentials", lambda: True)
    monkeypatch.setattr("src.client.get_auth_client", lambda: Client())

    open_orders = orders.get_open_orders("token-1", raise_on_error=True)

    assert [order.id for order in open_orders] == ["order-1"]


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
    def make_open_order(order_id: str, token_id: str, side: str, price: str) -> OpenOrder:
        return OpenOrder.model_validate(
            {
                "id": order_id,
                "market": "0x" + order_id[0] * 64,
                "asset_id": token_id,
                "owner": "0xowner",
                "maker_address": "0xmaker",
                "side": side,
                "price": price,
                "original_size": "8",
                "size_matched": "0",
                "outcome": "YES",
                "order_type": "GTC",
                "status": "LIVE",
                "associate_trades": [],
                "created_at": "2026-08-19T00:00:00Z",
                "expiration": None,
            }
        )

    remaining_ids = {"a", "b", "c"}
    open_orders_by_id = {
        "a": make_open_order("a", "token-1", "BUY", "0.4"),
        "b": make_open_order("b", "token-1", "SELL", "0.6"),
        "c": make_open_order("c", "token-2", "BUY", "0.5"),
    }

    class Client:
        @staticmethod
        def cancel_order(**kwargs):
            return CancelOrdersResponse.model_validate(
                {
                    "canceled": ["other-order"],
                    "not_canceled": {"order-1": "already-filled"},
                }
            )

        @staticmethod
        def cancel_market_orders(**kwargs):
            remaining_ids.difference_update({"a", "b"})
            return CancelOrdersResponse.model_validate(
                {
                    "canceled": ["a", "b"],
                    "not_canceled": {},
                }
            )

        @staticmethod
        def cancel_all():
            remaining_ids.clear()
            return CancelOrdersResponse.model_validate(
                {
                    "canceled": ["a", "b", "c"],
                    "not_canceled": {},
                }
            )

        @staticmethod
        def list_open_orders(**kwargs):
            token_id = kwargs.get("token_id")
            open_orders = tuple(
                order
                for order_id, order in open_orders_by_id.items()
                if order_id in remaining_ids
            )
            if token_id is None:
                return _Paginator(open_orders)
            return _Paginator(tuple(item for item in open_orders if item.token_id == token_id))

    monkeypatch.setattr(trading, "DRY_RUN", False)
    monkeypatch.setattr(trading, "has_credentials", lambda: True)
    monkeypatch.setattr(orders, "DRY_RUN", False)
    monkeypatch.setattr(orders, "has_credentials", lambda: True)
    monkeypatch.setattr("src.client.get_auth_client", lambda: Client())
    monkeypatch.setattr("src.rate_limiter.RateLimiter.wait_sync", lambda self: None)

    assert trading.cancel_order("order-1") is False
    assert trading.cancel_all_orders("token-1") == 2
    remaining_ids.update({"a", "b", "c"})
    assert trading.cancel_all_orders() == 3


def test_live_cancel_strict_mode_rejects_missing_credentials(monkeypatch) -> None:
    monkeypatch.setattr(trading, "DRY_RUN", False)
    monkeypatch.setattr(trading, "has_credentials", lambda: False)

    with pytest.raises(trading.OrderCancellationError, match="credentials"):
        trading.cancel_all_orders("token-1", verify=True, raise_on_failure=True)

    assert trading.cancel_all_orders("token-1") == 0


def test_runtime_contract_uses_real_polymarket_package() -> None:
    assert installed_version("polymarket-client") == "0.6.0"
