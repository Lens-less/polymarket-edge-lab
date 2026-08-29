from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import scripts.probe_account_trade_semantics as probe
from src.auth import PolymarketAdapterError


class _Paginator:
    def __init__(self, items) -> None:
        self._items = tuple(items)

    def iter_items(self):
        yield from self._items


def _maker_order(
    *,
    order_id: str = "maker-order-1",
    side: str = "BUY",
    price: Decimal = Decimal("0.40"),
    matched_amount: Decimal = Decimal(3),
    fee_rate_bps=Decimal(0),
    maker_address: str = "0xabcdef1234567890",
    owner: str = "owner-me-1234567890",
):
    return SimpleNamespace(
        order_id=order_id,
        token_id="token-1",
        maker_address=maker_address,
        owner=owner,
        side=side,
        price=price,
        matched_amount=matched_amount,
        fee_rate_bps=fee_rate_bps,
    )


def _trade(
    trade_id: str,
    *,
    trader_side: str,
    side: str = "BUY",
    status: str = "CONFIRMED",
    price: Decimal = Decimal("0.40"),
    size: Decimal = Decimal(3),
    fee_rate_bps=Decimal(700),
    maker_address: str = "0xabcdef1234567890",
    owner: str = "owner-me-1234567890",
    maker_orders=(),
    matched_at=None,
):
    return SimpleNamespace(
        id=trade_id,
        taker_order_id=f"taker-{trade_id}",
        token_id="token-1",
        side=side,
        trader_side=trader_side,
        price=price,
        size=size,
        fee_rate_bps=fee_rate_bps,
        status=status,
        matched_at=matched_at or datetime(2026, 8, 20, tzinfo=UTC),
        maker_address=maker_address,
        owner=owner,
        maker_orders=tuple(maker_orders),
    )


class _FakeClient:
    def __init__(self, trades=(), open_orders=(), *, raise_on_open_orders=None):
        self._trades = tuple(trades)
        self._open_orders = tuple(open_orders)
        self._raise_on_open_orders = raise_on_open_orders

    def list_account_trades(self, **kwargs):
        return _Paginator(self._trades)

    def list_open_orders(self, **kwargs):
        if self._raise_on_open_orders is not None:
            raise self._raise_on_open_orders
        return _Paginator(self._open_orders)


# --- redaction -------------------------------------------------------------


def test_redact_address_truncates_long_values() -> None:
    assert probe._redact_address("0x1234567890abcdef") == "0x1234...cdef"


def test_redact_address_leaves_short_values_alone() -> None:
    assert probe._redact_address("short") == "short"


# --- capture / analysis -----------------------------------------------------


def test_capture_normalizes_taker_and_maker_trades_and_flags_ambiguous(
    monkeypatch,
) -> None:
    owned_maker_order = _maker_order(matched_amount=Decimal(2))
    ambiguous_maker_orders = (
        _maker_order(order_id="a1", maker_address="0xother1", owner="other-1"),
        _maker_order(order_id="a2", maker_address="0xother2", owner="other-2"),
    )
    trades = (
        _trade("taker-trade", trader_side="TAKER", side="BUY", size=Decimal(5)),
        _trade(
            "maker-trade",
            trader_side="MAKER",
            maker_orders=(owned_maker_order,),
        ),
        _trade(
            "ambiguous-trade",
            trader_side="MAKER",
            maker_orders=ambiguous_maker_orders,
        ),
    )
    client = _FakeClient(trades=trades, open_orders=(SimpleNamespace(),))

    def _fake_balance(token_id, *, raise_on_error=False):
        return {"balance": Decimal("3"), "allowance": Decimal("3"), "sellable": Decimal("3")}

    monkeypatch.setattr(probe, "get_conditional_balance", _fake_balance)
    result = probe.capture("token-1", client=client)

    assert result.trade_count == 3
    by_id = {f.trade_id: f for f in result.findings}

    assert by_id["taker-trade"].normalize_ok is True
    assert by_id["taker-trade"].predicted_delta == Decimal(5)
    assert by_id["taker-trade"].owned_leg_sides == ("BUY",)

    assert by_id["maker-trade"].normalize_ok is True
    assert by_id["maker-trade"].predicted_delta == Decimal(2)

    assert by_id["ambiguous-trade"].normalize_ok is False
    assert "ambiguous-trade" in by_id["ambiguous-trade"].normalize_error
    assert by_id["ambiguous-trade"].predicted_delta is None

    # Ambiguous trade contributes nothing (unknown, not zero-by-assumption).
    assert result.predicted_cumulative_delta == Decimal(7)
    assert result.actual_balance == Decimal("3")
    assert result.open_order_count == 1

    # Redaction: raw long addresses must not appear verbatim.
    payload_text = str(result.redacted_trades)
    assert "0xabcdef1234567890" not in payload_text
    assert "owner-me-1234567890" not in payload_text
    assert "0xabcd...7890" in payload_text or "0xabcd" in payload_text


