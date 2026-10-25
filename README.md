# pyzwo

`pyzwo` is an independent, pure-Python interface to ZWO astronomy hardware (currently Electronic Filter Wheels via USB HID, with additional ZWO device classes planned incrementally). It provides framework-agnostic device access and error reporting without requiring ZWO's proprietary C shared libraries. Integrations with observatory-control frameworks are maintained separately.

> **Status:** Experimental synchronous ZWO EFW (Electronic Filter Wheel) baseline.

## Philosophy

I'm building `pyzwo` primarily for my own observatory, and I test it extensively against the hardware I actually own. At the same time, it is deliberately written **without hardcoded assumptions** tied to my specific rig—no baked-in slot counts or silent NVRAM overwrites on connect.

Every implemented module uses the device family's native USB protocol rather than model-specific constants. Where the lists below mention specific models or architectures, that reflects what has been verified on physical hardware so far versus what shares the same USB protocol and remains untested on hardware.

It started for my own setup, but it's MPL-2.0-licensed and free to grab for mainstream use.

## How it works

Rather than bundling or binding ZWO's precompiled C++ shared libraries (`libEFWFilter.so` / `.dylib` / `.dll`), `pyzwo` communicates directly with ZWO USB HID devices via `hid` (`hidapi`) in `pyzwo/hid.py` (`ZwoHid`) and exposes the filter wheel controller (`Efw` and `EfwStatus`) in `pyzwo/efw.py`.

> **Note:** This is an initial USB HID implementation—the public API is **not yet stable** and will change as async execution and additional device classes are introduced.

Key behaviors in the synchronous EFW controller (`Efw`):
- **Path-addressed, valid-by-construction handle:** `Efw(path)` opens the OS HID device node (discovered via `Efw.discover()` or a persistent `udev` symlink) on construction and holds the handle open until `close()` (or context-manager exit), avoiding the ~50 ms Linux `hidraw` open/close overhead and keeping per-device I/O off `hidapi`'s thread-unsafe open/close path.
- **1-based slots:** Slot positions are 1-based (`1 .. slot_count`), matching both the physical numbering on the filter wheel carousel and the HID wire protocol, with `slot=-1` indicating the wheel is currently moving or calibrating (and `slot_count=-1` while Hall-sensor calibration is counting slots).

## Concurrency, Deadlines & Limitations

- **Single-owner per device:** `Efw` is synchronous and designed for a single owner per physical wheel at a time. `discover()`, `__init__()`, and `close()` are serialized across threads with a module-level lock because `hidapi`'s `hid_enumerate`, `hid_open_path`, and `hid_close` share unsynchronized global state; once opened, separate `ZwoHid` instances for different physical devices can perform I/O in separate threads.
- **Polling deadlines vs. blocking USB I/O:** The `timeout` argument on `move_to_slot()` and `calibrate()` is a **Python-level monotonic polling deadline** checked between status queries, raising built-in `TimeoutError` if the wheel does not settle in time. Individual synchronous `hidapi` calls have no pre-emptive timeout if the kernel or USB stack blocks.

## Device classes & roadmap

- **EFW (Filter Wheels, USB HID `03c3:1f01` / `03c3:1f02`)** — **Implemented (synchronous baseline).**
  - [x] **ZWO EFW 7×2" (legacy revision, fw `3.0.6`, `03c3:1f01`)** — Verified on hardware (`openSUSE Tumbleweed x86_64`).
  - [ ] **ZWO EFW 7×2" II (USB Type-C revision, fw `3.5.2`, `03c3:1f01`)** — Untested with the new direct USB HID implementation.
  - [ ] Other slot variants (`5×2"`, `7×36mm`, `8×1.25"`, `Mini`) — Untested on hardware (uses the same USB HID protocol and dynamic `slot_count` discovery).
- **Asynchronous EFW execution** — **Planned next.**
- **EAF (Focusers, USB HID)** — **Planned next.**
- **ASI Cameras & AM Mounts** — **Planned.**

## Platform & architecture status

The package is pure Python (`requires-python = ">=3.11"`) built on `hid` (`hidapi`).

- [x] **Linux `x86_64`** — Verified on real hardware (`openSUSE Tumbleweed 20261008`, kernel `7.2.9-1-default x86_64`).
- [ ] **Linux `aarch64` / `armv7` / `armv6` / `x86`** — Untested on hardware.
- [ ] **macOS / Windows** — Untested on hardware.

## Linux device permissions (`udev`)

`pyzwo` never auto-installs system rules and never requires running the Python process as `root`.

On Linux, `hidapi-hidraw` accesses only `/dev/hidraw*` nodes. Unlike the upstream ZWO SDK ruleset, `pyzwo/udev/70-pyzwo.rules` does **not** grant world-writable (`0666`) access to USB buses, `/sys/bus/usb/drivers/usb/bind` or `unbind`, `drivers_autoprobe`, hub `power/level` attributes, or `usbcore.usbfs_memory_mb`. Instead, it scopes access strictly to ZWO USB HID nodes (`SUBSYSTEM=="hidraw"`, `ATTRS{idVendor}=="03c3"`):

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
from pyzwo import Efw

paths = Efw.discover()
with Efw(paths[0]) as wheel:
    status = wheel.get_status()  # EfwStatus(slot_count=7, slot=1)
    settled_slot = wheel.move_to_slot(2, unidirectional=False, timeout=30.0)
    print(f"Settled at slot {settled_slot}")
```

## Running tests

### Default unit suite (no hardware required)

The default test suite uses in-memory `hid` fakes and runs in CI without physical hardware attached:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv run mypy pyzwo tests
```

### Real-hardware tests (read-only vs. opt-in motion)

When `70-pyzwo.rules` and a physical ZWO EFW are installed locally:

- **Read-only hardware test (`test_hardware_readonly_status`)** discovers connected wheels and verifies `get_status()` returns a settled 1-based slot `1 <= slot <= slot_count`:
  ```bash
  PYZWO_RUN_HARDWARE_TESTS=1 uv run python -m unittest discover -s tests -v
  ```
- **Opt-in operational motion test (`test_hardware_move_next_slot_and_back`)** physically moves the wheel to `(slot % slot_count) + 1` and back to its starting slot, and requires explicit confirmation via `PYZWO_RUN_HARDWARE_MOTION_TESTS=1`:
  ```bash
  PYZWO_RUN_HARDWARE_MOTION_TESTS=1 uv run python -m unittest discover -s tests -v
  ```

> **Warning:** Hardware tests do **not** run in CI. Never run motion tests on equipment mounted on an active telescope or during live observations.

## License

`pyzwo` is an experimental interface to ZWO hardware. Users are encouraged to validate its behaviour with their specific equipment before relying on it for unattended operation.
Applications integrating `pyzwo` should provide appropriate safeguards, error recovery, and supervision for their intended use.
The project is distributed under MPL-2.0. See [LICENSE](LICENSE) for warranty and liability terms.

ZWO's original SDK license notice is preserved in [`pyzwo/LICENSE.zwo`](pyzwo/LICENSE.zwo).
