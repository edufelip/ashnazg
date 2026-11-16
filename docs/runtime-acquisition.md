# Runtime artifact acquisition primitives

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

Local verification passed 61 Rust tests and Clippy on macOS. The production
extractor also processed the matching `b8806d8` CI archive and its 4 GiB root
filesystem; its [receipt](evidence/native-session-acceptance-b8806d8/extractor-receipt.json)
is retained with the native acceptance evidence. The accompanying catalog is
explicitly local test metadata. Network tests use private fixtures, and Linux
locking/rename validation remains pending hosted CI.

The next layer must compose these APIs, validate cache hits, bootstrap the native
MCP session and route APK installation through the guest. Release packaging must
publish matching helper, guest, APK, source and notice assets before default
acquisition can be qualified on a clean machine.
