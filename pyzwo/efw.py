# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZWO EFW (Electronic Filter Wheel) SDK ctypes bindings and synchronous controller."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ._efw_sdk import EFWErrorCode, EfwSDKProtocol, ZwoEfwSDK
from ._loader import (
    DeviceState,
    ZwoError,
    ZwoTimeoutError,
    attach_rollback_error,
    validate_timeout,
)


@dataclass(frozen=True)
class EfwInfo:
    """Snapshot of ZWO EFW device properties."""

    id: int
    name: str
    slot_num: int
    firmware_version: str
    unidirectional: bool


class FilterWheel:
    """Synchronous, framework-agnostic ZWO EFW controller.

    Usage contract, deadlines & recovery semantics:
    - Single-owner, single-thread only. Concurrent calls across threads or simultaneous
      direct ``ZwoEfwSDK`` and ``FilterWheel`` calls on the same device are unsupported.
    - ``FilterWheel.open(...)`` is a static factory that returns a healthy ``FilterWheel``
      in ``DeviceState.READY`` or raises an exception. It is non-destructive by default
      and only writes ``unidirectional`` if explicitly passed. If any initialization step
      fails after ``EFWOpen(id)`` succeeds, ``open()`` rolls back via ``EFWClose(id)`` to
      prevent handle leaks (though hardware settings already written are not reverted).
    - ``close()`` is idempotent when disconnected. If ``EFWClose`` fails, the handle
      remains retained and ``self.state`` becomes ``DeviceState.UNKNOWN`` so ``close()``
      can be retried.
    - ``timeout`` parameters on ``move_to_slot()`` and ``calibrate()`` enforce
      Python-level monotonic polling deadlines between SDK calls. A blocked native C
      call inside ``libEFWFilter`` has no pre-emptive timeout.
    - ``EFW_ERROR_REMOVED`` disconnects transition ``self.state`` to
      ``DeviceState.FAULT``. To recover, call ``close()`` and open a new
      ``FilterWheel`` via ``FilterWheel.open(...)``.
    """

    def __init__(
        self,
        efw_id: int,
        info: EfwInfo,
        sdk: EfwSDKProtocol,
    ) -> None:
        self.efw_id: int = efw_id
        self.info: EfwInfo = info
        self.sdk = sdk
        self.state: DeviceState = DeviceState.READY

    @staticmethod
    def check_device(vid: int, pid: int, *, sdk: EfwSDKProtocol | None = None) -> bool:
        """Return True if the USB (VID, PID) pair is a supported ZWO EFW device."""
        active_sdk = sdk if sdk is not None else ZwoEfwSDK()
        return active_sdk.check(vid, pid)

    @staticmethod
    def open(
        device_index: int = 0,
        unidirectional: bool | None = None,
        sdk: EfwSDKProtocol | None = None,
    ) -> FilterWheel:
        if device_index < 0:
            raise ZwoError(
                f"EFWGetNum(index={device_index})",
                EFWErrorCode.EFW_ERROR_INVALID_INDEX,
                "EFW_ERROR_INVALID_INDEX",
            )
        active_sdk = sdk if sdk is not None else ZwoEfwSDK()
        num = active_sdk.get_num()
        if device_index >= num:
            raise ZwoError(
                f"EFWGetNum(index={device_index})",
                EFWErrorCode.EFW_ERROR_INVALID_INDEX,
                "EFW_ERROR_INVALID_INDEX",
            )

        efw_id = active_sdk.get_id(device_index)
        active_sdk.open(efw_id)
        try:
            if unidirectional is not None:
                active_sdk.set_direction(efw_id, unidirectional)
            unidir = active_sdk.get_direction(efw_id)

            _, dev_name, slot_num = active_sdk.get_property(efw_id)
            if slot_num <= 0:
                raise ZwoError(
                    f"EFWGetProperty({efw_id}) reported invalid slot_num={slot_num} during open",
                    EFWErrorCode.EFW_ERROR_ERROR_STATE,
                    "EFW_ERROR_ERROR_STATE",
                )
            pos = active_sdk.get_position(efw_id)
            if pos == -1:
                raise ZwoError(
                    f"EFWGetPosition({efw_id}) reported wheel moving (-1) during open",
                    EFWErrorCode.EFW_ERROR_MOVING,
                    "EFW_ERROR_MOVING",
                )

            fw = active_sdk.get_firmware_version(efw_id)
            info = EfwInfo(
                id=efw_id,
                name=dev_name,
                slot_num=slot_num,
                firmware_version=fw,
                unidirectional=unidir,
            )
            return FilterWheel(efw_id=efw_id, info=info, sdk=active_sdk)
        except BaseException as init_exc:
            try:
                active_sdk.close(efw_id)
            except BaseException as close_exc:
                attach_rollback_error(init_exc, efw_id, close_exc)
            raise

    def close(self) -> None:
        if self.state == DeviceState.DISCONNECTED:
            return
        try:
            self.sdk.close(self.efw_id)
        except BaseException:
            self.state = DeviceState.UNKNOWN
            raise
        self.state = DeviceState.DISCONNECTED

    def __enter__(self) -> FilterWheel:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _require_id(self) -> int:
        if self.state == DeviceState.UNKNOWN:
            raise ZwoError(
                "FilterWheel connection state is UNKNOWN; call close() to recover",
                EFWErrorCode.EFW_ERROR_ERROR_STATE,
                "EFW_ERROR_ERROR_STATE",
            )
        if self.state == DeviceState.FAULT:
            raise ZwoError(
                "FilterWheel is in FAULT state; close() and reopen required",
                EFWErrorCode.EFW_ERROR_ERROR_STATE,
                "EFW_ERROR_ERROR_STATE",
            )
        if self.state == DeviceState.DISCONNECTED:
            raise ZwoError("FilterWheel", EFWErrorCode.EFW_ERROR_CLOSED, "EFW_ERROR_CLOSED")
        return self.efw_id

    def _record_op_error(self, exc: BaseException) -> None:
        if isinstance(exc, ZwoError) and exc.code == EFWErrorCode.EFW_ERROR_REMOVED:
            self.state = DeviceState.FAULT

    def _wait_until_settled(self, op: str, timeout: float) -> tuple[int, int]:
        deadline = time.monotonic() + timeout
        moving_polls = 0
        while True:
            pos = self.get_slot()
            if pos != -1:
                self.state = DeviceState.READY
                return (pos, moving_polls)
            moving_polls += 1
            if time.monotonic() >= deadline:
                raise ZwoTimeoutError(
                    f"{op} timed out after {timeout:.2f}s "
                    f"(last pos={pos}, moving_polls={moving_polls}); physical outcome unknown"
                )

    def get_slot(self) -> int:
        """Return current 0-based slot (``0 .. slot_num - 1``), or ``-1`` if moving."""
        efw_id = self._require_id()
        try:
            return self.sdk.get_position(efw_id)
        except BaseException as exc:
            self._record_op_error(exc)
            raise

    def move_to_slot(
        self,
        slot: int,
        timeout: float = 60.0,
    ) -> tuple[int, int]:
        """Move to 0-based SDK slot and wait until settled. Returns ``(settled_slot, moving_polls)``."""
        validate_timeout(timeout)
        efw_id = self._require_id()

        slot_num = self.info.slot_num
        if slot < 0 or slot >= slot_num:
            raise ZwoError(
                f"EFWSetPosition({efw_id}, {slot}) outside allowed range [0, {slot_num - 1}]",
                EFWErrorCode.EFW_ERROR_INVALID_VALUE,
                "EFW_ERROR_INVALID_VALUE",
            )

        cur = self.get_slot()
        if cur == -1:
            raise ZwoError(
                f"EFWGetPosition({efw_id}) reported wheel is already moving (-1)",
                EFWErrorCode.EFW_ERROR_MOVING,
                "EFW_ERROR_MOVING",
            )
        if cur == slot:
            return (slot, 0)

        self.state = DeviceState.BUSY
        try:
            self.sdk.set_position(efw_id, slot)
        except BaseException as exc:
            self._record_op_error(exc)
            raise
        return self._wait_until_settled(f"EFWSetPosition({efw_id}, {slot})", timeout)

    def calibrate(
        self,
        timeout: float = 60.0,
    ) -> tuple[int, int]:
        """Run full hardware calibration (``EFWCalibrate``) and wait until settled."""
        validate_timeout(timeout)
        efw_id = self._require_id()
        self.state = DeviceState.BUSY
        try:
            self.sdk.calibrate(efw_id)
        except BaseException as exc:
            self._record_op_error(exc)
            raise
        return self._wait_until_settled(f"EFWCalibrate({efw_id})", timeout)


EFW = FilterWheel

__all__ = [
    "DeviceState",
    "EFW",
    "EFWErrorCode",
    "EfwInfo",
    "FilterWheel",
    "ZwoEfwSDK",
]
