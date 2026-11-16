# Native Session Supervisor Implementation Plan

> **For agentic workers:** Use `subagent-driven-development`, then independent review before publishing.

**Goal:** Own an explicit native helper session from process launch through authenticated readiness and orderly shutdown.

**Architecture:** One blocking worker owns the child process and both nonblocking output pipes for the entire lifetime. A bounded readiness channel transfers validated RPC/install clients to an async session handle; dropping that handle requests shutdown rather than dropping child ownership. A separate completion signal reports terminal state after cleanup.

**Tech Stack:** Existing Rust standard process/Unix descriptors, Tokio blocking tasks/channels, serde, SHA-256, zeroize and cancellation token. No new dependency or Tokio process feature is required.

## Files and scope

Create `src/native_session.rs` with inline fixture tests and export from `src/lib.rs`. The credential reader is reviewed at `6699e03`. If needed, expose its pure `canonical_components` validator crate-locally; do not change its credential contract. No CLI, MCP, HostConfig, guest, Swift, release, acquisition or workflow changes. The new library receives explicit local paths and expected artifact identities from its caller; the later CLI will obtain those from verified acquisition. It must not silently derive trusted hashes from arbitrary files.

```rust
pub struct NativeSessionLaunch {
    pub helper: PathBuf,
    pub helper_sha256: String,
    pub assets: PathBuf,
    pub state_dir: PathBuf,
    pub agent_apk: PathBuf,
    pub agent_apk_sha256: String,
    pub source_sha: String,
    pub adb_port: u16,
    pub agent_port: u16,
    pub install_port: u16,
    pub startup_timeout_secs: u16,
}

pub struct NativeSession { /* private worker handle, clients, public identity, cancellation */ }
impl NativeSession {
    pub async fn start(launch: NativeSessionLaunch, deadline: std::time::Instant,
        cancel: CancellationToken) -> anyhow::Result<Self>;
    pub fn helper_pid(&self) -> u32;
    pub fn session_id(&self) -> &str;
    pub fn guest_client(&self) -> anyhow::Result<crate::GuestClient>;
    pub fn install_client(&self) -> anyhow::Result<std::sync::Arc<crate::native_install::NativeInstallClient>>;
    pub async fn wait_terminated(&self) -> anyhow::Result<SessionExit>;
    pub async fn shutdown(self) -> anyhow::Result<SessionExit>;
}
```

`SessionExit` contains only non-secret status: child exit code/signal where available, fixed reason enum, whether force was required, and whether the credential was absent after the child stopped. Derive Clone/Debug only for public metadata. Never expose raw helper output or credentials in errors, receipts or Debug. Keep the existing `GuestClient` secret-storage limitation explicit.

## Task 1: Launch and readiness contract

- [ ] Start with a failing fixture test for public API absence. Implement process fixtures using only newly created test-owned executable scripts/programs and temporary files. Do not launch the actual VM helper in this task. Tests must run on Linux and macOS; a private cfg(test) launch path may bypass the production platform gate, never a production environment switch.
- [ ] Public startup supports macOS/aarch64 only. Validate before spawn: source 40 lowercase hex, helper/APK hashes 64 lowercase hex, pairwise distinct ports 1024..65535, helper timeout 15..1800 seconds, uncancelled/unexpired caller budget, canonical absolute bounded paths with no raw traversal or symlink normalization. The state parent must already exist as a private owned directory; the state itself may be missing so the helper can create it. Reject a pre-existing credential entry rather than adopting it.
- [ ] Treat helper and agent inputs as caller-verified local artifacts. Rehash the opened regular helper before execution against the supplied digest, bounded to 64 MiB and chunks at most 64 KiB with cancellation/deadline checks and observed identity-change refusal. Require current-user ownership, owner execution permission and no group/other write bits. Require its parent directory private/owned; reject symlink components. Protect the path chain against writable foreign replacement: ancestors must be owned by current UID or root and not group/other writable, except root-owned sticky directories. The private cache and canonical private temporary test roots satisfy this policy. Do not reopen a different helper silently after hashing.
- [ ] Agent APK must be a no-follow regular file, 1..128 MiB, supplied under a private owned directory. The Swift helper verifies its bytes against the supplied APK hash; do not add a second full APK scan here. Assets must be an existing private owned canonical directory; the helper performs full boot-asset verification. Do not weaken helper validation or invent download URLs.
- [ ] Spawn exactly the given helper with separate fixed arguments: --assets, --state-dir, --adb-port, --agent-port, --install-port, --timeout, --agent-apk and --agent-sha256. No shell, token arguments, host ADB or inherited token-based fallback. Remove ASHNAZG_TOKEN/ASHNAZG_AGENT_ADDR from the child's environment. Stdin is null; stdout/stderr are private pipes, never inherited by MCP stdio. Take cleanup ownership immediately after successful spawn, before any fallible pipe setup.
- [ ] Make both pipe descriptors nonblocking. Pump bounded chunks from both pipes fairly while polling child status and cancellation at most every 50 ms. Stdout lines have an 8 KiB cap; stdout and stderr each have a 64 KiB lifetime budget. Enforce bounds before allocation growth; never read an unbounded line or retain/log raw stderr. A stream error, oversized output, malformed or duplicate-key receipt, EOF before readiness, helper-reported failure or early child exit fails startup and begins owned cleanup.
- [ ] Parse the actual Swift RuntimeReceipt schema. Require one complete newline-terminated agent_ready receipt: schema 1, architecture aarch64, manifest version 0.3.0, matching source/APK hash, canonical UUID and exact requested loopback endpoints. Deny unknown/duplicate fields with typed receipt/timing structures. Never infer trusted expected source/ports/hash from this receipt. Status/error text is not reflected. Later duplicate readiness or conflicting identity is a lifecycle failure.
- [ ] After the child's readiness receipt, use `read_private_credentials` with UUID from that receipt, PID from the retained Child and identity/ports from launch. Require child still alive before and after credential validation. No foreign credential polling/adoption. Create one GuestClient, authenticate a ping within five seconds and the remaining startup budget, and require integer apiLevel >=34. Reuse that authenticated client in the returned session. A private Tokio runtime on the blocking worker may drive this bounded ping; pump/recheck pipe budgets and child state again before publishing readiness.
- [ ] Startup's absolute budget is min(caller deadline, entry + configured helper timeout), including helper hashing, spawn, readiness, file validation and ping. A deadline starts cleanup; startup returns failure only after owned cleanup completes. Cleanup has its own finite budget rather than pretending it is part of a completed readiness result.

