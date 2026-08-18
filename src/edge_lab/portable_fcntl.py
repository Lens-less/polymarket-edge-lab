"""Small cross-platform flock shim for edge-lab utilities.

The production services use POSIX ``fcntl.flock``. Windows developers and
collectors may import these modules without ever exercising the lock path, so
we provide a no-op fallback that preserves importability.
"""

from __future__ import annotations

try:  # pragma: no cover - host-specific import
    import fcntl as _fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows
    LOCK_EX = 1
    LOCK_SH = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def flock(fd: int, operation: int) -> None:
        _ = (fd, operation)

else:  # pragma: no cover - thin re-export
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_SH = _fcntl.LOCK_SH
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN
    flock = _fcntl.flock


__all__ = ["LOCK_EX", "LOCK_NB", "LOCK_SH", "LOCK_UN", "flock"]
