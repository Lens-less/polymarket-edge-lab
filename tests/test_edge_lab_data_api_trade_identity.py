from __future__ import annotations

from src.edge_lab.data_api_trade_identity import (
    data_api_trade_event_key,
    data_api_trade_observation_id,
)


def test_data_api_trade_observation_id_preserves_multiplicity() -> None:
    trade = {
        "conditionId": "0x" + "ab" * 32,
        "asset": "123",
        "side": "SELL",
        "size": "10",
        "price": "0.40",
        "timestamp": 1_784_974_804,
        "transactionHash": "0x" + "cd" * 32,
        "proxyWallet": "0x" + "11" * 20,
        "outcomeIndex": 0,
    }

    fingerprint = data_api_trade_event_key(trade)
    first = data_api_trade_observation_id(
        trade,
        snapshot_id="snapshot-a",
        page_number=1,
        row_number=1,
    )
    repeated_same_row_content = data_api_trade_observation_id(
        trade,
        snapshot_id="snapshot-a",
        page_number=1,
        row_number=2,
    )
    repeated_next_snapshot = data_api_trade_observation_id(
        trade,
        snapshot_id="snapshot-b",
        page_number=1,
        row_number=1,
    )

    assert fingerprint == data_api_trade_event_key({**trade, "size": "10.0"})
    assert first != repeated_same_row_content
    assert first != repeated_next_snapshot
    assert repeated_same_row_content != repeated_next_snapshot
