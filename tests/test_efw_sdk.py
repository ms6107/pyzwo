# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""C-header ABI verification (opt-in via PYZWO_RUN_ABI_TESTS=1) and ZwoEfwSDK ctypes wrapper unit tests."""

from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from pyzwo import ZwoError
from pyzwo._efw_sdk import _EFWID, _EFWSN, EFW_ID_MAX, EFWErrorCode, ZwoEfwSDK, _EFWInfo
from tests.header_checker import (
    INCLUDE_DIR,
    FakeCDLL,
    ast_top_level_names,
    compile_c_probe,
    enum_probe_c_lines,
    find_c_compiler,
    header_macro_names,
    signature_probe_c_lines,
    struct_probe_c_lines,
)

EFW_HEADER = INCLUDE_DIR / "EFW_filter.h"
MOVING_POS = -1
ZWO_VID = 0x03C3
EFW_PID = 0x1F01

EXPECTED_HEADER_TYPES = {"EFW_INFO", "EFW_ERROR_CODE", "EFW_ID", "EFW_SN"}
EXPECTED_HEADER_MACROS = {"EFW_FILTER_H", "EFW_API", "EFW_ID_MAX"}
UNBOUND_DEPRECATED_FUNCS = {"EFWGetProductIDs"}
EFW_EXTRA_C_TYPES: dict[Any, str] = {
    _EFWInfo: "EFW_INFO",
    _EFWID: "EFW_ID",
}


