# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZWO EFW (Electronic Filter Wheel) SDK ctypes bindings and synchronous controller."""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from ._loader import (
    DeviceState,
    ZwoError,
    ZwoTimeoutError,
    attach_rollback_error,
    load_zwo_library,
    validate_poll_and_timeout,
)


class EFWErrorCode(IntEnum):
    """ZWO EFW SDK error codes from EFW_filter.h."""

    EFW_SUCCESS = 0
    EFW_ERROR_INVALID_INDEX = 1
    EFW_ERROR_INVALID_ID = 2
    EFW_ERROR_INVALID_VALUE = 3
    EFW_ERROR_REMOVED = 4
    EFW_ERROR_MOVING = 5
    EFW_ERROR_ERROR_STATE = 6
    EFW_ERROR_GENERAL_ERROR = 7
    EFW_ERROR_NOT_SUPPORTED = 8
    EFW_ERROR_CLOSED = 9
    EFW_ERROR_END = -1


class _EFWInfo(ctypes.Structure):
    """C struct EFW_INFO from EFW_filter.h."""

    _fields_ = [
        ("ID", ctypes.c_int),
        ("Name", ctypes.c_char * 64),
        ("slotNum", ctypes.c_int),
    ]


@dataclass(frozen=True)
class EfwInfo:
    """Snapshot of ZWO EFW device properties."""

    id: int
    name: str
    slot_num: int
    firmware_version: str
    unidirectional: bool


class ZwoEfwSDK:
    """Thin synchronous ctypes wrapper around ZWO's libEFWFilter shared library."""

    def __init__(self, lib_path: str | None = None, *, _lib: Any | None = None) -> None:
        self._lib = _lib if _lib is not None else load_zwo_library("EFWFilter", explicit_path=lib_path)

        self._lib.EFWGetSDKVersion.argtypes = []
        self._lib.EFWGetSDKVersion.restype = ctypes.c_char_p

        self._lib.EFWGetNum.argtypes = []
        self._lib.EFWGetNum.restype = ctypes.c_int

        self._lib.EFWGetID.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self._lib.EFWGetID.restype = ctypes.c_int

        self._lib.EFWOpen.argtypes = [ctypes.c_int]
        self._lib.EFWOpen.restype = ctypes.c_int

        self._lib.EFWClose.argtypes = [ctypes.c_int]
        self._lib.EFWClose.restype = ctypes.c_int

        self._lib.EFWGetProperty.argtypes = [ctypes.c_int, ctypes.POINTER(_EFWInfo)]
        self._lib.EFWGetProperty.restype = ctypes.c_int

        self._lib.EFWGetPosition.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self._lib.EFWGetPosition.restype = ctypes.c_int

        self._lib.EFWSetPosition.argtypes = [ctypes.c_int, ctypes.c_int]
        self._lib.EFWSetPosition.restype = ctypes.c_int

        self._lib.EFWGetDirection.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_bool)]
        self._lib.EFWGetDirection.restype = ctypes.c_int

        self._lib.EFWSetDirection.argtypes = [ctypes.c_int, ctypes.c_bool]
        self._lib.EFWSetDirection.restype = ctypes.c_int

        self._lib.EFWCalibrate.argtypes = [ctypes.c_int]
        self._lib.EFWCalibrate.restype = ctypes.c_int

        self._lib.EFWGetFirmwareVersion.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(ctypes.c_ubyte),
        ]
        self._lib.EFWGetFirmwareVersion.restype = ctypes.c_int

    @staticmethod
    def _check(rc: int, op: str) -> None:
        if rc != EFWErrorCode.EFW_SUCCESS:
            try:
                err_name = EFWErrorCode(rc).name
            except ValueError:
                err_name = f"UNKNOWN({rc})"
            raise ZwoError(op, rc, err_name)

    def get_sdk_version(self) -> str:
        raw = self._lib.EFWGetSDKVersion()
        return raw.decode("ascii", errors="replace") if raw else ""

    def get_num(self) -> int:
        return int(self._lib.EFWGetNum())

    def get_id(self, index: int) -> int:
        efw_id = ctypes.c_int()
        rc = self._lib.EFWGetID(index, ctypes.byref(efw_id))
        self._check(rc, f"EFWGetID({index})")
        return int(efw_id.value)

    def open(self, efw_id: int) -> None:
        rc = self._lib.EFWOpen(efw_id)
        self._check(rc, f"EFWOpen({efw_id})")

    def close(self, efw_id: int) -> None:
        rc = self._lib.EFWClose(efw_id)
        self._check(rc, f"EFWClose({efw_id})")

    def get_property(self, efw_id: int) -> tuple[int, str, int]:
        info = _EFWInfo()
        rc = self._lib.EFWGetProperty(efw_id, ctypes.byref(info))
        self._check(rc, f"EFWGetProperty({efw_id})")
        return int(info.ID), info.Name.decode("ascii", errors="replace"), int(info.slotNum)

    def get_position(self, efw_id: int) -> int:
        pos = ctypes.c_int()
        rc = self._lib.EFWGetPosition(efw_id, ctypes.byref(pos))
        if rc == EFWErrorCode.EFW_ERROR_MOVING:
            return -1
        self._check(rc, f"EFWGetPosition({efw_id})")
        return int(pos.value)

    def set_position(self, efw_id: int, slot: int) -> None:
        rc = self._lib.EFWSetPosition(efw_id, slot)
        self._check(rc, f"EFWSetPosition({efw_id}, {slot})")

    def get_direction(self, efw_id: int) -> bool:
        unidir = ctypes.c_bool()
        rc = self._lib.EFWGetDirection(efw_id, ctypes.byref(unidir))
        self._check(rc, f"EFWGetDirection({efw_id})")
        return bool(unidir.value)

    def set_direction(self, efw_id: int, unidirectional: bool) -> None:
        rc = self._lib.EFWSetDirection(efw_id, unidirectional)
        self._check(rc, f"EFWSetDirection({efw_id}, {unidirectional})")

    def calibrate(self, efw_id: int) -> None:
        rc = self._lib.EFWCalibrate(efw_id)
        self._check(rc, f"EFWCalibrate({efw_id})")

    def get_firmware_version(self, efw_id: int) -> str:
        maj, min_, bld = ctypes.c_ubyte(), ctypes.c_ubyte(), ctypes.c_ubyte()
        rc = self._lib.EFWGetFirmwareVersion(efw_id, ctypes.byref(maj), ctypes.byref(min_), ctypes.byref(bld))
        self._check(rc, f"EFWGetFirmwareVersion({efw_id})")
        return f"{maj.value}.{min_.value}.{bld.value}"


