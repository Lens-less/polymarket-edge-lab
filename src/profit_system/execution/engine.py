from __future__ import annotations

import hmac
import inspect
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, TypeVar

from ..readiness import REQUIRED_CANARY_CHECKS
from .adapters import VenueAdapter
from .errors import (
    HeartbeatError,
    IdempotencyConflictError,
    OrderValidationError,
    PersistentKillError,
    QualificationError,
    ReconciliationRequiredError,
    ReleasePermitError,
    RiskLimitError,
    UserStreamGapError,
)
from .models import (
    LIVEISH_STATUSES,
    ExecutionApproval,
    ExecutionIntent,
    ExecutionMode,
    ExecutionPlan,
    ExecutionSnapshot,
    ExecutionTicket,
    FillRecord,
    InterventionAction,
    InterventionKind,
    InterventionResult,
    OrderLifecycleStatus,
    OrderRecord,
    QualificationLevel,
    QualificationRecord,
    ReleasePermit,
    RiskContext,
    TimeInForce,
    VenueEventKind,
    VenueOrderSpec,
    VenueOrderState,
    VenuePreflightRequest,
    VenueUserEvent,
    hmac_sha256_hex,
    non_empty,
    qualification_allows_mode,
    qualification_rank,
    sha256_hex,
    utc_now,
)


class QualificationRegistry(Protocol):
    def get(self, strategy_id: str) -> QualificationRecord | None: ...


class KillSwitchStore(Protocol):
    def load(self) -> Mapping[str, object]: ...

    def persist(self, state: Mapping[str, object]) -> None: ...


class SubmitJournalHook(Protocol):
    def __call__(
        self,
        *,
        plan: ExecutionPlan,
        approval: ExecutionApproval,
        orders: Sequence[OrderRecord],
    ) -> object: ...


@dataclass
class InMemoryQualificationRegistry(QualificationRegistry):
    records: dict[str, QualificationRecord]

    def get(self, strategy_id: str) -> QualificationRecord | None:
        return self.records.get(strategy_id)


@dataclass
class InMemoryKillSwitchStore(KillSwitchStore):
    state: dict[str, object] | None = None

    def load(self) -> Mapping[str, object]:
        return dict(self.state or {})

    def persist(self, state: Mapping[str, object]) -> None:
        self.state = dict(state)


