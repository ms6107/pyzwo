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

from pyzwo import Efw, EfwStatus, ZwoDevicePathError, ZwoError, ZwoQueryCollisionError

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

    DEVICES: tuple[tuple[int, int, bytes], ...] = (
        (Efw.VID, 0x1F01, b"/dev/hidraw11"),
        (Efw.VID, 0x1F01, b"/dev/hidraw10"),
        (Efw.VID, 0x1F02, b"/dev/hidraw10"),
        (Efw.VID, 0x1F10, b"/dev/hidraw12"),  # ZWO EAF focuser (same VID, different PID)
        (0x1234, 0x1F01, b"/dev/hidraw13"),  # Non-ZWO HID device (different VID, same PID)
    )

    def enumerate(self, vid: int = 0, pid: int = 0) -> list[dict[str, int | bytes]]:
        return [
            {"vendor_id": d_vid, "product_id": d_pid, "path": d_path}
            for d_vid, d_pid, d_path in self.DEVICES
            if vid in (0, d_vid) and pid in (0, d_pid)
        ]

    def Device(self, *, path: bytes) -> FakeHidDevice:
        if self.open_error is not None:
            raise self.open_error
        self.open_count += 1
        return FakeHidDevice(self, path)


class FakeHidDevice:
    """HID device handle created by FakeHidBackend.Device."""

    def __init__(self, backend: FakeHidBackend, path: bytes) -> None:
        self._backend = backend
        self.path = path
        self.closed = False

    def send_feature_report(self, data: bytes) -> int:
        if self.closed:
            raise OSError("device closed")
        if self._backend.io_error is not None:
            raise self._backend.io_error
        self._backend.sent_reports.append(bytes(data))
        return len(data)

    def get_feature_report(self, report_id: int, size: int) -> bytes:
        if self.closed:
            raise OSError("device closed")
        if self._backend.io_error is not None:
            raise self._backend.io_error
        if len(self._backend.status_replies) > 1:
            return self._backend.status_replies.pop(0)
        return self._backend.status_replies[0]

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._backend.close_count += 1