def test_capture_handles_balance_and_open_order_read_failures_gracefully(
    monkeypatch,
) -> None:
    trades = (_trade("t1", trader_side="TAKER"),)
    client = _FakeClient(trades=trades, raise_on_open_orders=ConnectionError("REST down"))

    def _raising_balance(token_id, *, raise_on_error=False):
        raise ConnectionError("balance REST down")

    monkeypatch.setattr(probe, "get_conditional_balance", _raising_balance)
    result = probe.capture("token-1", client=client)

    assert result.actual_balance is None
    assert "balance REST down" in result.balance_error
    assert result.open_order_count is None
    assert "REST down" in result.open_order_error
    # A secondary-read failure must not prevent the primary trade analysis.
    assert result.trade_count == 1


def test_capture_respects_limit(monkeypatch) -> None:
    trades = tuple(_trade(f"t{i}", trader_side="TAKER") for i in range(5))
    client = _FakeClient(trades=trades)

    def _fake_balance(token_id, *, raise_on_error=False):
        raise ConnectionError("no network in this test")

    monkeypatch.setattr(probe, "get_conditional_balance", _fake_balance)
    result = probe.capture("token-1", client=client, limit=2)

    assert result.trade_count == 2


# --- report rendering --------------------------------------------------------


def test_render_markdown_report_answers_all_four_questions_and_lists_trades() -> None:
    finding_taker = probe.TradeFinding(
        trade_id="t1",
        trader_side="TAKER",
        top_level_side="BUY",
        status="CONFIRMED",
        normalize_ok=True,
        normalize_error=None,
        owned_leg_sides=("BUY",),
        predicted_delta=Decimal(3),
        fee_rate_bps=Decimal("700"),
        price=Decimal("0.40"),
    )
    finding_maker_multi = probe.TradeFinding(
        trade_id="t2",
        trader_side="MAKER",
        top_level_side="SELL",
        status="CONFIRMED",
        normalize_ok=True,
        normalize_error=None,
        owned_leg_sides=("BUY", "BUY"),
        predicted_delta=Decimal(4),
        fee_rate_bps=Decimal("0"),
        price=Decimal("0.41"),
    )
    result = probe.ProbeResult(
        token_id="token-1",
        generated_at="2026-08-21T00:00:00+00:00",
        trade_count=2,
        findings=[finding_taker, finding_maker_multi],
        redacted_trades=[],
        predicted_cumulative_delta=Decimal(7),
        actual_balance=Decimal(7),
        balance_error=None,
        open_order_count=0,
        open_order_error=None,
    )

    report = probe.render_markdown_report(result)

    assert "# Account trade semantics probe -- token-1" in report
    assert "Consistent: **True**" in report
    assert "taker's direction" in report.lower()
    assert "maker_address" in report.lower()
    assert "r*p(1-p)" in report or "r*min(p,1-p)" in report
    assert "Yes -- observed 1 trade(s) with more than one owned maker leg" in report
    assert "| t1 |" in report
    assert "| t2 |" in report


