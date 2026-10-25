# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Stateless path-based USB HID transport for ZWO astronomy devices.

ZWO USB HID devices communicate via HID Feature Reports over USB Control
Endpoint 0 using a shared `7e 5a` (`"~Z"`) framing protocol:

- **Host -> Device (`SET_REPORT`, Report ID `0x03`, 16 bytes):**
  `03 7e 5a <payload> 00 ...` (zero-padded to 16 bytes).
- **Device -> Host (`GET_REPORT`, Report ID `0x01`, 17 bytes):**
  `01 7e 5a <selector> <data ...>`, where `reply[3]` echoes `payload[-1]`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_CMD_HEADER = b"\x03\x7e\x5a"
_CMD_LEN = 16

_REPLY_REPORT_ID = 0x01
_REPLY_HEADER = b"\x01\x7e\x5a"
_REPLY_LEN = 17
_QUERY_ATTEMPTS = 3


class ZwoError(Exception):
    """Error raised when a ZWO USB HID operation or protocol check fails."""


class ZwoQueryCollisionError(ZwoError):
    """Error raised when concurrent queries repeatedly overwrite the MCU reply buffer."""


def _get_hid() -> Any:
    """Import `hid` lazily so unit tests can run without `libhidapi` installed."""
    import hid  # type: ignore[import-untyped]

    return hid


@contextmanager
def _open_hid(path: bytes) -> Iterator[Any]:
    try:
        device = _get_hid().Device(path=path)
        try:
            yield device
        finally:
            device.close()
    except Exception as exc:
        raise ZwoError(f"HID I/O failed on {path!r}: {exc}") from exc


class ZwoHid:
    """Stateless path-based base class for ZWO USB HID devices (`03 7e 5a` framing).

    Each `_send()` or `_query()` opens the OS HID device node by path, performs
    the feature-report transaction, and closes the handle immediately.
    """

    VID = 0x03C3
    PIDS: tuple[int, ...] = ()

    def __init__(self, path: str) -> None:
        self.path = path
        self._raw_path = path.encode("utf-8")

    @classmethod
    def discover(cls) -> list[str]:
        """Return sorted HID device paths matching `cls.VID` and `cls.PIDS`."""
        backend = _get_hid()
        return sorted(
            {
                info["path"].decode("utf-8", errors="replace")
                for pid in cls.PIDS
                for info in backend.enumerate(cls.VID, pid)
            }
        )

    def _send(self, payload: bytes) -> None:
        """Open device, send a 16-byte `[03 7e 5a <payload>...]` feature report, and close."""
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        with _open_hid(self._raw_path) as device:
            device.send_feature_report(report)

    def _query(self, payload: bytes) -> bytes:
        """Open device, send `[03 7e 5a <payload>...]`, read 17-byte `[01 7e 5a <selector>...]` reply, and close.

        `send_feature_report` (`SET_REPORT`) is a synchronous USB Control Transfer (~3 ms)
        that populates the MCU's single 17-byte `0x01` reply register in RAM before
        returning, and `get_feature_report` (`GET_REPORT`, ~3 ms) reads that register
        non-destructively.

        If two queries with different selectors interleave (`A: send -> B: send -> A: get -> B: get`),
        `B`'s send overwrites the MCU reply register so both reads return `B`'s reply (never swapped).
        `B` matches `reply[3] == payload[-1]` on attempt 1 and exits, leaving `A` uncontended to
        succeed on the immediate next attempt without sleep or backoff. Raises
        `ZwoQueryCollisionError` if all `_QUERY_ATTEMPTS` attempts collide. (Verified on
        Linux `hidraw` only so far; macOS/Windows HID backends are untested.)
        """
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        with _open_hid(self._raw_path) as device:
            for _ in range(_QUERY_ATTEMPTS):
                device.send_feature_report(report)
                reply = bytes(device.get_feature_report(_REPLY_REPORT_ID, _REPLY_LEN))
                if not reply.startswith(_REPLY_HEADER):
                    raise ZwoError(f"Unexpected HID reply for {payload.hex(' ')}: {reply.hex(' ')}")
                # Every query echoes its selector (payload[-1]) in reply[3]; a mismatch means
                # a concurrent query overwrote the MCU's single reply buffer before our read.
                if reply[3] == payload[-1]:
                    return reply
        raise ZwoQueryCollisionError(f"HID query collision for {payload.hex(' ')}: {reply.hex(' ')}")