@unittest.skipUnless(
    os.environ.get("PYZWO_RUN_ABI_TESTS") == "1",
    "Set PYZWO_RUN_ABI_TESTS=1 to run C11 compiler ABI verification against EFW_filter.h",
)
class TestEfwCtypesHeader(unittest.TestCase):
    """Verify EFW ctypes structs, enums, function signatures, macros, and top-level AST declarations via C11 probe."""

    def test_struct_layouts_match_header(self) -> None:
        self.assertIs(_EFWSN, _EFWID)
        lines = [
            '#include "EFW_filter.h"',
            *struct_probe_c_lines(_EFWInfo, "EFW_INFO", EFW_EXTRA_C_TYPES),
            *struct_probe_c_lines(_EFWID, "EFW_ID", EFW_EXTRA_C_TYPES),
            '_Static_assert(_Generic((EFW_SN *)0, EFW_ID *: 1, default: 0), "EFW_SN must alias EFW_ID");',
        ]
        res = compile_c_probe("\n".join(lines), EFW_HEADER)
        self.assertEqual(res.returncode, 0, f"Struct layout ABI check failed:\n{res.stderr}")

    def test_error_enum_matches_header(self) -> None:
        lines = [
            '#include "EFW_filter.h"',
            *enum_probe_c_lines(EFWErrorCode, "EFW_ERROR_CODE"),
        ]
        res = compile_c_probe("\n".join(lines), EFW_HEADER)
        self.assertEqual(res.returncode, 0, f"Enum ABI check failed:\n{res.stderr}")

    def test_ctypes_signatures_match_c_header(self) -> None:
        lib = FakeCDLL()
        ZwoEfwSDK(_lib=lib)
        lines = [
            '#include "EFW_filter.h"',
            *signature_probe_c_lines(lib, EFW_EXTRA_C_TYPES),
        ]
        res = compile_c_probe("\n".join(lines), EFW_HEADER)
        self.assertEqual(res.returncode, 0, f"Function signature ABI check failed:\n{res.stderr}")

    def test_no_unhandled_header_declarations(self) -> None:
        lib = FakeCDLL()
        ZwoEfwSDK(_lib=lib)
        actual_symbols = ast_top_level_names(EFW_HEADER)
        expected_symbols = EXPECTED_HEADER_TYPES | set(lib.functions) | UNBOUND_DEPRECATED_FUNCS
        self.assertEqual(actual_symbols, expected_symbols)
        self.assertEqual(header_macro_names(EFW_HEADER), EXPECTED_HEADER_MACROS)
        res = compile_c_probe(
            f'#include "EFW_filter.h"\n_Static_assert(EFW_ID_MAX == {EFW_ID_MAX}, "EFW_ID_MAX mismatch");\n',
            EFW_HEADER,
        )
        self.assertEqual(res.returncode, 0, f"EFW_ID_MAX ABI check failed:\n{res.stderr}")

    def test_header_mutations_are_detected(self) -> None:
        original_text = EFW_HEADER.read_text(encoding="utf-8")
        mutated_text = original_text.replace(
            "#endif",
            "#define EFW_EXTRA_MACRO 256\ntypedef int EFW_EXTRA_TYPE;\nEFW_API int EFWExtraFunc(int ID);\n#endif",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            mutated_header = Path(tmpdir) / "EFW_filter.h"
            mutated_header.write_text(mutated_text, encoding="utf-8")
            self.assertEqual(
                ast_top_level_names(mutated_header) - ast_top_level_names(EFW_HEADER),
                {"EFWExtraFunc", "EFW_EXTRA_TYPE"},
            )
            self.assertEqual(
                header_macro_names(mutated_header) - header_macro_names(EFW_HEADER),
                {"EFW_EXTRA_MACRO"},
            )

    def test_abi_probes_fail_on_mismatch_or_syntax_error(self) -> None:
        mismatch_res = compile_c_probe(
            '#include "EFW_filter.h"\n_Static_assert(EFW_ID_MAX == 256, "EFW_ID_MAX mismatch");\n',
            EFW_HEADER,
        )
        self.assertNotEqual(mismatch_res.returncode, 0)
        syntax_res = compile_c_probe('#include "EFW_filter.h"\nint broken_c_syntax(\n', EFW_HEADER)
        self.assertNotEqual(syntax_res.returncode, 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            broken_header = Path(tmpdir) / "EFW_filter.h"
            broken_header.write_text("typedef struct { int unclosed;\n#if 1\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ast_top_level_names(broken_header)
            with self.assertRaises(RuntimeError):
                header_macro_names(broken_header)


class TestZwoEfwSDK(unittest.TestCase):
    """Unit tests for ZwoEfwSDK ctypes wrapper behavior using FakeCDLL."""

    def test_find_c_compiler_fails_closed_when_compiler_unavailable(self) -> None:
        cases = [
            ("missing_package", None),
            ("missing_binary", SimpleNamespace(__file__="/nonexistent/ziglang/__init__.py")),
        ]
        for label, fake_mod in cases:
            with (
                self.subTest(case=label),
                patch.dict(sys.modules, {"ziglang": fake_mod}),
                self.assertRaises(RuntimeError),
            ):
                find_c_compiler()

    def test_get_sdk_version_decodes_ascii_and_handles_empty(self) -> None:
        for raw_bytes, expected in [(b"1, 8, 4", "1, 8, 4"), (b"", "")]:
            with self.subTest(raw=raw_bytes):
                lib = FakeCDLL()
                lib.EFWGetSDKVersion.impl = lambda raw=raw_bytes: raw
                sdk = ZwoEfwSDK(_lib=lib)
                self.assertEqual(sdk.get_sdk_version(), expected)

    def test_check_forwards_vid_pid_and_converts_c_int_to_bool(self) -> None:
        for c_ret, expected in [(1, True), (0, False)]:
            with self.subTest(c_ret=c_ret):
                received: list[tuple[int, int]] = []
                lib = FakeCDLL()

                def fake_check(vid: int, pid: int, rc: int = c_ret, log: list[tuple[int, int]] = received) -> int:
                    log.append((vid, pid))
                    return rc

                lib.EFWCheck.impl = fake_check
                sdk = ZwoEfwSDK(_lib=lib)
                self.assertIs(sdk.check(ZWO_VID, EFW_PID), expected)
                self.assertEqual(received, [(ZWO_VID, EFW_PID)])

    def test_get_num_returns_connected_wheel_count(self) -> None:
        expected_count = 2
        lib = FakeCDLL()
        lib.EFWGetNum.impl = lambda: expected_count
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_num(), expected_count)

    def test_get_id_unmarshals_out_pointer(self) -> None:
        device_index = 1
        expected_id = 3
        received_indices: list[int] = []
        lib = FakeCDLL()

        def fake_get_id(index: int, out_id: ctypes.c_int) -> int:
            received_indices.append(index)
            out_id.value = expected_id
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWGetID.impl = fake_get_id
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_id(device_index), expected_id)
        self.assertEqual(received_indices, [device_index])

    def test_single_id_commands_forward_handle_id(self) -> None:
        efw_id = 4
        cases: list[tuple[str, Callable[[ZwoEfwSDK], Any]]] = [
            ("EFWOpen", lambda s: s.open(efw_id)),
            ("EFWClose", lambda s: s.close(efw_id)),
            ("EFWCalibrate", lambda s: s.calibrate(efw_id)),
        ]
        for c_name, invoke in cases:
            with self.subTest(func=c_name):
                received_ids: list[int] = []
                lib = FakeCDLL()

                def fake_cmd(dev_id: int, log: list[int] = received_ids) -> int:
                    log.append(dev_id)
                    return int(EFWErrorCode.EFW_SUCCESS)

                getattr(lib, c_name).impl = fake_cmd
                sdk = ZwoEfwSDK(_lib=lib)
                invoke(sdk)
                self.assertEqual(received_ids, [efw_id])

    def test_set_position_forwards_id_and_target_slot(self) -> None:
        efw_id = 2
        target_slot = 5
        received: list[tuple[int, int]] = []
        lib = FakeCDLL()

        def fake_set_pos(dev_id: int, slot: int) -> int:
            received.append((dev_id, slot))
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWSetPosition.impl = fake_set_pos
        sdk = ZwoEfwSDK(_lib=lib)
        sdk.set_position(efw_id, target_slot)
        self.assertEqual(received, [(efw_id, target_slot)])

    def test_set_direction_forwards_id_and_unidirectional_flag(self) -> None:
        efw_id = 1
        for expected_flag in (True, False):
            with self.subTest(unidirectional=expected_flag):
                received: list[tuple[int, bool]] = []
                lib = FakeCDLL()

                def fake_set_dir(
                    dev_id: int,
                    unidir: bool,
                    log: list[tuple[int, bool]] = received,
                ) -> int:
                    log.append((dev_id, unidir))
                    return int(EFWErrorCode.EFW_SUCCESS)

                lib.EFWSetDirection.impl = fake_set_dir
                sdk = ZwoEfwSDK(_lib=lib)
                sdk.set_direction(efw_id, expected_flag)
                self.assertEqual(received, [(efw_id, expected_flag)])

    def test_commands_raise_zwo_error_on_nonzero_return(self) -> None:
        expected_code = EFWErrorCode.EFW_ERROR_INVALID_ID
        cases: list[tuple[str, Callable[[ZwoEfwSDK], Any]]] = [
            ("EFWOpen", lambda s: s.open(0)),
            ("EFWClose", lambda s: s.close(0)),
            ("EFWSetPosition", lambda s: s.set_position(0, 1)),
            ("EFWSetDirection", lambda s: s.set_direction(0, True)),
            ("EFWCalibrate", lambda s: s.calibrate(0)),
            ("EFWSetID", lambda s: s.set_id(0, b"12345678")),
            ("EFWGetHWErrorCode", lambda s: s.get_hw_error_code(0)),
            ("EFWGetSerialNumber", lambda s: s.get_serial_number(0)),
        ]
        for c_name, invoke in cases:
            with self.subTest(func=c_name):
                lib = FakeCDLL()
                getattr(lib, c_name).impl = lambda *_args: int(expected_code)
                sdk = ZwoEfwSDK(_lib=lib)
                with self.assertRaises(ZwoError) as ctx:
                    invoke(sdk)
                self.assertEqual(ctx.exception.code, expected_code)
                self.assertEqual(ctx.exception.code_name, expected_code.name)

    def test_get_property_unmarshals_efw_info_struct(self) -> None:
        expected_id = 2
        expected_name = "ZWO EFW"
        expected_slots = 7
        lib = FakeCDLL()

        def fake_get_property(efw_id: int, out_info: _EFWInfo) -> int:
            out_info.ID = efw_id
            out_info.Name = expected_name.encode("ascii")
            out_info.slotNum = expected_slots
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWGetProperty.impl = fake_get_property
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_property(expected_id), (expected_id, expected_name, expected_slots))

    def test_get_position_returns_settled_slot_on_success(self) -> None:
        efw_id = 3
        settled_slot = 4
        received_ids: list[int] = []
        lib = FakeCDLL()

        def fake_get_position(dev_id: int, out_pos: ctypes.c_int) -> int:
            received_ids.append(dev_id)
            out_pos.value = settled_slot
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWGetPosition.impl = fake_get_position
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_position(efw_id), settled_slot)
        self.assertEqual(received_ids, [efw_id])

    def test_get_position_translates_moving_code_to_minus_one_sentinel(self) -> None:
        lib = FakeCDLL()
        lib.EFWGetPosition.impl = lambda _id, _ptr: int(EFWErrorCode.EFW_ERROR_MOVING)
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_position(0), MOVING_POS)

    def test_get_direction_unmarshals_bool_out_pointer(self) -> None:
        efw_id = 3
        for expected in (True, False):
            with self.subTest(unidirectional=expected):
                received_ids: list[int] = []
                lib = FakeCDLL()

                def fake_get_direction(
                    dev_id: int,
                    out_unidir: ctypes.c_bool,
                    val: bool = expected,
                    ids: list[int] = received_ids,
                ) -> int:
                    ids.append(dev_id)
                    out_unidir.value = val
                    return int(EFWErrorCode.EFW_SUCCESS)

                lib.EFWGetDirection.impl = fake_get_direction
                sdk = ZwoEfwSDK(_lib=lib)
                self.assertIs(sdk.get_direction(efw_id), expected)
                self.assertEqual(received_ids, [efw_id])

    def test_get_firmware_version_formats_major_minor_build(self) -> None:
        efw_id = 3
        received_ids: list[int] = []
        lib = FakeCDLL()

        def fake_get_fw(
            dev_id: int,
            maj: ctypes.c_ubyte,
            min_: ctypes.c_ubyte,
            bld: ctypes.c_ubyte,
        ) -> int:
            received_ids.append(dev_id)
            maj.value = 3
            min_.value = 5
            bld.value = 2
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWGetFirmwareVersion.impl = fake_get_fw
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_firmware_version(efw_id), "3.5.2")
        self.assertEqual(received_ids, [efw_id])

    def test_get_hw_error_code_unmarshals_out_pointer(self) -> None:
        efw_id = 3
        expected_hw_err = 42
        received_ids: list[int] = []
        lib = FakeCDLL()

        def fake_get_hw_err(dev_id: int, out_err: ctypes.c_int) -> int:
            received_ids.append(dev_id)
            out_err.value = expected_hw_err
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWGetHWErrorCode.impl = fake_get_hw_err
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_hw_error_code(efw_id), expected_hw_err)
        self.assertEqual(received_ids, [efw_id])

    def test_get_serial_number_unmarshals_8_byte_struct(self) -> None:
        efw_id = 3
        expected_sn = b"\x01\x02\x03\x04\x05\x06\x07\x08"
        received_ids: list[int] = []
        lib = FakeCDLL()

        def fake_get_sn(dev_id: int, out_sn: _EFWSN) -> int:
            received_ids.append(dev_id)
            for idx, byte in enumerate(expected_sn):
                out_sn.id[idx] = byte
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWGetSerialNumber.impl = fake_get_sn
        sdk = ZwoEfwSDK(_lib=lib)
        self.assertEqual(sdk.get_serial_number(efw_id), expected_sn)
        self.assertEqual(received_ids, [efw_id])

    def test_set_id_passes_8_byte_struct_by_value(self) -> None:
        efw_id = 3
        alias = b"FILTER01"
        received: list[tuple[int, bytes]] = []
        lib = FakeCDLL()

        def fake_set_id(dev_id: int, in_id: _EFWID) -> int:
            received.append((dev_id, bytes(in_id.id)))
            return int(EFWErrorCode.EFW_SUCCESS)

        lib.EFWSetID.impl = fake_set_id
        sdk = ZwoEfwSDK(_lib=lib)
        sdk.set_id(efw_id, alias)
        self.assertEqual(received, [(efw_id, alias)])

    def test_set_id_rejects_alias_not_8_bytes(self) -> None:
        for bad_alias in (b"", b"short", b"123456789"):
            with self.subTest(alias=bad_alias):
                called = False
                lib = FakeCDLL()

                def fake_set_id(_id: int, _in_id: _EFWID) -> int:
                    nonlocal called
                    called = True
                    return int(EFWErrorCode.EFW_SUCCESS)

                lib.EFWSetID.impl = fake_set_id
                sdk = ZwoEfwSDK(_lib=lib)
                with self.assertRaises(ValueError):
                    sdk.set_id(0, bad_alias)
                self.assertFalse(called)

    def test_nonzero_error_codes_raise_zwo_error_with_enum_name(self) -> None:
        cases = [
            EFWErrorCode.EFW_ERROR_REMOVED,
            EFWErrorCode.EFW_ERROR_UNKNOWN,
            EFWErrorCode.EFW_ERROR_CLOSED,
        ]
        for expected_code in cases:
            with self.subTest(code=expected_code):
                lib = FakeCDLL()
                lib.EFWGetPosition.impl = lambda _id, _ptr, rc=int(expected_code): rc
                sdk = ZwoEfwSDK(_lib=lib)
                with self.assertRaises(ZwoError) as ctx:
                    sdk.get_position(0)
                self.assertEqual(ctx.exception.code, expected_code)
                self.assertEqual(ctx.exception.code_name, expected_code.name)

    def test_unmapped_nonzero_error_code_uses_unknown_fallback_name(self) -> None:
        unmapped_code = 99
        lib = FakeCDLL()
        lib.EFWGetPosition.impl = lambda _id, _ptr: unmapped_code
        sdk = ZwoEfwSDK(_lib=lib)
        with self.assertRaises(ZwoError) as ctx:
            sdk.get_position(0)
        self.assertEqual(ctx.exception.code, unmapped_code)
        self.assertEqual(ctx.exception.code_name, f"UNKNOWN({unmapped_code})")


if __name__ == "__main__":
    unittest.main()
