"""Explicit live-network canary for the current low-level WebSocket client."""

import asyncio

import pytest


# Long-dated public market token, fixed deliberately so this test never falls
# back to REST market discovery. The canary is opt-in and may need maintenance
# if Polymarket retires the market before its advertised 2029 end date.
CANARY_TOKEN_ID = (
    "99020283867209289889119738709824148614345922887757122199903300146152690995270"
)


@pytest.mark.network
@pytest.mark.asyncio
async def test_live_websocket_connection_receives_market_data():
    """Connect, subscribe, and receive data through WebSocketConnection only."""
    from src.feed.websocket_conn import WebSocketConnection

    connection = WebSocketConnection()
    message_received = asyncio.Event()
    connection.on_message = lambda _message: message_received.set()

    try:
        connected = await asyncio.wait_for(connection.connect(), timeout=20)
        assert connected is True
        assert connection.is_connected is True

        subscribed = await asyncio.wait_for(
            connection.subscribe([CANARY_TOKEN_ID]),
            timeout=5,
        )
        assert subscribed is True
        await asyncio.wait_for(message_received.wait(), timeout=15)
    finally:
        await connection.disconnect()

    assert connection.is_connected is False
