#!/usr/bin/env python3
"""Read-only probe: capture real ClobTrade/MakerOrder payload shape.

Everything src/orders.py assumes about account-trade semantics today --
that ClobTrade.side is the taker's direction, that maker_address/owner
reliably identify this account's own maker leg(s), what fee_rate_bps
means, whether one taker trade can carry more than one of this account's
maker legs -- is inference from public docs and fixtures, never a real
authenticated payload. This script exists to close that gap. It is the
one thing in the live-readiness plan that a human with credentials can
actually advance; nothing here changes any code path (see src/orders.py
for the code under test), and this script must NOT be run by an
automated agent -- only by a human who has read the precautions below.

SAFETY
------
- Read-only. Never places, cancels, or amends an order.
- Requires POLY_PRIVATE_KEY (and optionally POLY_FUNDER / the
  POLY_API_KEY+POLY_API_SECRET+POLY_PASSPHRASE triple), exactly like the
  rest of this repo's live path. Refuses to run without them, printing
  what is missing instead of raising a traceback.
- Persists a REDACTED capture to disk: every maker_address/owner is
  truncated to its first 6 and last 4 characters. Nothing else about the
  account (API keys, the private key, raw wallet addresses) is written.

BEFORE YOU RUN THIS AGAINST A REAL ACCOUNT
-------------------------------------------
- Use an isolated wallet, not a wallet holding funds you would not want
  correlated with a still-partially-identifying (truncated but not
  anonymous) capture file.
- Fund it with the minimum size Polymarket allows.
- This script does not place orders, so it is safe to run against an
  account with no open orders -- it will simply report zero maker
  trades and the four questions below will come back "no evidence yet."
  To capture a fresh maker fill, place one small resting order yourself
  in another window/session, wait for it to (partially) fill, then run
  this script, then immediately cancel any remainder. Do not leave
  orders open longer than necessary to get one fill.

USAGE
-----
    python scripts/probe_account_trade_semantics.py TOKEN_ID [--out DIR] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.auth import (  # noqa: E402
    PolymarketAdapterError,
    get_conditional_balance,
    require_sdk_field,
    require_sdk_iter_items,
)
from src.client import get_auth_client  # noqa: E402
from src.config import has_credentials  # noqa: E402
from src.orders import _normalize_account_trade  # noqa: E402

DEFAULT_OUT_DIR = PROJECT_ROOT / "logs" / "probes"


def _redact_address(value: object) -> str:
    """Truncate an address/identifier to its first 6 and last 4 characters."""
    text = str(value)
    if len(text) <= 10:
        return text
    return f"{text[:6]}...{text[-4:]}"


def _decimal_text(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _redact_maker_order(maker_order: object) -> dict[str, Any]:
    return {
        "order_id": str(require_sdk_field(maker_order, "order_id")),
        "side": str(require_sdk_field(maker_order, "side")),
        "price": _decimal_text(require_sdk_field(maker_order, "price")),
        "matched_amount": _decimal_text(require_sdk_field(maker_order, "matched_amount")),
        "fee_rate_bps": _decimal_text(require_sdk_field(maker_order, "fee_rate_bps")),
        "maker_address": _redact_address(require_sdk_field(maker_order, "maker_address")),
        "owner": _redact_address(require_sdk_field(maker_order, "owner")),
    }


def _timestamp_text(value: object) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _redact_trade(item: object) -> dict[str, Any]:
    """Sanitize one raw ClobTrade for on-disk persistence."""
    return {
        "id": str(require_sdk_field(item, "id")),
        "trader_side": str(require_sdk_field(item, "trader_side")),
        "side": str(require_sdk_field(item, "side")),
        "status": str(require_sdk_field(item, "status")),
        "price": _decimal_text(require_sdk_field(item, "price")),
        "size": _decimal_text(require_sdk_field(item, "size")),
        "fee_rate_bps": _decimal_text(require_sdk_field(item, "fee_rate_bps")),
        "matched_at": _timestamp_text(require_sdk_field(item, "matched_at")),
        "maker_address": _redact_address(require_sdk_field(item, "maker_address")),
        "owner": _redact_address(require_sdk_field(item, "owner")),
        "maker_orders": [
            _redact_maker_order(maker_order)
            for maker_order in require_sdk_field(item, "maker_orders")
        ],
    }


@dataclass
class TradeFinding:
    trade_id: str
    trader_side: str
    top_level_side: str
    status: str
    normalize_ok: bool
    normalize_error: Optional[str]
    owned_leg_sides: tuple[str, ...]
    predicted_delta: Optional[Decimal]
    fee_rate_bps: Optional[Decimal]
    price: Optional[Decimal]

    @property
    def owned_leg_count(self) -> int:
        return len(self.owned_leg_sides)


def _analyze_trade(item: object) -> TradeFinding:
    """Run the account trade through this repo's real normalization code.

    This deliberately calls src.orders._normalize_account_trade -- the
    exact function LIVE trading depends on -- rather than re-deriving the
    logic here, so this probe is checking the real code path, not a
    second guess at it.
    """
    trade_id = str(require_sdk_field(item, "id"))
    trader_side = str(require_sdk_field(item, "trader_side"))
    top_level_side = str(require_sdk_field(item, "side"))
    status = str(require_sdk_field(item, "status"))
    raw_fee_rate_bps = require_sdk_field(item, "fee_rate_bps")
    fee_rate_bps = Decimal(str(raw_fee_rate_bps)) if raw_fee_rate_bps is not None else None
    price = Decimal(str(require_sdk_field(item, "price")))

    try:
        normalized = _normalize_account_trade(item)
    except PolymarketAdapterError as error:
        return TradeFinding(
            trade_id=trade_id,
            trader_side=trader_side,
            top_level_side=top_level_side,
            status=status,
            normalize_ok=False,
            normalize_error=str(error),
            owned_leg_sides=(),
            predicted_delta=None,
            fee_rate_bps=fee_rate_bps,
            price=price,
        )

    owned_leg_sides = tuple(trade.side.value for trade in normalized)
    predicted_delta = sum(
        (trade.size if trade.side.value == "BUY" else -trade.size for trade in normalized),
        Decimal("0"),
    )
    return TradeFinding(
        trade_id=trade_id,
        trader_side=trader_side,
        top_level_side=top_level_side,
        status=status,
        normalize_ok=True,
        normalize_error=None,
        owned_leg_sides=owned_leg_sides,
        predicted_delta=predicted_delta,
        fee_rate_bps=fee_rate_bps,
        price=price,
    )


@dataclass
class ProbeResult:
    token_id: str
    generated_at: str
    trade_count: int
    findings: list[TradeFinding]
    redacted_trades: list[dict[str, Any]]
    predicted_cumulative_delta: Decimal
    actual_balance: Optional[Decimal]
    balance_error: Optional[str]
    open_order_count: Optional[int]
    open_order_error: Optional[str]


def capture(
    token_id: str,
    *,
    client: Any = None,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> ProbeResult:
    """Fetch and analyze this account's raw trade/order history for one token."""
    active_client = client if client is not None else get_auth_client()

    raw_trades = list(
        require_sdk_iter_items(active_client.list_account_trades(token_id=token_id))
    )
    if limit is not None:
        raw_trades = raw_trades[:limit]

    findings = [_analyze_trade(item) for item in raw_trades]
    redacted_trades = [_redact_trade(item) for item in raw_trades]

    predicted_cumulative_delta = sum(
        (finding.predicted_delta for finding in findings if finding.predicted_delta is not None),
        Decimal("0"),
    )

    try:
        balance_state = get_conditional_balance(token_id, raise_on_error=True)
        actual_balance: Optional[Decimal] = Decimal(str(balance_state["balance"]))
        balance_error = None
    except Exception as error:  # noqa: BLE001 - this is a best-effort cross-check
        actual_balance = None
        balance_error = str(error)

    try:
        open_order_count: Optional[int] = len(
            list(require_sdk_iter_items(active_client.list_open_orders(token_id=token_id)))
        )
        open_order_error = None
    except Exception as error:  # noqa: BLE001 - auxiliary context, not the point of this probe
        open_order_count = None
        open_order_error = str(error)

    generated_at = (now or datetime.now(timezone.utc)).isoformat()

    return ProbeResult(
        token_id=token_id,
        generated_at=generated_at,
        trade_count=len(raw_trades),
        findings=findings,
        redacted_trades=redacted_trades,
        predicted_cumulative_delta=predicted_cumulative_delta,
        actual_balance=actual_balance,
        balance_error=balance_error,
        open_order_count=open_order_count,
        open_order_error=open_order_error,
    )


