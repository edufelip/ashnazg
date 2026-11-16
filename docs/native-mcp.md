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

The adapter is library-level fixture qualified. This document does not claim
native helper, VM, device, CLI interruption, or release acquisition acceptance.
