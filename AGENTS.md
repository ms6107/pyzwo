# Agent Instructions: `pyzwo`

`pyzwo` is a minimal, path-based Python interface to ZWO USB HID astronomy hardware using `hid` (`hidapi`).

## Architectural Boundaries
- **`pyzwo` scope:** Path-based USB HID transport (`ZwoHid` in `pyzwo/hid.py`), device controllers (`Efw` and `EfwStatus` in `pyzwo/efw.py`), and `ZwoError` / `ZwoQueryCollisionError`.
- **No proprietary vendor shared libraries:** Communicate directly via USB HID feature reports (`hid`). Do not add `pyobs`, `numpy`, or framework dependencies to `pyproject.toml`.
- **1-based EFW slots:** Slot indices are `1..slot_count` (matching both physical wheel markings and the HID wire protocol), with `slot=-1` indicating the wheel is currently moving or calibrating (and `slot_count=-1` while Hall-sensor calibration is counting slots).

## Concurrency & Deadlines
- **Persistent handle & serialized lifecycle:** `ZwoHid(path)` opens the `hid.Device` on construction and holds it open until `close()` (or context-manager exit), serializing `discover()`, `__init__()`, and `close()` with a module-level lock per `hidapi` thread-safety rules.
- **Single-owner synchronous baseline:** Synchronous methods (`get_status`, `move_to_slot`, `calibrate`, `clear_error`) assume a single caller per physical wheel at a time. `move_to_slot` and `calibrate` raise built-in `TimeoutError` if the wheel does not settle before `timeout`.

## Testing & Hardware Safety
- Default unit tests (`tests/test_efw.py`) mock `sys.modules["hid"]` and must pass without physical hardware connected.
- Real-hardware tests (`tests/test_efw_hardware.py`) are opt-in via `PYZWO_RUN_HARDWARE_TESTS=1` (read-only) and `PYZWO_RUN_HARDWARE_MOTION_TESTS=1` (motion). Never execute motion scripts or tests against hardware without explicit operator consent.

## Git & Repository Hygiene
- Never commit proprietary vendor binaries (`.so`, `.dylib`, `.dll`), `__pycache__`, or `.pyc` files.
- Present a concise diff summary, test results, and proposed commit message for maintainer review before committing.
