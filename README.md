# pyzwohid

`pyzwohid` is an independent, pure-Python interface to ZWO USB HID astronomy hardware (currently Electronic Filter Wheels, with EAF focusers planned next). It provides framework-agnostic device access and error reporting without requiring ZWO's proprietary C shared libraries. Integrations with observatory-control frameworks are maintained separately.

> **Status:** Experimental asynchronous ZWO EFW (Electronic Filter Wheel) baseline.

## Philosophy

I'm building `pyzwohid` primarily for my own observatory, and I test it extensively against the hardware I actually own. At the same time, it is deliberately written **without hardcoded assumptions** tied to my specific rig—no baked-in slot counts or silent NVRAM overwrites on connect.

Every implemented module uses the device family's native USB protocol rather than model-specific constants. Where the lists below mention specific models or architectures, that reflects what has been verified on physical hardware so far versus what shares the same USB protocol and remains untested on hardware.

It started for my own setup, but it's MPL-2.0-licensed and free to grab for mainstream use.

## How it works

Rather than bundling or binding ZWO's precompiled C++ shared libraries (`libEFWFilter.so` / `.dylib` / `.dll`), `pyzwohid` communicates directly with ZWO USB HID devices via `hid` (`hidapi`) in `pyzwohid/hid.py` (`ZwoHid`) and exposes the filter wheel controller (`Efw` and `EfwStatus`) in `pyzwohid/efw.py`.

> **Note:** This is an initial USB HID implementation—the public API is **not yet stable** and will change as additional USB HID device classes are introduced.

Key behaviors in the asynchronous EFW controller (`Efw`):
- **Path-addressed, valid-by-construction handle:** `await Efw.open(path)` verifies the USB `(VID, PID)` and opens the OS HID device node (discovered via `await Efw.discover()` or a persistent `udev` symlink), holding the handle open until `await close()` (or `async with` exit) to avoid the ~50 ms Linux `hidraw` open/close overhead.
- **1-based slots:** Slot positions are 1-based (`1 .. slot_count`), matching both the physical numbering on the filter wheel carousel and the HID wire protocol, with `slot=-1` indicating the wheel is currently moving or calibrating (and `slot_count=-1` while Hall-sensor calibration is counting slots).

## Concurrency, Deadlines & Limitations

- **Single-worker HID executor:** All `hidapi` C calls (`discover`, `open`, `close`, `_send`, `_query`) are dispatched onto a single shared `ThreadPoolExecutor(max_workers=1)` so `hidapi` operations never race on process-global state or on an open `hid_device` handle, while lightweight (~3–6 ms) feature-report transactions across multiple ZWO HID devices interleave cleanly between `asyncio.sleep()` polling intervals.
- **Polling deadlines vs. blocking USB I/O:** The `timeout` argument on `move_to_slot()` and `calibrate()` is a **Python-level monotonic polling deadline** checked between status queries, raising built-in `TimeoutError` if the wheel does not settle in time. Individual `hidapi` calls have no pre-emptive timeout if the kernel or USB stack blocks.

## Device classes & roadmap

- **EFW (Filter Wheels, USB HID `03c3:1f01` / `03c3:1f02`)** — **Implemented (asynchronous baseline).**
  - [x] **ZWO EFW 7×2" (legacy revision, fw `3.0.6`, `03c3:1f01`)** — Verified on hardware (`openSUSE Tumbleweed x86_64`).
  - [ ] **ZWO EFW 7×2" II (USB Type-C revision, fw `3.5.2`, `03c3:1f01`)** — Untested with the new direct USB HID implementation.
  - [ ] Other slot variants (`5×2"`, `7×36mm`, `8×1.25"`, `Mini`) — Untested on hardware (uses the same USB HID protocol and dynamic `slot_count` discovery).
- **EAF (Focusers, USB HID)** — **Planned next.**
- **CAA (Rotators, USB HID)** — **Tentative** (no hardware on hand—at most an experimental/skeleton implementation unless contributed and verified by a CAA owner).

## Platform & architecture status

The package is pure Python (`requires-python = ">=3.11"`) built on `hid` (`hidapi`).

- [x] **Linux `x86_64`** — Verified on real hardware (`openSUSE Tumbleweed 20261008`, kernel `7.2.9-1-default x86_64`).
- [ ] **Linux `aarch64` / `armv7` / `armv6` / `x86`** — Untested on hardware.
- [ ] **macOS / Windows** — Untested on hardware.

