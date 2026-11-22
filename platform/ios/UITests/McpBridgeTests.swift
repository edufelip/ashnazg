import XCTest
import Network

// Bridge test for the ashnazg MCP fixture server. It launches the fixture app,
// connects back to the Rust host bridge over a loopback TCP socket guarded by
// a per-run capability token, and serves newline-delimited JSON operations
// until the host closes the session. Every operation runs live against
// XCUIAutomation on the main actor, so responses always reflect the current
// fixture state. The test skips itself when the host has not selected it, so
// the ordinary probe scheme run keeps exercising the two native tests.
final class McpBridgeTests: XCTestCase {
    @MainActor private var typingSelector: String?

    /// True when this session launches an external manifest-pinned app
    /// (`ashnazg mcp --ios-app`) instead of the in-tree fixture.
    @MainActor private var externalApp = false

    private struct Command: Codable {
        let op: String
        let id: UInt64?
        let action: String?
        let testId: String?
        let text: String?
        let value: String?
        let url: String?
        let direction: String?
    }

    private struct ReadySelector: Codable {
        let testId: String?
        let text: String?
    }

    @MainActor
    private func perform(_ command: Command, app: XCUIApplication) -> [String: Any] {
        switch command.op {
        case "ui_snapshot":
            return Self.snapshotResponse(app: app, includeSystemPrompts: externalApp)
        case "ui_act":
            switch command.action ?? "" {
            case "tap":
                typingSelector = nil
                if let testId = command.testId, !testId.isEmpty {
                    let query = app.descendants(matching: .any).matching(identifier: testId)
                    return Self.tapFirstMatch(
                        of: query, selector: "testId \(testId)", in: app, external: externalApp
                    )
                }
                if let text = command.text, !text.isEmpty {
                    if externalApp {
                        return Self.tapExternalText(text, in: app)
                    }
                    let query = app.descendants(matching: .any)
                        .matching(NSPredicate(format: "label == %@", text))
                    return Self.tapFirstMatch(
                        of: query, selector: "text \(text)", in: app, external: externalApp
                    )
                }
                return ["ok": false, "error": "tap requires a testId or text selector"]
            case "type":
                // The typed payload rides `value`, separate from the testId
                // or text selector.
                guard let value = command.value else {
                    return ["ok": false, "error": "type requires text"]
                }
                if let testId = command.testId, !testId.isEmpty {
                    let query = app.descendants(matching: .any).matching(identifier: testId)
                    return typeText(
                        into: query, selector: "testId \(testId)", value: value, in: app
                    )
                }
                if let text = command.text, !text.isEmpty {
                    let query = app.descendants(matching: .any)
                        .matching(NSPredicate(format: "label == %@", text))
                    return typeText(
                        into: query, selector: "text \(text)", value: value, in: app
                    )
                }
                return ["ok": false, "error": "type requires a testId or text selector"]
            case "deep_link":
                // External launches only: the host never forwards a deep
                // link to the fixture, and the fixture keeps its exact
                // unsupported-action boundary.
                guard externalApp else {
                    return ["ok": false, "error": "bridge only supports the tap and type actions"]
                }
                guard let raw = command.url, let url = URL(string: raw) else {
                    return ["ok": false, "error": "deep_link requires a url"]
                }
                typingSelector = nil
                return openDeepLink(url, in: app)
            case "scroll_to":
                // External launches only: the keyboard-compressed scroll
                // reveal is an app-journey action; the fixture keeps its
                // exact unsupported-action boundary.
                guard externalApp else {
                    return ["ok": false, "error": "bridge only supports the tap and type actions"]
                }
                guard command.direction == "down" else {
                    return ["ok": false, "error": "scroll_to requires the down direction"]
                }
                typingSelector = nil
                if let testId = command.testId, !testId.isEmpty {
                    let selector = "testId \(testId)"
                    return Self.scrollTo(
                        selector: selector,
                        root: { $0.descendants(matching: .any).matching(identifier: testId) },
                        in: app,
                        includeSystemPrompts: externalApp
                    )
                }
                if let text = command.text, !text.isEmpty {
                    let selector = "text \(text)"
                    return Self.scrollTo(
                        selector: selector,
                        root: {
                            $0.descendants(matching: .any)
                                .matching(NSPredicate(format: "label == %@", text))
                        },
                        in: app,
                        includeSystemPrompts: externalApp
                    )
                }
                return ["ok": false, "error": "scroll_to requires a testId or text selector"]
            default:
                if externalApp {
                    return [
                        "ok": false,
                        "error": "bridge only supports the tap, type, scroll_to, and deep_link actions",
                    ]
                }
                return ["ok": false, "error": "bridge only supports the tap and type actions"]
            }
        case "ui_screenshot":
            var response: [String: Any] = [
                "ok": true,
                "png": app.screenshot().pngRepresentation.base64EncodedString(),
            ]
            // Only the fixture owns a counter element; an external app
            // reports its observed image only.
            if !externalApp {
                response["counter"] = app.staticTexts["fixture-count"].label
            }
            return response
        default:
            return ["ok": false, "error": "bridge operation unsupported: \(command.op)"]
        }
    }

