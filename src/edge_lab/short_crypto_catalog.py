"""Strict target catalog for short-horizon Chainlink crypto markets.

The public market WebSocket announces many unrelated markets.  This module
turns only the exact BTC/ETH ``Up or Down`` 5m/15m family into immutable,
content-addressed targets.  Any announcement that matches the target slug but
conflicts with its identity, outcome ordering, token mapping, or resolution
source is rejected rather than guessed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .data_store import canonical_json_bytes, canonical_record_id


_TARGET_SLUG = re.compile(r"^(btc|eth)-updown-(5m|15m)-([0-9]{10})$")
_CONDITION_ID = re.compile(r"^0x[0-9a-fA-F]{64}$")
_RECORD_ID = re.compile(r"^[0-9a-f]{64}$")
_DURATION_MS = {"5m": 5 * 60 * 1_000, "15m": 15 * 60 * 1_000}


class CatalogRejection(ValueError):
    """A target-looking announcement failed a strict catalog invariant."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class ShortCryptoTarget:
    """Content-addressed capture target derived from one public announcement."""

    slug: str
    market_id: str
    condition_id: str
    up_token_id: str
    down_token_id: str
    source_topic: str
    source_symbol: str
    horizon: str
    opens_at_ms: int
    closes_at_ms: int
    announced_at: str
    announcement_record_id: str
    rule_hash: str


def _mapping(value: Any, *, code: str, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogRejection(code, f"{label} must be an object")
    return value


def _string(value: Any, *, code: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogRejection(code, f"{label} must be a non-empty string")
    return value.strip()


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def parse_short_crypto_announcement(
    record: Mapping[str, Any],
) -> ShortCryptoTarget | None:
    """Parse one raw market announcement into a strict capture target.

    Unrelated announcements return ``None``.  Once the slug identifies a
    supported target family, every identity and rule conflict raises
    :class:`CatalogRejection`; callers must quarantine that observation rather
    than silently accepting a possibly different market contract.
    """

    recorder_payload = record.get("payload")
    if not isinstance(recorder_payload, Mapping):
        return None
    event = recorder_payload.get("payload")
    if not isinstance(event, Mapping):
        return None

    slug_value = event.get("slug")
    if not isinstance(slug_value, str):
        return None
    slug_match = _TARGET_SLUG.fullmatch(slug_value)
    if slug_match is None:
        return None
    if (
        record.get("schema_version") != "edge-lab-recorder.raw.v1"
        or recorder_payload.get("schema_version")
        != "clob-market-ws.new_market.v1"
    ):
        raise CatalogRejection(
            "announcement_schema_mismatch",
            "target announcement schema is not in the exact allowlist",
        )

    asset, horizon, opens_at_text = slug_match.groups()
    record_id = record.get("record_id")
    if (
        not isinstance(record_id, str)
        or _RECORD_ID.fullmatch(record_id) is None
        or record_id != canonical_record_id(record)
    ):
        raise CatalogRejection(
            "record_id_mismatch",
            "announcement record_id does not match its canonical envelope",
        )

    if (
        record.get("source") != "clob_market_ws"
        or recorder_payload.get("source") != "clob_market_ws"
        or recorder_payload.get("event_type") != "new_market"
        or event.get("event_type") != "new_market"
    ):
        raise CatalogRejection(
            "announcement_envelope_mismatch",
            "target must be a clob_market_ws new_market observation",
        )

    outcomes = _string_list(event.get("outcomes"))
    if outcomes != ["Up", "Down"]:
        raise CatalogRejection(
            "outcomes_mismatch",
            "outcomes must be exactly ['Up', 'Down'] in token order",
        )

    asset_ids = _string_list(event.get("assets_ids"))
    clob_token_ids = _string_list(event.get("clob_token_ids"))
    valid_tokens = (
        asset_ids is not None
        and clob_token_ids is not None
        and asset_ids == clob_token_ids
        and len(asset_ids) == 2
        and len(set(asset_ids)) == 2
        and all(token.isdigit() for token in asset_ids)
    )
    if not valid_tokens:
        raise CatalogRejection(
            "token_mapping_invalid",
            "assets_ids and clob_token_ids must be the same two unique integers",
        )
    assert asset_ids is not None

    condition_id = _string(
        event.get("condition_id"),
        code="condition_mapping_mismatch",
        label="condition_id",
    )
    if (
        _CONDITION_ID.fullmatch(condition_id) is None
        or event.get("market") != condition_id
    ):
        raise CatalogRejection(
            "condition_mapping_mismatch",
            "market must equal a 32-byte condition_id",
        )

    event_message = _mapping(
        event.get("event_message"),
        code="event_slug_mismatch",
        label="event_message",
    )
    if event_message.get("slug") != slug_value:
        raise CatalogRejection(
            "event_slug_mismatch",
            "event_message.slug must equal the announced market slug",
        )

    description = _string(
        event.get("description"),
        code="resolution_source_mismatch",
        label="description",
    )
    expected_symbol = f"{asset.upper()}/USD"
    expected_url = f"https://data.chain.link/streams/{asset}-usd"
    if (
        "Chainlink" not in description
        or expected_symbol not in description
        or expected_url not in description
    ):
        raise CatalogRejection(
            "resolution_source_mismatch",
            f"description must bind {expected_symbol} to {expected_url}",
        )

    market_id = _string(
        event.get("id"),
        code="market_id_invalid",
        label="market id",
    )
    if not market_id.isdigit():
        raise CatalogRejection("market_id_invalid", "market id must be an integer")

    announced_at = _string(
        record.get("received_at"),
        code="announcement_time_invalid",
        label="received_at",
    )
    opens_at_ms = int(opens_at_text) * 1_000
    closes_at_ms = opens_at_ms + _DURATION_MS[horizon]
    source_symbol = f"{asset}/usd"

    rule_identity = {
        "catalog_schema": "edge-lab.short-crypto-target.v1",
        "slug": slug_value,
        "market_id": market_id,
        "condition_id": condition_id.lower(),
        "outcomes": outcomes,
        "token_ids": asset_ids,
        "source_topic": "crypto_prices_chainlink",
        "source_symbol": source_symbol,
        "source_url": expected_url,
        "horizon": horizon,
        "opens_at_ms": opens_at_ms,
        "closes_at_ms": closes_at_ms,
        "description": description,
    }
    rule_hash = hashlib.sha256(canonical_json_bytes(rule_identity)).hexdigest()

    return ShortCryptoTarget(
        slug=slug_value,
        market_id=market_id,
        condition_id=condition_id.lower(),
        up_token_id=asset_ids[0],
        down_token_id=asset_ids[1],
        source_topic="crypto_prices_chainlink",
        source_symbol=source_symbol,
        horizon=horizon,
        opens_at_ms=opens_at_ms,
        closes_at_ms=closes_at_ms,
        announced_at=announced_at,
        announcement_record_id=record_id,
        rule_hash=rule_hash,
    )
