# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""C-header verification, unit tests, and opt-in hardware tests for pyzwo.efw."""

from __future__ import annotations

import ctypes
import os
import unittest
from unittest.mock import patch

from pyzwo import DeviceState, ZwoError, ZwoTimeoutError
from pyzwo.efw import _EFWID, _EFWSN, EFW, EFWErrorCode, FilterWheel, ZwoEfwSDK, _EFWInfo

try:
    from tests.header_checker import INCLUDE_DIR, FakeCDLL, parse_header_enum, parse_header_prototypes
except ModuleNotFoundError:
    from header_checker import (  # type: ignore[no-redef]
        INCLUDE_DIR,
        FakeCDLL,
        parse_header_enum,
        parse_header_prototypes,
    )

EFW_HEADER = INCLUDE_DIR / "EFW_filter.h"


class FakeEfwSDK:
    """In-memory mock of ZwoEfwSDK for unit testing FilterWheel without vendor shared libraries.

    Note: ``check()`` / ``FilterWheel.check_device()`` (wrapping ``EFWCheck``) and C-only bindings
    (``EFWGetHWErrorCode``, ``EFWGetSerialNumber``, ``EFWSetID``) are intentionally not mocked here;
    their C ABI signatures are verified in ``TestEfwCtypesHeader``, and testing a hardcoded fake
    VID/PID check provides no behavioral value.
    """

    def __init__(self, slot_num: int = 7, initial_slot: int = 0) -> None:
        self.slot_num = slot_num
        self.position = initial_slot
        self.unidirectional = False
        self.opened = False
        self.raw_open_handles: set[int] = set()
        self.strict_close = False
        self.open_calls = 0
        self.close_calls = 0
        self.get_num_calls = 0
        self.get_id_calls = 0
        self.set_calls: list[str] = []
        self.moves: list[int] = []
        self.calibrated = 0
        self.fail_on: dict[str, BaseException] = {}
        self.property_sequence: list[tuple[int, str, int] | BaseException] | None = None
        self.position_sequence: list[int] | None = None
        self.stuck_moving = False
        self.settle_at_wrong_slot: int | None = None
        self.calibrate_moving_polls = 2
        self._polls_remaining = 0
        self._target_slot = initial_slot

    def _maybe_fail(self, method: str) -> None:
        exc = self.fail_on.get(method)
        if exc is not None:
            raise exc

    def get_sdk_version(self) -> str:
        return "1, 8, 4"

    def get_num(self) -> int:
        self.get_num_calls += 1
        self._maybe_fail("get_num")
        return 1

    def get_id(self, index: int) -> int:
        self.get_id_calls += 1
        self._maybe_fail("get_id")
        return 0

    def open(self, efw_id: int) -> None:
        self._maybe_fail("open")
        self.open_calls += 1
        self.opened = True
        self.raw_open_handles.add(efw_id)

    def close(self, efw_id: int) -> None:
        self._maybe_fail("close")
        self.close_calls += 1
        if self.strict_close and efw_id not in self.raw_open_handles:
            raise ZwoError(
                f"EFWClose({efw_id})",
                EFWErrorCode.EFW_ERROR_CLOSED,
                "EFW_ERROR_CLOSED",
            )
        self.opened = False
        self.raw_open_handles.discard(efw_id)

    def get_property(self, efw_id: int) -> tuple[int, str, int]:
        self._maybe_fail("get_property")
        if self.property_sequence:
            item = self.property_sequence.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return (efw_id, "EFW", self.slot_num)

    def get_position(self, efw_id: int) -> int:
        self._maybe_fail("get_position")
        if self.strict_close and efw_id not in self.raw_open_handles:
            raise ZwoError(
                f"EFWGetPosition({efw_id})",
                EFWErrorCode.EFW_ERROR_CLOSED,
                "EFW_ERROR_CLOSED",
            )
        if self.position_sequence:
            return self.position_sequence.pop(0)
        if self.stuck_moving:
            return -1
        if self._polls_remaining > 0:
            self._polls_remaining -= 1
            if self._polls_remaining == 0:
                if self.settle_at_wrong_slot is not None:
                    self.position = self.settle_at_wrong_slot
                else:
                    self.position = self._target_slot
            return -1
        return self.position

    def set_position(self, efw_id: int, slot: int) -> None:
        self._maybe_fail("set_position")
        if slot == self.position:
            raise RuntimeError("EFW_ERROR_GENERAL_ERROR (rc=7)")
        self.moves.append(slot)
        self._target_slot = slot
        self._polls_remaining = 2

    def get_direction(self, efw_id: int) -> bool:
        self._maybe_fail("get_direction")
        return self.unidirectional

    def set_direction(self, efw_id: int, unidirectional: bool) -> None:
        self.set_calls.append(f"unidirectional={unidirectional}")
        self._maybe_fail("set_direction")
        self.unidirectional = unidirectional

    def calibrate(self, efw_id: int) -> None:
        self._maybe_fail("calibrate")
        self.calibrated += 1
        self._target_slot = 0
        self._polls_remaining = self.calibrate_moving_polls
        if self._polls_remaining == 0:
            self.position = 0

    def get_firmware_version(self, efw_id: int) -> str:
        self._maybe_fail("get_firmware_version")
        return "3.5.2"


