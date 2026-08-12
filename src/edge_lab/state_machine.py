"""Fail-closed state machine for shadow-testing complete-set quote cycles.

This module is execution-agnostic.  It codifies inventory and one-leg risk
invariants so a future CLOB V2 adapter cannot treat two independent orders as
an atomic basket.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Optional


class PairMode(str, Enum):
    PAIRED_BIDS = "paired_bids"
    PAIRED_ASKS_FROM_SPLIT = "paired_asks_from_split"


class CycleState(str, Enum):
    IDLE = "idle"
    BOTH_QUOTES_LIVE = "both_quotes_live"
    ONE_LEG_FILLED = "one_leg_filled"
    HEDGE_REQUIRED = "hedge_required"
    PAIR_COMPLETE = "pair_complete"
    MERGE_PENDING = "merge_pending"
    CANCEL_PENDING = "cancel_pending"
    RECONCILING = "reconciling"
    SAFE_STOP = "safe_stop"
    FLAT = "flat"


class StateTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class PairRiskLimits:
    max_collateral: Decimal
    max_single_leg_shares: Decimal
    max_single_leg_seconds: Decimal
    max_emergency_hedge_loss: Decimal


@dataclass
class PairCycle:
    condition_id: str
    mode: PairMode
    target_size: Decimal
    collateral_required: Decimal
    limits: PairRiskLimits
    state: CycleState = CycleState.IDLE
    first_filled: Decimal = Decimal("0")
    second_filled: Decimal = Decimal("0")
    one_leg_started_ms: Optional[int] = None
    stop_reason: Optional[str] = None

    @property
    def directional_exposure(self) -> Decimal:
        return abs(self.first_filled - self.second_filled)

    def _safe_stop(self, reason: str) -> None:
        self.state = CycleState.SAFE_STOP
        self.stop_reason = reason

    def activate(
        self,
        *,
        preflight: Mapping[str, Any],
        split_inventory_ready: bool,
        now_ms: int,
    ) -> None:
        if self.state != CycleState.IDLE:
            raise StateTransitionError(f"cannot activate from {self.state.value}")
        if not preflight.get("production_quote_eligible", False):
            self._safe_stop("preflight_failed")
            return
        if self.collateral_required > self.limits.max_collateral:
            self._safe_stop("collateral_limit")
            return
        if (
            self.mode == PairMode.PAIRED_ASKS_FROM_SPLIT
            and not split_inventory_ready
        ):
            self._safe_stop("split_inventory_missing")
            return
        self.state = CycleState.BOTH_QUOTES_LIVE

    def record_fill(self, leg: str, size: Decimal, *, now_ms: int) -> None:
        if self.state not in (
            CycleState.BOTH_QUOTES_LIVE,
            CycleState.ONE_LEG_FILLED,
        ):
            raise StateTransitionError(f"cannot record fill from {self.state.value}")
        if leg not in ("first", "second"):
            raise ValueError("leg must be 'first' or 'second'")
        if size <= 0:
            raise ValueError("fill size must be positive")

        if leg == "first":
            self.first_filled += size
        else:
            self.second_filled += size
        if (
            self.first_filled > self.target_size
            or self.second_filled > self.target_size
        ):
            self._safe_stop("overfill")
            return

        exposure = self.directional_exposure
        if exposure > self.limits.max_single_leg_shares:
            self._safe_stop("single_leg_share_limit")
            return
        if exposure == 0:
            self.one_leg_started_ms = None
            self.state = (
                CycleState.PAIR_COMPLETE
                if self.first_filled == self.target_size
                else CycleState.BOTH_QUOTES_LIVE
            )
        else:
            if self.one_leg_started_ms is None:
                self.one_leg_started_ms = now_ms
            self.state = CycleState.ONE_LEG_FILLED

    def check_timeout(self, *, now_ms: int) -> None:
        if (
            self.state != CycleState.ONE_LEG_FILLED
            or self.one_leg_started_ms is None
        ):
            return
        elapsed = Decimal(now_ms - self.one_leg_started_ms) / Decimal("1000")
        if elapsed >= self.limits.max_single_leg_seconds:
            self.state = CycleState.HEDGE_REQUIRED

    def approve_emergency_hedge(self, projected_loss: Decimal) -> bool:
        if self.state not in (
            CycleState.ONE_LEG_FILLED,
            CycleState.HEDGE_REQUIRED,
        ):
            raise StateTransitionError(
                f"cannot approve hedge from {self.state.value}"
            )
        if projected_loss > self.limits.max_emergency_hedge_loss:
            self._safe_stop("emergency_hedge_loss_limit")
            return False
        self.state = CycleState.RECONCILING
        return True

    def request_cancel(self) -> None:
        if self.state in (CycleState.FLAT, CycleState.SAFE_STOP):
            return
        self.state = CycleState.CANCEL_PENDING

    def mark_pair_settled(self) -> None:
        if self.state != CycleState.PAIR_COMPLETE:
            raise StateTransitionError(
                f"cannot settle pair from {self.state.value}"
            )
        if self.mode == PairMode.PAIRED_BIDS:
            self.state = CycleState.MERGE_PENDING
        else:
            self.state = CycleState.FLAT

    def mark_merge_confirmed(self) -> None:
        if self.state != CycleState.MERGE_PENDING:
            raise StateTransitionError(
                f"cannot confirm merge from {self.state.value}"
            )
        self.state = CycleState.FLAT

    def mark_reconciled_flat(self) -> None:
        if self.state not in (
            CycleState.RECONCILING,
            CycleState.CANCEL_PENDING,
            CycleState.SAFE_STOP,
        ):
            raise StateTransitionError(
                f"cannot reconcile flat from {self.state.value}"
            )
        self.first_filled = Decimal("0")
        self.second_filled = Decimal("0")
        self.one_leg_started_ms = None
        self.state = CycleState.FLAT
