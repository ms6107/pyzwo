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
                        self.assertTrue(focuser.model)
                        status = await focuser.get_status()
                        self.assertTrue(1 <= status.max_step <= focuser.step_range)
                        self.assertTrue(0 <= status.step <= status.max_step)
                        self.assertFalse(status.moving)


if __name__ == "__main__":
    unittest.main()
