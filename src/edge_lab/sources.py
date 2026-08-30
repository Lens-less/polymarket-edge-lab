"""Fail-closed, public-only HTTP sources for the Edge Discovery Lab.

The client deliberately exposes each response's original bytes alongside its
validated value.  Derived research artifacts can therefore retain the exact
source evidence instead of keeping only a lossy decoded representation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import random
import re
import threading
import time
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Generic,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    TypeVar,
)
from urllib.parse import parse_qsl, quote, urlsplit

import requests

from .data_api_trade_identity import data_api_trade_event_key
from .network_safety import (
    PublicNetworkSafetyError,
    configure_public_session,
    discard_session_cookies,
    is_sensitive_parameter_name,
    request_error_code,
    safe_error_details,
    sanitized_response_headers,
)


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
USER_AGENT = "polymarket-edge-lab/0.2.0 (public-read-only)"

T = TypeVar("T")


class PublicSourceError(RuntimeError):
    """A public source could not be fetched or safely interpreted."""

    def __init__(
        self,
        message: str,
        *,
        raw: Optional["RawResponse"] = None,
        code: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.code = code
        self.error_type = error_type


@dataclass(frozen=True)
class FetchMetadata:
    source: str
    method: str
    url: str
    request_params: Mapping[str, Any]
    status_code: int
    requested_at: float
    received_at: float
    attempt: int
    response_headers: Mapping[str, str]


@dataclass(frozen=True)
class RawResponse:
    body: bytes
    text: str
    metadata: FetchMetadata


@dataclass(frozen=True)
class Fetched(Generic[T]):
    raw: RawResponse
    value: T


@dataclass(frozen=True)
class GammaPage:
    items: tuple[Mapping[str, Any], ...]
    next_cursor: Optional[str]
    response_limit: int


@dataclass(frozen=True)
class CompactRewardConfig:
    min_size: Optional[Decimal]
    max_spread: Optional[Decimal]
    enabled: Optional[bool]
    min_order_age_seconds: Optional[int]
    skip_min_order_age: Optional[bool]

    def require_scoring_terms(self) -> "CompactRewardConfig":
        """Return complete, explicitly enabled terms or fail closed."""

        if self.enabled is not True:
            raise PublicSourceError(
                "market rewards are not explicitly enabled"
            )
        if self.min_size is None or self.max_spread is None:
            raise PublicSourceError(
                "enabled market rewards lack scoring size/spread terms"
            )
        return self


@dataclass(frozen=True)
class CompactFeeSchedule:
    rate: Decimal
    exponent: Decimal
    taker_only: bool


@dataclass(frozen=True)
class CompactCLOBMarket:
    condition_id: str
    tokens: tuple[Mapping[str, Any], ...]
    min_order_size: Decimal
    tick_size: Decimal
    accepting_orders: bool
    rewards: CompactRewardConfig
    fees: Optional[CompactFeeSchedule]
    raw: Mapping[str, Any]

    def require_fee_schedule(self) -> CompactFeeSchedule:
        """Return a complete fee schedule or reject economic evaluation."""

        if self.fees is None:
            raise PublicSourceError(
                "market fee schedule is unknown; missing complete fd.r/fd.e"
            )
        return self.fees


@dataclass(frozen=True)
class RewardPage:
    items: tuple[Mapping[str, Any], ...]
    next_cursor: Optional[str]
    response_limit: int
    count: int


@dataclass(frozen=True)
class SourceBookLevel:
    price: Decimal
    size: Decimal
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class SourceBook:
    token_id: str
    bids: tuple[SourceBookLevel, ...]
    asks: tuple[SourceBookLevel, ...]
    timestamp_ms: Optional[int]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ResolutionRules:
    market_id: str
    condition_id: str
    question_id: Optional[str]
    question: str
    description: str
    rules_text: str
    resolution_source: str
    end_date: str
    resolved_by: Optional[str]
    closed: bool
    uma_resolution_status: Optional[str]
    raw: Mapping[str, Any]


def _decode_json(raw: RawResponse) -> Any:
    try:
        return json.loads(raw.text, parse_float=Decimal)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PublicSourceError(
            "public source returned invalid JSON",
            code="invalid_json_response",
            error_type=type(exc).__name__,
        ) from exc


def _mapping(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PublicSourceError(f"{context} must be a JSON object")
    return dict(value)


def _required(
    value: Mapping[str, Any], fields: Sequence[str], *, context: str
) -> dict[str, Any]:
    missing = [field for field in fields if field not in value]
    if missing:
        raise PublicSourceError(
            f"{context} missing required field(s): {', '.join(missing)}"
        )
    return dict(value)


def _decimal(value: Any, *, context: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PublicSourceError(f"{context} must be a decimal value")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise PublicSourceError(f"{context} must be a decimal value") from exc


def _boolean(value: Any, *, context: str) -> bool:
    if not isinstance(value, bool):
        raise PublicSourceError(f"{context} must be a boolean")
    return value


def _integer(value: Any, *, context: str) -> int:
    number = _decimal(value, context=context)
    if number != number.to_integral_value() or number < 0:
        raise PublicSourceError(
            f"{context} must be a non-negative integer"
        )
    return int(number)


def _nonempty_text(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicSourceError(f"{context} must be a non-empty string")
    return value


_GAMMA_ARRAY_FIELDS = frozenset(
    {"clobTokenIds", "outcomes", "outcomePrices"}
)


def _gamma_item(value: Any, *, kind: str) -> Mapping[str, Any]:
    required = (
        ("id", "title", "markets")
        if kind == "events"
        else ("id", "question", "conditionId")
    )
    item = _required(
        _mapping(value, context=f"Gamma {kind} item"),
        required,
        context=f"Gamma {kind} item",
    )
    for field in _GAMMA_ARRAY_FIELDS:
        encoded = item.get(field)
        if not isinstance(encoded, str):
            continue
        try:
            decoded = json.loads(encoded, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise PublicSourceError(
                f"Gamma {kind} item field {field} is invalid JSON"
            ) from exc
        if not isinstance(decoded, list):
            raise PublicSourceError(
                f"Gamma {kind} item field {field} must decode to an array"
            )
        item[field] = decoded
    if kind == "events":
        markets = item["markets"]
        if not isinstance(markets, list):
            raise PublicSourceError(
                "Gamma events item field markets must be an array"
            )
        item["markets"] = tuple(
            _gamma_item(market, kind="markets") for market in markets
        )
    return MappingProxyType(item)


def _book_level(value: Any, *, context: str) -> SourceBookLevel:
    level = _required(
        _mapping(value, context=context),
        ("price", "size"),
        context=context,
    )
    return SourceBookLevel(
        price=_decimal(level["price"], context=f"{context} price"),
        size=_decimal(level["size"], context=f"{context} size"),
        raw=MappingProxyType(level),
    )


def _book(value: Any, *, expected_token_id: Optional[str] = None) -> SourceBook:
    book = _required(
        _mapping(value, context="CLOB book"),
        ("asset_id", "bids", "asks"),
        context="CLOB book",
    )
    token_id = str(book["asset_id"])
    if not token_id:
        raise PublicSourceError("CLOB book asset_id cannot be empty")
    if expected_token_id is not None and token_id != expected_token_id:
        raise PublicSourceError(
            "CLOB book asset_id does not match requested token"
        )
    if not isinstance(book["bids"], list) or not isinstance(book["asks"], list):
        raise PublicSourceError("CLOB book bids and asks must be arrays")
    timestamp = book.get("timestamp")
    timestamp_ms = (
        None
        if timestamp in (None, "")
        else _integer(timestamp, context="CLOB book timestamp")
    )
    return SourceBook(
        token_id=token_id,
        bids=tuple(
            _book_level(level, context="CLOB book bid")
            for level in book["bids"]
        ),
        asks=tuple(
            _book_level(level, context="CLOB book ask")
            for level in book["asks"]
        ),
        timestamp_ms=timestamp_ms,
        raw=MappingProxyType(book),
    )


class PublicSourcesClient:
    """Synchronous requests client with no authenticated or trading routes."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
        retries: int = 2,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
        backoff_base_seconds: float = 0.5,
        jitter_seconds: float = 0.1,
        rate_per_second: float = 10.0,
        burst: int = 1,
        min_interval_seconds: float = 0.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        if backoff_base_seconds < 0 or jitter_seconds < 0:
            raise ValueError("backoff and jitter cannot be negative")
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must be at least one")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds cannot be negative")
        self.session = session or requests.Session()
        if hasattr(self.session, "trust_env"):
            # Never implicitly read proxy or credential-related environment.
            self.session.trust_env = False
        self._assert_public_session()
        self.timeout = timeout
        self.retries = retries
        self.clock = clock
        self.sleeper = sleeper
        self.random_fn = random_fn
        self.backoff_base_seconds = backoff_base_seconds
        self.jitter_seconds = jitter_seconds
        self.rate_per_second = rate_per_second
        self.burst = burst
        self.min_interval_seconds = min_interval_seconds
        self._tokens = float(burst)
        self._last_refill_at = self.clock()
        self._last_request_at: Optional[float] = None
        self._request_lock = threading.RLock()

    def _assert_public_session(self) -> None:
        try:
            configure_public_session(self.session)
        except PublicNetworkSafetyError as exc:
            raise PublicSourceError(
                str(exc),
                code=exc.code,
                error_type=type(exc).__name__,
            ) from exc

    def _acquire_rate_token(self) -> None:
        now = self.clock()
        elapsed = max(0.0, now - self._last_refill_at)
        self._tokens = min(
            float(self.burst),
            self._tokens + elapsed * self.rate_per_second,
        )
        token_wait = (
            0.0
            if self._tokens >= 1.0
            else (1.0 - self._tokens) / self.rate_per_second
        )
        interval_wait = 0.0
        if self._last_request_at is not None:
            interval_wait = max(
                0.0,
                self.min_interval_seconds - (now - self._last_request_at),
            )
        wait = max(token_wait, interval_wait)
        if wait:
            self.sleeper(wait)
            now = self.clock()
            elapsed = max(0.0, now - self._last_refill_at)
            self._tokens = min(
                float(self.burst),
                self._tokens + elapsed * self.rate_per_second,
            )
        self._tokens = max(0.0, self._tokens - 1.0)
        self._last_refill_at = now
        self._last_request_at = now

    def _request(
        self,
        method: str,
        url: str,
        *,
        source: str,
        params: Optional[Mapping[str, Any]] = None,
        payload: Any = None,
        allow_public_batch: bool = False,
    ) -> RawResponse:
        # requests.Session and the shared token bucket are mutable.  Dynamic
        # capture workers call this client from independent executor threads,
        # so serialize each complete HTTP attempt/retry sequence.
        with self._request_lock:
            return self._request_locked(
                method,
                url,
                source=source,
                params=params,
                payload=payload,
                allow_public_batch=allow_public_batch,
            )

    def _request_locked(
        self,
        method: str,
        url: str,
        *,
        source: str,
        params: Optional[Mapping[str, Any]] = None,
        payload: Any = None,
        allow_public_batch: bool = False,
    ) -> RawResponse:
        method = method.upper()
        is_public_books = method == "POST" and url == f"{CLOB_BASE}/books"
        if method != "GET" and not (allow_public_batch and is_public_books):
            raise PublicSourceError(
                "request method is not an allowed public-only operation",
                code="request_method_forbidden",
            )
        request_params = dict(params or {})
        parsed_url = urlsplit(url)
        if any(
            is_sensitive_parameter_name(key)
            for key, _ in parse_qsl(
                parsed_url.query,
                keep_blank_values=True,
            )
        ) or any(
            is_sensitive_parameter_name(key) for key in request_params
        ):
            raise PublicSourceError(
                "public request contains a credential-like parameter",
                code="sensitive_request_parameter",
            )
        last_error: Optional[BaseException] = None
        for attempt_index in range(self.retries + 1):
            self._assert_public_session()
            self._acquire_rate_token()
            requested_at = self.clock()
            kwargs: dict[str, Any] = {
                "params": request_params,
                "timeout": self.timeout,
                "headers": {
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                },
                "allow_redirects": False,
            }
            if method == "POST":
                kwargs["json"] = payload
            try:
                response = self.session.request(method, url, **kwargs)
            except Exception as exc:
                discard_session_cookies(self.session)
                details = safe_error_details(
                    exc,
                    code=request_error_code(exc),
                )
                last_error = PublicSourceError(
                    (
                        "public request failed:"
                        f"{details['error_type']}:{details['error_code']}"
                    ),
                    code=details["error_code"],
                    error_type=details["error_type"],
                )
                if attempt_index >= self.retries:
                    raise last_error from exc
            else:
                discard_session_cookies(self.session)
                received_at = self.clock()
                body = bytes(response.content)
                metadata = FetchMetadata(
                    source=source,
                    method=method,
                    url=url,
                    request_params=MappingProxyType(request_params.copy()),
                    status_code=int(response.status_code),
                    requested_at=requested_at,
                    received_at=received_at,
                    attempt=attempt_index + 1,
                    response_headers=MappingProxyType(
                        sanitized_response_headers(response.headers)
                    ),
                )
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise PublicSourceError(
                        "public source returned non-UTF-8 content",
                        raw=RawResponse(
                            body=body,
                            text=body.decode("utf-8", errors="replace"),
                            metadata=metadata,
                        ),
                        code="non_utf8_response",
                        error_type=type(exc).__name__,
                    ) from exc
                raw = RawResponse(
                    body=body,
                    text=text,
                    metadata=metadata,
                )
                if 200 <= response.status_code < 300:
                    return raw
                last_error = PublicSourceError(
                    f"public source returned HTTP {response.status_code}",
                    raw=raw,
                    code="http_status_failure",
                    error_type="HTTPStatus",
                )
                retryable = response.status_code == 429 or (
                    500 <= response.status_code < 600
                )
                if not retryable or attempt_index >= self.retries:
                    raise last_error
            delay = (
                self.backoff_base_seconds * (2**attempt_index)
                + self.jitter_seconds * self.random_fn()
            )
            if delay:
                self.sleeper(delay)
        raise PublicSourceError(
            "public request failed",
            code=(
                last_error.code
                if isinstance(last_error, PublicSourceError)
                else "request_failure"
            ),
            error_type=(
                last_error.error_type
                if isinstance(last_error, PublicSourceError)
                else "RequestException"
            ),
        ) from last_error

    def _get(
        self,
        url: str,
        *,
        source: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> RawResponse:
        return self._request("GET", url, source=source, params=params)

    def _gamma_page(
        self, kind: str, *, limit: int, cursor: Optional[str]
    ) -> Fetched[GammaPage]:
        maximum = 500 if kind == "events" else 100
        requested_limit = min(max(int(limit), 1), maximum)
        params: dict[str, Any] = {"limit": requested_limit}
        if cursor is not None:
            params["after_cursor"] = cursor
        raw = self._get(
            f"{GAMMA_BASE}/{kind}/keyset", source="gamma", params=params
        )
        try:
            payload = _mapping(
                _decode_json(raw), context=f"Gamma {kind} page"
            )
            data = payload.get(kind)
            if not isinstance(data, list):
                raise PublicSourceError(
                    f"Gamma {kind} page {kind} must be an array"
                )
            cursor_value = payload.get("next_cursor")
            if cursor_value is not None and not isinstance(cursor_value, str):
                raise PublicSourceError(
                    f"Gamma {kind} page next_cursor must be a string or null"
                )
            page = GammaPage(
                items=tuple(
                    _gamma_item(item, kind=kind) for item in data
                ),
                next_cursor=cursor_value,
                response_limit=requested_limit,
            )
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=page)

    def gamma_markets(
        self, *, limit: int = 100, cursor: Optional[str] = None
    ) -> Fetched[GammaPage]:
        return self._gamma_page("markets", limit=limit, cursor=cursor)

    def gamma_events(
        self, *, limit: int = 100, cursor: Optional[str] = None
    ) -> Fetched[GammaPage]:
        return self._gamma_page("events", limit=limit, cursor=cursor)

    def server_time(self) -> Fetched[Decimal]:
        """Fetch the public CLOB clock for receive-time offset bounds."""

        raw = self._get(f"{CLOB_BASE}/time", source="clob_time")
        try:
            value = _decimal(
                _decode_json(raw),
                context="CLOB server time",
            )
            if value < 0:
                raise PublicSourceError(
                    "CLOB server time must be non-negative"
                )
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=value)

    def clob_market(
        self, condition_id: str
    ) -> Fetched[CompactCLOBMarket]:
        """Fetch and strictly parse current compact market-level parameters."""
        if not condition_id:
            raise ValueError("condition_id cannot be empty")
        url = f"{CLOB_BASE}/clob-markets/{quote(condition_id, safe='')}"
        raw = self._get(url, source="clob")
        try:
            payload = _required(
                _mapping(_decode_json(raw), context="CLOB compact market"),
                ("c", "t", "mos", "mts", "ao", "r"),
                context="CLOB compact market",
            )
            if str(payload["c"]) != condition_id:
                raise PublicSourceError(
                    "CLOB compact market condition does not match request"
                )
            tokens_value = payload["t"]
            if not isinstance(tokens_value, list):
                raise PublicSourceError(
                    "CLOB compact market t must be an array"
                )
            tokens = tuple(
                MappingProxyType(
                    _mapping(token, context="CLOB compact market token")
                )
                for token in tokens_value
            )
            reward_value = payload["r"]
            reward = (
                {}
                if reward_value is None
                else _mapping(
                    reward_value, context="CLOB compact market r"
                )
            )
            reward_min_size = (
                None
                if "mi" not in reward
                else _decimal(
                    reward["mi"],
                    context="CLOB compact market reward min size",
                )
            )
            reward_max_spread = (
                None
                if "ma" not in reward
                else _decimal(
                    reward["ma"],
                    context="CLOB compact market reward max spread",
                )
            )
            if (reward_min_size is None) != (reward_max_spread is None):
                missing_name = (
                    "reward min size"
                    if reward_min_size is None
                    else "reward max spread"
                )
                raise PublicSourceError(
                    f"CLOB compact market is missing {missing_name}"
                )
            reward_enabled = (
                None
                if "e" not in reward
                else _boolean(
                    reward["e"],
                    context="CLOB compact market r.e",
                )
            )
            if reward_enabled is True and reward_min_size is None:
                raise PublicSourceError(
                    "enabled CLOB rewards lack min size and max spread"
                )
            nested_age = (
                None
                if "moas" not in reward
                else _integer(
                    reward["moas"],
                    context="CLOB compact market r.moas",
                )
            )
            top_level_age = (
                None
                if "oas" not in payload
                else _integer(
                    payload["oas"],
                    context="CLOB compact market oas",
                )
            )
            if (
                nested_age is not None
                and top_level_age is not None
                and nested_age != top_level_age
            ):
                raise PublicSourceError(
                    "CLOB compact market r.moas conflicts with oas"
                )
            minimum_order_age = (
                nested_age if nested_age is not None else top_level_age
            )
            skip_min_order_age = (
                None
                if "smoa" not in reward
                else _boolean(
                    reward["smoa"],
                    context="CLOB compact market r.smoa",
                )
            )
            fee_value = payload.get("fd")
            fee_schedule: Optional[CompactFeeSchedule] = None
            if fee_value is not None:
                fee = _mapping(
                    fee_value, context="CLOB compact market fd"
                )
                if "r" in fee and "e" in fee:
                    fee_schedule = CompactFeeSchedule(
                        rate=_decimal(
                            fee["r"],
                            context="CLOB compact market fd.r",
                        ),
                        exponent=_decimal(
                            fee["e"],
                            context="CLOB compact market fd.e",
                        ),
                        taker_only=(
                            False
                            if "to" not in fee
                            else _boolean(
                                fee["to"],
                                context="CLOB compact market fd.to",
                            )
                        ),
                    )
            value = CompactCLOBMarket(
                condition_id=str(payload["c"]),
                tokens=tokens,
                min_order_size=_decimal(
                    payload["mos"], context="CLOB compact market mos"
                ),
                tick_size=_decimal(
                    payload["mts"], context="CLOB compact market mts"
                ),
                accepting_orders=_boolean(
                    payload["ao"], context="CLOB compact market ao"
                ),
                rewards=CompactRewardConfig(
                    min_size=reward_min_size,
                    max_spread=reward_max_spread,
                    enabled=reward_enabled,
                    min_order_age_seconds=minimum_order_age,
                    skip_min_order_age=skip_min_order_age,
                ),
                fees=fee_schedule,
                raw=MappingProxyType(payload),
            )
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=value)

    def current_reward_page(
        self, *, limit: int = 500, cursor: Optional[str] = None
    ) -> Fetched[RewardPage]:
        """Fetch one current-rewards page without inferring cursor contents."""
        requested_limit = min(max(int(limit), 1), 500)
        params: dict[str, Any] = {"limit": requested_limit}
        if cursor is not None:
            params["next_cursor"] = cursor
        raw = self._get(
            f"{CLOB_BASE}/rewards/markets/current",
            source="clob_rewards",
            params=params,
        )
        try:
            payload = _required(
                _mapping(_decode_json(raw), context="current rewards page"),
                ("data", "limit", "count", "next_cursor"),
                context="current rewards page",
            )
            data = payload["data"]
            if not isinstance(data, list):
                raise PublicSourceError(
                    "current rewards page data must be an array"
                )
            response_limit = _integer(
                payload["limit"], context="current rewards page limit"
            )
            if not 1 <= response_limit <= 500:
                raise PublicSourceError(
                    "current rewards page limit must be between 1 and 500"
                )
            count = _integer(
                payload["count"], context="current rewards page count"
            )
            cursor_value = payload["next_cursor"]
            if cursor_value is not None and not isinstance(cursor_value, str):
                raise PublicSourceError(
                    "current rewards page next_cursor must be a string or null"
                )
            required_item_fields = (
                "condition_id",
                "rewards_max_spread",
                "rewards_min_size",
                "rewards_config",
                "total_daily_rate",
            )
            items = tuple(
                MappingProxyType(
                    _required(
                        _mapping(item, context="current rewards item"),
                        required_item_fields,
                        context="current rewards item",
                    )
                )
                for item in data
            )
            page = RewardPage(
                items=items,
                next_cursor=cursor_value,
                response_limit=response_limit,
                count=count,
            )
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=page)

    def current_reward_pages(
        self,
        *,
        limit: int = 500,
        cursor: Optional[str] = None,
        max_pages: Optional[int] = None,
    ) -> Iterator[Fetched[RewardPage]]:
        """Yield pages, following only the server-provided opaque cursor."""
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be at least one")
        next_limit = min(max(int(limit), 1), 500)
        next_cursor = cursor
        seen_cursors = {cursor} if cursor is not None else set()
        page_number = 0
        while max_pages is None or page_number < max_pages:
            fetched = self.current_reward_page(
                limit=next_limit, cursor=next_cursor
            )
            yield fetched
            page_number += 1
            response_cursor = fetched.value.next_cursor
            if response_cursor in (None, "", "LTE="):
                return
            if response_cursor in seen_cursors:
                raise PublicSourceError(
                    "current rewards pagination repeated a cursor",
                    raw=fetched.raw,
                )
            seen_cursors.add(response_cursor)
            next_cursor = response_cursor
            # The service currently overrides small requested limits.  Its
            # response is the authoritative size for the following request.
            next_limit = fetched.value.response_limit

    def book(self, token_id: str) -> Fetched[SourceBook]:
        if not token_id:
            raise ValueError("token_id cannot be empty")
        raw = self._get(
            f"{CLOB_BASE}/book",
            source="clob_book",
            params={"token_id": token_id},
        )
        try:
            value = _book(_decode_json(raw), expected_token_id=token_id)
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=value)

    def books(
        self,
        token_ids: Sequence[str],
        *,
        allow_public_batch: bool = False,
    ) -> Fetched[tuple[SourceBook, ...]]:
        """Fetch public books in one POST only after an explicit opt-in."""
        if not allow_public_batch:
            raise PublicSourceError(
                "public /books POST requires allow_public_batch=True"
            )
        normalized = [str(token_id) for token_id in token_ids]
        if not normalized or any(not token_id for token_id in normalized):
            raise ValueError("token_ids must contain non-empty values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("token_ids cannot contain duplicates")
        payload = [{"token_id": token_id} for token_id in normalized]
        raw = self._request(
            "POST",
            f"{CLOB_BASE}/books",
            source="clob_books",
            payload=payload,
            allow_public_batch=True,
        )
        try:
            decoded = _decode_json(raw)
            if not isinstance(decoded, list):
                raise PublicSourceError(
                    "CLOB books response must be an array"
                )
            books = tuple(_book(item) for item in decoded)
            response_ids = [book.token_id for book in books]
            if len(set(response_ids)) != len(response_ids):
                raise PublicSourceError(
                    "CLOB books response contains duplicate asset_id"
                )
            if set(response_ids) != set(normalized):
                raise PublicSourceError(
                    "CLOB books response does not match requested tokens"
                )
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=books)

    def gamma_market(
        self, market_id: str
    ) -> Fetched[Mapping[str, Any]]:
        if not market_id:
            raise ValueError("market_id cannot be empty")
        raw = self._get(
            f"{GAMMA_BASE}/markets/{quote(market_id, safe='')}",
            source="gamma",
        )
        try:
            market = _gamma_item(_decode_json(raw), kind="markets")
            if str(market["id"]) != market_id:
                raise PublicSourceError(
                    "Gamma market id does not match request"
                )
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=market)

    def data_trades(
        self,
        condition_id: str,
        *,
        taker_only: bool = True,
        limit: int = 1_000,
        offset: int = 0,
    ) -> Fetched[tuple[Mapping[str, Any], ...]]:
        """Fetch public Data API trades with authoritative taker direction."""

        if not condition_id:
            raise ValueError("condition_id cannot be empty")
        if taker_only is not True:
            raise ValueError(
                "data_trades requires taker_only=True for side semantics"
            )
        requested_limit = min(max(int(limit), 1), 1_000)
        requested_offset = max(int(offset), 0)
        raw = self._get(
            f"{DATA_BASE}/trades",
            source="data_api_trades",
            params={
                "market": condition_id,
                "takerOnly": "true",
                "limit": requested_limit,
                "offset": requested_offset,
            },
        )
        try:
            decoded = _decode_json(raw)
            if not isinstance(decoded, list):
                raise PublicSourceError(
                    "Data API trades response must be an array"
                )
            rows: list[Mapping[str, Any]] = []
            for index, value in enumerate(decoded):
                trade = _required(
                    _mapping(
                        value,
                        context=f"Data API trade[{index}]",
                    ),
                    (
                        "conditionId",
                        "asset",
                        "side",
                        "size",
                        "price",
                        "timestamp",
                        "transactionHash",
                        "proxyWallet",
                        "outcomeIndex",
                    ),
                    context=f"Data API trade[{index}]",
                )
                if str(trade["conditionId"]).lower() != condition_id.lower():
                    raise PublicSourceError(
                        "Data API trade condition does not match request"
                    )
                if trade["side"] not in {"BUY", "SELL"}:
                    raise PublicSourceError(
                        "Data API trade side must be BUY or SELL"
                    )
                if not str(trade["asset"]).strip():
                    raise PublicSourceError(
                        "Data API trade asset must be non-empty"
                    )
                if _decimal(
                    trade["size"],
                    context=f"Data API trade[{index}] size",
                ) <= 0:
                    raise PublicSourceError(
                        "Data API trade size must be positive"
                    )
                price = _decimal(
                    trade["price"],
                    context=f"Data API trade[{index}] price",
                )
                if not Decimal("0") < price < Decimal("1"):
                    raise PublicSourceError(
                        "Data API trade price must be between zero and one"
                    )
                if _integer(
                    trade["timestamp"],
                    context=f"Data API trade[{index}] timestamp",
                ) < 0:
                    raise PublicSourceError(
                        "Data API trade timestamp cannot be negative"
                    )
                transaction_hash = str(trade["transactionHash"])
                if re.fullmatch(
                    r"0x[0-9a-fA-F]{64}",
                    transaction_hash,
                ) is None:
                    raise PublicSourceError(
                        "Data API trade transactionHash is invalid"
                    )
                try:
                    data_api_trade_event_key(trade)
                except ValueError as exc:
                    raise PublicSourceError(
                        f"Data API trade identity is invalid: {exc}"
                    ) from exc
                rows.append(MappingProxyType(dict(trade)))
            result = tuple(rows)
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=result)

    def resolution_rules(
        self, market_id: str
    ) -> Fetched[ResolutionRules]:
        """Fetch the complete rule-bearing Gamma market record."""
        fetched = self.gamma_market(market_id)
        raw = fetched.raw
        try:
            market = _required(
                fetched.value,
                (
                    "description",
                    "resolutionSource",
                    "endDate",
                    "closed",
                ),
                context="Gamma resolution market",
            )
            description = _nonempty_text(
                market["description"],
                context="Gamma resolution market description",
            )
            rules_value = market.get("rules")
            rules_text = (
                description
                if rules_value in (None, "")
                else _nonempty_text(
                    rules_value,
                    context="Gamma resolution market rules",
                )
            )
            value = ResolutionRules(
                market_id=_nonempty_text(
                    market["id"], context="Gamma resolution market id"
                ),
                condition_id=_nonempty_text(
                    market["conditionId"],
                    context="Gamma resolution market conditionId",
                ),
                question_id=(
                    None
                    if market.get("questionID") in (None, "")
                    else _nonempty_text(
                        market["questionID"],
                        context="Gamma resolution market questionID",
                    )
                ),
                question=_nonempty_text(
                    market["question"],
                    context="Gamma resolution market question",
                ),
                description=description,
                rules_text=rules_text,
                resolution_source=_nonempty_text(
                    market["resolutionSource"],
                    context="Gamma resolution market resolutionSource",
                ),
                end_date=_nonempty_text(
                    market["endDate"],
                    context="Gamma resolution market endDate",
                ),
                resolved_by=(
                    None
                    if market.get("resolvedBy") in (None, "")
                    else _nonempty_text(
                        market["resolvedBy"],
                        context="Gamma resolution market resolvedBy",
                    )
                ),
                closed=_boolean(
                    market["closed"],
                    context="Gamma resolution market closed",
                ),
                uma_resolution_status=(
                    None
                    if market.get("umaResolutionStatus") in (None, "")
                    else _nonempty_text(
                        market["umaResolutionStatus"],
                        context=(
                            "Gamma resolution market umaResolutionStatus"
                        ),
                    )
                ),
                raw=MappingProxyType(market),
            )
        except PublicSourceError as exc:
            if exc.raw is not None:
                raise
            raise PublicSourceError(str(exc), raw=raw) from exc
        return Fetched(raw=raw, value=value)
