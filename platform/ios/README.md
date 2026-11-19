# iOS Simulator control probe

This prototype uses a dedicated SwiftUI fixture and XCTest to test three
concepts from the Android MCP interface. A semantic selector resolves exactly
one enabled control, one tap changes the fixture counter, and a subsequent
query and screenshot capture the new state. A separate test checks text entry.
Screenshots and a small synthetic semantic snapshot are attachments in the
XCTest result bundle.

Use macOS with Xcode, an installed iOS Simulator runtime, and XcodeGen. Select a
dedicated simulator in the Device panel and use its exact UDID for the test
destination.

```sh
cd platform/ios
xcodegen generate
xcodebuild test \
  -project AshnazgSimulatorProbe.xcodeproj \
  -scheme AshnazgSimulatorProbe \
  -destination "platform=iOS Simulator,id=$ASHNAZG_PROBE_SIMULATOR" \
  -parallel-testing-enabled NO \
  -resultBundlePath "$ASHNAZG_PROBE_RESULT"
```

Use a new result path for each run. A passing test should retain the fixture
snapshot with `Count: 1` and two different screenshots. Coordinates are
Simulator points. The tests carry a synthetic text value and avoid real app
accounts and secure fields.

Both XCTest cases passed on hosted macOS using Xcode 26.6 and iOS 26.5.
The run produced an XCResult with semantic and screenshot attachments.
[Hosted Simulator run](https://github.com/edufelip/ashnazg/actions/runs/44289589457)

## MCP fixture session (macOS only)

`ashnazg mcp --ios-fixture` is the explicit entry point for the milestone 3
bridge. It opens one live XCTest fixture session — a disposable simulator with
a unique name, a private copy of these fixture sources, and an
`xcodebuild test` process whose `McpBridgeTests` bridge test answers live
`ui_snapshot`, `ui_act` (tap), and `ui_screenshot` operations over a
capability-token-guarded loopback socket — and serves exactly those three
tools over stdio MCP with the frozen production schemas. The default Android
server is unchanged; on Linux the flag refuses to start.

The bridge settles a tap only after XCTest observes the counter change, and
every response carries a session sequence id: the post-tap snapshot, its
screenshot metadata (including the live counter label), and the screenshot
sequence all prove they describe the same tapped state. A failed open or
close preserves the work directory as evidence; a successful close awaits and
verifies deletion of the owned simulator.

```sh
cargo build --locked
python3 platform/ios/verify_mcp_fixture.py \
  --binary target/debug/ashnazg --output-dir <empty owned dir>
```

The verifier drives the real binary through `initialize`, `tools/list`, and
`tools/call`: snapshot (selector for the tap comes from this response),
screenshot, tap, screenshot, plus clean rejections of the unsupported `type`
action and the disabled `app_install` tool. It asserts the distinct
accessibility state (`Count: 0` -> `Count: 1`), PNG evidence that differs
before and after, correlation of the screenshot with the post-tap state, exit
on EOF, and verified cleanup of the owned simulator and work directory. It
writes a sanitized receipt and raw artifacts to the output directory.

The prototype runs as an XCTest fixture. Real app qualification is future
work. See the [iOS research plan](../../docs/research/ios.md)
and Apple's [XCUIAutomation reference](https://developer.apple.com/documentation/XCUIAutomation).
