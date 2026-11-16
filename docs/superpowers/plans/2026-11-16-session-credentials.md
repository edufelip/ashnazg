# Native Session Credentials Implementation Plan

> **For agentic workers:** Use `subagent-driven-development`, then independent review before publishing.

**Goal:** Read the helper's private credential file and bind its values to the session identity observed by the process owner.

**Architecture:** A synchronous, bounded, descriptor-based reader accepts expectations supplied by the future helper supervisor. It returns an opaque credential handle that can create the existing RPC and native-install clients. It does not discover or adopt a helper, prove that a PID is alive, create credentials, or modify the state directory.

**Tech Stack:** Existing Rust Unix descriptor/filesystem APIs, serde, zeroize and cancellation token. No new dependency.

## Files and API

Create `src/session_credentials.rs` with inline tests; export from `src/lib.rs`. Only if useful, change `canonical_uuid` and `valid_hex` in `src/native_install.rs` to crate-visible helpers so validation rules have one implementation. No CLI/MCP/HostConfig, helper, guest, catalog, release or workflow changes.

```rust
pub struct SessionExpectations {
    pub session_id: String,
    pub helper_pid: u32,
    pub manifest_source_sha: String,
    pub agent_apk_sha256: String,
    pub agent_addr: std::net::SocketAddr,
    pub install_addr: std::net::SocketAddr,
}
pub struct SessionCredentials { /* private validated metadata and zeroized secrets */ }
pub fn read_private_credentials(
    state_dir: &std::path::Path,
    expected: &SessionExpectations,
    deadline: std::time::Instant,
    cancel: &tokio_util::sync::CancellationToken,
) -> anyhow::Result<SessionCredentials>;
```

Expose public metadata getters and `guest_client() -> crate::GuestClient` plus `install_client() -> anyhow::Result<crate::native_install::NativeInstallClient>`. Keep tokens private; the factories make the required owned copies. Debug must redact both tokens. Do not implement Serialize for the returned credentials. Stored secrets and raw credential bytes must zeroize on drop. Wrap deserialized token fields in a secret-owning Deserialize type so partial parse failures also drop secret wrappers. Existing GuestClient token storage is unchanged; do not claim this task zeroizes its internals.

The future supervisor obtains the session UUID from its own child's `agent_ready` stdout receipt, the PID from its retained Child handle, and source/APK/ports from verified artifacts and launch arguments. Comparing those values here does not independently authenticate a process or establish liveness. Missing credentials are an error after that readiness event; no polling, fallback to environment tokens or foreign-session adoption.

## Task 1: Strict schema and identity

- [ ] Add a failing test for a private owned directory and matching schema-1 credential file, using the actual Swift `AgentSessionCredentials` fields. Confirm the module/API is absent before implementation.
- [ ] Parse exactly the ten fields: schema_version, session_id, helper_pid, manifest_source_sha, agent_apk_sha256, rpc_token, install_token, agent_listen and install_listen. Reject duplicates, escaped duplicate names, unknown/missing fields and incorrect JSON types. The schema is unsigned integer 1; PID must be 2 through i32::MAX. UUID is canonical lowercase; source/hash fields are exact lowercase hex lengths 40/64. Both tokens are 64 lowercase hex characters and distinct.
- [ ] Validate expectations before filesystem access. Require IPv4 127.0.0.1 endpoints on distinct ports from 1024 through 65535. Require every public credential identity field to equal its corresponding expectation, including session ID, child PID, source, APK hash and exact canonical endpoint strings. Never infer an expected value from the credential file itself.
- [ ] Test valid factories and each identity mismatch, malformed types, schema, hashes, tokens, equal tokens, unknown/missing fields and duplicate keys. Errors must remain generic and never contain reflected fields, tokens, file content or user paths. Include sentinel checks for Display/Debug and malformed input errors.

## Task 2: Read-only filesystem boundary

- [ ] Require an absolute canonical path with at most 4096 path bytes and 256 components. Reject traversal and noncanonical lexical forms before normalization. Walk from an opened root directory with descriptor-relative O_DIRECTORY/O_NOFOLLOW/O_NONBLOCK/O_CLOEXEC opens; reject every symlink component. Do not call canonicalize first and erase evidence of a custom symlink or `..`. Callers on macOS must supply canonical `/private/tmp` or `/private/var` paths rather than system aliases.
- [ ] Final state directory must be current-user-owned, regular directory mode 0700. Retain its descriptor, its parent descriptor and final name so observed directory replacement is refused. Ancestors must be real directories but need not be private/current-user-owned.
- [ ] Open only fixed `.ashnazg-session-v1.json` relative to that held directory with O_RDONLY/O_NONBLOCK/O_NOFOLLOW/O_CLOEXEC. Validate the opened descriptor as a current-user-owned regular mode-0600 file, 1 through 4096 bytes, before reading. Reject FIFO/device/directory and symlink without waiting for another process. Read at most 4097 bytes and reject overflow, empty/truncated JSON and trailing non-whitespace data.
- [ ] Compare opened and named file identities before and after reading, including size/mode/UID and modification/change metadata. Revalidate state directory identity/UID/mode against its held descriptor and parent name. Preserve observed replacements; never unlink, chmod, create or repair anything. Directory mtime changes caused by unrelated runtime receipt writes need not invalidate the read.
- [ ] Check the same cancellation token and absolute deadline before traversal, between bounded reads and before success. These checks cannot preempt a blocking OS filesystem call; async session owners must join their blocking reader before cleanup. Hostile same-UID mutation remains outside the existing helper confidentiality boundary; do not claim atomic race protection beyond descriptor access and observed replacement checks.
- [ ] Add tests for direct/intermediate symlinks, `alias/../real/state`, relative/traversal paths, excessive path bounds, insecure directory/file modes, wrong owner where portable, FIFO with no writer, oversized/empty/trailing data and observed file/directory replacement. Use bounded deterministic hooks for replacement tests, with hooks unavailable outside cfg(test). Assert refusal leaves caller data unchanged and missing files are not created.

## Task 3: Verification and review

- [ ] Test an already-cancelled token and expired deadline before touching a nonexistent path, and cancellation/replacement at deterministic read checkpoints. Reader success must not outlive cancellation/deadline checks. No sleep-based races or unbounded threads.
- [ ] Run `cargo fmt --all -- --check`, `cargo test --locked --offline`, `cargo clippy --all-targets --locked --offline -- -D warnings`, and `git diff --check`. Commit only owned files and report the exact commit.
- [ ] Root verifies spec coverage and requests independent review. No process launch, device/VM/ADB, workflow, network acquisition, signing, push or publication in this task. Fixtures establish parsing/filesystem behavior only; real helper process supervision, liveness, shutdown and MCP integration remain separate implementation and acceptance work.