class HmacReleasePermitAuthority:
    def __init__(
        self,
        *,
        secret: bytes,
        version: str = "canary_release.v1",
        max_age: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not secret:
            raise ValueError("secret must be non-empty")
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self._secret = secret
        self._version = version
        self._max_age = max_age
        self._clock = clock
        self._lock = threading.Lock()
        self._reserved_nonces: set[str] = set()
        self._consumed_nonces: set[str] = set()

    def issue(
        self,
        *,
        plan: ExecutionPlan,
        qualification: QualificationRecord,
        confirmation_phrase: str,
        canary_checks: Mapping[str, bool],
        mode: ExecutionMode,
    ) -> ReleasePermit:
        if mode not in {ExecutionMode.LIVE_CANARY, ExecutionMode.LIVE_LIMITED}:
            raise ReleasePermitError("release permits apply only to live modes")
        if tuple(canary_checks.keys()) != REQUIRED_CANARY_CHECKS:
            raise ReleasePermitError("release permit must bind the exact required Canary checks")
        if not all(type(value) is bool and value for value in canary_checks.values()):
            raise ReleasePermitError("all Canary evidence checks must be explicitly green")
        if qualification.qualification not in {
            QualificationLevel.PROBE_ALLOWED,
            QualificationLevel.LIVE_ALLOWED,
        }:
            raise ReleasePermitError("qualification must be PROBE_ALLOWED or LIVE_ALLOWED")
        issued_at = self._clock()
        permit = ReleasePermit(
            version=self._version,
            strategy_id=plan.intent.strategy_id,
            qualification=qualification.qualification,
            mode=mode,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            confirmation_phrase=non_empty(
                confirmation_phrase,
                name="confirmation_phrase",
            ),
            canary_checks=dict(canary_checks),
            evidence_digest=qualification.evidence_digest,
            issued_at=issued_at,
            expires_at=issued_at + self._max_age,
            nonce=secrets.token_hex(16),
            signature="pending",
        )
        signature = hmac_sha256_hex(self._secret, permit.payload)
        return ReleasePermit(**{**permit.__dict__, "signature": signature})

    def _verify_unlocked(self, permit: ReleasePermit, *, now: datetime) -> None:
        expected = hmac_sha256_hex(self._secret, permit.payload)
        if not hmac.compare_digest(permit.signature, expected):
            raise ReleasePermitError("release permit signature is invalid")
        if tuple(permit.canary_checks.keys()) != REQUIRED_CANARY_CHECKS:
            raise ReleasePermitError("release permit Canary checks are incomplete or reordered")
        if not permit.canary_checks or not all(permit.canary_checks.values()):
            raise ReleasePermitError("release permit Canary evidence is not fully green")
        issued_at = permit.issued_at.astimezone(UTC)
        expires_at = permit.expires_at.astimezone(UTC)
        current = now.astimezone(UTC)
        if expires_at <= issued_at:
            raise ReleasePermitError("release permit expiry is invalid")
        if expires_at - issued_at > self._max_age:
            raise ReleasePermitError("release permit validity window exceeds verifier policy")
        if issued_at > current:
            raise ReleasePermitError("release permit was issued in the future")
        if current >= expires_at:
            raise ReleasePermitError("release permit has expired")

    def verify(self, permit: ReleasePermit, *, now: datetime) -> None:
        with self._lock:
            self._verify_unlocked(permit, now=now)

    def reserve(self, permit: ReleasePermit, *, now: datetime) -> None:
        with self._lock:
            self._verify_unlocked(permit, now=now)
            if permit.nonce in self._consumed_nonces or permit.nonce in self._reserved_nonces:
                raise ReleasePermitError("release permit nonce has already been used")
            self._reserved_nonces.add(permit.nonce)

    def release(self, permit: ReleasePermit) -> None:
        with self._lock:
            self._reserved_nonces.discard(permit.nonce)

    def consume(self, permit: ReleasePermit) -> None:
        with self._lock:
            if permit.nonce not in self._reserved_nonces:
                if permit.nonce in self._consumed_nonces:
                    raise ReleasePermitError("release permit nonce has already been used")
                raise ReleasePermitError("release permit nonce was not reserved")
            self._reserved_nonces.remove(permit.nonce)
            self._consumed_nonces.add(permit.nonce)


class ExecutionEngine:
    _TIdempotent = TypeVar("_TIdempotent")

    def __init__(
        self,
        *,
        mode: ExecutionMode,
        venue: VenueAdapter,
        qualification_registry: QualificationRegistry,
        kill_store: KillSwitchStore | None = None,
        permit_authority: HmacReleasePermitAuthority | None = None,
        clock: Callable[[], datetime] = utc_now,
        restored_orders: Sequence[OrderRecord] = (),
        submit_journal: SubmitJournalHook | None = None,
    ) -> None:
        self.mode = mode
        self.venue = venue
        self.qualification_registry = qualification_registry
        self.kill_store = kill_store or InMemoryKillSwitchStore()
        self.permit_authority = permit_authority
        self.clock = clock
        self.submit_journal = submit_journal
        self._plans: dict[str, ExecutionPlan] = {}
        self._orders: dict[str, OrderRecord] = {
            order.client_order_id: order for order in restored_orders
        }
        self._idempotent_reviews: dict[str, ExecutionPlan] = {}
        self._review_payloads: dict[str, str] = {}
        self._idempotent_confirms: dict[str, ExecutionTicket] = {}
        self._confirm_payloads: dict[str, str] = {}
        self._plan_confirmations: dict[str, str] = {}
        self._idempotent_interventions: dict[str, InterventionResult] = {}
        self._intervention_payloads: dict[str, str] = {}
        self._last_reconcile_at: datetime | None = None
        self._last_user_event_at: datetime | None = None
        self._last_heartbeat_at: datetime | None = None
        self._user_stream_backfill_complete = False
        persisted = self.kill_store.load()
        self._killed = bool(persisted.get("killed", False))
        raw_kill_reason = persisted.get("reason")
        self._kill_reason = raw_kill_reason if isinstance(raw_kill_reason, str) else None
        self._blocked_reasons: set[str] = set()
        if self._is_live_mode(self.mode):
            self._blocked_reasons.update(
                {
                    "heartbeat_absent",
                    "user_stream_absent",
                    "user_stream_backfill_pending",
                }
            )
        if self._killed:
            self._blocked_reasons.add("persistent_kill")
        if any(
            order.status
            in {
                OrderLifecycleStatus.SUBMITTING,
                OrderLifecycleStatus.AMBIGUOUS,
                OrderLifecycleStatus.CANCEL_REQUESTED,
                OrderLifecycleStatus.RECONCILING,
            }
            for order in restored_orders
        ):
            self._blocked_reasons.add("restart_reconcile_required")

    @staticmethod
    def _is_live_mode(mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.LIVE_CANARY, ExecutionMode.LIVE_LIMITED}

    def _payload_hash(self, payload: Mapping[str, object]) -> str:
        return str(sha256_hex(payload))

    def _remember_idempotent(
        self,
        store: Mapping[str, _TIdempotent],
        payloads: dict[str, str],
        key: str,
        payload_hash: str,
    ) -> _TIdempotent | None:
        previous_hash = payloads.get(key)
        if previous_hash is not None and previous_hash != payload_hash:
            raise IdempotencyConflictError("idempotency key was reused for a different payload")
        payloads[key] = payload_hash
        return store.get(key)

    def _qualification_for(self, strategy_id: str, mode: ExecutionMode) -> QualificationRecord:
        record = self.qualification_registry.get(strategy_id)
        if record is None:
            raise QualificationError(f"missing qualification for {strategy_id}")
        if record.qualification is QualificationLevel.STOPPED:
            raise QualificationError(
                f"strategy {strategy_id} is STOPPED; no new-order mode is permitted"
            )
        if mode not in record.allowed_modes or not qualification_allows_mode(
            record.qualification,
            mode,
        ):
            raise QualificationError(f"strategy {strategy_id} is not qualified for {mode.value}")
        if record.valid_until is not None and record.valid_until.astimezone(UTC) < self.clock():
            raise QualificationError(f"qualification for {strategy_id} is expired")
        return record

    def _mark_stream_fresh(self, occurred_at: datetime) -> None:
        timestamp = occurred_at.astimezone(UTC)
        self._last_user_event_at = timestamp
        self._last_heartbeat_at = timestamp
        self._blocked_reasons.discard("user_stream_absent")
        self._blocked_reasons.discard("user_stream_gap")
        self._blocked_reasons.discard("heartbeat_absent")
        self._blocked_reasons.discard("heartbeat_failed")

    def _ensure_live_stream_health(
        self,
        *,
        qualification: QualificationRecord,
        stage: str,
    ) -> None:
        if not self._is_live_mode(self.mode):
            return
        maximum_gap = timedelta(seconds=qualification.risk_limits.max_user_stream_gap_seconds)
        now = self.clock()
        if not self._user_stream_backfill_complete:
            self._blocked_reasons.add("user_stream_backfill_pending")
            raise ReconciliationRequiredError(
                f"live {stage} requires an authenticated user-stream backfill before new orders"
            )
        self._blocked_reasons.discard("user_stream_backfill_pending")
        if self._last_user_event_at is None:
            self._blocked_reasons.add("user_stream_absent")
            raise UserStreamGapError(
                f"live {stage} requires authenticated user-stream activity before new orders"
            )
        user_gap = now - self._last_user_event_at
        if user_gap > maximum_gap:
            self._blocked_reasons.add("user_stream_gap")
            raise UserStreamGapError(
                "authenticated user stream is stale for live "
                f"{stage}: {user_gap.total_seconds():.0f}s"
            )
        self._blocked_reasons.discard("user_stream_gap")
        self._blocked_reasons.discard("user_stream_absent")
        if self._last_heartbeat_at is None:
            self._blocked_reasons.add("heartbeat_absent")
            raise HeartbeatError(f"live {stage} requires heartbeat evidence before new orders")
        heartbeat_gap = now - self._last_heartbeat_at
        if heartbeat_gap > maximum_gap:
            self._blocked_reasons.add("heartbeat_failed")
            raise HeartbeatError(
                f"heartbeat is stale for live {stage}: {heartbeat_gap.total_seconds():.0f}s"
            )
        self._blocked_reasons.discard("heartbeat_absent")
        self._blocked_reasons.discard("heartbeat_failed")

    def _ensure_new_orders_allowed(self) -> None:
        if self._killed:
            raise PersistentKillError(f"persistent kill engaged: {self._kill_reason or 'killed'}")
        if self._blocked_reasons:
            raise RiskLimitError(
                "new orders are blocked: " + ", ".join(sorted(self._blocked_reasons))
            )

    def _validate_market_whitelist(
        self,
        qualification: QualificationRecord,
        orders: Sequence[VenueOrderSpec],
    ) -> None:
        whitelist = set(qualification.market_whitelist)
        for order in orders:
            if whitelist and order.market_id not in whitelist:
                raise QualificationError(
                    f"market {order.market_id} is outside the strategy whitelist"
                )

    def _enforce_risk_limits(
        self,
        *,
        qualification: QualificationRecord,
        intent: ExecutionIntent,
    ) -> None:
        limits = qualification.risk_limits
        risk = intent.risk_context
        if risk.open_order_count + len(intent.orders) > limits.max_open_orders:
            raise RiskLimitError("max_open_orders exceeded")
        total_notional = sum((order.notional for order in intent.orders), Decimal("0"))
        market_additions: dict[str, Decimal] = {}
        event_additions: dict[str, Decimal] = {}
        for order in intent.orders:
            if order.notional > limits.max_order_notional:
                raise RiskLimitError(f"max_order_notional exceeded for {order.client_order_id}")
            market_additions[order.market_id] = (
                market_additions.get(order.market_id, Decimal("0")) + order.notional
            )
            event_additions[order.event_id] = (
                event_additions.get(order.event_id, Decimal("0")) + order.notional
            )
        for market_id, added_notional in market_additions.items():
            if (
                risk.market_exposure.get(market_id, Decimal("0")) + added_notional
                > limits.max_market_exposure
            ):
                raise RiskLimitError(f"max_market_exposure exceeded for {market_id}")
        for event_id, added_notional in event_additions.items():
            if (
                risk.event_exposure.get(event_id, Decimal("0")) + added_notional
                > limits.max_event_exposure
            ):
                raise RiskLimitError(f"max_event_exposure exceeded for {event_id}")
        strategy_total = (
            risk.strategy_exposure.get(intent.strategy_id, Decimal("0")) + total_notional
        )
        if strategy_total > limits.max_strategy_exposure:
            raise RiskLimitError("max_strategy_exposure exceeded")
        if risk.total_exposure + total_notional > limits.max_total_exposure:
            raise RiskLimitError("max_total_exposure exceeded")
        if risk.realized_daily_loss > limits.max_daily_loss:
            raise RiskLimitError("max_daily_loss exceeded")
        if risk.drawdown > limits.max_drawdown:
            raise RiskLimitError("max_drawdown exceeded")
        blocked_statuses = {
            OrderLifecycleStatus.SUBMITTING,
            OrderLifecycleStatus.AMBIGUOUS,
            OrderLifecycleStatus.CANCEL_REQUESTED,
            OrderLifecycleStatus.RECONCILING,
        }
        if any(order.status in blocked_statuses for order in self._orders.values()):
            raise RiskLimitError("unresolved local order state blocks new orders")
        if risk.market_data_age_ms > limits.max_market_data_age_ms:
            raise RiskLimitError("market data is stale")

    async def review(
        self,
        intent: ExecutionIntent,
        actor: str,
        idempotency_key: str,
    ) -> ExecutionPlan:
        payload_hash = self._payload_hash(
            {
                "kind": "review",
                "strategy_id": intent.strategy_id,
                "mode": intent.mode.value,
                "orders": [order.to_document() for order in intent.orders],
                "actor": actor,
            }
        )
        cached = self._remember_idempotent(
            self._idempotent_reviews,
            self._review_payloads,
            idempotency_key,
            payload_hash,
        )
        if cached is not None:
            return cached
        qualification = self._qualification_for(intent.strategy_id, intent.mode)
        if self._is_live_mode(intent.mode):
            self._ensure_live_stream_health(qualification=qualification, stage="review")
        self._ensure_new_orders_allowed()
        self._validate_market_whitelist(qualification, intent.orders)
        self._enforce_risk_limits(qualification=qualification, intent=intent)
        preflight = await self.venue.preflight(
            VenuePreflightRequest(
                mode=intent.mode,
                qualification=qualification,
                orders=intent.orders,
                risk_context=intent.risk_context,
                idempotency_key=idempotency_key,
                now=self.clock(),
            )
        )
        plan_id = f"plan-{secrets.token_hex(8)}"
        plan = ExecutionPlan.create(
            plan_id=plan_id,
            actor=actor,
            intent=intent,
            qualification=qualification,
            preflight=preflight,
            created_at=self.clock(),
        )
        self._plans[plan.plan_id] = plan
        for order in intent.orders:
            self._orders[order.client_order_id] = OrderRecord(
                client_order_id=order.client_order_id,
                spec=order,
                status=OrderLifecycleStatus.REVIEWED,
                plan_id=plan.plan_id,
                updated_at=self.clock(),
            )
        self._idempotent_reviews[idempotency_key] = plan
        return plan

    def _require_live_permit(
        self,
        *,
        plan: ExecutionPlan,
        approval: ExecutionApproval,
        now: datetime,
    ) -> None:
        permit = approval.release_permit
        if permit is None:
            raise ReleasePermitError("live execution requires a structured release permit")
        if self.permit_authority is None:
            raise ReleasePermitError("no release permit authority is configured")
        self.permit_authority.verify(permit, now=now)
        if permit.plan_id != plan.plan_id or permit.plan_hash != plan.plan_hash:
            raise ReleasePermitError("release permit does not bind this exact plan")
        if permit.confirmation_phrase != approval.confirmation_phrase:
            raise ReleasePermitError("release permit does not bind this exact confirmation")
        if permit.strategy_id != plan.intent.strategy_id:
            raise ReleasePermitError("release permit strategy mismatch")
        if permit.mode is not plan.intent.mode:
            raise ReleasePermitError("release permit mode mismatch")
        if permit.evidence_digest != plan.qualification.evidence_digest:
            raise ReleasePermitError("release permit evidence digest mismatch")
        required_rank = (
            QualificationLevel.LIVE_ALLOWED
            if plan.intent.mode is ExecutionMode.LIVE_LIMITED
            else QualificationLevel.PROBE_ALLOWED
        )
        if qualification_rank(permit.qualification) < qualification_rank(required_rank):
            raise ReleasePermitError("release permit qualification is insufficient")

    def _ensure_gtd_orders_still_live(
        self,
        *,
        orders: Sequence[VenueOrderSpec],
        now: datetime,
    ) -> None:
        current = now.astimezone(UTC)
        for order in orders:
            if order.time_in_force is not TimeInForce.GTD:
                continue
            assert order.expires_at is not None
            if order.expires_at.astimezone(UTC) <= current:
                raise RiskLimitError(f"GTD order {order.client_order_id} expired before confirm")

    async def _run_submit_journal(
        self,
        *,
        plan: ExecutionPlan,
        approval: ExecutionApproval,
        orders: Sequence[OrderRecord],
    ) -> None:
        if self.submit_journal is None:
            return
        maybe_result = self.submit_journal(plan=plan, approval=approval, orders=orders)
        if inspect.isawaitable(maybe_result):
            await maybe_result

    async def confirm(
        self,
        plan_id: str,
        approval: ExecutionApproval,
    ) -> ExecutionTicket:
        plan = self._plans[plan_id]
        payload_hash = self._payload_hash(
            {
                "kind": "confirm",
                "plan_id": plan_id,
                "plan_hash": approval.plan_hash,
                "confirmation_phrase": approval.confirmation_phrase,
                "actor": approval.actor,
                "release_permit": (
                    approval.release_permit.payload if approval.release_permit is not None else None
                ),
            }
        )
        cached = self._remember_idempotent(
            self._idempotent_confirms,
            self._confirm_payloads,
            approval.idempotency_key,
            payload_hash,
        )
        if cached is not None:
            return cached
        bound_confirm_key = self._plan_confirmations.get(plan_id)
        if bound_confirm_key is not None and bound_confirm_key != approval.idempotency_key:
            raise IdempotencyConflictError(
                "execution plan has already been confirmed; "
                "retries must reuse the original idempotency key"
            )
        if approval.plan_hash != plan.plan_hash:
            raise ReleasePermitError("approval plan hash does not match the reviewed plan")
        if approval.confirmation_phrase != plan.confirmation_phrase:
            raise ReleasePermitError("approval confirmation phrase does not match the plan")
        confirmation_now = self.clock()
        if plan.intent.mode is ExecutionMode.SHADOW:
            raise RiskLimitError(
                "shadow mode is observational only; confirm is blocked before submit"
            )
        if plan.intent.mode in {ExecutionMode.LIVE_CANARY, ExecutionMode.LIVE_LIMITED}:
            self._require_live_permit(plan=plan, approval=approval, now=confirmation_now)
            self._ensure_live_stream_health(qualification=plan.qualification, stage="confirm")
        self._ensure_new_orders_allowed()
        self._ensure_gtd_orders_still_live(orders=plan.intent.orders, now=confirmation_now)
        fresh_preflight = await self.venue.preflight(
            VenuePreflightRequest(
                mode=plan.intent.mode,
                qualification=plan.qualification,
                orders=plan.intent.orders,
                risk_context=plan.intent.risk_context,
                idempotency_key=approval.idempotency_key,
                now=confirmation_now,
            )
        )
        if not fresh_preflight.ok:
            raise RiskLimitError(
                "venue preflight blocked confirm: " + ", ".join(fresh_preflight.blocking_reasons)
            )
        reserved_permit = None
        if self._is_live_mode(plan.intent.mode):
            permit = approval.release_permit
            assert permit is not None
            assert self.permit_authority is not None
            self.permit_authority.reserve(permit, now=confirmation_now)
            reserved_permit = permit
        self._plan_confirmations[plan_id] = approval.idempotency_key
        previous_records: dict[str, OrderRecord] = {}
        submitting_records: list[OrderRecord] = []
        for order in plan.intent.orders:
            reviewed = self._orders[order.client_order_id]
            previous_records[order.client_order_id] = reviewed
            approved = reviewed.transition(
                OrderLifecycleStatus.APPROVED,
                updated_at=self.clock(),
            )
            submitting = approved.transition(
                OrderLifecycleStatus.SUBMITTING,
                updated_at=self.clock(),
            )
            self._orders[order.client_order_id] = submitting
            submitting_records.append(submitting)
        try:
            await self._run_submit_journal(
                plan=plan,
                approval=approval,
                orders=tuple(submitting_records),
            )
        except Exception:
            if reserved_permit is not None and self.permit_authority is not None:
                self.permit_authority.release(reserved_permit)
            for client_order_id, record in previous_records.items():
                self._orders[client_order_id] = record
            raise
        try:
            batch = await self.venue.submit(
                orders=plan.intent.orders,
                mode=plan.intent.mode,
                idempotency_key=approval.idempotency_key,
            )
        except Exception:
            # Venue adapters must return AMBIGUOUS for an outcome-unknown
            # mutation.  A raised error is therefore definitive and can roll
            # back the pre-submit journal before a same-key retry.
            if reserved_permit is not None and self.permit_authority is not None:
                self.permit_authority.release(reserved_permit)
            rollback_records = tuple(
                previous_records[order.client_order_id] for order in plan.intent.orders
            )
            try:
                await self._run_submit_journal(
                    plan=plan,
                    approval=approval,
                    orders=rollback_records,
                )
            except Exception:
                self._blocked_reasons.add("submit_rollback_journal_failed")
                raise ReconciliationRequiredError(
                    "submit failed and the rollback journal could not be persisted"
                ) from None
            for client_order_id, record in previous_records.items():
                self._orders[client_order_id] = record
            raise
        updated_records: list[OrderRecord] = []
        for order, result in zip(plan.intent.orders, batch.results, strict=True):
            record = self._orders[order.client_order_id]
            target_status = result.status
            updated = record.transition(target_status, updated_at=batch.submitted_at)
            if result.venue_order_id is not None:
                updated = OrderRecord(
                    client_order_id=updated.client_order_id,
                    spec=updated.spec,
                    status=updated.status,
                    plan_id=updated.plan_id,
                    venue_order_id=result.venue_order_id,
                    parent_order_id=updated.parent_order_id,
                    fills=updated.fills,
                    reject_reason=result.reject_reason,
                    submitted_at=batch.submitted_at,
                    updated_at=batch.submitted_at,
                )
            else:
                updated = OrderRecord(
                    client_order_id=updated.client_order_id,
                    spec=updated.spec,
                    status=updated.status,
                    plan_id=updated.plan_id,
                    venue_order_id=updated.venue_order_id,
                    parent_order_id=updated.parent_order_id,
                    fills=updated.fills,
                    reject_reason=result.reject_reason,
                    submitted_at=batch.submitted_at,
                    updated_at=batch.submitted_at,
                )
            self._orders[order.client_order_id] = updated
            updated_records.append(updated)
            if result.status is OrderLifecycleStatus.AMBIGUOUS:
                self._blocked_reasons.add("unresolved_ambiguous_submission")
        ticket = ExecutionTicket(
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            approved_at=batch.submitted_at,
            orders=tuple(updated_records),
            results=batch.results,
        )
        if reserved_permit is not None and self.permit_authority is not None:
            self.permit_authority.consume(reserved_permit)
        self._idempotent_confirms[approval.idempotency_key] = ticket
        return ticket

    async def intervene(
        self,
        action: InterventionAction,
        actor: str,
        idempotency_key: str,
    ) -> InterventionResult:
        del actor
        payload_hash = self._payload_hash(
            {
                "kind": action.kind.value,
                "order_ids": list(action.order_ids),
                "replacement_orders": [order.to_document() for order in action.replacement_orders],
                "reason": action.reason,
            }
        )
        cached = self._remember_idempotent(
            self._idempotent_interventions,
            self._intervention_payloads,
            idempotency_key,
            payload_hash,
        )
        if cached is not None:
            return cached
        if action.kind is InterventionKind.RECONCILE:
            result = await self._reconcile_orders()
        elif action.kind is InterventionKind.CANCEL:
            result = await self._cancel_orders(action.order_ids, idempotency_key, action.reason)
        elif action.kind is InterventionKind.CANCEL_ALL:
            target_ids = tuple(
                order.venue_order_id
                for order in self._orders.values()
                if order.venue_order_id is not None and order.status in LIVEISH_STATUSES
            )
            result = await self._cancel_orders(target_ids, idempotency_key, action.reason)
        elif action.kind is InterventionKind.REPLACE:
            if self._is_live_mode(self.mode):
                raise ReleasePermitError(
                    "live replace requires a new explicit review/confirm cycle "
                    "with a release permit"
                )
            await self._cancel_orders(action.order_ids, idempotency_key + ":cancel", action.reason)
            self._ensure_new_orders_allowed()
            replacement_intent = ExecutionIntent(
                strategy_id=action.replacement_orders[0].strategy_id,
                mode=self.mode,
                orders=tuple(action.replacement_orders),
                risk_context=RiskContext(),
                note="replace",
            )
            plan = await self.review(
                replacement_intent,
                actor="replace",
                idempotency_key=idempotency_key + ":review",
            )
            confirmation = ExecutionApproval(
                actor="replace",
                idempotency_key=idempotency_key + ":confirm",
                plan_hash=plan.plan_hash,
                confirmation_phrase=plan.confirmation_phrase,
                release_permit=None,
            )
            ticket = await self.confirm(plan.plan_id, confirmation)
            result = InterventionResult(
                action=InterventionKind.REPLACE,
                completed_at=ticket.approved_at,
                orders=ticket.orders,
            )
        else:
            result = await self._engage_kill(reason=action.reason or "manual_kill")
        self._idempotent_interventions[idempotency_key] = result
        return result

    async def _cancel_orders(
        self,
        venue_order_ids: Sequence[str],
        idempotency_key: str,
        reason: str | None,
    ) -> InterventionResult:
        target_ids = set(venue_order_ids)
        for record in self._orders.values():
            if record.venue_order_id in target_ids and record.status in {
                OrderLifecycleStatus.LIVE,
                OrderLifecycleStatus.DELAYED,
                OrderLifecycleStatus.MATCHED,
                OrderLifecycleStatus.PARTIALLY_FILLED,
                OrderLifecycleStatus.ATTENTION_REQUIRED,
            }:
                self._orders[record.client_order_id] = record.transition(
                    OrderLifecycleStatus.CANCEL_REQUESTED,
                    updated_at=self.clock(),
                )
        canceled = await self.venue.cancel(
            order_ids=tuple(venue_order_ids),
            idempotency_key=idempotency_key,
            reason=reason,
        )
        affected: list[OrderRecord] = []
        for record in list(self._orders.values()):
            if record.venue_order_id in canceled.canceled_ids:
                updated = record.transition(
                    OrderLifecycleStatus.CANCELED,
                    updated_at=canceled.canceled_at,
                )
                self._orders[updated.client_order_id] = updated
                affected.append(updated)
            elif (
                record.venue_order_id in canceled.unresolved_ids
                and record.status is OrderLifecycleStatus.CANCEL_REQUESTED
            ):
                updated = record.transition(
                    OrderLifecycleStatus.RECONCILING,
                    updated_at=canceled.canceled_at,
                )
                self._orders[updated.client_order_id] = updated
                affected.append(updated)
        return InterventionResult(
            action=InterventionKind.CANCEL if venue_order_ids else InterventionKind.CANCEL_ALL,
            completed_at=canceled.canceled_at,
            orders=tuple(affected),
            canceled_ids=canceled.canceled_ids,
            remaining_ids=canceled.unresolved_ids,
        )

    def _reconcile_open_order_ids(self) -> tuple[str, ...] | None:
        open_order_ids = tuple(
            dict.fromkeys(
                order.venue_order_id
                for order in self._orders.values()
                if order.venue_order_id is not None and order.status in LIVEISH_STATUSES
            )
        )
        return open_order_ids or None

    async def _reconcile_orders(self) -> InterventionResult:
        snapshot = await self.venue.reconcile(open_order_ids=self._reconcile_open_order_ids())
        self._last_reconcile_at = snapshot.as_of
        for state in snapshot.open_orders:
            self._apply_venue_state(state, authoritative_reconciliation=True)
        for state in snapshot.resolved_orders:
            self._apply_venue_state(state, authoritative_reconciliation=True)
        for fill in snapshot.fills:
            self._apply_fill(fill)
        if snapshot.backfill_complete:
            self._blocked_reasons.discard("user_stream_backfill_pending")
        else:
            self._blocked_reasons.add("user_stream_backfill_pending")
        if snapshot.discrepancies:
            self._blocked_reasons.add("reconciliation_mismatch")
        else:
            self._blocked_reasons.discard("reconciliation_mismatch")
        unresolved = tuple(
            record.client_order_id
            for record in self._orders.values()
            if record.status
            in {
                OrderLifecycleStatus.AMBIGUOUS,
                OrderLifecycleStatus.RECONCILING,
                OrderLifecycleStatus.CANCEL_REQUESTED,
                OrderLifecycleStatus.SUBMITTING,
            }
        )
        if not unresolved and snapshot.backfill_complete and not snapshot.discrepancies:
            self._blocked_reasons.discard("restart_reconcile_required")
            self._blocked_reasons.discard("unresolved_ambiguous_submission")
        return InterventionResult(
            action=InterventionKind.RECONCILE,
            completed_at=snapshot.as_of,
            orders=tuple(self._orders.values()),
            reconciliation=snapshot,
        )

    def _transition_from_authoritative_reconciliation(
        self,
        *,
        record: OrderRecord,
        status: OrderLifecycleStatus,
        updated_at: datetime,
    ) -> OrderRecord:
        try:
            return record.transition(status, updated_at=updated_at)
        except OrderValidationError as exc:
            try:
                bridged = record.transition(
                    OrderLifecycleStatus.RECONCILING,
                    updated_at=updated_at,
                )
                return bridged.transition(status, updated_at=updated_at)
            except OrderValidationError:
                raise exc from None

    def _apply_venue_state(
        self,
        state: VenueOrderState,
        *,
        authoritative_reconciliation: bool = False,
    ) -> None:
        for client_order_id, record in list(self._orders.items()):
            if (
                record.venue_order_id == state.venue_order_id
                or client_order_id == state.client_order_id
            ):
                if record.status is state.status:
                    updated = OrderRecord(
                        client_order_id=record.client_order_id,
                        spec=record.spec,
                        status=record.status,
                        plan_id=record.plan_id,
                        venue_order_id=state.venue_order_id,
                        parent_order_id=record.parent_order_id,
                        fills=record.fills,
                        reject_reason=state.reject_reason,
                        submitted_at=record.submitted_at,
                        updated_at=state.updated_at,
                    )
                else:
                    updated = (
                        self._transition_from_authoritative_reconciliation(
                            record=record,
                            status=state.status,
                            updated_at=state.updated_at,
                        )
                        if authoritative_reconciliation
                        else record.transition(state.status, updated_at=state.updated_at)
                    )
                    updated = OrderRecord(
                        client_order_id=updated.client_order_id,
                        spec=updated.spec,
                        status=updated.status,
                        plan_id=updated.plan_id,
                        venue_order_id=state.venue_order_id,
                        parent_order_id=updated.parent_order_id,
                        fills=updated.fills,
                        reject_reason=state.reject_reason,
                        submitted_at=updated.submitted_at,
                        updated_at=state.updated_at,
                    )
                self._orders[client_order_id] = updated

    def _apply_fill(self, fill: FillRecord) -> None:
        for client_order_id, record in list(self._orders.items()):
            if record.venue_order_id == fill.venue_order_id:
                self._orders[client_order_id] = record.with_fill(fill)

    async def _engage_kill(self, *, reason: str) -> InterventionResult:
        self._killed = True
        self._kill_reason = reason
        self._blocked_reasons.add("persistent_kill")
        self.kill_store.persist({"killed": True, "reason": reason})
        open_ids = tuple(
            order.venue_order_id
            for order in self._orders.values()
            if order.venue_order_id is not None and order.status in LIVEISH_STATUSES
        )
        canceled = await self.venue.cancel(
            order_ids=open_ids,
            idempotency_key=f"kill:{reason}",
            reason=reason,
        )
        now = canceled.canceled_at
        affected: list[OrderRecord] = []
        for key, record in list(self._orders.items()):
            if record.status is OrderLifecycleStatus.KILLED:
                affected.append(record)
                continue
            target = record.status
            if target is OrderLifecycleStatus.DRAFT:
                updated = record.transition(OrderLifecycleStatus.KILLED, updated_at=now)
            else:
                updated = OrderRecord(
                    client_order_id=record.client_order_id,
                    spec=record.spec,
                    status=OrderLifecycleStatus.KILLED,
                    plan_id=record.plan_id,
                    venue_order_id=record.venue_order_id,
                    parent_order_id=record.parent_order_id,
                    fills=record.fills,
                    reject_reason=record.reject_reason,
                    submitted_at=record.submitted_at,
                    updated_at=now,
                )
            self._orders[key] = updated
            affected.append(updated)
        reconciliation = await self.venue.reconcile()
        self._last_reconcile_at = reconciliation.as_of
        return InterventionResult(
            action=InterventionKind.KILL,
            completed_at=now,
            orders=tuple(affected),
            canceled_ids=canceled.canceled_ids,
            remaining_ids=canceled.unresolved_ids,
            reconciliation=reconciliation,
        )

    def ingest_user_event(self, event: VenueUserEvent) -> None:
        if event.kind is VenueEventKind.GAP:
            self._last_user_event_at = event.occurred_at.astimezone(UTC)
            self._blocked_reasons.add("user_stream_gap")
            self._blocked_reasons.add("heartbeat_failed")
        elif event.kind is VenueEventKind.HEARTBEAT:
            self._mark_stream_fresh(event.occurred_at)
        elif event.kind is VenueEventKind.BACKFILL:
            self._user_stream_backfill_complete = True
            self._blocked_reasons.discard("user_stream_backfill_pending")
            self._mark_stream_fresh(event.occurred_at)
            self._blocked_reasons.discard("restart_reconcile_required")
        elif event.kind is VenueEventKind.ORDER and event.order is not None:
            self._mark_stream_fresh(event.occurred_at)
            self._apply_venue_state(event.order)
        elif event.kind is VenueEventKind.TRADE and event.fill is not None:
            self._mark_stream_fresh(event.occurred_at)
            self._apply_fill(event.fill)
        else:
            self._mark_stream_fresh(event.occurred_at)

    def snapshot(self, scope: str | None = None) -> ExecutionSnapshot:
        del scope
        unresolved = tuple(
            record.client_order_id
            for record in self._orders.values()
            if record.status
            in {
                OrderLifecycleStatus.AMBIGUOUS,
                OrderLifecycleStatus.RECONCILING,
                OrderLifecycleStatus.SUBMITTING,
                OrderLifecycleStatus.CANCEL_REQUESTED,
            }
        )
        return ExecutionSnapshot(
            mode=ExecutionMode.KILLED if self._killed else self.mode,
            killed=self._killed,
            blocked_reasons=tuple(sorted(self._blocked_reasons)),
            last_reconcile_at=self._last_reconcile_at,
            last_user_event_at=self._last_user_event_at,
            last_heartbeat_at=self._last_heartbeat_at,
            user_stream_backfill_complete=self._user_stream_backfill_complete,
            plans=tuple(self._plans.values()),
            orders=tuple(self._orders.values()),
            unresolved_order_ids=unresolved,
        )
