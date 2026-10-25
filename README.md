# pyzwo

`pyzwo` is an independent, pure-Python interface to the official ZWO SDKs (currently `libEFWFilter`, with additional ZWO device classes planned incrementally). It provides framework-agnostic device access, lifecycle management, and error reporting. Integrations with observatory-control frameworks are maintained separately.

> **Status:** Experimental synchronous ZWO EFW (Electronic Filter Wheel) baseline.

## Philosophy

I'm building `pyzwo` primarily for my own observatory, and I test it extensively against the hardware I actually own. At the same time, it is deliberately written **without hardcoded assumptions** tied to my specific rig—no baked-in slot counts or silent NVRAM overwrites on connect.

Every implemented module uses ZWO's official USB SDK for that device family rather than model-specific constants. Where the lists below mention specific models or architectures, that reflects what has been verified on physical hardware so far versus what shares the same USB SDK calls and remains untested on hardware.

It started for my own setup, but it's MPL-2.0-licensed and free to grab for mainstream use.

## How it works

ZWO distributes precompiled C shared libraries (`.so` / `.dylib` / `.dll`) and flat C headers (`.h`). Rather than compiling a C++ wrapper extension per Python version and CPU architecture, `pyzwo` binds the C ABI directly using standard-library `ctypes` in `pyzwo/_efw_sdk.py` and exposes the high-level controller (`FilterWheel` / `EFW`) in `pyzwo/efw.py`.

To verify `ctypes` struct layouts (`_EFWInfo`, `_EFWID`), `EFWErrorCode` enum values, function signatures, top-level AST declarations (functions and typedefs), and header macros (`EFW_ID_MAX`) against the bundled `pyzwo/include/EFW_filter.h` without requiring proprietary vendor binaries, `tests/header_checker.py` uses a C11 compiler ABI probe (`-std=c11 -fsyntax-only` with `_Static_assert` and `_Generic`). Because C ABI checks only need to run when SDK bindings or vendor headers change, the compiler probe (`TestEfwCtypesHeader`) is opt-in via `PYZWO_RUN_ABI_TESTS=1`. (Note: the bundled `EFW_filter.h` adds the missing `EFW_ERROR_UNKNOWN = 10` entry omitted in upstream SDK `1.8.4` so that `EFW_ERROR_CLOSED = 11` matches the actual runtime return code when operating on a closed handle.)

Key lifecycle and hardware behaviors in the synchronous EFW controller (`FilterWheel` / `EFW`):
- **Explicit discovery & zero-based slots:** `open()` validates `0 <= device_index < get_num()` before querying `EFWGetID(device_index)`. Slot positions follow the ZWO SDK's zero-based convention (`0 .. slot_num - 1`), with `-1` indicating the wheel is currently in motion (`EFW_ERROR_MOVING`).
- **Non-destructive `FilterWheel.open()` factory with exception-safe rollback:** `FilterWheel.open()` returns a healthy `FilterWheel` in `DeviceState.READY` or raises an exception. Opening a wheel only reads its current properties (`EFWGetProperty`, `EFWGetDirection`, `EFWGetFirmwareVersion`) unless `unidirectional` is explicitly passed to `FilterWheel.open(...)`. If any initialization step fails after `EFWOpen(id)` succeeds, `open()` automatically calls `EFWClose(id)` before re-raising; if rollback `EFWClose(id)` also fails, both errors are reported.
- **Hardware quirk guards:**
  - Commanding `EFWSetPosition` to the slot the wheel is already on returns `EFW_ERROR_GENERAL_ERROR (rc=7)` on real hardware; `move_to_slot()` checks the current slot first and returns `(slot, 0)` without issuing a redundant move (or rejects with `EFW_ERROR_MOVING` if the wheel is already moving).
  - On Linux, `libEFWFilter.so` references `udev_*` symbols without always declaring `DT_NEEDED` for `libudev.so.1`; `load_zwo_library()` preloads `libudev.so.1` with `RTLD_GLOBAL`.

## Concurrency, Deadlines & Limitations

