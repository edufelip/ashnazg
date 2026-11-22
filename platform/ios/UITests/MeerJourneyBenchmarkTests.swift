import Dispatch
import Network
import XCTest

// Native direct XCTest pilot for the pinned Meer login -> Home -> Search ->
// Home journey through its visible controls. The selectors, phase checks,
// stage order and bounds mirror platform/ios/verify_mcp_app.py
// --navigation ui: both login fields resolve uniquely before typing, each
// credential is typed exactly once with observed-prefix delivery, one
// bounded reveal scroll runs after each credential, the submit tap happens
// once from a fresh usable selection, and Home, Search and the returned
// Home are confirmed by bounded read-only waits. Navigation is real taps on
// home-search-button and search-back-button only — never a deep link.
//
// The test skips itself unless the runner environment selects it, so the
// ordinary probe scheme keeps exercising only the fixture tests and the
// bridge session. Pass every key through xcodebuild as
// TEST_RUNNER_<KEY>=<VALUE>; the runner process then sees the plain key.
//
//   ASHNAZG_MEER_JOURNEY        "1" selects this pilot; anything else skips.
//   ASHNAZG_MEER_BUNDLE_ID      externally installed dev bundle; must end in ".dev".
//   ASHNAZG_MEER_INITIAL_URL    loopback-only Metro URL for --initialUrl.
//   ASHNAZG_MEER_MOCK_EMAIL     synthetic mock login email; never printed or retained.
//   ASHNAZG_MEER_MOCK_PASSWORD  synthetic mock login password; never printed or retained.
//   ASHNAZG_MEER_CHECKPOINT_PORT  optional loopback TCP port for milestone handshakes.
//
// Checkpoint caller contract (optional live memory support): before the
// test runs, listen on 127.0.0.1:<port>. At each verified milestone end —
// after that milestone's end clock, outside every measured window — the
// test opens one connection, sends one newline-delimited JSON line
// {"milestone":"<name>","op":"checkpoint","sequence":<1-4>,"uptimeNs":<int>}
// and waits bounded for one newline reply. A host sampler reads the line,
// samples the app, simulator and helper processes it already resolved,
// replies with any line (e.g. {"ok":true}) and closes. One connection per
// milestone, at most four. Handshake failures are recorded as safe status
// scalars and never fail the journey; no timing or memory evidence is
// claimed here — the attachment records only that a milestone was signalled.
final class MeerJourneyBenchmarkTests: XCTestCase {
    // The pinned journey selectors, identical to verify_mcp_app.py.
    private static let loginEmail = "login-email-input"
    private static let loginPassword = "login-password-input"
    private static let loginSubmit = "login-submit-button"
    private static let homeSearch = "home-search-button"
    private static let searchInput = "search-input"
    private static let searchBack = "search-back-button"
    // The native secure mask observation, identical to McpBridgeTests.
    private static let maskBullets = "^[•●*]+$"
    private static let maxCredentialChars = 256

    // Bounds mirror verify_mcp_app.py.
    private static let firstLoginTimeout: TimeInterval = 180
    private static let homeAfterSubmitTimeout: TimeInterval = 240
    private static let navigationTimeout: TimeInterval = 180
    private static let settleTimeout: TimeInterval = 10
    private static let checkpointTimeout: TimeInterval = 10

    // Every mutating stage dispatches at most once, ever.
    private var attempted: Set<String> = []

    /// A stage failure carries a safe reason code and safe scalars only —
    /// never typed content, field values or element labels.
    private struct StageFailure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    private func failure(
        _ stage: String, _ code: String, _ scalars: [String: Any] = [:]
    ) -> StageFailure {
        var message = "\(stage): \(code)"
        for (key, value) in scalars.sorted(by: { $0.key < $1.key }) {
            message += " \(key)=\(value)"
        }
        return StageFailure(message: message)
    }

    private func dispatchOnce(_ stage: String) {
        if !attempted.insert(stage).inserted {
            XCTFail("\(stage): a mutating interaction was attempted twice")
        }
    }

    /// The observed field text with the placeholder normalized away; a
    /// placeholder is not typed content. Read in memory only.
    @MainActor
    private func observedValue(_ element: XCUIElement) -> String {
        let reported = element.value as? String ?? ""
        return reported == element.placeholderValue ? "" : reported
    }

