# pyzwo

`pyzwo` is an independent, pure-Python interface to the official ZWO SDKs (currently `libEFWFilter`, with additional ZWO device classes planned incrementally). It provides framework-agnostic device access, lifecycle management, and error reporting. Integrations with observatory-control frameworks are maintained separately.

## Philosophy

I'm building `pyzwo` primarily for my own observatory, and I test it extensively against the hardware I actually own. At the same time, it is deliberately written **without hardcoded assumptions** tied to my specific rig—no baked-in slot counts or silent NVRAM overwrites on connect.

Every implemented module uses ZWO's official USB SDK for that device family rather than model-specific constants.

It started for my own setup, but it's MPL-2.0-licensed and free to grab for mainstream use.

## How it works

ZWO distributes precompiled C shared libraries (`.so` / `.dylib` / `.dll`) and flat C headers (`.h`). Rather than compiling a C++ wrapper extension per Python version and CPU architecture, `pyzwo` binds the C ABI directly using standard-library `ctypes`.

To check `ctypes` signatures against the bundled ZWO `.h` header without requiring proprietary vendor binaries to be installed, `tests/header_checker.py` parses the C function prototypes for the bound USB API (`pyzwo/include/EFW_filter.h`) and verifies their `argtypes` and `restype` against a fake `CDLL` surface alongside unit checks for `_EFWInfo` struct layout and `EFWErrorCode` names. Unbound C functions and header-parsed enum integer values are not compared automatically.

On Linux, `libEFWFilter.so` references `udev_*` symbols without always declaring `DT_NEEDED` for `libudev.so.1`; `load_zwo_library()` preloads `libudev.so.1` with `RTLD_GLOBAL`.

## ZWO SDK versions used

| Device Class | Bundled Header | Shared Library | `GetSDKVersion()` |
|---|---|---|---|
| **EFW** | `pyzwo/include/EFW_filter.h` | `libEFWFilter.so.1.7.0` | `"1, 7"` (`1.7.0`) |

`ZwoEfwSDK` binds 12 of the 16 C functions declared in `EFW_filter.h` (`EFWGetSDKVersion`, `EFWGetNum`, `EFWGetID`, `EFWOpen`, `EFWClose`, `EFWGetProperty`, `EFWGetPosition`, `EFWSetPosition`, `EFWGetDirection`, `EFWSetDirection`, `EFWCalibrate`, `EFWGetFirmwareVersion`; `EFWGetProductIDs`, `EFWGetHWErrorCode`, `EFWGetSerialNumber`, and `EFWSetID` are not yet bound).

## Platform & architecture status

The package is pure Python (`dependencies = []`, `requires-python = ">=3.11"`) and resolves ZWO's shared library in this order: an explicit `ZwoEfwSDK(lib_path=...)`, the `PYZWO_EFWFILTER_LIB` environment variable, `/usr/local/lib`, `/usr/lib`, `pyzwo/lib/<arch>/`, then `ctypes.util.find_library`.

- [x] **Linux `x86_64` (`x64`)** — Verified on real hardware (`openSUSE Tumbleweed 20261008`, kernel `7.2.9-1-default x86_64`).
- [ ] **Linux `aarch64` (`armv8`) / `armv7` / `armv6` / `x86`** — Untested on hardware.
- [ ] **macOS (`mac`) / Windows (`x64` / `x86`)** — Untested on hardware.

## Linux device permissions (`udev`)

`pyzwo` never auto-installs system rules and never requires running the Python process as `root`.

`libEFWFilter.so.1.7.0` uses `hidapi-hidraw` on Linux and accesses only `/dev/hidraw*` nodes. Unlike the upstream ZWO SDK ruleset, `pyzwo/udev/70-pyzwo.rules` does **not** grant world-writable (`0666`) access to USB buses, `/sys/bus/usb/drivers/usb/bind` or `unbind`, `drivers_autoprobe`, hub `power/level` attributes, or `usbcore.usbfs_memory_mb`. Instead, it scopes access strictly to ZWO USB HID nodes (`SUBSYSTEM=="hidraw"`, `ATTRS{idVendor}=="03c3"`):

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

## Running tests

The default test suite uses in-memory fakes and `FakeCDLL` and runs in CI without `libEFWFilter.so` installed. Pointing `PYZWO_EFWFILTER_LIB` at a non-existent path guarantees that hardware tests are skipped even on a machine where the vendor library and a wheel are present:

```bash
PYZWO_EFWFILTER_LIB=/nonexistent/libEFWFilter.so python3 -m unittest discover -s tests -v
```

## License

`pyzwo` is licensed under the Mozilla Public License 2.0 (MPL-2.0). See [LICENSE](LICENSE).

The bundled ZWO SDK headers remain copyright of ZWO Company and are distributed under their respective license, reproduced in [`pyzwo/include/LICENSE.zwo`](pyzwo/include/LICENSE.zwo).

The proprietary ZWO SDK shared libraries are separate third-party components and are not covered by `pyzwo`'s license.
