# Contributing

Ashnazg v0.1 controls an existing Android API 34+ emulator/device. Start with [the protocol](docs/protocol-v0.1.md), [the performance decision](docs/adr-001-performance-stack.md) and the component READMEs.

Keep changes reviewable through a pull request. CI must pass before merging. A successful native build is not an Android integration result, and a fixture pass is not compatibility proof for every app or OS version. Retain raw benchmark samples and host/device metadata when discussing performance.

## Validation

Use the pinned Rust toolchain, Java 17, Android SDK 35 and checked-in Gradle wrappers. Cargo.lock and fixture package lockfiles are committed for reproducibility. See Makefile and .github/workflows for exact required commands; the component READMEs explain setup.

Native transport/protocol tests cover bounded input, authentication and response correlation. Android JVM checks cover selectors and request validation. Emulator tests exercise a real native fixture and real Compose/React Native fixtures. Screenshots and comparative measurements are CI artifacts, not generated source files.

Add focused regression tests for behavior changes. Test failure paths as well as success. Mutating input must never be silently retried after a disconnect or timeout. Keep all MCP stdout protocol-clean, keep tokens out of logs and artifacts, and use subprocess argument arrays rather than shell interpolation.

## Device selection and cleanup

Use an explicit ANDROID_SERIAL whenever more than one device exists. UiAutomation is exclusive: another UI automation runner or inspector must release its own connection before Ashnazg starts. Do not terminate another session to seize the connection. The integration runner manages only its own forward and instrumentation process; it must fail rather than overwrite someone else's session. Use dedicated test fixtures: the runner installs packages and changes their state. Do not target a personal app with fixture tests.

## Performance reports

Use scripts/benchmark.py. Report setup separately, snapshot vs XML dump separately, and screenshot encoding formats explicitly. Compare identical screen state and retain failed samples as errors. Shared hosted runner measurements describe that run; they are not universal guarantees. Do not turn an aspirational figure from the initial blueprint into a claimed result.

## Scope

v0.1 does not implement a ReDroid container, custom VSync governor, macOS microVM, boot/snapshot lifecycle, or iOS-on-Linux compatibility. Those require their own designs, tests and evidence. Preserve unsupported states explicitly rather than returning mock success.
