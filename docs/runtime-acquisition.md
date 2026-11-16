# Runtime artifact acquisition

The Rust library exposes acquisition as a cancellable API and the native CLI
bootstrap composes it with the owned helper session. No public runtime catalog
has been published, so a clean machine still reports native startup unavailable
until a matching release catalog and artifacts exist.

`runtime_catalog` parses the producer's bounded schema-1 catalog against an
expected release version and source commit. Artifact roles, platform names,
basenames, sizes and hashes are fixed. Download URLs are constructed for this
repository's exact release tag. This supplies repository-release integrity
metadata, not a separate publisher signature or intrinsic binary-source proof.

`runtime_download` streams HTTPS responses into a caller-owned asynchronous
writer. Redirects stay on the explicit GitHub download-host allowlist, and one
deadline and cancellation token cover the transfer. Nonidentity encodings,
unexpected lengths, excessive data and hash mismatches fail without a success
receipt. The caller must discard its partial writer. Errors omit signed URLs
and response bodies.

`runtime_extract` handles the pinned guest's eleven flat regular members with
bounded XZ memory, decompressed size and per-member sizes. It refuses links,
extensions, unsafe names and unexpected trailing data. Compressed bytes and
boot assets must match their hashes and manifest pins. The destination must be
an empty, private, owned directory. Cancellation and deadlines are cooperative
between bounded filesystem operations; an OS call itself cannot be interrupted.
An asynchronous caller must join a blocking extraction before removing its
staging directory.

`runtime_cache` owns the private cache root, exclusive locks, stages and atomic
promotion. It refuses unmarked caller data, rolls back failed setup, preserves
existing entries and never replaces a promotion destination. A stage borrows
its guard so the lock stays alive. Lookup validates structure and catalog-key
binding; it does not authenticate payload bytes. The acquisition coordinator
must perform that validation before execution. Private permissions and locks
exclude other users and serialize legitimate writers; hostile same-user
mutation is outside this boundary.

`runtime_acquire::acquire_runtime` is the library composition for the fixed
`0.3.0` helper contract. It checks the requested version, 40-character source
SHA, cancellation state and absolute deadline before opening the cache. One
blocking worker owns the lock and its stage from cache selection through
download, extraction, validation, cleanup and no-replace promotion. On a miss,
that worker lazily creates one private Tokio runtime and the existing fixed
production downloader. On a hit, no HTTP client or runtime is created. Dropping
the caller future cancels the worker token; the worker retains ownership and
finishes cleanup before releasing its lock. Deadlines are cooperative between
bounded operations, so they cannot interrupt an individual filesystem call.

Cache entries retain the cache's `stage.json` and `entry.json` control markers,
along with the exact catalog, integrity receipt, helper, two APKs and eleven
regular extracted assets. The bounded schema-1 integrity receipt records the
catalog-bound compressed guest identity and a unique vector of all eleven
extracted member names, sizes and hashes. Warm validation checks the catalog
hash and stamp, receipt schema and uniqueness, exact names/types/modes/sizes,
per-member size caps shared with the extractor, and the helper and APK hashes.
It rehashes the bounded boot manifest and
reapplies the pinned manifest checks using the receipt's kernel and rootfs
identities. It does not reread the 4 GiB rootfs or rehash every other extracted
asset. The Swift helper remains responsible for hashing kernel and rootfs
before boot. The receipt describes cold extraction; it does not independently
authenticate arbitrary later changes to guest assets, and hostile same-UID
mutation remains outside this cache boundary.

`native_bootstrap::config_for_user` validates an existing absolute user home
and an explicit 40-character lowercase source stamp. It derives the fixed
`Library/Caches/ashnazg/runtime` cache and a source-scoped
`Library/Application Support/ashnazg/native-mcp/<source>` state directory;
there are no environment or arbitrary path/hash overrides. Startup rejects
unsupported hosts before acquisition, ensures only these app-owned directory
trees with descriptor-relative no-follow operations, and refuses unsafe
existing directories without repairing or deleting them. It uses the fixed
0.3.0 contract and 900-second acquisition deadline, checks returned version
and source identity, then starts `NativeSession` with the helper and agent APK
hashes retained from the validated catalog. The launch identity uses ports
15555, 7777 and 17778 and a fresh 300-second helper startup deadline. A child
cancellation token reaches both stages; each stage is awaited so orderly
cancellation joins its existing cleanup before returning. Persistent cache and
state are retained.

Small private-source tar/XZ fixtures cover cold promotion, offline warm reuse,
catalog hash handoff, corruption refusal and dropped-future acquisition
cleanup. Pure bootstrap/path/identity tests do not prove live GitHub
acquisition, multi-gigabyte handling, VM boot, helper startup, CLI/MCP
acceptance or Linux locking and rename behavior. Those still require their
separate hosted and runtime acceptance evidence.

Separate prior extractor evidence shows it processed the matching `b8806d8` CI
archive and its 4 GiB root filesystem; its
[receipt](evidence/native-session-acceptance-b8806d8/extractor-receipt.json) is
retained with native acceptance evidence. The accompanying catalog is
explicitly local test metadata. Network tests use private fixtures, and Linux
locking/rename validation remains pending hosted CI.

The next layer must expose the coordinator through the CLI, bootstrap the native
MCP session and route APK installation through the guest. Release packaging must
publish matching helper, guest, APK, source and notice assets before default
acquisition can be qualified on a clean machine.

## Prepublication offline qualification

A prepared candidate can be staged in a new, private acceptance cache using the explicit `stage_runtime_candidate` example. It uses the production catalog parser, guest extractor and cache transaction APIs; it does not contact the network or change production URL policy. This supports real CLI/VM qualification of exact prepared bytes before publication, and is not cold-download evidence.

```sh
python3 scripts/runtime_release.py verify --directory "$CANDIDATE" --source-sha "$SOURCE_SHA"
mkdir -m 700 "$ACCEPTANCE_HOME"
mkdir -p "$ACCEPTANCE_HOME/Library/Caches/ashnazg/runtime"
chmod 700 "$ACCEPTANCE_HOME/Library/Caches/ashnazg" "$ACCEPTANCE_HOME/Library/Caches/ashnazg/runtime"
cargo run --release --locked --example stage_runtime_candidate -- \
  "$CANDIDATE" "$ACCEPTANCE_HOME/Library/Caches/ashnazg/runtime" "$SOURCE_SHA"
```

Use the source-matching packaged host for acceptance after staging. The helper still validates the kernel/rootfs before boot. The utility refuses an existing entry rather than replacing it; cache and persistent state remain owned by the acceptance home. Public cold acquisition must later use a different empty home without this staging step.
