# Owned native sessions

`native_session::NativeSession` owns an explicit local helper session on Apple
Silicon macOS. Its caller supplies verified helper/APK hashes, the guest source
SHA, local artifact paths and three distinct loopback ports. It does not acquire
artifacts, discover an existing VM or configure the CLI/MCP server automatically.

The helper and APK must be regular current-user files under private directories.
Paths must be absolute and canonical, with no symlink components or traversal.
Ancestors must be owned by the current user or root and prohibit group/other
writes, except root-owned sticky temporary directories. Assets and the state
parent must be private owned directories; the final state directory may be
missing. Existing session credentials are refused. The supervisor retains
verified descriptors through spawn and rejects observed replacements; hostile
same-user mutation between the final check and named execution is outside this
boundary. Directory timestamps can change for unrelated entries; directory
identity, owner and permissions are checked separately from full file metadata.

One worker owns the helper process and both bounded nonblocking output pipes.
Startup validates its readiness receipt, binds private credentials to the
retained child PID and requested artifact/endpoints, then authenticates an agent
ping on the caller's Tokio runtime. The owner acknowledges activation before
`start` returns. Use the resulting `guest_client()` and `install_client()` on that
runtime. The former reuses the authenticated connection; the latter installs
through the fixed guest backend without invoking host ADB. The Swift helper
still verifies the kernel, rootfs template and agent APK bytes before readiness.
`adb_addr()` returns the non-secret host relay endpoint bound by that readiness
receipt, separately from the guest's internal Android serial. It is session
metadata and does not perform a liveness check.

The startup budget is the smaller of the caller deadline and configured helper
timeout. Passing readiness ends that startup deadline. `wait_terminated()` can be
cancelled without losing worker join ownership. Explicit shutdown, cancellation
or dropping the session requests cleanup of only the owned child. Session cleanup
does not cancel the parent token. The caller must translate application signals
and transport EOF into cancellation or shutdown; the library installs no global
process signal handlers.

Requested shutdown allows 220 seconds for helper cleanup, then sends SIGKILL and
allows five seconds for reap. Pipe limits remain enforced during cleanup,
including buffered output after process exit. Success requires an unforced zero
exit and absent credentials. Forced stops, unknown status and leftover
credentials are failures. The supervisor preserves state, logs and persistent
disks and does not remove leftover credentials. Recovery after forced shutdown
remains unqualified. Filesystem calls are cooperative deadline checkpoints and
cannot be preempted; existing `GuestClient` token storage is unchanged.

The [retained restart acceptance](evidence/native-owner-acceptance-5256b94/receipt.json)
used host source `5256b94`, the previously qualified `0155f64` guest and locally
pinned, ad-hoc-signed helper/APK inputs. It passed three installations and launches
through the session's Rust client factory, 28 RPC checks and seven legacy MCP
compatibility checks. No host ADB or offline guest patches were used. The owner
joined clean helper shutdown, left its parent token uncancelled, removed the
credential and closed all three owned listeners. The persistent disk and state
directory retained their filesystem identities; this does not assert unchanged
disk contents.

The helper's ready receipt reports 27.464 seconds; the driver observed readiness
after 38.572 seconds including preflight and host setup. The driver's 59.389-second
total includes checks and shutdown. These are separate intervals from one
restart, not performance distributions. The probe's dependency package identities
and checksums match the repository's runtime subset; only root test dependencies
`tempfile` and `fastrand` are absent. The pinned lock hashes were revalidated
before this repeat. The earlier package comparison was independently audited
before this run.

The driver hashes and checks the exact loaded harness file and prefix before
executing it, and records both hashes. An altered-file regression fails before
execution. This full repeat supersedes the earlier run whose driver did not
capture that binding; the earlier run is not retroactively attested.

This acceptance covers the explicit library session. Its seven MCP checks use
the existing token/address compatibility mode. The newer [native MCP acceptance](native-mcp.md)
separately covers library-level native `app_install` and HTTP session ownership.
Fresh-state startup with this supervisor, broader injected failures, automatic
CLI setup, actual CLI interruption, live release acquisition and distribution
remain separate work. No new benchmark, CPU-saving, Developer ID signing or
notarization claim is made.
