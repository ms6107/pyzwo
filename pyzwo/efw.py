# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZWO EFW (Electronic Filter Wheel) SDK ctypes bindings and synchronous controller."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from enum import IntEnum
import time
from typing import Any

from ._loader import (
    DeviceState,
    ZwoError,
    ZwoTimeoutError,
    attach_rollback_error,
    load_zwo_library,
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
        rc = self._lib.EFWGetFirmwareVersion(
            efw_id, ctypes.byref(maj), ctypes.byref(min_), ctypes.byref(bld)
        )
        self._check(rc, f"EFWGetFirmwareVersion({efw_id})")
        return f"{maj.value}.{min_.value}.{bld.value}"


__all__ = [
    "DeviceState",
    "EFWErrorCode",
    "EfwInfo",
    "ZwoEfwSDK",
]
