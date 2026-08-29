from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import orders, trading
from src.models import OrderSide
from src.risk.manager import RiskManager


class _Paginator:
    def __init__(self, items) -> None:
        self._items = tuple(items)

    def iter_items(self):
        yield from self._items


def _configure_live_adapters(monkeypatch, client) -> None:
    monkeypatch.setattr(trading, "DRY_RUN", False)
    monkeypatch.setattr(trading, "has_credentials", lambda: True)
    monkeypatch.setattr(orders, "DRY_RUN", False)
    monkeypatch.setattr(orders, "has_credentials", lambda: True)
    monkeypatch.setattr("src.client.get_auth_client", lambda: client)
    monkeypatch.setattr("src.auth.get_auth_client", lambda: client)


def test_cancel_all_still_sends_bulk_cancel_when_open_order_listing_raises(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            raise ConnectionError("REST down")

        @staticmethod
        def cancel_market_orders(**kwargs):
            calls.append("cancel_market_orders")
            return SimpleNamespace(canceled=("orphan-order",), not_canceled={})

    _configure_live_adapters(monkeypatch, Client())

    assert trading.cancel_all_orders("token-1") == 1
    assert calls
    assert set(calls) == {"cancel_market_orders"}


def test_cancel_all_still_sends_bulk_cancel_when_open_order_listing_is_empty(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            return _Paginator(())

        @staticmethod
        def cancel_market_orders(**kwargs):
            calls.append("cancel_market_orders")
            return SimpleNamespace(canceled=("eventually-consistent-order",), not_canceled={})

    _configure_live_adapters(monkeypatch, Client())

    assert trading.cancel_all_orders("token-1") == 1
    assert calls == ["cancel_market_orders"]


def test_cancel_all_reports_typed_error_when_preflight_fails_but_verify_finds_order(
    monkeypatch,
) -> None:
    list_calls = 0

    class Client:
        @staticmethod
        def list_open_orders(**kwargs):
            nonlocal list_calls
            list_calls += 1
            if list_calls == 1:
                raise ConnectionError("preflight REST down")
            return _Paginator(
                (
                    SimpleNamespace(
                        id="still-live",
                        token_id="token-1",
                        side="BUY",
                        price=Decimal("0.40"),
                        original_size=Decimal(5),
                        size_matched=Decimal(0),
                        status="LIVE",
                    ),
                )
            )

        @staticmethod
        def cancel_market_orders(**kwargs):
            return SimpleNamespace(canceled=(), not_canceled={})

        @staticmethod
        def cancel_order(**kwargs):
            return SimpleNamespace(canceled=(), not_canceled={"still-live": "busy"})

    _configure_live_adapters(monkeypatch, Client())

    with pytest.raises(
        trading.OrderCancellationError,
        match="preflight count: unknown",
    ):
        trading.cancel_all_orders("token-1", verify=True)


def test_get_position_fails_closed_when_live_balance_and_trade_apis_fail(
    monkeypatch,
) -> None:
    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            raise ConnectionError("trade REST down")

        @staticmethod
        def get_balance_allowance(**kwargs):
            raise ConnectionError("balance REST down")

    _configure_live_adapters(monkeypatch, Client())

    with pytest.raises(ConnectionError, match="REST down"):
        orders.get_position("token-1")


def _sdk_trade(
    trade_id: str,
    *,
    status: str,
    trader_side: str = "TAKER",
    side: str = "BUY",
    fee_rate_bps: Decimal = Decimal(0),
    matched_at: datetime | None = None,
    maker_orders=(),
):
    return SimpleNamespace(
        id=trade_id,
        taker_order_id=f"taker-{trade_id}",
        token_id="token-1",
        side=side,
        trader_side=trader_side,
        price=Decimal("0.40"),
        size=Decimal(3),
        fee_rate_bps=fee_rate_bps,
        status=status,
        matched_at=matched_at or datetime(2026, 8, 20, tzinfo=UTC),
        maker_address="0xme",
        owner="owner-me",
        maker_orders=tuple(maker_orders),
    )


def test_get_trades_uses_owned_maker_order_and_filters_non_fills(monkeypatch) -> None:
    owned_maker_order = SimpleNamespace(
        order_id="maker-order-1",
        token_id="token-1",
        maker_address="0xme",
        owner="owner-me",
        side="BUY",
        price=Decimal("0.41"),
        matched_amount=Decimal(2),
        fee_rate_bps=None,
    )
    items = (
        _sdk_trade(
            "maker-trade",
            status="CONFIRMED",
            trader_side="MAKER",
            side="SELL",
            maker_orders=(owned_maker_order,),
        ),
        _sdk_trade("failed-trade", status="FAILED"),
        _sdk_trade("retrying-trade", status="RETRYING"),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=None, raise_on_error=True)

    assert len(result) == 1
    assert result[0].side is OrderSide.BUY
    assert result[0].order_id == "maker-order-1"
    assert result[0].price == Decimal("0.41")
    assert result[0].size == Decimal(2)
    assert result[0].fee == Decimal("0")


def test_get_trades_charges_taker_and_maker_fee_from_the_same_formula(
    monkeypatch,
) -> None:
    # Both legs report the same fee_rate_bps/price/size here, so they must
    # produce the same fee: the taker and maker paths share one fee formula
    # (src.orders._fee_from_rate_bps -> src.edge_lab.economics.taker_fee).
    maker_order = SimpleNamespace(
        order_id="maker-order-1",
        token_id="token-1",
        maker_address="0xme",
        owner="owner-me",
        side="BUY",
        price=Decimal("0.40"),
        matched_amount=Decimal(3),
        fee_rate_bps=Decimal(700),
    )
    items = (
        _sdk_trade(
            "taker-trade",
            status="CONFIRMED",
            trader_side="TAKER",
            fee_rate_bps=Decimal(700),
        ),
        _sdk_trade(
            "maker-trade",
            status="CONFIRMED",
            trader_side="MAKER",
            fee_rate_bps=Decimal(700),
            maker_orders=(maker_order,),
        ),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=None, raise_on_error=True)
    by_id = {trade.id: trade for trade in result}

    assert by_id["taker-trade"].fee == Decimal("0.05040")
    assert by_id["maker-trade:maker-order-1"].fee == Decimal("0.05040")


def test_get_trades_maker_fee_is_zero_when_fee_rate_bps_is_absent_or_zero(
    monkeypatch,
) -> None:
    zero_rate_maker_order = SimpleNamespace(
        order_id="maker-order-zero",
        token_id="token-1",
        maker_address="0xme",
        owner="owner-me",
        side="BUY",
        price=Decimal("0.40"),
        matched_amount=Decimal(3),
        fee_rate_bps=Decimal(0),
    )
    none_rate_maker_order = SimpleNamespace(
        order_id="maker-order-none",
        token_id="token-1",
        maker_address="0xme",
        owner="owner-me",
        side="BUY",
        price=Decimal("0.40"),
        matched_amount=Decimal(3),
        fee_rate_bps=None,
    )
    items = (
        _sdk_trade(
            "zero-rate-trade",
            status="CONFIRMED",
            trader_side="MAKER",
            maker_orders=(zero_rate_maker_order,),
        ),
        _sdk_trade(
            "none-rate-trade",
            status="CONFIRMED",
            trader_side="MAKER",
            maker_orders=(none_rate_maker_order,),
        ),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=None, raise_on_error=True)
    by_id = {trade.id: trade for trade in result}

    assert by_id["zero-rate-trade:maker-order-zero"].fee == Decimal("0")
    assert by_id["none-rate-trade:maker-order-none"].fee == Decimal("0")


def test_get_trades_taker_fee_is_driven_by_the_configured_exponent(
    monkeypatch,
) -> None:
    # LIVE_FEE_RATE_EXPONENT is an open question (docs/config.py comment); this
    # pins that src.orders actually reads it rather than a hardcoded exponent.
    monkeypatch.setattr(orders, "LIVE_FEE_RATE_EXPONENT", Decimal("2"))
    items = (
        _sdk_trade(
            "taker-trade",
            status="CONFIRMED",
            trader_side="TAKER",
            fee_rate_bps=Decimal(700),
        ),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=None, raise_on_error=True)

    # size=3, price=0.40, rate=0.07, probability_term=0.24
    # exponent=1 -> 0.05040 (see the sibling test); exponent=2 must differ.
    assert result[0].fee == Decimal("0.01210")


def test_get_trades_logs_maker_identity_payload_before_adapter_failure(
    monkeypatch,
) -> None:
    maker_orders = tuple(
        SimpleNamespace(
            order_id=f"maker-order-{index}",
            token_id="token-1",
            maker_address=f"0xother{index}",
            owner=f"owner-other-{index}",
            side="BUY",
            price=Decimal("0.40"),
            matched_amount=Decimal(index),
            fee_rate_bps=Decimal(0),
        )
        for index in (1, 2)
    )
    item = _sdk_trade(
        "ambiguous-maker-trade",
        status="CONFIRMED",
        trader_side="MAKER",
        maker_orders=maker_orders,
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator((item,))

    _configure_live_adapters(monkeypatch, Client())

    with (
        patch.object(orders.logger, "error") as error_log,
        pytest.raises(orders.PolymarketAdapterError, match="ambiguous-maker-trade"),
    ):
        orders.get_trades("token-1", limit=None, raise_on_error=True)

    logged = str(error_log.call_args)
    assert "0xme" in logged
    assert "owner-me" in logged
    assert "maker-order-1" in logged
    assert "0xother2" in logged
    assert "owner-other-2" in logged


def test_get_trades_sorts_before_applying_recent_trade_limit(monkeypatch) -> None:
    items = (
        _sdk_trade(
            "oldest",
            status="CONFIRMED",
            matched_at=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        ),
        _sdk_trade(
            "newest",
            status="CONFIRMED",
            matched_at=datetime(2026, 8, 20, 0, 2, tzinfo=UTC),
        ),
        _sdk_trade(
            "middle",
            status="CONFIRMED",
            matched_at=datetime(2026, 8, 20, 0, 1, tzinfo=UTC),
        ),
    )

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            return _Paginator(items)

    _configure_live_adapters(monkeypatch, Client())

    result = orders.get_trades("token-1", limit=2, raise_on_error=True)

    assert [trade.id for trade in result] == ["middle", "newest"]


def test_get_trades_forwards_incremental_after_watermark(monkeypatch) -> None:
    observed_kwargs: dict[str, object] = {}

    class Client:
        @staticmethod
        def list_account_trades(**kwargs):
            observed_kwargs.update(kwargs)
            return _Paginator(())

    _configure_live_adapters(monkeypatch, Client())

    assert orders.get_trades(
        "token-1",
        limit=None,
        raise_on_error=True,
        after="1787184000",
    ) == []
    assert observed_kwargs == {
        "token_id": "token-1",
        "after": "1787184000",
    }


def test_kill_switch_can_scope_cancellation_to_one_strategy_token() -> None:
    manager = RiskManager(enforce=True)

    with patch("src.risk.manager.cancel_all_orders", return_value=2) as cancel:
        manager.kill_switch("strategy stop", token_id="token-1")

    cancel.assert_called_once_with("token-1", verify=True, raise_on_failure=True)


def test_example_env_does_not_disable_live_risk_enforcement() -> None:
    active_lines = [
        line.strip()
        for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "RISK_ENFORCE=false" not in active_lines
