# Automatic Native Runtime Delivery Plan

> **For agentic workers:** Use subagent-driven-development with isolated ownership. Root performs specification review, then requests independent code review. No nested agents.

**Goal:** Let a released macOS Apple Silicon host acquire its matching runtime and start an authenticated Android agent without host Android SDK tools.

**Architecture:** A release catalog binds named artifacts to a release/source commit. The Swift helper remains the VM owner and prepares the agent through a fixed host-CID-only guest protocol. The guest uses its already locked ADB; no host ADB download or general guest execution interface is introduced. MCP can either join an owned running session or own a complete runtime session itself.

**Tech Stack:** Rust host, Swift Virtualization.framework, Python guest standard library and locked Alpine android-tools, deterministic Python release tooling.

## Decisions

- Reuse the guest's locked ADB. Do not add Google SDK acceptance or a new macOS ADB build to first-run setup.
- Preserve the native helper's lifecycle ownership, exclusive state lock, artifact checks, absolute deadlines, signal handling and graceful guest shutdown.
- The default release path may download only matching public release assets. Explicit development assets remain supported. An unstamped development build must not invent a default URL or silently use a different release.
- Public packaging must include a real component/source inventory for the complete rootfs, including base packages. ReDroid remains a first-boot upstream download; publishing a derived OCI image remains a separate unresolved component-provenance task.
- No 7-second startup claim is supported. Retained observations are 108.923 seconds from VM startup cold and 24.460 seconds on restart; CLI-launch intervals are separately recorded.

## Release catalog v1

The catalog filename is `ashnazg-runtime-v<version>.json`. Required root fields are `schema_version` (1), `version` (stable semantic version without `v`), `release_tag` (`v` plus version), `source_sha` (40 lowercase hex), `artifacts`, and `source_archives` (an array, possibly empty during unpublished development).

Required artifact roles and fixed basenames:

| Role | Platform | Basename | Maximum bytes |
|---|---|---|---:|
| `guest` | `linux-aarch64` | `ashnazg-microvm-v<version>-linux-aarch64.tar.xz` | 1 GiB |
| `helper` | `macos-aarch64` | `ashnazg-vm-v<version>-macos-aarch64` | 64 MiB |
| `agent` | `android` | `ashnazg-agent-v<version>.apk` | 64 MiB |
| `fixture` | `android` | `ashnazg-fixture-v<version>.apk` | 64 MiB |

Each entry contains exactly `name`, `platform`, `source_sha`, `size`, and `sha256`. Size is a positive integer, never a JSON boolean. Hashes are lowercase 64-character hex. Source SHA must match the catalog. Roles and basenames are unique; arbitrary URLs, traversal, links, empty files and a catalog that lists itself are rejected. Source archives use basenames `ashnazg-runtime-sources-v<version>-NNN.tar.xz`, platform `source`, the same entry fields and source SHA, unique three-digit indices, and at most 1 GiB per part. At most 64 parts are allowed. The release publisher must require complete source/notice evidence before publication; an empty array is not a source-closure claim.

The catalog does not hash itself or the host ZIP containing the binary. The release's external checksums cover host ZIPs. A release host is compiled with the exact release source SHA; its resolver requires both the compiled version and source SHA to match the catalog. This is HTTPS/repository-release provenance, not a separate publisher signature or notarization claim.

The resolver constructs URLs from the fixed `edufelip/ashnazg` repository, exact release tag and validated basename. No custom catalog URL is accepted by the product default. Bound redirects to the supported GitHub download hosts, catalog size to 64 KiB, each artifact to its catalog size and role cap, and network operations to explicit deadlines. Stage in a private locked cache, reject unsafe archive entries, verify bytes before use, and atomically promote a complete cache. Preserve verified cache on session shutdown; remove incomplete staging on failure.

## Task 1: Catalog generator

Files: create `scripts/runtime_catalog.py` and `tests/test_runtime_catalog.py` only.

Provide pure `build_catalog(version, source_sha, artifacts, source_archives=()) -> dict` and `write_catalog(..., output) -> Path` functions. `artifacts` maps the four required roles to already staged regular files with the fixed release basenames. `source_archives` is an iterable of staged source-part files. Stream file hashes; reject final-component symlinks, missing/nonregular/empty inputs, files exceeding their cap, inconsistent role sets, unsafe versions, duplicate source indices, and output/input aliasing. Do not follow arbitrary URLs or evaluate any input as code. Detect input replacement or mutation during hashing rather than reporting a digest for a changed file.

