# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Opt-in real-hardware tests for pyzwohid.eaf."""

from __future__ import annotations

import os
import tempfile
import unittest

from pyzwohid import Eaf


@unittest.skipUnless(
    os.environ.get("PYZWOHID_RUN_HARDWARE_TESTS") == "1" or os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") == "1",
    "Set PYZWOHID_RUN_HARDWARE_TESTS=1 to run real-hardware tests",
)
class TestEafHardware(unittest.IsolatedAsyncioTestCase):
    """Real-hardware tests for ZWO EAF over USB HID.

    - ``test_hardware_readonly_status`` is read-only and queries ``get_status()``.
    - ``test_hardware_move_and_back`` physically moves the focuser by 20 steps and back,
      and requires explicit operator confirmation via ``PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1``.
    """

    async def asyncSetUp(self) -> None:
        self.paths = await Eaf.discover()
        if not self.paths:
            self.skipTest("No physical ZWO EAF discovered via USB HID")

    async def test_hardware_readonly_status(self) -> None:
        for path in self.paths:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmpdir:
                link_path = os.path.join(tmpdir, "zwo-eaf")
                os.symlink(path, link_path)
                for target_path in (path, link_path):
                    async with await Eaf.open(target_path) as focuser:
                        self.assertIn(focuser.step_range, (60_000, 600_000))
                        self.assertNotEqual(focuser.firmware_version, (0, 0, 0))
                        status = await focuser.get_status()
                        self.assertTrue(1 <= status.max_step <= focuser.step_range)
                        self.assertTrue(0 <= status.step <= status.max_step)
                        self.assertFalse(status.moving)

    @unittest.skipUnless(
        os.environ.get("PYZWOHID_RUN_HARDWARE_MOTION_TESTS") == "1",
        "Set PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 to run physical focuser motion tests",
    )
    async def test_hardware_move_and_back(self) -> None:
        for path in self.paths:
            with self.subTest(path=path):
                async with await Eaf.open(path) as focuser:
                    status = await focuser.get_status()
                    self.assertFalse(status.moving)
                    self.assertGreaterEqual(status.max_step, 20)
                    start_step = status.step
                    target_step = start_step + 20 if start_step + 20 <= status.max_step else start_step - 20

                    settled_next = await focuser.move(target_step, timeout=30.0)
                    self.assertEqual(settled_next, target_step)
                    self.assertEqual((await focuser.get_status()).step, target_step)

                    settled_back = await focuser.move(start_step, timeout=30.0)
                    self.assertEqual(settled_back, start_step)
                    self.assertEqual((await focuser.get_status()).step, start_step)
                    self.assertEqual(await focuser.stop(timeout=5.0), start_step)


if __name__ == "__main__":
    unittest.main()