## Task 2: Lifetime and cleanup

- [ ] The worker retains the only Child/process/pipes ownership. The async session handle owns a worker child cancellation token and join/completion handle; it never manipulates raw PID names or filesystem entries. Factories refuse a known terminated session. The worker monitors child exit, pipe faults and caller cancellation throughout the session; `wait_terminated` gives the later MCP loop a terminal signal. Passing readiness does not promise the process cannot exit immediately afterward.
- [ ] Caller cancellation, dropped startup future, dropped session, explicit shutdown or startup failure all request shutdown of only this child. A drop guard must be disarmed/transferred when startup successfully returns a live session; do not cancel the worker accidentally on successful return. Parent cancellation must not be set by dropping a child session.
- [ ] Attempt SIGTERM on the owned, unreaped child, continue bounded pipe draining, and wait for it to exit/reap. Normal helper cleanup includes sequential relay drains and guest shutdown: use a 220-second production grace to cover the existing Swift bounds, then SIGKILL and a further five-second reap budget. Test-only policy values may shorten this deterministically. Do not kill by process name, scan devices, remove forwards or touch a foreign PID taken from the credential file.
- [ ] Normal requested shutdown succeeds only for child exit zero, no forced kill, and an absent credential entry. Nonzero/unexpected exit, forced stop, failed reap or a leftover credential is reported as failure with fixed non-secret status. Preserve the persistent disk, logs and state. Do not delete or repair a leftover/replaced credential: forced-shutdown recovery is not qualified by this first supervisor. Never claim orderly cleanup after force.
- [ ] Join child ownership and pipe cleanup before normal API completion; no detached pipe reader may outlive the owner. On future drop, the worker continues to own and finish cleanup independently. Add an immediate child guard so early errors/unwind do not simply drop a live std::process::Child. OS calls cannot be preempted; do not falsely report a child reaped when its status is unknown. Avoid unbounded wait(), blocking pipe reads or unchecked thread joins in the implementation.

## Task 3: Deterministic tests and delivery

- [ ] Parent-owned loopback RPC fixtures and newly created fake helper scripts may emulate credentials/readiness and signal traps. They must not launch VMs or operate devices. Verify fixed helper argv and token-free child environment, matching credential bindings and a real authenticated ping before readiness. All fixture accepts, channel waits and joins must have finite bounds; do not depend on the machine's SDK or helper installation.
- [ ] Cover success and explicit shutdown, missing/corrupt helper before spawn, occupied credential/state refusal, malformed/oversized/duplicate output, wrong PID/UUID/source/hash/ports, invalid or stalled ping, startup timeout/cancellation, child exit before/after readiness, stdout/stderr flooding, drop during startup and after readiness, graceful signal exit, ignored SIGTERM requiring forced failure, and leftover credentials preserved. Keep a separately owned sentinel process alive when a bad credential PID points to it; cleanup must affect only the spawned child.
- [ ] Verify a failed startup returns only after the fake child is reaped, normal shutdown removes only the fake helper's own credential, parent cancellation stays unchanged on child drop, and the persistent disk/sentinel caller data remains untouched. Test-only hooks/policies are private and unavailable in production.
- [ ] Run formatting, `cargo test --locked --offline`, Clippy on all targets with warnings denied, and whitespace checks. Commit only owned files. Report exact commit, actual test counts, deliberate limitations and any unmet plan item. No push, publication, real helper/VM/device/ADB, external network or workflow run in this task. Root will review and perform actual explicit-session acceptance before CLI/MCP integration.
