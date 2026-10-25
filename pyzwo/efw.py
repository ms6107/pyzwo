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

from ._efw_sdk import EFWErrorCode, ZwoEfwSDK
from ._loader import (
    DeviceState,
    ZwoError,
    ZwoTimeoutError,
    attach_rollback_error,
    validate_poll_and_timeout,
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
    - ``close()`` is idempotent when disconnected and clears ``self.info``. If
      ``EFWClose`` fails, the handle remains retained and ``self.state`` becomes
      ``DeviceState.UNKNOWN`` so ``close()`` can be retried.
    - ``timeout`` parameters on ``open()``, ``move_to_slot()``, and ``calibrate()``
      enforce Python-level monotonic polling deadlines between SDK calls. A blocked
      native C call inside ``libEFWFilter`` has no pre-emptive timeout.
    - Failed or timed-out movements/calibrations and ``EFW_ERROR_REMOVED`` disconnects
      transition ``self.state`` to ``DeviceState.FAULT``. To recover, call ``close()``
      and open a new ``FilterWheel`` via ``FilterWheel.open(...)``.
    """

    def __init__(
        self,
        device_index: int,
        efw_id: int,
        info: EfwInfo,
        sdk: ZwoEfwSDK,
    ) -> None:
        self.device_index = device_index
        self.efw_id: int | None = efw_id
        self.info: EfwInfo | None = info
        self.sdk = sdk
        self.state: DeviceState = DeviceState.READY

    @staticmethod
    def check_device(vid: int, pid: int, *, sdk: ZwoEfwSDK | None = None) -> bool:
        """Return True if the USB (VID, PID) pair is a supported ZWO EFW device."""
        active_sdk = sdk if sdk is not None else ZwoEfwSDK()
        return active_sdk.check(vid, pid)

    @staticmethod
    def open(
        device_index: int = 0,
        unidirectional: bool | None = None,
        timeout: float = 15.0,
        poll_interval: float = 0.1,
        sdk: ZwoEfwSDK | None = None,
    ) -> FilterWheel:
        validate_poll_and_timeout(poll_interval, timeout)
        if isinstance(device_index, bool) or not isinstance(device_index, int) or device_index < 0:
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

            deadline = time.monotonic() + timeout
            dev_name = ""
            slot_num = 0
            while True:
                try:
                    _, dev_name, slot_num = active_sdk.get_property(efw_id)
                    if slot_num > 0:
                        pos = active_sdk.get_position(efw_id)
                        if pos != -1:
                            if pos < 0 or pos >= slot_num:
                                raise ZwoError(
                                    f"EFWGetPosition({efw_id}) reported invalid slot {pos} during open",
                                    EFWErrorCode.EFW_ERROR_ERROR_STATE,
                                    "EFW_ERROR_ERROR_STATE",
                                )
                            break
                        if time.monotonic() >= deadline:
                            raise ZwoTimeoutError(
                                f"EFWGetPosition({efw_id}) timed out after {timeout:.2f}s while wheel moving (-1)"
                            )
                    elif time.monotonic() >= deadline:
                        raise ZwoTimeoutError(
                            f"EFWGetProperty({efw_id}) timed out after {timeout:.2f}s with slot_num={slot_num}"
                        )
                except ZwoError as e:
                    if e.code != EFWErrorCode.EFW_ERROR_MOVING:
                        raise
                    if time.monotonic() >= deadline:
                        raise ZwoTimeoutError(
                            f"EFWGetProperty({efw_id}) timed out after {timeout:.2f}s while wheel moving"
                        ) from e
                time.sleep(poll_interval)

            fw = active_sdk.get_firmware_version(efw_id)
            info = EfwInfo(
                id=efw_id,
                name=dev_name,
                slot_num=slot_num,
                firmware_version=fw,
                unidirectional=unidir,
            )
            return FilterWheel(device_index=device_index, efw_id=efw_id, info=info, sdk=active_sdk)
        except BaseException as init_exc:
            try:
                active_sdk.close(efw_id)
            except BaseException as close_exc:
                attach_rollback_error(init_exc, efw_id, close_exc)
            raise

    def close(self) -> None:
        self.info = None
        if self.efw_id is None:
            self.state = DeviceState.DISCONNECTED
            return
        efw_id = self.efw_id
        try:
            self.sdk.close(efw_id)
        except BaseException:
            self.state = DeviceState.UNKNOWN
            raise

        self.efw_id = None
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
        if self.efw_id is None or self.state == DeviceState.DISCONNECTED:
            raise ZwoError("FilterWheel", EFWErrorCode.EFW_ERROR_CLOSED, "EFW_ERROR_CLOSED")
        return self.efw_id

    def _record_op_error(self, exc: BaseException) -> None:
        if isinstance(exc, ZwoError) and exc.code == EFWErrorCode.EFW_ERROR_REMOVED:
            self.info = None
            self.state = DeviceState.FAULT
        elif self.state == DeviceState.BUSY:
            self.state = DeviceState.FAULT

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
        poll_interval: float = 0.05,
        timeout: float = 60.0,
    ) -> tuple[int, int]:
        """Move to 0-based SDK slot and wait until settled. Returns ``(settled_slot, moving_polls)``."""
        validate_poll_and_timeout(poll_interval, timeout)
        efw_id = self._require_id()
        if self.state == DeviceState.BUSY:
            raise ZwoError("FilterWheel is busy", EFWErrorCode.EFW_ERROR_MOVING, "EFW_ERROR_MOVING")

        slot_num = self.info.slot_num if self.info is not None else 0
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0 or (slot_num > 0 and slot >= slot_num):
            upper = slot_num - 1 if slot_num > 0 else "?"
            raise ZwoError(
                f"EFWSetPosition({efw_id}, {slot}) outside allowed range [0, {upper}]",
                EFWErrorCode.EFW_ERROR_INVALID_VALUE,
                "EFW_ERROR_INVALID_VALUE",
            )

        self.state = DeviceState.BUSY
        try:
            cur = self.sdk.get_position(efw_id)
            if cur == -1:
                raise ZwoError(
                    f"EFWGetPosition({efw_id}) reported wheel is already moving (-1)",
                    EFWErrorCode.EFW_ERROR_MOVING,
                    "EFW_ERROR_MOVING",
                )
            if cur < 0 or (slot_num > 0 and cur >= slot_num):
                raise ZwoError(
                    f"EFWGetPosition({efw_id}) reported invalid slot {cur}",
                    EFWErrorCode.EFW_ERROR_ERROR_STATE,
                    "EFW_ERROR_ERROR_STATE",
                )
            if cur == slot:
                self.state = DeviceState.READY
                return (slot, 0)

            deadline = time.monotonic() + timeout
            self.sdk.set_position(efw_id, slot)
            moving_polls = 0
            wrong_settled_polls = 0
            while True:
                pos = self.sdk.get_position(efw_id)
                if pos == -1:
                    moving_polls += 1
                    wrong_settled_polls = 0
                elif pos == slot:
                    self.state = DeviceState.READY
                    return (pos, moving_polls)
                else:
                    wrong_settled_polls += 1
                    if moving_polls > 0 or wrong_settled_polls >= 2:
                        raise ZwoError(
                            f"EFWSetPosition({efw_id}, {slot}) settled at wrong slot {pos}",
                            EFWErrorCode.EFW_ERROR_ERROR_STATE,
                            "EFW_ERROR_ERROR_STATE",
                        )
                if time.monotonic() >= deadline:
                    raise ZwoTimeoutError(
                        f"EFWSetPosition({efw_id}, {slot}) timed out after {timeout:.2f}s "
                        f"(last pos={pos}, moving_polls={moving_polls}); physical outcome unknown"
                    )
                time.sleep(poll_interval)
        except BaseException as exc:
            self._record_op_error(exc)
            raise

    def calibrate(
        self,
        poll_interval: float = 0.05,
        timeout: float = 60.0,
    ) -> tuple[int, int]:
        """Run full hardware calibration (``EFWCalibrate``) and wait until settled."""
        validate_poll_and_timeout(poll_interval, timeout)
        efw_id = self._require_id()
        if self.state == DeviceState.BUSY:
            raise ZwoError("FilterWheel is busy", EFWErrorCode.EFW_ERROR_MOVING, "EFW_ERROR_MOVING")
        slot_num = self.info.slot_num if self.info is not None else 0
        self.state = DeviceState.BUSY
        try:
            deadline = time.monotonic() + timeout
            self.sdk.calibrate(efw_id)
            moving_polls = 0
            settled_polls = 0
            last_settled_pos: int | None = None
            while True:
                pos = self.sdk.get_position(efw_id)
                if pos == -1:
                    moving_polls += 1
                    settled_polls = 0
                    last_settled_pos = None
                elif pos < 0 or (slot_num > 0 and pos >= slot_num):
                    raise ZwoError(
                        f"EFWCalibrate({efw_id}) reported invalid settled slot {pos}",
                        EFWErrorCode.EFW_ERROR_ERROR_STATE,
                        "EFW_ERROR_ERROR_STATE",
                    )
                else:
                    if pos == last_settled_pos:
                        settled_polls += 1
                    else:
                        last_settled_pos = pos
                        settled_polls = 1
                    if settled_polls >= 2:
                        self.state = DeviceState.READY
                        return (pos, moving_polls)
                if time.monotonic() >= deadline:
                    raise ZwoTimeoutError(
                        f"EFWCalibrate({efw_id}) timed out after {timeout:.2f}s "
                        f"(last pos={pos}, moving_polls={moving_polls}); physical outcome unknown"
                    )
                time.sleep(poll_interval)
        except BaseException as exc:
            self._record_op_error(exc)
            raise


EFW = FilterWheel

__all__ = [
    "DeviceState",
    "EFW",
    "EFWErrorCode",
    "EfwInfo",
    "FilterWheel",
    "ZwoEfwSDK",
]