`write_catalog` serializes deterministic sorted JSON with one final newline, refuses to replace an existing output or unowned path, and writes through an owned temporary file/atomic promotion. The CLI takes `--version`, `--source-sha`, four role flags (`--guest`, `--helper`, `--agent`, `--fixture`), repeated `--source-archive`, and `--output`. Producer provenance is supplied by the release workflow's validated source; this library computes byte integrity and does not claim to prove a binary's source revision intrinsically.

- [x] Write unittest fixtures first, including a valid catalog, deterministic bytes, source/version/name errors, missing/duplicate roles, booleans rejected by catalog validation, symlink/nonregular inputs, role caps, source-part uniqueness, mutation during hashing, output refusal/aliasing, and no self-hash cycle.
- [x] Run `python3 -m unittest tests.test_runtime_catalog -v` before implementation and retain the expected failure.
- [x] Implement catalog creation and a matching strict `validate_catalog` function using the contract above; validate generated catalogs before writing.
- [x] Re-run focused tests, compilation and `git diff --check`; commit only the two owned files. Do not publish, change release versions, edit workflows or manufacture artifact hashes.

Catalog implementation and focused verification are complete. The producer has passed independent review and root repaired the populated intermediate-symlink case using held directory descriptors. This does not create a published catalog or qualify artifact acquisition.

## Guest session protocol contract

Keep existing vsock control port 10777 and raw ADB port 15555. Add fixed agent RPC and installation channels on guest vsock ports 17777 and 17778, respectively. Accept only the host CID on every control/installation connection. These ports never select an arbitrary guest command or target.

An agent preparation request on the control channel has schema version 1, operation `prepare_agent`, a UUID `session_id`, fresh distinct 256-bit lowercase-hex RPC and install tokens, and an agent APK size/SHA-256. The header is bounded newline JSON (4 KiB). The guest refuses a second live session and authenticates ownership by host CID. It acknowledges readiness to receive bytes before reading exactly the declared APK size, verifies its digest, and retains it in an anonymous sealed memory file. The APK limit is 128 MiB; the upload deadline is absolute and bounded. Only the fixed package `dev.ashnazg.agent` may be prepared.

The guest installs the agent on the explicit fixed ReDroid serial, creates only its own local agent forward, starts the fixed instrumentation with the fresh token, then requires an authenticated `ping`. Only afterward does it report `agent_ready`. Preparation failure tears down only resources created during that attempt. Expired or partial uploads never become an installed success. The existing instrumentation token mechanism remains in use; tokens must not appear in host arguments, receipts or logs.

The installation channel requires the install token and session UUID. Its only operation is a bounded APK upload/install with optional validated package launch. Header validation/authentication and a single-install capacity check precede a `ready_for_payload` response. Verify the exact byte count and SHA-256 before running fixed ADB argv on the fixed serial. Receive into an anonymous memory file, seal it against mutation after verification, and pass its descriptor to the fixed guest installer on stdin. Close owned descriptors on success, error, timeout, interruption or shutdown. Reject truncated, oversized, extra or malformed frames. No shell command, file destination, serial selector, display mode, or arbitrary execution parameter is accepted. The existing host package-name decoder supplies an explicit validated package when launch is requested; the guest independently validates it and resolves only its launcher activity.

Upload connections carry exactly one operation. After the declared payload the sender half-closes its write direction; the receiver requires EOF, rejecting extra bytes under the same absolute upload deadline before installing anything. Replies still travel in the other direction. Header and payload together have at most 30 seconds; per-byte arrival cannot reset that budget.

### Upload codec ownership

Create only `runtime/microvm/apk_stream.py` and `tests/test_apk_stream.py` for the first guest-side task. Export `read_upload_header(socket, *, deadline, clock=time.monotonic)` and `receive_apk(socket, header, *, deadline, clock=time.monotonic)`. The latter returns a context-managed `StagedApk` with `fileno()`, `size`, `sha256` and `close()`. Linux `memfd_create` supplies anonymous storage; after size/hash/EOF validation, require write/grow/shrink/further-seal protection. `close()` closes its owned descriptor and never unlinks a pathname. The caller may pass the borrowed descriptor as installer stdin and must keep the context alive until the child command finishes.

