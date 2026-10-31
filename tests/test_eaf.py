# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyzwohid.eaf."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterator
from types import TracebackType
from unittest.mock import patch

from pyzwohid import Eaf, EafStatus, ZwoDevicePathError, ZwoError

DEV_PATH = "/dev/hidraw14"
SECOND_DEV_PATH = "/dev/hidraw15"


def _firmware_report(
    *,
    version: tuple[int, int, int] = (3, 2, 2),
    model_raw: bytes = b"\x00EAF-0-0\x00",
) -> bytes:
    major, minor, build = version
    return (bytes((0x01, 0x7E, 0x5A, 0x04, major, minor, build)) + model_raw).ljust(16, b"\x00")[:16]


def _status_report_legacy(
    *,
    state: int = 0x00,
    backlash: int = 0,
    speed: int = 512,
    step: int = 30_068,
    raw_temp: int = 0x82DD,
    flags: int = 0x32,
    max_step: int = 60_000,
) -> bytes:
    return bytes(
        (
            0x01,
            0x7E,
            0x5A,
            0x03,
            state,
            backlash,
            (speed >> 8) & 0xFF,
            speed & 0xFF,
            (step >> 8) & 0xFF,
            step & 0xFF,
            0x00,
            (raw_temp >> 8) & 0xFF,
            raw_temp & 0xFF,
            flags,
            (max_step >> 8) & 0xFF,
            max_step & 0xFF,
        )
    )


def _status_report_modern(
    *,
    state: int = 0x00,
    backlash: int = 0,
    step: int = 120_000,
    raw_temp: int = 0x82DD,
    flags: int = 0x30,
    max_step: int = 300_000,
) -> bytes:
    return bytes(
        (
            0x01,
            0x7E,
            0x5A,
            0x03,
            state,
            backlash,
            (max_step >> 16) & 0xFF,
            (step >> 16) & 0xFF,
            (step >> 8) & 0xFF,
            step & 0xFF,
            0x00,
            (raw_temp >> 8) & 0xFF,
            raw_temp & 0xFF,
            flags,
            (max_step >> 8) & 0xFF,
            max_step & 0xFF,
        )
    )


class FakeEafHidBackend:
    """In-memory fake for the `hid` module and `hid.Device` for EAF tests."""

    def __init__(
        self,
        *,
        replies: list[bytes] | None = None,
        replies_by_path: dict[bytes, list[bytes]] | None = None,
        open_error: Exception | None = None,
        io_error: Exception | None = None,
        on_open: Callable[[bytes], None] | None = None,
        on_send: Callable[[bytes, bytes], None] | None = None,
    ) -> None:
        self.replies = list(replies or [_firmware_report(), _status_report_legacy()])
        self.replies_by_path = {path: list(r) for path, r in (replies_by_path or {}).items()}
        self.open_error = open_error
        self.io_error = io_error
        self.on_open = on_open
        self.on_send = on_send
        self.open_count = 0
        self.close_count = 0
        self.sent_reports: list[bytes] = []
        self.events: list[tuple[str, bytes]] = []
        self._active_calls = 0
        self.max_concurrent_calls = 0
        self._patcher = patch.dict(sys.modules, {"hid": self})

    @contextlib.contextmanager
    def _track_call(self, op: str, path: bytes = b"") -> Iterator[None]:
        self._active_calls += 1
        self.max_concurrent_calls = max(self.max_concurrent_calls, self._active_calls)
        self.events.append((op, path))
        try:
            yield
        finally:
            self._active_calls -= 1

    def __enter__(self) -> FakeEafHidBackend:
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
        (Eaf.VID, 0x1F10, b"/dev/hidraw15"),
        (Eaf.VID, 0x1F10, b"/dev/hidraw14"),
        (Eaf.VID, 0x1F01, b"/dev/hidraw10"),  # ZWO EFW filter wheel (same VID, different PID)
        (0x1234, 0x1F10, b"/dev/hidraw16"),  # Non-ZWO HID device (different VID, same PID)
    )

    def enumerate(self, vid: int = 0, pid: int = 0) -> list[dict[str, int | bytes]]:
        with self._track_call("enumerate"):
            return [
                {"vendor_id": d_vid, "product_id": d_pid, "path": d_path}
                for d_vid, d_pid, d_path in self.DEVICES
                if vid in (0, d_vid) and pid in (0, d_pid)
            ]

    def Device(self, *, path: bytes) -> FakeEafHidDevice:
        with self._track_call("open", path):
            if self.on_open is not None:
                self.on_open(path)
            if self.open_error is not None:
                raise self.open_error
            self.open_count += 1
            return FakeEafHidDevice(self, path)