    /// One live tap on a uniquely matched enabled hittable element. The
    /// fixture launch settles on its observed counter change; an external
    /// app has no counter and settles on the observed foreground readback
    /// instead, returning fresh observed state — unchanged UI included —
    /// and never claiming the app's own journey succeeded.
    /// External-app text tap with fresh-install system prompts in play.
    /// Strict uniqueness is kept for normal app UI; the one documented
    /// exception is the OS AutoFill "Not Now" sheet, whose button plus its
    /// inner static text can both match a text selector in the app's extra
    /// window, so that label falls back to a buttons-only query; a
    /// Springboard alert button is the last resort.
    @MainActor
    private static func tapExternalText(_ text: String, in app: XCUIApplication) -> [String: Any] {
        let appQuery = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label == %@", text))
        let matches = appQuery.count
        if matches == 1 {
            return tapFirstMatch(of: appQuery, selector: "text \(text)", in: app, external: true)
        }
        if matches > 1, text == "Not Now", app.windows.count >= 2 {
            let buttonsOnly = app.buttons.matching(NSPredicate(format: "label == %@", text))
            if buttonsOnly.count == 1 {
                return tapFirstMatch(
                    of: buttonsOnly, selector: "text \(text)", in: app, external: true
                )
            }
        }
        if matches == 0 {
            let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
            let alertButtons = springboard.alerts.buttons
                .matching(NSPredicate(format: "label == %@", text))
            if alertButtons.count == 1 {
                let button = alertButtons.element(boundBy: 0)
                guard button.isEnabled, button.isHittable else {
                    return [
                        "ok": false,
                        "error": "selector text \(text) is not an enabled hittable element",
                    ]
                }
                button.tap()
                // Only re-activate when the alert actually took the
                // foreground; repeated activations open the App Switcher.
                if app.state != .runningForeground {
                    app.activate()
                }
                guard waitAppForeground(app) else {
                    return [
                        "ok": false,
                        "error": "app state was not observed in the foreground after the tap",
                    ]
                }
                return snapshotResponse(app: app, includeSystemPrompts: true)
            }
        }
        return [
            "ok": false,
            "error": "selector text \(text) matched \(matches) elements, expected exactly one",
        ]
    }

