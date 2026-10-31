# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyzwo.hid and pyzwo.efw."""

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

from pyzwo import Efw, EfwStatus, ZwoDevicePathError, ZwoError, ZwoQueryCollisionError

DEV_PATH = "/dev/hidraw10"
SECOND_DEV_PATH = "/dev/hidraw11"
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
        status_replies_by_path: dict[bytes, list[bytes]] | None = None,
        open_error: Exception | None = None,
        io_error: Exception | None = None,
        on_open: Callable[[bytes], None] | None = None,
        on_send: Callable[[bytes, bytes], None] | None = None,
    ) -> None:
        self.status_replies = list(status_replies or [_status_report(slots=(3, 3, 3))])
        self.status_replies_by_path = {path: list(replies) for path, replies in (status_replies_by_path or {}).items()}
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
        with self._track_call("enumerate"):
            return [
                {"vendor_id": d_vid, "product_id": d_pid, "path": d_path}
                for d_vid, d_pid, d_path in self.DEVICES
                if vid in (0, d_vid) and pid in (0, d_pid)
            ]

    def Device(self, *, path: bytes) -> FakeHidDevice:
        with self._track_call("open", path):
            if self.on_open is not None:
                self.on_open(path)
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
            replies = self._backend.status_replies_by_path.get(self.path, self._backend.status_replies)
            if len(replies) > 1:
                return replies.pop(0)
            return replies[0]

    def close(self) -> None:
        with self._backend._track_call("close", self.path):
            if not self.closed:
                self.closed = True
                self._backend.close_count += 1


