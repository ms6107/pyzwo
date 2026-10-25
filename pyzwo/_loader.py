# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shared library loader, exceptions, and validation helpers for pyzwo."""

from __future__ import annotations

import ctypes
import math
import os
import platform
import sys
from ctypes.util import find_library
from enum import StrEnum
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent


class DeviceState(StrEnum):
    """Lifecycle and connection state of a synchronous ZWO controller."""

    DISCONNECTED = "disconnected"
    READY = "ready"
    BUSY = "busy"
    FAULT = "fault"
    UNKNOWN = "unknown"


class ZwoError(RuntimeError):
    """Error raised when a ZWO C SDK function returns a non-zero error code."""

    def __init__(
        self,
        operation: str,
        code: int,
        code_name: str,
    ) -> None:
        self.operation = operation
        self.code = code
        self.code_name = code_name
        self.rollback_error: BaseException | None = None
        super().__init__(f"{operation} failed with {code_name} (rc={code})")


def attach_rollback_error(init_exc: BaseException, dev_id: int, close_exc: BaseException) -> None:
    """Attach a rollback close failure to an initialization exception without masking it."""
    note = f"rollback close(id={dev_id}) also failed: {close_exc}"
    init_exc.add_note(note)
    if isinstance(init_exc, (ZwoError, ZwoTimeoutError)):
        init_exc.rollback_error = close_exc


def validate_poll_and_timeout(poll_interval: float, timeout: float) -> None:
    """Validate that poll_interval and timeout are positive finite numbers."""
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError(f"poll_interval must be a positive finite number, got {poll_interval!r}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")


class ZwoTimeoutError(TimeoutError):
    """Error raised when a Python-level polling loop exceeds its monotonic deadline.

    Note: In the synchronous baseline, deadlines are checked between SDK calls; a
    blocked native C call inside the vendor library has no pre-emptive timeout.
    """

    rollback_error: BaseException | None = None


def _arch_subdir() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "mac"
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "armv8"
    if machine.startswith("armv7"):
        return "armv7"
    if machine.startswith("armv6"):
        return "armv6"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    return machine


def load_zwo_library(lib_stem: str, explicit_path: str | None = None) -> ctypes.CDLL:
    """Load a ZWO shared library (e.g. 'EFWFilter') across Linux/macOS/Windows.

    On Linux, preloads ``libudev.so.1`` with ``RTLD_GLOBAL`` because ZWO's USB HID shared
    libraries reference ``udev_*`` symbols without always declaring ``DT_NEEDED`` for ``libudev``.
    """
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL("libudev.so.1", mode=ctypes.RTLD_GLOBAL)
        except OSError:
            udev_found = find_library("udev")
            if udev_found:
                ctypes.CDLL(udev_found, mode=ctypes.RTLD_GLOBAL)

    if explicit_path:
        return ctypes.CDLL(explicit_path)

    env_key = f"PYZWO_{lib_stem.upper()}_LIB"
    if os.environ.get(env_key):
        return ctypes.CDLL(os.environ[env_key])

    if sys.platform.startswith("linux"):
        filename = f"lib{lib_stem}.so"
    elif sys.platform == "darwin":
        filename = f"lib{lib_stem}.dylib"
    else:
        filename = f"{lib_stem}.dll"

    candidates = [
        Path("/usr/local/lib") / filename,
        Path("/usr/lib") / filename,
        _PACKAGE_DIR / "lib" / _arch_subdir() / filename,
    ]
    for path in candidates:
        if path.exists():
            return ctypes.CDLL(str(path))

    found = find_library(lib_stem)
    if found:
        return ctypes.CDLL(found)

    return ctypes.CDLL(filename)