    @MainActor
    private static func tapFirstMatch(
        of query: XCUIElementQuery,
        selector: String,
        in app: XCUIApplication,
        external: Bool
    ) -> [String: Any] {
        let matches = query.count
        guard matches == 1 else {
            return ["ok": false,
                    "error": "selector \(selector) matched \(matches) elements, expected exactly one"]
        }
        let element = query.element(boundBy: 0)
        guard element.isEnabled, element.isHittable else {
            return ["ok": false, "error": "selector \(selector) is not an enabled hittable element"]
        }
        if external {
            element.tap()
            guard waitAppForeground(app) else {
                return [
                    "ok": false,
                    "error": "app state was not observed in the foreground after the tap",
                ]
            }
            return snapshotResponse(app: app, includeSystemPrompts: external)
        }
        let count = app.staticTexts["fixture-count"]
        let before = count.label
        element.tap()
        let settled = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label != %@", before),
            object: count
        )
        guard XCTWaiter().wait(for: [settled], timeout: 10) == .completed else {
            return ["ok": false, "error": "fixture state did not settle after the tap"]
        }
        return snapshotResponse(app: app, includeSystemPrompts: external)
    }

    /// One live text entry into a uniquely matched editable field. The tap
    /// focuses the field and `typeText` is genuine XCTest keyboard input;
    /// delivery types the first grapheme, observes that exact prefix, then
    /// sends the remainder once, so a lost or changed burst stops at the
    /// first unconfirmed prefix without ever replaying it. The settle
    /// observes the field value without ever echoing it. An empty
    /// payload settles as a no-op before any secure-text boundary; a nonempty
    /// payload onto existing secure text is rejected, mirroring the Android
    /// `type` boundary. Secure values never appear in snapshots or errors.
    @MainActor
    private func typeText(
        into query: XCUIElementQuery,
        selector: String,
        value: String,
        in app: XCUIApplication
    ) -> [String: Any] {
        let matches = query.count
        guard matches == 1 else {
            return ["ok": false,
                    "error": "selector \(selector) matched \(matches) elements, expected exactly one"]
        }
        let element = query.element(boundBy: 0)
        guard element.isEnabled, element.isHittable else {
            return ["ok": false, "error": "selector \(selector) is not an enabled hittable element"]
        }
        guard element.elementType == .textField || element.elementType == .secureTextField else {
            return ["ok": false, "error": "selector \(selector) is not an editable text element"]
        }
        // XCUI may report a placeholder as the value of an empty field; that
        // is not typed content.
        let reported = element.value as? String ?? ""
        let placeholder = element.placeholderValue ?? ""
        let existing = reported == placeholder ? "" : reported
        if value.isEmpty {
            // Parity with the shared schema: an empty payload settles as a
            // no-op with a fresh snapshot, never an error.
            return Self.snapshotResponse(app: app, includeSystemPrompts: externalApp)
        }
        if element.elementType == .secureTextField && !existing.isEmpty {
            // Mirrors the Android boundary on nonempty secure text; the
            // bridge offers no clear action to work around it.
            return ["ok": false,
                    "error": "selector \(selector) holds existing secure text; typing onto it is not supported"]
        }
        // Re-tapping a populated field can replace its previous value.
        // Only append when this bridge established focus through a settled type.
        let keepsFocus = typingSelector == selector
        if !existing.isEmpty && !keepsFocus {
            return ["ok": false, "error": "append requires focus established by the bridge"]
        }
        typingSelector = nil
        if !keepsFocus {
            element.tap()
            if !Self.awaitKeyboardFocus(element, timeout: 5) {
                guard element.isEnabled, element.isHittable else {
                    return [
                        "ok": false,
                        "error": "selector \(selector) is not an enabled hittable element",
                    ]
                }
                // One clean focus retry when a cold simulator tap returned
                // before the field latched first responder; no text was
                // typed yet.
                element.tap()
                guard Self.awaitKeyboardFocus(element, timeout: 5) else {
                    return [
                        "ok": false,
                        "error": "selector \(selector) did not receive keyboard focus",
                    ]
                }
            }
        } else if !Self.awaitKeyboardFocus(element, timeout: 2) {
            return ["ok": false, "error": "selector \(selector) lost keyboard focus"]
        }
        // Observed-prefix delivery: the empty-payload boundary above
        // guarantees `value` is nonempty. Type only the first grapheme,
        // observe that exact prefix, then send the remainder once. A single
        // bulk typeText lost or changed text on one real app; the prefix
        // observation bounds the damage and a rejected mutation is never
        // replayed.
        let prefix = String(value.first!)
        element.typeText(prefix)
        // The prefix observation mirrors the final settle: a plain field
        // settles on the exact observed prefix; a secure field settles on a
        // genuine native mask observation only — every character a mask
        // bullet, exactly one more than before — without reading or echoing
        // content.
        let prefixSettled: XCTNSPredicateExpectation
        if element.elementType == .secureTextField {
            prefixSettled = XCTNSPredicateExpectation(
                predicate: NSPredicate(
                    format: "value MATCHES %@ AND value.length == %d",
                    "^[•●*]+$",
                    existing.count + 1
                ),
                object: element
            )
        } else {
            prefixSettled = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "value == %@", existing + prefix),
                object: element
            )
        }
        guard XCTWaiter().wait(for: [prefixSettled], timeout: 10) == .completed else {
            // A rejected mutation is never replayed. The error classifies
            // the unconfirmed prefix with the observed new-character count
            // only; typed content is never echoed, matching the existing
            // redaction style.
            typingSelector = nil
            let postTyping = element.value as? String ?? ""
            let observed = postTyping == element.placeholderValue ? "" : postTyping
            let delta = observed.count - existing.count
            let classification: String
            switch delta {
            case ...0: classification = "no typed characters observed"
            case 1: classification = "one unconfirmed character observed"
            default: classification = "more characters than requested observed"
            }
            return [
                "ok": false,
                "error": "unconfirmed prefix after first grapheme: "
                    + "\(classification), requested 1, observed \(delta)",
            ]
        }
        let remainder = String(value.dropFirst())
        if !remainder.isEmpty {
            element.typeText(remainder)
        }
        let expected = existing + value
        // The settle never embeds the typed value in a predicate: XCTest may
        // retain predicate descriptions in native diagnostics. A plain field
        // settles on the observed content (the payload is the synthetic
        // fixture value); a secure field settles on a genuine native mask
        // observation only — every character a mask bullet, exactly the
        // typed length, mirroring the Android password mask comparison
        // without reading or echoing content.
        let settled: XCTNSPredicateExpectation
        if element.elementType == .secureTextField {
            settled = XCTNSPredicateExpectation(
                predicate: NSPredicate(
                    format: "value MATCHES %@ AND value.length == %d",
                    "^[•●*]+$",
                    value.count
                ),
                object: element
            )
        } else {
            settled = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "value == %@", expected),
                object: element
            )
        }
        guard XCTWaiter().wait(for: [settled], timeout: 10) == .completed else {
            return ["ok": false, "error": "fixture text did not settle after typing"]
        }
        typingSelector = selector
        return Self.snapshotResponse(app: app, includeSystemPrompts: externalApp)
    }

    /// True when the field or any of its descendants currently owns XCTest
    /// keyboard focus.
    @MainActor
    private static func hasKeyboardFocus(_ element: XCUIElement) -> Bool {
        if (element.value(forKey: "hasKeyboardFocus") as? Bool) == true {
            return true
        }
        return element.descendants(matching: .any)
            .matching(NSPredicate(format: "hasKeyboardFocus == true"))
            .firstMatch
            .exists
    }

    /// Bounded wait for `element` (or a descendant) to take keyboard focus
    /// before dispatching `typeText`.
    @MainActor
    private static func awaitKeyboardFocus(_ element: XCUIElement, timeout: TimeInterval) -> Bool {
        if hasKeyboardFocus(element) {
            return true
        }
        let direct = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "hasKeyboardFocus == true"),
            object: element
        )
        if XCTWaiter().wait(for: [direct], timeout: timeout) == .completed {
            return true
        }
        return element.descendants(matching: .any)
            .matching(NSPredicate(format: "hasKeyboardFocus == true"))
            .firstMatch
            .exists
    }

    /// One controlled reveal of a uniquely matched enabled element clipped
    /// by a compressed scroll container. A `down` scroll moves content
    /// upward: exactly one bounded swipe on the unique native scroll view
    /// containing the target, dispatched only when the target is not
    /// already visibly hittable, and the answer returns only after the
    /// exact target is hittable. No coordinates, no retry, no second
    /// swipe; an unresolved or ambiguous container fails closed.
    @MainActor
    private static func scrollTo(
        selector: String,
        root: (XCUIElement) -> XCUIElementQuery,
        in app: XCUIApplication,
        includeSystemPrompts: Bool
    ) -> [String: Any] {
        let query = root(app)
        let matches = query.count
        guard matches == 1 else {
            return ["ok": false,
                    "error": "selector \(selector) matched \(matches) elements, expected exactly one"]
        }
        let element = query.element(boundBy: 0)
        guard element.isEnabled else {
            return ["ok": false, "error": "selector \(selector) is not an enabled hittable element"]
        }
        if element.isHittable {
            // Already visibly hittable: a settled no-op with fresh observed
            // state, mirroring the empty-payload type boundary.
            return snapshotResponse(app: app, includeSystemPrompts: includeSystemPrompts)
        }
        // The target is clipped. The containing native scroll view resolves
        // by exact descendant matching — the public XCUI ancestry test — so
        // hidden elements still resolve and no coordinates are computed.
        let containers = app.scrollViews.allElementsBoundByIndex.filter { container in
            container.exists && root(container).count == 1
        }
        guard containers.count == 1, let container = containers.first, container.isHittable else {
            return ["ok": false,
                    "error": "selector \(selector) did not resolve to a unique containing scroll view"]
        }
        // Direction down moves content upward: one bounded swipe, no retry.
        container.swipeUp()
        let revealed = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "hittable == true AND enabled == true"),
            object: element
        )
        guard XCTWaiter().wait(for: [revealed], timeout: 10) == .completed,
              query.count == 1 else {
            return [
                "ok": false,
                "error": "selector \(selector) did not settle into a visibly hittable "
                    + "element after one bounded swipe",
            ]
        }
        return snapshotResponse(app: app, includeSystemPrompts: includeSystemPrompts)
    }

    /// Deliver one host-allowlisted URL through the simulator's system URL
    /// handler. Return fresh state after the pinned app is in the foreground
    /// so the caller can verify the requested destination.
    @MainActor
    private func openDeepLink(_ url: URL, in app: XCUIApplication) -> [String: Any] {
        XCUIDevice.shared.system.open(url)
        guard Self.waitAppForeground(app) else {
            return [
                "ok": false,
                "error": "app state was not observed in the foreground after the deep link",
            ]
        }
        return Self.snapshotResponse(app: app, includeSystemPrompts: externalApp)
    }

    /// Bounded observation of the app owning the foreground.
    @MainActor
    private static func waitAppForeground(_ app: XCUIApplication) -> Bool {
        let foreground = XCTNSPredicateExpectation(
            predicate: NSPredicate(
                format: "state == %d",
                XCUIApplication.State.runningForeground.rawValue
            ),
            object: app
        )
        return XCTWaiter().wait(for: [foreground], timeout: 30) == .completed
    }

    /// Bounded external-launch readiness: the app must own the foreground
    /// and the manifest ready selector must resolve to exactly one usable
    /// element. Cold app content may not exist while a Metro-backed bundle
    /// loads, so the wait comes first and the exact count is verified after.
    @MainActor
    private static func awaitAppReadiness(
        _ app: XCUIApplication,
        selector: ReadySelector
    ) -> Bool {
        guard waitAppForeground(app) else { return false }
        let field: XCUIElementQuery
        if let testId = selector.testId {
            field = app.descendants(matching: .any).matching(identifier: testId)
        } else if let text = selector.text {
            field = app.descendants(matching: .any)
                .matching(NSPredicate(format: "label == %@", text))
        } else {
            return false
        }
        guard field.firstMatch.waitForExistence(timeout: 120) else { return false }
        return field.count == 1 && field.element(boundBy: 0).exists
    }

    /// Collect the app's descendants from a single native capture. The
    /// captured attributes avoid a live XCTest query for every property.
    /// With `includeSystemPrompts`, the fresh-install overlays the capture
    /// may not carry — the in-app AutoFill "Save Password?" button and any
    /// Springboard-owned alert — are appended from explicit queries.
    @MainActor
    static func snapshotNodes(
        app: XCUIApplication,
        includeSystemPrompts: Bool = false
    ) throws -> [[String: Any]] {
        let root = try app.snapshot()
        var nodes: [[String: Any]] = []
        for child in root.children {
            appendNodes(of: child, into: &nodes)
        }
        if includeSystemPrompts {
            appendSystemPromptNodes(app: app, into: &nodes)
        }
        return nodes
    }

    /// Appends the system prompts the app capture itself may not carry:
    /// the AutoFill "Save Password?" sheet's "Not Now" button when it
    /// lives in the app's own window hierarchy, and any Springboard-owned
    /// alert. Only labels, geometry and enabled state are read — never
    /// field values or alert content.
    @MainActor
    private static func appendSystemPromptNodes(
        app: XCUIApplication,
        into nodes: inout [[String: Any]]
    ) {
        let notNow = app.buttons["Not Now"]
        if notNow.exists, notNow.isHittable,
           !nodes.contains(where: { node in
               (node["role"] as? String) == "button" && (node["text"] as? String) == "Not Now"
           }) {
            let frame = notNow.frame
            if frame.width > 0, frame.height > 0,
               frame.minX.isFinite, frame.minY.isFinite,
               frame.maxX.isFinite, frame.maxY.isFinite {
                nodes.append([
                    "testId": "",
                    "role": "button",
                    "text": "Not Now",
                    "rect": [frame.minX, frame.minY, frame.maxX, frame.maxY],
                    "enabled": true,
                ])
            }
        }
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        if springboard.alerts.firstMatch.exists,
           let alertSnapshot = try? springboard.alerts.firstMatch.snapshot() {
            appendNodes(of: alertSnapshot, into: &nodes)
        }
    }

    /// One pre-order visit. An element becomes a node only with a finite
    /// positive frame — a non-finite or degenerate frame also cannot be
    /// serialized — but a frame-less container still owns the elements
    /// inside it, so the walk always descends regardless of the gate.
    /// Secure field values are never read or serialized, and a
    /// placeholder is not typed content even for a plain field.
    @MainActor
    private static func appendNodes(
        of snapshot: any XCUIElementSnapshot,
        into nodes: inout [[String: Any]]
    ) {
        let frame = snapshot.frame
        if frame.width > 0, frame.height > 0,
           frame.minX.isFinite, frame.minY.isFinite,
           frame.maxX.isFinite, frame.maxY.isFinite {
            var node: [String: Any] = [
                "testId": snapshot.identifier,
                "role": roleName(of: snapshot.elementType),
                "text": snapshot.label,
                "rect": [frame.minX, frame.minY, frame.maxX, frame.maxY],
                "enabled": snapshot.isEnabled,
            ]
            if snapshot.elementType != .secureTextField,
               let value = snapshot.value as? String, !value.isEmpty,
               value != snapshot.placeholderValue {
                node["value"] = value
            }
            nodes.append(node)
        }
        for child in snapshot.children {
            appendNodes(of: child, into: &nodes)
        }
    }

    @MainActor
    private static func snapshotResponse(
        app: XCUIApplication,
        includeSystemPrompts: Bool = false
    ) -> [String: Any] {
        do {
            return [
                "ok": true,
                "nodes": try snapshotNodes(app: app, includeSystemPrompts: includeSystemPrompts)
            ]
        } catch {
            // A failed observation leaves any preceding action uncertain.
            return ["ok": false, "error": "the native element snapshot could not be read"]
        }
    }

    private static func roleName(of type: XCUIElement.ElementType) -> String {
        switch type {
        case .button: return "button"
        case .staticText: return "text"
        case .textField: return "textfield"
        case .secureTextField: return "securetextfield"
        default: return "other"
        }
    }

    @MainActor
    func testBridgeSession() throws {
        continueAfterFailure = false
        let environment = ProcessInfo.processInfo.environment
        guard let portValue = environment["ASHNAZG_PROBE_BRIDGE"] else {
            throw XCTSkip("bridge session not selected: run ashnazg mcp --ios-fixture or --ios-app")
        }
        guard let token = environment["ASHNAZG_PROBE_TOKEN"], !token.isEmpty else {
            XCTFail("ASHNAZG_PROBE_TOKEN capability is missing from the test runner environment")
            return
        }
        guard let port = UInt16(portValue) else {
            XCTFail("ASHNAZG_PROBE_BRIDGE is not a port: \(portValue)")
            return
        }
        let app: XCUIApplication
        let greetingApp: String
        if let bundleId = environment["ASHNAZG_APP_BUNDLE_ID"], !bundleId.isEmpty {
            // External manifest-pinned app: the host has already validated
            // the manifest, staged the pinned bytes, created and booted the
            // owned simulator, and installed the app.
            externalApp = true
            app = XCUIApplication(bundleIdentifier: bundleId)
            if let initialUrl = environment["ASHNAZG_APP_INITIAL_URL"], !initialUrl.isEmpty {
                // Expo dev-client reads `--initialUrl` from the app argv
                // (expo/expo#33235); the dev-menu flags disable its launch
                // sheet, onboarding and floating button so journeys run on
                // the app itself.
                app.launchArguments += [
                    "--initialUrl", initialUrl,
                    "-EXDevMenuShowsAtLaunch", "NO",
                    "-EXDevMenuIsOnboardingFinished", "YES",
                    "-EXDevMenuShowFloatingActionButton", "NO",
                ]
            }
            greetingApp = bundleId
            // External apps hit one-time iOS system permission alerts
            // (notifications / location); dismiss them when they interrupt
            // a tap or type so in-app dismissal stays primary.
            addUIInterruptionMonitor(withDescription: "Ashnazg external app system prompt") {
                alert in
                let dismissLabels = ["Don’t Allow", "Don't Allow", "Allow While Using App"]
                for label in dismissLabels {
                    let button = alert.buttons[label]
                    if button.exists, button.isHittable {
                        button.tap()
                        return true
                    }
                }
                return false
            }
        } else {
            app = XCUIApplication()
            greetingApp = "dev.ashnazg.ios.fixture"
        }
        app.launch()
        defer { app.terminate() }
        if externalApp {
            guard let selectorData = environment["ASHNAZG_APP_READY_SELECTOR"]?.data(using: .utf8),
                  let selector = try? JSONDecoder().decode(ReadySelector.self, from: selectorData),
                  Self.awaitAppReadiness(app, selector: selector)
            else {
                XCTFail("the manifest ready selector was not observed")
                return
            }
        } else {
            XCTAssertTrue(
                app.staticTexts["fixture-count"].waitForExistence(timeout: 60),
                "fixture counter did not appear"
            )
        }

        let closed = expectation(description: "bridge session closed by the host")
        var sequence = 0
        var finished = false
        let bridge = BridgeClient(host: "127.0.0.1", port: port)
        // Any connection loss ends the session immediately instead of waiting
        // out the full timeout; a normal close arrives as the close command.
        bridge.onClose = {
            DispatchQueue.main.async {
                guard !finished else { return }
                finished = true
                XCTFail("bridge connection lost before the host closed the session")
                closed.fulfill()
            }
        }
        bridge.onLine = { payload in
            DispatchQueue.main.async {
                guard !finished else { return }
                guard let command = try? JSONDecoder().decode(Command.self, from: payload) else {
                    bridge.fail("malformed bridge command")
                    return
                }
                if command.op == "close" {
                    finished = true
                    closed.fulfill()
                    return
                }
                var response = self.perform(command, app: app)
                response["id"] = command.id ?? 0
                sequence += 1
                response["sequence"] = sequence
                bridge.send(response)
            }
        }
        bridge.startReceiving()
        bridge.send(["ready": true, "token": token, "app": greetingApp])
        wait(for: [closed], timeout: 900)
    }
}

