# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""C11 compiler ABI probe and fake CDLL surface for verifying ctypes bindings without vendor binaries."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path
from typing import Any

INCLUDE_DIR = Path(__file__).resolve().parent.parent / "pyzwo" / "include"

BASE_CTYPE_TO_C: dict[Any, str] = {
    ctypes.c_int: "int",
    ctypes.c_bool: "_Bool",
    ctypes.c_ubyte: "unsigned char",
    ctypes.c_char: "char",
    ctypes.c_char_p: "char *",
    ctypes.c_float: "float",
    ctypes.c_double: "double",
}

SCALAR_CTYPE_TO_PY: dict[Any, type | tuple[type, ...]] = {
    ctypes.c_int: int,
    ctypes.c_ubyte: int,
    ctypes.c_bool: bool,
    ctypes.c_float: (int, float),
    ctypes.c_double: (int, float),
    ctypes.c_char_p: (bytes, type(None)),
}


class FakeCFunction:
    """Mock ctypes function symbol recording configured argtypes and restype."""

    def __init__(self, name: str, hook: Callable[..., Any] | None = None) -> None:
        self.name = name
        self.argtypes: list[Any] | None = None
        self.restype: Any = ctypes.c_int
        self._hook = hook
        self.impl: Callable[..., Any] | None = None

    def _unwrap_args(self, args: tuple[Any, ...]) -> tuple[Any, ...]:
        if self.argtypes is None:
            return tuple(getattr(a, "_obj", a) for a in args)
        if len(args) != len(self.argtypes):
            raise TypeError(f"{self.name} expected {len(self.argtypes)} arguments, got {len(args)}")
        unwrapped: list[Any] = []
        for idx, (arg, expected_type) in enumerate(zip(args, self.argtypes, strict=True)):
            if isinstance(expected_type, type) and issubclass(expected_type, ctypes._Pointer):
                if hasattr(arg, "_obj"):
                    pointee = arg._obj
                elif isinstance(arg, ctypes._Pointer):
                    pointee = arg.contents
                else:
                    raise TypeError(
                        f"{self.name} arg {idx}: expected ctypes.byref/pointer for {expected_type}, got {arg!r}"
                    )
                if not isinstance(pointee, expected_type._type_):
                    raise TypeError(
                        f"{self.name} arg {idx}: expected pointee of type {expected_type._type_}, got {pointee!r}"
                    )
                unwrapped.append(pointee)
            else:
                if hasattr(arg, "_obj") or isinstance(arg, ctypes._Pointer):
                    raise TypeError(
                        f"{self.name} arg {idx}: expected value for {expected_type}, got pointer/byref {arg!r}"
                    )
                if isinstance(expected_type, type) and issubclass(expected_type, ctypes.Structure):
                    if not isinstance(arg, expected_type):
                        raise TypeError(f"{self.name} arg {idx}: expected struct {expected_type.__name__}, got {arg!r}")
                elif expected_type in SCALAR_CTYPE_TO_PY:
                    py_type = SCALAR_CTYPE_TO_PY[expected_type]
                    if not isinstance(arg, py_type) or (expected_type is not ctypes.c_bool and isinstance(arg, bool)):
                        raise TypeError(f"{self.name} arg {idx}: expected {py_type} for {expected_type}, got {arg!r}")
                unwrapped.append(arg)
        return tuple(unwrapped)

    def __call__(self, *args: Any) -> Any:
        unwrapped = self._unwrap_args(args)
        if self.impl is not None:
            return self.impl(*unwrapped)
        if self._hook is not None:
            return self._hook(self.name, *unwrapped)
        if self.restype is ctypes.c_char_p:
            return b"fake"
        return 0


class FakeCDLL:
    """Fake ctypes.CDLL surface that exposes C symbols on attribute access without loading a native binary."""

    def __init__(self, hook: Callable[..., Any] | None = None) -> None:
        self.functions: dict[str, FakeCFunction] = {}
        self._hook = hook

    def __getattr__(self, name: str) -> FakeCFunction:
        if name.startswith("_"):
            raise AttributeError(name)
        func = self.functions.get(name)
        if func is None:
            func = FakeCFunction(name, hook=self._hook)
            self.functions[name] = func
        return func