## Linux device permissions (`udev`)

`pyzwohid` never auto-installs system rules and never requires running the Python process as `root`.

On Linux, `hidapi-hidraw` accesses only `/dev/hidraw*` nodes. Unlike the upstream ZWO SDK ruleset, `pyzwohid/udev/70-pyzwohid.rules` does **not** grant world-writable (`0666`) access to USB buses, `/sys/bus/usb/drivers/usb/bind` or `unbind`, `drivers_autoprobe`, hub `power/level` attributes, or `usbcore.usbfs_memory_mb`. Instead, it scopes access strictly to ZWO USB HID nodes (`SUBSYSTEM=="hidraw"`, `ATTRS{idVendor}=="03c3"`):

- **Attended desktop Linux sessions:** `TAG+="uaccess"` lets `systemd-logind` dynamically grant an ACL on the matching `/dev/hidraw*` node to the active local seat user (ordered at `70-` so it runs before `73-seat-late.rules`).
- **Headless observatory services / daemons / SSH sessions:** Desktop seat ACLs (`uaccess`) do not apply when no local seat session is active. The rule assigns `GROUP="zwo", MODE="0660"` so only `root` and members of the `zwo` group can open the device node.
- **Persistent USB port symlinks:** Because `/dev/hidrawN` indices can change across reboots and ZWO EFWs do not expose a unique USB descriptor serial number, you can pass `systemd-udevd`'s built-in `/dev/input/by-path/<ID_PATH>-hidraw` symlink directly to `await Efw.open(...)` or add a `SYMLINK+="..."` rule pinned to `KERNELS` / `ENV{ID_PATH}` in `70-pyzwohid.rules` (see examples in [`pyzwohid/udev/70-pyzwohid.rules`](pyzwohid/udev/70-pyzwohid.rules)).

To configure non-root access on a Linux host:

```bash
sudo groupadd --system zwo
sudo usermod -aG zwo "$USER"   # or your headless service account (e.g. pyobs)
sudo install -m 0644 pyzwohid/udev/70-pyzwohid.rules /etc/udev/rules.d/70-pyzwohid.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
```

After adding a user to the `zwo` group, log out and log back in (or restart the systemd service with `SupplementaryGroups=zwo`) so the new group membership takes effect, or replug the USB device if `udevadm trigger` was not run.

## Usage

```python
import asyncio
from pyzwohid import Efw


async def main() -> None:
    paths = await Efw.discover()
    async with await Efw.open(paths[0]) as wheel:
        status = await wheel.get_status()  # EfwStatus(slot_count=7, slot=1)
        settled_slot = await wheel.move_to_slot(2, unidirectional=False, timeout=30.0)
        print(f"Settled at slot {settled_slot}")


asyncio.run(main())
```

## Running tests

### Default unit suite (no hardware required)

The default test suite uses in-memory `hid` fakes and runs in CI without physical hardware attached:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv run mypy pyzwohid tests
```

### Real-hardware tests (read-only vs. opt-in motion)

When `70-pyzwohid.rules` and a physical ZWO EFW are installed locally:

- **Read-only hardware test (`test_hardware_readonly_status`)** discovers connected wheels and verifies `get_status()` returns a settled 1-based slot `1 <= slot <= slot_count`:
  ```bash
  PYZWOHID_RUN_HARDWARE_TESTS=1 uv run python -m unittest discover -s tests -v
  ```
- **Opt-in operational motion test (`test_hardware_move_next_slot_and_back`)** physically moves the wheel to `(slot % slot_count) + 1` and back to its starting slot, and requires explicit confirmation via `PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1`:
  ```bash
  PYZWOHID_RUN_HARDWARE_MOTION_TESTS=1 uv run python -m unittest discover -s tests -v
  ```

> **Warning:** Hardware tests do **not** run in CI. Never run motion tests on equipment mounted on an active telescope or during live observations.

## License

`pyzwohid` is an experimental interface to ZWO USB HID hardware. Users are encouraged to validate its behaviour with their specific equipment before relying on it for unattended operation.
Applications integrating `pyzwohid` should provide appropriate safeguards, error recovery, and supervision for their intended use.
The project is distributed under MPL-2.0. See [LICENSE](LICENSE) for warranty and liability terms.

ZWO's original SDK license notice is preserved in [`pyzwohid/LICENSE.zwo`](pyzwohid/LICENSE.zwo).
