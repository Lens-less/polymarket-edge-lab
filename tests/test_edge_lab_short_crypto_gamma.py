"""Strict Gamma identity and rule reconciliation for short crypto targets."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from src.edge_lab.short_crypto_catalog import ShortCryptoTarget
from src.edge_lab.short_crypto_gamma import (
    GammaReconciliationError,
    is_strict_gamma_preactivation,
    reconcile_short_crypto_gamma,
)


DESCRIPTION = (
    'This market will resolve to "Up" if the Bitcoin price at the end of the '
    "time range specified in the title is greater than or equal to the price "
    "at the beginning of that range. Otherwise, it will resolve to "
    '"Down".\n'
    "The resolution source for this market is information from Chainlink, "
    "specifically the BTC/USD data stream available at "
    "https://data.chain.link/streams/btc-usd.\n"
    "Please note that this market is about the price according to Chainlink "
    "data stream BTC/USD, not according to other sources or spot markets."
)


def target() -> ShortCryptoTarget:
    return ShortCryptoTarget(
        slug="btc-updown-15m-1784986200",
        market_id="3081056",
        condition_id=(
            "0xa05b7e8e2011345e010e13794d95a3ad"
            "cc044b754e926ef217cc2038025611e4"
        ),
        up_token_id=(
            "6965839325933880049127536366351210143855636348162446026324014689"
            "8711641351040"
        ),
        down_token_id=(
            "4777337799278475686969132494177605544534258323741481924354989944"
            "0873079135632"
        ),
        source_topic="crypto_prices_chainlink",
        source_symbol="btc/usd",
        horizon="15m",
        opens_at_ms=1_784_986_200_000,
        closes_at_ms=1_784_987_100_000,
        announced_at="2026-07-24T13:37:34.673984Z",
        announcement_record_id=(
            "4fbe5d5fafa24569642a6d9a055faaa"
            "615a490ddbbae002b3b18f07437374ea7"
        ),
        rule_hash=(
            "1de5ab48f0b7cb06b6e1bb54e20f44f"
            "dd1776657392fc0a5d44c390c0f4a8216"
        ),
    )


def gamma_market() -> dict[str, object]:
    expected = target()
    return {
        "id": expected.market_id,
        "slug": expected.slug,
        "conditionId": expected.condition_id,
        "outcomes": '["Up", "Down"]',
        "clobTokenIds": (
            f'["{expected.up_token_id}", "{expected.down_token_id}"]'
        ),
        "description": DESCRIPTION,
        "resolutionSource": "https://data.chain.link/streams/btc-usd",
        "endDate": "2026-07-25T13:45:00Z",
        # startDate is Gamma publication metadata, not the market-window open.
        "startDate": "2026-07-24T13:37:00Z",
        "active": True,
        "closed": False,
        "acceptingOrders": True,
    }


def raw_market(market: dict[str, object] | None = None) -> bytes:
    return json.dumps(
        gamma_market() if market is None else market,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def test_reconciles_exact_gamma_identity_rules_status_and_raw_evidence() -> None:
    result = reconcile_short_crypto_gamma(target(), raw_market())

    assert result.slug == target().slug
    assert result.market_id == target().market_id
    assert result.condition_id == target().condition_id
    assert result.outcomes == ("Up", "Down")
    assert result.token_ids == (target().up_token_id, target().down_token_id)
    assert result.resolution_source == "https://data.chain.link/streams/btc-usd"
    assert result.end_date_ms == target().closes_at_ms
    assert result.status.active is True
    assert result.status.closed is False
    assert result.status.accepting_orders is True
    assert result.evidence.byte_length == len(raw_market())
    assert result.evidence.sha256 == (
        "df937101fd83abf18095f81c606db269"
        "496d6563269b08c49c0199750e2c0014"
    )


@pytest.mark.parametrize(
    ("field", "replacement", "code"),
    [
        ("id", "999", "gamma_market_id_conflict"),
        (
            "slug",
            "btc-updown-15m-1784987100",
            "gamma_slug_conflict",
        ),
        (
            "conditionId",
            "0x" + "cd" * 32,
            "gamma_condition_conflict",
        ),
        ("outcomes", '["Down", "Up"]', "gamma_outcomes_conflict"),
        (
            "clobTokenIds",
            f'["{target().down_token_id}", "{target().up_token_id}"]',
            "gamma_token_mapping_conflict",
        ),
    ],
)
def test_rejects_every_identity_or_outcome_token_mapping_conflict(
    field: str,
    replacement: str,
    code: str,
) -> None:
    market = deepcopy(gamma_market())
    market[field] = replacement

    with pytest.raises(GammaReconciliationError) as caught:
        reconcile_short_crypto_gamma(target(), raw_market(market))

    assert caught.value.code == code


@pytest.mark.parametrize(
    ("field", "replacement", "code"),
    [
        (
            "resolutionSource",
            "https://example.invalid/btc-usd",
            "gamma_resolution_source_conflict",
        ),
        (
            "endDate",
            "2026-07-25T13:45:01Z",
            "gamma_end_date_conflict",
        ),
        (
            "description",
            f"{DESCRIPTION}\nAn unannounced fallback source may also be used.",
            "gamma_rule_hash_conflict",
        ),
    ],
)
def test_rejects_resolution_window_or_rule_conflict(
    field: str,
    replacement: str,
    code: str,
) -> None:
    market = deepcopy(gamma_market())
    market[field] = replacement

    with pytest.raises(GammaReconciliationError) as caught:
        reconcile_short_crypto_gamma(target(), raw_market(market))

    assert caught.value.code == code


def test_never_uses_gamma_start_date_as_contract_window_open() -> None:
    market = deepcopy(gamma_market())
    market["startDate"] = "1900-01-01T00:00:00Z"

    result = reconcile_short_crypto_gamma(target(), raw_market(market))

    assert result.end_date_ms == target().closes_at_ms


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("active", 1),
        ("closed", "false"),
        ("acceptingOrders", None),
    ],
)
def test_rejects_status_schema_drift(
    field: str,
    replacement: object,
) -> None:
    market = deepcopy(gamma_market())
    market[field] = replacement

    with pytest.raises(GammaReconciliationError) as caught:
        reconcile_short_crypto_gamma(target(), raw_market(market))

    assert caught.value.code == "gamma_status_schema_mismatch"


@pytest.mark.parametrize(
    "raw_body",
    [
        b"{not-json",
        b"[]",
        json.dumps(
            {
                key: value
                for key, value in gamma_market().items()
                if key != "acceptingOrders"
            },
            separators=(",", ":"),
        ).encode(),
    ],
)
def test_rejects_structural_gamma_schema_drift(raw_body: bytes) -> None:
    with pytest.raises(GammaReconciliationError) as caught:
        reconcile_short_crypto_gamma(target(), raw_body)

    assert caught.value.code in {"gamma_json_invalid", "gamma_schema_mismatch"}


def test_recognizes_only_identity_valid_exact_preactivation_shape() -> None:
    market = deepcopy(gamma_market())
    del market["acceptingOrders"]
    market.update(
        {
            "active": False,
            "closed": False,
            "ready": False,
            "funded": False,
            "outcomePrices": None,
        }
    )

    assert is_strict_gamma_preactivation(target(), raw_market(market))

    conflict = deepcopy(market)
    conflict["conditionId"] = "0x" + "cd" * 32
    assert not is_strict_gamma_preactivation(
        target(),
        raw_market(conflict),
    )

    activated = deepcopy(market)
    activated["ready"] = True
    assert not is_strict_gamma_preactivation(
        target(),
        raw_market(activated),
    )


def test_rejects_unannounced_distinct_rule_text() -> None:
    market = deepcopy(gamma_market())
    market["rules"] = "An additional rule not present in the announcement."

    with pytest.raises(GammaReconciliationError) as caught:
        reconcile_short_crypto_gamma(target(), raw_market(market))

    assert caught.value.code == "gamma_rule_text_conflict"


def test_requires_target_close_to_equal_slug_open_plus_horizon() -> None:
    inconsistent_target = replace(target(), horizon="5m")

    with pytest.raises(GammaReconciliationError) as caught:
        reconcile_short_crypto_gamma(inconsistent_target, raw_market())

    assert caught.value.code == "target_window_invalid"
