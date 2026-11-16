# Native CLI Bootstrap Parallel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this plan task-by-task. Root coordinates integration and fresh bounded reviews. Workers use isolated worktrees and commit only their owned files.

**Goal:** Start MCP immediately, initialize one verified native runtime in the background on macOS/aarch64, and join transport/runtime cleanup on EOF or signals while preserving explicit external Android configuration.

**Architecture:** The CLI owns selection, signals and the disposable stdio bridge. A shared initializing MCP provider owns acquisition/session startup independently of request lifetimes. Catalog identities flow from verified acquisition into the existing NativeSession supervisor. Build identity is an explicit validated source stamp; a missing stamp or unavailable release remains an explicit startup failure.

**Tech Stack:** Rust 1.100, Tokio, existing RMCP/Axum, existing runtime acquisition and NativeSession libraries. No new library versions or runtime tools are required.

## Coordination and ownership

Root owns `src/main.rs`, `src/lib.rs`, `Cargo.toml`, this plan, the stdio bridge repair, integration, final full-suite verification, runtime acceptance, PR publication and CI/merge. Workers must not edit those files. Root creates module exports and the build-identity integration test target before dispatch.

| Lane | Owned files | Deliverable |
| --- | --- | --- |
| A: acquisition and launch | `src/native_bootstrap.rs`, `src/runtime_acquire.rs`, `docs/runtime-acquisition.md` | Private default paths, validated source pin, catalog hash handoff, cancellable acquisition-to-session composition |
| B: initializing MCP | `src/mcp.rs`, `docs/native-mcp.md` | Immediate protocol handshake/list, one initializer, cancellable readiness waits, retained session ownership and joined cleanup |
| C: build stamp | `build.rs`, `build_support/source_stamp.rs`, `src/build_identity.rs`, `rust-tests/build_identity.rs`, `.github/workflows/release.yml`, `docs/releases.md` | Explicit validated build-source stamp and release build propagation; no version bump or release publication |

All three lanes start from the same root commit. They do not push, publish, run workflows/VMs/devices or delegate further. Root cherry-picks completed commits in C/A/B order, resolves only expected export/contract integration changes, and dispatches fresh bounded reviewers. A failed lane does not authorize another lane to edit its files. No child may change the agreed signatures without reporting a concrete blocker.

## Shared APIs

Lane A exports:

```rust
pub struct NativeBootstrapConfig { /* private validated fields */ }
pub fn config_for_user(home: std::path::PathBuf, source_sha: Option<&str>)
    -> anyhow::Result<NativeBootstrapConfig>;
pub async fn start_native(
    config: NativeBootstrapConfig,
    cancel: tokio_util::sync::CancellationToken,
) -> anyhow::Result<crate::native_session::NativeSession>;
```

Lane C exports:

```rust
pub fn runtime_source_sha() -> Option<&'static str>;
```

It reads only the validated compiler value `ASHNAZG_RUNTIME_SOURCE_SHA`; an empty compiler value means unavailable. `build.rs` accepts the explicit input `ASHNAZG_BUILD_SOURCE_SHA`, validates exactly forty lowercase hexadecimal characters, and always emits `cargo:rustc-env=ASHNAZG_RUNTIME_SOURCE_SHA=...` (empty when absent) so an ambient output-name variable cannot bypass input validation. Emit `cargo:rerun-if-env-changed=ASHNAZG_BUILD_SOURCE_SHA`. Do not shell out to Git, invent a stamp, or expose invalid input in errors. The Cargo package version remains 0.1.0 and the runtime contract remains 0.3.0 in this slice.

Lane B adds these public functions while retaining existing ready-session and external APIs:

```rust
pub async fn serve_initializing_native_io<R, W, Init, InitFuture>(
    initialize: Init, read: R, write: W,
    cancel: tokio_util::sync::CancellationToken,
) -> anyhow::Result<()>
where
    R: tokio::io::AsyncRead + Unpin + Send + 'static,
    W: tokio::io::AsyncWrite + Unpin + Send + 'static,
    Init: FnOnce(tokio_util::sync::CancellationToken) -> InitFuture + Send + 'static,
    InitFuture: std::future::Future<Output = anyhow::Result<crate::native_session::NativeSession>> + Send + 'static;

pub async fn serve_initializing_native_http<Init, InitFuture>(
    initialize: Init, listener: tokio::net::TcpListener, bearer: String,
    cancel: tokio_util::sync::CancellationToken,
) -> anyhow::Result<()>
where
    Init: FnOnce(tokio_util::sync::CancellationToken) -> InitFuture + Send + 'static,
    InitFuture: std::future::Future<Output = anyhow::Result<crate::native_session::NativeSession>> + Send + 'static;
```

The initializer returns a real NativeSession only. Use a private test seam based on the existing NativeSessionOwner trait for fake-owner lifecycle tests, not a public alternative trust bypass.

## Task A: verified acquisition-to-launch composition

- [ ] Add failing tests for missing/malformed source stamps, relative/missing home, default source-scoped state paths, source/version mismatch, and catalog helper/agent hash handoff.
- [ ] Extend `RuntimeArtifacts` with `helper_sha256: String` and `agent_apk_sha256: String`, populated from the already validated RuntimeCatalog entries on both cold and warm success. Update its local fixtures; do not rehash a file and call that new hash its expected provenance.
- [ ] `config_for_user` validates the source SHA and existing absolute home and derives `Library/Caches/ashnazg/runtime` and `Library/Application Support/ashnazg/native-mcp/<source_sha>`. It exposes no new environment/path/hash override collection. `up` overrides remain unrelated.
- [ ] Create only required private owned directories with descriptor-relative no-follow checks. Refuse symlink/replaced/unsafe existing directories; do not chmod or delete caller data. Do not mutate a refused directory. Keep the existing cache ownership checks and helper's state/asset checks.
- [ ] `start_native` rejects unsupported platform before acquisition, uses the fixed runtime 0.3.0 contract and a 900-second acquisition deadline, verifies the returned version/source, then builds NativeSessionLaunch using the returned catalog helper/agent hashes. Use existing fixed relays 15555/7777/17778, startup_timeout_secs=300, and a fresh startup deadline after acquisition. Port collision fails closed; never adopt another listener/helper.
- [ ] Pass a child cancellation token through both stages. On orderly cancellation retain and await the acquisition/start future through its cleanup; never use caller-future drop as a claim of joined cleanup. Do not cancel the parent's token or delete persistent state.
- [ ] Run focused pure/path/catalog tests, format and strict lint. No HTTP/VM/guest proof is claimed by these tests. Commit owned files and report limitations.

## Task B: single-flight initializing MCP provider

