#!/usr/bin/env python3.11
"""Grant V0.7 read-only ACLs through stable no-follow file descriptors."""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
from pathlib import Path

SOURCE_RUNS_ROOT = Path(
    "/var/lib/poly-mm-v06/data/"
    "btc_5m_15m_relative_value_paper_v06_linux_2026-08-14/runs"
)
SERVICE_USER = "polybotv07"
PROSPECTIVE_CUTOFF_SECONDS = 1_786_892_400
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _open_directory(
    name: str,
    *,
    parent_fd: int,
    expected_device: int,
) -> int:
    descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode) or status.st_dev != expected_device:
        os.close(descriptor)
        raise RuntimeError("source ACL directory escaped its filesystem")
    return descriptor


def _open_regular_single_link(
    name: str,
    *,
    parent_fd: int,
    expected_device: int,
) -> int:
    descriptor = os.open(name, FILE_FLAGS, dir_fd=parent_fd)
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_dev != expected_device
    ):
        os.close(descriptor)
        raise RuntimeError(
            "source ACL target is not a same-device single-link regular file"
        )
    return descriptor


def _setfacl_via_descriptor(
    descriptor: int,
    *,
    operation: str,
    acl: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["setfacl", operation, acl, f"/proc/self/fd/{descriptor}"],
        check=True,
        capture_output=True,
        text=True,
        pass_fds=(descriptor,),
    )


def _revoke_acl(descriptor: int) -> None:
    _setfacl_via_descriptor(
        descriptor,
        operation="-x",
        acl=f"u:{SERVICE_USER}",
    )


def _path_status(name: str, *, parent_fd: int) -> os.stat_result:
    return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)


def _grant_checkpoint_acl(
    checkpoint_name: str,
    *,
    checkpoint_root_fd: int,
    expected_device: int,
) -> None:
    descriptor = _open_regular_single_link(
        checkpoint_name,
        parent_fd=checkpoint_root_fd,
        expected_device=expected_device,
    )
    try:
        before = os.fstat(descriptor)
        current_before = _path_status(
            checkpoint_name,
            parent_fd=checkpoint_root_fd,
        )
        identity = (before.st_dev, before.st_ino)
        if (
            (current_before.st_dev, current_before.st_ino) != identity
            or current_before.st_dev != expected_device
            or not stat.S_ISREG(current_before.st_mode)
            or current_before.st_nlink != 1
        ):
            raise RuntimeError("source ACL target changed before repair")
        granted = False
        try:
            _setfacl_via_descriptor(
                descriptor,
                operation="-m",
                acl=f"u:{SERVICE_USER}:r--",
            )
            granted = True
            after = os.fstat(descriptor)
            current = _path_status(
                checkpoint_name,
                parent_fd=checkpoint_root_fd,
            )
            if (
                (after.st_dev, after.st_ino) != identity
                or (current.st_dev, current.st_ino) != identity
                or after.st_dev != expected_device
                or current.st_dev != expected_device
                or not stat.S_ISREG(after.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or after.st_nlink != 1
                or current.st_nlink != 1
            ):
                raise RuntimeError("source ACL target changed during repair")
            completed = subprocess.run(
                ["getfacl", "-cp", f"/proc/self/fd/{descriptor}"],
                check=True,
                capture_output=True,
                text=True,
                pass_fds=(descriptor,),
            )
            acl_lines = set(completed.stdout.splitlines())
            if (
                f"user:{SERVICE_USER}:r--" not in acl_lines
                or not any(line.startswith("mask::r") for line in acl_lines)
                or stat.S_IMODE(after.st_mode) & stat.S_IWGRP
            ):
                raise RuntimeError(
                    "source ACL target is not effectively read-only"
                )
        except BaseException:
            if granted:
                _revoke_acl(descriptor)
            raise
    finally:
        os.close(descriptor)


def _repair_attempt(
    attempt_name: str,
    *,
    expiry_fd: int,
    expected_device: int,
) -> None:
    attempt_fd = _open_directory(
        attempt_name,
        parent_fd=expiry_fd,
        expected_device=expected_device,
    )
    try:
        try:
            summary_fd = _open_regular_single_link(
                "capture-summary.json",
                parent_fd=attempt_fd,
                expected_device=expected_device,
            )
        except FileNotFoundError:
            return
        else:
            os.close(summary_fd)
        try:
            checkpoint_root_fd = _open_directory(
                "checkpoints",
                parent_fd=attempt_fd,
                expected_device=expected_device,
            )
        except FileNotFoundError:
            return
        try:
            for checkpoint_name in sorted(os.listdir(checkpoint_root_fd)):
                if not checkpoint_name.endswith(".json"):
                    continue
                _grant_checkpoint_acl(
                    checkpoint_name,
                    checkpoint_root_fd=checkpoint_root_fd,
                    expected_device=expected_device,
                )
        finally:
            os.close(checkpoint_root_fd)
    finally:
        os.close(attempt_fd)


def main() -> int:
    if SOURCE_RUNS_ROOT.resolve(strict=True) != SOURCE_RUNS_ROOT:
        raise RuntimeError("the frozen V0.6 source runs root is invalid")
    pwd.getpwnam(SERVICE_USER)
    root_fd = os.open(SOURCE_RUNS_ROOT, DIRECTORY_FLAGS)
    try:
        root_device = os.fstat(root_fd).st_dev
        for expiry_name in sorted(os.listdir(root_fd)):
            try:
                expiry_seconds = int(expiry_name)
            except ValueError:
                continue
            if expiry_seconds < PROSPECTIVE_CUTOFF_SECONDS:
                continue
            expiry_fd = _open_directory(
                expiry_name,
                parent_fd=root_fd,
                expected_device=root_device,
            )
            try:
                for attempt_name in sorted(os.listdir(expiry_fd)):
                    _repair_attempt(
                        attempt_name,
                        expiry_fd=expiry_fd,
                        expected_device=root_device,
                    )
            finally:
                os.close(expiry_fd)
    finally:
        os.close(root_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
