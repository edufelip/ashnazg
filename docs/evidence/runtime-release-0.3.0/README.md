# Packaged CLI and VM acceptance

The unchanged candidate from Release prepare [43914898031, attempt 2](https://github.com/edufelip/ashnazg/actions/runs/43914898031) passed all three prepublication modes on Apple Silicon macOS: EOF, idle SIGTERM, and SIGTERM during a client-observed pending long press. Its source is `50434b8a056514fafd9fcb63cc534ce369880184`, whose [main Quality run passed](https://github.com/edufelip/ashnazg/actions/runs/43912871419). The artifact, catalog, and candidate hashes are bound in [receipt.json](receipt.json).

Each run used the packaged `bin/ashnazg mcp` with a private HOME and an explicitly staged, catalog-verified cache. The driver supplied no host SDK tools, external Android serial, or external agent token. All five tools passed, including inferred fixture installation, `Count: 0` to `Count: 1`, fresh snapshot IDs, and changed normalized screenshot pixels. The screenshots were also inspected visually.

All modes exited with status zero, joined the CLI, observed the helper and bridge gone, removed credentials, closed all three listeners, and preserved captured persistent state/cache identities. `runtime.json` and `serial.log` are rewritten diagnostics; disk contents may change. The in-flight run submitted a long press, observed no response for 200 ms, sent SIGTERM with stdin open, and observed no successful reply. It does not assert a server-entry timestamp.

CLI-observed readiness was 40.449, 49.887, and 43.534 seconds. Helper startup intervals were 35.508, 44.909, and 37.367 seconds. These are different intervals from three warm restarts after an initial diagnostic boot, not a performance distribution or cold-acquisition result.

The executed driver and wrapper are retained. Driver corrections address a busy-host process inventory and atomic runtime-receipt replacement; they do not change the packaged binaries. Each passing run scanned its retained evidence and runtime diagnostics against both actual session tokens held only in memory. No matching secret was found.

This acceptance does not establish public cold acquisition, network-denied warm reuse, Developer ID signing/notarization, intrinsic compiler-to-binary provenance, or legal clearance. Retained files cannot independently attest the external environment or actual secret values used by the scan. Those publication and acquisition steps remain separate. The helper's ad-hoc signature and virtualization entitlement were verified locally.