- **Single-owner, single-thread contract:** `FilterWheel` (`EFW`) and `ZwoEfwSDK` are synchronous and designed for a single owner on a single thread. This baseline makes **no promise of thread safety** across concurrent threads and does **not** support simultaneous raw (`ZwoEfwSDK`) and managed (`FilterWheel`) access to the same device or vendor library. Do not rely on the Python GIL to make concurrent native C calls safe. Separate OS processes accessing the same USB device cannot be coordinated in-process.
- **Polling deadlines vs. native C calls:** The `timeout` argument on `move_to_slot()` and `calibrate()` is a **Python-level monotonic polling deadline** checked between SDK calls. A synchronous native C call inside `libEFWFilter` has **no pre-emptive timeout**—if the vendor C function blocks in the kernel or USB stack, the calling Python thread will remain blocked until that C call returns.
- **Honest fault & recovery states (`DeviceState`):** On `EFW_ERROR_REMOVED` (USB disconnect), `FilterWheel.state` transitions to `DeviceState.FAULT` and subsequent commands fail fast until `close()` + `open()`. If `close()` itself fails, the handle remains retained and `state` transitions to `DeviceState.UNKNOWN` so `close()` can be retried.

## Device classes & roadmap

- **EFW (Filter Wheels, SDK `v1.8.4` / `libEFWFilter.so.1.8.4`)** — **Implemented (synchronous baseline).**
  - [x] **ZWO EFW 7×2" II (USB Type-C revision, fw `3.5.2`, `03c3:1f01`)** — Verified on hardware (read-only + `test_hardware_move_next_slot_and_back`, pyzwo 0.1.0, SDK 1.8.4, openSUSE Tumbleweed x86_64).
  - [x] **ZWO EFW 7×2" (legacy revision, fw `3.0.6`, `03c3:1f01`)** — Verified on hardware (read-only + `test_hardware_move_next_slot_and_back`, pyzwo 0.1.0, SDK 1.8.4, openSUSE Tumbleweed x86_64).
  - [ ] Other slot variants (`5×2"`, `7×36mm`, `8×1.25"`, `Mini`) — Untested on hardware (uses the same `libEFWFilter` SDK calls and dynamic `slotNum` discovery).
- **Asynchronous EFW execution** — **Planned next (Phase 2).**
- **EAF (Focusers, SDK `v1.7.7` / `libEAFFocuser.so.1.7.7`)** — **Planned next (Phases 3–4).**
- **ASI Cameras (`libASICamera2`) & AM Mounts** — **Planned.**

## ZWO SDK versions used

| Device Class | Bundled Header | Shared Library | `GetSDKVersion()` |
|---|---|---|---|
| **EFW** | `pyzwo/include/EFW_filter.h` | `libEFWFilter.so.1.8.4` | `"1, 8, 4"` (`1.8.4`) |

## Platform & architecture status

The package is pure Python (`dependencies = []`, `requires-python = ">=3.11"`) and resolves ZWO's shared library in this order: an explicit `ZwoEfwSDK(lib_path=...)`, the `PYZWO_EFWFILTER_LIB` environment variable, `/usr/local/lib`, `/usr/lib`, `pyzwo/lib/<arch>/`, then `ctypes.util.find_library`.

- [x] **Linux `x86_64` (`x64`)** — Verified on real hardware (`openSUSE Tumbleweed 20261008`, kernel `7.2.9-1-default x86_64`).
- [ ] **Linux `aarch64` (`armv8`) / `armv7` / `armv6` / `x86`** — Untested on hardware.
- [ ] **macOS (`mac`) / Windows (`x64` / `x86`)** — Untested on hardware.

## Linux device permissions (`udev`)

`pyzwo` never auto-installs system rules and never requires running the Python process as `root`.

`libEFWFilter.so.1.8.4` uses `hidapi-hidraw` on Linux and accesses only `/dev/hidraw*` nodes. Unlike the upstream ZWO SDK ruleset, `pyzwo/udev/70-pyzwo.rules` does **not** grant world-writable (`0666`) access to USB buses, `/sys/bus/usb/drivers/usb/bind` or `unbind`, `drivers_autoprobe`, hub `power/level` attributes, or `usbcore.usbfs_memory_mb`. Instead, it scopes access strictly to ZWO USB HID nodes (`SUBSYSTEM=="hidraw"`, `ATTRS{idVendor}=="03c3"`):

