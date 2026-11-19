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
    private struct Command: Codable {
        let op: String
        let id: UInt64?
        let action: String?
        let testId: String?
        let text: String?
        let value: String?
    }

    @MainActor
    private func perform(_ command: Command, app: XCUIApplication) -> [String: Any] {
        switch command.op {
        case "ui_snapshot":
            return ["ok": true, "nodes": Self.snapshotNodes(app: app)]
        case "ui_act":
            switch command.action ?? "" {
            case "tap":
                if let testId = command.testId, !testId.isEmpty {
                    let query = app.descendants(matching: .any).matching(identifier: testId)
                    return Self.tapFirstMatch(of: query, selector: "testId \(testId)", in: app)
                }
                if let text = command.text, !text.isEmpty {
                    let query = app.descendants(matching: .any)
                        .matching(NSPredicate(format: "label == %@", text))
                    return Self.tapFirstMatch(of: query, selector: "text \(text)", in: app)
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
                    return Self.typeText(
                        into: query, selector: "testId \(testId)", value: value, in: app
                    )
                }
                if let text = command.text, !text.isEmpty {
                    let query = app.descendants(matching: .any)
                        .matching(NSPredicate(format: "label == %@", text))
                    return Self.typeText(
                        into: query, selector: "text \(text)", value: value, in: app
                    )
                }
                return ["ok": false, "error": "type requires a testId or text selector"]
            default:
                return ["ok": false, "error": "bridge only supports the tap and type actions"]
            }
        case "ui_screenshot":
            let png = app.screenshot().pngRepresentation
            return ["ok": true,
                    "png": png.base64EncodedString(),
                    "counter": app.staticTexts["fixture-count"].label]
        default:
            return ["ok": false, "error": "bridge operation unsupported: \(command.op)"]
        }
    }

    @MainActor
    private static func tapFirstMatch(
        of query: XCUIElementQuery,
        selector: String,
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
        return ["ok": true, "nodes": snapshotNodes(app: app)]
    }

    /// One live text entry into a uniquely matched editable field. The tap
    /// focuses the field and `typeText` is genuine XCTest keyboard input; the
    /// settle observes the field value without ever echoing it. An empty
    /// payload settles as a no-op before any secure-text boundary; a nonempty
    /// payload onto existing secure text is rejected, mirroring the Android
    /// `type` boundary. Secure values never appear in snapshots or errors.
    @MainActor
    private static func typeText(
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
            return ["ok": true, "nodes": snapshotNodes(app: app)]
        }
        if element.elementType == .secureTextField && !existing.isEmpty {
            // Mirrors the Android boundary on nonempty secure text; the
            // bridge offers no clear action to work around it.
            return ["ok": false,
                    "error": "selector \(selector) holds existing secure text; typing onto it is not supported"]
        }
        element.tap()
        element.typeText(value)
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
        return ["ok": true, "nodes": snapshotNodes(app: app)]
    }

    private static func snapshotNodes(app: XCUIApplication) -> [[String: Any]] {
        var nodes: [[String: Any]] = []
        for element in app.descendants(matching: .any).allElementsBoundByIndex {
            guard element.exists else { continue }
            let frame = element.frame
            guard frame.width > 0, frame.height > 0 else { continue }
            var node: [String: Any] = [
                "testId": element.identifier,
                "role": roleName(of: element.elementType),
                "text": element.label,
                "rect": [frame.minX, frame.minY, frame.maxX, frame.maxY],
                "enabled": element.isEnabled,
            ]
            // Secure field values are never serialized, and a placeholder is
            // not typed content even for a plain field.
            if element.elementType != .secureTextField,
               let value = element.value as? String, !value.isEmpty,
               value != element.placeholderValue {
                node["value"] = value
            }
            nodes.append(node)
        }
        return nodes
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
            throw XCTSkip("bridge session not selected: run ashnazg mcp --ios-fixture")
        }
        guard let token = environment["ASHNAZG_PROBE_TOKEN"], !token.isEmpty else {
            XCTFail("ASHNAZG_PROBE_TOKEN capability is missing from the test runner environment")
            return
        }
        guard let port = UInt16(portValue) else {
            XCTFail("ASHNAZG_PROBE_BRIDGE is not a port: \(portValue)")
            return
        }
        let app = XCUIApplication()
        app.launch()
        defer { app.terminate() }
        XCTAssertTrue(
            app.staticTexts["fixture-count"].waitForExistence(timeout: 60),
            "fixture counter did not appear"
        )

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
        bridge.send(["ready": true, "token": token, "app": "dev.ashnazg.ios.fixture"])
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