class FilterWheel:
    """Synchronous, framework-agnostic ZWO EFW controller.

    Usage contract, deadlines & recovery semantics:
    - Single-owner, single-thread only. Concurrent calls across threads or simultaneous
      direct ``ZwoEfwSDK`` and ``FilterWheel`` calls on the same device are unsupported.
    - ``open()`` is non-destructive by default; it only writes ``unidirectional`` if
      explicitly passed to the constructor. If any initialization step fails after
      ``EFWOpen(id)`` succeeds, ``open()`` rolls back via ``EFWClose(id)`` to prevent
      handle leaks (though hardware settings already written are not reverted).
    - ``close()`` is idempotent when disconnected and clears ``self.info``. If
      ``EFWClose`` fails, the handle remains retained and ``self.state`` becomes
      ``DeviceState.UNKNOWN`` so ``close()`` can be retried.
    - ``timeout`` parameters on ``open()``, ``move_to_slot()``, and ``calibrate()``
      enforce Python-level monotonic polling deadlines between SDK calls. A blocked
      native C call inside ``libEFWFilter`` has no pre-emptive timeout.
    - Failed or timed-out movements/calibrations and ``EFW_ERROR_REMOVED`` disconnects
      transition ``self.state`` to ``DeviceState.FAULT``. The controller only returns to
      ``DeviceState.READY`` after re-querying live hardware via ``reconcile()`` or
      ``close()`` + ``open()``.
    """

    def __init__(
        self,
        device_index: int = 0,
        unidirectional: bool | None = None,
        sdk: ZwoEfwSDK | None = None,
    ) -> None:
        self.device_index = device_index
        self._cfg_unidirectional = unidirectional
        self._sdk_instance: ZwoEfwSDK | None = sdk
        self.efw_id: int | None = None
        self.info: EfwInfo | None = None
        self.state: DeviceState = DeviceState.DISCONNECTED

    @property
    def sdk(self) -> ZwoEfwSDK:
        if self._sdk_instance is None:
            self._sdk_instance = ZwoEfwSDK()
        return self._sdk_instance

    @sdk.setter
    def sdk(self, value: ZwoEfwSDK) -> None:
        self._sdk_instance = value

    def open(self, timeout: float = 15.0, poll_interval: float = 0.1) -> EfwInfo:
        validate_poll_and_timeout(poll_interval, timeout)
        if self.state == DeviceState.UNKNOWN:
            raise ZwoError(
                f"EFWOpen(id={self.efw_id}): connection state is UNKNOWN; call close() first",
                EFWErrorCode.EFW_ERROR_ERROR_STATE,
                "EFW_ERROR_ERROR_STATE",
            )
        if self.efw_id is not None or self.state in (
            DeviceState.READY,
            DeviceState.BUSY,
            DeviceState.CONNECTING,
            DeviceState.FAULT,
        ):
            raise ZwoError(
                f"EFWOpen(id={self.efw_id}): already open",
                EFWErrorCode.EFW_ERROR_GENERAL_ERROR,
                "EFW_ERROR_GENERAL_ERROR",
            )
        if isinstance(self.device_index, bool) or not isinstance(self.device_index, int) or self.device_index < 0:
            raise ZwoError(
                f"EFWGetNum(index={self.device_index})",
                EFWErrorCode.EFW_ERROR_INVALID_INDEX,
                "EFW_ERROR_INVALID_INDEX",
            )
        num = self.sdk.get_num()
        if self.device_index >= num:
            raise ZwoError(
                f"EFWGetNum(index={self.device_index})",
                EFWErrorCode.EFW_ERROR_INVALID_INDEX,
                "EFW_ERROR_INVALID_INDEX",
            )

        efw_id = self.sdk.get_id(self.device_index)
        self.efw_id = efw_id
        self.info = None
        self.state = DeviceState.CONNECTING
        open_succeeded = False
        try:
            self.sdk.open(efw_id)
            open_succeeded = True

            if self._cfg_unidirectional is not None:
                self.sdk.set_direction(efw_id, self._cfg_unidirectional)
            unidir = self.sdk.get_direction(efw_id)

            deadline = time.monotonic() + timeout
            dev_name = ""
            slot_num = 0
            while True:
                try:
                    _, dev_name, slot_num = self.sdk.get_property(efw_id)
                    if slot_num > 0:
                        break
                    if time.monotonic() >= deadline:
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

            fw = self.sdk.get_firmware_version(efw_id)
            info = EfwInfo(
                id=efw_id,
                name=dev_name,
                slot_num=slot_num,
                firmware_version=fw,
                unidirectional=unidir,
            )
            self.info = info
            self.state = DeviceState.READY
            return info
        except BaseException as init_exc:
            self.info = None
            if open_succeeded:
                try:
                    self.sdk.close(efw_id)
                except BaseException as close_exc:
                    self.state = DeviceState.UNKNOWN
                    attach_rollback_error(init_exc, efw_id, close_exc)
                else:
                    self.efw_id = None
                    self.state = DeviceState.DISCONNECTED
            else:
                self.efw_id = None
                self.state = DeviceState.DISCONNECTED
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

    def reconcile(self) -> EfwInfo:
        """Re-query live physical hardware state to recover from FAULT state.

        Only transitions ``self.state`` back to ``DeviceState.READY`` if the wheel reports a
        valid ``slot_num > 0`` and a settled slot position ``0 <= pos < slot_num`` (not ``-1``).
        """
        if self.state == DeviceState.UNKNOWN:
            raise ZwoError(
                "FilterWheel connection state is UNKNOWN after failed close; call close() to recover",
                EFWErrorCode.EFW_ERROR_ERROR_STATE,
                "EFW_ERROR_ERROR_STATE",
            )
        if self.efw_id is None or self.state == DeviceState.DISCONNECTED:
            raise ZwoError("FilterWheel", EFWErrorCode.EFW_ERROR_CLOSED, "EFW_ERROR_CLOSED")

        efw_id = self.efw_id
        try:
            _, dev_name, slot_num = self.sdk.get_property(efw_id)
            if slot_num <= 0:
                self.state = DeviceState.FAULT
                raise ZwoError(
                    f"EFWGetProperty({efw_id}) reported slot_num={slot_num} during reconcile",
                    EFWErrorCode.EFW_ERROR_MOVING,
                    "EFW_ERROR_MOVING",
                )
            pos = self.sdk.get_position(efw_id)
            if pos == -1:
                self.state = DeviceState.FAULT
                raise ZwoError(
                    f"EFWGetPosition({efw_id}) reported wheel still moving (-1) during reconcile",
                    EFWErrorCode.EFW_ERROR_MOVING,
                    "EFW_ERROR_MOVING",
                )
            if pos < 0 or pos >= slot_num:
                self.state = DeviceState.FAULT
                raise ZwoError(
                    f"EFWGetPosition({efw_id}) reported invalid slot {pos} during reconcile",
                    EFWErrorCode.EFW_ERROR_ERROR_STATE,
                    "EFW_ERROR_ERROR_STATE",
                )
            unidir = self.sdk.get_direction(efw_id)
            fw = self.sdk.get_firmware_version(efw_id)
            info = EfwInfo(
                id=efw_id,
                name=dev_name,
                slot_num=slot_num,
                firmware_version=fw,
                unidirectional=unidir,
            )
            self.info = info
            self.state = DeviceState.READY
            return info
        except BaseException as exc:
            self._record_op_error(exc)
            raise

    def __enter__(self) -> FilterWheel:
        self.open()
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
                "FilterWheel is in FAULT state; reconcile() or close() and reopen required",
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
