"""Strict target-catalog tests for short Chainlink crypto markets."""

from __future__ import annotations

from copy import deepcopy

import pytest

from src.edge_lab.data_store import canonical_record_id
from src.edge_lab.short_crypto_catalog import (
    CatalogRejection,
    parse_short_crypto_announcement,
)


def announcement_record(
    *,
    asset: str = "btc",
    horizon: str = "5m",
    opens_at_seconds: int = 1_784_979_900,
) -> dict[str, object]:
    display_asset = {"btc": "Bitcoin", "eth": "Ethereum"}[asset]
    source_asset = asset.upper()
    slug = f"{asset}-updown-{horizon}-{opens_at_seconds}"
    condition_id = "0x" + "ab" * 32
    tokens = ["1" * 76, "2" * 76]
    event = {
        "event_type": "new_market",
        "id": "3081000",
        "slug": slug,
        "condition_id": condition_id,
        "market": condition_id,
        "assets_ids": tokens,
        "clob_token_ids": tokens,
        "outcomes": ["Up", "Down"],
        "question": (
            f"{display_asset} Up or Down - July 25, 7:45AM-7:50AM ET"
        ),
        "description": (
            f'This market will resolve to "Up" if the {display_asset} price '
            "at the end of the time range specified in the title is greater "
            "than or equal to the price at the beginning of that range. "
            'Otherwise, it will resolve to "Down".\n'
            "The resolution source for this market is information from "
            f"Chainlink, specifically the {source_asset}/USD data stream "
            "available at "
            f"https://data.chain.link/streams/{asset}-usd."
        ),
        "event_message": {
            "id": "742901",
            "slug": slug,
            "ticker": slug,
            "title": (
                f"{display_asset} Up or Down - "
                "July 25, 7:45AM-7:50AM ET"
            ),
        },
        "timestamp": "1784889000000",
        "active": False,
    }
    recorder_payload = {
        "schema_version": "clob-market-ws.new_market.v1",
        "source": "clob_market_ws",
        "kind": "data",
        "event_type": "new_market",
        "session_id": "session-a",
        "connection_id": "connection-a",
        "received_at": "2026-07-24T13:20:00.000000Z",
        "event_at": None,
        "sequence": None,
        "payload": event,
    }
    record: dict[str, object] = {
        "schema_version": "edge-lab-recorder.raw.v1",
        "source": "clob_market_ws",
        "received_at": "2026-07-24T13:20:00.000000Z",
        "event_at": None,
        "sequence": None,
        "payload": recorder_payload,
    }
    record["record_id"] = canonical_record_id(record)
    return record


@pytest.mark.parametrize(
    ("asset", "horizon", "duration_ms", "source_symbol"),
    [
        ("btc", "5m", 300_000, "btc/usd"),
        ("eth", "15m", 900_000, "eth/usd"),
    ],
)
def test_parses_content_addressed_chainlink_target(
    asset: str,
    horizon: str,
    duration_ms: int,
    source_symbol: str,
) -> None:
    record = announcement_record(asset=asset, horizon=horizon)

    target = parse_short_crypto_announcement(record)

    assert target is not None
    assert target.slug.startswith(f"{asset}-updown-{horizon}-")
    assert target.market_id == "3081000"
    assert target.condition_id == "0x" + "ab" * 32
    assert target.up_token_id == "1" * 76
    assert target.down_token_id == "2" * 76
    assert target.source_topic == "crypto_prices_chainlink"
    assert target.source_symbol == source_symbol
    assert target.horizon == horizon
    assert target.opens_at_ms == 1_784_979_900_000
    assert target.closes_at_ms - target.opens_at_ms == duration_ms
    assert target.announcement_record_id == record["record_id"]
    assert len(target.rule_hash) == 64


def test_ignores_unrelated_public_market_announcements() -> None:
    unrelated = announcement_record()
    payload = unrelated["payload"]
    assert isinstance(payload, dict)
    event = payload["payload"]
    assert isinstance(event, dict)
    event["slug"] = "nba-lal-bos-2026-07-25"
    unrelated["record_id"] = canonical_record_id(unrelated)

    assert parse_short_crypto_announcement(unrelated) is None


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("record_id", "record_id_mismatch"),
        ("outcomes", "outcomes_mismatch"),
        ("duplicate_tokens", "token_mapping_invalid"),
        ("condition", "condition_mapping_mismatch"),
        ("event_slug", "event_slug_mismatch"),
        ("resolution_source", "resolution_source_mismatch"),
        ("outer_schema", "announcement_schema_mismatch"),
        ("inner_schema", "announcement_schema_mismatch"),
    ],
)
def test_matching_slug_fails_closed_on_identity_or_rule_conflict(
    mutation: str,
    code: str,
) -> None:
    record = announcement_record()
    mutated = deepcopy(record)
    payload = mutated["payload"]
    assert isinstance(payload, dict)
    event = payload["payload"]
    assert isinstance(event, dict)

    if mutation == "record_id":
        mutated["record_id"] = "0" * 64
    elif mutation == "outcomes":
        event["outcomes"] = ["Down", "Up"]
        mutated["record_id"] = canonical_record_id(mutated)
    elif mutation == "duplicate_tokens":
        event["assets_ids"] = ["1" * 76, "1" * 76]
        event["clob_token_ids"] = ["1" * 76, "1" * 76]
        mutated["record_id"] = canonical_record_id(mutated)
    elif mutation == "condition":
        event["market"] = "0x" + "cd" * 32
        mutated["record_id"] = canonical_record_id(mutated)
    elif mutation == "event_slug":
        event_message = event["event_message"]
        assert isinstance(event_message, dict)
        event_message["slug"] = "btc-updown-5m-999"
        mutated["record_id"] = canonical_record_id(mutated)
    elif mutation == "outer_schema":
        mutated["schema_version"] = "edge-lab-recorder.raw.v2"
        mutated["record_id"] = canonical_record_id(mutated)
    elif mutation == "inner_schema":
        payload["schema_version"] = "clob-market-ws.new_market.v2"
        mutated["record_id"] = canonical_record_id(mutated)
    else:
        event["description"] = str(event["description"]).replace(
            "data.chain.link",
            "example.invalid",
        )
        mutated["record_id"] = canonical_record_id(mutated)

    with pytest.raises(CatalogRejection) as caught:
        parse_short_crypto_announcement(mutated)
    assert caught.value.code == code
