# Native APK installation client

`native_install::NativeInstallClient` installs an APK through the native helper's
authenticated loopback relay. It uses guest ADB through the existing fixed guest
backend; it never runs host ADB. The caller supplies an already-owned endpoint,
session ID and installation token. Credential discovery, protected-file validation
and binding those values to a spawned helper are now provided by the explicit
[native session owner](native-session.md).

The client opens one regular APK descriptor without following a final symlink or
blocking on a FIFO, hashes it, waits for the guest's payload ACK and streams that
same descriptor. It verifies the streamed hash, half-closes the upload and requires
a matching installation result followed by EOF. Requests are never replayed.
An optional validated package requests launch after installation; the caller must
infer that package from the APK if its user-facing API offers automatic inference.

One blocking worker owns the file and socket. Upload has a 30-second deadline from
call entry; the result has a 180-second limit, both capped by the caller's absolute
deadline. Nonblocking I/O uses waits of at most 50 ms and checks deadlines after
I/O, including final EOF. Cancelling or dropping a call cancels its child token;
the worker retains cleanup ownership. Connect cancellation can wait for its single
bounded connect attempt, at most five seconds. Filesystem calls are cooperative
checkpoints rather than preemptible operations. Errors and Debug omit credentials
and guest response text.

Local verification passed 88 Rust tests, formatting and Clippy. The deadline
regression first reproduced acceptance of a late EOF, then passed after the
nonblocking-I/O repair. Fixture server waits are bounded, and the backpressure
fixture holds the receiver until the client expires.

The [native restart check](evidence/native-rust-install-618cc59/receipt.json) tested
client source `618cc59` against the previously qualified immutable `0155f64` guest.
The Rust client installed and launched all three native, Compose and React Native
fixtures. The subsequent 28 RPC and 7 MCP compatibility checks passed, with no host
ADB or offline guest changes. The helper exited zero, removed its credential and
closed its listeners. The receipt binds the driver, test-only probe, binary hashes
and matching guest verification. Existing Python harness requests tested invalid
authentication and corrupt-upload rejection; those requests were not sent by the
Rust client.

This qualifies the tested library-to-helper installation path on one owned restart.
That earlier probe's credential reader is test scaffolding. The explicit native
session owner now supplies the production credential binding and installation
client factory. The CLI, MCP `app_install` and release acquisition do not use this
session path yet.
There is no new cold-start, release, comparative performance or CPU claim.
