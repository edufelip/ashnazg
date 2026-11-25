# Native MCP CLI bootstrap

The CLI selects the external Android backend whenever `ASHNAZG_TOKEN` is present. Empty or non-Unicode token values fail external configuration validation; they do not select another backend. The existing agent address, ADB, serial and HTTP bearer settings retain their external behavior. The `rpc`, `install` and `up` commands keep their existing responsibilities.

Without `ASHNAZG_TOKEN`, MCP selects the native backend on macOS with Apple Silicon. Other platforms report that an external token or a supported native platform is required. The native MCP path uses guest-side APK installation and does not invoke host ADB or aapt. The standalone `install` command remains the external ADB command.

## Startup and readiness

MCP transport initialization and `tools/list` do not wait for artifact acquisition or VM startup. One shared initializer acquires a catalog-bound runtime, launches the helper and authenticates readiness. All five tools use that shared state. A tool can wait up to thirty seconds for readiness; a timeout reports that startup is still in progress and can be retried. Cancelling that request does not cancel initialization. Failed initialization remains a failed state for that server lifetime, with details on stderr and a fixed error at the MCP boundary.

Native acquisition uses the fixed runtime `0.3.2` contract and the host binary's validated build-source stamp (prior [public v0.3.1 release](https://github.com/edufelip/ashnazg/releases/tag/v0.3.1) matches source `509a74a329529f764f8fef955f45c04eed3e02cc`). Use its matching released host or build that exact tag with its source stamp; another source tree needs its own matching catalog. An unstamped development build can still serve the external backend. A missing matching catalog or asset is an initialization failure. Cargo package and runtime contract both use `0.3.2`; installation is described in [releases](releases.md).

Default cache and state paths derive from the current user's existing home directory:

- `Library/Caches/ashnazg/runtime` holds verified artifacts.
- `Library/Application Support/ashnazg/native-mcp/<source-sha>` holds source-scoped persistent VM state.

Acquisition has a fifteen-minute deadline. Helper startup has a separate five-minute deadline beginning after acquisition. Existing private-path, cache, credential and boot-asset checks still apply. The native relays use loopback ports 15555, 7777 and 17778; an occupied port fails rather than adopting another session. `up --assets`, `up --vm-helper` and `ASHNAZG_MICROVM_ASSETS` do not override the catalog-bound native MCP input set.

## Transports and shutdown

Native stdio uses the [owned stdio bridge](stdio-bridge.md). Protocol bytes go to stdout and diagnostics go to stderr. Pipes, local stream sockets and terminal descriptors are supported; regular files and unsupported descriptors are rejected before initialization. Terminal line discipline remains active, with no prompt. Cancellation can stop the isolated bridge even when terminal input waits for Enter or output is stopped by flow control. There is no Tokio blocking-stdin fallback on this native path.

Native HTTP requires its independent `ASHNAZG_HTTP_TOKEN` and a loopback listener. Bearer validation and binding happen before initialization. The HTTP token is not the guest credential and must not be printed in diagnostics.

EOF, transport failure, SIGINT or SIGTERM cancels the server lifecycle. The owner awaits in-progress initialization cleanup, closes native install admission, drains tracked work and joins helper shutdown. The CLI also joins bridge and signal-task cleanup. Persistent state and verified cache entries are retained; incomplete acquisition staging follows the existing owned-cache cleanup rules.

## Evidence boundary

Host fixtures cover descriptor isolation and cancellation, source stamping, catalog-to-launch identity, shared readiness and CLI protocol behavior. Separate [packaged CLI/VM acceptance](evidence/runtime-release-0.3.1/README.md) passed all three shutdown modes. The subsequent [public-release acceptance](evidence/public-runtime-0.3.1/README.md) verified all ten anonymous asset bodies, automatic acquisition into an empty HOME, and a warm restart with outbound IP denied for the CLI subtree. These observations do not establish a startup distribution, performance savings, every macOS configuration, or whole-guest network isolation. The helper is ad-hoc signed rather than Developer ID signed/notarized.

## Release qualification

`scripts/native_cli_acceptance.py` verifies the immutable candidate directory before testing its packaged executable and fixture. It checks that their hashes belong to that source-bound candidate, freezes all five tool input contracts, installs/launches the fixture by inferred package, verifies an exact counter increment and decoded screenshot pixel change, and requires owned helper/bridge, credential and listener cleanup.

Run separate empty evidence directories for `--mode eof`, `--mode sigterm`, and `--mode sigterm-inflight`, reusing an explicitly owned private `--home` when testing a restart. In-flight qualification submits the pinned agent's 800 ms long press, observes the submitted call without a response for 200 ms, then signals the CLI and rejects a successful late response. This is a client-observed pending call; no server-entry timestamp is claimed.

```sh
python3 scripts/native_cli_acceptance.py --candidate-dir "$CANDIDATE" \
  --binary "$EXTRACTED_MAC_PACKAGE/bin/ashnazg" \
  --fixture-apk "$CANDIDATE/ashnazg-fixture-v0.3.2.apk" \
  --source-sha "$SOURCE_SHA" --home "$ACCEPTANCE_HOME" \
  --output-dir "$EVIDENCE_DIRECTORY" --mode eof
```

Prepublication qualification uses an explicitly staged verified cache as described in [runtime acquisition](runtime-acquisition.md). Public cold acquisition later requires a fresh home without staging. Fake-process tests qualify the driver only; actual VM and public acquisition receipts are separate evidence.
