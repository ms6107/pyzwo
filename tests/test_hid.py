# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Unit tests for pyzwohid.hid."""

from __future__ import annotations

import unittest

from pyzwohid import ZwoHid


class TestZwoHid(unittest.TestCase):
    def test_subclass_requires_non_empty_pids(self) -> None:
        with self.assertRaises(TypeError):

            class _MissingPidsDevice(ZwoHid):
                pass


if __name__ == "__main__":
    unittest.main()
