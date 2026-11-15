# Android integration and benchmark runners

These commands exercise the real `agent-android` instrumentation and fixture APKs. They never select an arbitrary adb target: set `ANDROID_SERIAL`, or expose exactly one online emulator. `ASHNAZG_ADB` may select a non-default adb executable. The harness creates an ephemeral guest token, installs APKs on the selected device, owns `tcp:7777` forwarding, waits for a token-authenticated `ping`, and stops its instrumentation/forward in `finally` cleanup.

## Build and run

```sh
cd agent-android && ./gradlew test lint assembleDebug
cd ../fixtures && npm --prefix reactnative ci
./gradlew :compose:assembleDebug :reactnative:assembleDebug
cd ..
cargo build
python3 -m unittest discover -s tests -v
python3 scripts/integration.py --framework-fixtures \
  --output artifacts/integration.json --artifacts artifacts/integration
cargo build --release --locked
python3 scripts/benchmark.py --iterations 10 --output artifacts/benchmark.json
```

The integration suite covers authenticated JSON-RPC, correlation over a persistent connection, every action against the native fixture, stale and ambiguous selectors, malformed and oversized requests, logs, PNG/WebP screenshots and current screenshot marks. It also runs actual Compose and React Native APKs and verifies their semantics and screenshot capture. MCP stdio is initialized using a negotiated protocol exchange; its screenshot must return MCP image content.

The native fixture also verifies password redaction. Typing into a nonempty masked field is expected to fail because the existing value cannot be read safely; clear it first, then type, and keep its value out of snapshots.

The benchmark defaults to the optimized `target/release/ashnazg` host (or honors `ASHNAZG_BIN`), and runs the stock `uiautomator dump` plus `adb pull` and `adb exec-out screencap` baselines before persistent instrumentation claims `UiAutomation`. It then measures a persistent guest socket and initialized native MCP stdio for snapshots and PNG/WebP screenshots, followed by settled tap actions through both paths. Every action sample validates `settled: true`, a fresh snapshot ID, the increment button's stable test ID, and its exact expected count. Action timing includes the UI-settled barrier and updated snapshot. Output keeps every successful sample, p50/p95, setup phases, Android environment, source HEAD/dirty state and host binary SHA-256. Any failed iteration fails the command; failures are never stored as zero. These are observed measurements, not a speedup guarantee.

`make harness-unit` runs the stdlib-only deterministic harness tests. `make integration` assumes the host debug binary and APKs were already built and an emulator is online. CI uses `scripts/ci_emulator.py` to create and supervise its own API 35 default x86_64 Pixel 6 emulator, then runs the unit suite, real instrumentation integration, and benchmark sequentially with `ANDROID_SERIAL` set to that emulator.

The CI supervisor is restricted to GitHub Actions (`GITHUB_ACTIONS=true`). It refuses a pre-existing emulator serial or occupied emulator ports, creates a uniquely named AVD in a private runner-temp directory, and only terminates the process group it launched. It does not send input key events during boot. After `sys.boot_completed`, it asks WindowManager to dismiss the keyguard, then waits on bounded conditions: a clean last-ANR marker, a display 0 focused application window, a matching visible WindowManager entry, a ready surface, and `HAS_DRAWN`. An ANR or readiness timeout fails the job before tests. The emulator boot log is uploaded with the existing JSON and screenshot evidence; bounded last-ANR, input, window, and logcat diagnostics are written only for pre-test boot failures.

On integration failure, device diagnostics are collected only when `ASHNAZG_CI_DIAGNOSTICS=1`. The CI workflow opts in for its disposable emulator and stores bounded `lastanr`, recent logcat, process and memory information after redacting the run token. The same opt-in passes `-e traceWindowLookupMiss true` to instrumentation; failed target-window lookups log the target ID and at most eight visible window IDs, without UI text or tokens. Local runs do not collect or persist device logs unless explicitly enabled.
