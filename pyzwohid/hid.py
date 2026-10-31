# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based USB HID transport for ZWO astronomy devices.

ZWO USB HID devices communicate via HID Feature Reports over USB Control
Endpoint 0 using a shared `7e 5a` (`"~Z"`) framing protocol:

- **Host -> Device (`SET_REPORT`, Report ID `0x03`, 16 bytes):**
  `03 7e 5a <payload> 00 ...` (zero-padded to 16 bytes).
- **Device -> Host (`GET_REPORT`, Report ID `0x01`, 16–17 bytes):**
  `01 7e 5a <selector> <data ...>`, where `reply[3]` echoes `payload[-1]`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Self, TypeVar

_CMD_HEADER = b"\x03\x7e\x5a"
_CMD_LEN = 16

_REPLY_REPORT_ID = 0x01
_REPLY_HEADER = b"\x01\x7e\x5a"
_REPLY_LEN = 17
_MIN_REPLY_LEN = 16
_QUERY_ATTEMPTS = 3

# All hidapi calls execute on a single dedicated worker thread so that hid_init,
# hid_enumerate, hid_open_path, hid_close, and per-device feature-report I/O never
# race on hidapi's unsynchronized global state or on an open hid_device handle.
_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_T = TypeVar("_T")


async def _run_in_executor(func: Callable[..., _T], *args: Any) -> _T:
    return await asyncio.get_running_loop().run_in_executor(_EXECUTOR, func, *args)


class ZwoError(Exception):
    """Error raised when a ZWO USB HID operation or protocol check fails."""


class ZwoDevicePathError(ZwoError):
    """Error raised when a path does not match an enumerated device for the ZWO HID class."""


class ZwoQueryCollisionError(ZwoError):
    """Error raised when concurrent queries from another OS process repeatedly overwrite the MCU reply buffer."""


def _get_hid() -> Any:
    """Import `hid` lazily so unit tests can run without `libhidapi` installed."""
    import hid  # type: ignore[import-untyped]

    return hid


class ZwoHid:
    """Path-based base class for ZWO USB HID devices (`03 7e 5a` framing).

    `await cls.open(path)` opens the OS HID device node and holds the handle open
    across `_send()` and `_query()` calls until `await close()` (or `async with` exit).
    Holding the handle open avoids the ~45-50 ms Linux `hidraw` first-open /
    last-close penalty (`hid_hw_open` / `usb_kill_urb` on the interrupt URB), and
    all `hidapi` C calls are serialized on `_EXECUTOR` (1 worker thread).
    """

    VID = 0x03C3
    PIDS: tuple[int, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.PIDS:
            raise TypeError(f"{cls.__name__} must define a non-empty PIDS tuple")

    def __init__(self, path: str, device: Any) -> None:
        self.path = path
        self._device = device

    @classmethod
    def _open_device(cls, path: str) -> Any:
        raw_path = os.fsencode(path)
        if os.path.islink(raw_path):
            raw_path = os.path.realpath(raw_path)
        backend = _get_hid()
        try:
            valid_paths = {info["path"] for pid in cls.PIDS for info in backend.enumerate(cls.VID, pid)}
            if raw_path in valid_paths:
                return backend.Device(path=raw_path)
        except Exception as exc:
            raise ZwoError(f"HID open failed on {path!r}: {exc}") from exc
        raise ZwoDevicePathError(f"Device at {path!r} does not match {cls.__name__}")

    @staticmethod
    def _close_unclaimed(raw_future: Future[Any]) -> None:
        if raw_future.exception() is None:
            raw_future.result().close()

    @classmethod
    async def open(cls, path: str) -> Self:
        """Verify and open the OS HID device node at `path`."""
        raw_future = _EXECUTOR.submit(cls._open_device, path)
        try:
            device = await asyncio.shield(asyncio.wrap_future(raw_future))
        except BaseException:
            if not raw_future.cancel() and (not raw_future.done() or raw_future.exception() is None):
                _EXECUTOR.submit(cls._close_unclaimed, raw_future)
            raise
        return cls(path, device)

    def _close_device(self) -> None:
        self._device.close()

    async def close(self) -> None:
        """Close the underlying HID device handle."""
        await _run_in_executor(self._close_device)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @classmethod
    def _discover_paths(cls) -> list[str]:
        backend = _get_hid()
        return sorted({os.fsdecode(info["path"]) for pid in cls.PIDS for info in backend.enumerate(cls.VID, pid)})

    @classmethod
    async def discover(cls) -> list[str]:
        """Return sorted HID device paths matching `cls.VID` and `cls.PIDS`."""
        return await _run_in_executor(cls._discover_paths)

    def _send_sync(self, payload: bytes) -> None:
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        try:
            self._device.send_feature_report(report)
        except Exception as exc:
            raise ZwoError(f"HID I/O failed on {self.path!r}: {exc}") from exc

    async def _send(self, payload: bytes) -> None:
        """Send a 16-byte `[03 7e 5a <payload>...]` feature report."""
        await _run_in_executor(self._send_sync, payload)

    def _query_sync(self, payload: bytes) -> bytes:
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        for _ in range(_QUERY_ATTEMPTS):
            try:
                self._device.send_feature_report(report)
                reply = bytes(self._device.get_feature_report(_REPLY_REPORT_ID, _REPLY_LEN))
            except Exception as exc:
                raise ZwoError(f"HID I/O failed on {self.path!r}: {exc}") from exc
            if len(reply) < _MIN_REPLY_LEN or not reply.startswith(_REPLY_HEADER):
                raise ZwoError(f"Unexpected HID reply for {payload.hex(' ')}: {reply.hex(' ')}")
            # Every query echoes its selector (payload[-1]) in reply[3]; a mismatch means
            # another OS process overwrote the MCU's single reply buffer before our read.
            if reply[3] == payload[-1]:
                return reply
        raise ZwoQueryCollisionError(f"HID query collision for {payload.hex(' ')}: {reply.hex(' ')}")

    async def _query(self, payload: bytes) -> bytes:
        """Send `[03 7e 5a <payload>...]` and read its `[01 7e 5a <selector>...]` reply.

        `send_feature_report` (`SET_REPORT`) is a synchronous USB Control Transfer (~3 ms)
        that populates the MCU's single `0x01` reply register in RAM before returning, and
        `get_feature_report` (`GET_REPORT`, ~3 ms) reads that register non-destructively
        (requesting up to 17 bytes as in the vendor SDK; the EFW USB HID report descriptor
        declares Report ID `0x01` as 15 payload bytes + 1 report ID byte = 16 bytes).

        Within this process, `_EXECUTOR` serializes each `_query_sync` call end-to-end.

        If two independent OS processes open the same HID device and their queries with
        different selectors interleave (`A: send -> B: send -> A: get -> B: get`), `B`'s
        send overwrites the MCU reply register so both reads return `B`'s reply (never
        swapped). `B` matches `reply[3] == payload[-1]` on attempt 1 and exits, leaving
        `A` uncontended to succeed on the immediate next attempt without sleep or backoff.
        Raises `ZwoQueryCollisionError` if all `_QUERY_ATTEMPTS` attempts collide.
        (Verified on Linux `hidraw` only so far; macOS/Windows HID backends are untested.)
        """
        return await _run_in_executor(self._query_sync, payload)
