"""Small cross-platform advisory-file-lock shim for edge-lab utilities."""

from __future__ import annotations

try:  # pragma: no cover - host-specific import
    import fcntl as _fcntl
except ModuleNotFoundError:  # pragma: no cover - exercised on Windows
    import msvcrt
    import os

    LOCK_EX = 1
    LOCK_SH = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def flock(fd: int, operation: int) -> None:
        """Approximate ``flock`` with a one-byte Windows locking region."""

        os.lseek(fd, 0, os.SEEK_SET)
        if operation & LOCK_UN:
            mode = msvcrt.LK_UNLCK
        elif operation & LOCK_NB:
            mode = msvcrt.LK_NBLCK
        else:
            mode = msvcrt.LK_LOCK
        try:
            msvcrt.locking(fd, mode, 1)
        except OSError as exc:
            if operation & LOCK_NB:
                raise BlockingIOError(str(exc)) from exc
            raise

else:  # pragma: no cover - thin re-export
    LOCK_EX = _fcntl.LOCK_EX
    # A few Windows-only tests inject a minimal fcntl stub. Shared locking is
    # unused by the service runner, so preserve the stub while supplying the
    # conventional bit value for modules imported later in the same process.
    LOCK_SH = getattr(_fcntl, "LOCK_SH", 2)
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN
    flock = _fcntl.flock


__all__ = ["LOCK_EX", "LOCK_NB", "LOCK_SH", "LOCK_UN", "flock"]