def find_c_compiler() -> list[str]:
    """Return the `zig clang` command from `ziglang`, or raise RuntimeError if unavailable."""
    try:
        import ziglang  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("ziglang is required when PYZWO_RUN_ABI_TESTS=1 (run via `uv run --group abi ...`)") from exc
    zig_file = getattr(ziglang, "__file__", None)
    if not zig_file:
        raise RuntimeError("ziglang package has no __file__ path")
    zig_bin = Path(zig_file).resolve().parent / ("zig.exe" if os.name == "nt" else "zig")
    if not zig_bin.is_file() or not os.access(zig_bin, os.X_OK):
        raise RuntimeError(f"zig compiler binary not found or not executable at {zig_bin}")
    return [str(zig_bin), "clang"]


def ctype_to_c_decl(ct: Any, extra_c_types: dict[Any, str] | None = None) -> str:
    """Translate a ctypes type (scalar, Structure, POINTER) to its C type declaration string."""
    if extra_c_types and ct in extra_c_types:
        return extra_c_types[ct]
    if ct in BASE_CTYPE_TO_C:
        return BASE_CTYPE_TO_C[ct]
    if hasattr(ct, "_type_") and not hasattr(ct, "_length_"):
        return f"{ctype_to_c_decl(ct._type_, extra_c_types)} *"
    raise TypeError(f"Unsupported ctypes type for C ABI probe: {ct!r}")


def _field_ptr_c_type(field_ctype: Any, extra_c_types: dict[Any, str] | None = None) -> str:
    """Return the C pointer type `&(((Struct *)0)->field)` for C11 `_Generic` verification."""
    if hasattr(field_ctype, "_length_") and hasattr(field_ctype, "_type_"):
        elem_c = ctype_to_c_decl(field_ctype._type_, extra_c_types)
        return f"{elem_c} (*)[{field_ctype._length_}]"
    return f"{ctype_to_c_decl(field_ctype, extra_c_types)} *"


def struct_probe_c_lines(
    struct_cls: type[ctypes.Structure],
    c_name: str,
    extra_c_types: dict[Any, str] | None = None,
) -> list[str]:
    """Generate C11 `_Static_assert` and field-exhaustiveness checks for a `ctypes.Structure`."""
    lines = [
        f'_Static_assert(sizeof({c_name}) == {ctypes.sizeof(struct_cls)}, "{c_name} sizeof mismatch");',
        f'_Static_assert(_Alignof({c_name}) == {ctypes.alignment(struct_cls)}, "{c_name} alignof mismatch");',
    ]
    inits: list[str] = []
    for item in struct_cls._fields_:
        fname, ftype = item[0], item[1]
        descriptor = getattr(struct_cls, fname)
        ptr_type = _field_ptr_c_type(ftype, extra_c_types)
        lines.append(
            f"_Static_assert(__builtin_offsetof({c_name}, {fname}) == {descriptor.offset}, "
            f'"{c_name}.{fname} offset mismatch");'
        )
        lines.append(
            f'_Static_assert(sizeof((({c_name} *)0)->{fname}) == {descriptor.size}, "{c_name}.{fname} size mismatch");'
        )
        lines.append(
            f"_Static_assert(_Generic(&((({c_name} *)0)->{fname}), {ptr_type}: 1, default: 0), "
            f'"{c_name}.{fname} type mismatch");'
        )
        inits.append("{0}" if hasattr(ftype, "_length_") else "0")
    lines.append(f"static const {c_name} _exhaust_{c_name} = {{ {', '.join(inits)} }};")
    return lines


