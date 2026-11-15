# v0.1 validation evidence

Local validation on 2026-11-15 uses the optimized Rust host, Android API 37 arm64 (16 KB pages), and a 1080×1920 existing emulator on macOS 26.5. It does not measure a custom runtime boot.

## Correctness

- Rust: formatting, Clippy with warnings denied, 14 tests and locked release build.
- Android agent: JVM tests for both debug/release variants, Android lint and APK build. There are 28 distinct JVM cases per variant.
- Harness: Python unit tests, direct CLI invocation, actionlint, and real-device acceptance.
- Actual Android acceptance: **28 RPC groups and 10 MCP groups passed**. Coverage includes every action, Unicode input, password redaction/clear/type, exact and ambiguous selectors, stale IDs, screenshot-mark selection, PNG/WebP, literal/time-filtered cross-app logs, auth, malformed/oversized frames, HTTP and stdio MCP, and APK install/launch.
- Actual Compose and React Native checks confirm test IDs, taps, exact Unicode type/clear, and screenshots. The RN APK uses real Hermes bytecode, New Architecture and generated `libappmodules.so` for arm64/x86_64; local execution verifies arm64. Hosted CI is configured for API 35 x86_64; its required PR checks provide the hosted execution evidence.
- Independent Luna reviews were completed; actionable findings were repaired and covered by regression checks. Runtime acceptance remained a separate requirement.

[Full local acceptance receipt](evidence/local-2026-11-15/integration.json).

## Performance

Ten samples per path, **100 successful samples and no errors**. Snapshot cases repeatedly inspect the unchanged native fixture; cache-warm inspection is distinct from an action with state stabilization. Setup phases, raw samples, host load, device settings and binary hash are retained in the [unaltered benchmark receipt](evidence/local-2026-11-15/benchmark.json).

| Path | Sample p50 (ms) | Sample p95 (ms) |
| --- | ---: | ---: |
| ADB XML dump + pull | 1970.50 | 2134.73 |
| Persistent guest snapshot | 2.52 | 4.48 |
| MCP stdio snapshot | 1.29 | 1.71 |
| ADB PNG capture | 94.26 | 118.55 |
| MCP PNG capture | 45.52 | 47.36 |
| MCP WebP capture | 63.45 | 73.41 |
| Settled tap via guest | 369.87 | 399.35 |
| Settled tap via MCP | 369.75 | 414.11 |

The initial proposal's 15–25 ms screenshot and approximately 25 ms settled-action figures are **not achieved** by this implementation on this device. Public bitmap capture/encoding includes copies; actions deliberately include target stability, input and a bounded 120 ms accessibility quiet period. PNG was faster than WebP on this flat fixture; other screens can differ.

These are sequential local samples on a shared workstation, not a universal speedup guarantee. The raw receipts truthfully mark the source worktree as uncommitted at measurement time and retain the release binary SHA-256. Hosted CI checks the explicit PR head and publishes a separate receipt; local results are not hosted-CI evidence.

## Visual evidence

The following files are unchanged copies from the successful acceptance run, with no production/user data:

- [Native fixture with numbered marks](evidence/local-2026-11-15/fixture-overlay.png)
- [Compose fixture](evidence/local-2026-11-15/compose.png)
- [React Native fixture](evidence/local-2026-11-15/react-native.png)

## GitHub quality gate

The repository is public and MIT licensed. `main` requires the **Quality gate**, a current branch, and a pull request; enforcement includes admins, and force pushes/deletion are blocked. The [workflow](../.github/workflows/quality.yml) requires Rust Linux/macOS checks, Android unit/lint/build, and actual emulator integration plus benchmarks. It uploads run-scoped images and sanitized failure diagnostics. Consult the PR's required checks for current hosted execution status.

## Scope limits

v0.1 controls an existing Android API 34+ device. Container/microVM boot, a custom VSync governor, zero-copy capture and iOS remain later milestones. Compose tags require application opt-in. UiAutomation has one owner. A quiet accessibility stream does not establish network idleness, and an ambiguous mutation outcome must not be retried automatically. See the [API guide](api.md).
