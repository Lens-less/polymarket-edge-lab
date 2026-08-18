from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.feed import trades_poller


class _Page:
    def __init__(self, items) -> None:
        self.items = tuple(items)


class _Paginator:
    def __init__(self, items) -> None:
        self._items = items

    def first_page(self) -> _Page:
        return _Page(self._items)


@pytest.mark.asyncio
async def test_poller_uses_public_taker_tape_and_filters_the_requested_token(
    monkeypatch,
) -> None:
    calls: list[tuple[str, object]] = []
    observed: list[tuple[Decimal, Decimal, str, bool]] = []
    timestamp = datetime(2026, 8, 18, tzinfo=timezone.utc)
    expected = SimpleNamespace(
        transaction_hash="0x1",
        token_id="token-a",
        side="BUY",
        price=Decimal("0.42"),
        size=Decimal("7"),
        timestamp=timestamp,
    )
    other_token = SimpleNamespace(
        transaction_hash="0x2",
        token_id="token-b",
        side="SELL",
        price=Decimal("0.58"),
        size=Decimal("4"),
        timestamp=timestamp,
    )

    class Client:
        @staticmethod
        def get_order_book(*, token_id: str):
            calls.append(("book", token_id))
            return SimpleNamespace(condition_id="condition-a")

        @staticmethod
        def list_trades(**kwargs):
            calls.append(("trades", kwargs))
            return _Paginator((expected, other_token))

    monkeypatch.setattr(trades_poller, "get_client", lambda: Client())
    poller = trades_poller.TradesPoller()
    poller.register_callback("token-a", lambda *args: observed.append(args))

    await poller._poll_token("token-a")

    assert calls == [
        ("book", "token-a"),
        (
            "trades",
            {
                "market": ["condition-a"],
                "taker_only": True,
                "page_size": 100,
            },
        ),
    ]
    assert observed == [(Decimal("0.42"), Decimal("7"), "BUY", True)]

    await poller._poll_token("token-a")
    assert [name for name, _value in calls].count("book") == 1
    assert observed == [(Decimal("0.42"), Decimal("7"), "BUY", True)]
