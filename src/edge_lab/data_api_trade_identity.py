"""Canonical identity for one public Data API matched-trade row."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from .data_store import canonical_json_bytes


_TRANSACTION = re.compile(r"^0x[0-9a-fA-F]{64}$")
_WALLET = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _decimal_text(value: Any, *, label: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a positive decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be a positive decimal")
    return format(parsed.normalize(), "f")


def data_api_trade_event_key(trade: Mapping[str, Any]) -> str:
    """Return a stable composite key for one documented taker-only match.

    One transaction can contain several match rows, so transaction hash alone
    is insufficient. The same match also appears in repeated polling
    snapshots, so snapshot record ID must not enter this identity.
    """

    condition_id = trade.get("conditionId")
    asset = trade.get("asset")
    side = trade.get("side")
    transaction_hash = trade.get("transactionHash")
    proxy_wallet = trade.get("proxyWallet")
    outcome_index = trade.get("outcomeIndex")
    timestamp = trade.get("timestamp")
    if not isinstance(condition_id, str) or not condition_id:
        raise ValueError("conditionId must be a non-empty string")
    if not isinstance(asset, str) or not asset:
        raise ValueError("asset must be a non-empty string")
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if (
        not isinstance(transaction_hash, str)
        or _TRANSACTION.fullmatch(transaction_hash) is None
    ):
        raise ValueError("transactionHash must be a 32-byte hex hash")
    if (
        not isinstance(proxy_wallet, str)
        or _WALLET.fullmatch(proxy_wallet) is None
    ):
        raise ValueError("proxyWallet must be a 20-byte hex address")
    if (
        isinstance(outcome_index, bool)
        or not isinstance(outcome_index, int)
        or outcome_index < 0
    ):
        raise ValueError("outcomeIndex must be a non-negative integer")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (str, int))
        or not str(timestamp).isdigit()
    ):
        raise ValueError("timestamp must be Unix seconds")
    identity = {
        "schema_version": "edge-lab.data-api-trade-event-key.v1",
        "condition_id": condition_id.lower(),
        "asset": asset,
        "taker_side": side,
        "size": _decimal_text(trade.get("size"), label="size"),
        "price": _decimal_text(trade.get("price"), label="price"),
        "timestamp_seconds": int(str(timestamp)),
        "transaction_hash": transaction_hash.lower(),
        "proxy_wallet": proxy_wallet.lower(),
        "outcome_index": outcome_index,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


__all__ = ["data_api_trade_event_key"]
