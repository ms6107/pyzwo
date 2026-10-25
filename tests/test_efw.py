# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyzwo.hid and pyzwo.efw."""

from __future__ import annotations

import math
import sys
import unittest
from types import TracebackType
from unittest.mock import patch

from pyzwo import Efw, EfwStatus, ZwoError

DEV_PATH = "/dev/hidraw10"
SLOT_COUNT = 7


def _status_report(
    *,
    state: int = 0x01,
    error_code: int = 0x00,
    slots: tuple[int, int, int] = (1, 1, 1),
    slot_count: int = SLOT_COUNT,
    dual_disc: int = 0x00,
) -> bytes:
    target_slot, prev_slot, next_slot = slots
    return bytes(
        (
            0x01,
            0x7E,
            0x5A,
            0x01,
            state,
            error_code,
            target_slot,
            prev_slot,
            next_slot,
            slot_count,
            0,
            0,
            dual_disc,
            0,
            0,
            0,
            0,
        )
    )


class FakeHidBackend:
    """In-memory fake for the `hid` module and `hid.Device`."""

    def __init__(
        self,
        *,
        status_replies: list[bytes] | None = None,
        open_error: Exception | None = None,
        io_error: Exception | None = None,
    ) -> None:
        self.status_replies = list(status_replies or [_status_report(slots=(3, 3, 3))])
        self.open_error = open_error
        self.io_error = io_error
        self.open_count = 0
        self.close_count = 0
        self.sent_reports: list[bytes] = []
        self._patcher = patch.dict(sys.modules, {"hid": self})

    def __enter__(self) -> FakeHidBackend:
        self._patcher.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._patcher.stop()

    def enumerate(self, vid: int, pid: int) -> list[dict[str, bytes]]:
        if vid == Efw.VID and pid == 0x1F01:
            return [{"path": b"/dev/hidraw11"}, {"path": b"/dev/hidraw10"}]
        if vid == Efw.VID and pid == 0x1F02:
            return [{"path": b"/dev/hidraw10"}]
        return []

    def Device(self, *, path: bytes) -> FakeHidDevice:
        if self.open_error is not None:
            raise self.open_error
        self.open_count += 1
        return FakeHidDevice(self, path)


class FakeHidDevice:
    """Single short-lived HID device handle created by FakeHidBackend.Device."""

    def __init__(self, backend: FakeHidBackend, path: bytes) -> None:
        self._backend = backend
        self.path = path
        self.closed = False

    def send_feature_report(self, data: bytes) -> int:
        if self._backend.io_error is not None:
            raise self._backend.io_error
        self._backend.sent_reports.append(bytes(data))
        return len(data)

    def get_feature_report(self, report_id: int, size: int) -> bytes:
        if self._backend.io_error is not None:
            raise self._backend.io_error
        if len(self._backend.status_replies) > 1:
            return self._backend.status_replies.pop(0)
        return self._backend.status_replies[0]

    def close(self) -> None:
        self.closed = True
        self._backend.close_count += 1


