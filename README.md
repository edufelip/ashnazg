# Ashnazg

**One Emulator to Rule Them All.** A native control layer for AI agents and Android test pipelines.

This repository is public and MIT licensed. The v0.1 control layer uses a Rust MCP host connected to a persistent Android UiAutomation agent. It uses an existing Android emulator/device; the lightweight container and macOS microVM from the original proposal are later milestones.

## What v0.1 covers

- Compact semantic snapshots with test IDs, text, bounds and state.
- Tap, long press, Unicode text input, clear, bounded scroll-to, back and deep links, with a fresh post-action snapshot.
- PNG/WebP screenshots and numbered Set-of-Marks overlays.
- Filtered logs from a bounded persistent logcat reader.
- MCP stdio and authenticated loopback Streamable HTTP, plus APK installation setup.
- Real native, Compose and React Native fixtures; comparative latency benchmarks; public GitHub Actions checks.

These describe the release scope. See CI and [validation evidence](docs/validation.md) for what has actually passed. The original proposal's global performance figures are not release guarantees; snapshots, screenshots and settled actions are measured separately.

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

After starting the agent and forwarding its port:

```sh
export ANDROID_SERIAL=emulator-5554
# Set ASHNAZG_TOKEN to the same ephemeral token passed to instrumentation.
./target/release/ashnazg mcp
```

Configure a local MCP client to launch that command with the environment above. For Streamable HTTP, set `ASHNAZG_HTTP_TOKEN` and run `./target/release/ashnazg mcp --http 127.0.0.1:7778`; the endpoint is `/mcp` and requires `Authorization: Bearer …`. Stdio writes only MCP messages to stdout.

Advertised tools are `ui_snapshot`, `ui_act`, `ui_screenshot`, `app_logs` and `app_install`. `device_boot`/snapshots, custom rendering, container images, microVMs and iOS are not implemented in v0.1. Compose apps must opt in to `testTagsAsResourceId`; a hidden tag is not retrievable simply by including unimportant views.

## Quality and performance

Every PR runs native formatting/lint/tests/build, Android unit tests/lint/build, and real-emulator integration with screenshot and benchmark artifacts. A stable aggregate `Quality gate` represents the required checks. CI proves the tested cases on its recorded platform, not arbitrary app compatibility.

Benchmarks compare persistent RPC with stock adb XML dump and screenshot commands. They record raw samples, p50/p95, errors, host/device metadata and setup time separately. Screenshots use public UiAutomation capture and encoding; v0.1 does not claim zero-copy access. The accessibility quiet window is bounded and reported separately from RPC processing.

## Design and roadmap

- [MCP API and usage](docs/api.md)
- [Wire protocol and scope](docs/protocol-v0.1.md)
- [Performance stack decision and specification corrections](docs/adr-001-performance-stack.md)
- [Implementation plan](docs/superpowers/plans/2026-11-15-v0.1.md)
- [Original architecture proposal](docs/architecture-blueprint.md) — preserved as research targets, not implementation evidence.
- [Contributing](CONTRIBUTING.md)

After the v0.1 control layer, the roadmap is a stripped Android container (v0.2), a macOS Apple Silicon microVM launcher (v0.3), then a separate iOS bridge/research track (v0.4+).