class TestEfwCtypesHeader(unittest.TestCase):
    """Verify EFW ctypes structs, enums, and function signatures against EFW_filter.h."""

    def test_efw_struct_and_enum_match_header(self) -> None:
        text = EFW_HEADER.read_text(encoding="utf-8", errors="replace")
        self.assertIn("typedef struct _EFW_INFO", text)
        self.assertIn("typedef struct _EFW_ID", text)
        self.assertIn("typedef EFW_ID EFW_SN;", text)
        self.assertEqual(ctypes.sizeof(_EFWInfo), 4 + 64 + 4)
        self.assertEqual(ctypes.sizeof(_EFWID), 8)
        self.assertIs(_EFWSN, _EFWID)
        header_enum = parse_header_enum(EFW_HEADER, "_EFW_ERROR_CODE")
        self.assertEqual({m.name: int(m.value) for m in EFWErrorCode}, header_enum)

    def test_efw_ctypes_signatures_match_c_header(self) -> None:
        prototypes = parse_header_prototypes(
            EFW_HEADER,
            "EFW_API",
            extra_types={
                "EFW_INFO*": ctypes.POINTER(_EFWInfo),
                "EFW_SN*": ctypes.POINTER(_EFWSN),
                "EFW_ID": _EFWID,
                "EFW_ERROR_CODE": ctypes.c_int,
            },
        )
        with patch(
            "pyzwo.efw.load_zwo_library",
            side_effect=OSError("native binary must not be loaded by header tests"),
        ):
            sdk = ZwoEfwSDK(_lib=FakeCDLL())
        bound_funcs = [
            "EFWGetSDKVersion",
            "EFWCheck",
            "EFWGetNum",
            "EFWGetID",
            "EFWOpen",
            "EFWClose",
            "EFWGetProperty",
            "EFWGetPosition",
            "EFWSetPosition",
            "EFWGetDirection",
            "EFWSetDirection",
            "EFWCalibrate",
            "EFWGetHWErrorCode",
            "EFWGetFirmwareVersion",
            "EFWGetSerialNumber",
            "EFWSetID",
        ]
        for name in bound_funcs:
            self.assertIn(name, prototypes, f"{name} not found in EFW_filter.h")
            expected_restype, expected_argtypes = prototypes[name]
            c_func = getattr(sdk._lib, name)
            self.assertEqual(c_func.restype, expected_restype, f"{name} restype mismatch")
            self.assertEqual(list(c_func.argtypes), expected_argtypes, f"{name} argtypes mismatch")

    def test_raw_sdk_error_translation_and_moving_position_sentinel(self) -> None:
        lib = FakeCDLL()
        sdk = ZwoEfwSDK(_lib=lib)

        # EFW_ERROR_MOVING in EFWGetPosition is translated to -1 sentinel, not an exception
        lib.EFWGetPosition._impl = lambda _id, _ptr: int(EFWErrorCode.EFW_ERROR_MOVING)
        self.assertEqual(sdk.get_position(0), -1)

        # Other non-zero return codes raise ZwoError with code and enum name
        lib.EFWGetPosition._impl = lambda _id, _ptr: int(EFWErrorCode.EFW_ERROR_REMOVED)
        with self.assertRaises(ZwoError) as ctx:
            sdk.get_position(0)
        self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_REMOVED)
        self.assertEqual(ctx.exception.code_name, "EFW_ERROR_REMOVED")

        # SDK 1.8.4 closed-handle return code (11) and unknown code (10)
        lib.EFWGetPosition._impl = lambda _id, _ptr: 11
        with self.assertRaises(ZwoError) as ctx_closed:
            sdk.get_position(0)
        self.assertEqual(ctx_closed.exception.code, EFWErrorCode.EFW_ERROR_CLOSED)
        self.assertEqual(ctx_closed.exception.code_name, "EFW_ERROR_CLOSED")

        lib.EFWGetPosition._impl = lambda _id, _ptr: 10
        with self.assertRaises(ZwoError) as ctx_unk:
            sdk.get_position(0)
        self.assertEqual(ctx_unk.exception.code, EFWErrorCode.EFW_ERROR_UNKNOWN)
        self.assertEqual(ctx_unk.exception.code_name, "EFW_ERROR_UNKNOWN")


