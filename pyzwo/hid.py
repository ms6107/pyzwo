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

hid: Any = None


class ZwoError(Exception):
    """Error raised when a ZWO USB HID operation or protocol check fails."""


def _get_hid() -> Any:
    global hid
    if hid is None:
        import hid as _hid  # type: ignore[import-untyped]

        hid = _hid
    return hid


@contextmanager
def _open_hid(path: bytes) -> Iterator[Any]:
    try:
        dev = _get_hid().Device(path=path)
    except Exception as exc:
        raise ZwoError(f"Failed to open HID device {path!r}: {exc}") from exc
    try:
        yield dev
    except ZwoError:
        raise
    except Exception as exc:
        raise ZwoError(f"HID I/O failed on {path!r}: {exc}") from exc
    finally:
        dev.close()


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
        report = (b"\x03\x7e\x5a" + payload).ljust(16, b"\x00")
        with _open_hid(self._raw_path) as dev:
            dev.send_feature_report(report)

    def _query(self, selector: int) -> bytes:
        """Open device, send [03 7e 5a 02 <selector>], read 17-byte [01 7e 5a <selector>...] reply, and close."""
        cmd = (b"\x03\x7e\x5a\x02" + bytes((selector,))).ljust(16, b"\x00")
        with _open_hid(self._raw_path) as dev:
            dev.send_feature_report(cmd)
            rx = bytes(dev.get_feature_report(0x01, 17))
        if rx[:4] != b"\x01\x7e\x5a" + bytes((selector,)):
            raise ZwoError(f"Unexpected HID reply for selector 0x{selector:02x}: {rx.hex(' ')}")
        return rx
