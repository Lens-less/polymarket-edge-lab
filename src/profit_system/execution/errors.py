from __future__ import annotations


class ExecutionError(RuntimeError):
    """Base class for execution-engine failures."""


class ConfigurationError(ExecutionError):
    """Raised when required execution configuration is missing."""


class OrderValidationError(ExecutionError, ValueError):
    """Raised when an order or plan is structurally invalid."""


class IdempotencyConflictError(ExecutionError):
    """Raised when one idempotency key is reused for a different mutation."""


class QualificationError(ExecutionError):
    """Raised when a strategy is not qualified for a requested mode."""


class ReleasePermitError(ExecutionError):
    """Raised when live release authorization is missing or invalid."""


class RiskLimitError(ExecutionError):
    """Raised when risk limits block a new order."""


class VenuePolicyError(ExecutionError):
    """Raised when the venue rejects a request for policy reasons."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GeoblockError(VenuePolicyError):
    """Raised when the venue reports a blocked jurisdiction."""


class CloseOnlyError(VenuePolicyError):
    """Raised when the venue is in close-only mode."""


class AllowanceError(VenuePolicyError):
    """Raised when allowance or collateral is insufficient."""


class MarketDataStaleError(VenuePolicyError):
    """Raised when market data is too old for safe execution."""


class ReconciliationRequiredError(ExecutionError):
    """Raised when a mutation must reconcile before proceeding."""


class AmbiguousSubmissionError(ReconciliationRequiredError):
    """Raised when a submission outcome is unknown until reconciliation."""


class PersistentKillError(ExecutionError):
    """Raised when the engine is durably killed."""


class UserStreamGapError(ExecutionError):
    """Raised when the authenticated user stream gap breaches the limit."""


class HeartbeatError(ExecutionError):
    """Raised when the order/user stream heartbeat becomes unhealthy."""