class TestEfw(unittest.IsolatedAsyncioTestCase):
    async def test_discover_deduplicates_and_sorts_paths(self) -> None:
        with FakeHidBackend():
            self.assertEqual(await Efw.discover(), ["/dev/hidraw10", "/dev/hidraw11"])

    async def test_get_status_settled_and_closes_handle(self) -> None:
        with FakeHidBackend(status_replies=[_status_report(slots=(5, 5, 5))]) as backend:
            async with await Efw.open(DEV_PATH) as wheel:
                status = await wheel.get_status()
            await wheel.close()  # idempotent second close

        self.assertEqual(status, EfwStatus(slot_count=SLOT_COUNT, slot=5))
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)
        self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x02\x01".ljust(16, b"\x00")])

    async def test_get_status_calibrating_and_moving_return_minus_one(self) -> None:
        calibrating_cases = [
            ("calibrating_power_on", _status_report(state=0x00, slots=(0, 0, 0), slot_count=0)),
            ("calibrating_magnet_counter", _status_report(state=0x00, slots=(3, 3, 3), slot_count=2)),
        ]
        for label, report in calibrating_cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]):
                async with await Efw.open(DEV_PATH) as wheel:
                    self.assertEqual(await wheel.get_status(), EfwStatus(slot_count=-1, slot=-1))

        moving_cases = [
            ("rotating_between_slots", _status_report(state=0x04, slots=(6, 2, 3))),
            ("fine_damping_at_target", _status_report(state=0x04, slots=(6, 6, 6))),
            ("inter_command_handoff", _status_report(state=0x01, slots=(2, 3, 3))),
        ]
        for label, report in moving_cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]):
                async with await Efw.open(DEV_PATH) as wheel:
                    self.assertEqual(await wheel.get_status(), EfwStatus(slot_count=SLOT_COUNT, slot=-1))

    async def test_get_status_protocol_and_hardware_errors_raise_zwo_error(self) -> None:
        cases = [
            ("bad_header", bytes(17)),
            ("dual_disc", _status_report(dual_disc=1)),
            ("hw_error", _status_report(state=0x06, error_code=2)),
            ("zero_slots", _status_report(slot_count=0)),
            ("too_many_slots", _status_report(slot_count=17)),
        ]
        for label, report in cases:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]) as backend:
                with self.assertRaises(ZwoError):
                    async with await Efw.open(DEV_PATH) as wheel:
                        await wheel.get_status()
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

    async def test_query_retries_on_selector_collision(self) -> None:
        other_reply = b"\x01\x7e\x5a\x04".ljust(17, b"\x00")
        replies = [other_reply, other_reply, _status_report(slots=(4, 4, 4))]
        with FakeHidBackend(status_replies=replies) as backend:
            async with await Efw.open(DEV_PATH) as wheel:
                self.assertEqual(await wheel.get_status(), EfwStatus(slot_count=SLOT_COUNT, slot=4))
            self.assertEqual(len(backend.sent_reports), 3)
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

    async def test_query_raises_collision_error_after_exhausting_retries(self) -> None:
        other_reply = b"\x01\x7e\x5a\x04".ljust(17, b"\x00")
        with FakeHidBackend(status_replies=[other_reply]) as backend:
            with self.assertRaises(ZwoError) as ctx:
                async with await Efw.open(DEV_PATH) as wheel:
                    await wheel.get_status()
            self.assertIsInstance(ctx.exception, ZwoQueryCollisionError)
            self.assertEqual(len(backend.sent_reports), 3)
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

    async def test_hid_open_and_io_exceptions_wrap_into_zwo_error(self) -> None:
        with FakeHidBackend(open_error=OSError("permission denied")), self.assertRaises(ZwoError):
            await Efw.open(DEV_PATH)

        with FakeHidBackend(io_error=OSError("input/output error")) as io_backend:
            with self.assertRaises(ZwoError):
                async with await Efw.open(DEV_PATH) as wheel:
                    await wheel.get_status()
            self.assertEqual(io_backend.open_count, 1)
            self.assertEqual(io_backend.close_count, 1)

        with FakeHidBackend():
            closed_wheel = await Efw.open(DEV_PATH)
            await closed_wheel.close()
            with self.assertRaises(ZwoError):
                await closed_wheel.get_status()

    async def test_open_rejects_non_matching_device_path(self) -> None:
        for path in (
            "/dev/hidraw12",  # existing ZWO EAF (same VID 0x03C3, non-EFW PID 0x1F10)
            "/dev/hidraw13",  # existing non-ZWO device (wrong VID 0x1234, PID 0x1F01)
            "/dev/hidraw99",  # non-existent path
        ):
            with self.subTest(path=path), FakeHidBackend() as backend:
                with self.assertRaises(ZwoError) as ctx:
                    await Efw.open(path)
                self.assertIsInstance(ctx.exception, ZwoDevicePathError)
                self.assertEqual(backend.open_count, 0)

    async def test_open_resolves_symlink_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, FakeHidBackend() as backend:
            link_path = os.path.join(tmpdir, "zwo-efw")
            os.symlink(DEV_PATH, link_path)
            async with await Efw.open(link_path) as wheel:
                self.assertEqual(wheel.path, link_path)
            self.assertEqual(backend.open_count, 1)
            self.assertEqual(backend.close_count, 1)

    async def test_move_to_slot_validates_arguments(self) -> None:
        with FakeHidBackend(status_replies=[_status_report(slots=(1, 1, 1))]):
            async with await Efw.open(DEV_PATH) as wheel:
                for bad_timeout in (0.0, -1.0, math.nan, math.inf):
                    with self.subTest(timeout=bad_timeout), self.assertRaises(ValueError):
                        await wheel.move_to_slot(1, timeout=bad_timeout)
                    with self.subTest(calibrate_timeout=bad_timeout), self.assertRaises(ValueError):
                        await wheel.calibrate(timeout=bad_timeout)

                for bad_slot in (0, -1, SLOT_COUNT + 1):
                    with self.subTest(slot=bad_slot), self.assertRaises(ValueError):
                        await wheel.move_to_slot(bad_slot)

    async def test_move_to_slot_and_calibrate_reject_when_already_moving_or_calibrating(self) -> None:
        busy_reports = [
            ("calibrating", _status_report(state=0x00, slots=(3, 3, 3), slot_count=1)),
            ("rotating", _status_report(state=0x04, slots=(6, 2, 3))),
        ]
        for label, report in busy_reports:
            with self.subTest(case=label), FakeHidBackend(status_replies=[report]) as backend:
                async with await Efw.open(DEV_PATH) as wheel:
                    with self.assertRaises(ZwoError):
                        await wheel.move_to_slot(5)
                    with self.assertRaises(ZwoError):
                        await wheel.calibrate()
                self.assertEqual(len(backend.sent_reports), 2)

    async def test_move_to_slot_bidirectional_and_unidirectional(self) -> None:
        for unidirectional, expected_dir in ((False, 0x02), (True, 0x03)):
            with self.subTest(unidirectional=unidirectional):
                replies = [
                    _status_report(slots=(1, 1, 1)),
                    _status_report(state=0x04, slots=(6, 2, 3)),
                    _status_report(slots=(6, 6, 6)),
                ]
                with (
                    FakeHidBackend(status_replies=replies) as backend,
                    patch("pyzwo.efw.asyncio.sleep"),
                ):
                    async with await Efw.open(DEV_PATH) as wheel:
                        settled = await wheel.move_to_slot(6, unidirectional=unidirectional)

                self.assertEqual(settled, 6)
                self.assertEqual(
                    backend.sent_reports[1],
                    bytes((0x03, 0x7E, 0x5A, 0x01, expected_dir, 6)).ljust(16, b"\x00"),
                )
                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)

    async def test_calibrate_sends_command_and_polls_until_settled(self) -> None:
        replies = [
            _status_report(slots=(3, 3, 3)),
            _status_report(state=0x00, slots=(3, 3, 3), slot_count=2),
            _status_report(state=0x00, slots=(1, 3, 1), slot_count=SLOT_COUNT),
            _status_report(slots=(1, 1, 1)),
        ]
        with (
            FakeHidBackend(status_replies=replies) as backend,
            patch("pyzwo.efw.asyncio.sleep"),
        ):
            async with await Efw.open(DEV_PATH) as wheel:
                settled = await wheel.calibrate()

        self.assertEqual(settled, 1)
        self.assertEqual(
            backend.sent_reports[1],
            b"\x03\x7e\x5a\x01\x01".ljust(16, b"\x00"),
        )
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)

    async def test_move_to_slot_and_calibrate_timeout_raise_builtin_timeout_error(self) -> None:
        replies = [
            _status_report(slots=(1, 1, 1)),
            _status_report(state=0x00, slots=(0, 0, 0)),
        ]
        with (
            FakeHidBackend(status_replies=list(replies)),
            patch("pyzwo.efw.asyncio.sleep"),
            patch("pyzwo.efw.monotonic", side_effect=[0.0, 1.0]),
            self.assertRaises(TimeoutError),
        ):
            async with await Efw.open(DEV_PATH) as wheel:
                await wheel.move_to_slot(4, timeout=0.5)

        with (
            FakeHidBackend(status_replies=list(replies)),
            patch("pyzwo.efw.asyncio.sleep"),
            patch("pyzwo.efw.monotonic", side_effect=[0.0, 1.0]),
            self.assertRaises(TimeoutError),
        ):
            async with await Efw.open(DEV_PATH) as wheel:
                await wheel.calibrate(timeout=0.5)

    async def test_clear_error_sends_command(self) -> None:
        with FakeHidBackend() as backend:
            async with await Efw.open(DEV_PATH) as wheel:
                await wheel.clear_error()

        self.assertEqual(backend.sent_reports, [b"\x03\x7e\x5a\x01\x0f".ljust(16, b"\x00")])
        self.assertEqual(backend.open_count, 1)
        self.assertEqual(backend.close_count, 1)

    async def test_executor_queues_and_serializes_concurrent_calls_across_devices(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        first_send = True

        def block_first_send(path: bytes, _payload: bytes) -> None:
            nonlocal first_send
            if first_send and path == b"/dev/hidraw10":
                first_send = False
                entered.set()
                release.wait(timeout=5.0)

        with FakeHidBackend(
            status_replies_by_path={
                b"/dev/hidraw10": [_status_report(slots=(2, 2, 2))],
                b"/dev/hidraw11": [_status_report(slots=(6, 6, 6))],
            },
            on_send=block_first_send,
        ) as backend:
            wheel1 = await Efw.open(DEV_PATH)
            wheel2 = await Efw.open(SECOND_DEV_PATH)
            backend.events.clear()

            task1 = asyncio.create_task(wheel1.get_status())
            self.assertTrue(await asyncio.to_thread(entered.wait, 5.0))

            task2 = asyncio.create_task(wheel2.get_status())
            task3 = asyncio.create_task(wheel1.close())
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            self.assertFalse(task1.done())
            self.assertFalse(task2.done())
            self.assertFalse(task3.done())
            self.assertEqual(backend.close_count, 0)

            release.set()
            status1, status2, _ = await asyncio.gather(task1, task2, task3)
            await wheel2.close()

        self.assertEqual(status1, EfwStatus(slot_count=SLOT_COUNT, slot=2))
        self.assertEqual(status2, EfwStatus(slot_count=SLOT_COUNT, slot=6))
        self.assertEqual(backend.max_concurrent_calls, 1)
        self.assertEqual(
            backend.events,
            [
                ("send", b"/dev/hidraw10"),
                ("get", b"/dev/hidraw10"),
                ("send", b"/dev/hidraw11"),
                ("get", b"/dev/hidraw11"),
                ("close", b"/dev/hidraw10"),
                ("close", b"/dev/hidraw11"),
            ],
        )

    async def test_concurrent_moves_across_devices_interleave_polls(self) -> None:
        poll_barrier = asyncio.Barrier(2)

        async def synchronized_sleep(_delay: float) -> None:
            await poll_barrier.wait()

        with (
            FakeHidBackend(
                status_replies_by_path={
                    b"/dev/hidraw10": [
                        _status_report(slots=(1, 1, 1)),
                        _status_report(state=0x04, slots=(6, 2, 3)),
                        _status_report(slots=(6, 6, 6)),
                    ],
                    b"/dev/hidraw11": [
                        _status_report(slots=(2, 2, 2)),
                        _status_report(state=0x04, slots=(4, 2, 3)),
                        _status_report(slots=(4, 4, 4)),
                    ],
                }
            ) as backend,
            patch("pyzwo.efw.asyncio.sleep", side_effect=synchronized_sleep),
        ):
            async with await Efw.open(DEV_PATH) as wheel1, await Efw.open(SECOND_DEV_PATH) as wheel2:
                backend.events.clear()
                settled1, settled2 = await asyncio.gather(
                    wheel1.move_to_slot(6),
                    wheel2.move_to_slot(4),
                )

        self.assertEqual((settled1, settled2), (6, 4))
        self.assertEqual(backend.max_concurrent_calls, 1)
        send_paths = [path for op, path in backend.events if op == "send"]
        # Both wheels complete pre-sleep (get_status + move) before poll round 1, then alternate per poll round.
        self.assertEqual(len(send_paths), 8)
        self.assertEqual(set(send_paths[4:6]), {b"/dev/hidraw10", b"/dev/hidraw11"})
        self.assertEqual(set(send_paths[6:8]), {b"/dev/hidraw10", b"/dev/hidraw11"})

    async def test_cancellation_during_poll_and_in_flight_query_closes_handle_safely(self) -> None:
        with self.subTest("cancelled_during_poll_sleep"):
            sleeping = asyncio.Event()
            never: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            async def block_in_sleep(_delay: float) -> None:
                sleeping.set()
                await never

            with (
                FakeHidBackend(
                    status_replies=[
                        _status_report(slots=(1, 1, 1)),
                        _status_report(state=0x04, slots=(6, 2, 3)),
                    ]
                ) as backend,
                patch("pyzwo.efw.asyncio.sleep", side_effect=block_in_sleep),
            ):

                async def run_move() -> int:
                    async with await Efw.open(DEV_PATH) as wheel:
                        return await wheel.move_to_slot(6)

                task = asyncio.create_task(run_move())
                await sleeping.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.max_concurrent_calls, 1)

        with self.subTest("cancelled_during_in_flight_executor_query"):
            entered = threading.Event()
            release = threading.Event()

            def block_send(_path: bytes, _payload: bytes) -> None:
                entered.set()
                release.wait(timeout=5.0)

            with FakeHidBackend(on_send=block_send) as backend:

                async def run_query() -> EfwStatus:
                    async with await Efw.open(DEV_PATH) as wheel:
                        return await wheel.get_status()

                query_task = asyncio.create_task(run_query())
                self.assertTrue(await asyncio.to_thread(entered.wait, 5.0))

                query_task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                # __aexit__ queues _close_device behind the in-flight _query_sync on _EXECUTOR
                self.assertEqual(backend.close_count, 0)

                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await query_task

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.max_concurrent_calls, 1)
                self.assertEqual(
                    [(op, path) for op, path in backend.events if op != "enumerate"],
                    [
                        ("open", b"/dev/hidraw10"),
                        ("send", b"/dev/hidraw10"),
                        ("get", b"/dev/hidraw10"),
                        ("close", b"/dev/hidraw10"),
                    ],
                )

        with self.subTest("cancelled_during_in_flight_executor_open"):
            open_entered = threading.Event()
            open_release = threading.Event()

            def block_open(_path: bytes) -> None:
                open_entered.set()
                open_release.wait(timeout=5.0)

            with FakeHidBackend(on_open=block_open) as backend:

                async def run_open() -> EfwStatus:
                    async with await Efw.open(DEV_PATH) as wheel:
                        return await wheel.get_status()

                open_task = asyncio.create_task(run_open())
                self.assertTrue(await asyncio.to_thread(open_entered.wait, 5.0))

                open_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await open_task

                open_release.set()
                # Flush _EXECUTOR by running discover() so the queued _close_unclaimed job completes.
                await Efw.discover()

                self.assertEqual(backend.open_count, 1)
                self.assertEqual(backend.close_count, 1)
                self.assertEqual(backend.max_concurrent_calls, 1)
                self.assertEqual(
                    [(op, path) for op, path in backend.events if op != "enumerate"],
                    [
                        ("open", b"/dev/hidraw10"),
                        ("close", b"/dev/hidraw10"),
                    ],
                )


if __name__ == "__main__":
    unittest.main()
