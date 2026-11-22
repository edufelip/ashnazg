# Ashnazg

**One Emulator to Rule Them All.** A native control layer for AI agents and Android test pipelines.

This repository is public and MIT licensed. A Rust MCP host connects to a persistent Android UiAutomation agent. The [v0.3.0 experimental release](https://github.com/edufelip/ashnazg/releases/tag/v0.3.0) includes automatic Android VM setup on Apple Silicon macOS, with no host Android SDK required. External Android devices remain supported on Linux and macOS.

## Control tools

- Compact semantic snapshots with test IDs, text, bounds and state.
- Tap, long press, Unicode text input, clear, bounded scroll-to, back and deep links, with a fresh post-action snapshot.
- PNG/WebP screenshots and numbered Set-of-Marks overlays.
- Filtered logs from a bounded persistent logcat reader.
- MCP stdio and authenticated loopback Streamable HTTP, plus APK installation setup.
- Real native, Compose and React Native fixtures; comparative latency benchmarks; public GitHub Actions checks.

These describe the release scope. See CI and [validation evidence](docs/validation.md) for what has actually passed. The original proposal's global performance figures are not release guarantees; snapshots, screenshots and settled actions are measured separately.

## Install a packaged release

Download the [macOS ARM64 package](https://github.com/edufelip/ashnazg/releases/download/v0.3.0/ashnazg-v0.3.0-macos-aarch64.zip) and follow the [release installation guide](docs/releases.md) for checksums and MCP client configuration. The released host downloads its matching runtime and owns VM/agent startup and shutdown. The helper is ad-hoc signed, without Developer ID signing or notarization.

[Real CLI/VM acceptance](docs/evidence/public-runtime-0.3.0/README.md) covers public cold acquisition and a network-denied warm restart. Linux packages use the external Android setup below. Building from source remains supported.

On Ubuntu x86_64, the [development session guide](docs/ubuntu-development.md)
shows how to run the published Linux CLI with an owned Android guest, keep app
data across sessions, and use [scrcpy alongside MCP](docs/development-viewer.md).

## Build and test

Requirements: the pinned Rust toolchain (rustup), Java 17, Android SDK/platform-tools, SDK platform 35, Python 3.10+, and Node 24.15.0/npm for the framework fixtures (`.nvmrc`).

```sh
cargo build --release --locked
cargo test --locked
(cd agent-android && ./gradlew test lint assembleDebug)
```

The [Android agent guide](agent-android/README.md), [framework fixture guide](fixtures/README.md), and [integration/benchmark guide](scripts/README.md) contain the exact setup and device-test commands. The integration harness installs only the test packages and refuses to replace an existing port forward.

Select your device with `ANDROID_SERIAL`. Android permits a single UiAutomation owner: stop other inspection/test automation before starting Ashnazg; it reports an explicit startup failure if that connection is occupied. The guest listens on loopback port 7777 and requires an explicit token. `ASHNAZG_TOKEN` supplies the host credential; `ASHNAZG_AGENT_ADDR` changes the loopback address. Never expose the forwarded guest socket to a network.

## MCP

For an external Android device, after starting the agent and forwarding its port:

```sh
export ANDROID_SERIAL=emulator-5554
# Set ASHNAZG_TOKEN to the same ephemeral token passed to instrumentation.
./target/release/ashnazg mcp
```

Configure a local MCP client to launch that command with the environment above. For Streamable HTTP, set `ASHNAZG_HTTP_TOKEN` and run `./target/release/ashnazg mcp --http 127.0.0.1:7778`; the endpoint is `/mcp` and requires `Authorization: Bearer …`. Stdio writes only MCP messages to stdout.

On Apple Silicon macOS, the released host selects the [native MCP bootstrap](docs/native-cli.md) when `ASHNAZG_TOKEN` is absent. It initializes MCP before runtime readiness and owns signal cleanup. The public catalog matches the `v0.3.0` source stamp; unstamped development builds can use the external Android configuration above.

Advertised tools are `ui_snapshot`, `ui_act`, `ui_screenshot`, `app_logs` and `app_install`. Native MCP owns VM startup; the tool set does not expose VM snapshot management or iOS control. Compose apps must opt in to `testTagsAsResourceId`; a hidden tag is not retrievable simply by including unimportant views.

## Quality and performance

Every PR runs native formatting/lint/tests/build, Android unit tests/lint/build, and real-emulator integration with screenshot and benchmark artifacts. A stable aggregate `Quality gate` represents the required checks. CI proves the tested cases on its recorded platform, not arbitrary app compatibility.

Benchmarks compare persistent RPC with stock adb XML dump and screenshot commands. They record raw samples, p50/p95, errors, host/device metadata and setup time separately. Screenshots use public UiAutomation capture and encoding; v0.1 does not claim zero-copy access. The accessibility quiet window is bounded and reported separately from RPC processing.

## Design and roadmap

- [MCP API and usage](docs/api.md)
- [Paired Android session benchmark](docs/agent-session-benchmark.md)
- [macOS iOS Simulator fixture experiment](platform/ios/README.md)
- [Wire protocol and scope](docs/protocol-v0.1.md)
- [Performance stack decision and specification corrections](docs/adr-001-performance-stack.md)
- [Implementation plan](docs/superpowers/plans/2026-11-15-v0.1.md)
- [Original architecture proposal](docs/architecture-blueprint.md) — preserved as research targets, not implementation evidence.
- [Contributing](CONTRIBUTING.md)

The experimental Android VM is distributed for macOS in v0.3.0. The source
tree also has a separate, opt-in iOS Simulator fixture experiment. Performance
claims are scoped to the configurations in the linked evidence.
