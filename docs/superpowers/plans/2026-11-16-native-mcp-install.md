# Native MCP installation implementation plan

Goal: preserve MCP `app_install` behavior using the owned native session, including
package inference for `launch=true` without a package, without host ADB/aapt/SDK.
Implement and review the parser layers before changing installation or MCP routing.
Keep explicit RPC/ADB compatibility mode and the MCP parameter/result schema.

Use isolated Luna implementation tasks with independent review. Decoder and archive
reader own separate files; only the archive task changes Cargo dependencies. Root
owns integration, client changes and MCP lifetime wiring. No child launches a VM,
device, SDK tool or workflow. Test-owned files and local socket fixtures are allowed.

## 1. Narrow compiled-manifest decoder

Own `src/axml_package.rs`, with inline tests. API:

```rust
pub fn package_name(bytes: &[u8], deadline: Instant,
    cancel: &CancellationToken) -> anyhow::Result<String>;
```

Accept compiled Android binary XML, UTF-8 or UTF-16 string pools, and return the
single unqualified `package` attribute of one unqualified `manifest` root. Text
XML is rejected: this path inspects APKs produced by Android's compiler, not
uncompiled project manifests. Require a string value; if raw and typed strings
both exist they must agree. Reject duplicate packages, invalid native package
names, malformed framing/indices/lengths, mismatched nesting, multiple roots,
missing end tags, unsupported chunks and trailing data. Reuse the existing native
package validator (ASCII, <=255 bytes, at least two valid dot-separated segments).

Use checked byte slices/arithmetic and a narrow chunk scanner. Do not build a DOM
or eagerly decode the string pool. Cap input at 4 MiB, depth at 256, chunks at
65,536 and attributes per element at 4,096. Validate count/table bounds before
allocation; only decode bounded names/package strings needed for inference.
Check deadline/cancellation during bounded scans and before success. No panics,
unsafe code or raw input in errors. This is package inference, not full APK or
Android semantic validation; Android still decides installation validity.

Layout reference is the pinned Android 15 [ResourceTypes.h](https://android.googlesource.com/platform/frameworks/base/+/cc8bb19595c661f5cf42f330457000e643c67d1b/libs/androidfw/include/androidfw/ResourceTypes.h).
`libaxml 0.0.2` was inspected and is not selected: its string-pool decoder calls
`Vec::with_capacity` with unvalidated input counts and eagerly decodes every
string. The narrow scanner avoids that allocation boundary.

Tests start red for API absence. Cover generated UTF-8/UTF-16 pools, raw/typed
agreement, namespaces, duplicate/missing/invalid package, bogus counts/indices,
truncation and arithmetic boundaries, nesting/chunk/attribute caps, cancellation,
deadline and generic errors. Also decode the three retained project-compiled
manifest fixtures; do not require a local SDK.

## 2. Bounded archive member reader

Own `src/apk_manifest.rs`, Cargo.toml and Cargo.lock, with inline tests. First API:

```rust
pub fn read_android_manifest(file: &File, deadline: Instant,
    cancel: &CancellationToken) -> anyhow::Result<Vec<u8>>;
```

Pin `rawzip = =0.5.2` with defaults disabled and only `std`, and
`flate2 = =1.1.11` with defaults disabled and only `rust_backend`.
Rawzip is chosen over materializing the whole central directory;
reuse its ZIP parsing/integrity APIs, not a project ZIP decoder. Root inspected
the exact registry sources and checksums before selecting these versions. Consult
the pinned [reader](https://docs.rs/rawzip/0.5.2/rawzip/) and validation APIs.

Read the supplied descriptor positionally using a custom bounded ReaderAt; never
reopen its path, alter its shared cursor, extract files or invoke tools. Require a
regular file of 1..128 MiB; compare its dev/inode/size/mtime/ctime before and after.
Cap directory entries at 16,384, directory bytes at 4 MiB, cumulative I/O at 32 MiB,
each read at 64 KiB and I/O calls at 100,000. Reject inconsistent entry counts,
invalid ranges/headers, duplicate exact `AndroidManifest.xml`, absent/empty/
encrypted manifest and local/central manifest identity disagreement. No name
normalization or filesystem extraction; only the exact root member is selected.

Read only the manifest payload, supporting stored and raw DEFLATE, cap output at
4 MiB (including actual streamed output), and require complete decompression,
declared size and CRC agreement. Reject hidden trailing compressed bytes. Check
deadline/cancellation throughout directory/decompression reads and before success.
Use one absolute budget passed by the caller; OS calls remain cooperative rather
than preemptible. Keep all errors fixed and free of archive text/paths.

Tests start red. Cover stored/deflated members, real compiled manifest contents,
missing/duplicate/encrypted/truncated/CRC-bad/over-limit members, malformed counts
and ranges, compression bombs, incomplete/trailing streams, cursor preservation,
descriptor path replacement, observed mutation, cancellation and deadlines.

Root then composes `infer_package(&File, deadline, cancel)` with one five-second
absolute inference budget for both layers, hides the decoder/raw reader helpers,
and adds combined APK fixtures. Intermediate module exports are scaffolding;
no parser or native MCP behavior is advertised until integration and review pass.

## 3. Bind inference to native installation

Add an inferred-launch operation or internal `LaunchRequest::{None, Explicit,
Infer}` without changing existing install semantics. The worker opens once,
snapshots identity, infers from that File, then hashes and streams that same File.
Carry its existing 30-second upload and caller deadline through inference. Add
ctime to the snapshot so observed in-place changes with restored mtime also fail.
Explicit package and no-launch paths bypass inference. All existing ACK, digest,
EOF, cancellation and generic-error rules remain.

Use private deterministic checkpoints to replace the path or mutate the opened
file after inference. Assert both header package and transmitted bytes remain
bound to the original descriptor, or the operation refuses before connecting.

## 4. Native MCP adapter and lifecycle

Add a library entry point that owns NativeSession for the server lifetime and
obtains both clients only through its factories. Dispatch native installation
without global token environment changes or foreign credential adoption. Preserve
legacy constructors/mode and InstallParams/result fields. Native results should
identify the owned host ADB relay endpoint, validated in readiness, as `serial`;
the guest-internal ADB address is a separate implementation detail. Add only the
non-secret session metadata getter needed for that mapping.

On stdio EOF, HTTP return, cancellation or helper termination, cancel outstanding
install work, stop serving and join session shutdown. Preserve server and cleanup
failure information without raw credentials/helper/guest output. Test unchanged
schema/results, explicit and inferred launch, compatibility mode, malformed APKs,
request cancellation and serving shutdown with bounded fixtures.

CLI acquisition/default changes remain separate: matching release assets/catalog
are not public yet. First qualify the library adapter using explicit owned local
inputs and a real MCP client: native `app_install` with and without package,
fixture behavior, EOF/interruption and owned cleanup, with host ADB/aapt disabled.

## Delivery gates

Each implementation reports its exact commit and real fixture results; no push.
Run fmt, applicable Rust tests, strict all-target Clippy and whitespace checks.
Root reviews each layer and combined descriptor/install behavior before real VM
acceptance. Publish one reviewed candidate and require exact-head Linux/macOS,
emulator, ReDroid and applicable HWC/MicroVM CI before merging. Keep fixture,
runtime, acquisition and release claims separate.
