"""Operational schedule helpers for complete BTC TWAP paper evidence."""


_CYCLE_MS = 15 * 60 * 1_000
_DEFAULT_STARTUP_LEAD_SECONDS = 6 * 60
_DEFAULT_CLOCK_REFRESH_LEAD_SECONDS = 60


def next_bootstrap_expiry_seconds(
    now_ms: int,
    *,
    startup_lead_seconds: int = _DEFAULT_STARTUP_LEAD_SECONDS,
) -> int:
    """Return the first expiry whose opening can be observed after startup.

    The selected 15-minute market opens one boundary before its expiry.  That
    opening is required for exact Chainlink anchoring, so a new process targets
    an expiry whose opening is still at least six minutes away.  This affects
    capture availability only; no strategy or validation threshold changes.
    """

    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    if (
        isinstance(startup_lead_seconds, bool)
        or not isinstance(startup_lead_seconds, int)
        or not 0 <= startup_lead_seconds < _CYCLE_MS // 1_000
    ):
        raise ValueError("startup_lead_seconds must be inside [0, 900)")
    next_boundary_ms = (now_ms // _CYCLE_MS + 1) * _CYCLE_MS
    if next_boundary_ms - now_ms < startup_lead_seconds * 1_000:
        next_boundary_ms += _CYCLE_MS
    return next_boundary_ms // 1_000 + _CYCLE_MS // 1_000


def bootstrap_clock_refresh_at_ms(
    expiry_seconds: int,
    *,
    refresh_lead_seconds: int = _DEFAULT_CLOCK_REFRESH_LEAD_SECONDS,
) -> int:
    """Schedule fresh clock evidence shortly before the 15-minute opening."""

    cycle_seconds = _CYCLE_MS // 1_000
    if (
        isinstance(expiry_seconds, bool)
        or not isinstance(expiry_seconds, int)
        or expiry_seconds < cycle_seconds
        or expiry_seconds % cycle_seconds != 0
    ):
        raise ValueError("expiry_seconds must be a positive 15-minute boundary")
    if (
        isinstance(refresh_lead_seconds, bool)
        or not isinstance(refresh_lead_seconds, int)
        or not 0 <= refresh_lead_seconds < cycle_seconds
    ):
        raise ValueError("refresh_lead_seconds must be inside [0, 900)")
    opening_seconds = expiry_seconds - cycle_seconds
    return (opening_seconds - refresh_lead_seconds) * 1_000