class TestEfwClientUnit(unittest.TestCase):
    """Synchronous unit tests for the FilterWheel (EFW) controller."""

    def test_import_and_controller_instantiation_do_not_load_vendor_library(self) -> None:
        with patch(
            "pyzwo.efw.load_zwo_library",
            side_effect=OSError("libEFWFilter.so not installed"),
        ):
            wheel = FilterWheel(device_index=0)
            self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
            self.assertIsNone(wheel.info)
            self.assertIsNone(wheel.efw_id)

    def test_sync_context_manager_and_move(self) -> None:
        fake = FakeEfwSDK(slot_num=7, initial_slot=0)
        wheel = EFW(sdk=fake)  # type: ignore[arg-type]
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
        with wheel:
            self.assertEqual(fake.set_calls, [])
            self.assertEqual(wheel.state, DeviceState.READY)
            self.assertIsNotNone(wheel.info)
            assert wheel.info is not None
            self.assertEqual(wheel.info.slot_num, 7)
            self.assertEqual(wheel.get_slot(), 0)
            settled, polls = wheel.move_to_slot(4, poll_interval=0.001)
            self.assertEqual((settled, polls), (4, 2))
            self.assertEqual(wheel.state, DeviceState.READY)
            # Moving to current slot is a no-op (avoids rc=7)
            settled2, polls2 = wheel.move_to_slot(4, poll_interval=0.001)
            self.assertEqual((settled2, polls2), (4, 0))
            self.assertEqual(fake.moves, [4])
        self.assertFalse(fake.opened)
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
        self.assertIsNone(wheel.info)
        self.assertIsNone(wheel.efw_id)

    def test_negative_and_out_of_range_indices_rejected_before_get_id(self) -> None:
        fake = FakeEfwSDK()
        for bad_idx in (-1, -5, 1, 2, True):
            wheel = EFW(device_index=bad_idx, sdk=fake)  # type: ignore[arg-type]
            with self.assertRaises(ZwoError) as ctx:
                wheel.open()
            self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_INVALID_INDEX)
            self.assertEqual(fake.get_id_calls, 0)
            self.assertEqual(fake.open_calls, 0)
            self.assertEqual(wheel.state, DeviceState.DISCONNECTED)

    def test_rollback_closes_handle_on_failure_at_each_init_step(self) -> None:
        # 1. If EFWOpen itself fails, close() must NOT be called (verified via strict_close=True)
        fake_open_fail = FakeEfwSDK()
        fake_open_fail.strict_close = True
        open_err = ZwoError(
            "EFWOpen(0)",
            EFWErrorCode.EFW_ERROR_GENERAL_ERROR,
            "EFW_ERROR_GENERAL_ERROR",
        )
        fake_open_fail.fail_on["open"] = open_err
        wheel_open_fail = EFW(unidirectional=True, sdk=fake_open_fail)  # type: ignore[arg-type]
        with self.assertRaises(ZwoError) as open_ctx:
            wheel_open_fail.open()
        self.assertIs(open_ctx.exception, open_err)
        self.assertIsNone(open_ctx.exception.rollback_error)
        self.assertEqual(fake_open_fail.open_calls, 0)
        self.assertEqual(fake_open_fail.close_calls, 0)
        self.assertEqual(fake_open_fail.raw_open_handles, set())
        self.assertIsNone(wheel_open_fail.efw_id)
        self.assertIsNone(wheel_open_fail.info)
        self.assertEqual(wheel_open_fail.state, DeviceState.DISCONNECTED)
        wheel_open_fail.close()
        self.assertEqual(fake_open_fail.close_calls, 0)

        # 2. If any post-open initialization step fails, rollback calls close() once
        steps = ["set_direction", "get_direction", "get_property", "get_position", "get_firmware_version"]
        for step in steps:
            fake = FakeEfwSDK()
            fake.strict_close = True
            injected = ZwoError(
                f"injected_{step}",
                EFWErrorCode.EFW_ERROR_GENERAL_ERROR,
                "EFW_ERROR_GENERAL_ERROR",
            )
            fake.fail_on[step] = injected
            wheel = EFW(unidirectional=True, sdk=fake)  # type: ignore[arg-type]
            with self.assertRaises(ZwoError) as ctx:
                wheel.open()
            self.assertIs(ctx.exception, injected)
            self.assertIsNone(ctx.exception.rollback_error)
            self.assertEqual(fake.open_calls, 1, f"step {step} did not open")
            self.assertEqual(fake.close_calls, 1, f"step {step} did not roll back close")
            self.assertEqual(fake.raw_open_handles, set())
            self.assertFalse(fake.opened)
            self.assertIsNone(wheel.efw_id)
            self.assertIsNone(wheel.info)
            self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
            # Subsequent close() after failed open with successful rollback is a no-op
            wheel.close()
            self.assertEqual(fake.close_calls, 1)

    def test_rollback_failure_reports_both_errors_and_marks_state_unknown(self) -> None:
        fake = FakeEfwSDK()
        fake.strict_close = True
        init_err = ZwoError(
            "EFWGetFirmwareVersion(0)",
            EFWErrorCode.EFW_ERROR_GENERAL_ERROR,
            "EFW_ERROR_GENERAL_ERROR",
        )
        close_err = ZwoError(
            "EFWClose(0)",
            EFWErrorCode.EFW_ERROR_GENERAL_ERROR,
            "EFW_ERROR_GENERAL_ERROR",
        )
        fake.fail_on["get_firmware_version"] = init_err
        fake.fail_on["close"] = close_err

        wheel = EFW(sdk=fake)  # type: ignore[arg-type]
        with self.assertRaises(ZwoError) as ctx:
            wheel.open()
        self.assertIs(ctx.exception, init_err)
        self.assertIs(ctx.exception.rollback_error, close_err)
        self.assertIn("EFWGetFirmwareVersion(0)", str(ctx.exception))
        self.assertIn("rollback close(id=0) also failed", str(ctx.exception))
        self.assertEqual(wheel.state, DeviceState.UNKNOWN)
        self.assertIsNone(wheel.info)
        self.assertEqual(wheel.efw_id, 0)
        self.assertEqual(fake.raw_open_handles, {0})

        # Operations and open() are rejected while state is UNKNOWN
        with self.assertRaises(ZwoError) as op_ctx:
            wheel.get_slot()
        self.assertEqual(op_ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)

        with self.assertRaises(ZwoError) as reopen_ctx:
            wheel.open()
        self.assertEqual(reopen_ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)

        # Once close succeeds on retry, handle is released and state is DISCONNECTED
        del fake.fail_on["close"]
        wheel.close()
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
        self.assertIsNone(wheel.efw_id)
        self.assertFalse(fake.opened)
        self.assertEqual(fake.raw_open_handles, set())

    def test_double_open_double_close_close_failure_and_reopen(self) -> None:
        fake = FakeEfwSDK()
        fake.strict_close = True
        wheel = EFW(sdk=fake)  # type: ignore[arg-type]
        wheel.open()
        self.assertEqual(wheel.state, DeviceState.READY)
        self.assertEqual(fake.open_calls, 1)
        self.assertEqual(fake.raw_open_handles, {0})

        # Second open() is rejected without calling sdk.open() again or leaking the handle
        with self.assertRaises(ZwoError) as ctx:
            wheel.open()
        self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_GENERAL_ERROR)
        self.assertEqual(fake.open_calls, 1)
        self.assertTrue(fake.opened)
        self.assertEqual(wheel.state, DeviceState.READY)

        # If close() fails, handle is retained and state becomes UNKNOWN; info is cleared
        fake.fail_on["close"] = ZwoError(
            "EFWClose(0)",
            EFWErrorCode.EFW_ERROR_GENERAL_ERROR,
            "EFW_ERROR_GENERAL_ERROR",
        )
        with self.assertRaises(ZwoError):
            wheel.close()
        self.assertEqual(wheel.state, DeviceState.UNKNOWN)
        self.assertIsNone(wheel.info)
        self.assertEqual(wheel.efw_id, 0)
        self.assertEqual(fake.raw_open_handles, {0})

        # Retry close() succeeds and is idempotent on repeat
        del fake.fail_on["close"]
        wheel.close()
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
        self.assertIsNone(wheel.efw_id)
        self.assertEqual(fake.close_calls, 1)
        self.assertEqual(fake.raw_open_handles, set())
        wheel.close()
        self.assertEqual(fake.close_calls, 1)

        # Reopen after close succeeds
        info = wheel.open()
        self.assertEqual(info.slot_num, 7)
        self.assertEqual(wheel.state, DeviceState.READY)
        self.assertEqual(fake.open_calls, 2)
        wheel.close()

    def test_open_discovery_deadline_terminates_on_zero_slots_or_repeating_moving(self) -> None:
        # 1. Transient slot_num=0, EFW_ERROR_MOVING, and -1 position before settling succeeds within deadline
        fake_transient = FakeEfwSDK(slot_num=7)
        fake_transient.property_sequence = [
            (0, "EFW", 0),
            ZwoError("EFWGetProperty(0)", EFWErrorCode.EFW_ERROR_MOVING, "EFW_ERROR_MOVING"),
            (0, "EFW", 7),
            (0, "EFW", 7),
            (0, "EFW", 7),
        ]
        fake_transient.position_sequence = [-1, -1, 3]
        wheel_transient = EFW(sdk=fake_transient)  # type: ignore[arg-type]
        info = wheel_transient.open(timeout=0.5, poll_interval=0.002)
        self.assertEqual(info.slot_num, 7)
        self.assertEqual(wheel_transient.state, DeviceState.READY)
        self.assertEqual(fake_transient.position_sequence, [])
        wheel_transient.close()

        # 2. Infinite slot_num=0 responses must time out and roll back close()
        fake_zero = FakeEfwSDK(slot_num=0)
        wheel_zero = EFW(sdk=fake_zero)  # type: ignore[arg-type]
        with self.assertRaises(ZwoTimeoutError):
            wheel_zero.open(timeout=0.01, poll_interval=0.002)
        self.assertEqual(fake_zero.open_calls, 1)
        self.assertEqual(fake_zero.close_calls, 1)
        self.assertFalse(fake_zero.opened)
        self.assertEqual(wheel_zero.state, DeviceState.DISCONNECTED)

        # 3. Infinite EFW_ERROR_MOVING responses must time out and roll back close()
        fake_moving = FakeEfwSDK(slot_num=7)
        fake_moving.fail_on["get_property"] = ZwoError(
            "EFWGetProperty(0)",
            EFWErrorCode.EFW_ERROR_MOVING,
            "EFW_ERROR_MOVING",
        )
        wheel_moving = EFW(sdk=fake_moving)  # type: ignore[arg-type]
        with self.assertRaises(ZwoTimeoutError):
            wheel_moving.open(timeout=0.01, poll_interval=0.002)
        self.assertEqual(fake_moving.open_calls, 1)
        self.assertEqual(fake_moving.close_calls, 1)
        self.assertFalse(fake_moving.opened)
        self.assertEqual(wheel_moving.state, DeviceState.DISCONNECTED)

        # 4. Infinite -1 moving position after slot_num > 0 must time out and roll back close()
        fake_pos_moving = FakeEfwSDK(slot_num=7)
        fake_pos_moving.stuck_moving = True
        wheel_pos_moving = EFW(sdk=fake_pos_moving)  # type: ignore[arg-type]
        with self.assertRaises(ZwoTimeoutError):
            wheel_pos_moving.open(timeout=0.01, poll_interval=0.002)
        self.assertEqual(fake_pos_moving.open_calls, 1)
        self.assertEqual(fake_pos_moving.close_calls, 1)
        self.assertFalse(fake_pos_moving.opened)
        self.assertEqual(wheel_pos_moving.state, DeviceState.DISCONNECTED)

        # 5. Out-of-range settled slot reported during open() must raise EFW_ERROR_ERROR_STATE and roll back close()
        fake_bad_pos = FakeEfwSDK(slot_num=7, initial_slot=9)
        wheel_bad_pos = EFW(sdk=fake_bad_pos)  # type: ignore[arg-type]
        with self.assertRaises(ZwoError) as bad_pos_ctx:
            wheel_bad_pos.open(timeout=0.05, poll_interval=0.002)
        self.assertEqual(bad_pos_ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)
        self.assertEqual(fake_bad_pos.open_calls, 1)
        self.assertEqual(fake_bad_pos.close_calls, 1)
        self.assertFalse(fake_bad_pos.opened)
        self.assertEqual(wheel_bad_pos.state, DeviceState.DISCONNECTED)

    def test_slot_bounds_wrong_settled_slot_and_permanent_moving_timeout(self) -> None:
        fake = FakeEfwSDK(slot_num=7, initial_slot=2)
        with EFW(sdk=fake) as wheel:  # type: ignore[arg-type]
            # Negative slot, slot == slot_num, slot > slot_num, and bool are rejected before SDK call
            for bad_slot in (-1, -3, 7, 8, True):
                with self.assertRaises(ZwoError) as ctx:
                    wheel.move_to_slot(bad_slot, poll_interval=0.001, timeout=0.05)  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_INVALID_VALUE)
            self.assertEqual(fake.moves, [])

            # Invalid poll_interval / timeout rejected with ValueError
            for bad_poll, bad_timeout in [
                (0.0, 1.0),
                (-0.1, 1.0),
                (0.01, 0.0),
                (0.01, -1.0),
                (float("nan"), 1.0),
            ]:
                with self.assertRaises(ValueError):
                    wheel.move_to_slot(3, poll_interval=bad_poll, timeout=bad_timeout)

            # Distinguish -1 (moving) from valid settled slot during get_slot()
            fake.position_sequence = [-1, 2]
            self.assertEqual(wheel.get_slot(), -1)
            self.assertEqual(wheel.get_slot(), 2)

            # If initial get_position() inside move_to_slot() returns -1 (already moving),
            # reject with EFW_ERROR_MOVING without issuing set_position() and mark FAULT
            fake.position_sequence = [-1]
            with self.assertRaises(ZwoError) as already_moving_ctx:
                wheel.move_to_slot(4, poll_interval=0.001, timeout=0.05)
            self.assertEqual(already_moving_ctx.exception.code, EFWErrorCode.EFW_ERROR_MOVING)
            self.assertEqual(fake.moves, [])
            self.assertEqual(wheel.state, DeviceState.FAULT)
            wheel.reconcile()
            self.assertEqual(wheel.state, DeviceState.READY)

            # Wrong settled slot after moving raises EFW_ERROR_ERROR_STATE and marks FAULT
            fake.settle_at_wrong_slot = 5
            with self.assertRaises(ZwoError) as wrong_ctx:
                wheel.move_to_slot(4, poll_interval=0.001, timeout=0.05)
            self.assertEqual(wrong_ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)
            self.assertEqual(wheel.state, DeviceState.FAULT)
            # Unsafe commands fail fast while in FAULT until reconcile() or close()/open()
            with self.assertRaises(ZwoError) as fault_ctx:
                wheel.move_to_slot(2, poll_interval=0.001, timeout=0.05)
            self.assertEqual(fault_ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)
            fake.settle_at_wrong_slot = None
            wheel.reconcile()
            self.assertEqual(wheel.state, DeviceState.READY)

            # Wrong settled slot without ever reporting -1 also terminates with EFW_ERROR_ERROR_STATE
            fake.position_sequence = [5, 1, 1, 1]
            with self.assertRaises(ZwoError) as wrong_no_move_ctx:
                wheel.move_to_slot(6, poll_interval=0.001, timeout=0.05)
            self.assertEqual(wrong_no_move_ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)
            self.assertEqual(wheel.state, DeviceState.FAULT)
            fake.position_sequence = None
            fake._polls_remaining = 0
            wheel.reconcile()
            self.assertEqual(wheel.state, DeviceState.READY)

            # Permanent -1 moving state terminates with ZwoTimeoutError and marks FAULT
            fake.stuck_moving = True
            fake.position = 2
            fake.position_sequence = [2]  # initial get_position check before set_position
            with self.assertRaises(ZwoTimeoutError):
                wheel.move_to_slot(3, poll_interval=0.002, timeout=0.01)
            self.assertEqual(wheel.state, DeviceState.FAULT)
            # Reconcile while hardware still reports -1 moving fails and keeps state FAULT
            with self.assertRaises(ZwoError) as rec_moving_ctx:
                wheel.reconcile()
            self.assertEqual(rec_moving_ctx.exception.code, EFWErrorCode.EFW_ERROR_MOVING)
            self.assertEqual(wheel.state, DeviceState.FAULT)

    def test_calibrate_fast_normal_and_never_completing(self) -> None:
        fake = FakeEfwSDK(slot_num=7, initial_slot=3)
        with EFW(sdk=fake) as wheel:  # type: ignore[arg-type]
            # 1. Fast calibration with no observed -1 transition (completes before first poll,
            #    confirmed by 2 consecutive settled readings)
            fake.calibrate_moving_polls = 0
            settled, polls = wheel.calibrate(poll_interval=0.001, timeout=0.05)
            self.assertEqual((settled, polls), (0, 0))

            # 2. Single pre-motion sample (pos=3) followed by -1 moving polls and then 2 settled
            #    readings at 0 must NOT prematurely complete on the first poll
            fake.position_sequence = [3, -1, -1, 0, 0]
            settled2, polls2 = wheel.calibrate(poll_interval=0.001, timeout=0.05)
            self.assertEqual((settled2, polls2), (0, 2))
            self.assertEqual(fake.position_sequence, [])

            # 3. Normal calibration (-1, -1, then 0, 0)
            fake.position_sequence = None
            fake.calibrate_moving_polls = 2
            settled3, polls3 = wheel.calibrate(poll_interval=0.001, timeout=0.05)
            self.assertEqual((settled3, polls3), (0, 2))

            # 4. Never-completing calibration terminates with ZwoTimeoutError and marks FAULT
            fake.stuck_moving = True
            with self.assertRaises(ZwoTimeoutError):
                wheel.calibrate(poll_interval=0.002, timeout=0.01)
            self.assertEqual(wheel.state, DeviceState.FAULT)

    def test_disconnect_removed_error_fault_state_and_reconcile_or_reopen_recovery(self) -> None:
        fake = FakeEfwSDK(slot_num=7, initial_slot=1)
        wheel = EFW(sdk=fake)  # type: ignore[arg-type]
        wheel.open()
        self.assertEqual(wheel.state, DeviceState.READY)
        self.assertIsNotNone(wheel.info)

        # Simulate USB disconnect (EFW_ERROR_REMOVED) during a slot query
        removed_err = ZwoError(
            "EFWGetPosition(0)",
            EFWErrorCode.EFW_ERROR_REMOVED,
            "EFW_ERROR_REMOVED",
        )
        fake.fail_on["get_position"] = removed_err
        with self.assertRaises(ZwoError) as ctx:
            wheel.get_slot()
        self.assertEqual(ctx.exception.code, EFWErrorCode.EFW_ERROR_REMOVED)
        self.assertEqual(wheel.state, DeviceState.FAULT)
        self.assertIsNone(wheel.info)

        # Subsequent operations fail fast in FAULT state
        with self.assertRaises(ZwoError) as fault_ctx:
            wheel.move_to_slot(2)
        self.assertEqual(fault_ctx.exception.code, EFWErrorCode.EFW_ERROR_ERROR_STATE)

        # Reconcile while device is still removed fails and keeps state FAULT
        fake.fail_on["get_property"] = removed_err
        with self.assertRaises(ZwoError):
            wheel.reconcile()
        self.assertEqual(wheel.state, DeviceState.FAULT)
        self.assertIsNone(wheel.info)

        # After physical USB reconnect, close() + open() re-enumerates via get_num()/get_id()
        del fake.fail_on["get_position"]
        del fake.fail_on["get_property"]
        wheel.close()
        self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
        num_calls_before = fake.get_num_calls
        id_calls_before = fake.get_id_calls
        info = wheel.open()
        self.assertEqual(fake.get_num_calls, num_calls_before + 1)
        self.assertEqual(fake.get_id_calls, id_calls_before + 1)
        self.assertEqual(wheel.state, DeviceState.READY)
        self.assertEqual(info.slot_num, 7)
        wheel.close()


