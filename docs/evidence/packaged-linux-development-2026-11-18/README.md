# Packaged Linux development acceptance

Two sessions booted concurrently on Linux x86_64 with separate private Binderfs
instances and independently allocated host ADB agent relays. Both initialized
the v0.3.0 packaged CLI over stdio, listed all five MCP tools and returned a
live UI snapshot. The second session returned another live snapshot after the
first exited. Both exited cleanly. [MCP checks](parallel-mcp.json) ·
[Relay assignments](parallel-routing.json)

The two exact-source supervisor receipts record the pinned Android 15 image,
Android readiness intervals and cleanup. Docker no longer knew either
container, the owned ADB forwards were absent and neither ADB TCP port was
listening. [Session A](session-15641.json) · [Session B](session-15642.json) ·
[Package and launcher hashes](inputs.json)

A preceding development run used the same packaged CLI with a frozen Meer DEV
APK. Packaged MCP read the login screen, filled its email field with a
synthetic value, confirmed the value in a new snapshot, captured a 57,225-byte PNG, and returned app logs.
scrcpy rendered a 720×1280 texture on initial connection, after reconnect, and
during MCP input.
[Meer and viewer receipt](meer-mcp.json)

A focused follow-up with scrcpy 3.3.4 verified desktop input after reconnect
on a disposable guest. The operator clicked the fixture counter once in
scrcpy. Independent Device panel screenshots showed the counter advance from
0 to 1, and the fixture emitted one `count=1` event. The viewer, guest,
ADB registration, and owned listeners were cleared. [Input receipt](scrcpy-input.json)

A follow-up ran the packaged workflow twice with one private state directory
and two guest identities. Meer logged in through the mock-backed Detox journey
on the first boot. On the next boot, the preparation helper reinstalled the
same APK with data retention. Detox found Home’s Search control and the login
form was absent. The resume test did not inspect account identity. The first
successful MCP snapshot arrived 16.07 s and 13.07 s
after the two session launches. These are individual Android-session timings.
[Persistence and timing receipt](meer-persistence.json) · [Resume test](meer-resume.e2e.js)

A subsequent run upgraded a separately built Meer DEV APK from Android
version code 1 to 2 across two boots using the same private state. Android
reported the original install timestamp, and Detox reached Home without a
login action. Account identity after the update was outside the resume assertion. Both APKs use the same signing certificate and
DEV version name. This run covers the Android upgrade transaction and post-update Home
assertion for this pair. [Upgrade receipt](meer-upgrade.json) · [Replay instructions](meer-upgrade-replay.md)

The native Apple Silicon Meer journey is documented in
[real app development acceptance](../../real-app-development.md).
