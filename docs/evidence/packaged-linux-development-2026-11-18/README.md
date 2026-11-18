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

A preceding development run with the same packaged CLI installed the frozen
Meer DEV build and reversed the mock API and Metro ports. Packaged MCP read the
login screen, filled its email field with a synthetic value, confirmed the
value in a new snapshot, captured a 57,225-byte PNG, and returned app logs.
scrcpy rendered a 720×1280 texture on initial connection, after reconnect, and
during MCP input. The Device panel's separate Android snapshot helper failed
to parse a response and reported a helper process crash on retry.
[Meer and viewer receipt](meer-mcp.json)

A focused follow-up with scrcpy 3.3.4 verified desktop input after reconnect
on a disposable guest. The operator clicked the fixture counter once in
scrcpy. Independent Device panel screenshots showed the counter advance from
0 to 1, and the fixture emitted one `count=1` event. The viewer, guest,
ADB registration, and owned listeners were cleared. [Input receipt](scrcpy-input.json)

A follow-up ran the packaged workflow twice with one private state directory
and two guest identities. Meer logged in through the mock-backed Detox journey
on the first boot. On the next boot, the preparation helper reinstalled the
same APK with data retention, and Detox reached the authenticated Home without
logging in again. The first successful MCP snapshot arrived 16.07 s and 13.07 s
after the two session launches. These are individual Android-session timings.
[Persistence and timing receipt](meer-persistence.json) · [Resume test](meer-resume.e2e.js)

Cross-version app upgrades still need a live run. The native Apple Silicon Meer journey is documented in
[real app development acceptance](../../real-app-development.md).
