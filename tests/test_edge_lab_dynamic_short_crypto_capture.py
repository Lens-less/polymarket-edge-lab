"""Pure lifecycle planning for dynamic short-crypto capture workers."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from src.edge_lab.dynamic_short_crypto_capture import (
    CaptureDecisionAction,
    CaptureSupervisorError,
    DynamicShortCryptoCaptureSupervisor,
    TargetLifecycleState,
)
from src.edge_lab.short_crypto_catalog import ShortCryptoTarget


BASE_OPEN_MS = 1_784_980_800_000


def target(
    *,
    asset: str,
    horizon: str,
    opens_at_ms: int,
    suffix: str,
) -> ShortCryptoTarget:
    duration_ms = {"5m": 300_000, "15m": 900_000}[horizon]
    token_seed = str(int(suffix, 16))
    return ShortCryptoTarget(
        slug=f"{asset}-updown-{horizon}-{opens_at_ms // 1_000}",
        market_id=str(3_081_000 + int(suffix, 16)),
        condition_id="0x" + suffix * 64,
        up_token_id=(token_seed + "1") * 38,
        down_token_id=(token_seed + "2") * 38,
        source_topic="crypto_prices_chainlink",
        source_symbol=f"{asset}/usd",
        horizon=horizon,
        opens_at_ms=opens_at_ms,
        closes_at_ms=opens_at_ms + duration_ms,
        announced_at="2026-07-24T13:20:00.000000Z",
        announcement_record_id=suffix * 64,
        rule_hash=(hex(int(suffix, 16) + 8)[2:] * 64)[:64],
    )


def overlapping_targets() -> tuple[ShortCryptoTarget, ...]:
    return (
        target(
            asset="btc",
            horizon="5m",
            opens_at_ms=BASE_OPEN_MS,
            suffix="1",
        ),
        target(
            asset="eth",
            horizon="5m",
            opens_at_ms=BASE_OPEN_MS,
            suffix="2",
        ),
        target(
            asset="btc",
            horizon="5m",
            opens_at_ms=BASE_OPEN_MS + 300_000,
            suffix="3",
        ),
        target(
            asset="btc",
            horizon="15m",
            opens_at_ms=BASE_OPEN_MS,
            suffix="4",
        ),
    )


def test_overlapping_targets_form_one_deterministic_frozen_worker_scope() -> None:
    targets = overlapping_targets()
    first = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    second = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    for item in targets:
        first.announce(item)
    for item in reversed(targets):
        second.announce(item)

    emitted_at_ms = BASE_OPEN_MS - 60_000
    first_decisions = first.advance(emitted_at_ms)
    second_decisions = second.advance(emitted_at_ms)

    assert len(first_decisions) == len(second_decisions) == 1
    decision = first_decisions[0]
    assert decision == second_decisions[0]
    assert decision.action is CaptureDecisionAction.SUBSCRIBE
    assert decision.emitted_at_ms == emitted_at_ms
    assert len(decision.asset_ids) == 8
    assert decision.asset_ids == tuple(
        token_id
        for item in sorted(
            targets,
            key=lambda candidate: (
                candidate.opens_at_ms,
                candidate.closes_at_ms,
                candidate.slug,
            ),
        )
        for token_id in (item.up_token_id, item.down_token_id)
    )
    assert {
        (binding.announcement_record_id, binding.rule_hash)
        for binding in decision.targets
    } == {
        (item.announcement_record_id, item.rule_hash) for item in targets
    }
    assert len(decision.decision_id) == 64
    assert all(
        first.target_snapshot(item.slug).state
        is TargetLifecycleState.ANNOUNCED
        for item in targets
    )
    with pytest.raises(FrozenInstanceError):
        decision.asset_ids = ()  # type: ignore[misc]


def test_overlapping_targets_respect_deterministic_asset_scope_cap() -> None:
    targets = overlapping_targets()
    first = DynamicShortCryptoCaptureSupervisor(
        subscribe_lead_ms=60_000,
        max_assets_per_group=4,
    )
    second = DynamicShortCryptoCaptureSupervisor(
        subscribe_lead_ms=60_000,
        max_assets_per_group=4,
    )
    for item in targets:
        first.announce(item)
    for item in reversed(targets):
        second.announce(item)

    emitted_at_ms = BASE_OPEN_MS - 60_000
    first_decisions = first.advance(emitted_at_ms)
    second_decisions = second.advance(emitted_at_ms)

    assert first_decisions == second_decisions
    assert tuple(
        tuple(binding.slug for binding in decision.targets)
        for decision in first_decisions
    ) == (
        (
            f"btc-updown-5m-{BASE_OPEN_MS // 1_000}",
            f"eth-updown-5m-{BASE_OPEN_MS // 1_000}",
        ),
        (
            f"btc-updown-15m-{BASE_OPEN_MS // 1_000}",
            f"btc-updown-5m-{BASE_OPEN_MS // 1_000 + 300}",
        ),
    )
    assert all(
        decision.action is CaptureDecisionAction.SUBSCRIBE
        and len(decision.asset_ids) <= 4
        for decision in first_decisions
    )


def test_group_identity_ignores_duplicate_observation_record_id() -> None:
    original = target(
        asset="btc",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="1",
    )
    duplicate_observation = replace(
        original,
        announced_at="2026-07-24T13:21:00.000000Z",
        announcement_record_id="f" * 64,
    )
    first = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    second = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    first.announce(original)
    second.announce(duplicate_observation)

    first_decision = first.advance(BASE_OPEN_MS - 60_000)[0]
    second_decision = second.advance(BASE_OPEN_MS - 60_000)[0]

    assert first_decision.group_id == second_decision.group_id
    assert first_decision.decision_id != second_decision.decision_id


def test_worker_ack_drives_full_settled_lifecycle_and_unsubscribe() -> None:
    item = target(
        asset="btc",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="5",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(item)
    subscribe = supervisor.advance(BASE_OPEN_MS - 60_000)[0]

    assert (
        supervisor.target_snapshot(item.slug).state
        is TargetLifecycleState.ANNOUNCED
    )
    with pytest.raises(CaptureSupervisorError, match="initial snapshot"):
        supervisor.acknowledge(
            subscribe.decision_id,
            applied_at_ms=BASE_OPEN_MS - 59_000,
        )
    with pytest.raises(CaptureSupervisorError, match="before market open"):
        supervisor.acknowledge(
            subscribe.decision_id,
            applied_at_ms=BASE_OPEN_MS,
            initial_snapshot_watermark_ns=80_000_000_000,
        )
    supervisor.acknowledge(
        subscribe.decision_id,
        applied_at_ms=BASE_OPEN_MS - 59_000,
        initial_snapshot_watermark_ns=81_000_000_000,
    )
    assert (
        supervisor.target_snapshot(item.slug).state
        is TargetLifecycleState.SUBSCRIBED
    )
    assert supervisor.group_snapshot(subscribe.group_id).active
    assert (
        supervisor.group_snapshot(
            subscribe.group_id
        ).last_resnapshot_watermark_ns
        == 81_000_000_000
    )

    assert supervisor.advance(BASE_OPEN_MS) == ()
    assert (
        supervisor.target_snapshot(item.slug).state
        is TargetLifecycleState.OPEN
    )
    assert supervisor.advance(item.closes_at_ms) == ()
    assert (
        supervisor.target_snapshot(item.slug).state
        is TargetLifecycleState.CLOSED
    )

    settlement_record_id = "e" * 64
    supervisor.mark_settled(
        item.slug,
        settlement_record_id=settlement_record_id,
        observed_at_ms=item.closes_at_ms + 2_000,
    )
    assert (
        supervisor.target_snapshot(item.slug).state
        is TargetLifecycleState.SETTLED
    )
    unsubscribe = supervisor.advance(item.closes_at_ms + 2_000)

    assert len(unsubscribe) == 1
    assert unsubscribe[0].action is CaptureDecisionAction.UNSUBSCRIBE
    assert unsubscribe[0].group_id == subscribe.group_id
    assert unsubscribe[0].asset_ids == subscribe.asset_ids
    assert unsubscribe[0].targets == subscribe.targets
    supervisor.acknowledge(
        unsubscribe[0].decision_id,
        applied_at_ms=item.closes_at_ms + 2_001,
    )
    assert not supervisor.group_snapshot(subscribe.group_id).active
    snapshot = supervisor.target_snapshot(item.slug)
    assert snapshot.state is TargetLifecycleState.SETTLED
    assert snapshot.settlement_record_id == settlement_record_id


def test_disconnect_freezes_scope_until_resnapshot_reaches_watermark() -> None:
    item = target(
        asset="eth",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="6",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(item)
    subscribe = supervisor.advance(BASE_OPEN_MS - 60_000)[0]
    supervisor.acknowledge(
        subscribe.decision_id,
        applied_at_ms=BASE_OPEN_MS - 59_000,
        initial_snapshot_watermark_ns=90_000_000_000,
    )
    scope_before = supervisor.group_snapshot(subscribe.group_id)

    disconnected_at_ns = 91_000_000_000
    requirement = supervisor.on_disconnect(
        subscribe.group_id,
        disconnected_at_ns=disconnected_at_ns,
        observed_at_ms=BASE_OPEN_MS + 10_000,
    )

    assert requirement.action is CaptureDecisionAction.RESNAPSHOT_REQUIRED
    assert requirement.group_id == subscribe.group_id
    assert requirement.asset_ids == subscribe.asset_ids
    assert requirement.targets == subscribe.targets
    assert requirement.minimum_snapshot_watermark_ns == disconnected_at_ns
    scope_during = supervisor.group_snapshot(subscribe.group_id)
    assert scope_during.resync_required
    assert scope_during.asset_ids == scope_before.asset_ids
    assert scope_during.target_slugs == scope_before.target_slugs

    supervisor.advance(item.closes_at_ms)
    supervisor.mark_settled(
        item.slug,
        settlement_record_id="f" * 64,
        observed_at_ms=item.closes_at_ms + 1,
    )
    assert supervisor.advance(item.closes_at_ms + 1) == ()
    assert supervisor.group_snapshot(subscribe.group_id).asset_ids == (
        scope_before.asset_ids
    )

    with pytest.raises(CaptureSupervisorError, match="before disconnect"):
        supervisor.complete_resnapshot(
            subscribe.group_id,
            snapshot_watermark_ns=disconnected_at_ns - 1,
            completed_at_ms=item.closes_at_ms + 2,
        )
    assert supervisor.group_snapshot(subscribe.group_id).resync_required

    supervisor.complete_resnapshot(
        subscribe.group_id,
        snapshot_watermark_ns=disconnected_at_ns + 1,
        completed_at_ms=item.closes_at_ms + 3,
    )
    recovered = supervisor.group_snapshot(subscribe.group_id)
    assert not recovered.resync_required
    assert recovered.last_resnapshot_watermark_ns == disconnected_at_ns + 1
    unsubscribe = supervisor.advance(item.closes_at_ms + 3)
    assert [decision.action for decision in unsubscribe] == [
        CaptureDecisionAction.UNSUBSCRIBE
    ]
    assert unsubscribe[0].asset_ids == scope_before.asset_ids


def test_excluded_target_retains_reason_and_never_enters_worker_scope() -> None:
    item = target(
        asset="btc",
        horizon="15m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="7",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(item)

    snapshot = supervisor.exclude(
        item.slug,
        reason="gamma_rule_conflict",
        evidence_record_id="a" * 64,
        observed_at_ms=BASE_OPEN_MS - 120_000,
    )

    assert snapshot.state is TargetLifecycleState.EXCLUDED
    assert snapshot.exclusion_reason == "gamma_rule_conflict"
    assert snapshot.exclusion_record_id == "a" * 64
    assert snapshot.excluded_at_ms == BASE_OPEN_MS - 120_000
    assert snapshot.group_id is None
    assert supervisor.advance(BASE_OPEN_MS - 60_000) == ()


@pytest.mark.parametrize(
    ("state", "evidence_record_id"),
    [
        (TargetLifecycleState.SETTLED, "a" * 64),
        (TargetLifecycleState.EXCLUDED, "b" * 64),
    ],
)
def test_finalized_terminal_recovery_is_idempotent_and_never_schedules(
    state: TargetLifecycleState,
    evidence_record_id: str,
) -> None:
    item = target(
        asset="btc",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="8",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)

    first = supervisor.restore_finalized_terminal(
        item,
        state=state,
        evidence_record_id=evidence_record_id,
        exclusion_reason=(
            "capture_interrupted_during_market"
            if state is TargetLifecycleState.EXCLUDED
            else None
        ),
        observed_at_ms=item.closes_at_ms + 1,
    )
    second = supervisor.restore_finalized_terminal(
        item,
        state=state,
        evidence_record_id=evidence_record_id,
        exclusion_reason=(
            "capture_interrupted_during_market"
            if state is TargetLifecycleState.EXCLUDED
            else None
        ),
        observed_at_ms=item.closes_at_ms + 1,
    )

    assert first == second
    assert first.state is state
    if state is TargetLifecycleState.SETTLED:
        assert first.settlement_record_id == evidence_record_id
    else:
        assert first.exclusion_reason == "capture_interrupted_during_market"
        assert first.exclusion_record_id == evidence_record_id
    assert supervisor.advance(item.closes_at_ms + 2) == ()


def test_recovered_decision_sequence_is_not_reused() -> None:
    item = target(
        asset="eth",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="9",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.restore_decision_sequence(41)
    supervisor.announce(item)

    decision = supervisor.advance(BASE_OPEN_MS - 60_000)[0]

    assert decision.sequence == 42


def test_missed_subscription_window_is_excluded_instead_of_backfilled() -> None:
    item = target(
        asset="eth",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="8",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(item)

    assert supervisor.advance(item.closes_at_ms) == ()

    snapshot = supervisor.target_snapshot(item.slug)
    assert snapshot.state is TargetLifecycleState.EXCLUDED
    assert snapshot.exclusion_reason == "subscription_window_missed"
    assert snapshot.exclusion_record_id == item.announcement_record_id
    assert snapshot.excluded_at_ms == item.closes_at_ms


def test_unacknowledged_worker_is_excluded_at_open_deadline() -> None:
    item = target(
        asset="btc",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="9",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(item)
    subscribe = supervisor.advance(BASE_OPEN_MS - 60_000)[0]

    assert supervisor.advance(BASE_OPEN_MS) == ()

    snapshot = supervisor.target_snapshot(item.slug)
    assert snapshot.state is TargetLifecycleState.EXCLUDED
    assert snapshot.exclusion_reason == "subscription_not_ready_before_open"
    assert not supervisor.group_snapshot(subscribe.group_id).active
    with pytest.raises(CaptureSupervisorError, match="unknown or already"):
        supervisor.acknowledge(
            subscribe.decision_id,
            applied_at_ms=BASE_OPEN_MS,
            initial_snapshot_watermark_ns=1,
        )


def test_late_group_readiness_preserves_still_future_members() -> None:
    early = target(
        asset="btc",
        horizon="15m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="c",
    )
    later = target(
        asset="eth",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS + 300_000,
        suffix="d",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(early)
    supervisor.announce(later)
    subscribe = supervisor.advance(BASE_OPEN_MS - 60_000)[0]

    assert supervisor.advance(BASE_OPEN_MS) == ()
    assert (
        supervisor.target_snapshot(early.slug).state
        is TargetLifecycleState.EXCLUDED
    )
    assert (
        supervisor.target_snapshot(later.slug).state
        is TargetLifecycleState.ANNOUNCED
    )

    supervisor.acknowledge(
        subscribe.decision_id,
        applied_at_ms=BASE_OPEN_MS + 100_000,
        initial_snapshot_watermark_ns=123,
    )

    assert supervisor.group_snapshot(subscribe.group_id).active
    assert (
        supervisor.target_snapshot(early.slug).state
        is TargetLifecycleState.EXCLUDED
    )
    assert (
        supervisor.target_snapshot(later.slug).state
        is TargetLifecycleState.SUBSCRIBED
    )


def test_disconnect_supersedes_unacknowledged_unsubscribe() -> None:
    item = target(
        asset="eth",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="a",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(item)
    subscribe = supervisor.advance(BASE_OPEN_MS - 60_000)[0]
    supervisor.acknowledge(
        subscribe.decision_id,
        applied_at_ms=BASE_OPEN_MS - 59_000,
        initial_snapshot_watermark_ns=100,
    )
    supervisor.advance(item.closes_at_ms)
    supervisor.mark_settled(
        item.slug,
        settlement_record_id="b" * 64,
        observed_at_ms=item.closes_at_ms,
    )
    first_unsubscribe = supervisor.advance(item.closes_at_ms)[0]

    requirement = supervisor.on_disconnect(
        subscribe.group_id,
        disconnected_at_ns=200,
        observed_at_ms=item.closes_at_ms + 1,
    )

    assert requirement.action is CaptureDecisionAction.RESNAPSHOT_REQUIRED
    with pytest.raises(CaptureSupervisorError, match="unknown or already"):
        supervisor.acknowledge(
            first_unsubscribe.decision_id,
            applied_at_ms=item.closes_at_ms + 1,
        )
    supervisor.complete_resnapshot(
        subscribe.group_id,
        snapshot_watermark_ns=201,
        completed_at_ms=item.closes_at_ms + 2,
    )
    second_unsubscribe = supervisor.advance(item.closes_at_ms + 2)[0]
    assert second_unsubscribe.action is CaptureDecisionAction.UNSUBSCRIBE
    assert second_unsubscribe.decision_id != first_unsubscribe.decision_id


def test_abandoned_resnapshot_releases_terminal_group_for_unsubscribe() -> None:
    item = target(
        asset="btc",
        horizon="5m",
        opens_at_ms=BASE_OPEN_MS,
        suffix="b",
    )
    supervisor = DynamicShortCryptoCaptureSupervisor(subscribe_lead_ms=60_000)
    supervisor.announce(item)
    subscribe = supervisor.advance(BASE_OPEN_MS - 60_000)[0]
    supervisor.acknowledge(
        subscribe.decision_id,
        applied_at_ms=BASE_OPEN_MS - 59_000,
        initial_snapshot_watermark_ns=100,
    )
    supervisor.on_disconnect(
        subscribe.group_id,
        disconnected_at_ns=200,
        observed_at_ms=BASE_OPEN_MS - 30_000,
    )
    supervisor.exclude(
        item.slug,
        reason="resnapshot_not_ready_before_open",
        evidence_record_id="c" * 64,
        observed_at_ms=BASE_OPEN_MS,
    )

    assert supervisor.advance(BASE_OPEN_MS) == ()
    supervisor.abandon_resnapshot(
        subscribe.group_id,
        observed_at_ms=BASE_OPEN_MS,
    )
    unsubscribe = supervisor.advance(BASE_OPEN_MS)

    assert [item.action for item in unsubscribe] == [
        CaptureDecisionAction.UNSUBSCRIBE
    ]
