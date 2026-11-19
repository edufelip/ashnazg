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

## External iOS app session (macOS only)

`ashnazg mcp --ios-app <manifest.json>` is the explicit entry point for
driving an external, already-built app on a disposable simulator. It is
mutually exclusive with `--ios-fixture` and serves stdio only; on Linux it
refuses to start. One live session reuses the same machinery as the fixture
bridge — the owned simulator with a unique name, the capability-token
loopback transport, the sequencing/poisoning rules, and the same
`McpBridgeTests` bridge case, which takes an external launch branch when
`ASHNAZG_APP_BUNDLE_ID` is present. The fixture `typeText` helper is
byte-identical to the published source.

The manifest is camelCase JSON with unknown fields rejected:

| Field | Meaning |
| --- | --- |
| `appPath` | the `.app` bundle directory to install |
| `appSha256` | REQUIRED pinned digest of the whole bundle tree (below) |
| `bundleId` | must equal the staged app's `CFBundleIdentifier` |
| `runtime` + `deviceType` | explicit identifiers; the pair is qualified against the selected available runtime's own `supportedDeviceTypes`, never by separate presence |
| `initialUrl` | optional loopback Metro URL handed to the app argv |
| `readySelector` | REQUIRED: exactly one of `testId` or `text` |
| `deepLinks` | exact allowlist; each entry's scheme must be declared in the staged app's `CFBundleURLTypes` |

Every wrong-bundle, wrong-platform, wrong-digest, wrong-path and wrong-URI
failure happens before the simulator exists. The bundle is staged into an
owned work directory while its bytes are hashed, so the digest pins exactly
what `simctl install` installs; relative in-tree symlinks are preserved,
while escaping, absolute or dangling links are rejected. The staged
`Info.plist` is read with `plutil`; the executable must be one plain
bundle-root basename; `lipo -archs` must contain the host's native slice
(universal simulator builds pass) and `xcrun vtool -arch <native>
-show-build` must report the iOSSimulator platform — compared
case-insensitively on the exact platform line, with the numeric platform 7
accepted. Then the owned device is created with the explicit pair, booted
with a bounded `bootstatus` wait, the pinned copy installed within a
bounded deadline, and the bridge handshake asserts the manifest bundle id.

`appSha256` is the Meer `hashApp` convention
(`apps/mobile/ios/ci_scripts/debug-smoke-proof.cjs` at
e6aca58675085368f1eb21fa92db4446e8581e6b): entries sort by name in UTF-16
code-unit order (non-UTF-8 names are rejected), each entry contributes the
JSON frame `["<relative path>",<mode & 0o777>,<directory ? 0 : size>]`
followed by file bytes or the read symlink target, so retargeted symlinks
and changed modes rebind the digest. Compute it with that script
(`node -e '…hashApp(app)…'`); two recorded cross-tool reference digests in
`src/ios_app.rs` pin the parity.

The external launch uses `XCUIApplication(bundleIdentifier:)`. When
`initialUrl` is supplied the app argv gains `--initialUrl <url>` plus the
Expo dev-menu disable flags (`-EXDevMenuShowsAtLaunch NO`,
`-EXDevMenuIsOnboardingFinished YES`,
`-EXDevMenuShowFloatingActionButton NO`), matching the Expo dev-client argv
convention (expo/expo#33235; EXDevLauncherController reads exactly
`--initialUrl`). The established app recipe keeps its own
`?disableOnboarding=1` marker on the loopback URL and this slice accepts
that URL unchanged — the extra controlled launch inputs beyond the URL
itself are exactly those three dev-menu argv flags. Readiness is bounded:
the app must own the foreground and the ready selector must resolve to
exactly one usable element. A tap settles on that observed foreground
readback — there is no fixture counter in the external path — and
`deep_link` opens the host-allowlisted URI via availability-gated
`XCUIApplication.open` (iOS 16.4+).

## Scope and unproven cases

The external app session returns observed state only: an unchanged
post-tap readback is honest success-with-unchanged-UI, and nothing here
asserts an app journey such as a login passing. Ambiguous selectors are
rejected and never replayed. The `--initialUrl` and dev-menu argv
conventions require an Expo dev-client build and stay unproven until the
hosted macOS run records a real receipt; the shared scheme also builds the
in-tree fixture app alongside the bridge runner, and the absolute-symlink
rejection keeps the installed copy relocatable.

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
