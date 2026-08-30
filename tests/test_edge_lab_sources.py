"""Contract tests for the public-only Edge Lab HTTP sources."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
import threading
import time
from typing import Any

import pytest
import requests

from src.edge_lab.data_api_trade_identity import data_api_trade_event_key
from src.edge_lab.sources import PublicSourceError, PublicSourcesClient


@dataclass
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: dict[str, str] | None = None
    reason: str = "OK"

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {"content-type": "application/json"}


class StubSession:
    def __init__(self, *responses: StubResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ConcurrentSession(StubSession):
    def __init__(self) -> None:
        super().__init__(
            StubResponse(b"1784899000"),
            StubResponse(b"1784899001"),
        )
        self.active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        with self._active_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return super().request(method, url, **kwargs)
        finally:
            with self._active_lock:
                self.active -= 1


def test_shared_public_client_serializes_session_and_rate_state() -> None:
    session = ConcurrentSession()
    client = PublicSourcesClient(
        session=session,
        retries=0,
        rate_per_second=1_000,
        burst=2,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = tuple(
            executor.map(lambda _: client.server_time().value, range(2))
        )

    assert set(values) == {Decimal("1784899000"), Decimal("1784899001")}
    assert session.max_active == 1


def test_gamma_market_keyset_page_preserves_raw_and_unknown_fields() -> None:
    body = (
        b'{"$schema":"https://gamma-api.polymarket.com/schemas/'
        b'MarketsKeysetListResponse.json","markets":[{"id":"12",'
        b'"question":"Will it rain?",'
        b'"conditionId":"0xabc","outcomes":"[\\"Yes\\",\\"No\\"]",'
        b'"clobTokenIds":"[\\"yes-token\\",\\"no-token\\"]",'
        b'"unknown":{"ratio":0.125}}],"next_cursor":"opaque+/="}'
    )
    session = StubSession(StubResponse(body))
    fake_time = FakeTime()
    client = PublicSourcesClient(
        session=session,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
        random_fn=lambda: 0.0,
        timeout=7.5,
    )

    result = client.gamma_markets(limit=101, cursor="cursor/keep+opaque=")

    assert result.raw.body == body
    assert result.raw.text == body.decode()
    assert result.raw.metadata.status_code == 200
    assert result.value.next_cursor == "opaque+/="
    assert result.value.response_limit == 100
    assert result.value.items[0]["outcomes"] == ["Yes", "No"]
    assert result.value.items[0]["clobTokenIds"] == [
        "yes-token",
        "no-token",
    ]
    assert result.value.items[0]["unknown"]["ratio"] == Decimal("0.125")
    assert session.calls == [
        {
            "method": "GET",
            "url": "https://gamma-api.polymarket.com/markets/keyset",
            "params": {
                "limit": 100,
                "after_cursor": "cursor/keep+opaque=",
            },
            "timeout": 7.5,
            "headers": {
                "Accept": "application/json",
                "User-Agent": (
                    "polymarket-edge-lab/0.2.0 (public-read-only)"
                ),
            },
            "allow_redirects": False,
        }
    ]


def _empty_market_page() -> StubResponse:
    return StubResponse(b'{"markets":[],"next_cursor":null}')


def test_retry_backoff_is_exponential_and_deterministic_when_injected() -> None:
    session = StubSession(
        requests.ConnectionError("offline"),
        StubResponse(b'{"error":"busy"}', status_code=503, reason="busy"),
        _empty_market_page(),
    )
    fake_time = FakeTime()
    client = PublicSourcesClient(
        session=session,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
        random_fn=lambda: 0.4,
        retries=2,
        backoff_base_seconds=1.0,
        jitter_seconds=0.5,
        rate_per_second=100,
        burst=1,
    )

    result = client.gamma_markets()

    assert result.raw.metadata.attempt == 3
    assert fake_time.sleeps == pytest.approx([1.2, 2.2])
    assert [call["timeout"] for call in session.calls] == [30.0, 30.0, 30.0]


def test_token_bucket_and_minimum_interval_throttle_every_http_attempt() -> None:
    session = StubSession(_empty_market_page(), _empty_market_page())
    fake_time = FakeTime()
    client = PublicSourcesClient(
        session=session,
        clock=fake_time.clock,
        sleeper=fake_time.sleep,
        random_fn=lambda: 0.0,
        retries=0,
        rate_per_second=2,
        burst=1,
        min_interval_seconds=1.0,
    )

    client.gamma_markets()
    client.gamma_markets()

    assert fake_time.sleeps == [1.0]


def test_server_time_preserves_raw_public_clock_probe() -> None:
    body = b"1784899000"
    session = StubSession(StubResponse(body))
    client = PublicSourcesClient(session=session)

    result = client.server_time()

    assert result.raw.body == body
    assert result.value == Decimal("1784899000")
    assert result.raw.metadata.source == "clob_time"
    assert session.calls == [
        {
            "method": "GET",
            "url": "https://clob.polymarket.com/time",
            "params": {},
            "timeout": 30.0,
            "headers": {
                "Accept": "application/json",
                "User-Agent": (
                    "polymarket-edge-lab/0.2.0 (public-read-only)"
                ),
            },
            "allow_redirects": False,
        }
    ]

    invalid = PublicSourcesClient(
        session=StubSession(StubResponse(b"-1"))
    )
    with pytest.raises(PublicSourceError, match="non-negative"):
        invalid.server_time()


def test_non_retryable_failure_is_explicit_and_retains_raw_response() -> None:
    response = StubResponse(
        b'{"error":"invalid cursor"}', status_code=400, reason="Bad Request"
    )
    session = StubSession(response)
    client = PublicSourcesClient(session=session, retries=3)

    with pytest.raises(PublicSourceError, match=r"HTTP 400") as caught:
        client.gamma_markets(cursor="invalid")

    assert caught.value.raw is not None
    assert caught.value.raw.body == response.content
    assert len(session.calls) == 1


def test_injected_session_with_authentication_headers_is_rejected() -> None:
    session = StubSession(_empty_market_page())
    session.headers["Authorization"] = "must-not-leak"

    with pytest.raises(PublicSourceError, match="authentication header"):
        PublicSourcesClient(session=session)

    auth_session = StubSession(_empty_market_page())
    auth_session.auth = ("user", "secret")
    with pytest.raises(PublicSourceError, match="authentication credentials"):
        PublicSourcesClient(session=auth_session)


def test_compact_clob_market_parameters_are_decimal_and_fail_closed() -> None:
    body = (
        b'{"c":"0xabc/condition","t":[{"tid":"yes"},{"tid":"no"}],'
        b'"mos":5,"mts":0.0025,"ao":true,'
        b'"r":{"mi":50,"ma":5.5,"e":true,"moas":30},'
        b'"fd":{"r":0.04,"e":1,"to":true},"future_field":{"x":7}}'
    )
    session = StubSession(StubResponse(body))
    client = PublicSourcesClient(session=session)

    result = client.clob_market("0xabc/condition")

    assert result.raw.body == body
    assert result.value.condition_id == "0xabc/condition"
    assert result.value.min_order_size == Decimal("5")
    assert result.value.tick_size == Decimal("0.0025")
    assert result.value.accepting_orders is True
    assert result.value.rewards.min_size == Decimal("50")
    assert result.value.rewards.max_spread == Decimal("5.5")
    assert result.value.rewards.enabled is True
    assert result.value.rewards.min_order_age_seconds == 30
    assert result.value.fees.rate == Decimal("0.04")
    assert result.value.fees.exponent == Decimal("1")
    assert result.value.fees.taker_only is True
    assert result.value.raw["future_field"]["x"] == 7
    assert session.calls[0]["url"].endswith(
        "/clob-markets/0xabc%2Fcondition"
    )

    sparse_rewards = StubSession(
        StubResponse(
            b'{"c":"0xabc","t":[],"mos":"5","mts":"0.01","ao":true,'
            b'"r":{"mi":"50","ma":"4.5","moas":30},'
            b'"fd":{"r":"0.07","e":"1","to":true}}'
        )
    )
    sparse = PublicSourcesClient(session=sparse_rewards).clob_market("0xabc")
    assert sparse.value.rewards.min_size == Decimal("50")
    assert sparse.value.rewards.max_spread == Decimal("4.5")
    assert sparse.value.rewards.enabled is None
    with pytest.raises(PublicSourceError, match="not explicitly enabled"):
        sparse.value.rewards.require_scoring_terms()

    age_only = StubSession(
        StubResponse(
            b'{"c":"0xabc","t":[],"mos":"5","mts":"0.01","ao":true,'
            b'"r":{"moas":4}}'
        )
    )
    age_market = PublicSourcesClient(session=age_only).clob_market("0xabc")
    assert age_market.value.rewards.min_size is None
    assert age_market.value.rewards.max_spread is None
    assert age_market.value.rewards.enabled is None
    assert age_market.value.rewards.min_order_age_seconds == 4
    assert age_market.value.fees is None
    with pytest.raises(PublicSourceError, match="fee schedule is unknown"):
        age_market.value.require_fee_schedule()

    top_level_age = StubSession(
        StubResponse(
            b'{"c":"0xabc","t":[],"mos":"5","mts":"0.01","ao":true,'
            b'"r":null,"oas":9,"fd":{"r":"0.04","e":"1"}}'
        )
    )
    top_level_market = PublicSourcesClient(
        session=top_level_age
    ).clob_market("0xabc")
    assert top_level_market.value.rewards.min_order_age_seconds == 9
    assert top_level_market.value.fees is not None
    assert top_level_market.value.fees.taker_only is False

    malformed = StubSession(
        StubResponse(
            b'{"c":"0xabc","t":[],"mos":"5","mts":"0.01","ao":true,'
            b'"r":{"e":true,"mi":"50","moas":30},'
            b'"fd":{"r":"0.04","e":"1","to":true}}'
        )
    )
    with pytest.raises(PublicSourceError, match="reward max spread") as caught:
        PublicSourcesClient(session=malformed).clob_market("0xabc")
    assert caught.value.raw is not None


def test_public_data_api_trades_bind_taker_only_side_and_raw_bytes() -> None:
    condition_id = "0x" + "ab" * 32
    transaction_hash = "0x" + "cd" * 32
    body = (
        "[{"
        f'"conditionId":"{condition_id}",'
        '"asset":"123","side":"SELL","size":"7.5","price":"0.40",'
        '"timestamp":1784974804,'
        f'"transactionHash":"{transaction_hash}",'
        '"proxyWallet":"0x1111111111111111111111111111111111111111",'
        '"outcome":"Up","outcomeIndex":0'
        "}]"
    ).encode()
    session = StubSession(StubResponse(body))
    client = PublicSourcesClient(session=session)

    result = client.data_trades(
        condition_id,
        taker_only=True,
        limit=1000,
    )

    assert result.raw.body == body
    assert result.raw.metadata.source == "data_api_trades"
    assert result.value[0]["side"] == "SELL"
    assert result.value[0]["transactionHash"] == transaction_hash
    assert session.calls[0]["url"] == (
        "https://data-api.polymarket.com/trades"
    )
    assert session.calls[0]["params"] == {
        "market": condition_id,
        "takerOnly": "true",
        "limit": 1000,
        "offset": 0,
    }

    malformed = StubSession(
        StubResponse(
            body.replace(b'"SELL"', b'"UNDEFINED"', 1)
        )
    )
    with pytest.raises(PublicSourceError, match="side") as caught:
        PublicSourcesClient(session=malformed).data_trades(
            condition_id,
            taker_only=True,
        )
    assert caught.value.raw is not None

    missing_match_identity = StubSession(
        StubResponse(
            body.replace(
                b',"proxyWallet":"0x1111111111111111111111111111111111111111"',
                b"",
                1,
            )
        )
    )
    with pytest.raises(PublicSourceError, match="proxyWallet") as caught:
        PublicSourcesClient(session=missing_match_identity).data_trades(
            condition_id,
            taker_only=True,
        )
    assert caught.value.raw is not None


def test_data_api_trade_composite_key_normalizes_repeated_snapshot_rows() -> None:
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

    first = data_api_trade_event_key(trade)
    repeated = data_api_trade_event_key(
        {
            **trade,
            "conditionId": str(trade["conditionId"]).upper().replace(
                "0X",
                "0x",
            ),
            "size": "10.0",
            "price": "0.4000",
            "transactionHash": str(trade["transactionHash"]).upper().replace(
                "0X",
                "0x",
            ),
            "proxyWallet": str(trade["proxyWallet"]).upper().replace(
                "0X",
                "0x",
            ),
        }
    )

    assert repeated == first
    assert data_api_trade_event_key({**trade, "asset": "456"}) != first


def test_current_rewards_pagination_uses_response_limit_and_opaque_cursor() -> None:
    first = StubResponse(
        b'{"data":[{"condition_id":"0x1","rewards_max_spread":3.5,'
        b'"rewards_min_size":50,"rewards_config":[],'
        b'"native_daily_rate":12.25,"total_daily_rate":12.25,'
        b'"unknown":"kept"}],"limit":500,"count":1,'
        b'"next_cursor":"opaque/+NTAw=="}'
    )
    second = StubResponse(
        b'{"data":[],"limit":500,"count":0,"next_cursor":null}'
    )
    session = StubSession(first, second)
    client = PublicSourcesClient(
        session=session,
        rate_per_second=100,
        min_interval_seconds=0,
    )

    pages = list(client.current_reward_pages(limit=3))

    assert len(pages) == 2
    assert pages[0].value.response_limit == 500
    assert pages[0].value.count == 1
    assert pages[0].value.next_cursor == "opaque/+NTAw=="
    assert pages[0].value.items[0]["rewards_max_spread"] == Decimal("3.5")
    assert pages[0].value.items[0]["native_daily_rate"] == Decimal("12.25")
    assert pages[0].value.items[0]["unknown"] == "kept"
    assert pages[0].raw.body == first.content
    assert session.calls[0]["params"] == {"limit": 3}
    assert session.calls[1]["params"] == {
        "limit": 500,
        "next_cursor": "opaque/+NTAw==",
    }


def test_current_rewards_accepts_sponsored_only_row_without_native_rate() -> None:
    sponsored = StubResponse(
        b'{"data":[{"condition_id":"0x1","rewards_max_spread":4.5,'
        b'"rewards_min_size":50,"rewards_config":[],'
        b'"sponsored_daily_rate":1.99872,"sponsors_count":1,'
        b'"total_daily_rate":1.99872}],'
        b'"limit":500,"count":1,"next_cursor":null}'
    )

    page = PublicSourcesClient(session=StubSession(sponsored)).current_reward_page()

    assert page.value.items[0]["total_daily_rate"] == Decimal("1.99872")
    assert "native_daily_rate" not in page.value.items[0]


def test_book_get_and_explicit_public_batch_preserve_raw_depth() -> None:
    single_body = (
        b'{"asset_id":"yes","market":"0x1","timestamp":"123",'
        b'"bids":[{"price":"0.40","size":"12.5","orders":2}],'
        b'"asks":[{"price":0.42,"size":7}],"unknown":"kept"}'
    )
    single_session = StubSession(StubResponse(single_body))
    single_client = PublicSourcesClient(session=single_session)

    fetched = single_client.book("yes")

    assert fetched.raw.body == single_body
    assert fetched.value.token_id == "yes"
    assert fetched.value.bids[0].price == Decimal("0.40")
    assert fetched.value.bids[0].size == Decimal("12.5")
    assert fetched.value.bids[0].raw["orders"] == 2
    assert fetched.value.asks[0].price == Decimal("0.42")
    assert fetched.value.raw["unknown"] == "kept"
    assert single_session.calls[0]["method"] == "GET"
    assert single_session.calls[0]["params"] == {"token_id": "yes"}

    batch_body = (
        b'[{"asset_id":"yes","bids":[],"asks":[]},'
        b'{"asset_id":"no","bids":[],"asks":[]}]'
    )
    batch_session = StubSession(StubResponse(batch_body))
    batch_client = PublicSourcesClient(session=batch_session)
    with pytest.raises(PublicSourceError, match="allow_public_batch"):
        batch_client.books(["yes", "no"])
    assert batch_session.calls == []

    batch = batch_client.books(
        ["yes", "no"], allow_public_batch=True
    )

    assert batch.raw.body == batch_body
    assert [book.token_id for book in batch.value] == ["yes", "no"]
    assert batch_session.calls[0]["method"] == "POST"
    assert batch_session.calls[0]["url"].endswith("/books")
    assert batch_session.calls[0]["json"] == [
        {"token_id": "yes"},
        {"token_id": "no"},
    ]
    assert "Authorization" not in batch_session.calls[0]["headers"]


def test_resolution_rules_fetch_retains_full_gamma_evidence() -> None:
    body = (
        b'{"id":"market/42","question":"Highest temperature?",'
        b'"conditionId":"0xweather","questionID":"0xquestion",'
        b'"description":"Use the final daily history row, including revisions.",'
        b'"rules":"Whole degrees Celsius; revisions count until next day.",'
        b'"resolutionSource":"https://example.test/station/OPKC",'
        b'"endDate":"2026-07-26T23:59:00Z","resolvedBy":"0xresolver",'
        b'"closed":false,"umaResolutionStatus":"unresolved",'
        b'"outcomes":"[\\"28 C or below\\",\\"29 C\\"]",'
        b'"extra_resolution_flag":"kept"}'
    )
    session = StubSession(StubResponse(body))
    client = PublicSourcesClient(session=session)

    fetched = client.resolution_rules("market/42")

    assert fetched.raw.body == body
    assert fetched.value.market_id == "market/42"
    assert fetched.value.condition_id == "0xweather"
    assert fetched.value.question_id == "0xquestion"
    assert fetched.value.question == "Highest temperature?"
    assert fetched.value.rules_text.startswith("Whole degrees Celsius")
    assert fetched.value.description.startswith("Use the final daily")
    assert fetched.value.resolution_source.endswith("/OPKC")
    assert fetched.value.end_date == "2026-07-26T23:59:00Z"
    assert fetched.value.closed is False
    assert fetched.value.uma_resolution_status == "unresolved"
    assert fetched.value.raw["outcomes"] == ["28 C or below", "29 C"]
    assert fetched.value.raw["extra_resolution_flag"] == "kept"
    assert session.calls[0]["url"].endswith("/markets/market%2F42")

    missing_source = StubSession(
        StubResponse(
            b'{"id":"1","question":"Q","conditionId":"0x1",'
            b'"description":"rules","endDate":"2026-01-01",'
            b'"closed":false}'
        )
    )
    with pytest.raises(
        PublicSourceError, match="resolutionSource"
    ) as caught:
        PublicSourcesClient(session=missing_source).resolution_rules("1")
    assert caught.value.raw is not None


def test_gamma_event_keyset_normalizes_nested_market_arrays() -> None:
    body = (
        b'{"$schema":"https://gamma-api.polymarket.com/schemas/'
        b'EventsKeysetListResponse.json","events":[{"id":"event-1",'
        b'"title":"Daily weather",'
        b'"markets":[{"id":"market-1","question":"29 C?",'
        b'"conditionId":"0x1","outcomes":"[\\"Yes\\",\\"No\\"]",'
        b'"outcomePrices":"[\\"0.4\\",\\"0.6\\"]",'
        b'"clobTokenIds":["yes","no"],"future":true}],'
        b'"event_future":"kept"}],'
        b'"next_cursor":"event-cursor"}'
    )
    session = StubSession(StubResponse(body))
    client = PublicSourcesClient(session=session)

    fetched = client.gamma_events(limit=500, cursor="opaque/start")

    event = fetched.value.items[0]
    assert event["event_future"] == "kept"
    assert event["markets"][0]["outcomes"] == ["Yes", "No"]
    assert event["markets"][0]["outcomePrices"] == ["0.4", "0.6"]
    assert event["markets"][0]["future"] is True
    assert session.calls[0]["params"] == {
        "limit": 500,
        "after_cursor": "opaque/start",
    }
    assert fetched.value.response_limit == 500

    invalid = StubSession(
        StubResponse(
            b'{"events":[{"id":"e","markets":[]}],'
            b'"next_cursor":null}'
        )
    )
    with pytest.raises(PublicSourceError, match="title") as caught:
        PublicSourcesClient(session=invalid).gamma_events()
    assert caught.value.raw is not None
