# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Stateless path-based controller for ZWO Electronic Filter Wheels (EFW)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .hid import ZwoError, ZwoHid


@dataclass(frozen=True, slots=True)
class EfwStatus:
    """Snapshot of EFW status from a single 0x01 status report."""

    slot_num: int
    slot: int


class Efw(ZwoHid):
    """Stateless synchronous ZWO EFW controller."""

    PIDS = (0x1F01, 0x1F02)

    def get_status(self) -> EfwStatus:
        """Return EfwStatus(slot_num, slot), where slot is 1..slot_num or -1 while moving."""
        rx = self._query(0x01)
        if rx[12] != 0:
            raise ZwoError(f"Dual-disc EFW format not supported: {rx.hex(' ')}")
        if rx[4] == 0x06:
            raise ZwoError(f"EFW hardware error (code={rx[5]}): {rx.hex(' ')}")
        slot_num = rx[9]
        if not (1 <= slot_num <= 16):
            raise ZwoError(f"Invalid EFW slot_num={slot_num}: {rx.hex(' ')}")
        if rx[4] == 0x01 and rx[6] == rx[7] == rx[8] and 1 <= rx[7] <= slot_num:
            return EfwStatus(slot_num=slot_num, slot=rx[7])
        return EfwStatus(slot_num=slot_num, slot=-1)

    def move_to_slot(
        self,
        slot: int,
        *,
        unidirectional: bool = False,
        timeout: float = 30.0,
    ) -> int:
        """Move to 1-based slot (1..slot_num) and poll until settled. Returns settled slot."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")

        status = self.get_status()
        if not (1 <= slot <= status.slot_num):
            raise ValueError(f"slot={slot} outside allowed range [1, {status.slot_num}]")
        if status.slot == -1:
            raise ZwoError("EFW is already moving (-1)")

        dir_byte = 0x03 if unidirectional else 0x02
        self._send(bytes((0x01, dir_byte, slot)))

        deadline = time.monotonic() + timeout
        while True:
            time.sleep(0.2)
            cur = self.get_status().slot
            if cur != -1:
                return cur
            if time.monotonic() >= deadline:
                raise TimeoutError(f"move_to_slot({slot}) timed out after {timeout:.2f}s")