The immutable header object represents exactly one of two schemas. Preparation requires `schema_version`, `op`, `session_id`, `rpc_token`, `install_token`, `apk_size`, `apk_sha256`. Installation requires `schema_version`, `op`, `session_id`, `install_token`, `apk_size`, `apk_sha256`, `launch`; it requires `package` only when `launch` is true. Both tokens are 64 lowercase hex, UUIDs are canonical lowercase strings, SHA-256 is 64 lowercase hex, and size is an integer from 1 through 128 MiB excluding booleans. A launch package has at least two ASCII identifier segments separated by dots and is at most 255 characters. Unknown/duplicate fields, invalid UTF-8, non-object JSON, unsupported versions/operations and oversized headers are errors. Errors and object representations must not reveal tokens or raw header text. This module does not authenticate peer/token ownership; its caller must do so before acknowledging or receiving payload.

Use Linux anonymous memory files with close-on-exec and mode 0600, with no named-file fallback. Stream/hash at most 64 KiB at a time; the per-upload maximum remains 128 MiB and the session handler must admit only one active upload before receiving a body. Require exact size, matching hash and EOF; close incomplete descriptors on every error. Bound every socket operation by the original finite absolute deadline, capped once at 30 seconds across header and payload, including the final EOF check. No ADB invocation, install, arbitrary command, or background process belongs in this codec. Anonymous descriptors remove the check-then-unlink race that cannot be eliminated by a preceding inode check on a mutable pathname.

Write stdlib unittest fixtures first for valid upload/half-close, strict header schemas and secret redaction, oversize/truncation/hash mismatch/extra bytes, absolute deadlines under slow progress, anonymous descriptor ownership, no pathname unlink on cleanup, seal failure cleanup and success/error context cleanup. macOS host tests may substitute anonymous temporary-file storage for framing tests; Linux CI must exercise real memfd and seal enforcement, and the substitution is not Linux capability proof. Run `python3 -m unittest tests.test_apk_stream -v` red then green, compile Python, run existing guest tests and `git diff --check`, and commit only the two owned files. These tests prove framing/staging only; authenticated service and real package installation remain separate acceptance.

Agent RPC is a raw proxy to the guest-owned local agent forward. The installer returns bounded JSON results; a command exit alone is not a readiness or launch-success claim. The guest's session object owns instrumentation, the exact forward pair and upload descriptors. Guest shutdown closes session listeners/connections and agent resources before PID 1 stops Docker and powers off.

## Native session contract

The helper generates both tokens, keeps them private, sends them only over the owned host-to-guest channel, and writes a private session credential file atomically under its already locked state directory. Public runtime receipts identify session UUID, source/artifact identity and loopback agent/install endpoints but contain no tokens. A credential file must be a regular non-symlink file owned by the current user with mode 0600; its directory is private. Remove the active credential file on failure/shutdown. Do not adopt an unowned stale session.

`ashnazg up` without explicit development inputs resolves the matching release artifacts and starts this agent-ready session. Existing explicit VM-only development operation remains available. `ashnazg mcp --runtime` owns a helper session for the lifetime of its stdio MCP connection; `ashnazg mcp --session DIR` joins an already running owned session without taking VM lifecycle ownership. Preserve the existing environment-based device/MCP mode. Do not print credential values or intermix readiness JSON with MCP stdout.

The MCP owner must cancel on VM/instrumentation exit, EOF or signal and await owned shutdown. The install MCP tool selects the native installer when using a native session and the existing explicit-host-ADB path for external devices. Joining a session must not terminate it on MCP client EOF. Failed initialization must leave no owned forward, VM, child process, credential file or acquisition lock behind.

## Subsequent implementation ownership

1. Guest session/bootstrap/installer module and portable fake-boundary tests; root integrates it with `guest_bridge.py` and the guest builder after review.
2. Swift session preparation, loopback agent/install relays, private credential lifecycle and helper tests, preserving the current VM owner.
3. Rust catalog acquisition/cache and CLI resolution; keep it separate from the later MCP session owner.
4. Rust native-session/MCP integration with signal/EOF cleanup and existing external-device behavior preserved.
5. Release packaging, rootfs source/notice inventory and public catalog/assets on an exact main source with successful required CI.
6. Actual clean-cache/cached runtime acceptance without host ADB, authenticated MCP initialize/list/call/install, injected startup/upload/shutdown failures, recovery, and retained receipts. Record setup/download/VM/agent/MCP timings separately; do not infer old acceptance results for this new session path.

