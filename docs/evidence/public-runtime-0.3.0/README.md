# Public runtime distribution acceptance

[Ashnazg v0.3.0](https://github.com/edufelip/ashnazg/releases/tag/v0.3.0) was published by [run 43929683661](https://github.com/edufelip/ashnazg/actions/runs/43929683661). The public tag points to the accepted source `50434b8a056514fafd9fcb63cc534ce369880184`. All ten uploaded assets match the prepared candidate; an independent anonymous GET audit streamed every body once and verified all 726,340,394 bytes against the accepted sizes and SHA-256 values.

The public macOS ZIP was downloaded anonymously, checked against the accepted hash, and extracted. Its actual `bin/ashnazg mcp` ran with a different, initially empty private HOME, no manually staged runtime, an empty tool PATH, and no host SDK, external Android serial, or external agent-token variables. The native CLI acquired its catalog-bound runtime from the fixed public release URLs and started the VM. Both cache and state snapshots were empty before this run.

The cold run passed all five tools, inferred fixture installation, `Count: 0` to `Count: 1`, fresh snapshot IDs and changed normalized screenshot pixels. EOF joined CLI/helper/bridge shutdown, removed credentials, closed all three listeners and retained the new persistent disk. The public-cache warm run passed the same checks and preserved that disk's identity.

The warm run launched only the CLI subtree through the retained `sandbox-exec` profile. Outbound IPv4 and IPv6 probes under that profile returned EPERM; fixed loopback relay ports remain allowed. The controller stays outside because macOS refuses execution of setuid `/bin/ps` inside the sandbox. The retained launch wrapper records exactly one profile application to the verified CLI command and checks the policy hash before and after the run. This qualifies host artifact-cache reuse with outbound IP denied; guest NAT may use external host services, so whole-guest network isolation is not claimed.

Driver-observed readiness was 90.640 seconds cold and 37.701 seconds warm. The helper's later-starting startup intervals were 60.346 and 30.571 seconds. These are two observations on the recorded macOS host, not a startup distribution or a performance improvement claim. A fresh HOME is not a factory-reset-machine test.

Each run checked retained evidence and runtime diagnostics against its two actual session tokens, held only in memory; no match was found. Retained files cannot independently attest the external environment or those secret values. The binaries remain ad-hoc signed without Developer ID/notarization; intrinsic compiler-to-binary provenance and legal clearance are not claimed.

The [receipt](receipt.json) binds all retained payloads. The [prepublication evidence](../runtime-release-0.3.0/README.md) separately covers EOF, idle SIGTERM and client-observed in-flight SIGTERM for these unchanged release bytes.