// Minimal line-oriented TCP client. Reads run on a background queue with a hard
// line-length bound; send and connection failures end the session immediately
// instead of waiting out the session timeout.
private final class BridgeClient {
    private static let lineLimit = 1 << 20

    var onLine: ((Data) -> Void)?
    var onClose: (() -> Void)?
    private let queue = DispatchQueue(label: "dev.ashnazg.ios.bridge")
    private var buffer = Data()
    private let connection: Network.NWConnection
    private var failed = false

    init(host: String, port: UInt16) {
        connection = Network.NWConnection(
            host: Network.NWEndpoint.Host(host),
            port: Network.NWEndpoint.Port(rawValue: port)!,
            using: .tcp
        )
    }

    func send(_ payload: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        else { fail("bridge response was not serializable"); return }
        var line = data
        line.append(0x0A)
        connection.send(content: line, completion: .contentProcessed { [weak self] error in
            if let error {
                self?.fail("bridge send failed: \(error)")
            }
        })
    }

    func startReceiving() {
        connection.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                self?.receiveNext()
            case .failed(let error):
                self?.fail("bridge connection failed: \(error)")
            case .cancelled:
                self?.onClose?()
            default:
                break
            }
        }
        connection.start(queue: queue)
    }

    private func receiveNext() {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { [weak self] content, _, done, error in
            guard let self else { return }
            if let content, !content.isEmpty {
                self.buffer.append(content)
            }
            if self.buffer.count > Self.lineLimit {
                self.fail("bridge command line exceeded \(Self.lineLimit) bytes")
                return
            }
            while let range = self.buffer.firstRange(of: Data([0x0A])) {
                let line = self.buffer.subdata(in: 0..<range.lowerBound)
                self.buffer.removeSubrange(0...range.lowerBound)
                self.onLine?(line)
            }
            if done || error != nil {
                self.onClose?()
                return
            }
            self.receiveNext()
        }
    }

    func fail(_ message: String) {
        queue.async { [weak self] in
            guard let self, !self.failed else { return }
            self.failed = true
            self.connection.cancel()
            DispatchQueue.main.async {
                XCTFail(message)
            }
        }
    }
}
