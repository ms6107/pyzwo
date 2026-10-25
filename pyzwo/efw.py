# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based controller for ZWO Electronic Filter Wheels (EFW)."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .hid import ZwoError, ZwoHid

# Status query (02 01) and reply decoding
_CMD_GET_STATUS = b"\x02\x01"
_STATE_SETTLED = 0x01
_STATE_ERROR = 0x06
_DUAL_DISC_OFFSET = 12
_MAX_SLOTS = 16

# Move command (01 <dir> <slot>) and polling
_CMD_MOVE = 0x01
_DIR_BIDIRECTIONAL = 0x02
_DIR_UNIDIRECTIONAL = 0x03
_POLL_INTERVAL_S = 0.2


@dataclass(frozen=True, slots=True)
class EfwStatus:
    """Snapshot of EFW status from a single 0x01 status report."""

    slot_count: int
    slot: int


class Efw(ZwoHid):
    """Synchronous ZWO EFW controller."""

    PIDS = (0x1F01, 0x1F02)

    def get_status(self) -> EfwStatus:
        """Return EfwStatus(slot_count, slot), where slot is 1..slot_count or -1 while moving."""
        reply = self._query(_CMD_GET_STATUS)
        # (prev_slot, next_slot) is the bounding slot pair of the current carousel position:
        # (N, N) when centered over slot N, or (N, N + 1) / (slot_count, 1) when between slots
        # (ordered by slot number, independent of CW/CCW rotation direction).
        state, error_code, target_slot, prev_slot, next_slot, slot_count = reply[4:10]
        if reply[_DUAL_DISC_OFFSET] != 0:
            raise ZwoError(f"Dual-disc EFW format not supported: {reply.hex(' ')}")
        if state == _STATE_ERROR:
            raise ZwoError(f"EFW hardware error (code={error_code}): {reply.hex(' ')}")
        if not (1 <= slot_count <= _MAX_SLOTS):
            raise ZwoError(f"Invalid EFW slot_count={slot_count}: {reply.hex(' ')}")
        if state == _STATE_SETTLED and target_slot == prev_slot == next_slot and 1 <= target_slot <= slot_count:
            return EfwStatus(slot_count=slot_count, slot=target_slot)
        return EfwStatus(slot_count=slot_count, slot=-1)

    def move_to_slot(
        self,
        slot: int,
        *,
        unidirectional: bool = False,
        timeout: float = 30.0,
    ) -> int:
        """Move to 1-based slot (1..slot_count) and poll until settled. Returns settled slot."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"timeout must be a positive finite number, got {timeout!r}")

        status = self.get_status()
        if not (1 <= slot <= status.slot_count):
            raise ValueError(f"slot={slot} outside allowed range [1, {status.slot_count}]")
        if status.slot == -1:
            raise ZwoError("EFW is already moving (-1)")

        direction = _DIR_UNIDIRECTIONAL if unidirectional else _DIR_BIDIRECTIONAL
        self._send(bytes((_CMD_MOVE, direction, slot)))

        deadline = time.monotonic() + timeout
        while True:
            time.sleep(_POLL_INTERVAL_S)
            current_slot = self.get_status().slot
            if current_slot != -1:
                return current_slot
            if time.monotonic() >= deadline:
                raise TimeoutError(f"move_to_slot({slot}) timed out after {timeout:.2f}s")
