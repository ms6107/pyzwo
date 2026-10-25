# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Opt-in real-hardware tests for pyzwo.efw."""

from __future__ import annotations

import os
import unittest

from pyzwo import Efw


@unittest.skipUnless(
    os.environ.get("PYZWO_RUN_HARDWARE_TESTS") == "1" or os.environ.get("PYZWO_RUN_HARDWARE_MOTION_TESTS") == "1",
    "Set PYZWO_RUN_HARDWARE_TESTS=1 to run real-hardware tests",
)
class TestEfwHardware(unittest.TestCase):
    """Real-hardware tests for ZWO EFW over USB HID.

    - ``test_hardware_readonly_status`` is read-only and queries ``get_status()``.
    - ``test_hardware_move_next_slot_and_back`` physically moves the wheel to
      ``(slot % slot_count) + 1`` and back, and requires explicit operator confirmation
      via ``PYZWO_RUN_HARDWARE_MOTION_TESTS=1``.
    """

    def setUp(self) -> None:
        self.paths = Efw.discover()
        if not self.paths:
            self.skipTest("No physical ZWO EFW discovered via USB HID")

    def test_hardware_readonly_status(self) -> None:
        for path in self.paths:
            with self.subTest(path=path):
                status = Efw(path).get_status()
                self.assertTrue(1 <= status.slot_count <= 16)
                self.assertTrue(1 <= status.slot <= status.slot_count)

    @unittest.skipUnless(
        os.environ.get("PYZWO_RUN_HARDWARE_MOTION_TESTS") == "1",
        "Set PYZWO_RUN_HARDWARE_MOTION_TESTS=1 to run physical filter wheel motion tests",
    )
    def test_hardware_move_next_slot_and_back(self) -> None:
        for path in self.paths:
            with self.subTest(path=path):
                wheel = Efw(path)
                status = wheel.get_status()
                self.assertGreater(status.slot_count, 1)
                start_slot = status.slot
                self.assertTrue(1 <= start_slot <= status.slot_count)
                target_slot = (start_slot % status.slot_count) + 1

                settled_next = wheel.move_to_slot(target_slot, timeout=30.0)
                self.assertEqual(settled_next, target_slot)
                self.assertEqual(wheel.get_status().slot, target_slot)

                settled_back = wheel.move_to_slot(start_slot, timeout=30.0)
                self.assertEqual(settled_back, start_slot)
                self.assertEqual(wheel.get_status().slot, start_slot)


if __name__ == "__main__":
    unittest.main()
