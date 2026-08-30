from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.profit_system.execution import (
    ExecutionMode,
    OrderLifecycleStatus,
    OrderSide,
    QualificationLevel,
    QualificationRecord,
    RiskContext,
    RiskLimits,
    TimeInForce,
    VenueOrderSpec,
    VenuePolicyError,
    VenuePreflightRequest,
)
from src.profit_system.execution.adapters import PolymarketLiveVenueAdapter


def _limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal("100"),
        max_market_exposure=Decimal("100"),
        max_event_exposure=Decimal("100"),
        max_strategy_exposure=Decimal("100"),
        max_total_exposure=Decimal("100"),
        max_daily_loss=Decimal("20"),
        max_drawdown=Decimal("20"),
        max_open_orders=10,
        max_unmatched_leg_seconds=30,
        max_market_data_age_ms=500,
        max_user_stream_gap_seconds=30,
        max_order_rate=5,
    )


def _qualification() -> QualificationRecord:
    return QualificationRecord(
        strategy_id="strat-1",
        qualification=QualificationLevel.PROBE_ALLOWED,
        allowed_modes=(ExecutionMode.LIVE_CANARY,),
        market_whitelist=("market-1",),
        risk_limits=_limits(),
        evidence_digest="digest-1",
    )


def _order(client_order_id: str = "client-1") -> VenueOrderSpec:
    return VenueOrderSpec(
        client_order_id=client_order_id,
        strategy_id="strat-1",
        market_id="market-1",
        token_id="token-1",
        event_id="event-1",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("0.40"),
        time_in_force=TimeInForce.GTD,
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=5),
    )


def _request() -> VenuePreflightRequest:
    return VenuePreflightRequest(
        mode=ExecutionMode.LIVE_CANARY,
        qualification=_qualification(),
        orders=(_order(),),
        risk_context=RiskContext(market_data_age_ms=100),
        idempotency_key="preflight-1",
        now=datetime.now(tz=UTC),
    )


@dataclass
class _Allowance:
    balance: int
    allowances: dict[str, int]


class _AsyncList:
    def __init__(self, items: tuple[object, ...] = ()) -> None:
        self._items = items

    def __aiter__(self):
        async def iterate():
            for item in self._items:
                yield item

        return iterate()

    def iter_items(self):
        return self.__aiter__()


class _HttpFailure(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        retry_after: str | None = None,
    ) -> None:
        headers = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        self.response = type(
            "Response",
            (),
            {
                "status_code": status_code,
                "status": status_code,
                "headers": headers,
            },
        )()
        super().__init__(f"http {status_code}")


def _market() -> SimpleNamespace:
    return SimpleNamespace(
        id="gamma-market-1",
        condition_id="market-1",
        state=SimpleNamespace(
            active=True,
            closed=False,
            archived=False,
            accepting_orders=True,
            enable_order_book=True,
            neg_risk=False,
        ),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(token_id="token-1"),
            no=SimpleNamespace(token_id="token-2"),
        ),
        trading=SimpleNamespace(
            minimum_order_size=Decimal("1"),
            minimum_tick_size=Decimal("0.01"),
            fees_enabled=True,
            fee_schedule=SimpleNamespace(
                rate=Decimal("0.35"),
                exponent=Decimal("1"),
                taker_only=True,
            ),
        ),
    )


class _SubmitFailureClient:
    def __init__(self, failure: _HttpFailure) -> None:
        self._failure = failure
        self.wallet = "0xwallet"

    async def get_closed_only_mode(self) -> bool:
        return False

    async def get_balance_allowance(
        self,
        *,
        asset_type: str,
        token_id: str | None = None,
    ) -> _Allowance:
        del asset_type, token_id
        return _Allowance(
            balance=25_000_000,
            allowances={"0xexchange-a": 25_000_000, "0xexchange-b": 25_000_000},
        )

    def list_markets(self, *, condition_ids, page_size: int):
        del condition_ids, page_size
        return _AsyncList((_market(),))

    async def get_order_book(self, *, token_id: str):
        return SimpleNamespace(
            market="market-1",
            condition_id="market-1",
            token_id=token_id,
            timestamp=datetime.now(tz=UTC),
            bids=(),
            asks=(),
            min_order_size=Decimal("1"),
            tick_size=Decimal("0.01"),
            neg_risk=False,
        )

    async def create_limit_order(self, **kwargs):
        return {"signed": kwargs}

    async def post_order(self, signed_order):
        del signed_order
        raise self._failure

    async def list_open_orders(self):
        return _AsyncList()

    async def list_account_trades(self, *, after=None):
        del after
        return _AsyncList()

    async def subscribe(self, spec):
        del spec
        return _AsyncList()


