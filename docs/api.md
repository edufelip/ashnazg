# v0.1 MCP API

Start `./target/release/ashnazg mcp` after starting the Android agent and forwarding its port. The host requires `ASHNAZG_TOKEN`. HTTP uses `/mcp` at the explicitly configured loopback address and additionally requires `ASHNAZG_HTTP_TOKEN` as a Bearer credential. Protocol negotiation is handled by the official Rust MCP SDK.

## Snapshots and targets

`ui_snapshot` accepts `{ "includeInvisible": false }`. It returns `snapshotId`, `nodes`, and `truncated`. Nodes carry IDs, roles, text, rectangles, enabled/focused/clickable/checkable/checked/selected/scrollable/password state, and available test IDs, hints and descriptions. Password values are omitted.

A target contains exactly one of:

```json
{"testId":"com.example.app:id/submit"}
{"testId":"compose-button"}
{"text":"Continue"}
{"id":3,"snapshotId":42}
```

Qualified resource IDs preserve their package. Unqualified native ID aliases must be unique. Text matching is exact and ambiguous matches fail. Numeric IDs require the current snapshot, including the ID from a screenshot's mark metadata. Take a new snapshot after external UI changes; stale/recycled nodes are rejected when detected.

## Actions

`ui_act` accepts these argument shapes:

```json
{"action":"tap","target":{"testId":"submit"}}
{"action":"long_press","target":{"text":"Hold"}}
{"action":"type","target":{"testId":"input"},"text":"Hello 你好"}
{"action":"clear","target":{"testId":"input"}}
{"action":"scroll_to","target":{"text":"Destination"},"direction":"down"}
{"action":"back"}
{"action":"deep_link","text":"example://route"}
```

`type` appends through accessibility text actions; it does not synthesize individual keyboard key events. Clear a nonempty password field before typing, because its existing value may be masked. Scroll direction defaults to down and search stops after twelve swipes.

The result contains `{ "snapshot": ..., "settled": true }`. Settling uses bounded state/navigation checks and a 120 ms accessibility quiet window; it does not establish that network requests or every asynchronous task have finished. A false `settled` value or transport error may follow an action that already executed: inspect the state before taking another action. The host never automatically replays mutations.

## Screenshots and logs

`ui_screenshot` accepts `{ "format": "webp", "somOverlay": true }`; PNG is also supported. MCP returns image content plus JSON metadata (`snapshotId`, marks, width, height, MIME type). Numbered marks refer to that screenshot's snapshot ID. The implementation uses public bitmap capture/encoding, including copies.

`app_logs` accepts `{ "tagFilter": "MyTag", "sinceMs": 0 }`. Tags match literally; time is Unix epoch milliseconds. It returns a bounded 2,000-line ring and `truncated` status. A failed log source returns an unavailable error, rather than pretending there were no logs. The guest token is redacted from buffered lines.

## Installation

`app_install` accepts `{ "path": "/absolute/path/app.apk", "launch": true }`; launch defaults to false. An optional `package` supplies the launch package explicitly. Otherwise `aapt` discovers it; use `ASHNAZG_AAPT` or the Android SDK environment to select the tool.

`ANDROID_SERIAL` must select the installation device. Launch requires exactly one validated launcher activity; ambiguous or missing launchers fail explicitly. Install/launch is setup work via adb, separate from the persistent UI RPC path. An install result confirms that operation, not that a temporary system update overlay has disappeared; observe the UI before interacting.

`device_boot`, VM/container lifecycle, iOS and standalone legacy SSE are not advertised by this release. See [the wire contract](protocol-v0.1.md) and [the stack decision](adr-001-performance-stack.md).