def test_render_markdown_report_flags_inconsistent_balance() -> None:
    result = probe.ProbeResult(
        token_id="token-1",
        generated_at="2026-08-21T00:00:00+00:00",
        trade_count=0,
        findings=[],
        redacted_trades=[],
        predicted_cumulative_delta=Decimal(5),
        actual_balance=Decimal(3),
        balance_error=None,
        open_order_count=0,
        open_order_error=None,
    )
    report = probe.render_markdown_report(result)
    assert "Consistent: **False**" in report


def test_render_markdown_report_handles_missing_balance() -> None:
    result = probe.ProbeResult(
        token_id="token-1",
        generated_at="2026-08-21T00:00:00+00:00",
        trade_count=0,
        findings=[],
        redacted_trades=[],
        predicted_cumulative_delta=Decimal(0),
        actual_balance=None,
        balance_error="boom",
        open_order_count=None,
        open_order_error="boom2",
    )
    report = probe.render_markdown_report(result)
    assert "unavailable (boom)" in report
    assert "unavailable (boom2)" in report


def test_answer_questions_with_no_maker_trades_reports_no_evidence() -> None:
    finding = probe.TradeFinding(
        trade_id="t1",
        trader_side="TAKER",
        top_level_side="BUY",
        status="CONFIRMED",
        normalize_ok=True,
        normalize_error=None,
        owned_leg_sides=("BUY",),
        predicted_delta=Decimal(1),
        fee_rate_bps=Decimal(0),
        price=Decimal("0.5"),
    )
    answers = probe._answer_questions([finding])
    assert "cannot answer from this run" in answers["q1"]
    assert "cannot answer from this run" in answers["q2"]
    assert "cannot answer from this run" in answers["q4"]


# --- CLI ----------------------------------------------------------------


def test_main_without_credentials_prints_guidance_not_a_traceback(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(probe, "has_credentials", lambda: False)

    exit_code = probe.main(["some-token-id"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    assert "POLY_PRIVATE_KEY" in captured.err


def test_main_writes_redacted_payload_and_report_files(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setattr(probe, "has_credentials", lambda: True)

    trades = (
        _trade(
            "t1",
            trader_side="TAKER",
            maker_address="0xdeadbeef00000000",
            owner="owner-full-address-1",
        ),
    )
    client = _FakeClient(trades=trades)
    monkeypatch.setattr(probe, "get_auth_client", lambda: client)

    def _fake_balance(token_id, *, raise_on_error=False):
        return {"balance": Decimal("3"), "allowance": Decimal("3"), "sellable": Decimal("3")}

    monkeypatch.setattr(probe, "get_conditional_balance", _fake_balance)

    exit_code = probe.main(["token-1", "--out", str(tmp_path)])

    assert exit_code == 0
    written = list(tmp_path.iterdir())
    assert any(p.name.startswith("trades_") and p.suffix == ".json" for p in written)
    assert any(p.name.startswith("report_") and p.suffix == ".md" for p in written)

    payload_path = next(p for p in written if p.name.startswith("trades_"))
    payload_text = payload_path.read_text(encoding="utf-8")
    assert "0xdeadbeef00000000" not in payload_text
    assert "owner-full-address-1" not in payload_text

    captured = capsys.readouterr()
    assert "Captured 1 trade(s)" in captured.out


def test_main_reports_adapter_errors_without_a_traceback(monkeypatch, capsys) -> None:
    monkeypatch.setattr(probe, "has_credentials", lambda: True)

    class _BrokenClient:
        def list_account_trades(self, **kwargs):
            raise PolymarketAdapterError("payload contract broken")

    monkeypatch.setattr(probe, "get_auth_client", lambda: _BrokenClient())

    exit_code = probe.main(["token-1"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Traceback" not in captured.err
    assert "payload contract broken" in captured.err