    /// One usable element: exactly one match of the exact testId with the
    /// pinned role, enabled — the native mirror of the verifier's select().
    @MainActor
    private func select(
        _ app: XCUIApplication, _ testId: String, _ type: XCUIElement.ElementType, _ stage: String
    ) throws -> XCUIElement {
        let query = app.descendants(matching: .any).matching(identifier: testId)
        let matches = query.count
        guard matches == 1 else {
            throw failure(stage, "selector-ambiguous-or-absent", ["matched": matches])
        }
        let element = query.element(boundBy: 0)
        guard element.elementType == type, element.isEnabled else {
            throw failure(stage, "selector-not-usable", ["enabled": element.isEnabled])
        }
        return element
    }

    /// Bounded read-only wait for one usable element after an accepted
    /// action — the native mirror of the verifier's poll_for(). Ambiguity
    /// or a timeout fails the stage closed instead of being retried.
    @MainActor
    private func awaitUsable(
        _ app: XCUIApplication, _ testId: String, _ type: XCUIElement.ElementType,
        _ bound: TimeInterval, _ stage: String
    ) throws -> XCUIElement {
        let query = app.descendants(matching: .any).matching(identifier: testId)
        guard query.firstMatch.waitForExistence(timeout: bound) else {
            throw failure(stage, "target-not-observed-within-bound", ["bound_seconds": Int(bound)])
        }
        return try select(app, testId, type, stage)
    }

