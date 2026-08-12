"""Deterministic lifecycle planning for short-crypto capture workers.

This module is deliberately a pure supervisor: it performs no network I/O,
imports no trading adapter, and cannot create orders.  A ``subscribe`` decision
is the integration seam for launching one independent public CLOB recorder
whose scope is exactly ``asset_ids``.  The worker must use its own initial
full-book snapshot before consuming deltas; concretely, the integration should
construct ``RecorderConfig(clob_asset_ids=decision.asset_ids,
rtds_enabled=False, initial_clob_resnapshot=True)``.  ``unsubscribe`` stops
that whole worker rather than mutating its configured scope.  RTDS remains a
separately shared public stream.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

from .data_store import canonical_json_bytes
from .short_crypto_catalog import ShortCryptoTarget


_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")


class CaptureSupervisorError(RuntimeError):
    """A lifecycle command would violate a frozen capture invariant."""


class TargetLifecycleState(str, Enum):
    """Auditable lifecycle states for a catalog target."""

    ANNOUNCED = "announced"
    SUBSCRIBED = "subscribed"
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    EXCLUDED = "excluded"


class CaptureDecisionAction(str, Enum):
    """Commands emitted for an external public-data worker."""

    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    RESNAPSHOT_REQUIRED = "resnapshot_required"


@dataclass(frozen=True)
class DecisionTargetBinding:
    """Immutable target identity carried by every worker decision."""

    slug: str
    announcement_record_id: str
    rule_hash: str
    up_token_id: str
    down_token_id: str


@dataclass(frozen=True)
class CaptureDecision:
    """Content-addressed worker command suitable for immutable persistence."""

    schema_version: str
    decision_id: str
    sequence: int
    action: CaptureDecisionAction
    group_id: str
    asset_ids: tuple[str, ...]
    targets: tuple[DecisionTargetBinding, ...]
    emitted_at_ms: int
    reason: str
    minimum_snapshot_watermark_ns: int | None = None


@dataclass(frozen=True)
class TargetLifecycleSnapshot:
    """Read-only view of one target's current supervisor state."""

    target: ShortCryptoTarget
    state: TargetLifecycleState
    group_id: str | None
    settlement_record_id: str | None
    exclusion_reason: str | None
    exclusion_record_id: str | None
    excluded_at_ms: int | None


@dataclass(frozen=True)
class CaptureGroupSnapshot:
    """Frozen worker scope plus its connection recovery status."""

    group_id: str
    asset_ids: tuple[str, ...]
    target_slugs: tuple[str, ...]
    active: bool
    resync_required: bool
    minimum_snapshot_watermark_ns: int | None
    last_resnapshot_watermark_ns: int | None


@dataclass
class _TargetEntry:
    target: ShortCryptoTarget
    state: TargetLifecycleState = TargetLifecycleState.ANNOUNCED
    group_id: str | None = None
    settlement_record_id: str | None = None
    exclusion_reason: str | None = None
    exclusion_record_id: str | None = None
    excluded_at_ms: int | None = None


@dataclass
class _GroupEntry:
    group_id: str
    asset_ids: tuple[str, ...]
    target_slugs: tuple[str, ...]
    active: bool = False
    resync_required: bool = False
    minimum_snapshot_watermark_ns: int | None = None
    last_resnapshot_watermark_ns: int | None = None


def _target_sort_key(target: ShortCryptoTarget) -> tuple[int, int, str]:
    return (target.opens_at_ms, target.closes_at_ms, target.slug)


def _binding(target: ShortCryptoTarget) -> DecisionTargetBinding:
    return DecisionTargetBinding(
        slug=target.slug,
        announcement_record_id=target.announcement_record_id,
        rule_hash=target.rule_hash,
        up_token_id=target.up_token_id,
        down_token_id=target.down_token_id,
    )


