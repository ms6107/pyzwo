# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""C-header verification, unit tests, and opt-in hardware tests for pyzwo.efw."""
from __future__ import annotations

import ctypes
import unittest
from unittest.mock import patch

from pyzwo import DeviceState, ZwoError, ZwoTimeoutError
from pyzwo.efw import EFWErrorCode, ZwoEfwSDK, _EFWInfo

try:
    from tests.header_checker import INCLUDE_DIR, FakeCDLL, parse_header_prototypes
except ModuleNotFoundError:
    from header_checker import INCLUDE_DIR, FakeCDLL, parse_header_prototypes  # type: ignore[no-redef]

EFW_HEADER = INCLUDE_DIR / "EFW_filter.h"


class FakeEfwSDK:
    """In-memory mock of ZwoEfwSDK for unit testing without vendor shared libraries."""

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
        return "1, 7"

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
        self.assertEqual(ctypes.sizeof(_EFWInfo), 4 + 64 + 4)
        for member in EFWErrorCode:
            self.assertIn(member.name, text, f"{member.name} missing from EFW_filter.h")

    def test_efw_ctypes_signatures_match_c_header(self) -> None:
        prototypes = parse_header_prototypes(
            EFW_HEADER,
            "EFW_API",
            extra_types={
                "EFW_INFO*": ctypes.POINTER(_EFWInfo),
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
            "EFWGetFirmwareVersion",
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


if __name__ == "__main__":
    unittest.main()