def enum_probe_c_lines(enum_cls: type[IntEnum], c_enum_name: str) -> list[str]:
    """Generate C11 `_Static_assert` value checks and an exhaustive `switch` check for an `IntEnum`."""
    lines = [
        f'_Static_assert(sizeof({c_enum_name}) == sizeof(int), "{c_enum_name} size must match c_int");',
    ]
    cases: list[str] = []
    for member in enum_cls:
        lines.append(f'_Static_assert((int){member.name} == {int(member.value)}, "{member.name} value mismatch");')
        cases.append(f"        case {member.name}:")
    lines.extend(
        [
            f"static void _check_{c_enum_name}_exhaustive({c_enum_name} code) {{",
            "    switch (code) {",
            *cases,
            "            break;",
            "    }",
            "}",
        ]
    )
    return lines


def signature_probe_c_lines(
    lib: FakeCDLL,
    extra_c_types: dict[Any, str] | None = None,
) -> list[str]:
    """Generate C11 `_Generic` function-pointer signature checks for all functions bound on `lib`."""
    lines: list[str] = []
    for name, c_func in lib.functions.items():
        ret_c = ctype_to_c_decl(c_func.restype, extra_c_types)
        args_c = ", ".join(ctype_to_c_decl(a, extra_c_types) for a in (c_func.argtypes or [])) or "void"
        fn_ptr_type = f"{ret_c} (*)({args_c})"
        lines.append(f'_Static_assert(_Generic(&{name}, {fn_ptr_type}: 1, default: 0), "{name} signature mismatch");')
    return lines


def compile_c_probe(c_source: str, header_path: Path) -> subprocess.CompletedProcess[str]:
    """Run C11 `-fsyntax-only` compilation on `c_source` against `header_path`."""
    cc = find_c_compiler()
    cmd = [
        *cc,
        "-std=c11",
        "-fsyntax-only",
        "-Dbool=_Bool",
        "-Wall",
        "-Wextra",
        "-Werror=switch",
        "-Werror=missing-field-initializers",
        "-Werror=excess-initializers",
        "-Wno-unused-const-variable",
        "-Wno-unused-function",
        f"-I{header_path.parent}",
        "-x",
        "c",
        "-",
    ]
    return subprocess.run(cmd, input=c_source, capture_output=True, text=True, check=False)


def ast_top_level_names(header_path: Path) -> set[str]:
    """Use Clang's JSON AST dump (`-Xclang -ast-dump=json`) to extract top-level function and typedef names."""
    cc = find_c_compiler()
    resolved_header = header_path.resolve()
    res = subprocess.run(
        [
            *cc,
            "-std=c11",
            "-fsyntax-only",
            "-Dbool=_Bool",
            "-Xclang",
            "-ast-dump=json",
            "-x",
            "c",
            str(resolved_header),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Clang AST dump failed ({res.returncode}):\n{res.stderr}")
    ast = json.loads(res.stdout)
    names: set[str] = set()
    current_file: Path | None = None
    for node in ast.get("inner", []):
        loc = node.get("loc", {})
        file_str = loc.get("file") or loc.get("expansionLoc", {}).get("file")
        if file_str is not None:
            current_file = Path(file_str).resolve()
        if node.get("isImplicit") or current_file != resolved_header:
            continue
        if node.get("kind") in ("FunctionDecl", "TypedefDecl") and node.get("name"):
            names.add(node["name"])
    return names


def _dump_macro_names(cc: list[str], source_args: list[str], stdin: str | None = None) -> set[str]:
    res = subprocess.run(
        [*cc, "-std=c11", "-Dbool=_Bool", "-dM", "-E", *source_args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Clang macro dump failed ({res.returncode}):\n{res.stderr}")
    names: set[str] = set()
    for line in res.stdout.splitlines():
        if line.startswith("#define "):
            token = line.removeprefix("#define ").split(None, 1)[0]
            names.add(token.split("(", 1)[0])
    return names


def header_macro_names(header_path: Path) -> set[str]:
    """Use Clang's `-dM -E` preprocessor dump to return macro names defined by `header_path`."""
    cc = find_c_compiler()
    builtin_macros = _dump_macro_names(cc, ["-x", "c", "-"], stdin="")
    all_macros = _dump_macro_names(cc, ["-x", "c", str(header_path.resolve())])
    return all_macros - builtin_macros
