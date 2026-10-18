# Copyright (c) 2026 Maksim Semenov
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Reusable C-header parser and fake CDLL surface for verifying ctypes signatures without native binaries."""

from __future__ import annotations

import ctypes
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

INCLUDE_DIR = Path(__file__).resolve().parent.parent / "pyzwo" / "include"

BASE_C_TYPE_MAP: dict[str, Any] = {
    "int": ctypes.c_int,
    "int*": ctypes.POINTER(ctypes.c_int),
    "bool": ctypes.c_bool,
    "bool*": ctypes.POINTER(ctypes.c_bool),
    "float*": ctypes.POINTER(ctypes.c_float),
    "char*": ctypes.c_char_p,
    "unsigned char*": ctypes.POINTER(ctypes.c_ubyte),
}


class FakeCFunction:
    """Mock ctypes function symbol recording configured argtypes and restype."""

    def __init__(self, name: str, hook: Callable[..., Any] | None = None) -> None:
        self.name = name
        self.argtypes: list[Any] | None = None
        self.restype: Any = ctypes.c_int
        self._hook = hook
        self._impl: Callable[..., Any] | None = None

    def __call__(self, *args: Any) -> Any:
        if self._impl is not None:
            return self._impl(*args)
        if self._hook is not None:
            return self._hook(self.name, *args)
        if self.restype is ctypes.c_char_p:
            return b"fake"
        return 0


class FakeCDLL:
    """Fake ctypes.CDLL surface that exposes C symbols on attribute access without loading a native binary."""

    def __init__(self, hook: Callable[..., Any] | None = None) -> None:
        self._funcs: dict[str, FakeCFunction] = {}
        self._hook = hook

    def __getattr__(self, name: str) -> FakeCFunction:
        if name.startswith("_"):
            raise AttributeError(name)
        func = self._funcs.get(name)
        if func is None:
            func = FakeCFunction(name, hook=self._hook)
            self._funcs[name] = func
        return func


def normalize_c_param_type(raw_param: str) -> str:
    """Normalize a C parameter declaration (e.g. 'unsigned char *major') to 'unsigned char*'."""
    s = re.sub(r"/\*.*?\*/", "", raw_param).strip()
    if not s or s == "void":
        return ""
    is_ptr = "*" in s
    s = s.replace("*", " ")
    tokens = s.split()
    if len(tokens) >= 2 and tokens[-1] not in ("char", "int", "short", "long"):
        type_tokens = tokens[:-1]
    else:
        type_tokens = tokens
    base = " ".join(type_tokens)
    return f"{base}*" if is_ptr else base


def parse_header_prototypes(
    header_path: Path,
    api_macro: str,
    extra_types: dict[str, Any] | None = None,
) -> dict[str, tuple[Any, list[Any]]]:
    """Parse `<API_MACRO> <ret> <func_name>(<params>);` declarations from a ZWO C header."""
    type_map = dict(BASE_C_TYPE_MAP)
    if extra_types:
        type_map.update(extra_types)

    text = header_path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        rf"^\s*{api_macro}\s+([A-Za-z0-9_]+\s*\*?)\s*([A-Za-z0-9_]+)\s*\(([^)]*)\)\s*;",
        re.MULTILINE,
    )
    prototypes: dict[str, tuple[Any, list[Any]]] = {}
    for match in pattern.finditer(text):
        ret_raw, func_name, params_raw = match.groups()
        ret_norm = ret_raw.replace(" ", "") if "*" in ret_raw else ret_raw.strip()
        if ret_norm not in type_map:
            continue
        arg_types: list[Any] = []
        norm_ok = True
        for p in params_raw.split(","):
            p = p.strip()
            if not p:
                continue
            c_type = normalize_c_param_type(p)
            if c_type not in type_map:
                norm_ok = False
                break
            arg_types.append(type_map[c_type])
        if norm_ok:
            prototypes[func_name] = (type_map[ret_norm], arg_types)
    return prototypes


def parse_header_enum(header_path: Path, enum_name: str) -> dict[str, int]:
    """Parse `typedef enum <enum_name> { ... }` from a C header into a `{name: int_value}` dict."""
    text = header_path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    pattern = re.compile(rf"typedef\s+enum\s+{re.escape(enum_name)}\s*\{{([^}}]+)\}}", re.DOTALL)
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"Enum {enum_name} not found in {header_path}")
    body = match.group(1)
    values: dict[str, int] = {}
    next_val = 0
    for raw_entry in body.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" in entry:
            name_part, val_part = entry.split("=", 1)
            name = name_part.strip()
            val = int(val_part.strip(), 0)
        else:
            name = entry
            val = next_val
        values[name] = val
        next_val = val + 1
    return values