def _answer_questions(findings: list[TradeFinding]) -> dict[str, str]:
    maker_findings = [f for f in findings if f.trader_side == "MAKER"]
    resolved_maker_findings = [f for f in maker_findings if f.normalize_ok]

    # Q1: is ClobTrade.side the taker's direction when this account is maker?
    if resolved_maker_findings:
        opposite = sum(
            1
            for f in resolved_maker_findings
            if f.owned_leg_sides and f.top_level_side not in f.owned_leg_sides
        )
        same = len(resolved_maker_findings) - opposite
        q1 = (
            f"{len(resolved_maker_findings)} maker trade(s) resolved to an owned "
            f"leg. {opposite} had ClobTrade.side opposite the owned leg's side "
            f"(consistent with .side = the taker's direction); {same} matched it. "
        )
        if opposite and not same:
            q1 += "All evidence so far is consistent with .side = taker direction."
        elif same and not opposite:
            q1 += (
                "All evidence so far is consistent with .side = the maker's own "
                "direction, not the taker's. src.orders never reads top-level "
                ".side for a maker trade (it uses maker_orders[].side instead), "
                "so this would not require a code change, but it contradicts "
                "the assumption named in the question and is worth flagging."
            )
        else:
            q1 += "Mixed results across this sample -- inconclusive; capture more maker fills."
    else:
        q1 = "No maker trade resolved to an owned leg in this capture; cannot answer from this run."

    # Q2: can maker_address/owner reliably identify the account's own maker leg?
    if maker_findings:
        ambiguous = len(maker_findings) - len(resolved_maker_findings)
        q2 = (
            f"{len(resolved_maker_findings)}/{len(maker_findings)} maker trade(s) "
            "resolved to a unique owned leg via maker_address/owner (or, "
            f"trivially, by being the only leg present); {ambiguous} were "
            "ambiguous and src.orders._normalize_account_trade raised "
            "PolymarketAdapterError rather than guessing."
        )
    else:
        q2 = "No maker trades observed in this capture; cannot answer from this run."

    # Q3: is fee_rate_bps r*p(1-p) or r*min(p,1-p)? Not answerable from a listing alone.
    q3 = (
        "Not determinable from this script alone. Answering it needs the actual "
        "USDC fee deducted per trade, which requires a collateral-balance "
        "before/after comparison this script does not attempt (that would need "
        "point-in-time balance history this probe has no access to). Use the "
        "per-trade table's fee_rate_bps/price/size to compute both candidate "
        "formulas by hand and compare against what you observe deducted in the "
        "Polymarket UI or on-chain."
    )

    # Q4: can one taker trade carry multiple of this account's own maker legs?
    multi_leg = [f for f in findings if f.normalize_ok and f.owned_leg_count > 1]
    if multi_leg:
        q4 = (
            f"Yes -- observed {len(multi_leg)} trade(s) with more than one owned "
            f"maker leg (e.g. trade {multi_leg[0].trade_id} with "
            f"{multi_leg[0].owned_leg_count} legs)."
        )
    elif resolved_maker_findings:
        q4 = (
            f"No multi-leg trade observed across {len(resolved_maker_findings)} "
            "resolved maker trade(s) in this capture. Absence of evidence is not "
            "evidence of absence -- src.orders already handles this case "
            "(_owned_maker_orders can return more than one leg), so no code "
            "change is implied either way; this only tells you whether *this* "
            "capture happened to observe it."
        )
    else:
        q4 = "No maker trades observed in this capture; cannot answer from this run."

    return {"q1": q1, "q2": q2, "q3": q3, "q4": q4}


