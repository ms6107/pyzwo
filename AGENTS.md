# Agent Instructions: `pyzwohid`

`pyzwohid` is a minimal, path-based Python interface to ZWO USB HID astronomy hardware using `hid` (`hidapi`).

## Architectural Boundaries
- **`pyzwohid` scope:** Path-based USB HID transport (`ZwoHid` in `pyzwohid/hid.py`), USB HID device controllers (`Efw` and `EfwStatus` in `pyzwohid/efw.py`), and `ZwoError` / `ZwoDevicePathError` / `ZwoQueryCollisionError`. Non-HID ZWO devices (cameras, mounts) are out of scope.
- **No proprietary vendor shared libraries:** Communicate directly via USB HID feature reports (`hid`). Do not add `pyobs`, `numpy`, or framework dependencies to `pyproject.toml`.
- **1-based EFW slots:** Slot indices are `1..slot_count` (matching both physical wheel markings and the HID wire protocol), with `slot=-1` indicating the wheel is currently moving or calibrating (and `slot_count=-1` while Hall-sensor calibration is counting slots).

## Concurrency & Deadlines
- **Persistent handle & single-worker HID executor:** `await ZwoHid.open(path)` verifies `(VID, PID)` and opens the `hid.Device`, holding it open until `await close()` (or `async with` exit). All `hidapi` calls (`discover`, `open`, `close`, `_send`, `_query`) run on a single shared `ThreadPoolExecutor(max_workers=1)` so `hidapi` C calls never race on global state or open device handles.
- **Single-owner asynchronous baseline:** Asynchronous methods (`get_status`, `move_to_slot`, `calibrate`, `clear_error`) assume a single caller per physical wheel at a time. `move_to_slot` and `calibrate` poll with `await asyncio.sleep(0.2)` and raise built-in `TimeoutError` if the wheel does not settle before `timeout`.
- **Unit testing:** Unit tests (`tests/test_efw.py`, `tests/test_hid.py`) mock `sys.modules["hid"]` and must pass without physical hardware connected.
- Real-hardware tests (`tests/test_efw_hardware.py`) are opt-in via `PYZWOHID_RUN_HARDWARE_TESTS=1` (read-only) and `PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1` (motion). Never execute motion scripts or tests against hardware without explicit operator consent.

## Git & Repository Hygiene
- Never commit proprietary vendor binaries (`.so`, `.dylib`, `.dll`), `__pycache__`, or `.pyc` files.
- Present a concise diff summary, test results, and proposed commit message for maintainer review before committing.