- [ ] Write failing real MCP duplex/HTTP fixtures proving initialize and tools/list complete while a fake initializer is blocked, and the initializer starts exactly once across concurrent calls.
- [ ] Represent external, ready native, and initializing native backends explicitly. Do not manufacture dummy HostConfig or GuestClient credentials for a pending backend. All four UI/log tools and app_install resolve through the same provider readiness state.
- [ ] Start one initializer after transport validation/setup. One lifecycle owner retains its future and eventual unique session cleanup handle. Publish pending/ready/failed/stopped states; no external fallback. On initialization failure, keep transport alive so tool calls receive a fixed startup error; do not expose arbitrary initializer exception text or credentials.
- [ ] Tool calls wait at most thirty seconds for readiness, honor their RMCP request cancellation, and return a clear still-starting/retry error when that bound expires. A request cancellation or timeout never cancels shared initialization. Bound simultaneous readiness waiters to eight; reject excess waiters without an unbounded queue.
- [ ] Preserve the five existing tool names, input schemas and result fields. Cancellation context extractors must not become JSON parameters. Keep the native install single-admission/180-second result/30-second upload behavior and tracked draining.
- [ ] EOF, listener failure, SIGINT/SIGTERM cancellation, helper exit, or public-future drop cancels the child lifecycle. The owner awaits initialization cleanup if still pending; if a session was created it closes request/install admission, drains tracked work and joins its shutdown. Keep the session outside serving unwind boundaries. Preserve both serve and shutdown failures and leave the parent's cancellation token unchanged.
- [ ] HTTP validates loopback/listener/bearer before starting initialization and cancels unfinished request bodies during shutdown. Test authenticated native HTTP initialize/list and one tool call with fake owner, not just unauthorized rejection.
- [ ] Add fixtures for startup failure, request timeout/cancellation, waiter cap, EOF before/during initialization, cancellation after readiness, helper exit, caller drop, serving panic, and owner cleanup failure. Ensure every test-owned task/process/socket is joined/closed.
- [ ] Run focused MCP fixtures and strict lint. Commit owned files with fixture-only proof limits.

## Task C: explicit build source stamp

- [ ] Write failing pure validator tests for absent, valid, uppercase, short, long, nonhex and non-Unicode input. Keep the build-support validator standard-library-only and include it in the integration test target so normal Cargo tests exercise the same function as build.rs.
- [ ] Implement input validation and compiler-value emission as specified above. `runtime_source_sha` returns only a valid nonempty compiler stamp; ordinary unstamped development builds remain usable through the external Android branch.
- [ ] Pass the existing validated release source SHA into the host Cargo build step in release.yml. Keep tag/version/publication gates and permissions intact. Do not bump Cargo, publish a catalog, dispatch a workflow or claim a 0.3.0 runtime release exists.
- [ ] Verify validator tests, default build behavior, malformed-input build rejection in an isolated target if needed, YAML/actionlint and format/lint. Avoid rebuilding unrelated full suites in parallel with other lanes. Commit owned files.

## Root integration and delivery

- [ ] Finish stdio inheritance/fixture repairs and a fresh bounded recheck. The child closes unrelated descriptors after trusted executable startup and before copy threads; document that exact boundary rather than promising close-on-exec for caller handles that lack it.
- [ ] Integrate reviewed lanes. Select external mode whenever ASHNAZG_TOKEN is present, including empty/non-Unicode values that must fail explicitly. Without it, select native only on macOS/aarch64; elsewhere return an unsupported/unconfigured error.
- [ ] For native stdio, construct/validate OwnedStdioBridge before protocol startup, retain it through serving and explicitly join shutdown. No Tokio blocking stdin fallback. For HTTP, validate bearer and bind before invoking the initializing provider. All diagnostics stay on stderr.
- [ ] Use runtime_source_sha plus the current user's home to build config inside the initializer; missing source/catalog/assets becomes an explicit logged startup failure and a fixed MCP tool error. Do not preboot before MCP initialize or make up client timeout claims.
- [ ] Root owns SIGINT/SIGTERM forwarding via a child lifecycle token, handles EOF and transport failures, and joins both session and bridge cleanup. Preserve existing rpc/install/up behavior and HTTP's independent bearer configuration.
- [ ] Run one combined offline full Rust suite, strict Clippy, formatting/diff checks and public CLI selection/interruption fixtures using only owned descriptors/fake helpers. Broaden testing only for actual failures or changed boundaries.
- [ ] Review the combined diff with fresh bounded delegates, repair demonstrated issues, publish a draft PR, link immediately, and watch CI without polling. Merge only on exact-head applicable green checks and resolved findings.
- [ ] Gather real owned restart/stdio interruption acceptance separately after source-matching assets are available. Existing explicit HTTP acceptance does not establish this CLI path, cold acquisition, public distribution or CPU savings. Matching catalog/helper/guest/APK publication and source/notice closure remain release work.
