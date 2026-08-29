"""Read-only clients for Polymarket's public CLOB, Gamma, and data APIs."""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import quote, urlsplit

import requests

from .models import OrderBook
from .network_safety import (
    PublicNetworkSafetyError,
    configure_public_session,
    discard_session_cookies,
    is_sensitive_parameter_name,
    request_error_code,
    safe_error_details,
)


CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"


class PublicAPIError(RuntimeError):
    """A sanitized public-source failure safe to persist in research output."""

    def __init__(
        self,
        *,
        error_type: str,
        error_code: str,
        status_code: int | None = None,
    ) -> None:
        self.error_type = error_type
        self.error_code = error_code
        self.status_code = status_code
        suffix = "" if status_code is None else f":HTTP_{status_code}"
        super().__init__(f"{error_code}:{error_type}{suffix}")


class PublicPolymarketAPI:
    """Small retrying HTTP client that has no authenticated/write methods."""

    def __init__(
        self,
        *,
        timeout: float = 30,
        retries: int = 2,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        configure_public_session(self.session)
        self.session.headers.update(
            {"User-Agent": "polymarket-edge-lab/0.1 (public-read-only)"}
        )

    def _json(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        payload: Any = None,
    ) -> Any:
        method = method.upper()
        parsed_url = urlsplit(url)
        allowed_hosts = {
            urlsplit(base).hostname for base in (CLOB_BASE, GAMMA_BASE, DATA_BASE)
        }
        is_public_books = (
            method == "POST"
            and url == f"{CLOB_BASE}/books"
        )
        if (
            (method != "GET" and not is_public_books)
            or parsed_url.scheme != "https"
            or parsed_url.hostname not in allowed_hosts
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise PublicAPIError(
                error_type="PublicNetworkSafetyError",
                error_code="public_route_forbidden",
            )
        parameter_names: list[Any]
        if params is None:
            parameter_names = []
        elif isinstance(params, Mapping):
            parameter_names = list(params)
        elif isinstance(params, Sequence) and not isinstance(
            params,
            (str, bytes, bytearray),
        ):
            parameter_names = [
                item[0]
                for item in params
                if isinstance(item, Sequence)
                and not isinstance(item, (str, bytes, bytearray))
                and item
            ]
        else:
            raise PublicAPIError(
                error_type="PublicNetworkSafetyError",
                error_code="invalid_request_parameters",
            )
        if any(
            is_sensitive_parameter_name(name) for name in parameter_names
        ):
            raise PublicAPIError(
                error_type="PublicNetworkSafetyError",
                error_code="sensitive_request_parameter",
            )
        last_error = {
            "error_type": "RequestException",
            "error_code": "request_failure",
        }
        last_status: int | None = None
        for attempt in range(self.retries + 1):
            response: Any = None
            try:
                configure_public_session(self.session)
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except Exception as exc:
                last_status = None
                last_error = safe_error_details(
                    exc,
                    code=(
                        exc.code
                        if isinstance(exc, PublicNetworkSafetyError)
                        else request_error_code(exc)
                    ),
                )
            else:
                discard_session_cookies(self.session)
                last_status = int(response.status_code)
                if 200 <= last_status < 300:
                    try:
                        return response.json()
                    except ValueError as exc:
                        last_error = safe_error_details(
                            exc,
                            code="invalid_json_response",
                        )
                elif 300 <= last_status < 400:
                    last_error = {
                        "error_type": "RedirectResponse",
                        "error_code": "redirect_rejected",
                    }
                else:
                    last_error = {
                        "error_type": "HTTPStatus",
                        "error_code": "http_status_failure",
                    }
            finally:
                discard_session_cookies(self.session)
            if attempt < self.retries:
                time.sleep(0.5 * (2**attempt))
        raise PublicAPIError(
            error_type=last_error["error_type"],
            error_code=last_error["error_code"],
            status_code=last_status,
        )

    def reward_markets(
        self,
        *,
        page_size: int = 500,
        next_cursor: Optional[str] = None,
        order_by: str = "competitiveness",
        position: str = "ASC",
    ) -> Mapping[str, Any]:
        params = {
            "page_size": min(max(page_size, 1), 500),
            "order_by": order_by,
            "position": position,
        }
        if next_cursor:
            params["next_cursor"] = next_cursor
        return self._json("GET", f"{CLOB_BASE}/rewards/markets/multi", params=params)

    def reward_market(self, condition_id: str) -> Mapping[str, Any]:
        return self._json(
            "GET",
            f"{CLOB_BASE}/rewards/markets/{quote(condition_id, safe='')}",
        )

    def clob_market(self, condition_id: str) -> Mapping[str, Any]:
        return self._json(
            "GET",
            f"{CLOB_BASE}/markets/{quote(condition_id, safe='')}",
        )

    def books(self, token_ids: Sequence[str]) -> dict[str, OrderBook]:
        if not token_ids:
            return {}
        results: dict[str, OrderBook] = {}
        for start in range(0, len(token_ids), 500):
            chunk = token_ids[start : start + 500]
            self._load_book_chunk(chunk, results)
        return results

    def _load_book_chunk(
        self,
        token_ids: Sequence[str],
        results: dict[str, OrderBook],
    ) -> None:
        """Split failed public batches so one stale token cannot kill a scan."""
        if not token_ids:
            return
        payload = [{"token_id": token_id} for token_id in token_ids]
        try:
            raw_books = self._json(
                "POST", f"{CLOB_BASE}/books", payload=payload
            )
        except PublicAPIError:
            if len(token_ids) == 1:
                return
            midpoint = len(token_ids) // 2
            self._load_book_chunk(token_ids[:midpoint], results)
            self._load_book_chunk(token_ids[midpoint:], results)
            return
        for raw in raw_books:
            book = OrderBook.from_api(raw)
            results[book.token_id] = book

    def gamma_markets(
        self, condition_ids: Sequence[str]
    ) -> dict[str, Mapping[str, Any]]:
        results: dict[str, Mapping[str, Any]] = {}
        for start in range(0, len(condition_ids), 100):
            chunk = condition_ids[start : start + 100]
            params = [("condition_ids", condition_id) for condition_id in chunk]
            raw_markets = self._json(
                "GET", f"{GAMMA_BASE}/markets", params=params
            )
            for raw in raw_markets:
                condition_id = raw.get("conditionId") or raw.get("condition_id")
                if condition_id:
                    results[str(condition_id)] = raw
        return results

    def gamma_active_markets(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        return self._json(
            "GET",
            f"{GAMMA_BASE}/markets",
            params={
                "active": "true",
                "closed": "false",
                # Gamma currently caps this route at 100 even when a larger
                # value is requested.
                "limit": min(max(limit, 1), 100),
                "offset": max(offset, 0),
            },
        )

    def trades(
        self,
        condition_id: str,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        return self._json(
            "GET",
            f"{DATA_BASE}/trades",
            params={
                "market": condition_id,
                "limit": min(max(limit, 1), 1000),
                "offset": max(offset, 0),
            },
        )

    def orderbook_history(
        self,
        token_id: str,
        *,
        start_ms: int,
        end_ms: int,
        limit: int = 500,
        max_records: int = 10_000,
    ) -> list[OrderBook]:
        """Read the currently working but undocumented history route.

        This endpoint is not in the public API directory/SDK contract.  Callers
        must treat it as opportunistic research data and retain their own
        WebSocket capture for reproducibility.
        """
        records: list[OrderBook] = []
        offset = 0
        page_limit = min(max(limit, 1), 1000)
        while len(records) < max_records:
            raw = self._json(
                "GET",
                f"{CLOB_BASE}/orderbook-history",
                params={
                    "asset_id": token_id,
                    "startTs": start_ms,
                    "endTs": end_ms,
                    "limit": page_limit,
                    "offset": offset,
                },
            )
            data = raw.get("data", [])
            records.extend(OrderBook.from_api(item) for item in data)
            offset += len(data)
            count = int(raw.get("count", len(data)))
            if not data or offset >= count or len(data) < page_limit:
                break
        records.sort(key=lambda book: book.timestamp_ms or 0)
        return records[:max_records]


def reward_daily_rate(raw: Mapping[str, Any]) -> float:
    """Sum currently listed daily reward rates in a reward-market row."""
    total = 0.0
    for config in raw.get("rewards_config", []) or []:
        total += float(config.get("rate_per_day") or 0)
    return total
