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
    """Return a stable normalized fingerprint for one documented match row.

    One transaction can contain several match rows, so transaction hash alone
    is insufficient. This fingerprint intentionally excludes snapshot/page/row
    provenance so repeated polling of the same row stays comparable across
    snapshots.
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


def data_api_trade_observation_id(
    trade: Mapping[str, Any],
    *,
    snapshot_id: str,
    page_number: int,
    row_number: int,
) -> str:
    """Return a unique ID for one observed row instance inside a snapshot."""

    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("snapshot_id must be a non-empty string")
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number <= 0
    ):
        raise ValueError("page_number must be a positive integer")
    if (
        isinstance(row_number, bool)
        or not isinstance(row_number, int)
        or row_number <= 0
    ):
        raise ValueError("row_number must be a positive integer")
    identity = {
        "schema_version": "edge-lab.data-api-trade-observation-id.v1",
        "trade_fingerprint": data_api_trade_event_key(trade),
        "snapshot_id": snapshot_id,
        "page_number": page_number,
        "row_number": row_number,
    }
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


__all__ = ["data_api_trade_event_key", "data_api_trade_observation_id"]
