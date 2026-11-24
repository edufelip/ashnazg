# Cross-Platform MCP & Runtime Comparison

This report synthesizes Ashnazg's verified Android and iOS Simulator evidence into one side-by-side reference. Every figure comes directly from the committed measurement receipts; Android (`Intel Core i5-12600K` Linux, API 35, cgroup v2) and iOS (`MacBook Air` Apple Silicon, `macOS 27.2.1`, `Xcode 27.2`, `iOS 26.5`, `proc_pid_rusage`) run on distinct hosts, OS architectures, workloads, and memory models and must be interpreted within their own platform columns.

> **Interactive Chart:** Open [cross-platform-comparison.html](cross-platform-comparison.html) in a browser for an interactive Cold / Warm visualization of these datasets.

---

## 1. Android: ReDroid Container vs Android Studio AVD

### 1a. Ashnazg MCP Fixture Benchmark (`N = 2` opposite-order pairs)

Source: [agent-session-benchmark.md](../agent-session-benchmark.md) and [summary.json](../evidence/agent-session-comparison-2026-11-19/summary.json).

- **Host & constraints:** Linux x86_64 (`Intel Core i5-12600K`), same 2-CPU cgroup v2 quota, `8 GiB` memory cap, swap disabled, `Android 15` (`API 35`, x86_64), `720×1280` @ `320 dpi`, SwiftShader software rendering, zero animation scales.
- **Workload:** Boot owned guest, install/launch agent + fixture, `initialize`, `ui_snapshot`, single `ui_act` (`tap`), poll `ui_screenshot` until decoded PNG marker increments (`0 → 1`), and verify owned cgroup removal on teardown.

| Metric (median across 2 pairs) | ReDroid Cold | AVD Cold | ReDroid Warm | AVD Warm | ReDroid Delta (Cold / Warm) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Launch to first usable MCP snapshot | `18.74 s` | `69.26 s` | `16.44 s` | `41.67 s` | **`-72.9%`** / **`-60.5%`** |
| Tap RPC round trip | `740.9 ms` | `1,057.0 ms` | `744.4 ms` | `1,155.5 ms` | `-29.9%` / `-35.6%` |
| Tap response to fresh decoded PNG | `29.7 ms` | `413.9 ms` | `30.0 ms` | `553.4 ms` | `-92.8%` / `-94.6%` |
| **End-to-end tap + fresh PNG** | **`771 ms`** | **`1,471 ms`** | **`774 ms`** | **`1,709 ms`** | **`-47.6%`** / **`-54.7%`** |
| Idle cgroup memory (`memory.current`) | `1.35 GiB` | `6.28 GiB` | `1.13 GiB` | `4.97 GiB` | `-78.5%` / `-77.3%` |
| Idle anonymous memory | `0.72 GiB` | `4.97 GiB` | `0.62 GiB` | `3.89 GiB` | `-85.5%` / `-84.1%` |
| Sampled interaction memory max | `1.35 GiB` | `6.40 GiB` | `1.15 GiB` | `5.36 GiB` | `-78.9%` / `-78.5%` |
| Shutdown (excluding post-agent window) | `1.76 s` | `1.35 s` | `1.65 s` | `1.89 s` | `+0.41 s` / `-0.24 s` |

### 1b. Android Meer Real-App Detox Baseline (`N = 4` pairs × 5 measured runs)

Source: [meer-runtime-comparison-2026-11-18](../evidence/meer-runtime-comparison-2026-11-18/README.md).

- **Scope note:** Timers run inside the existing Detox `Login → Search → Home` journey body (excluding Detox startup, APK installation, and test setup). Post-trial memory uses `PSS + SwapPSS` across different process scopes (`QEMU` tree on AVD vs guest-visible processes on ReDroid, with ReDroid completing on the second sweep).

| Metric | ReDroid | Android Studio AVD | Difference |
| --- | ---: | ---: | ---: |
| Full journey `p50` | `5.87 s` | `10.18 s` | `-42.3%` |
| Full journey `p95` (nearest-rank) | `6.10 s` | `11.07 s` | `-44.9%` |
| Login helper to Home `p50` | `2.43 s` | `4.10 s` | `-40.8%` |
| Search helper + feed readiness `p50` | `2.71 s` | `4.95 s` | `-45.4%` |
| Post-trial `PSS + SwapPSS` `p50` | `1.81 GiB` | `3.70 GiB` | Separate scopes & sweep times |

---

## 2. iOS Simulator (`iOS 26.5`): `ashnazg mcp` vs `direct-xctest` (`N = 3`)

Source: [ios-mcp-comparison.md](ios-mcp-comparison.md).

- **Host & app:** Linked Apple Silicon `MacBook Air` (`macOS 27.2.1`, `Xcode 27.2`), `iPhone-17` on `iOS 26.5` (`23F77`), pinned signed `Meer-Dev` (`com.edufelip.meer.dev`), loopback Metro (`127.0.0.1:8095`), epoch-aware mock API (`127.0.0.1:4019`).
- **Workload:** `Login → Home (+ 1206×2622 PNG) → Search → Home (+ 1206×2622 PNG)` via visible UI controls (`verify_mcp_app.py --navigation ui` and `MeerJourneyBenchmarkTests`). Cold = newly created simulator; Warm = rebooted same simulator with expired refresh token (`POST /auth/refresh 401` forcing fresh login).
- **Apple `mcpbridge` (`Xcode 27.1`/`27.2`) exclusion:** Apple's MCP bridge passed the public SwiftUI fixture on hosted `Xcode 27.1` / `iOS 27.0` (`ece016b`), but requires `iOS 27.0+` (rejects `iOS 26.5`) while normal Xcode-signed `Meer.app` aborts on `iOS 27.0` (`Library not loaded: /usr/lib/libSystem.B.dylib`) and earlier ad-hoc scene-patched `3d902a3f` failed password-field focus before typing.