- **Attended desktop Linux sessions:** `TAG+="uaccess"` lets `systemd-logind` dynamically grant an ACL on the matching `/dev/hidraw*` node to the active local seat user (ordered at `70-` so it runs before `73-seat-late.rules`).
- **Headless observatory services / daemons / SSH sessions:** Desktop seat ACLs (`uaccess`) do not apply when no local seat session is active. The rule assigns `GROUP="zwo", MODE="0660"` so only `root` and members of the `zwo` group can open the device node.

To configure non-root access on a Linux host:

```bash
sudo groupadd --system zwo
sudo usermod -aG zwo "$USER"   # or your headless service account (e.g. pyobs)
sudo install -m 0644 pyzwo/udev/70-pyzwo.rules /etc/udev/rules.d/70-pyzwo.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
```

After adding a user to the `zwo` group, log out and log back in (or restart the systemd service with `SupplementaryGroups=zwo`) so the new group membership takes effect, or replug the USB device if `udevadm trigger` was not run.

## Usage

```python
from pyzwo import EFW

with EFW.open(device_index=0) as wheel:
    print(wheel.info)  # EfwInfo(id=0, name='EFW', slot_num=7, firmware_version='3.5.2', unidirectional=False)
    print(wheel.get_slot())  # 0-based SDK slot (0 .. slot_num - 1), or -1 if moving
    settled_slot, polls = wheel.move_to_slot(2, timeout=30.0)
    print(f"Settled at slot {settled_slot} after {polls} moving polls")
```

## Running tests

### Default unit suite (no vendor binaries, C compiler, or hardware required)

The default test suite uses in-memory fakes and `FakeCDLL` and runs in CI without `libEFWFilter.so` or a C compiler installed. Pointing `PYZWO_EFWFILTER_LIB` at a non-existent path guarantees that hardware tests are skipped even on a machine where the vendor library and a wheel are present:

```bash
PYZWO_EFWFILTER_LIB=/nonexistent/libEFWFilter.so python3 -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
uvx mypy pyzwo tests
```

### Opt-in C11 compiler ABI verification (when `pyzwo/_efw_sdk.py` or C headers change)

To verify `ctypes` struct layouts, enum values, function signatures, top-level AST declarations (functions and typedefs), and header macro definitions (`EFW_ID_MAX`) against `pyzwo/include/EFW_filter.h` using `ziglang` (`zig clang` from `[dependency-groups].abi`):

```bash
PYZWO_RUN_ABI_TESTS=1 PYZWO_EFWFILTER_LIB=/nonexistent/libEFWFilter.so uv run --group abi python -m unittest tests.test_efw_sdk -v
```

### Real-hardware tests (read-only vs. opt-in motion)

When `libEFWFilter.so.1.8.4`, `70-pyzwo.rules`, and a physical ZWO EFW are installed locally:

- **Read-only hardware test (`test_hardware_readonly_open_and_query`)** opens the wheel without writing settings, verifies `slot_num > 0`, and checks that `get_slot()` returns a settled slot `0 <= slot < slot_num`:
  ```bash
  sg zwo -c "python3 -m unittest discover -s tests -v"
  ```
- **Opt-in operational motion test (`test_hardware_move_next_slot_and_back`)** physically moves the wheel to `(slot + 1) % slot_num` and back to its starting slot, and requires explicit confirmation via `PYZWO_RUN_HARDWARE_MOTION_TESTS=1`:
  ```bash
  sg zwo -c "PYZWO_RUN_HARDWARE_MOTION_TESTS=1 python3 -m unittest discover -s tests -v"
  ```

> **Warning:** Hardware tests do **not** run in CI. Never run motion, calibration, or USB unbind/rebind recovery tests on equipment mounted on an active telescope or during live observations.

## License

`pyzwo` is an experimental interface to ZWO hardware. Users are encouraged to validate its behaviour with their specific equipment before relying on it for unattended operation.
Applications integrating `pyzwo` should provide appropriate safeguards, error recovery, and supervision for their intended use.
The project is distributed under MPL-2.0. See [LICENSE](LICENSE) for warranty and liability terms.

The bundled ZWO SDK headers remain copyright of ZWO Company and are distributed under their respective license, reproduced in [`pyzwo/include/LICENSE.zwo`](pyzwo/include/LICENSE.zwo).

The proprietary ZWO SDK shared libraries are separate third-party components and are not covered by `pyzwo`'s license.
