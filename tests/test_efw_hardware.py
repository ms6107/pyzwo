# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Opt-in real-hardware tests for pyzwohid.efw."""

from __future__ import annotations

import os
import tempfile
import unittest

from pyzwohid import Efw


@unittest.skipUnless(
    os.environ.get("PYZWOHID_RUN_HARDWARE_TESTS") == "1" or os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") == "1",
    "Set PYZWOHID_RUN_HARDWARE_TESTS=1 to run real-hardware tests",
)
class TestEfwHardware(unittest.IsolatedAsyncioTestCase):
    """Real-hardware tests for ZWO EFW over USB HID.

    - ``test_hardware_readonly_status`` is read-only and queries ``get_status()``.
    - ``test_hardware_move_next_slot_and_back`` physically moves the wheel to
      ``(slot % slot_count) + 1`` and back, and requires explicit operator confirmation
      via ``PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1``.
    """

    async def asyncSetUp(self) -> None:
        self.paths = await Efw.discover()
        if not self.paths:
            self.skipTest("No physical ZWO EFW discovered via USB HID")

    async def test_hardware_readonly_status(self) -> None:
        for path in self.paths:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmpdir:
                link_path = os.path.join(tmpdir, "zwo-efw")
                os.symlink(path, link_path)
                for target_path in (path, link_path):
                    async with await Efw.open(target_path) as wheel:
                        status = await wheel.get_status()
                        self.assertTrue(1 <= status.slot_count <= 16)
                        self.assertTrue(1 <= status.slot <= status.slot_count)

    @unittest.skipUnless(
        os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") == "1",
        "Set PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 to run physical filter wheel motion tests",
    )
    async def test_hardware_move_next_slot_and_back(self) -> None:
        for path in self.paths:
            with self.subTest(path=path):
                async with await Efw.open(path) as wheel:
                    status = await wheel.get_status()
                    self.assertGreater(status.slot_count, 1)
                    start_slot = status.slot
                    self.assertTrue(1 <= start_slot <= status.slot_count)
                    target_slot = (start_slot % status.slot_count) + 1

                    settled_next = await wheel.move_to_slot(target_slot, timeout=30.0)
                    self.assertEqual(settled_next, target_slot)
                    self.assertEqual((await wheel.get_status()).slot, target_slot)

                    settled_back = await wheel.move_to_slot(start_slot, timeout=30.0)
                    self.assertEqual(settled_back, start_slot)
                    self.assertEqual((await wheel.get_status()).slot, start_slot)


if __name__ == "__main__":
    unittest.main()