### 2a. Latency Comparison (`N = 3` medians `[min – max]`)

| Stage / Metric | `direct-xctest` Cold | `ashnazg-mcp` Cold | `direct-xctest` Warm | `ashnazg-mcp` Warm | Boundary Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| `arm_start` | `9.8 ms` | `22,914.6 ms` | `6.6 ms` | `28,553.1 ms` | Runner spawn vs bridge spawn + MCP handshake + sim/app launch |
| `first_usable_snapshot` | `9,198.4 ms` | `225.6 ms` | `8,816.4 ms` | `228.8 ms` | `direct-xctest` absorbs `xcodebuild` startup + launch here |
| `login_to_home` | `20,406.5 ms` | `21,349.0 ms` | `19,196.2 ms` | `17,117.2 ms` | Full credential entry + reveal scrolls + submit to confirmed Home |
| `submit_to_home` | `7,388.1 ms` | `6,940.0 ms` | `6,102.8 ms` | `2,621.4 ms` | `ashnazg-mcp` splits additional `2.5s` settle into `prompt_settle` (`6,594.0` / `8,097.9 ms`) |
| `home_screenshot` (`1206×2622` PNG) | `140.6 ms` | `145.3 ms` | `200.2 ms` | `199.8 ms` | In-memory PNG validation (`published: false`) |
| **`home_to_search`** | **`2,318.2 ms`** | **`2,520.3 ms`** | **`2,481.6 ms`** | **`2,712.7 ms`** | **`+202 ms` cold / `+231 ms` warm over native XCTest** |
| **`search_to_home` (hierarchy)** | **`2,076.2 ms`** | **`2,392.8 ms`** | **`2,224.4 ms`** | **`2,437.2 ms`** | Back button tap to usable `home-search-button` |
| `final_home_screenshot` | `171.3 ms` | `259.2 ms` | `185.9 ms` | `258.7 ms` | Post-return `1206×2622` PNG |
| **`search_to_home_with_screenshot`** | **`2,247.5 ms`** | **`2,714.4 ms`** | **`2,411.0 ms`** | **`2,691.3 ms`** | **`+467 ms` cold / `+280 ms` warm over native XCTest** |
| `shutdown` | `3,785.5 ms` | `5,770.4 ms` | `3,815.8 ms` | `5,459.4 ms` | Runner exit vs stdio EOF + bridge/simulator cleanup |
| **Whole-arm `elapsed_seconds`** | **`51.178 s`** | **`59.095 s`** | **`55.748 s`** | **`63.987 s`** | **`+7.92 s` (`+15.5%`) cold / `+8.24 s` (`+14.8%`) warm** |

### 2b. Peak Physical Memory (`N = 3` medians `ri_phys_footprint` via `proc_pid_rusage`)

| Process Role | `direct-xctest` Cold | `ashnazg-mcp` Cold | `direct-xctest` Warm | `ashnazg-mcp` Warm |
| --- | ---: | ---: | ---: | ---: |
| `app` (`Meer`) | `332.5 MiB` | `329.0 MiB` | `295.2 MiB` | `328.4 MiB` |
| `simulator` (`launchd_sim`) | `33.8 MiB` | `33.6 MiB` | `22.5 MiB` | `23.8 MiB` |
| `helper` (max XCTest runner) | `71.7 MiB` | `71.8 MiB` | `71.2 MiB` | `70.8 MiB` |

In the matching `N = 1` per-stage profile breakdown, the `ashnazg` Rust host process added `5.5–12.2 MiB` physical footprint (`10.3–21.1 MiB` RSS). Shared `SimStreamProcessorService` remained separate and flat at `11.5–11.6 MiB` footprint across all stages.

---

## 3. Architectural Summary Across Platforms

| Dimension | Android (Ashnazg + ReDroid) | Android (Ashnazg + AVD) | iOS Simulator (`ashnazg mcp --ios-app`) |
| --- | --- | --- | --- |
| Primary isolation unit | Owned Docker container (`Binderfs` + Android 15) | Owned QEMU KVM emulator + AVD home | Owned CoreSimulator device (`simctl`) + XCTest runner |
| Accessibility transport | UiAutomation / in-guest Instrumentation (`agent-android`) | UiAutomation / in-guest Instrumentation (`agent-android`) | Batched `XCUIApplication.snapshot()` over token-guarded loopback TCP (`McpBridgeTests.swift`) |
| Cold boot / ready time | `18.74 s` (fixture benchmark) | `69.26 s` (fixture benchmark) | `~23.1 s` cold (`arm_start + first_usable_snapshot`, Meer) |
| Idle / peak runtime RAM | `1.35 GiB` cold idle cgroup | `6.28 GiB` cold idle cgroup | `33.6 MiB` `launchd_sim` + `329.0 MiB` app + `71.8 MiB` runner (`ri_phys_footprint`) |
| Per-action MCP tax vs native | `771 ms` tap + fresh PNG (`29.7 ms` frame delta) | `1,471 ms` tap + fresh PNG (`413.9 ms` frame delta) | `+202–467 ms` per UI navigation step vs direct XCTest; `+7.9–8.2 s` total E2E overhead |
| Key open limitation | Requires Linux KVM/Binderfs (or macOS experimental VM `ashnazg-vm`) | `~5–6.5 GiB` RAM pressure under cgroup limits | Cold/unloaded `EXDevLauncher` (`reactHost == nil`); `Meer-Dev` aborts on `iOS 27.0` (`iOS 26.5` `--navigation deep-link` passes) |
