# Runtime artifact acquisition

The Rust library now has the parts needed to acquire a native runtime safely.
They are separate APIs; the CLI does not yet compose them or download a default
runtime. No public runtime catalog has been published.

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

The coordinator is available to library callers; the CLI and MCP session do
not yet invoke it, and no public runtime catalog has been published. Small
private-source tar/XZ fixtures cover cold promotion, offline warm reuse,
corruption refusal and dropped-future cleanup. They do not prove live GitHub
acquisition, multi-gigabyte handling, VM boot, CLI/session acceptance or Linux
locking and rename behavior. Those still require their separate hosted and
runtime acceptance evidence.

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
