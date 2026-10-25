# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Synchronous unit tests for the FilterWheel (EFW) controller."""

from __future__ import annotations

import unittest
from collections.abc import Callable

from pyzwo import DeviceState, ZwoError, ZwoTimeoutError
from pyzwo.efw import EFW, EFWErrorCode, FilterWheel

ZWO_VID = 0x03C3
EFW_PID = 0x1F01
EFW_PID_ALT = 0x1F02
UNKNOWN_PID = 0x0000

SLOT_COUNT = 7
MOVING_POS = -1


def make_zwo_error(op: str, code: EFWErrorCode) -> ZwoError:
    """Create a fresh ZwoError instance with an accurate SDK operation label."""
    return ZwoError(op, code, code.name)


class FakeEfwSDK:
    """In-memory single-device mock of ZwoEfwSDK for unit testing FilterWheel.

    Enforces realistic connection ownership by default: all operations taking an
    ``efw_id`` require ``efw_id`` to be in ``self.open_handles`` or raise
    ``EFW_ERROR_CLOSED``.

    Note: This fake models single-device behaviour (``efw_id == index`` for
    ``0 <= index < num_devices``); multi-device cross-handle quirks of the native
    C SDK are covered separately.
    """

    def __init__(self, slot_num: int = SLOT_COUNT, initial_slot: int = 0, num_devices: int = 1) -> None:
        self.num_devices = num_devices
        self.slot_num = slot_num
        self.position = initial_slot
        self.unidirectional = False
        self.open_handles: set[int] = set()
        self.open_attempts = 0
        self.close_attempts = 0
        self.set_calls: list[str] = []
        self.move_attempts: list[tuple[int, int]] = []
        self.moves: list[int] = []
        self.fail_on: dict[str, Exception] = {}
        self.stuck_moving = False
        self._polls_remaining = 0
        self._target_slot = initial_slot
        self._moving = False

    def _maybe_fail(self, method: str) -> None:
        exc = self.fail_on.get(method)
        if exc is not None:
            raise exc

    def _require_open(self, efw_id: int, op: str) -> None:
        if efw_id not in self.open_handles:
            raise make_zwo_error(f"{op}({efw_id})", EFWErrorCode.EFW_ERROR_CLOSED)

    def get_sdk_version(self) -> str:
        return "1, 8, 4"

    def check(self, vid: int, pid: int) -> bool:
        return vid == ZWO_VID and pid in (EFW_PID, EFW_PID_ALT)

    def get_num(self) -> int:
        self._maybe_fail("get_num")
        return self.num_devices

    def get_id(self, index: int) -> int:
        self._maybe_fail("get_id")
        if index < 0 or index >= self.num_devices:
            raise make_zwo_error(f"EFWGetID({index})", EFWErrorCode.EFW_ERROR_INVALID_INDEX)
        return index

    def open(self, efw_id: int) -> None:
        self.open_attempts += 1
        self._maybe_fail("open")
        self.open_handles.add(efw_id)

    def close(self, efw_id: int) -> None:
        self.close_attempts += 1
        self._maybe_fail("close")
        self._require_open(efw_id, "EFWClose")
        self.open_handles.discard(efw_id)
        self._moving = False

    def get_property(self, efw_id: int) -> tuple[int, str, int]:
        self._require_open(efw_id, "EFWGetProperty")
        self._maybe_fail("get_property")
        return (efw_id, "EFW", self.slot_num)

    def get_position(self, efw_id: int) -> int:
        self._require_open(efw_id, "EFWGetPosition")
        self._maybe_fail("get_position")
        if not self._moving:
            return self.position
        if self.stuck_moving:
            return MOVING_POS
        if self._polls_remaining > 0:
            self._polls_remaining -= 1
            if self._polls_remaining == 0:
                self.position = self._target_slot
                self._moving = False
            return MOVING_POS
        self._moving = False
        return self.position

    def set_position(self, efw_id: int, slot: int) -> None:
        self.move_attempts.append((efw_id, slot))
        self._require_open(efw_id, "EFWSetPosition")
        self._maybe_fail("set_position")
        if slot < 0 or slot >= self.slot_num:
            raise make_zwo_error(f"EFWSetPosition({efw_id}, {slot})", EFWErrorCode.EFW_ERROR_INVALID_VALUE)
        if slot == self.position:
            raise make_zwo_error(f"EFWSetPosition({efw_id}, {slot})", EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
        self.moves.append(slot)
        self._target_slot = slot
        self._polls_remaining = 2
        self._moving = True

    def get_direction(self, efw_id: int) -> bool:
        self._require_open(efw_id, "EFWGetDirection")
        self._maybe_fail("get_direction")
        return self.unidirectional

    def set_direction(self, efw_id: int, unidirectional: bool) -> None:
        self._require_open(efw_id, "EFWSetDirection")
        self.set_calls.append(f"unidirectional={unidirectional}")
        self._maybe_fail("set_direction")
        self.unidirectional = unidirectional

    def calibrate(self, efw_id: int) -> None:
        self._require_open(efw_id, "EFWCalibrate")
        self._maybe_fail("calibrate")
        self._target_slot = 0
        self._polls_remaining = 2
        self._moving = True

    def get_firmware_version(self, efw_id: int) -> str:
        self._require_open(efw_id, "EFWGetFirmwareVersion")
        self._maybe_fail("get_firmware_version")
        return "3.5.2"


class TestEfwClientUnit(unittest.TestCase):
    """Synchronous unit tests for the FilterWheel (EFW) controller."""

    def tearDown(self) -> None:
        FilterWheel.sdk = None

    def test_check_device(self) -> None:
        FilterWheel.sdk = FakeEfwSDK()
        self.assertTrue(FilterWheel.check_device(ZWO_VID, EFW_PID))
        self.assertFalse(FilterWheel.check_device(ZWO_VID, UNKNOWN_PID))

    def test_get_num(self) -> None:
        FilterWheel.sdk = FakeEfwSDK(num_devices=2)
        self.assertEqual(FilterWheel.get_num(), 2)

    def test_context_manager_opens_and_closes(self) -> None:
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=0)
        EFW.sdk = fake
        with EFW.open() as wheel:
            self.assertEqual(wheel.state, DeviceState.READY)
            self.assertEqual(wheel.info.slot_num, SLOT_COUNT)
            self.assertEqual(wheel.info.id, 0)
            self.assertEqual(fake.open_handles, {0})
        self.assertEqual(fake.open_handles, set())
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)

    def test_context_manager_closes_on_body_exception(self) -> None:
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=0)
        EFW.sdk = fake
        with self.assertRaises(RuntimeError), EFW.open() as wheel:
            self.assertEqual(fake.open_handles, {0})
            raise RuntimeError("caller error inside with block")
        self.assertEqual(fake.open_handles, set())
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)

    def test_open_sets_direction_only_when_configured(self) -> None:
        cases: list[tuple[bool | None, list[str], bool]] = [
            (None, [], False),
            (True, ["unidirectional=True"], True),
            (False, ["unidirectional=False"], False),
        ]
        for unidir_arg, expected_calls, expected_unidir in cases:
            with self.subTest(unidirectional=unidir_arg):
                fake = FakeEfwSDK()
                EFW.sdk = fake
                with EFW.open(unidirectional=unidir_arg) as wheel:
                    self.assertEqual(fake.set_calls, expected_calls)
                    self.assertEqual(fake.unidirectional, expected_unidir)
                    self.assertEqual(wheel.info.unidirectional, expected_unidir)

    def test_open_rejects_invalid_device_index(self) -> None:
        for num_devices, bad_idx in [(0, 0), (1, -1), (1, 1)]:
            with self.subTest(num_devices=num_devices, device_index=bad_idx):
                fake = FakeEfwSDK(num_devices=num_devices)
                EFW.sdk = fake
                with self.assertRaises(ZwoError) as ctx:
                    EFW.open(device_index=bad_idx)
                self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_INVALID_INDEX)
                self.assertEqual(fake.open_attempts, 0)
                self.assertEqual(fake.open_handles, set())

    def test_close_when_disconnected_is_idempotent(self) -> None:
        fake = FakeEfwSDK()
        EFW.sdk = fake
        wheel = EFW.open()
        wheel.close()
        wheel.close()
        self.assertEqual(fake.close_attempts, 1)
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)

    def test_reopen_after_close_succeeds(self) -> None:
        initial_slot = 3
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=initial_slot)
        EFW.sdk = fake
        wheel = EFW.open()
        wheel.close()
        self.assertEqual(fake.open_handles, set())

        wheel = EFW.open()
        self.assertEqual(wheel.info.slot_num, SLOT_COUNT)
        self.assertEqual(wheel.state, DeviceState.READY)
        self.assertEqual(fake.open_handles, {0})
        self.assertEqual(wheel.get_slot(), initial_slot)
        wheel.close()

    def test_open_failure_does_not_call_close(self) -> None:
        fake = FakeEfwSDK()
        EFW.sdk = fake
        open_err = make_zwo_error("EFWOpen(0)", EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
        fake.fail_on["open"] = open_err
        with self.assertRaises(ZwoError) as ctx:
            EFW.open(unidirectional=True)
        self.assertIs(ctx.exception, open_err)
        self.assertEqual(fake.open_attempts, 1)
        self.assertEqual(fake.close_attempts, 0)
        self.assertEqual(fake.open_handles, set())

    def test_post_open_init_failure_rolls_back_close(self) -> None:
        steps = ["set_direction", "get_direction", "get_property", "get_firmware_version"]
        for step in steps:
            with self.subTest(step=step):
                fake = FakeEfwSDK()
                EFW.sdk = fake
                injected = make_zwo_error(f"injected_{step}", EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
                fake.fail_on[step] = injected
                with self.assertRaises(ZwoError) as ctx:
                    EFW.open(unidirectional=True)
                self.assertIs(ctx.exception, injected)
                self.assertEqual(fake.open_attempts, 1)
                self.assertEqual(fake.close_attempts, 1)
                self.assertEqual(fake.open_handles, set())

    def test_open_rollback_close_failure_attaches_error(self) -> None:
        fake = FakeEfwSDK()
        EFW.sdk = fake
        init_err = make_zwo_error("EFWGetFirmwareVersion(0)", EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
        close_err = make_zwo_error("EFWClose(0)", EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
        fake.fail_on["get_firmware_version"] = init_err
        fake.fail_on["close"] = close_err
        with self.assertRaises(ZwoError) as ctx:
            EFW.open()
        self.assertIs(ctx.exception, init_err)
        self.assertIn(f"rollback close(id=0) also failed: {close_err}", ctx.exception.__notes__)
        self.assertEqual(fake.open_handles, {0})

    def test_close_failure_retains_handle_and_marks_unknown_until_retry(self) -> None:
        fake = FakeEfwSDK()
        EFW.sdk = fake
        wheel = EFW.open()
        fake.fail_on["close"] = make_zwo_error("EFWClose(0)", EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
        with self.assertRaises(ZwoError):
            wheel.close()
        self.assertEqual(wheel.state, DeviceState.UNKNOWN)
        self.assertEqual(wheel.info.id, 0)
        self.assertEqual(fake.open_handles, {0})

        del fake.fail_on["close"]
        wheel.close()
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
        self.assertEqual(fake.open_handles, set())

    def test_operations_rejected_when_state_is_unknown(self) -> None:
        fake = FakeEfwSDK()
        EFW.sdk = fake
        wheel = EFW.open()
        fake.fail_on["close"] = make_zwo_error("EFWClose(0)", EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
        with self.assertRaises(ZwoError):
            wheel.close()
        self.assertEqual(wheel.state, DeviceState.UNKNOWN)

        calls: list[tuple[str, Callable[[], object]]] = [
            ("get_slot", wheel.get_slot),
            ("move_to_slot", lambda: wheel.move_to_slot(1)),
            ("calibrate", wheel.calibrate),
        ]
        for label, call in calls:
            with self.subTest(op=label):
                with self.assertRaises(ZwoError) as ctx:
                    call()
                self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)

    def test_get_slot_returns_minus_one_when_moving_and_slot_when_settled(self) -> None:
        first_slot = 0
        last_slot = SLOT_COUNT - 1
        for pos in (MOVING_POS, first_slot, last_slot):
            with self.subTest(pos=pos):
                fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=2)
                EFW.sdk = fake
                with EFW.open() as wheel:
                    fake.position = pos
                    self.assertEqual(wheel.get_slot(), pos)

    def test_move_to_slot_polls_until_settled(self) -> None:
        target_slot = 4
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=0)
        EFW.sdk = fake
        with EFW.open() as wheel:
            settled, polls = wheel.move_to_slot(target_slot)
            self.assertEqual(settled, target_slot)
            self.assertGreater(polls, 0)
            self.assertEqual(fake.position, target_slot)
            self.assertEqual(wheel.state, DeviceState.READY)
            self.assertEqual(fake.moves, [target_slot])

    def test_move_to_current_slot_is_noop(self) -> None:
        current_slot = 4
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=current_slot)
        EFW.sdk = fake
        with EFW.open() as wheel:
            settled, polls = wheel.move_to_slot(current_slot)
            self.assertEqual((settled, polls), (current_slot, 0))
            self.assertEqual(fake.move_attempts, [])
            self.assertEqual(fake.moves, [])
            self.assertEqual(wheel.state, DeviceState.READY)

    def test_move_to_slot_rejects_invalid_slot(self) -> None:
        for bad_slot in (-1, SLOT_COUNT):
            with self.subTest(slot=bad_slot):
                fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=2)
                EFW.sdk = fake
                with EFW.open() as wheel:
                    with self.assertRaises(ZwoError) as ctx:
                        wheel.move_to_slot(bad_slot, timeout=0.05)
                    self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_INVALID_VALUE)
                    self.assertEqual(fake.move_attempts, [])
                    self.assertEqual(fake.moves, [])

    def test_move_to_slot_rejects_invalid_timeout(self) -> None:
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=2)
        EFW.sdk = fake
        with EFW.open() as wheel:
            for bad_timeout in (0.0, -1.0, float("nan"), float("inf")):
                with self.subTest(timeout=bad_timeout):
                    with self.assertRaises(ValueError):
                        wheel.move_to_slot(3, timeout=bad_timeout)

    def test_move_to_slot_rejects_when_wheel_already_moving(self) -> None:
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=2)
        EFW.sdk = fake
        with EFW.open() as wheel:
            fake.position = MOVING_POS
            with self.assertRaises(ZwoError) as ctx:
                wheel.move_to_slot(4, timeout=0.05)
            self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_MOVING)
            self.assertEqual(fake.move_attempts, [])
            self.assertEqual(fake.moves, [])

    def test_move_to_slot_timeout_raises(self) -> None:
        initial_slot = 2
        target_slot = 3
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=initial_slot)
        EFW.sdk = fake
        with EFW.open() as wheel:
            fake.stuck_moving = True
            with self.assertRaises(ZwoTimeoutError):
                wheel.move_to_slot(target_slot, timeout=0.01)
            self.assertEqual(wheel.state, DeviceState.READY)

    def test_calibrate_polls_until_settled(self) -> None:
        initial_slot = 3
        calibrated_slot = 0
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=initial_slot)
        EFW.sdk = fake
        with EFW.open() as wheel:
            settled_slot, polls = wheel.calibrate(timeout=0.05)
            self.assertEqual(settled_slot, calibrated_slot)
            self.assertGreater(polls, 0)
            self.assertEqual(fake.position, calibrated_slot)
            self.assertEqual(wheel.get_slot(), calibrated_slot)
            self.assertEqual(wheel.state, DeviceState.READY)

    def test_calibrate_timeout_raises(self) -> None:
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=3)
        EFW.sdk = fake
        with EFW.open() as wheel:
            fake.stuck_moving = True
            with self.assertRaises(ZwoTimeoutError):
                wheel.calibrate(timeout=0.01)
            self.assertEqual(wheel.state, DeviceState.READY)

    def test_removed_error_transitions_to_fault(self) -> None:
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=1)
        EFW.sdk = fake
        with EFW.open() as wheel:
            fake.fail_on["get_position"] = make_zwo_error("EFWGetPosition(0)", EFWErrorCode.EFW_ERROR_REMOVED)
            with self.assertRaises(ZwoError) as ctx:
                wheel.get_slot()
            self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_REMOVED)
            self.assertEqual(wheel.state, DeviceState.FAULT)

    def test_operations_rejected_while_in_fault_state(self) -> None:
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=1)
        EFW.sdk = fake
        with EFW.open() as wheel:
            fake.fail_on["get_position"] = make_zwo_error("EFWGetPosition(0)", EFWErrorCode.EFW_ERROR_REMOVED)
            with self.assertRaises(ZwoError):
                wheel.get_slot()
            del fake.fail_on["get_position"]

            calls: list[tuple[str, Callable[[], object]]] = [
                ("get_slot", wheel.get_slot),
                ("move_to_slot", lambda: wheel.move_to_slot(2)),
                ("calibrate", wheel.calibrate),
            ]
            for label, call in calls:
                with self.subTest(op=label):
                    with self.assertRaises(ZwoError) as ctx:
                        call()
                    self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)

    def test_close_and_reopen_recovers_from_removed_fault(self) -> None:
        initial_slot = 1
        fake = FakeEfwSDK(slot_num=SLOT_COUNT, initial_slot=initial_slot)
        EFW.sdk = fake
        wheel = EFW.open()
        fake.fail_on["get_position"] = make_zwo_error("EFWGetPosition(0)", EFWErrorCode.EFW_ERROR_REMOVED)
        with self.assertRaises(ZwoError):
            wheel.get_slot()
        self.assertEqual(wheel.state, DeviceState.FAULT)

        del fake.fail_on["get_position"]
        wheel.close()
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
        self.assertEqual(fake.open_handles, set())

        wheel = EFW.open()
        self.assertEqual(wheel.state, DeviceState.READY)
        self.assertEqual(wheel.info.slot_num, SLOT_COUNT)
        self.assertEqual(wheel.get_slot(), initial_slot)
        wheel.close()


if __name__ == "__main__":
    unittest.main()
