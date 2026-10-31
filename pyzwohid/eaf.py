# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Path-based controller for ZWO Electronic Automatic Focusers (EAF)."""

from __future__ import annotations

from typing import Any, Self

from .hid import ZwoError, ZwoHidDevice

# Firmware query (02 04) and coordinate-width split (fw <= 3.3.5 vs fw > 3.3.5)
_CMD_GET_FIRMWARE = b"\x02\x04"
_LEGACY_FW_LIMIT = (3, 3, 5)
_LEGACY_STEP_RANGE = 60_000
_MODERN_STEP_RANGE = 600_000

# Action commands
_CMD_CLEAR_ERROR = b"\x01\x0f"


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

    async def clear_error(self) -> None:
        """Send a fire-and-forget command (`01 0f`) to clear latched MCU hardware error state (`0x06`)."""
        await self._send(_CMD_CLEAR_ERROR)
