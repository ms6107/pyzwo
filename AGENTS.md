# Agent Instructions: `pyzwo`

`pyzwo` is a standalone, zero-dependency Python interface to official ZWO C SDKs using standard-library `ctypes`.

## Architectural Boundaries
- **`pyzwo` scope:** Vendor SDK bindings, device discovery, connection lifecycle, authoritative hardware/controller state (`DeviceState`), and error translation (`ZwoError`, `ZwoTimeoutError`).
- **No external runtime dependencies:** Do not add `pyobs`, `numpy`, or any third-party runtime packages to `dependencies` in `pyproject.toml`. Integrations with observatory-control frameworks (such as `pyobs-zwo`) live in separate repositories.
- **No premature abstraction:** Implement and verify each device family vertically (synchronous core first, then asynchronous execution) before extracting shared abstractions across device classes.

## Concurrency & Deadline Rules
- **Single-owner synchronous core:** Synchronous controllers follow a single-owner, single-thread usage contract. Do not claim thread safety across concurrent callers or between simultaneous raw SDK and managed controller access on the same device. Do not rely on the Python GIL to synchronize native C SDK calls.
- **Polling deadlines vs. native C calls:** Python-level `timeout` parameters on synchronous operations bound the monotonic polling loop *between* SDK calls. They cannot preempt or terminate a blocked native C/USB function call inside the vendor library.
- **Honest hardware state:** Never fabricate a `DeviceState.READY` state or claim physical movement has stopped without re-querying the hardware.

## Testing & Hardware Safety
- **SDK-independent default test suite:** Importing `pyzwo` and running the default unit/header test suite (`python -m unittest discover -s tests -v`) must succeed on machines without ZWO shared libraries installed. Use `tests/header_checker.py` (`FakeCDLL`) and in-memory SDK fakes for unit and ABI signature tests.
- **Opt-in hardware tests:** Real-hardware tests must skip cleanly when the vendor library or USB device is absent. Do not imply hardware tests run in CI.
- **Equipment safety:** Never run physical motion or USB unbind/rebind tests on equipment attached to an active telescope without explicit operator confirmation.

## Git & Repository Hygiene
- Never commit proprietary vendor binaries (`.so`, `.dylib`, `.dll`), `__pycache__`, `.pyc` files, or host-specific secrets.
- Do not automatically commit, squash, force-push, or publish changes; present a concise diff summary, test results, and proposed commit message for maintainer review at each step.
