# Native MCP CLI bootstrap

The CLI selects the external Android backend whenever `ASHNAZG_TOKEN` is present. Empty or non-Unicode token values fail external configuration validation; they do not select another backend. The existing agent address, ADB, serial and HTTP bearer settings retain their external behavior. The `rpc`, `install` and `up` commands keep their existing responsibilities.

Without `ASHNAZG_TOKEN`, MCP selects the native backend on macOS with Apple Silicon. Other platforms report that an external token or a supported native platform is required. The native MCP path uses guest-side APK installation and does not invoke host ADB or aapt. The standalone `install` command remains the external ADB command.

## Startup and readiness

MCP transport initialization and `tools/list` do not wait for artifact acquisition or VM startup. One shared initializer acquires a catalog-bound runtime, launches the helper and authenticates readiness. All five tools use that shared state. A tool can wait up to thirty seconds for readiness; a timeout reports that startup is still in progress and can be retried. Cancelling that request does not cancel initialization. Failed initialization remains a failed state for that server lifetime, with details on stderr and a fixed error at the MCP boundary.

Native acquisition uses the fixed runtime `0.3.0` contract and the host binary's validated build-source stamp. An ordinary unstamped development build can still serve the external backend; native tool startup reports its unavailable source identity. A missing matching catalog or asset is an initialization failure. No matching public runtime catalog is established by this change. Cargo package and runtime contract now both use `0.3.0`; candidate preparation and publication are described in [releases](releases.md).

Default cache and state paths derive from the current user's existing home directory:

- `Library/Caches/ashnazg/runtime` holds verified artifacts.
- `Library/Application Support/ashnazg/native-mcp/<source-sha>` holds source-scoped persistent VM state.

Acquisition has a fifteen-minute deadline. Helper startup has a separate five-minute deadline beginning after acquisition. Existing private-path, cache, credential and boot-asset checks still apply. The native relays use loopback ports 15555, 7777 and 17778; an occupied port fails rather than adopting another session. `up --assets`, `up --vm-helper` and `ASHNAZG_MICROVM_ASSETS` do not override the catalog-bound native MCP input set.

## Transports and shutdown

Native stdio uses the [owned stdio bridge](stdio-bridge.md). Protocol bytes go to stdout and diagnostics go to stderr. Pipes, local stream sockets and terminal descriptors are supported; regular files and unsupported descriptors are rejected before initialization. Terminal line discipline remains active, with no prompt. Cancellation can stop the isolated bridge even when terminal input waits for Enter or output is stopped by flow control. There is no Tokio blocking-stdin fallback on this native path.

Native HTTP requires its independent `ASHNAZG_HTTP_TOKEN` and a loopback listener. Bearer validation and binding happen before initialization. The HTTP token is not the guest credential and must not be printed in diagnostics.

EOF, transport failure, SIGINT or SIGTERM cancels the server lifecycle. The owner awaits in-progress initialization cleanup, closes native install admission, drains tracked work and joins helper shutdown. The CLI also joins bridge and signal-task cleanup. Persistent state and verified cache entries are retained; incomplete acquisition staging follows the existing owned-cache cleanup rules.

## Evidence boundary

This implementation slice has separate host fixtures for descriptor isolation and cancellation, source stamping, catalog-to-launch identity, shared readiness and actual CLI protocol behavior. A passing host fixture does not establish a real VM launch, live GitHub acquisition, a clean-machine distribution, or performance savings. Existing explicit native HTTP acceptance used an already-prepared owned runtime and does not qualify automatic CLI acquisition. Source-matching release assets and separate owned CLI/VM acceptance remain necessary for those claims.
