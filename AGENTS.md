# Agent Instructions: `pyzwo`

`pyzwo` is a minimal, stateless, path-based Python interface to ZWO USB HID astronomy hardware using `hid` (`hidapi`).

## Architectural Boundaries
- **`pyzwo` scope:** Stateless path-based USB HID transport (`ZwoHid` in `pyzwo/hid.py`), device controllers (`Efw` and `EfwStatus` in `pyzwo/efw.py`), and `ZwoError`.
- **No proprietary vendor shared libraries:** Communicate directly via USB HID feature reports (`hid`). Do not add `pyobs`, `numpy`, or framework dependencies to `pyproject.toml`.
- **1-based EFW slots:** Slot indices are `1..slot_num` (matching both physical wheel markings and the HID wire protocol), with `-1` indicating the wheel is currently moving.

## Concurrency & Deadlines
- **Stateless per-command open/close:** Each `_send()` and `_query()` opens the HID device by path, performs the feature-report transaction, and closes it immediately.
- **Single-owner synchronous baseline:** Synchronous methods (`get_status`, `move_to_slot`) assume a single caller per physical wheel at a time. `move_to_slot` raises built-in `TimeoutError` if the wheel does not settle before `timeout`.

## Testing & Hardware Safety
- Default unit tests (`tests/test_efw.py`) mock `pyzwo.hid.hid` and must pass without physical hardware connected.
- Real-hardware tests (`tests/test_efw_hardware.py`) are opt-in via `PYZWO_RUN_HARDWARE_TESTS=1` (read-only) and `PYZWO_RUN_HARDWARE_MOTION_TESTS=1` (motion). Never execute motion scripts or tests against hardware without explicit operator consent.

## Git & Repository Hygiene
- Never commit proprietary vendor binaries (`.so`, `.dylib`, `.dll`), `__pycache__`, or `.pyc` files.
- Present a concise diff summary, test results, and proposed commit message for maintainer review before committing.