Each later owner receives a complete concrete task after its boundary is integrated and reviewed. No public release is performed by a delegated child. The root performs publication only after review and applicable CI/runtime/source-closure checks.

Anonymous upload decision: AOSP Android 15 PackageManagerShellCommand supports APK input from stdin with `-S BYTES` and `-`; this is the fixed guest-install path to qualify. Kernel `CONFIG_MEMFD_CREATE=y` already exists. Live Android installation through this path remains required evidence.

### Task 2a: Guest session admission and protocol owner

Create only `runtime/microvm/agent_session.py` and `tests/test_agent_session.py`. Keep actual ADB subprocesses and bridge listener wiring outside this task. Inject a backend with `prepare_agent(staged_apk, rpc_token, *, deadline)`, `install(staged_apk, *, package, deadline)`, `is_alive()`, and `close()`. Backend calls return no untrusted output; prepare returns an agent-loopback port integer, install returns only after the backend has verified success (and optional launch). The backend owns exact forward/instrumentation cleanup. It must permit `close()` concurrently with a call to cancel that work; the session must never publish ready after cancellation.

Export `GuestSession(backend, *, clock=time.monotonic)` with `serve_prepare(connection, *, peer_cid, deadline)`, `serve_install(connection, *, peer_cid, deadline)`, `agent_endpoint()` and `close()`. The bridge will pass kernel-observed vsock peer CID, never a request field. Host CID is exactly 2. Use the reviewed codec and keep all replies strict newline JSON, under 4 KiB, generic errors with no credentials or exception text. The caller owns the connection; each serve method ends the operation and closes it. ACK is `{"schema_version":1,"status":"ready_for_payload"}`. Prepare success is `{"schema_version":1,"status":"agent_ready","session_id":"..."}`. Install success is `{"schema_version":1,"status":"installed","session_id":"...","launched":false}` (or true). Failure is a fixed schema/version/status/error code chosen from the module; never include headers or backend output.

Acquire one global nonblocking upload slot before ACK, covering preparation or installation through the backend result. Only host CID 2 may submit anything. Preparation requires operation `prepare_agent`, distinct RPC/install credentials, no live/preparing session and a live non-closed owner. Installation requires `install`, ready state, backend liveness, exact session UUID and constant-time install-token comparison before ACK/body. Rejection must not consume payload or invoke the backend. Keep credentials private and absent from repr/status/returned endpoint. Agent endpoint is fixed loopback plus the validated backend-created forward port and is available only while ready/alive; it contains no token. A lost backend invalidates the session and requires guest restart rather than adopting another process.

Preparation's whole deadline is finite and capped at 180 seconds; its header/body retain the codec's single 30-second cap. Installation has the same whole-operation bound. Backend calls receive only the remaining absolute deadline; they are responsible for respecting it, and the session checks it again before publishing success. All socket writes are bounded by the remaining deadline. No shell/ADB/raw command/serial/destination selector belongs in this layer. Staged contexts stay open until backend return and always close. On preparation failure, close the backend and mark the owner terminal; do not attempt to reuse partially created resources. Normal close is idempotent, rejects new admissions, closes all admitted sockets to interrupt reads/writes, cancels the backend, and allows no success after closure. Track in-flight calls with a condition; offer bounded drain for shutdown without holding a lock while invoking backend work. A timed-out drain is an error, never a successful ownership release.

Use real socketpairs and a fake backend/clock for stdlib unittest coverage: wrong CID; strict op; distinct secrets; invalid auth/session; rejection before ACK/payload/backend; ready response only after authenticated backend preparation; second preparation; one active upload; install/optional launch; expired deadlines; malformed/truncated upload; backend failure/loss; close during upload/backend; no credentials in errors/repr; context/connection cleanup and bounded drain. These tests prove session contracts, not Android installation or vsock identity. Do not modify bridge/builder/codec or claim runtime acceptance. Run tests red before implementation, then focused session/codec/guest suites, compile and diff checks; commit only the two owned files.
