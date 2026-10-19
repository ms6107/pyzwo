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

Test observable behavior and documented contracts, not implementation structure.

- **Separate files by layer:** Keep low-level `ctypes` structs, error enums, and thin SDK wrappers in `pyzwo/_<device>_sdk.py` and high-level controllers in `pyzwo/<device>.py`. Split tests per device family into `test_<device>_sdk.py` (`FakeCDLL` SDK wrapper unit tests and opt-in C11 compiler ABI checks via `PYZWO_RUN_ABI_TESTS=1`), `test_<device>.py` (controller unit tests via `Fake<Device>SDK`), and `test_<device>_hardware.py` (opt-in real-hardware tests). Place each test at the layer it actually exercises.
- **Observable contracts over call counts:** Prefer assertions about public results, device state, resource ownership, and simulated hardware state. Assert SDK command attempts only when their presence or absence is a safety or lifecycle requirement; avoid asserting exact counts or ordering of ordinary SDK queries (`get_num`, `get_id`, poll iterations) unless required by the vendor protocol. Do not depend on private methods or incidental initialization sequences.
- **Minimal scope & readability:** Each test method must verify a single behavior or state transition without chaining scenarios on mutated instances or jumping to helper functions. Prefer small, explicit setup duplication, and explicitly pass fake parameters that a test depends on even if they match defaults (e.g., `FakeEfwSDK(num_devices=1)`). Preserve independent regression coverage when refactoring; a reduced test count is not a goal in itself.
- **Targeted parameterization, no logic in tests:** Use `for ... in cases: with self.subTest(...):` only when all cases share the exact same call and assertion shape. Never put branching logic (`if/else`) or re-implemented production logic inside tests, and omit logically duplicate subcases (keep one per equivalence class/boundary).
- **Named constants over magic values:** Use descriptive constants for sequence items, sentinels (`MOVING_POS`), bounds (`SLOT_COUNT`), hardware IDs, and local roles (`initial_slot`, `target_slot`, `wrong_slot`). Inject post-open fault sequences after `open()` rather than padding sequences with hidden initial elements.
- **Test-double (`Fake*`) fidelity:** Use simulated hardware state as the primary source of observations and scripted sequences for specific transitions (with strict sequence exhaustion opt-in via `strict_script = True`). Enforce connection ownership by default on all handle-taking methods, record command attempts (`open_attempts`, `close_attempts`, `move_attempts`, `id_requests`) at the start of fake methods *before* fake-side validation or failure injection so negative assertions never pass vacuously, and create fresh exception instances with accurate operation labels per failure.
- **SDK-independent unit suite, ABI probes & hardware safety:** Default unit tests must pass without ZWO shared libraries or a C compiler installed. Verify C headers via the C11 compiler ABI probe (`PYZWO_RUN_ABI_TESTS=1`, using `ziglang` in `[dependency-groups].abi` and `-Xclang -ast-dump=json` rather than custom regex parsers or cache globs) whenever `pyzwo/_<device>_sdk.py` or bundled `.h` headers change. Real-hardware tests must skip cleanly when hardware is absent, never run in CI, require explicit operator confirmation (`PYZWO_RUN_HARDWARE_MOTION_TESTS=1`) before moving equipment, and leave hardware stopped in place (without `try/finally` recovery moves) if an intermediate assertion fails.

## Git & Repository Hygiene
- **Clean tree:** Never commit proprietary vendor binaries (`.so`, `.dylib`, `.dll`), `__pycache__`, `.pyc` files, or host-specific secrets.
- **Review before commit:** Do not automatically commit, squash, force-push, or publish changes; present a concise diff summary, test results, and proposed commit message for maintainer review at each step.
