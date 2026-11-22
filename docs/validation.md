# v0.1 validation evidence

Local validation on 2026-11-15 uses the optimized Rust host, Android API 37 arm64 (16 KB pages), and a 1080×1920 existing emulator on macOS 26.5. It does not measure a custom runtime boot.

## Correctness

- Rust: formatting, Clippy with warnings denied, 14 tests and locked release build.
- Android agent: JVM tests for both debug/release variants, Android lint and APK build. There are 47 distinct JVM cases per variant.
- Harness: Python unit tests, direct CLI invocation, actionlint, and real-device acceptance.
- Actual Android acceptance: **28 RPC groups and 10 MCP groups passed**. Coverage includes every action, Unicode input, password redaction/clear/type, exact and ambiguous selectors, stale IDs, screenshot-mark selection, PNG/WebP, literal/time-filtered cross-app logs, auth, malformed/oversized frames, HTTP and stdio MCP, and APK install/launch.
- Actual Compose and React Native checks confirm test IDs, taps, exact Unicode type/clear, and screenshots. The RN APK uses real Hermes bytecode, New Architecture and generated `libappmodules.so` for arm64/x86_64; local execution verifies arm64. Hosted CI is configured for API 35 x86_64; the final PR and merged-main emulator run details below document hosted execution.
- Independent Luna reviews were completed; actionable findings were repaired and covered by regression checks. Runtime acceptance remained a separate requirement.

[Full local acceptance receipt](evidence/local-2026-11-15-window-guard/integration.json).

A follow-up regression pass applied the emulator’s Pixel 6 cutout profile on the local API 37 device. Three fresh launches of each real framework passed the same one-tap counter, Unicode type/clear and screenshot checks ([24 check groups](evidence/local-2026-11-15-window-guard/cutout-acceptance.json)). This covers the fixture inset correction and the pre-input window geometry guard; it is separate from hosted API 35 execution.

A subsequent local fault injection replaced the native fixture Activity during pointer preparation by enabling the Pixel 6 overlay. The original implementation failed with a stale node and count zero; semantic reacquisition reached count one. Numeric/snapshot admission, package/identity changes, deadline expiry and the single-reacquisition limit are covered by JVM tests. The full local acceptance and benchmark receipts below predate the additional recovery and navigation changes. They remain historical local results; the final PR and merged-main hosted runs below provide validation evidence for their respective source revisions.

## Performance

The [paired Android MCP session benchmark](agent-session-benchmark.md) measures
launch to the first usable snapshot, a state-verified tap and PNG, and shutdown.
It samples both runtime cgroups from the same host.

The following are historical local measurements: ten samples per path, **100 successful samples and no errors**. Snapshot cases repeatedly inspect the unchanged native fixture; cache-warm inspection is distinct from an action with state stabilization. Setup phases, raw samples, host load, device settings and binary hash are retained in the [unaltered local benchmark receipt](evidence/local-2026-11-15-window-guard/benchmark.json). These values are separate from the final hosted run below.

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

These are sequential local samples on a shared workstation, not a universal speedup guarantee. The raw local receipts truthfully mark the source worktree as uncommitted at measurement time and retain the release binary SHA-256. Local results are not hosted-CI evidence.

### Final merged-main hosted run

The final [merged-main Quality run 43732725299](https://github.com/edufelip/ashnazg/actions/runs/43732725299) completed successfully on 2026-11-15 at merge SHA `001b066721933c6f1ffe05a634888330351d6095`. All five jobs passed: Android Gradle/framework APKs, Rust on Ubuntu, Rust on macOS, Android emulator integration/benchmark, and the Quality gate. The integration receipt and benchmark receipt both record this exact GitHub SHA as `head` and `github_sha`; `dirty` is `false`.

The [unaltered hosted integration receipt](evidence/hosted-43732725299-integration.json) records **28 RPC checks**, **10 MCP checks**, framework fixtures enabled, and no diagnostic evidence. The [unaltered hosted benchmark receipt](evidence/hosted-43732725299-benchmark.json) records ten successful samples for each of ten paths (**100 samples total**) and zero errors. Hosted p50/p95 values (ms) are:

| Path | Sample p50 (ms) | Sample p95 (ms) |
| --- | ---: | ---: |
| ADB XML dump + pull | 1957.56 | 2215.64 |
| Persistent guest snapshot | 8.20 | 18.13 |
| MCP stdio snapshot | 3.53 | 7.54 |
| ADB PNG capture | 203.08 | 229.89 |
| Persistent guest PNG capture | 159.92 | 214.62 |
| Persistent guest WebP capture | 148.66 | 157.80 |
| MCP PNG capture | 128.68 | 171.81 |
| MCP WebP capture | 148.67 | 152.73 |
| Settled tap via guest | 380.18 | 828.85 |
| Settled tap via MCP | 372.99 | 391.74 |

The hosted environment was Linux x86_64 with Android API 35 x86_64 at 1080×2400; its recorded host binary SHA-256 is in the raw benchmark receipt. The run's integration ID is `20261115T160420.684955Z-6702`. Five unaltered images from that run are retained under [hosted integration images](evidence/hosted-43732725299/integration/20261115T160420.684955Z-6702/): Compose, React Native, numbered fixture overlay, MCP screenshot, and WebP fixture output.

The preceding [final PR Quality run 43726910650](https://github.com/edufelip/ashnazg/actions/runs/43726910650) also passed all five jobs on PR source SHA `6525c54863c2497149269c79fb9cbacd1068350d`. That run validates the PR source; merged-main run 43732725299 is the hosted evidence for the accepted merge SHA. The PR run's success does not change the source SHA recorded in the retained main-run receipts.

## Visual evidence

The following files are unchanged copies from the original successful acceptance run, with no production/user data:

- [Native fixture with numbered marks](evidence/local-2026-11-15/fixture-overlay.png)
- [Compose fixture](evidence/local-2026-11-15/compose.png)
- [React Native fixture](evidence/local-2026-11-15/react-native.png)

The [updated Compose cutout screenshot](evidence/local-2026-11-15-window-guard/compose-cutout.png) records the safe-drawing layout after a successful single tap under the local Pixel 6 cutout profile.

## GitHub quality gate

The repository is public and MIT licensed. `main` requires the **Quality gate**, a current branch, and a pull request; enforcement includes admins, and force pushes/deletion are blocked. The [workflow](../.github/workflows/quality.yml) requires Rust Linux/macOS checks, Android unit/lint/build, and actual emulator integration plus benchmarks. It uploads run-scoped images and sanitized failure diagnostics. The run-specific links and receipts above document the PR and merged-main outcomes for this closeout.

## Scope limits

v0.1 controls an existing Android API 34+ device. Container/microVM boot, a custom VSync governor, zero-copy capture and iOS remain later milestones. Compose tags require application opt-in. UiAutomation has one owner. A quiet accessibility stream does not establish network idleness, and an ambiguous mutation outcome must not be retried automatically. See the [API guide](api.md).