class TestEfw(unittest.TestCase):
    def test_discover_deduplicates_and_sorts_paths(self) -> None:
        with FakeHidBackend():
            self.assertEqual(Efw.discover(), ["/dev/hidraw10", "/dev/hidraw11"])

    def test_get_status_settled_and_closes_handle(self) -> None:
        with FakeHidBackend(status_replies=[_status_report(slots=(5, 5, 5))]) as backend:
            status = Efw(DEV_PATH).get_status()

        self.assertEqual(status, EfwStatus(slot_count=SLOT_COUNT, slot=5))
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)
        self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x01".ljust(16, b"\x00")])

    def test_get_status_moving_returns_minus_one(self) -> None:
        cases = [
            ("state_zero", _status_report(state=0x00, slots=(0, 0, 0))),
            ("sensor_mismatch", _status_report(state=0x01, slots=(2, 3, 3))),
        ]
        for label, report in cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]):
                self.assertEqual(Efw(DEV_PATH).get_status(), EfwStatus(slot_count=SLOT_COUNT, slot=-1))

    def test_get_status_protocol_and_hardware_errors_raise_zwo_error(self) -> None:
        cases = [
            ("bad_header", bytes(17)),
            ("bad_reply_type", b"\x01\x7e\x5a\x02".ljust(17, b"\x00")),
            ("dual_disc", _status_report(dual_disc=1)),
            ("hw_error", _status_report(state=0x06, error_code=2)),
            ("zero_slots", _status_report(slot_count=0)),
            ("too_many_slots", _status_report(slot_count=17)),
        ]
        for label, report in cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]) as backend:
                with self.assertRaises(ZwoError):
                    Efw(DEV_PATH).get_status()
                self.assertEqual(backend.open_count, backend.close_count)

    def test_hid_open_and_io_exceptions_wrap_into_zwo_error(self) -> None:
        with FakeHidBackend(open_error=OSError("permission denied")), self.assertRaises(ZwoError):
            Efw(DEV_PATH).get_status()

        with FakeHidBackend(io_error=OSError("input/output error")) as io_backend:
            with self.assertRaises(ZwoError):
                Efw(DEV_PATH).get_status()
            self.assertEqual(io_backend.open_count, 1)
            self.assertEqual(io_backend.close_count, 1)

    def test_move_to_slot_validates_arguments(self) -> None:
        wheel = Efw(DEV_PATH)
        with FakeHidBackend(status_replies=[_status_report(slots=(1, 1, 1))]):
            for bad_timeout in (0.0, -1.0, math.nan, math.inf):
                with self.subTest(timeout=bad_timeout), self.assertRaises(ValueError):
                    wheel.move_to_slot(1, timeout=bad_timeout)

            for bad_slot in (0, -1, SLOT_COUNT + 1):
                with self.subTest(slot=bad_slot), self.assertRaises(ValueError):
                    wheel.move_to_slot(bad_slot)

    def test_move_to_slot_rejects_when_already_moving(self) -> None:
        with FakeHidBackend(status_replies=[_status_report(state=0x00, slots=(0, 0, 0))]) as backend:
            with self.assertRaises(ZwoError):
                Efw(DEV_PATH).move_to_slot(2)
            self.assertEqual(len(backend.sent_reports), 1)

    def test_move_to_slot_bidirectional_and_unidirectional(self) -> None:
        for unidirectional, expected_dir in ((False, 0x02), (True, 0x03)):
            with self.subTest(unidirectional=unidirectional):
                replies = [
                    _status_report(slots=(1, 1, 1)),
                    _status_report(state=0x00, slots=(0, 0, 0)),
                    _status_report(slots=(6, 6, 6)),
                ]
                with FakeHidBackend(status_replies=replies) as backend, patch("pyzwo.efw.time.sleep"):
                    settled = Efw(DEV_PATH).move_to_slot(6, unidirectional=unidirectional)

                self.assertEqual(settled, 6)
                self.assertEqual(
                    backend.sent_reports[1],
                    bytes((0x03, 0x7E, 0x5A, 0x01, expected_dir, 6)).ljust(16, b"\x00"),
                )
                self.assertEqual(backend.open_count, backend.close_count)

    def test_move_to_slot_timeout_raises_builtin_timeout_error(self) -> None:
        replies = [
            _status_report(slots=(1, 1, 1)),
            _status_report(state=0x00, slots=(0, 0, 0)),
        ]
        with (
            FakeHidBackend(status_replies=replies),
            patch("pyzwo.efw.time.sleep"),
            patch("pyzwo.efw.time.monotonic", side_effect=[0.0, 1.0]),
            self.assertRaises(TimeoutError),
        ):
            Efw(DEV_PATH).move_to_slot(4, timeout=0.5)


if __name__ == "__main__":
    unittest.main()
