# Runtime Resolver Implementation Plan

> **For agentic workers:** Use `subagent-driven-development` to implement the steps, then review the completed diff independently.

**Goal:** Compose the verified runtime catalog, downloads, extraction and cache into one cancellable library operation with offline cache reuse.

**Architecture:** One blocking worker owns the cache lock and stage throughout acquisition. It creates its own current-thread Tokio runtime and production HTTP downloader only on a cache miss. All stage file writes happen synchronously on that worker, so cancelling a network future cannot leave asynchronous filesystem writes racing with cleanup.

**Tech Stack:** Existing Rust, Tokio, reqwest, serde, SHA-256, tar/XZ and Unix descriptor primitives. No new dependency, CLI, release or session changes.

## Files and public boundary

- Create `src/runtime_acquire.rs`: coordinator, bounded entry validation, private download adapter and inline contract tests.
- Export from `src/lib.rs`.
- Extend `src/runtime_cache.rs` narrowly with validated, bounded entry-key enumeration and its tests; preserve existing ownership, setup rollback and no-replace promotion.
- Permit crate-local reuse of `runtime_extract::validate_manifest`; do not duplicate or loosen manifest pins.
- Update `docs/runtime-acquisition.md` to explain actual cold/warm validation and remaining delivery boundaries.

```rust
pub struct RuntimeArtifacts {
    pub version: String,
    pub source_sha: String,
    pub catalog_sha256: String,
    pub helper: PathBuf,
    pub agent_apk: PathBuf,
    pub fixture_apk: PathBuf,
    pub assets: PathBuf,
    pub cache_hit: bool,
}

pub async fn acquire_runtime(
    cache_root: PathBuf,
    expected_version: String,
    expected_source_sha: String,
    deadline: std::time::Instant,
    cancel: tokio_util::sync::CancellationToken,
) -> anyhow::Result<RuntimeArtifacts>;
```

The current helper/guest contract supports exactly `0.3.0`; reject another version, malformed source SHA, cancellation or an expired deadline before touching the cache. This does not change Cargo's host package version or embed a release source stamp yet.

## Task 1: Cache selection and validation

- [ ] Add failing inline fixtures for selecting an exact version/source offline, refusing a corrupted catalog and refusing ambiguous matching entries. Use a private fake artifact source; no test-only URL override in production.
- [ ] Add `CacheGuard::entry_keys()` (crate-local is sufficient), using its owned entries descriptor, maximum 128 entries, strict 64-character lowercase SHA-256 keys, and existing entry validation. Refuse malformed entries and oversized enumeration; do not prune or adopt anything.
- [ ] Read each `catalog.json` with no-follow/nonblocking regular-file opens, current UID, mode 0600, maximum 64 KiB. Its raw SHA-256 must equal the entry key. Extract its advertised version/source with a typed envelope, then run the existing strict `RuntimeCatalog::parse` against that stamp before selecting. Reject multiple entries matching the requested stamp, rather than choosing directory order. Other valid stamps may coexist.
- [ ] Define strict bounded schema-1 `integrity.json` for the requested stamp, catalog hash, verified guest compressed size/hash and all eleven extracted members. Represent member records as a vector with explicit uniqueness and exact-member/cap checks, avoiding silent duplicate map overwrites. Bind the guest archive identity to the catalog. Never serialize temporary absolute paths or signed URLs.
- [ ] On a selected entry, require the exact expected file layout and private owned regular files/directories. Rehash helper and both APKs against the catalog before returning them. Require helper mode 0700; APK/catalog/receipt/assets files mode 0600 and directories 0700. Rehash the bounded boot manifest against the extraction receipt and reuse pinned manifest validation with the receipt's kernel/rootfs identities. Validate the actual eleven asset names, sizes and types.
- [ ] Do not hash the 4 GiB rootfs on warm lookup: the Swift helper hashes both boot files before boot. Document that the returned assets remain subject to helper validation. The private receipt records verification performed during cold extraction; it does not authenticate arbitrary externally modified guest files independently of that helper. Hostile same-UID mutation remains outside the existing cache boundary.
- [ ] Run focused red/green tests, including small artifact corruption, symlink/FIFO/type/mode refusal, malformed/missing/duplicate integrity records, mismatched manifest/source and valid unrelated stamp coexistence. Keep every metadata read bounded.

