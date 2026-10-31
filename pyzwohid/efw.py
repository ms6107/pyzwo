# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based controller for ZWO Electronic Filter Wheels (EFW)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic

from .hid import ZwoError, ZwoHidDevice, _validate_timeout

# Status query (02 01) and reply decoding
_CMD_GET_STATUS = b"\x02\x01"
_STATE_CALIBRATING = 0x00
_STATE_SETTLED = 0x01
_STATE_ERROR = 0x06
_DUAL_DISC_OFFSET = 12
_MAX_SLOTS = 16

# Action commands (01 <subcmd> ...) and polling
_CMD_CALIBRATE = b"\x01\x01"
_CMD_CLEAR_ERROR = b"\x01\x0f"
_CMD_MOVE = 0x01
_DIR_BIDIRECTIONAL = 0x02
_DIR_UNIDIRECTIONAL = 0x03
_POLL_INTERVAL_S = 0.2


@dataclass(frozen=True, slots=True)
class EfwStatus:
    """Snapshot of EFW status from a single 0x01 status report.

    Attributes:
        slot_count: Total slots on the wheel (1..16), or -1 while calibrating (state 0x00).
        slot: Settled 1-based slot (1..slot_count), or -1 while moving or calibrating.
    """

    slot_count: int
    slot: int


class Efw(ZwoHidDevice):
    """Asynchronous ZWO EFW controller."""

    PIDS = (0x1F01, 0x1F02)

    async def get_status(self) -> EfwStatus:
        """Return EfwStatus(slot_count, slot).

        slot_count is 1..16, or -1 while Hall-sensor calibration is in progress (state 0x00).
        slot is 1..slot_count when settled, or -1 while moving or calibrating.
        """
        reply = await self._query(_CMD_GET_STATUS)
        # (prev_slot, next_slot) is the bounding slot pair of the current carousel position:
        # (N, N) when centered over slot N, or (N, N + 1) / (slot_count, 1) when between slots
        # (ordered by slot number, independent of CW/CCW rotation direction).
        state, error_code, target_slot, prev_slot, next_slot, slot_count = reply[4:10]
        if reply[_DUAL_DISC_OFFSET] != 0:
            raise ZwoError(f"Dual-disc EFW format not supported: {reply.hex(' ')}")
        if state == _STATE_ERROR:
            raise ZwoError(f"EFW hardware error (code={error_code}): {reply.hex(' ')}")
        if state == _STATE_CALIBRATING:
            # During Hall-sensor calibration (power-on or calibrate()), reply[9] is the live
            # magnet counter (1..slot_count) rather than the total slot count, and reply[6:9]
            # stays frozen at the pre-calibration slot until the final homing step onto slot 1.
            return EfwStatus(slot_count=-1, slot=-1)
        if not (1 <= slot_count <= _MAX_SLOTS):
            raise ZwoError(f"Invalid EFW slot_count={slot_count}: {reply.hex(' ')}")
        if state == _STATE_SETTLED and target_slot == prev_slot == next_slot and 1 <= target_slot <= slot_count:
            return EfwStatus(slot_count=slot_count, slot=target_slot)
        return EfwStatus(slot_count=slot_count, slot=-1)

    async def move_to_slot(
        self,
        slot: int,
        *,
        unidirectional: bool = False,
        timeout: float = 30.0,
    ) -> int:
        """Move to 1-based slot (1..slot_count) and poll until settled. Returns settled slot."""
        _validate_timeout(timeout)

        status = await self.get_status()
        if status.slot == -1:
            raise ZwoError("EFW is already moving (-1)")
        if not (1 <= slot <= status.slot_count):
            raise ValueError(f"slot={slot} outside allowed range [1, {status.slot_count}]")

        direction = _DIR_UNIDIRECTIONAL if unidirectional else _DIR_BIDIRECTIONAL
        await self._send(bytes((_CMD_MOVE, direction, slot)))
        return await self._wait_until_settled(timeout, f"move_to_slot({slot})")

    async def calibrate(self, timeout: float = 60.0) -> int:
        """Run a full Hall-sensor calibration sweep and poll until settled (at slot 1). Returns settled slot."""
        _validate_timeout(timeout)

        if (await self.get_status()).slot == -1:
            raise ZwoError("EFW is already moving (-1)")

        await self._send(_CMD_CALIBRATE)
        return await self._wait_until_settled(timeout, "calibrate()")

    async def clear_error(self) -> None:
        """Send a fire-and-forget command (`01 0f`) to clear latched MCU hardware error state (`0x06`)."""
        await self._send(_CMD_CLEAR_ERROR)

    async def _wait_until_settled(self, timeout: float, operation: str) -> int:
        deadline = monotonic() + timeout
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{operation} timed out after {timeout:.2f}s")
            await asyncio.sleep(min(_POLL_INTERVAL_S, remaining))
            # The final query waking at `deadline` may finish slightly past `deadline`;
            # if the wheel has settled by then, return the settled slot rather than raising.
            current_slot = (await self.get_status()).slot
            if current_slot != -1:
                return current_slot