class TestEfw(unittest.TestCase):
    def test_discover_deduplicates_and_sorts_paths(self) -> None:
        with FakeHidBackend():
            self.assertEqual(Efw.discover(), ["/dev/hidraw10", "/dev/hidraw11"])

    def test_get_status_settled_and_closes_handle(self) -> None:
        with FakeHidBackend(status_replies=[_status_report(slots=(5, 5, 5))]) as backend:
            with Efw.open(DEV_PATH) as wheel:
                status = wheel.get_status()
            wheel.close()  # idempotent second close

        self.assertEqual(status, EfwStatus(slot_count=SLOT_COUNT, slot=5))
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)
        self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x01".ljust(16, b"\x00")])

    def test_get_status_calibrating_and_moving_return_minus_one(self) -> None:
        calibrating_cases = [
            ("calibrating_power_on", _status_report(state=0x00, slots=(0, 0, 0), slot_count=0)),
            ("calibrating_magnet_counter", _status_report(state=0x00, slots=(3, 3, 3), slot_count=2)),
        ]
        for label, report in calibrating_cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]), Efw.open(DEV_PATH) as wheel:
                self.assertEqual(wheel.get_status(), EfwStatus(slot_count=-1, slot=-1))

        moving_cases = [
            ("rotating_between_slots", _status_report(state=0x04, slots=(6, 2, 3))),
            ("fine_damping_at_target", _status_report(state=0x04, slots=(6, 6, 6))),
            ("inter_command_handoff", _status_report(state=0x01, slots=(2, 3, 3))),
        ]
        for label, report in moving_cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]), Efw.open(DEV_PATH) as wheel:
                self.assertEqual(wheel.get_status(), EfwStatus(slot_count=SLOT_COUNT, slot=-1))

    def test_get_status_protocol_and_hardware_errors_raise_zwo_error(self) -> None:
        cases = [
            ("bad_header", bytes(17)),
            ("dual_disc", _status_report(dual_disc=1)),
            ("hw_error", _status_report(state=0x06, error_code=2)),
            ("zero_slots", _status_report(slot_count=0)),
            ("too_many_slots", _status_report(slot_count=17)),
        ]
        for label, report in cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]) as backend:
                with self.assertRaises(ZwoError), Efw.open(DEV_PATH) as wheel:
                    wheel.get_status()
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

    def test_query_retries_on_selector_collision(self) -> None:
        other_reply = b"\x01\x7e\x5a\x04".ljust(17, b"\x00")
        replies = [other_reply, other_reply, _status_report(slots=(4, 4, 4))]
        with FakeHidBackend(status_replies=replies) as backend:
            with Efw.open(DEV_PATH) as wheel:
                self.assertEqual(wheel.get_status(), EfwStatus(slot_count=SLOT_COUNT, slot=4))
            self.assertEqual(len(backend.sent_reports), 3)
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

    def test_query_raises_collision_error_after_exhausting_retries(self) -> None:
        other_reply = b"\x01\x7e\x5a\x04".ljust(17, b"\x00")
        with FakeHidBackend(status_replies=[other_reply]) as backend:
            with self.assertRaises(ZwoError) as ctx, Efw.open(DEV_PATH) as wheel:
                wheel.get_status()
            self.assertIsInstance(ctx.exception, ZwoQueryCollisionError)
            self.assertEqual(len(backend.sent_reports), 3)
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

    def test_hid_open_and_io_exceptions_wrap_into_zwo_error(self) -> None:
        with FakeHidBackend(open_error=OSError("permission denied")), self.assertRaises(ZwoError):
            Efw.open(DEV_PATH)

        with FakeHidBackend(io_error=OSError("input/output error")) as io_backend:
            with self.assertRaises(ZwoError), Efw.open(DEV_PATH) as wheel:
                wheel.get_status()
            self.assertEqual(io_backend.open_count, 1)
            self.assertEqual(io_backend.close_count, 1)

        with FakeHidBackend():
            closed_wheel = Efw.open(DEV_PATH)
            closed_wheel.close()
            with self.assertRaises(ZwoError):
                closed_wheel.get_status()

    def test_open_rejects_non_matching_device_path(self) -> None:
        for path in (
            "/dev/hidraw12",  # existing ZWO EAF (same VID 0x03C3, non-EFW PID 0x1F10)
            "/dev/hidraw13",  # existing non-ZWO device (wrong VID 0x1234, PID 0x1F01)
            "/dev/hidraw99",  # non-existent path
        ):
            with self.subTest(path=path), FakeHidBackend() as backend:
                with self.assertRaises(ZwoError) as ctx:
                    Efw.open(path)
                self.assertIsInstance(ctx.exception, ZwoDevicePathError)
                self.assertEqual(backend.open_count, 0)

    def test_move_to_slot_validates_arguments(self) -> None:
        with FakeHidBackend(status_replies=[_status_report(slots=(1, 1, 1))]), Efw.open(DEV_PATH) as wheel:
            for bad_timeout in (0.0, -1.0, math.nan, math.inf):
                with self.subTest(timeout=bad_timeout), self.assertRaises(ValueError):
                    wheel.move_to_slot(1, timeout=bad_timeout)
                with self.subTest(calibrate_timeout=bad_timeout), self.assertRaises(ValueError):
                    wheel.calibrate(timeout=bad_timeout)

            for bad_slot in (0, -1, SLOT_COUNT + 1):
                with self.subTest(slot=bad_slot), self.assertRaises(ValueError):
                    wheel.move_to_slot(bad_slot)

    def test_move_to_slot_and_calibrate_reject_when_already_moving_or_calibrating(self) -> None:
        busy_reports = [
            ("calibrating", _status_report(state=0x00, slots=(3, 3, 3), slot_count=1)),
            ("rotating", _status_report(state=0x04, slots=(6, 2, 3))),
        ]
        for label, report in busy_reports:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]) as backend:
                with Efw.open(DEV_PATH) as wheel:
                    with self.assertRaises(ZwoError):
                        wheel.move_to_slot(5)
                    with self.assertRaises(ZwoError):
                        wheel.calibrate()
                self.assertEqual(len(backend.sent_reports), 2)

    def test_move_to_slot_bidirectional_and_unidirectional(self) -> None:
        for unidirectional, expected_dir in ((False, 0x02), (True, 0x03)):
            with self.subTest(unidirectional=unidirectional):
                replies = [
                    _status_report(slots=(1, 1, 1)),
                    _status_report(state=0x04, slots=(6, 2, 3)),
                    _status_report(slots=(6, 6, 6)),
                ]
                with (
                    FakeHidBackend(status_replies=replies) as backend,
                    patch("pyzwo.efw.time.sleep"),
                    Efw.open(DEV_PATH) as wheel,
                ):
                    settled = wheel.move_to_slot(6, unidirectional=unidirectional)

                self.assertEqual(settled, 6)
                self.assertEqual(
                    backend.sent_reports[1],
                    bytes((0x03, 0x7E, 0x5A, 0x01, expected_dir, 6)).ljust(16, b"\x00"),
                )
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

    def test_calibrate_sends_command_and_polls_until_settled(self) -> None:
        replies = [
            _status_report(slots=(3, 3, 3)),
            _status_report(state=0x00, slots=(3, 3, 3), slot_count=2),
            _status_report(state=0x00, slots=(1, 3, 1), slot_count=SLOT_COUNT),
            _status_report(slots=(1, 1, 1)),
        ]
        with (
            FakeHidBackend(status_replies=replies) as backend,
            patch("pyzwo.efw.time.sleep"),
            Efw.open(DEV_PATH) as wheel,
        ):
            settled = wheel.calibrate()

        self.assertEqual(settled, 1)
        self.assertEqual(
            backend.sent_reports[1],
            b"\x03\x7e\x5a\x01\x01".ljust(16, b"\x00"),
        )
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)

    def test_move_to_slot_and_calibrate_timeout_raise_builtin_timeout_error(self) -> None:
        replies = [
            _status_report(slots=(1, 1, 1)),
            _status_report(state=0x00, slots=(0, 0, 0)),
        ]
        with (
            FakeHidBackend(status_replies=list(replies)),
            patch("pyzwo.efw.time.sleep"),
            patch("pyzwo.efw.time.monotonic", side_effect=[0.0, 1.0]),
            self.assertRaises(TimeoutError),
            Efw.open(DEV_PATH) as wheel,
        ):
            wheel.move_to_slot(4, timeout=0.5)

        with (
            FakeHidBackend(status_replies=list(replies)),
            patch("pyzwo.efw.time.sleep"),
            patch("pyzwo.efw.time.monotonic", side_effect=[0.0, 1.0]),
            self.assertRaises(TimeoutError),
            Efw.open(DEV_PATH) as wheel,
        ):
            wheel.calibrate(timeout=0.5)

    def test_clear_error_sends_command(self) -> None:
        with FakeHidBackend() as backend, Efw.open(DEV_PATH) as wheel:
            wheel.clear_error()

        self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x01\x0f".ljust(16, b"\x00")])
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)


if __name__ == "__main__":
    unittest.main()