## Task 2: Cold acquisition transaction

- [ ] Add failing small real tar/XZ fixtures containing the extractor's exact eleven members, matching strict catalog and helper/APK bytes. Require a cold acquisition, one promotion, then an offline cache hit with zero artifact-source calls.
- [ ] Validate input stamp and hold `CacheGuard` exclusively throughout selection, writes, extraction and promotion. Retry only `Ok(None)` lock contention with at most 25 ms sleeps bounded by the same absolute deadline/cancellation; propagate other cache errors.
- [ ] Use a private synchronous `ArtifactSource` boundary for worker tests. The production implementation lazily initializes its local Tokio runtime and existing `RuntimeDownloader`; all URLs, redirects, encoding and transfer limits remain the existing production policy. No environment fixture endpoints, shell curl, host ADB or network fallback.
- [ ] Create one owned stage; use exclusive no-follow mode-0600 files with only validated catalog basenames. The async-writer adapter wraps the worker's standard `File`, writes at most 64 KiB per poll and checks deadline/cancellation. Do not use `tokio::fs::File`, detached writes or spawn another owner of stage contents. Bound each hash/read similarly.
- [ ] Download the catalog and guest/helper/agent/fixture; independently bind returned receipts to catalog identities. Store exact raw catalog bytes. Create a private empty `assets/` directory and invoke the production extractor with the same deadline and cancellation token. Validate its source, compressed identity and complete member receipt before serializing integrity metadata. Set helper executable only after verification.
- [ ] Remove only the stage-owned compressed guest archive after successful extraction to avoid retaining a second large copy. Source archive entries remain in the catalog but are not downloaded by the runtime coordinator. Write and sync catalog/integrity/files before no-replace promotion; perform final cancellation/deadline checks before promotion. Return paths under the promoted entry, never staging paths.
- [ ] On any failure explicitly abort the owned stage and surface cleanup failure. Preserve earlier valid entries and caller-owned/unmarked roots. No command execution, stale-stage adoption, replacement or pruning.
- [ ] Red/green coverage: hash/size failure, truncated extraction, missing artifact, source mismatch, cancellation during fetch and extraction, occupied cache lock/deadline, unexpected existing destination, no stage left after failure, and prior valid entry unchanged.

## Task 3: Async ownership and verification

- [ ] `spawn_blocking` owns the complete worker transaction. Await and join it on normal completion, cancellation and errors before reporting completion. A drop guard cancels a worker-local child token if the caller drops the future; the worker retains its lock/stage and performs its own cleanup. Do not clean a stage externally while its worker runs. Caller cancellation must not cancel an unrelated parent token.
- [ ] Test dropping an in-flight acquisition with controlled fixture barriers: cancellation is observed, the worker finishes and releases the lock, and a later acquisition sees no partial promoted entry. Use bounded thread synchronization, not unbounded waits. Cooperative deadlines cannot preempt a blocking OS filesystem call; retain that explicit limitation.
- [ ] Update documentation with cold verification, offline warm behavior, deferred boot hashing, lock/cancellation ownership and absence of published catalog/CLI composition. Never claim live GitHub or VM acceptance from fixtures.
- [ ] Run `cargo fmt --all -- --check`, `cargo test --locked --offline`, `cargo clippy --all-targets --locked --offline -- -D warnings`, and `git diff --check`. Commit only owned files. Linux locking/rename branches require hosted CI; no workflow or VM launch in this implementation task.
- [ ] Root performs spec review and requests independent defect review before pushing a draft PR. Real multi-gigabyte acquisition and default CLI/session acceptance remain separate proof.
