# iOS Simulator control probe

This prototype uses a dedicated SwiftUI fixture and XCTest to test three
concepts from the Android MCP interface. A semantic selector resolves exactly
one enabled control, one tap changes the fixture counter, and a subsequent
query and screenshot capture the new state. A separate test checks text entry,
and the fixture carries a synthetic secure field for redaction checks.
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
Simulator points. The tests type synthetic fixture values only and touch no
real app accounts; the secure field holds a synthetic sentinel, never real
credentials.

Both XCTest cases passed on hosted macOS using Xcode 26.6 and iOS 26.5.
The run produced an XCResult with semantic and screenshot attachments.
[Hosted Simulator run](https://github.com/edufelip/ashnazg/actions/runs/44289589457)

## MCP fixture session (macOS only)

`ashnazg mcp --ios-fixture` is the explicit entry point for the milestone 3
bridge. It opens one live XCTest fixture session — a disposable simulator with
a unique name, a private copy of these fixture sources, and an
`xcodebuild test` process whose `McpBridgeTests` bridge test answers live
`ui_snapshot`, `ui_act` (tap and type), and `ui_screenshot` operations over a
capability-token-guarded loopback socket — and serves exactly those three
tools over stdio MCP with the frozen production schemas. The default Android
server is unchanged; on Linux the flag refuses to start. A `type` request
carries its payload on a wire field separate from the selector text, must
resolve to one uniquely matched enabled hittable editable field, appends onto
existing nonempty text like the Android `type` action, settles on the
observed value, and returns a fresh snapshot; every other action is rejected
with the same invalid-params boundary as before.

The bridge settles a tap only after XCTest observes the counter change, and
every response carries a session sequence id: the post-tap snapshot, its
screenshot metadata (including the live counter label), and the screenshot
sequence all prove they describe the same tapped state. A failed open or
close preserves the work directory as evidence; a successful close awaits and
verifies deletion of the owned simulator.

```sh
cargo build --locked
fixture_evidence=$(mktemp -d)
python3 platform/ios/verify_mcp_fixture.py \
  --binary target/debug/ashnazg --output-dir "$fixture_evidence"
```

The verifier drives the real binary through `initialize`, `tools/list`, and
`tools/call`: snapshot (the tap and type selectors come from this response),
screenshot, tap, screenshot, text entry typed twice to prove append onto
existing nonempty text plus an empty-payload no-op, a direct fresh snapshot,
a synthetic sentinel typed into the secure field with its absence from every
snapshot and retained artifact asserted, bad-input rejections (a non-editable
target, a selector with no match, type without text), plus clean rejections
of the schema-valid but unmapped `clear` action and the disabled `app_install`
tool. It asserts the distinct accessibility state (`Count: 0` -> `Count: 1`),
PNG evidence that differs before and after, correlation of the screenshot
with the post-tap state, exit on EOF, and verified cleanup of the owned
simulator and work directory. It writes a sanitized receipt (including two
single-run monotonic fixture timings with their exact boundaries) and raw
artifacts to the output directory.

## Scope and unproven cases

Text entry is fixture-scoped. XCUIAutomation exposes no editable flag, so
the bridge routes payloads only to uniquely matched, enabled, hittable native
text field elements. An empty field may report its placeholder as the value;
the bridge normalizes that to empty, and the native probe asserts the raw
report is empty or exactly the placeholder before typing. Repeated append
assumes the caret rests at the end after the first entry; the settle fails
loudly if it does not, so this stays unproven until the hosted repeated
append passes. `XCUIElement.typeKey(_:modifierFlags:)` with
`XCUIKeyboardKey.end` exists in the API surface, but availability does not
prove the key jumps the caret to the end on iPhone. The secure settle observes
a native mask of the typed length only; it never reads or echoes content, and
the typed value never appears in a predicate. Redaction is proven for bridge
snapshots and the evidence this verifier owns; native `xcodebuild`/`xcresult`
diagnostics retained by Xcode are not scanned and stay unverified. Nothing
here says production credentials are safe.

The prototype runs as an XCTest fixture. Real app qualification is future
work. See the [iOS research plan](../../docs/research/ios.md)
and Apple's [XCUIAutomation reference](https://developer.apple.com/documentation/XCUIAutomation).
