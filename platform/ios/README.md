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

The prototype runs as an XCTest fixture. MCP transport and real app
qualification are future work. See the [iOS research plan](../../docs/research/ios.md)
and Apple's [XCUIAutomation reference](https://developer.apple.com/documentation/XCUIAutomation).