class _CancelFailureClient(_SubmitFailureClient):
    async def cancel_order(self, *, order_id: str):
        del order_id
        raise self._failure

    async def cancel_orders(self, *, order_ids):
        del order_ids
        raise self._failure

    async def cancel_all(self):
        raise self._failure


class _SecondSigningFailureClient(_SubmitFailureClient):
    def __init__(self) -> None:
        super().__init__(_HttpFailure(status_code=503))
        self.signing_calls = 0

    async def create_limit_order(self, **kwargs):
        self.signing_calls += 1
        if self.signing_calls > 1:
            raise RuntimeError("local signing failed")
        return {"signed": kwargs}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "retry_after", "expected_retry_after"),
    [
        (425, "3", 3),
        (429, "7", 7),
        (429, "soon", None),
    ],
)
async def test_submit_surfaces_safe_retry_http_failures_as_policy_errors(
    status_code: int,
    retry_after: str | None,
    expected_retry_after: int | None,
) -> None:
    client = _SubmitFailureClient(_HttpFailure(status_code=status_code, retry_after=retry_after))
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())

    with pytest.raises(VenuePolicyError) as exc:
        await adapter.submit(
            orders=(_order(),),
            mode=ExecutionMode.LIVE_CANARY,
            idempotency_key=f"submit-{status_code}-{retry_after or 'missing'}",
        )

    assert exc.value.retry_after_seconds == expected_retry_after


@pytest.mark.asyncio
async def test_submit_503_is_ambiguous_and_cached_instead_of_retryable() -> None:
    client = _SubmitFailureClient(_HttpFailure(status_code=503))
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())

    first = await adapter.submit(
        orders=(_order(),),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-503",
    )
    second = await adapter.submit(
        orders=(_order(),),
        mode=ExecutionMode.LIVE_CANARY,
        idempotency_key="submit-503",
    )

    assert first.results[0].status is OrderLifecycleStatus.AMBIGUOUS
    assert second.results[0].status is OrderLifecycleStatus.AMBIGUOUS


@pytest.mark.asyncio
async def test_submit_does_not_misclassify_local_signing_failure_as_ambiguous() -> None:
    client = _SecondSigningFailureClient()
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())

    with pytest.raises(
        VenuePolicyError,
        match="venue order signing failed before submission",
    ):
        await adapter.submit(
            orders=(_order(),),
            mode=ExecutionMode.LIVE_CANARY,
            idempotency_key="submit-signing-failure",
        )


@pytest.mark.asyncio
async def test_submit_does_not_misclassify_definitive_http_rejection_as_ambiguous() -> None:
    client = _SubmitFailureClient(_HttpFailure(status_code=400))
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())

    with pytest.raises(_HttpFailure, match="http 400"):
        await adapter.submit(
            orders=(_order(),),
            mode=ExecutionMode.LIVE_CANARY,
            idempotency_key="submit-400",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "retry_after", "expected_retry_after"),
    [
        (425, "2", 2),
        (429, "5", 5),
        (429, "later", None),
    ],
)
async def test_cancel_surfaces_safe_retry_http_failures_as_policy_errors(
    status_code: int,
    retry_after: str | None,
    expected_retry_after: int | None,
) -> None:
    client = _CancelFailureClient(_HttpFailure(status_code=status_code, retry_after=retry_after))
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())

    with pytest.raises(VenuePolicyError) as exc:
        await adapter.cancel(
            order_ids=("venue-1",),
            idempotency_key=f"cancel-{status_code}-{retry_after or 'missing'}",
            reason="fault-drill",
        )

    assert exc.value.retry_after_seconds == expected_retry_after


@pytest.mark.asyncio
async def test_cancel_503_returns_unresolved_ids_for_explicit_targets() -> None:
    client = _CancelFailureClient(_HttpFailure(status_code=503))
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())
    result = await adapter.cancel(
        order_ids=("venue-1",),
        idempotency_key="cancel-503-one",
        reason="fault-drill",
    )

    assert result.canceled_ids == ()
    assert result.unresolved_ids == ("venue-1",)


@pytest.mark.asyncio
async def test_cancel_all_503_uses_outcome_unknown_sentinel_without_live_ids() -> None:
    client = _CancelFailureClient(_HttpFailure(status_code=503))
    adapter = PolymarketLiveVenueAdapter(client_factory=lambda: client)

    await adapter.preflight(_request())
    result = await adapter.cancel(
        order_ids=(),
        idempotency_key="cancel-503-all",
        reason="fault-drill",
    )

    assert result.canceled_ids == ()
    assert result.unresolved_ids == ("cancel-all:outcome-unknown",)
