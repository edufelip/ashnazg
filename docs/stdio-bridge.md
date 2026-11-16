# Owned stdio bridge

`OwnedStdioBridge` is a Unix-only adapter for a future native CLI bootstrap. It keeps blocking inherited stdio out of the MCP/session owner by moving it into a short-lived copy-only child process. It does not change MCP backend selection or the current default CLI behavior.

## Ownership and lifecycle

`spawn` borrows and validates its input and output descriptors, then takes private descriptor clones for the bridge child. Input must be readable; output must be writable. Supported endpoints are pipes, local Unix stream sockets, and terminal descriptors. Datagram and non-local sockets, regular files, non-terminal character devices, invalid descriptors, and reversed access directions are rejected before the child starts. The bridge requires an active Tokio runtime.

The parent and child communicate over a private Unix stream socketpair with bounded kernel buffering. Only the child endpoint is deliberately inherited. The child receives fixed internal arguments, no inherited environment, and no stderr diagnostics. Its protocol is raw bytes; it accepts no commands, paths, or caller-supplied process identifiers. The copy buffers are fixed at 16 KiB.

`take_io` transfers the parent IPC stream once as owned async read/write halves. The read half carries bytes read from inherited stdin; the write half carries bytes to inherited stdout. Stdin EOF half-closes the child-to-parent direction after buffered bytes. A stdin read error terminates the bridge with failure rather than reporting a normal EOF. Parent write EOF ends the child output loop, even if its stdin reader is blocked.

One Tokio lifecycle task owns the child handle. Cancellation or owner drop shuts down private IPC, kills the bridge child, and waits up to two seconds for reap. Dropping the owner detaches this cleanup task, which continues while its Tokio runtime remains alive. `shutdown().await` cancels and joins that task; it returns only after reap succeeds. Unexpected nonzero child exit is reported as a generic error. Child cleanup never cancels the caller's `CancellationToken` and never signals a caller-supplied PID.

## Descriptor and terminal behavior

The bridge does not call `F_SETFL` on inherited descriptors and preserves their `O_NONBLOCK` setting. Darwin can expose additional kernel-maintained flags after a write even without the bridge, so raw `F_GETFL` equality is not promised. The child retries nonblocking inherited stdio with `poll`; if readiness becomes stale on a blocking descriptor, any resulting blocked operation is confined to the disposable child and remains cancellable by killing that process. Parent IPC alone is configured nonblocking for Tokio.

Terminal line discipline remains active. Canonical input may wait for a newline, and XOFF may stop output. There is no prompt and the bridge does not alter terminal settings. These conditions can block only the isolated child; cancellation still kills and reaps it.

Rust's actual child executable is used to exercise closed-output behavior. On the tested host, a closed output pipe produced a child-side failure while the parent process survived and joined the child. The tests do not inspect or modify live process standard descriptors, and they do not claim VM or native MCP acceptance.

## Evidence boundary

The `stdio_bridge` integration fixtures use only newly created pipes, socketpairs, a test-owned PTY, and the actual `ashnazg` executable. They cover idle cancellation, EOF and bidirectional draining, a 256 KiB ordered burst, a full output pipe, a competing pipe reader, closed output, canonical/XOFF terminal cancellation, rejected descriptors, status-flag preservation, owner-drop cleanup, unexpected child exit, malformed hidden mode, and public help visibility. The output tests wait for actual child activity and sustained IPC backpressure before cancellation; the PTY test sends the configured stop character and confirms output is suppressed. A fault fixture gives the hidden child a directory as stdin to force a real read error and require a failing exit; the public parent API separately rejects that descriptor type.

Passing these host fixtures proves the bounded bridge foundation on that host. It does not qualify the native MCP serving integration, VM lifecycle, device behavior, or another Unix platform's descriptor and terminal semantics.
