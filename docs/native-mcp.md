# Native MCP adapter

The library exposes ready-session and initializing native entry points:

```rust,ignore
ashnazg::mcp::serve_native_io(session, reader, writer, cancel).await?;
ashnazg::mcp::serve_native_http(session, loopback_listener, http_bearer, cancel).await?;

ashnazg::mcp::serve_initializing_native_io(initialize, reader, writer, cancel).await?;
ashnazg::mcp::serve_initializing_native_http(initialize, loopback_listener, http_bearer, cancel).await?;
```

The initializing calls accept one `FnOnce(CancellationToken) -> Future`
whose output is `anyhow::Result<NativeSession>`. They start MCP transport
immediately and retain that future in a lifecycle task. `initialize` and
`tools/list` do not wait for native startup. Tool calls share its single
readiness state: each waits up to 30 seconds and receives a fixed retryable
still-starting or startup-failed response. At most eight tool calls may wait
for readiness at once. Request cancellation and readiness timeout affect only
that request; they do not cancel initialization. Initialization failure leaves
the MCP transport available until transport shutdown, with no external-backend
fallback.

An already-cancelled tool request cannot acquire a ready backend. Readiness
notifications share the same absolute deadline, and leaving a waiter releases
its admission slot. The HTTP fixture performs a successful authenticated tool
call against a test-owned guest peer.

`serve_native_io` owns its asynchronous reader and writer. It works with an
application-owned transport such as a duplex stream; the library intentionally
does not wrap Tokio's standard input. On macOS, Tokio's standard input uses an
uninterruptible blocking read, so this API does not promise signal-safe CLI
shutdown. The [native CLI wrapper](native-cli.md) separately uses the disposable
stdio bridge and joins it on signal or protocol shutdown.

The HTTP listener must already be bound to a loopback address. Its bearer is
independent of the guest credential and must be a nonempty, valid HTTP header
value no longer than 4096 bytes.

All native entry points derive guest and install clients from the owned
`NativeSession`. The `serial` result is the session's validated host ADB relay
address. Native installation does not consult `ASHNAZG_TOKEN`,
`ANDROID_SERIAL`, host ADB, aapt, or the Android SDK. Launch without an explicit
package infers the package from the same APK descriptor that is hashed and
uploaded. Explicit package and no-launch requests skip inference.
The adapter preserves the installer's 30-second upload and 180-second result
budgets; installation and launch can finish after the upload phase ends.

On EOF, HTTP shutdown, cancellation, serving failure, or helper exit, the
adapter closes native install admission, cancels request work, joins tracked
install operations, and then joins the helper. During initialization shutdown,
the lifecycle owner first awaits initializer cleanup; if initialization returns
a session, it then joins that session's shutdown. A normal awaited return means
that cleanup has joined. Dropping the public future cancels its child lifecycle;
the detached owner continues teardown while the Tokio runtime remains alive.
The caller's parent token is not cancelled. Destroying the Tokio runtime itself
is outside this guarantee. Serving-phase panics are caught while the session is
retained, and helper shutdown is joined before returning the serving failure.
HTTP validates its already-bound loopback listener and bearer before invoking
the initializer. HTTP cancellation also interrupts unfinished request bodies,
so they cannot hold helper teardown open.

Initializer callbacks must honor their cancellation token and finish cleanup
of resources they own before returning. Callback construction/polling failures
become fixed startup errors; serving construction/polling failures retain any
returned session through joined teardown. A serving failure remains an error
when initialization subsequently ends through cancellation. EOF before MCP
initialization is likewise reported as a serving failure after cleanup.

The [retained native MCP acceptance](evidence/native-mcp-acceptance-da745f5/receipt.json)
tested host source `da745f5` against the retained `0155f64` guest and pinned,
ad-hoc-signed helper. Two restarts of the existing owned state each passed 19
checks through 30 MCP tool calls. They covered authenticated HTTP initialization,
all five tools, inferred and explicit fixture installs, no-launch behavior,
native/Compose/React Native tap and Unicode input/clear, PNG images and semantic
marks, navigation, logs, and invalid installation requests. Host ADB and aapt
were absent from the test PATH; neither `ASHNAZG_TOKEN` nor `ANDROID_SERIAL` was
provided.

One run requested normal shutdown; the other delivered SIGTERM to the acceptance
controller after its request sequence. Both joined clean helper shutdown, reaped
the helper, removed credentials and closed the three helper listeners plus HTTP.
The caller's session parent token remained uncancelled. State and disk filesystem
identities were preserved; their bytes are not claimed unchanged.

The helper's ready receipts report 39.770 and 40.522 seconds. Complete controller
runs, including checks and shutdown, took 49.824 and 49.509 seconds. These are
different measurement intervals from two restarts, not a startup distribution or
performance claim. The evidence also retains an initial controller failure with
zero completed checks; the client's SSE decoder was repaired before the passing
repeats, without changing host or guest code.

Generic-I/O EOF, in-flight cancellation, caller drop and panic recovery remain
fixture evidence. Initializing MCP tests are fixture evidence only; they do not
establish CLI or VM startup, acquisition, cold-start performance, or a released
runtime. Actual stdio/CLI interruption, automatic CLI setup, live acquisition
and release distribution remain separate work. The driver reports
its credential scan; retained files do not independently attest the external
runtime environment or the secret values used by that scan.