def render_markdown_report(result: ProbeResult) -> str:
    answers = _answer_questions(result.findings)
    lines = [
        f"# Account trade semantics probe -- {result.token_id}",
        "",
        f"- Generated: {result.generated_at}",
        f"- Trades captured: {result.trade_count}",
        (
            f"- Open orders: {result.open_order_count}"
            if result.open_order_error is None
            else f"- Open orders: unavailable ({result.open_order_error})"
        ),
        "",
        "## Wallet-balance cross-check",
        "",
        "Predicted cumulative delta -- sum of +size for BUY / -size for SELL "
        "across every captured trade, computed by this repo's real "
        "`src.orders._normalize_account_trade`:",
        "",
        f"**{result.predicted_cumulative_delta}**",
        "",
    ]
    if result.actual_balance is not None:
        consistent = result.actual_balance == result.predicted_cumulative_delta
        lines += [
            f"Actual current conditional balance: **{result.actual_balance}**",
            "",
            f"Consistent: **{consistent}**",
            "",
            "This check only holds if the wallet had zero balance in this token "
            "before its first trade and nothing besides trades -- no transfers, "
            "splits, merges, or redemptions -- touched the balance in between "
            "(see `src.orders.get_position`'s own docstring for the same caveat). "
            "An inconsistency here does not by itself prove the normalization is "
            "wrong; check for those other balance-moving events first.",
        ]
    else:
        lines.append(
            f"Actual current conditional balance: unavailable ({result.balance_error})"
        )

    lines += [
        "",
        "## Answers",
        "",
        "1. **Is `ClobTrade.side` the taker's direction when the account is maker?**"
        f"\n\n   {answers['q1']}",
        "",
        "2. **Can `maker_address`/`owner` reliably identify the account's own maker leg?**"
        f"\n\n   {answers['q2']}",
        "",
        "3. **Is `fee_rate_bps` `r*p(1-p)` or `r*min(p,1-p)`?**"
        f"\n\n   {answers['q3']}",
        "",
        "4. **Can one taker trade carry multiple of this account's own maker legs?**"
        f"\n\n   {answers['q4']}",
        "",
        "## Per-trade findings",
        "",
        "| trade_id | trader_side | top_level_side | status | normalize_ok | "
        "owned_legs | predicted_delta / error | fee_rate_bps | price |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for finding in result.findings:
        delta_or_error = (
            str(finding.predicted_delta)
            if finding.predicted_delta is not None
            else finding.normalize_error
        )
        lines.append(
            f"| {finding.trade_id} | {finding.trader_side} | "
            f"{finding.top_level_side} | {finding.status} | "
            f"{finding.normalize_ok} | {','.join(finding.owned_leg_sides) or '-'} | "
            f"{delta_or_error} | {finding.fee_rate_bps} | {finding.price} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only probe of real ClobTrade/MakerOrder payload shape.",
    )
    parser.add_argument("token_id", help="Outcome token id to probe")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT_DIR),
        help=f"Output directory for the redacted capture and report (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of trades analyzed (most recent N, after fetch)",
    )
    args = parser.parse_args(argv)

    if not has_credentials():
        print(
            "Missing credentials: this probe needs POLY_PRIVATE_KEY set "
            "(and optionally POLY_FUNDER, or the complete "
            "POLY_API_KEY/POLY_API_SECRET/POLY_PASSPHRASE triple), exactly "
            "like the rest of this repo's live path. This is a read-only "
            "probe, but see the module docstring for the isolated-wallet "
            "and minimal-funds precautions before running it against a "
            "real account.",
            file=sys.stderr,
        )
        return 1

    try:
        result = capture(args.token_id, limit=args.limit)
    except PolymarketAdapterError as error:
        print(f"Adapter contract violation while probing: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - top-level CLI boundary
        print(f"Probe failed: {error}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.generated_at.replace(":", "").replace("-", "").replace(".", "")
    safe_token = "".join(ch if ch.isalnum() else "_" for ch in args.token_id[:16])
    payload_path = out_dir / f"trades_{safe_token}_{stamp}.json"
    report_path = out_dir / f"report_{safe_token}_{stamp}.md"

    payload_path.write_text(
        json.dumps(result.redacted_trades, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    report_path.write_text(render_markdown_report(result), encoding="utf-8")

    print(f"Captured {result.trade_count} trade(s) for {args.token_id}")
    print(f"Redacted payload: {payload_path}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