class DynamicShortCryptoCaptureSupervisor:
    """Plan immutable target-group scopes from strict catalog targets.

    Market windows are treated as half-open intervals.  Targets in the same
    connected overlap component are assigned to frozen worker scopes.  A
    configured asset cap deterministically bounds each scope; without one, a
    15-minute market can group all simultaneous 5-minute markets.  Adjacent
    5-minute markets don't join merely because their boundaries touch.
    """

    def __init__(
        self,
        *,
        subscribe_lead_ms: int,
        max_assets_per_group: int | None = None,
    ) -> None:
        if (
            isinstance(subscribe_lead_ms, bool)
            or not isinstance(subscribe_lead_ms, int)
            or subscribe_lead_ms < 0
        ):
            raise ValueError("subscribe_lead_ms must be a non-negative integer")
        if (
            max_assets_per_group is not None
            and (
                isinstance(max_assets_per_group, bool)
                or not isinstance(max_assets_per_group, int)
                or max_assets_per_group < 2
            )
        ):
            raise ValueError(
                "max_assets_per_group must be None or an integer of at least 2"
            )
        self._subscribe_lead_ms = subscribe_lead_ms
        self._max_targets_per_group = (
            None
            if max_assets_per_group is None
            else max_assets_per_group // 2
        )
        self._targets: dict[str, _TargetEntry] = {}
        self._groups: dict[str, _GroupEntry] = {}
        self._pending_decisions: dict[str, CaptureDecision] = {}
        self._sequence = 0

    def announce(
        self,
        target: ShortCryptoTarget,
    ) -> TargetLifecycleSnapshot:
        """Register one already-validated, content-addressed announcement."""

        if not isinstance(target, ShortCryptoTarget):
            raise TypeError("target must be a ShortCryptoTarget")
        if (
            target.opens_at_ms >= target.closes_at_ms
            or _CONTENT_HASH.fullmatch(target.announcement_record_id) is None
            or _CONTENT_HASH.fullmatch(target.rule_hash) is None
        ):
            raise CaptureSupervisorError("target identity or time window is invalid")
        existing = self._targets.get(target.slug)
        if existing is not None:
            if existing.target != target:
                raise CaptureSupervisorError(
                    f"conflicting announcement for target {target.slug}"
                )
            return self.target_snapshot(target.slug)

        claimed_tokens = {
            token_id
            for entry in self._targets.values()
            for token_id in (
                entry.target.up_token_id,
                entry.target.down_token_id,
            )
        }
        if (
            target.up_token_id == target.down_token_id
            or target.up_token_id in claimed_tokens
            or target.down_token_id in claimed_tokens
        ):
            raise CaptureSupervisorError("target token scope is not unique")

        self._targets[target.slug] = _TargetEntry(target=target)
        return self.target_snapshot(target.slug)

    def restore_decision_sequence(self, sequence: int) -> None:
        """Advance the command sequence to a finalized recovery watermark."""

        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
        ):
            raise ValueError("sequence must be a non-negative integer")
        self._sequence = max(self._sequence, sequence)

    def restore_finalized_terminal(
        self,
        target: ShortCryptoTarget,
        *,
        state: TargetLifecycleState,
        evidence_record_id: str,
        exclusion_reason: str | None,
        observed_at_ms: int,
    ) -> TargetLifecycleSnapshot:
        """Restore a terminal state already proven by finalized evidence.

        Recovery must not replay historical worker transitions merely to reach
        their terminal result: doing so could emit duplicate start/stop
        commands.  This narrow seam accepts only ``SETTLED`` or ``EXCLUDED``
        and remains idempotent for the exact same finalized evidence.
        """

        if state not in {
            TargetLifecycleState.SETTLED,
            TargetLifecycleState.EXCLUDED,
        }:
            raise CaptureSupervisorError(
                "only finalized terminal states may be restored"
            )
        if _CONTENT_HASH.fullmatch(evidence_record_id) is None:
            raise CaptureSupervisorError(
                "evidence_record_id must be a lowercase content hash"
            )
        if (
            isinstance(observed_at_ms, bool)
            or not isinstance(observed_at_ms, int)
            or observed_at_ms < 0
        ):
            raise ValueError("observed_at_ms must be a non-negative integer")
        normalized_reason = (
            None
            if exclusion_reason is None
            else exclusion_reason.strip()
        )
        if state is TargetLifecycleState.SETTLED:
            if normalized_reason is not None:
                raise CaptureSupervisorError(
                    "settled recovery cannot carry an exclusion reason"
                )
            if observed_at_ms < target.closes_at_ms:
                raise CaptureSupervisorError(
                    "settled recovery cannot predate market close"
                )
        elif not normalized_reason:
            raise CaptureSupervisorError(
                "excluded recovery requires a non-empty reason"
            )

        snapshot = self.announce(target)
        entry = self._targets[target.slug]
        if snapshot.state in {
            TargetLifecycleState.SETTLED,
            TargetLifecycleState.EXCLUDED,
        }:
            if (
                snapshot.state is state
                and (
                    state is TargetLifecycleState.SETTLED
                    and snapshot.settlement_record_id
                    == evidence_record_id
                    or state is TargetLifecycleState.EXCLUDED
                    and snapshot.exclusion_reason == normalized_reason
                    and snapshot.exclusion_record_id
                    == evidence_record_id
                    and snapshot.excluded_at_ms == observed_at_ms
                )
            ):
                return snapshot
            raise CaptureSupervisorError(
                f"conflicting terminal recovery for target {target.slug}"
            )
        if entry.group_id is not None:
            raise CaptureSupervisorError(
                "cannot restore terminal state after worker planning"
            )
        entry.state = state
        if state is TargetLifecycleState.SETTLED:
            entry.settlement_record_id = evidence_record_id
        else:
            entry.exclusion_reason = normalized_reason
            entry.exclusion_record_id = evidence_record_id
            entry.excluded_at_ms = observed_at_ms
        return self.target_snapshot(target.slug)

    def target_snapshot(self, slug: str) -> TargetLifecycleSnapshot:
        """Return an immutable lifecycle view."""

        try:
            entry = self._targets[slug]
        except KeyError as exc:
            raise KeyError(f"unknown target: {slug}") from exc
        return TargetLifecycleSnapshot(
            target=entry.target,
            state=entry.state,
            group_id=entry.group_id,
            settlement_record_id=entry.settlement_record_id,
            exclusion_reason=entry.exclusion_reason,
            exclusion_record_id=entry.exclusion_record_id,
            excluded_at_ms=entry.excluded_at_ms,
        )

    def group_snapshot(self, group_id: str) -> CaptureGroupSnapshot:
        """Return a scope copy that external worker orchestration can consume."""

        try:
            group = self._groups[group_id]
        except KeyError as exc:
            raise KeyError(f"unknown capture group: {group_id}") from exc
        return CaptureGroupSnapshot(
            group_id=group.group_id,
            asset_ids=group.asset_ids,
            target_slugs=group.target_slugs,
            active=group.active,
            resync_required=group.resync_required,
            minimum_snapshot_watermark_ns=(
                group.minimum_snapshot_watermark_ns
            ),
            last_resnapshot_watermark_ns=group.last_resnapshot_watermark_ns,
        )

    def advance(self, now_ms: int) -> tuple[CaptureDecision, ...]:
        """Emit newly due target-group subscriptions in deterministic order."""

        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("now_ms must be a non-negative integer")

        for entry in self._targets.values():
            self._advance_entry_state(entry, now_ms=now_ms)
        for group in tuple(self._groups.values()):
            pending_subscriptions = [
                (decision_id, decision)
                for decision_id, decision in self._pending_decisions.items()
                if decision.group_id == group.group_id
                and decision.action is CaptureDecisionAction.SUBSCRIBE
            ]
            if group.active or not pending_subscriptions:
                continue
            for slug in group.target_slugs:
                entry = self._targets[slug]
                if (
                    entry.state is not TargetLifecycleState.ANNOUNCED
                    or now_ms < entry.target.opens_at_ms
                ):
                    continue
                self.exclude(
                    slug,
                    reason="subscription_not_ready_before_open",
                    evidence_record_id=(
                        entry.target.announcement_record_id
                    ),
                    observed_at_ms=now_ms,
                )
            if any(
                self._targets[slug].state
                is TargetLifecycleState.ANNOUNCED
                for slug in group.target_slugs
            ):
                continue
            for decision_id, _ in pending_subscriptions:
                del self._pending_decisions[decision_id]
        for entry in tuple(self._targets.values()):
            if (
                entry.group_id is None
                and entry.state is TargetLifecycleState.ANNOUNCED
                and now_ms >= entry.target.closes_at_ms
            ):
                self.exclude(
                    entry.target.slug,
                    reason="subscription_window_missed",
                    evidence_record_id=(
                        entry.target.announcement_record_id
                    ),
                    observed_at_ms=now_ms,
                )

        unassigned = sorted(
            (
                entry.target
                for entry in self._targets.values()
                if entry.group_id is None
                and entry.state is TargetLifecycleState.ANNOUNCED
            ),
            key=_target_sort_key,
        )
        components: list[list[ShortCryptoTarget]] = []
        component_end_ms = -1
        for candidate in unassigned:
            if not components or candidate.opens_at_ms >= component_end_ms:
                components.append([candidate])
                component_end_ms = candidate.closes_at_ms
                continue
            components[-1].append(candidate)
            component_end_ms = max(component_end_ms, candidate.closes_at_ms)

        decisions: list[CaptureDecision] = []
        for component in components:
            scope_size = self._max_targets_per_group or len(component)
            for start in range(0, len(component), scope_size):
                scope = component[start : start + scope_size]
                due_at_ms = min(
                    item.opens_at_ms - self._subscribe_lead_ms
                    for item in scope
                )
                if now_ms < due_at_ms:
                    continue
                decision = self._subscribe_decision(
                    scope,
                    emitted_at_ms=now_ms,
                )
                decisions.append(decision)
                self._groups[decision.group_id] = _GroupEntry(
                    group_id=decision.group_id,
                    asset_ids=decision.asset_ids,
                    target_slugs=tuple(
                        binding.slug for binding in decision.targets
                    ),
                )
                self._pending_decisions[decision.decision_id] = decision
                for item in scope:
                    self._targets[item.slug].group_id = decision.group_id

        for group in sorted(
            self._groups.values(),
            key=lambda candidate: candidate.group_id,
        ):
            if (
                not group.active
                or group.resync_required
                or self._group_has_pending_scope_decision(group.group_id)
                or not all(
                    self._targets[slug].state
                    in {
                        TargetLifecycleState.SETTLED,
                        TargetLifecycleState.EXCLUDED,
                    }
                    for slug in group.target_slugs
                )
            ):
                continue
            decision = self._group_decision(
                group,
                action=CaptureDecisionAction.UNSUBSCRIBE,
                emitted_at_ms=now_ms,
                reason="all_targets_terminal",
            )
            self._pending_decisions[decision.decision_id] = decision
            decisions.append(decision)
        return tuple(decisions)

    def acknowledge(
        self,
        decision_id: str,
        *,
        applied_at_ms: int,
        initial_snapshot_watermark_ns: int | None = None,
    ) -> None:
        """Acknowledge that an external worker applied a scope decision.

        For ``subscribe``, acknowledge only after the worker has subscribed and
        completed its required initial full-book resnapshot.  For
        ``unsubscribe``, acknowledge after the whole worker has stopped.
        """

        if (
            isinstance(applied_at_ms, bool)
            or not isinstance(applied_at_ms, int)
            or applied_at_ms < 0
        ):
            raise ValueError("applied_at_ms must be a non-negative integer")
        try:
            decision = self._pending_decisions[decision_id]
        except KeyError as exc:
            raise CaptureSupervisorError(
                f"unknown or already acknowledged decision: {decision_id}"
            ) from exc
        if applied_at_ms < decision.emitted_at_ms:
            raise CaptureSupervisorError(
                "decision cannot be applied before it was emitted"
            )
        if initial_snapshot_watermark_ns is not None and (
            isinstance(initial_snapshot_watermark_ns, bool)
            or not isinstance(initial_snapshot_watermark_ns, int)
            or initial_snapshot_watermark_ns < 0
        ):
            raise ValueError(
                "initial_snapshot_watermark_ns must be a non-negative integer"
            )
        group = self._groups[decision.group_id]
        if decision.action is CaptureDecisionAction.RESNAPSHOT_REQUIRED:
            raise CaptureSupervisorError(
                "resnapshot requirements must be completed with a watermark"
            )
        if decision.action is CaptureDecisionAction.SUBSCRIBE:
            if group.active:
                raise CaptureSupervisorError("capture group is already active")
            if initial_snapshot_watermark_ns is None:
                raise CaptureSupervisorError(
                    "subscribe acknowledgement requires an initial snapshot "
                    "watermark"
                )
            eligible_slugs = [
                slug
                for slug in group.target_slugs
                if self._targets[slug].state
                is TargetLifecycleState.ANNOUNCED
                and applied_at_ms < self._targets[slug].target.opens_at_ms
            ]
            if not eligible_slugs:
                raise CaptureSupervisorError(
                    "subscription was not ready before market open; "
                    "no eligible target remains"
                )
            for slug in group.target_slugs:
                entry = self._targets[slug]
                if (
                    entry.state is TargetLifecycleState.ANNOUNCED
                    and applied_at_ms >= entry.target.opens_at_ms
                ):
                    self.exclude(
                        slug,
                        reason="subscription_not_ready_before_open",
                        evidence_record_id=(
                            entry.target.announcement_record_id
                        ),
                        observed_at_ms=applied_at_ms,
                    )
            group.active = True
            group.last_resnapshot_watermark_ns = (
                initial_snapshot_watermark_ns
            )
            for slug in eligible_slugs:
                entry = self._targets[slug]
                entry.state = TargetLifecycleState.SUBSCRIBED
                self._advance_entry_state(entry, now_ms=applied_at_ms)
        else:
            if initial_snapshot_watermark_ns is not None:
                raise CaptureSupervisorError(
                    "unsubscribe acknowledgement cannot carry a snapshot"
                )
            if not group.active:
                raise CaptureSupervisorError("capture group is already inactive")
            if group.resync_required:
                raise CaptureSupervisorError(
                    "cannot mutate worker scope during resync"
                )
            group.active = False
        del self._pending_decisions[decision_id]

    def mark_settled(
        self,
        slug: str,
        *,
        settlement_record_id: str,
        observed_at_ms: int,
    ) -> TargetLifecycleSnapshot:
        """Attach immutable settlement evidence after the market has closed."""

        if _CONTENT_HASH.fullmatch(settlement_record_id) is None:
            raise CaptureSupervisorError(
                "settlement_record_id must be a lowercase content hash"
            )
        if (
            isinstance(observed_at_ms, bool)
            or not isinstance(observed_at_ms, int)
            or observed_at_ms < 0
        ):
            raise ValueError("observed_at_ms must be a non-negative integer")
        try:
            entry = self._targets[slug]
        except KeyError as exc:
            raise KeyError(f"unknown target: {slug}") from exc
        if entry.state is TargetLifecycleState.SETTLED:
            if entry.settlement_record_id != settlement_record_id:
                raise CaptureSupervisorError(
                    f"conflicting settlement for target {slug}"
                )
            return self.target_snapshot(slug)
        self._advance_entry_state(entry, now_ms=observed_at_ms)
        if entry.state is not TargetLifecycleState.CLOSED:
            raise CaptureSupervisorError(
                f"cannot settle target from {entry.state.value}"
            )
        entry.state = TargetLifecycleState.SETTLED
        entry.settlement_record_id = settlement_record_id
        return self.target_snapshot(slug)

    def exclude(
        self,
        slug: str,
        *,
        reason: str,
        evidence_record_id: str,
        observed_at_ms: int,
    ) -> TargetLifecycleSnapshot:
        """Fail closed while retaining why and from which immutable evidence."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        if _CONTENT_HASH.fullmatch(evidence_record_id) is None:
            raise CaptureSupervisorError(
                "evidence_record_id must be a lowercase content hash"
            )
        if (
            isinstance(observed_at_ms, bool)
            or not isinstance(observed_at_ms, int)
            or observed_at_ms < 0
        ):
            raise ValueError("observed_at_ms must be a non-negative integer")
        try:
            entry = self._targets[slug]
        except KeyError as exc:
            raise KeyError(f"unknown target: {slug}") from exc
        normalized_reason = reason.strip()
        if entry.state is TargetLifecycleState.EXCLUDED:
            if (
                entry.exclusion_reason != normalized_reason
                or entry.exclusion_record_id != evidence_record_id
                or entry.excluded_at_ms != observed_at_ms
            ):
                raise CaptureSupervisorError(
                    f"conflicting exclusion for target {slug}"
                )
            return self.target_snapshot(slug)
        if entry.state is TargetLifecycleState.SETTLED:
            raise CaptureSupervisorError("settled targets cannot be excluded")

        entry.state = TargetLifecycleState.EXCLUDED
        entry.exclusion_reason = normalized_reason
        entry.exclusion_record_id = evidence_record_id
        entry.excluded_at_ms = observed_at_ms
        return self.target_snapshot(slug)

    def on_disconnect(
        self,
        group_id: str,
        *,
        disconnected_at_ns: int,
        observed_at_ms: int,
    ) -> CaptureDecision:
        """Freeze a worker scope and require a post-disconnect snapshot."""

        if (
            isinstance(disconnected_at_ns, bool)
            or not isinstance(disconnected_at_ns, int)
            or disconnected_at_ns < 0
        ):
            raise ValueError(
                "disconnected_at_ns must be a non-negative integer"
            )
        if (
            isinstance(observed_at_ms, bool)
            or not isinstance(observed_at_ms, int)
            or observed_at_ms < 0
        ):
            raise ValueError("observed_at_ms must be a non-negative integer")
        try:
            group = self._groups[group_id]
        except KeyError as exc:
            raise KeyError(f"unknown capture group: {group_id}") from exc
        if not group.active:
            raise CaptureSupervisorError(
                "cannot resync an inactive capture group"
            )
        pending_scope = [
            (decision_id, decision)
            for decision_id, decision in self._pending_decisions.items()
            if decision.group_id == group_id
            and decision.action
            in {
                CaptureDecisionAction.SUBSCRIBE,
                CaptureDecisionAction.UNSUBSCRIBE,
            }
        ]
        if any(
            decision.action is CaptureDecisionAction.SUBSCRIBE
            for _, decision in pending_scope
        ):
            raise CaptureSupervisorError(
                "cannot disconnect with an unresolved scope decision"
            )
        for decision_id, _ in pending_scope:
            del self._pending_decisions[decision_id]

        group.resync_required = True
        current_floor = group.minimum_snapshot_watermark_ns
        group.minimum_snapshot_watermark_ns = (
            disconnected_at_ns
            if current_floor is None
            else max(current_floor, disconnected_at_ns)
        )
        for decision_id, decision in tuple(self._pending_decisions.items()):
            if (
                decision.group_id == group_id
                and decision.action
                is CaptureDecisionAction.RESNAPSHOT_REQUIRED
            ):
                del self._pending_decisions[decision_id]
        requirement = self._group_decision(
            group,
            action=CaptureDecisionAction.RESNAPSHOT_REQUIRED,
            emitted_at_ms=observed_at_ms,
            reason="websocket_disconnect",
            minimum_snapshot_watermark_ns=(
                group.minimum_snapshot_watermark_ns
            ),
        )
        self._pending_decisions[requirement.decision_id] = requirement
        return requirement

    def complete_resnapshot(
        self,
        group_id: str,
        *,
        snapshot_watermark_ns: int,
        completed_at_ms: int,
    ) -> CaptureGroupSnapshot:
        """Release a frozen scope only after a sufficiently new full snapshot."""

        if (
            isinstance(snapshot_watermark_ns, bool)
            or not isinstance(snapshot_watermark_ns, int)
            or snapshot_watermark_ns < 0
        ):
            raise ValueError(
                "snapshot_watermark_ns must be a non-negative integer"
            )
        if (
            isinstance(completed_at_ms, bool)
            or not isinstance(completed_at_ms, int)
            or completed_at_ms < 0
        ):
            raise ValueError("completed_at_ms must be a non-negative integer")
        try:
            group = self._groups[group_id]
        except KeyError as exc:
            raise KeyError(f"unknown capture group: {group_id}") from exc
        floor = group.minimum_snapshot_watermark_ns
        if not group.resync_required or floor is None:
            raise CaptureSupervisorError(
                "capture group does not require a resnapshot"
            )
        if snapshot_watermark_ns < floor:
            raise CaptureSupervisorError(
                "snapshot watermark is before disconnect"
            )

        group.resync_required = False
        group.minimum_snapshot_watermark_ns = None
        group.last_resnapshot_watermark_ns = snapshot_watermark_ns
        for decision_id, decision in tuple(self._pending_decisions.items()):
            if (
                decision.group_id == group_id
                and decision.action
                is CaptureDecisionAction.RESNAPSHOT_REQUIRED
            ):
                del self._pending_decisions[decision_id]
        return self.group_snapshot(group_id)

    def abandon_resnapshot(
        self,
        group_id: str,
        *,
        observed_at_ms: int,
    ) -> CaptureGroupSnapshot:
        """Fail closed and clear a resnapshot requirement without a watermark.

        The caller must separately exclude every target whose required capture
        window intersects the abandoned gap.  This method only releases the
        group state so a terminal scope can be unsubscribed and finalized.
        """

        if (
            isinstance(observed_at_ms, bool)
            or not isinstance(observed_at_ms, int)
            or observed_at_ms < 0
        ):
            raise ValueError("observed_at_ms must be a non-negative integer")
        try:
            group = self._groups[group_id]
        except KeyError as exc:
            raise KeyError(f"unknown capture group: {group_id}") from exc
        if not group.active or not group.resync_required:
            raise CaptureSupervisorError(
                "capture group has no active resnapshot to abandon"
            )
        group.resync_required = False
        group.minimum_snapshot_watermark_ns = None
        for decision_id, decision in tuple(self._pending_decisions.items()):
            if (
                decision.group_id == group_id
                and decision.action
                is CaptureDecisionAction.RESNAPSHOT_REQUIRED
            ):
                del self._pending_decisions[decision_id]
        return self.group_snapshot(group_id)

    @staticmethod
    def _advance_entry_state(entry: _TargetEntry, *, now_ms: int) -> None:
        if entry.state is TargetLifecycleState.SUBSCRIBED:
            if now_ms >= entry.target.closes_at_ms:
                entry.state = TargetLifecycleState.CLOSED
            elif now_ms >= entry.target.opens_at_ms:
                entry.state = TargetLifecycleState.OPEN
        elif (
            entry.state is TargetLifecycleState.OPEN
            and now_ms >= entry.target.closes_at_ms
        ):
            entry.state = TargetLifecycleState.CLOSED

    def _group_has_pending_scope_decision(self, group_id: str) -> bool:
        return any(
            decision.group_id == group_id
            and decision.action
            in {
                CaptureDecisionAction.SUBSCRIBE,
                CaptureDecisionAction.UNSUBSCRIBE,
            }
            for decision in self._pending_decisions.values()
        )

    def _subscribe_decision(
        self,
        targets: list[ShortCryptoTarget],
        *,
        emitted_at_ms: int,
    ) -> CaptureDecision:
        ordered = tuple(sorted(targets, key=_target_sort_key))
        bindings = tuple(_binding(target) for target in ordered)
        asset_ids = tuple(
            token_id
            for target in ordered
            for token_id in (target.up_token_id, target.down_token_id)
        )
        group_hash = hashlib.sha256(
            canonical_json_bytes(
                {
                    "schema_version": "edge-lab.short-crypto-capture-group.v1",
                    "targets": [
                        {
                            "slug": binding.slug,
                            "rule_hash": binding.rule_hash,
                        }
                        for binding in bindings
                    ],
                    "asset_ids": asset_ids,
                }
            )
        ).hexdigest()
        group_id = f"short-crypto-{group_hash[:24]}"
        return self._new_decision(
            action=CaptureDecisionAction.SUBSCRIBE,
            group_id=group_id,
            asset_ids=asset_ids,
            bindings=bindings,
            emitted_at_ms=emitted_at_ms,
            reason="capture_window_due",
        )

    def _group_decision(
        self,
        group: _GroupEntry,
        *,
        action: CaptureDecisionAction,
        emitted_at_ms: int,
        reason: str,
        minimum_snapshot_watermark_ns: int | None = None,
    ) -> CaptureDecision:
        bindings = tuple(
            _binding(self._targets[slug].target)
            for slug in group.target_slugs
        )
        return self._new_decision(
            action=action,
            group_id=group.group_id,
            asset_ids=group.asset_ids,
            bindings=bindings,
            emitted_at_ms=emitted_at_ms,
            reason=reason,
            minimum_snapshot_watermark_ns=minimum_snapshot_watermark_ns,
        )

    def _new_decision(
        self,
        *,
        action: CaptureDecisionAction,
        group_id: str,
        asset_ids: tuple[str, ...],
        bindings: tuple[DecisionTargetBinding, ...],
        emitted_at_ms: int,
        reason: str,
        minimum_snapshot_watermark_ns: int | None = None,
    ) -> CaptureDecision:
        self._sequence += 1
        decision_body = {
            "schema_version": "edge-lab.short-crypto-capture-decision.v1",
            "sequence": self._sequence,
            "action": action.value,
            "group_id": group_id,
            "asset_ids": asset_ids,
            "targets": [
                {
                    "slug": binding.slug,
                    "announcement_record_id": binding.announcement_record_id,
                    "rule_hash": binding.rule_hash,
                    "up_token_id": binding.up_token_id,
                    "down_token_id": binding.down_token_id,
                }
                for binding in bindings
            ],
            "emitted_at_ms": emitted_at_ms,
            "reason": reason,
            "minimum_snapshot_watermark_ns": minimum_snapshot_watermark_ns,
        }
        decision_id = hashlib.sha256(
            canonical_json_bytes(decision_body)
        ).hexdigest()
        return CaptureDecision(
            schema_version=decision_body["schema_version"],
            decision_id=decision_id,
            sequence=self._sequence,
            action=action,
            group_id=group_id,
            asset_ids=asset_ids,
            targets=bindings,
            emitted_at_ms=emitted_at_ms,
            reason=reason,
            minimum_snapshot_watermark_ns=minimum_snapshot_watermark_ns,
        )
