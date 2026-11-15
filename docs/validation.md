# v0.1 validation evidence

Local validation on 2026-11-15 uses the optimized Rust host, Android API 37 arm64 (16 KB pages), and a 1080×1920 existing emulator on macOS 26.5. It does not measure a custom runtime boot.

## Correctness

- Rust: formatting, Clippy with warnings denied, 14 tests and locked release build.
- Android agent: JVM tests for both debug/release variants, Android lint and APK build. There are 47 distinct JVM cases per variant.
- Harness: Python unit tests, direct CLI invocation, actionlint, and real-device acceptance.
- Actual Android acceptance: **28 RPC groups and 10 MCP groups passed**. Coverage includes every action, Unicode input, password redaction/clear/type, exact and ambiguous selectors, stale IDs, screenshot-mark selection, PNG/WebP, literal/time-filtered cross-app logs, auth, malformed/oversized frames, HTTP and stdio MCP, and APK install/launch.
- Actual Compose and React Native checks confirm test IDs, taps, exact Unicode type/clear, and screenshots. The RN APK uses real Hermes bytecode, New Architecture and generated `libappmodules.so` for arm64/x86_64; local execution verifies arm64. Hosted CI is configured for API 35 x86_64; its required PR checks provide the hosted execution evidence.
- Independent Luna reviews were completed; actionable findings were repaired and covered by regression checks. Runtime acceptance remained a separate requirement.

[Full local acceptance receipt](evidence/local-2026-11-15-window-guard/integration.json).

A follow-up regression pass applied the emulator’s Pixel 6 cutout profile on the local API 37 device. Three fresh launches of each real framework passed the same one-tap counter, Unicode type/clear and screenshot checks ([24 check groups](evidence/local-2026-11-15-window-guard/cutout-acceptance.json)). This covers the fixture inset correction and the pre-input window geometry guard; it is separate from hosted API 35 execution.

A subsequent local fault injection replaced the native fixture Activity during pointer preparation by enabling the Pixel 6 overlay. The original implementation failed with a stale node and count zero; semantic reacquisition reached count one. Numeric/snapshot admission, package/identity changes, deadline expiry and the single-reacquisition limit are covered by JVM tests. The full acceptance and benchmark receipts below predate the additional recovery and navigation changes; the current PR head still requires hosted integration validation.

## Performance

Ten samples per path, **100 successful samples and no errors**. Snapshot cases repeatedly inspect the unchanged native fixture; cache-warm inspection is distinct from an action with state stabilization. Setup phases, raw samples, host load, device settings and binary hash are retained in the [unaltered benchmark receipt](evidence/local-2026-11-15-window-guard/benchmark.json).

| Path | Sample p50 (ms) | Sample p95 (ms) |
| --- | ---: | ---: |
| ADB XML dump + pull | 1953.70 | 3288.92 |
| Persistent guest snapshot | 2.51 | 2.99 |
| MCP stdio snapshot | 2.92 | 3.46 |
| ADB PNG capture | 239.92 | 390.93 |
| MCP PNG capture | 83.27 | 98.08 |
| MCP WebP capture | 141.43 | 151.41 |
| Settled tap via guest | 562.71 | 862.25 |
| Settled tap via MCP | 487.82 | 606.08 |

The initial proposal's 15–25 ms screenshot and approximately 25 ms settled-action figures are **not achieved** by this implementation on this device. Public bitmap capture/encoding includes copies; actions deliberately include target/window stability, input and a bounded 120 ms accessibility quiet period. New or changed windows require 500 ms of observed geometry stability before coordinate input; unchanged windows use 120 ms. PNG was faster than WebP on this flat fixture; other screens can differ.

These are sequential local samples on a shared workstation, not a universal speedup guarantee. The raw receipts truthfully mark the source worktree as uncommitted at measurement time and retain the release binary SHA-256. Hosted CI checks the explicit PR head and publishes a separate receipt; local results are not hosted-CI evidence.

## Visual evidence

The following files are unchanged copies from the original successful acceptance run, with no production/user data:

- [Native fixture with numbered marks](evidence/local-2026-11-15/fixture-overlay.png)
- [Compose fixture](evidence/local-2026-11-15/compose.png)
- [React Native fixture](evidence/local-2026-11-15/react-native.png)

The [updated Compose cutout screenshot](evidence/local-2026-11-15-window-guard/compose-cutout.png) records the safe-drawing layout after a successful single tap under the local Pixel 6 cutout profile.

## GitHub quality gate

The repository is public and MIT licensed. `main` requires the **Quality gate**, a current branch, and a pull request; enforcement includes admins, and force pushes/deletion are blocked. The [workflow](../.github/workflows/quality.yml) requires Rust Linux/macOS checks, Android unit/lint/build, and actual emulator integration plus benchmarks. It uploads run-scoped images and sanitized failure diagnostics. Consult the PR's required checks for current hosted execution status.

## Scope limits

v0.1 controls an existing Android API 34+ device. Container/microVM boot, a custom VSync governor, zero-copy capture and iOS remain later milestones. Compose tags require application opt-in. UiAutomation has one owner. A quiet accessibility stream does not establish network idleness, and an ambiguous mutation outcome must not be retried automatically. See the [API guide](api.md).
