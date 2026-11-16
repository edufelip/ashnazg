# Native Install Client Implementation Plan

> **For agentic workers:** Use `subagent-driven-development`, then independent review before publishing.

**Goal:** Implement the Rust client for the existing helper's authenticated APK installation relay, without a host ADB subprocess.

**Architecture:** An async library facade starts one blocking owner of an opened APK descriptor and TCP connection. The worker hashes and streams that same regular file, waits for the protocol ACK before sending APK bytes, and retains ownership through completion or cancellation. Existing Swift/Python session owners and wire protocol remain unchanged.

**Tech Stack:** Existing Rust standard filesystem/TCP, Tokio blocking-worker support, serde, SHA-256, zeroize and cancellation token. No new dependencies.

## Boundary

Create only `src/native_install.rs` with inline tests and export it from `src/lib.rs`. Do not modify CLI, `HostConfig`, MCP tools, helper, guest, Cargo dependencies, release versions or workflows. The later session supervisor owns credential-file validation and binds the relay/session to its spawned helper; this client receives already-selected session values and validates their wire shape. It does not discover credentials, adopt a helper or claim peer identity beyond that caller-owned session boundary.

```rust
pub struct NativeInstallClient { /* private endpoint, session id, secret */ }
pub struct NativeInstallResult { pub launched: bool, pub package: Option<String> }

impl NativeInstallClient {
    pub fn new(endpoint: std::net::SocketAddr, session_id: String, install_token: String)
        -> anyhow::Result<Self>;
    pub async fn install(&self, apk: std::path::PathBuf, launch_package: Option<String>,
        deadline: std::time::Instant, cancel: tokio_util::sync::CancellationToken)
        -> anyhow::Result<NativeInstallResult>;
}
```

Use a secret-owning representation with redacted Debug; never derive Debug/Serialize for credentials or include tokens, headers, raw guest replies or path-dependent error text in failures. A worker-owned copy of the secret must zeroize on drop. Reject non-loopback endpoints, port zero, invalid canonical lowercase UUID session IDs and tokens other than 64 lowercase hexadecimal characters. Restrict optional package names to the existing Android package grammar; do not execute package or path text. `None` means install without launch, `Some` means launch that validated package. Package inference from Android manifests remains the later MCP caller's responsibility.

## Task 1: Wire fixtures first

- [ ] Write a loopback fixture that accepts one client, reads the newline JSON header, checks that no payload arrives before it sends `ready_for_payload`, receives exact APK bytes then EOF, and sends the existing strict `installed` reply. Use the real `runtime/microvm/apk_stream.py`, `agent_session.py` and retained native acceptance driver as protocol references; do not invent new fields.
- [ ] Add a failing Rust test calling the missing public API. Run it and retain the expected missing-module/symbol failure.
- [ ] Implement only the client contract. Header fields are exactly schema_version=1, op=install, session_id, install_token, apk_size, apk_sha256, launch and optional package. ACK is exactly schema_version=1/status=ready_for_payload. Success is exactly schema_version=1/status=installed/session_id matching the request/launched matching the requested launch. Deny unknown/duplicate response fields and wrong JSON types, especially boolean-versus-integer confusion. Parse error replies generically; never echo their contents.
- [ ] Header and each response frame must fit 4 KiB including newline. Use bounded reads; refuse missing/truncated/oversized frames. No APK bytes precede ACK. Stream exact bytes, half-close the TCP write direction and wait for a complete success response and EOF; reject trailing response data. Do not retry or replay an installation after any bytes have been sent.
- [ ] Run the protocol test green and inspect the actual transmitted bytes and returned result. No real Android/device/VM operation is involved.

## Task 2: Descriptor ownership and bounded I/O

- [ ] Open the APK with no-follow, nonblocking and close-on-exec flags before validating by descriptor. Reject symlinks, FIFO/device/directory, empty files and files larger than 128 MiB without waiting for a writer. Retain one regular-file descriptor; compute SHA-256 in chunks at most 64 KiB and stream that same descriptor. Compare descriptor identity, size and modification metadata before/after hashing and streaming so observed in-place changes fail closed. Do not reopen the pathname after hashing. Replacing the pathname does not authorize reading the replacement.
- [ ] Use one absolute caller deadline and worker child cancellation token from entry through hashing, connect, upload and result. Header/ACK/body upload gets at most 30 seconds from entry; installation response gets at most 180 seconds from entry and never exceeds the caller deadline. Progress must not reset either budget. Reject invalid/expired input before connecting.
- [ ] Bound connect to at most the remaining budget and five seconds. Bound every connected socket wait to at most 50 ms and check cancellation between waits; retry short read/write timeouts on the same frame and preserve byte offsets. Handle interrupted and partial I/O correctly. No sleeping or busy loops on immediate EOF/errors, no unbounded sendall or byte-progress deadline extension. Connect cancellation may wait for that single bounded connect; document that limit.
- [ ] A single `spawn_blocking` worker owns all descriptors and cleanup; normal failure/cancellation awaits it before returning. If the caller drops its future, a drop guard cancels only the worker child token; the worker closes its own connection/file and exits at the next bounded checkpoint. Never clean up from another task while that worker still uses descriptors. File OS calls remain cooperatively bounded, not preemptible.
- [ ] Add meaningful tests for FIFO-with-no-writer, symlink, empty/oversized APKs, pathname replacement after hash/ACK, observed file mutation, fragmented ACK/response, stalled ACK, upload backpressure, EOF/trailing bytes and cancellation/drop. Synchronize fixtures with bounded channels/events rather than timing guesses. Include same-connection progress across short timeouts. Keep test secrets out of output.

## Task 3: Failure contract and verification

- [ ] Exercise invalid token/session/endpoint/package before connection, unauthorized/rejected ACK, missing ACK, unknown fields, duplicate fields, wrong status/session/launched flag and guest error text containing a sentinel secret. Every failure must remain bounded and omit that sentinel/token; installation is never reported successful after an error.
- [ ] Verify that future-drop cancellation does not cancel the caller's parent token, and that the fixture observes the owned connection close. Test retries do not open a second install connection after a partial request.
- [ ] Run `cargo fmt --all -- --check`, `cargo test --locked --offline`, `cargo clippy --all-targets --locked --offline -- -D warnings` and `git diff --check`. Commit only the two owned source files. Report exact commit and proof limits.
- [ ] Root reviews the wire/ownership boundary, then requests an independent read-only review. No push, workflow, external download, ADB, VM, device, signing or publication in the implementation task. These fixtures establish client behavior only; later native MCP acceptance must use the actual helper session and guest backend.
