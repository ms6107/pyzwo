# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based controller for ZWO Electronic Automatic Focusers (EAF)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

from .hid import ZwoError, ZwoHidDevice

# Firmware query (02 04) and coordinate-width split (fw <= 3.3.5 vs fw > 3.3.5)
_CMD_GET_FIRMWARE = b"\x02\x04"
_LEGACY_FW_LIMIT = (3, 3, 5)
_LEGACY_STEP_RANGE = 60_000
_MODERN_STEP_RANGE = 600_000

# Status query (02 03) and reply decoding
_CMD_GET_STATUS = b"\x02\x03"
_STATE_SETTLED = 0x00
_STATE_MOVING = 0x01
_STATE_ERROR = 0x06

# Master control frame (03 <run> <backlash> <b6..b9> <opcode> 00 00 <flags> <max_hi..lo>) and actions
_CMD_CONTROL = 0x03
_RUN_STOP = 0x00
_OPCODE_RESET_POSITION = 0x01
_CMD_CLEAR_ERROR = b"\x01\x0f"


@dataclass(frozen=True, slots=True)
class EafStatus:
    """Snapshot of EAF status from a single 0x03 status report.

    Attributes:
        step: Current focuser position in steps (0..max_step).
        max_step: Configured maximum step limit (1..step_range).
        moving: True while the focuser is moving.
    """

    step: int
    max_step: int
    moving: bool


class Eaf(ZwoHidDevice):
    """Asynchronous ZWO EAF focuser controller."""

    PIDS = (0x1F10,)

    def __init__(
        self,
        path: str,
        device: Any,
        firmware_version: tuple[int, int, int],
        model: str,
    ) -> None:
        super().__init__(path, device)
        self.firmware_version = firmware_version
        self.model = model
        self.step_range = _MODERN_STEP_RANGE if firmware_version > _LEGACY_FW_LIMIT else _LEGACY_STEP_RANGE

    @classmethod
    async def open(cls, path: str) -> Self:
        """Verify and open the EAF at `path`, reading its firmware version and model."""
        transport = ZwoHidDevice(path, await cls._open_handle(path))
        try:
            reply = await transport._query(_CMD_GET_FIRMWARE)
            major, minor, build = reply[4:7]
            if major == 0 and minor == 0 and build == 0:
                raise ZwoError(f"Invalid EAF firmware version 0.0.0: {reply.hex(' ')}")
            model = reply[7:16].lstrip(b"\x00").split(b"\x00", 1)[0].decode("ascii", errors="replace")
        except BaseException:
            await transport.close()
            raise
        return cls(path, transport._device, (major, minor, build), model)

    async def _read_status_report(self) -> tuple[EafStatus, bytes]:
        reply = await self._query(_CMD_GET_STATUS)
        state = reply[4]
        if state == _STATE_ERROR:
            raise ZwoError(f"EAF hardware error: {reply.hex(' ')}")
        if state not in (_STATE_SETTLED, _STATE_MOVING):
            raise ZwoError(f"Unexpected EAF state=0x{state:02x}: {reply.hex(' ')}")

        if self.firmware_version > _LEGACY_FW_LIMIT:
            step = int.from_bytes(reply[7:10], "big")
            max_step = (reply[6] << 16) | int.from_bytes(reply[14:16], "big")
        else:
            step = int.from_bytes(reply[8:10], "big")
            max_step = int.from_bytes(reply[14:16], "big")

        if not (1 <= max_step <= self.step_range):
            raise ZwoError(f"Invalid EAF max_step={max_step} (step_range={self.step_range}): {reply.hex(' ')}")
        if not (0 <= step <= max_step):
            raise ZwoError(f"Invalid EAF step={step} (max_step={max_step}): {reply.hex(' ')}")

        return EafStatus(step=step, max_step=max_step, moving=state == _STATE_MOVING), reply

    async def get_status(self) -> EafStatus:
        """Return EafStatus(step, max_step, moving) from a 0x03 status report."""
        status, _ = await self._read_status_report()
        return status

    def _build_control_payload(self, status_reply: bytes, *, run: int, step: int, opcode: int) -> bytes:
        backlash = status_reply[5]
        flags = status_reply[13]
        max_step_lo = status_reply[14:16]
        if self.firmware_version > _LEGACY_FW_LIMIT:
            coord_bytes = bytes((status_reply[6],)) + step.to_bytes(3, "big")
        else:
            coord_bytes = status_reply[6:8] + step.to_bytes(2, "big")
        return bytes((_CMD_CONTROL, run, backlash)) + coord_bytes + bytes((opcode, 0x00, 0x00, flags)) + max_step_lo

    async def reset_position(self, step: int) -> None:
        """Overwrite the current focuser step coordinate (0..max_step) in the MCU without moving."""
        status, reply = await self._read_status_report()
        if status.moving:
            raise ZwoError("EAF is already moving")
        if not (0 <= step <= status.max_step):
            raise ValueError(f"step={step} outside allowed range [0, {status.max_step}]")

        await self._send(self._build_control_payload(reply, run=_RUN_STOP, step=step, opcode=_OPCODE_RESET_POSITION))

    async def clear_error(self) -> None:
        """Send a fire-and-forget command (`01 0f`) to clear latched MCU hardware error state (`0x06`)."""
        await self._send(_CMD_CLEAR_ERROR)
