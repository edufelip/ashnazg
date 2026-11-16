# Native MCP adapter

The library exposes two native session entry points:

```rust,ignore
ashnazg::mcp::serve_native_io(session, reader, writer, cancel).await?;
ashnazg::mcp::serve_native_http(session, loopback_listener, http_bearer, cancel).await?;
```

`serve_native_io` owns its asynchronous reader and writer. It works with an
application-owned transport such as a duplex stream; the library intentionally
does not wrap Tokio's standard input. On macOS, Tokio's standard input uses an
uninterruptible blocking read, so this API does not promise signal-safe CLI
shutdown.

The HTTP listener must already be bound to a loopback address. Its bearer is
independent of the guest credential and must be a nonempty, valid HTTP header
value no longer than 4096 bytes.

Both entry points derive guest and install clients from the consumed
`NativeSession`. The `serial` result is the session's validated host ADB relay
address. Native installation does not consult `ASHNAZG_TOKEN`,
`ANDROID_SERIAL`, host ADB, aapt, or the Android SDK. Launch without an explicit
package infers the package from the same APK descriptor that is hashed and
uploaded. Explicit package and no-launch requests skip inference.
The adapter preserves the installer's 30-second upload and 180-second result
budgets; installation and launch can finish after the upload phase ends.

On EOF, HTTP shutdown, cancellation, serving failure, or helper exit, the
adapter closes native install admission, cancels request work, joins tracked
install operations, and then joins the helper. A normal awaited return means
that cleanup has joined. Dropping the public future cancels its owned lifecycle;
the detached lifecycle owner continues teardown while the Tokio runtime remains
alive. Destroying the Tokio runtime itself is outside this guarantee.
Serving-phase panics are caught while the session remains owned, and helper
shutdown is joined before returning the serving failure. HTTP cancellation also
interrupts request-body handling, so an unfinished body cannot hold helper
teardown open.

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
fixture evidence. Cold starts, actual stdio/CLI interruption, automatic CLI setup,
live acquisition and release distribution remain separate work. The driver reports
its credential scan; retained files do not independently attest the external
runtime environment or the secret values used by that scan.
