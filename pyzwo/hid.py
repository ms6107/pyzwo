# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Stateless path-based USB HID base class for ZWO astronomy devices."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_CMD_HEADER = b"\x03\x7e\x5a"
_CMD_LEN = 16

_REPLY_REPORT_ID = 0x01
_REPLY_HEADER = b"\x01\x7e\x5a"
_REPLY_LEN = 17


class ZwoError(Exception):
    """Error raised when a ZWO USB HID operation or protocol check fails."""


def _get_hid() -> Any:
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
    """Stateless path-based base class for ZWO USB HID devices (03 7e 5a framing)."""

    VID = 0x03C3
    PIDS: tuple[int, ...] = ()

    def __init__(self, path: str) -> None:
        self.path = path
        self._raw_path = path.encode("utf-8")

    @classmethod
    def discover(cls) -> list[str]:
        """Return sorted HID device paths matching cls.VID and cls.PIDS."""
        backend = _get_hid()
        return sorted(
            {
                info["path"].decode("utf-8", errors="replace")
                for pid in cls.PIDS
                for info in backend.enumerate(cls.VID, pid)
            }
        )

    def _send(self, payload: bytes) -> None:
        """Open device, send a 16-byte [03 7e 5a <payload>...] feature report, and close."""
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        with _open_hid(self._raw_path) as device:
            device.send_feature_report(report)

    def _query(self, payload: bytes) -> bytes:
        """Open device, send [03 7e 5a <payload>...], read 17-byte [01 7e 5a <selector>...] reply, and close."""
        report = (_CMD_HEADER + payload).ljust(_CMD_LEN, b"\x00")
        with _open_hid(self._raw_path) as device:
            device.send_feature_report(report)
            reply = bytes(device.get_feature_report(_REPLY_REPORT_ID, _REPLY_LEN))
        if reply[:4] != _REPLY_HEADER + payload[-1:]:
            raise ZwoError(f"Unexpected HID reply for {payload.hex(' ')}: {reply.hex(' ')}")
        return reply