    /// One live text entry into a uniquely matched editable field with the
    /// bridge's observed-prefix delivery: the first grapheme is typed and
    /// observed once, the remainder is sent once after that prefix is
    /// confirmed, and a rejected or ambiguous mutation is never replayed.
    /// A plain field settles on a length wait plus an exact in-memory
    /// comparison, so the synthetic value never enters a predicate that
    /// XCTest could retain in native diagnostics; a secure field settles on
    /// the native bullet mask and the typed length only, and its value is
    /// never echoed. shortcut: the remainder is one burst after the
    /// confirmed prefix; switch to per-grapheme pacing if a live run loses it.
    @MainActor
    private func typeOnce(
        _ stage: String, app: XCUIApplication, testId: String,
        role: XCUIElement.ElementType, value: String
    ) throws {
        dispatchOnce(stage)
        let element = try select(app, testId, role, stage)
        guard element.isHittable else {
            throw failure(stage, "target-not-enabled-hittable")
        }
        let existing = observedValue(element)
        // The pinned login starts from empty fields; typing onto existing
        // text is never attempted, and the bridge offers no clear action.
        if !existing.isEmpty {
            throw failure(stage, "field-held-existing-text", ["observed_chars": existing.count])
        }
        element.tap()
        let prefix = String(value.first!)
        element.typeText(prefix)
        // The plain wait keeps the synthetic value out of predicate
        // descriptions; the exact comparison happens in memory right after.
        let prefixSettled: XCTNSPredicateExpectation
        if role == .secureTextField {
            prefixSettled = XCTNSPredicateExpectation(
                predicate: NSPredicate(
                    format: "value MATCHES %@ AND value.length == %d",
                    Self.maskBullets, existing.count + 1
                ),
                object: element
            )
        } else {
            prefixSettled = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "value.length == %d", existing.count + 1),
                object: element
            )
        }
        guard XCTWaiter().wait(for: [prefixSettled], timeout: Self.settleTimeout) == .completed else {
            let observed = observedValue(element)
            throw failure(
                stage, "unconfirmed-prefix-after-first-grapheme",
                ["requested": 1, "observed_delta": observed.count - existing.count]
            )
        }
        if role != .secureTextField && observedValue(element) != existing + prefix {
            throw failure(stage, "prefix-readback-differed")
        }
        let remainder = String(value.dropFirst())
        if !remainder.isEmpty {
            element.typeText(remainder)
        }
        let settled: XCTNSPredicateExpectation
        if role == .secureTextField {
            settled = XCTNSPredicateExpectation(
                predicate: NSPredicate(
                    format: "value MATCHES %@ AND value.length == %d",
                    Self.maskBullets, value.count
                ),
                object: element
            )
        } else {
            settled = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "value.length == %d", value.count),
                object: element
            )
        }
        guard XCTWaiter().wait(for: [settled], timeout: Self.settleTimeout) == .completed else {
            throw failure(stage, "type-did-not-settle", ["expected_chars": value.count])
        }
        // The plain-field exact readback happens in memory only; the secure
        // field carries mask and length evidence only, never its value.
        if role != .secureTextField && observedValue(element) != existing + value {
            throw failure(stage, "typed-value-did-not-settle")
        }
    }

    /// One controlled reveal of a uniquely matched element clipped by the
    /// keyboard-compressed scroll container — the public-XCUI mirror of
    /// the bridge's scroll_to: a settled no-op when the target is already
    /// visibly hittable, otherwise exactly one bounded swipe on the unique
    /// containing native scroll view, never a second swipe or a retry.
    @MainActor
    private func revealOnce(_ stage: String, app: XCUIApplication, testId: String) throws {
        dispatchOnce(stage)
        let query = app.descendants(matching: .any).matching(identifier: testId)
        let matches = query.count
        guard matches == 1 else {
            throw failure(stage, "selector-ambiguous-or-absent", ["matched": matches])
        }
        let element = query.element(boundBy: 0)
        guard element.isEnabled else {
            throw failure(stage, "target-not-enabled")
        }
        if element.isHittable {
            return
        }
        let containers = app.scrollViews.allElementsBoundByIndex.filter { container in
            container.exists
                && container.descendants(matching: .any).matching(identifier: testId).count == 1
        }
        guard containers.count == 1, let container = containers.first, container.isHittable else {
            throw failure(stage, "scroll-container-not-resolved", ["matched_containers": containers.count])
        }
        container.swipeUp()
        let revealed = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "hittable == true AND enabled == true"),
            object: element
        )
        guard XCTWaiter().wait(for: [revealed], timeout: Self.settleTimeout) == .completed,
              query.count == 1 else {
            throw failure(stage, "reveal-did-not-settle-after-one-swipe")
        }
    }

    /// Bounded post-Home settle for fresh installs. Order of operations:
    /// (a) the AutoFill "Save Password?" prompt (an in-app second window on
    /// iOS 26) is dismissed first via its "Not Now" button; (b) Springboard
    /// alerts (notifications / location); (c) any Springboard-owned
    /// "Not Now". app.activate() is called ONLY when state is no longer
    /// foreground after a Springboard dismissal — repeated activations
    /// while foreground opened the App Switcher in earlier runs. Only
    /// button labels and geometry are logged — never field values.
    @MainActor
    private func settleHomeAndClearSystemAlerts(_ app: XCUIApplication) throws {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let alertLabels = ["Don’t Allow", "Don't Allow", "Not Now", "Allow While Using App", "Allow Once", "Allow"]
        let bound: Double = 15.0
        let stepMs: useconds_t = 200_000
        var dismissals = 0
        var lastDiag = -3.0
        var clearSince: DispatchTime? = nil
        let start = DispatchTime.now()
        while true {
            let elapsed = Double(DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000_000

            // (a) AutoFill "Save Password?" lives in the app's extra window.
            if dismissals < 4 {
                let appNotNow = app.descendants(matching: .button)
                    .matching(NSPredicate(format: "label == 'Not Now'")).firstMatch
                if appNotNow.exists, appNotNow.isHittable {
                    print("[prompt-dismiss] app tapped: Not Now")
                    appNotNow.tap()
                    dismissals += 1
                    clearSince = nil
                    Thread.sleep(forTimeInterval: 0.3)
                    continue
                }
            }

            // (b) Springboard alerts.
            if dismissals < 4 {
                let alert = springboard.alerts.firstMatch
                var handled = false
                if alert.exists {
                    for label in alertLabels {
                        let button = alert.buttons[label]
                        if button.exists, button.isHittable {
                            print("[alert-dismiss] springboard-alert label=\(label)")
                            button.tap()
                            dismissals += 1
                            clearSince = nil
                            Thread.sleep(forTimeInterval: 0.3)
                            if app.state != .runningForeground {
                                app.activate()
                                _ = app.wait(for: .runningForeground, timeout: 5.0)
                            }
                            handled = true
                            break
                        }
                    }
                }
                if handled { continue }
            }

            // (c) Springboard-owned sheets / prompts with "Not Now".
            if dismissals < 4 {
                let sbNotNow = springboard.descendants(matching: .button)
                    .matching(NSPredicate(format: "label == 'Not Now'")).firstMatch
                if sbNotNow.exists, sbNotNow.isHittable {
                    print("[prompt-dismiss] springboard tapped: Not Now")
                    sbNotNow.tap()
                    dismissals += 1
                    clearSince = nil
                    Thread.sleep(forTimeInterval: 0.3)
                    if app.state != .runningForeground {
                        app.activate()
                        _ = app.wait(for: .runningForeground, timeout: 5.0)
                    }
                    continue
                }
            }

            // (d) Settled only after a 2.5s consecutive prompt-free streak so
            // delayed AutoFill Save-Password sheets on warm boots are cleared.
            let query = app.descendants(matching: .any).matching(identifier: Self.homeSearch)
            let element = query.firstMatch
            if query.count == 1, element.exists, element.isEnabled, element.isHittable,
               !springboard.alerts.firstMatch.exists,
               !app.buttons["Not Now"].exists {
                let now = DispatchTime.now()
                let streakStart = clearSince ?? now
                clearSince = streakStart
                let clearElapsed = Double(now.uptimeNanoseconds - streakStart.uptimeNanoseconds) / 1_000_000_000
                if clearElapsed >= 2.5 {
                    return
                }
            } else {
                clearSince = nil
            }

            // (e) Bounded safe diagnostics while the control is not hittable.
            if elapsed - lastDiag >= 3.0 {
                lastDiag = elapsed
                var windowInfo = ""
                if app.windows.count > 1 {
                    let lastWindow = app.windows.element(boundBy: app.windows.count - 1)
                    let labels = lastWindow.buttons.allElementsBoundByIndex.prefix(6).map { $0.label }
                    windowInfo = " lastWindowButtons=\(labels)"
                }
                print(
                    "[settle-diag] state=\(app.state.rawValue)"
                        + " windows=\(app.windows.count)"
                        + " sbAlerts=\(springboard.alerts.count)"
                        + " sbSheets=\(springboard.sheets.count)"
                        + windowInfo
                )
            }
            if elapsed >= bound {
                throw failure(
                    "home/settle", "home-not-settled-within-bound",
                    ["bound_seconds": Int(bound), "dismissals": dismissals]
                )
            }
            usleep(stepMs)
        }
    }

    /// Exactly one screen-navigation tap on a uniquely matched enabled
    /// hittable element, selected from the live observed state immediately
    /// before the single dispatch.
    @MainActor
    private func tapOnce(
        _ stage: String, app: XCUIApplication, testId: String, type: XCUIElement.ElementType
    ) throws {
        dispatchOnce(stage)
        let element = try select(app, testId, type, stage)
        guard element.isHittable else {
            throw failure(stage, "target-not-enabled-hittable")
        }
        element.tap()
    }

    /// One bounded loopback milestone handshake for a host-side sampler.
    /// Returns a safe status scalar only; see the contract in this file's
    /// header comment.
    private static func signalCheckpoint(
        port: UInt16, milestone: String, sequence: Int, uptimeNs: UInt64
    ) -> String {
        let payload: [String: Any] = [
            "op": "checkpoint", "milestone": milestone,
            "sequence": sequence, "uptimeNs": Int(uptimeNs),
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        else { return "payload-not-serializable" }
        var line = data
        line.append(0x0A)
        let handshake = XCTestExpectation(description: "milestone checkpoint handshake")
        var finished = false
        var acknowledged = false
        let connection = NWConnection(
            host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!, using: .tcp
        )
        func finish(_ ack: Bool) {
            DispatchQueue.main.async {
                guard !finished else { return }
                finished = true
                acknowledged = ack
                connection.cancel()
                handshake.fulfill()
            }
        }
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                connection.send(content: line, completion: .contentProcessed { error in
                    guard error == nil else {
                        finish(false)
                        return
                    }
                    connection.receive(minimumIncompleteLength: 1, maximumLength: 4096) {
                        content, _, _, receiveError in
                        finish(receiveError == nil && content != nil && !content!.isEmpty)
                    }
                })
            case .failed:
                finish(false)
            default:
                break
            }
        }
        connection.start(queue: DispatchQueue(label: "dev.ashnazg.ios.meer-checkpoint"))
        let verdict = XCTWaiter().wait(for: [handshake], timeout: checkpointTimeout)
        connection.cancel()
        if verdict != .completed {
            return "no-ack-within-bound"
        }
        return acknowledged ? "acked" : "signaled-unacknowledged"
    }

    @MainActor
    private func checkpoint(
        port: UInt16?, statuses: inout [[String: Any]],
        milestone: String, sequence: Int, uptimeNs: UInt64
    ) {
        guard let port else { return }
        let status = Self.signalCheckpoint(
            port: port, milestone: milestone, sequence: sequence, uptimeNs: uptimeNs
        )
        statuses.append([
            "milestone": milestone, "sequence": sequence, "status": status,
        ])
    }

    @MainActor
    func testPinnedJourneyThroughVisibleControls() throws {
        continueAfterFailure = false
        let environment = ProcessInfo.processInfo.environment
        guard environment["ASHNAZG_MEER_JOURNEY"] == "1" else {
            throw XCTSkip(
                "meer journey pilot not selected: set ASHNAZG_MEER_JOURNEY=1 with the"
                    + " ASHNAZG_MEER_* runner keys"
            )
        }
        guard let bundleId = environment["ASHNAZG_MEER_BUNDLE_ID"],
              !bundleId.isEmpty,
              bundleId.allSatisfy({ $0.isLetter || $0.isNumber || ".-_".contains($0) }),
              // shortcut: the ".dev" suffix is the whole dev-bundle classifier;
              // replace with an allowlist if dev ids without the suffix appear.
              bundleId.hasSuffix(".dev")
        else {
            throw failure("journey/config", "ASHNAZG_MEER_BUNDLE_ID must be the installed .dev bundle id")
        }
        guard let initialUrl = environment["ASHNAZG_MEER_INITIAL_URL"],
              let url = URL(string: initialUrl),
              ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
              ["127.0.0.1", "localhost", "::1"].contains(url.host?.lowercased() ?? "")
        else {
            // shortcut: guard-let bindings are not in scope in the else branch; static message
            throw failure("journey/config", "ASHNAZG_MEER_INITIAL_URL must be an http(s) URL on a loopback host")
        }
        guard let email = environment["ASHNAZG_MEER_MOCK_EMAIL"],
              let password = environment["ASHNAZG_MEER_MOCK_PASSWORD"],
              !email.isEmpty, !password.isEmpty,
              email.count <= Self.maxCredentialChars, password.count <= Self.maxCredentialChars
        else {
            throw failure(
                "journey/config",
                "ASHNAZG_MEER_MOCK_EMAIL and ASHNAZG_MEER_MOCK_PASSWORD must be nonempty"
                    + " bounded synthetic values"
            )
        }
        var checkpointPort: UInt16?
        if let rawPort = environment["ASHNAZG_MEER_CHECKPOINT_PORT"] {
            guard let port = UInt16(rawPort) else {
                throw failure("journey/config", "ASHNAZG_MEER_CHECKPOINT_PORT is not a port")
            }
            checkpointPort = port
        }
        var checkpointStatuses: [[String: Any]] = []

        // The app is externally installed; only its dev bundle is launched,
        // with the same Expo dev-client flags McpBridgeTests uses so the
        // journey runs on the app itself, never the dev-menu sheet.
        let app = XCUIApplication(bundleIdentifier: bundleId)
        app.launchArguments += [
            "--initialUrl", initialUrl,
            "-EXDevMenuShowsAtLaunch", "NO",
            "-EXDevMenuIsOnboardingFinished", "YES",
            "-EXDevMenuShowFloatingActionButton", "NO",
        ]
        // One-time iOS system permission alerts (notifications / location)
        // appear asynchronously on a fresh install; dismiss them during any
        // interaction so no stage guard is blocked behind an alert.
        addUIInterruptionMonitor(withDescription: "Dismiss iOS System Alert") { alert in
            let dismissLabels = ["Don’t Allow", "Don't Allow", "Allow While Using App", "Allow Once", "Allow"]
            for label in dismissLabels {
                let button = alert.buttons[label]
                if button.exists {
                    print("[alert-dismiss] interruption monitor tapped: \(label)")
                    button.tap()
                    return true
                }
            }
            return false
        }
        let firstValidLoginStart = DispatchTime.now().uptimeNanoseconds
        app.launch()
        defer { app.terminate() }

        // First valid login state: both fields resolve uniquely in the live
        // app, and the secure field holds no existing text.
        _ = try awaitUsable(
            app, Self.loginEmail, .textField, Self.firstLoginTimeout, "login/first-state"
        )
        let passwordField = try select(app, Self.loginPassword, .secureTextField, "login/first-state")
        if !observedValue(passwordField).isEmpty {
            throw failure("login/first-state", "secure-field-held-existing-text")
        }
        let firstValidLoginEnd = DispatchTime.now().uptimeNanoseconds
        checkpoint(
            port: checkpointPort, statuses: &checkpointStatuses,
            milestone: "first_valid_login", sequence: 1, uptimeNs: firstValidLoginEnd
        )

        // Each credential is typed exactly once; the keyboard compresses
        // the login scroll container after the email lands, so one bounded
        // reveal scroll makes the exact next control visibly hittable
        // before its single interaction — the verifier's exact stage order.
        let credentialEntryStart = DispatchTime.now().uptimeNanoseconds
        try typeOnce(
            "login/email", app: app, testId: Self.loginEmail, role: .textField, value: email
        )
        try revealOnce("login/email-reveal", app: app, testId: Self.loginPassword)
        try typeOnce(
            "login/password", app: app, testId: Self.loginPassword,
            role: .secureTextField, value: password
        )
        try revealOnce("login/password-reveal", app: app, testId: Self.loginSubmit)

        // One submit tap from a fresh usable selection, then Home confirmed
        // by bounded read-only waiting.
        let submitToHomeStart = DispatchTime.now().uptimeNanoseconds
        try tapOnce("login/submit", app: app, testId: Self.loginSubmit, type: .button)
        _ = try awaitUsable(
            app, Self.homeSearch, .button, Self.homeAfterSubmitTimeout, "home/after-submit"
        )
        // Fresh installs show one-time iOS system permission alerts
        // (notifications / location) over Home; clear them once each and
        // settle before the single navigation tap. Never retries the tap.
        try settleHomeAndClearSystemAlerts(app)
        let submitToHomeEnd = DispatchTime.now().uptimeNanoseconds
        let homeScreenshotStart = DispatchTime.now().uptimeNanoseconds
        let homeCapture = app.screenshot()
        let homeCapturePNG = homeCapture.pngRepresentation
        let homeCaptureImage = try XCTUnwrap(homeCapture.image.cgImage)
        let homeScreenshotEnd = DispatchTime.now().uptimeNanoseconds
        XCTAssertFalse(homeCapturePNG.isEmpty)
        XCTAssertGreaterThan(homeCaptureImage.width, 0)
        XCTAssertGreaterThan(homeCaptureImage.height, 0)
        checkpoint(
            port: checkpointPort, statuses: &checkpointStatuses,
            milestone: "submit_to_home", sequence: 2, uptimeNs: submitToHomeEnd
        )

        // Visible-control navigation: one tap on the exact home search
        // button, then the Search input confirmed.
        let searchStart = DispatchTime.now().uptimeNanoseconds
        try tapOnce("search/open-tap", app: app, testId: Self.homeSearch, type: .button)
        _ = try awaitUsable(
            app, Self.searchInput, .textField, Self.navigationTimeout, "search/after-tap"
        )
        let searchEnd = DispatchTime.now().uptimeNanoseconds
        checkpoint(
            port: checkpointPort, statuses: &checkpointStatuses,
            milestone: "home_to_search", sequence: 3, uptimeNs: searchEnd
        )

        // One tap on the exact native back control, then the Home search
        // control confirmed again, followed by final Home screenshot capture.
        let homeReturnStart = DispatchTime.now().uptimeNanoseconds
        try tapOnce("home-return/back-tap", app: app, testId: Self.searchBack, type: .button)
        _ = try awaitUsable(
            app, Self.homeSearch, .button, Self.navigationTimeout, "home-return/after-tap"
        )
        let homeReturnEnd = DispatchTime.now().uptimeNanoseconds
        let finalHomeScreenshotStart = DispatchTime.now().uptimeNanoseconds
        let finalHomeCapture = app.screenshot()
        let finalHomeCapturePNG = finalHomeCapture.pngRepresentation
        let finalHomeCaptureImage = try XCTUnwrap(finalHomeCapture.image.cgImage)
        let finalHomeScreenshotEnd = DispatchTime.now().uptimeNanoseconds
        XCTAssertFalse(finalHomeCapturePNG.isEmpty)
        XCTAssertEqual(finalHomeCaptureImage.width, homeCaptureImage.width)
        XCTAssertEqual(finalHomeCaptureImage.height, homeCaptureImage.height)
        checkpoint(
            port: checkpointPort, statuses: &checkpointStatuses,
            milestone: "search_to_home", sequence: 4, uptimeNs: homeReturnEnd
        )

        // The journey intervals are dispatch -> verified UI state. Both Home
        // captures and checkpoint handshakes sit strictly outside the login
        // screen and after each milestone stop clock.
        guard firstValidLoginStart < firstValidLoginEnd,
              firstValidLoginEnd <= credentialEntryStart,
              credentialEntryStart < submitToHomeStart,
              submitToHomeStart < submitToHomeEnd,
              submitToHomeEnd <= homeScreenshotStart,
              homeScreenshotStart < homeScreenshotEnd,
              homeScreenshotEnd <= searchStart,
              searchStart < searchEnd,
              searchEnd <= homeReturnStart,
              homeReturnStart < homeReturnEnd,
              homeReturnEnd <= finalHomeScreenshotStart,
              finalHomeScreenshotStart < finalHomeScreenshotEnd
        else {
            throw failure("journey/clocks", "journey clocks are out of order")
        }
        for (name, screenshot) in [("home-screenshot", homeCapture), ("final-home-screenshot", finalHomeCapture)] {
            let captureAttachment = XCTAttachment(screenshot: screenshot)
            captureAttachment.name = name
            captureAttachment.lifetime = .keepAlways
            add(captureAttachment)
        }
        var checkpointRecord: [String: Any] = ["requested": checkpointPort != nil]
        if !checkpointStatuses.isEmpty {
            checkpointRecord["milestones"] = checkpointStatuses
        }
        let boundaries: [String: Any] = [
            "schema": "meer-journey-boundaries",
            "version": 1,
            "clock": "dispatch-uptime-ns",
            "navigation": "visible-controls",
            "bundleId": bundleId,
            "initialUrlScope": "loopback-only",
            "firstValidLoginStartUptimeNs": Int(firstValidLoginStart),
            "firstValidLoginEndUptimeNs": Int(firstValidLoginEnd),
            "credentialEntryStartUptimeNs": Int(credentialEntryStart),
            "submitToHomeStartUptimeNs": Int(submitToHomeStart),
            "submitToHomeEndUptimeNs": Int(submitToHomeEnd),
            "homeScreenshotStartUptimeNs": Int(homeScreenshotStart),
            "homeScreenshotEndUptimeNs": Int(homeScreenshotEnd),
            "searchStartUptimeNs": Int(searchStart),
            "searchEndUptimeNs": Int(searchEnd),
            "homeReturnStartUptimeNs": Int(homeReturnStart),
            "homeReturnEndUptimeNs": Int(homeReturnEnd),
            "finalHomeScreenshotStartUptimeNs": Int(finalHomeScreenshotStart),
            "finalHomeScreenshotEndUptimeNs": Int(finalHomeScreenshotEnd),
            "homeCapture": "home-screenshot",
            "finalHomeCapture": "final-home-screenshot",
            "captureExtent": "app",
            "capturePixels": [finalHomeCaptureImage.width, finalHomeCaptureImage.height],
            "emailReadback": "exact",
            "passwordMaskEvidence": "native-bullets-and-length",
            "typedStagesOnce": true,
            "mutationReplays": 0,
            "mockEmailChars": email.count,
            "mockPasswordChars": password.count,
            "checkpoints": checkpointRecord,
            "secrecy": [
                "credential_values_retained": false,
                "values_never_printed": true,
            ],
        ]
        let record = try JSONSerialization.data(withJSONObject: boundaries, options: [.sortedKeys])
        let text = String(decoding: record, as: UTF8.self)
        if text.contains(email) || text.contains(password) {
            // The suspect record is discarded before any publish; only this
            // static failure remains, mirroring the verifier's scrub rule.
            throw failure(
                "journey/secrecy",
                "the synthetic mock credentials would have leaked into the sanitized record;"
                    + " it was discarded"
            )
        }
        let attachment = XCTAttachment(data: record, uniformTypeIdentifier: "public.json")
        attachment.name = "meer-journey-boundaries"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