class FakeEafHidDevice:
    """HID device handle created by FakeEafHidBackend.Device."""

    def __init__(self, backend: FakeEafHidBackend, path: bytes) -> None:
        self._backend = backend
        self.path = path
        self.closed = False

    def send_feature_report(self, data: bytes) -> int:
        with self._backend._track_call("send", self.path):
            if self.closed:
                raise OSError("device closed")
            if self._backend.io_error is not None:
                raise self._backend.io_error
            payload = bytes(data)
            self._backend.sent_reports.append(payload)
            if self._backend.on_send is not None:
                self._backend.on_send(self.path, payload)
            return len(data)

    def get_feature_report(self, report_id: int, size: int) -> bytes:
        with self._backend._track_call("get", self.path):
            if self.closed:
                raise OSError("device closed")
            if self._backend.io_error is not None:
                raise self._backend.io_error
            replies = self._backend.replies_by_path.get(self.path, self._backend.replies)
            if len(replies) > 1:
                return replies.pop(0)
            return replies[0]

    def close(self) -> None:
        with self._backend._track_call("close", self.path):
            if not self.closed:
                self.closed = True
                self._backend.close_count += 1


class TestEaf(unittest.IsolatedAsyncioTestCase):
    async def test_discover_deduplicates_and_sorts_paths(self) -> None:
        with FakeEafHidBackend():
            self.assertEqual(await Eaf.discover(), ["/dev/hidraw14", "/dev/hidraw15"])

    async def test_open_reads_firmware_and_model_for_legacy_and_modern_versions(self) -> None:
        cases = [
            ("legacy_3_2_2", (3, 2, 2), b"\x00EAF-0-0\x00", "EAF-0-0", 60_000),
            ("legacy_boundary_3_3_5", (3, 3, 5), b"\x00EAF-S-0\x00", "EAF-S-0", 60_000),
            ("modern_3_3_7", (3, 3, 7), b"EEAF-S-0\x00", "EEAF-S-0", 600_000),
        ]
        for label, version, model_raw, expected_model, expected_range in cases:
            with (
                self.subTest(case=label),
                FakeEafHidBackend(replies=[_firmware_report(version=version, model_raw=model_raw)]) as backend,
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    self.assertEqual(focuser.path, DEV_PATH)
                    self.assertEqual(focuser.firmware_version, version)
                    self.assertEqual(focuser.model, expected_model)
                    self.assertEqual(focuser.step_range, expected_range)
                await focuser.close()  # idempotent second close

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x04".ljust(16, b"\x00")])

    async def test_open_closes_handle_on_firmware_query_failure_or_cancellation(self) -> None:
        error_cases = [
            ("zero_firmware_version", [_firmware_report(version=(0, 0, 0))]),
            ("corrupt_reply", [bytes(16)]),
        ]
        for label, replies in error_cases:
            with self.subTest(case=label), FakeEafHidBackend(replies=replies) as backend:
                with self.assertRaises(ZwoError):
                    await Eaf.open(DEV_PATH)
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

        with self.subTest(case="io_error_during_firmware_query"):
            with FakeEafHidBackend(io_error=OSError("usb error")) as backend:
                with self.assertRaises(ZwoError):
                    await Eaf.open(DEV_PATH)
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

        with self.subTest(case="cancelled_during_firmware_query"):
            entered = threading.Event()
            release = threading.Event()

            def block_send(_path: bytes, _payload: bytes) -> None:
                entered.set()
                release.wait(timeout=5.0)

            with FakeEafHidBackend(on_send=block_send) as backend:
                open_task = asyncio.create_task(Eaf.open(DEV_PATH))
                self.assertTrue(await asyncio.to_thread(entered.wait, 5.0))

                open_task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(backend.close_count, 0)

                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await open_task

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.max_concurrent_calls, 1)

    async def test_open_rejects_non_matching_device_path_and_resolves_symlink(self) -> None:
        for bad_path in (
            "/dev/hidraw10",  # ZWO EFW (same VID 0x03C3, non-EAF PID 0x1F01)
            "/dev/hidraw16",  # Non-ZWO device (wrong VID 0x1234, PID 0x1F10)
            "/dev/hidraw99",  # Non-existent path
        ):
            with self.subTest(path=bad_path), FakeEafHidBackend() as backend:
                with self.assertRaises(ZwoDevicePathError):
                    await Eaf.open(bad_path)
                self.assertEqual(backend.open_count, 0)

        with tempfile.TemporaryDirectory() as tmpdir, FakeEafHidBackend() as backend:
            link_path = os.path.join(tmpdir, "zwo-eaf")
            os.symlink(DEV_PATH, link_path)
            async with await Eaf.open(link_path) as focuser:
                self.assertEqual(focuser.path, link_path)
                self.assertEqual(focuser.firmware_version, (3, 2, 2))
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

    async def test_get_status_decodes_legacy_16bit_and_modern_24bit_reports(self) -> None:
        cases = [
            (
                "legacy_settled",
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, step=30_068, max_step=60_000, flags=0x32),
                EafStatus(step=30_068, max_step=60_000, moving=False),
            ),
            (
                "legacy_motor_moving",
                _firmware_report(version=(3, 3, 5)),
                _status_report_legacy(state=0x01, step=12_345, max_step=50_000, flags=0x00),
                EafStatus(step=12_345, max_step=50_000, moving=True),
            ),
            (
                "modern_settled_24bit",
                _firmware_report(version=(3, 3, 7), model_raw=b"EEAF-S-0\x00"),
                _status_report_modern(state=0x00, step=123_456, max_step=600_000, flags=0x30),
                EafStatus(step=123_456, max_step=600_000, moving=False),
            ),
            (
                "modern_motor_moving_24bit",
                _firmware_report(version=(3, 4, 0), model_raw=b"EEAF-S-0\x00"),
                _status_report_modern(state=0x01, step=250_000, max_step=400_000, flags=0x00),
                EafStatus(step=250_000, max_step=400_000, moving=True),
            ),
        ]
        for label, fw_report, status_report, expected in cases:
            with self.subTest(case=label), FakeEafHidBackend(replies=[fw_report, status_report]) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    status = await focuser.get_status()
                self.assertEqual(status, expected)
                self.assertEqual(
                    backend.sent_reports,
                    [
                        b"\x03\x7e\x5a\x02\x04".ljust(16, b"\x00"),
                        b"\x03\x7e\x5a\x02\x03".ljust(16, b"\x00"),
                    ],
                )

    async def test_get_status_protocol_and_hardware_errors_raise_zwo_error(self) -> None:
        cases = [
            ("hardware_error_state_06", _firmware_report(version=(3, 2, 2)), _status_report_legacy(state=0x06)),
            ("unknown_state_02", _firmware_report(version=(3, 2, 2)), _status_report_legacy(state=0x02)),
            ("zero_max_step", _firmware_report(version=(3, 2, 2)), _status_report_legacy(step=0, max_step=0)),
            (
                "legacy_max_step_exceeds_60k",
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(step=0, max_step=60_001),
            ),
            (
                "modern_max_step_exceeds_600k",
                _firmware_report(version=(3, 3, 7)),
                _status_report_modern(step=0, max_step=600_001),
            ),
            (
                "step_exceeds_max_step",
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(step=40_001, max_step=40_000),
            ),
        ]
        for label, fw_report, bad_status in cases:
            with self.subTest(case=label), FakeEafHidBackend(replies=[fw_report, bad_status]) as backend:
                with self.assertRaises(ZwoError):
                    async with await Eaf.open(DEV_PATH) as focuser:
                        await focuser.get_status()
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

    async def test_reset_position_legacy_and_modern_preserves_device_settings(self) -> None:
        with self.subTest("legacy_16bit"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, backlash=15, speed=512, step=30_068, flags=0x32, max_step=60_000),
            ]
            with FakeEafHidBackend(replies=replies) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    await focuser.reset_position(25_000)

            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,  # run = 0
                        15,  # backlash preserved
                        0x02,
                        0x00,  # speed = 512 preserved
                        (25_000 >> 8) & 0xFF,
                        25_000 & 0xFF,
                        0x01,  # opcode = reset_position
                        0x00,
                        0x00,
                        0x32,  # flags preserved
                        0xEA,
                        0x60,  # max_step = 60_000 preserved
                    )
                ),
            )

        with self.subTest("modern_24bit"):
            replies = [
                _firmware_report(version=(3, 3, 7), model_raw=b"EEAF-S-0\x00"),
                _status_report_modern(state=0x00, backlash=8, step=120_000, flags=0x41, max_step=450_000),
            ]
            with FakeEafHidBackend(replies=replies) as backend:
                async with await Eaf.open(DEV_PATH) as focuser:
                    await focuser.reset_position(200_000)

            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,  # run = 0
                        8,  # backlash preserved
                        (450_000 >> 16) & 0xFF,  # max_step upper byte preserved
                        (200_000 >> 16) & 0xFF,
                        (200_000 >> 8) & 0xFF,
                        200_000 & 0xFF,
                        0x01,  # opcode = reset_position
                        0x00,
                        0x00,
                        0x41,  # flags preserved
                        (450_000 >> 8) & 0xFF,
                        450_000 & 0xFF,  # max_step lower 16 bits preserved
                    )
                ),
            )

    async def test_reset_position_validates_bounds_and_rejects_when_moving(self) -> None:
        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                for bad_step in (-1, 40_001):
                    with self.subTest(step=bad_step), self.assertRaises(ValueError):
                        await focuser.reset_position(bad_step)
            # Only open(02 04) + 2 status queries(02 03); no 03 control report sent
            self.assertEqual(len(backend.sent_reports), 3)

        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x01, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                with self.assertRaises(ZwoError):
                    await focuser.reset_position(10_000)
            self.assertEqual(len(backend.sent_reports), 2)

    async def test_move_legacy_and_modern_sends_command_and_polls_until_settled(self) -> None:
        with self.subTest("legacy_16bit"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, backlash=4, speed=512, step=30_000, flags=0x32, max_step=60_000),
                _status_report_legacy(state=0x01, backlash=4, speed=512, step=30_050, flags=0x32, max_step=60_000),
                _status_report_legacy(state=0x00, backlash=4, speed=512, step=30_100, flags=0x32, max_step=60_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep"),
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.move(30_100)

            self.assertEqual(settled, 30_100)
            self.assertEqual(
                backend.sent_reports[2],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x01,  # run = 1
                        4,  # backlash preserved
                        0x02,
                        0x00,  # speed = 512 preserved
                        (30_100 >> 8) & 0xFF,
                        30_100 & 0xFF,
                        0x00,  # opcode = move/stop
                        0x00,
                        0x00,
                        0x32,  # flags preserved
                        0xEA,
                        0x60,  # max_step = 60_000 preserved
                    )
                ),
            )

        with self.subTest("modern_24bit"):
            replies = [
                _firmware_report(version=(3, 3, 7), model_raw=b"EEAF-S-0\x00"),
                _status_report_modern(state=0x00, backlash=10, step=100_000, flags=0x40, max_step=500_000),
                _status_report_modern(state=0x01, backlash=10, step=150_000, flags=0x40, max_step=500_000),
                _status_report_modern(state=0x00, backlash=10, step=200_000, flags=0x40, max_step=500_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep"),
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.move(200_000)

            self.assertEqual(settled, 200_000)
            self.assertEqual(
                backend.sent_reports[2],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x01,  # run = 1
                        10,  # backlash preserved
                        (500_000 >> 16) & 0xFF,
                        (200_000 >> 16) & 0xFF,
                        (200_000 >> 8) & 0xFF,
                        200_000 & 0xFF,
                        0x00,  # opcode = move/stop
                        0x00,
                        0x00,
                        0x40,
                        (500_000 >> 8) & 0xFF,
                        500_000 & 0xFF,
                    )
                ),
            )

    async def test_stop_when_idle_and_while_moving(self) -> None:
        with self.subTest("already_settled"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, backlash=2, speed=512, step=30_068, flags=0x32, max_step=60_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.stop()

            self.assertEqual(settled, 30_068)
            mock_sleep.assert_not_awaited()
            self.assertEqual(
                backend.sent_reports[-1],
                bytes(
                    (
                        0x03,
                        0x7E,
                        0x5A,
                        0x03,
                        0x00,  # run = 0
                        2,
                        0x02,
                        0x00,
                        (30_068 >> 8) & 0xFF,
                        30_068 & 0xFF,
                        0x00,  # opcode = move/stop
                        0x00,
                        0x00,
                        0x32,
                        0xEA,
                        0x60,
                    )
                ),
            )

        with self.subTest("moving_polls_until_settled"):
            replies = [
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x01, step=30_050, max_step=60_000),
                _status_report_legacy(state=0x01, step=30_055, max_step=60_000),
                _status_report_legacy(state=0x00, step=30_058, max_step=60_000),
            ]
            with (
                FakeEafHidBackend(replies=replies) as backend,
                patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            ):
                async with await Eaf.open(DEV_PATH) as focuser:
                    settled = await focuser.stop()

            self.assertEqual(settled, 30_058)
            self.assertEqual(mock_sleep.await_count, 2)

    async def test_move_and_stop_validate_arguments_and_reject_busy_move(self) -> None:
        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x00, step=10_000, max_step=40_000),
            ]
        ):
            async with await Eaf.open(DEV_PATH) as focuser:
                for bad_timeout in (0.0, -1.0, math.nan, math.inf):
                    with self.subTest(move_timeout=bad_timeout), self.assertRaises(ValueError):
                        await focuser.move(10_000, timeout=bad_timeout)
                    with self.subTest(stop_timeout=bad_timeout), self.assertRaises(ValueError):
                        await focuser.stop(timeout=bad_timeout)

                for bad_step in (-1, 40_001):
                    with self.subTest(step=bad_step), self.assertRaises(ValueError):
                        await focuser.move(bad_step)

        with FakeEafHidBackend(
            replies=[
                _firmware_report(version=(3, 2, 2)),
                _status_report_legacy(state=0x01, step=10_000, max_step=40_000),
            ]
        ) as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                with self.assertRaises(ZwoError):
                    await focuser.move(15_000)
            self.assertEqual(len(backend.sent_reports), 2)

    async def test_move_and_stop_timeout_raise_builtin_timeout_error_and_cap_sleep(self) -> None:
        move_replies = [
            _firmware_report(version=(3, 2, 2)),
            _status_report_legacy(state=0x00, step=10_000),
            _status_report_legacy(state=0x01, step=10_010),
        ]
        with (
            FakeEafHidBackend(replies=move_replies),
            patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            patch("pyzwohid.hid.monotonic", side_effect=[0.0, 0.0, 0.01]),
            self.assertRaises(TimeoutError),
        ):
            async with await Eaf.open(DEV_PATH) as focuser:
                await focuser.move(10_100, timeout=0.01)
        mock_sleep.assert_awaited_once_with(0.01)

        stop_replies = [
            _firmware_report(version=(3, 2, 2)),
            _status_report_legacy(state=0x01, step=10_010),
            _status_report_legacy(state=0x01, step=10_012),
        ]
        with (
            FakeEafHidBackend(replies=stop_replies),
            patch("pyzwohid.hid.asyncio.sleep") as mock_sleep,
            patch("pyzwohid.hid.monotonic", side_effect=[0.0, 0.0, 0.01]),
            self.assertRaises(TimeoutError),
        ):
            async with await Eaf.open(DEV_PATH) as focuser:
                await focuser.stop(timeout=0.01)
        mock_sleep.assert_awaited_once_with(0.01)

    async def test_clear_error_sends_command(self) -> None:
        with FakeEafHidBackend() as backend:
            async with await Eaf.open(DEV_PATH) as focuser:
                await focuser.clear_error()

        self.assertEqual(
            backend.sent_reports,
            [
                b"\x03\x7e\x5a\x02\x04".ljust(16, b"\x00"),
                b"\x03\x7e\x5a\x01\x0f".ljust(16, b"\x00"),
            ],
        )
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)


if __name__ == "__main__":
    unittest.main()