def _efw_connected() -> bool:
    try:
        return ZwoEfwSDK().get_num() > 0
    except Exception:
        return False


class TestEfwHardware(unittest.TestCase):
    """Real-hardware tests (skipped in CI or when no ZWO EFW is attached).

    - ``test_hardware_readonly_open_and_query`` is read-only and runs whenever a
      physical ZWO EFW is connected.
    - ``test_hardware_move_next_slot_and_back`` physically moves the wheel to
      ``(slot + 1) % slot_num`` and back, and requires explicit operator confirmation
      via ``PYZWO_RUN_HARDWARE_MOTION_TESTS=1``.

    Warning: Do not run motion or USB unbind/rebind tests on equipment mounted on
    an active telescope or in use during observations.
    """

    @unittest.skipUnless(_efw_connected(), "No physical ZWO EFW connected via USB")
    def test_hardware_readonly_open_and_query(self) -> None:
        num_wheels = ZwoEfwSDK().get_num()
        self.assertGreater(num_wheels, 0)
        for idx in range(num_wheels):
            with self.subTest(device_index=idx):
                with EFW(device_index=idx) as wheel:
                    assert wheel.info is not None
                    dev_id = wheel.info.id
                    self.assertGreater(wheel.info.slot_num, 0)
                    slot = wheel.get_slot()
                    self.assertTrue(
                        0 <= slot < wheel.info.slot_num,
                        f"Unexpected slot={slot} for slot_num={wheel.info.slot_num}",
                    )
                with self.assertRaises(ZwoError) as ctx_closed:
                    wheel.sdk.get_position(dev_id)
                self.assertEqual(ctx_closed.exception.code, EFWErrorCode.EFW_ERROR_CLOSED)
                self.assertEqual(ctx_closed.exception.code_name, "EFW_ERROR_CLOSED")

    @unittest.skipUnless(_efw_connected(), "No physical ZWO EFW connected via USB")
    @unittest.skipUnless(
        os.environ.get("PYZWO_RUN_HARDWARE_MOTION_TESTS") == "1",
        "Set PYZWO_RUN_HARDWARE_MOTION_TESTS=1 to run physical filter wheel motion tests",
    )
    def test_hardware_move_next_slot_and_back(self) -> None:
        num_wheels = ZwoEfwSDK().get_num()
        self.assertGreater(num_wheels, 0)
        for idx in range(num_wheels):
            with self.subTest(device_index=idx), EFW(device_index=idx) as wheel:
                assert wheel.info is not None
                slot_num = wheel.info.slot_num
                self.assertGreater(slot_num, 1)
                start_slot = wheel.get_slot()
                self.assertTrue(0 <= start_slot < slot_num)
                target_slot = (start_slot + 1) % slot_num

                settled_next, _ = wheel.move_to_slot(target_slot, poll_interval=0.05, timeout=30.0)
                self.assertEqual(settled_next, target_slot)
                self.assertEqual(wheel.get_slot(), target_slot)
                self.assertEqual(wheel.state, DeviceState.READY)

                settled_back, _ = wheel.move_to_slot(start_slot, poll_interval=0.05, timeout=30.0)
                self.assertEqual(settled_back, start_slot)
                self.assertEqual(wheel.get_slot(), start_slot)
                self.assertEqual(wheel.state, DeviceState.READY)


if __name__ == "__main__":
    unittest.main()
