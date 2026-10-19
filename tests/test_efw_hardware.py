# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Opt-in real-hardware tests for pyzwo.efw."""

from __future__ import annotations

import os
import unittest

from pyzwo import DeviceState, ZwoError
from pyzwo.efw import EFW, EFWErrorCode, ZwoEfwSDK


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

    def setUp(self) -> None:
        if not _efw_connected():
            self.skipTest("No physical ZWO EFW connected via USB")

    def test_hardware_readonly_open_and_query(self) -> None:
        num_wheels = ZwoEfwSDK().get_num()
        self.assertGreater(num_wheels, 0)
        for idx in range(num_wheels):
            with self.subTest(device_index=idx):
                with EFW(device_index=idx) as wheel:
                    self.assertEqual(wheel.state, DeviceState.READY)
                    self.assertIsNotNone(wheel.info)
                    assert wheel.info is not None
                    dev_id = wheel.info.id
                    self.assertGreater(wheel.info.slot_num, 0)
                    slot = wheel.get_slot()
                    self.assertTrue(
                        0 <= slot < wheel.info.slot_num,
                        f"Unexpected slot={slot} for slot_num={wheel.info.slot_num}",
                    )
                self.assertEqual(wheel.state, DeviceState.DISCONNECTED)
                self.assertIsNone(wheel.info)
                with self.assertRaises(ZwoError) as ctx_closed:
                    wheel.sdk.get_position(dev_id)
                self.assertEqual(ctx_closed.exception.code, EFWErrorCode.EFW_ERROR_CLOSED)
                self.assertEqual(ctx_closed.exception.code_name, "EFW_ERROR_CLOSED")

    @unittest.skipUnless(
        os.environ.get("PYZWO_RUN_HARDWARE_MOTION_TESTS") == "1",
        "Set PYZWO_RUN_HARDWARE_MOTION_TESTS=1 to run physical filter wheel motion tests",
    )
    def test_hardware_move_next_slot_and_back(self) -> None:
        num_wheels = ZwoEfwSDK().get_num()
        self.assertGreater(num_wheels, 0)
        for idx in range(num_wheels):
            with self.subTest(device_index=idx), EFW(device_index=idx) as wheel:
                self.assertIsNotNone(wheel.info)
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
