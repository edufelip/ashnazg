import XCTest

final class SimulatorProbeTests: XCTestCase {
    @MainActor
    func testObserveTapObserveAndCapture() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launch()
        defer { app.terminate() }

        let count = app.staticTexts["fixture-count"]
        XCTAssertTrue(count.waitForExistence(timeout: 10))
        XCTAssertEqual(count.label, "Count: 0")
        let buttonQuery = app.buttons.matching(identifier: "fixture-increment")
        XCTAssertEqual(buttonQuery.count, 1)
        let button = buttonQuery.firstMatch
        XCTAssertTrue(button.isEnabled && button.isHittable)
        let before = app.screenshot()

        button.tap()
        let changed = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == %@", "Count: 1"), object: count
        )
        XCTAssertEqual(XCTWaiter.wait(for: [changed], timeout: 5), .completed)
        let after = app.screenshot()
        XCTAssertNotEqual(before.pngRepresentation, after.pngRepresentation)

        let frame = count.frame
        XCTAssertGreaterThan(frame.width, 0)
        XCTAssertGreaterThan(frame.height, 0)
        let node: [String: Any] = [
            "testId": count.identifier,
            "role": "text",
            "text": count.label,
            "rect": [frame.minX, frame.minY, frame.maxX, frame.maxY],
            "enabled": count.isEnabled,
        ]
        let snapshot = try JSONSerialization.data(withJSONObject: ["nodes": [node]], options: [.sortedKeys])
        let tree = XCTAttachment(data: snapshot, uniformTypeIdentifier: "public.json")
        tree.name = "fixture-snapshot"
        tree.lifetime = .keepAlways
        add(tree)
        for (name, screenshot) in [("before-increment", before), ("after-increment", after)] {
            let attachment = XCTAttachment(screenshot: screenshot)
            attachment.name = name
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }

    @MainActor
    func testTypesIntoTheIdentifiedField() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launch()
        defer { app.terminate() }

        let query = app.textFields.matching(identifier: "fixture-input")
        let input = query.firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        XCTAssertEqual(query.count, 1)
        XCTAssertTrue(input.isHittable)
        // Pins the empty-field report the bridge normalization relies on:
        // before typing, XCUI reports either no value or exactly the
        // placeholder, never anything else.
        let initial = input.value as? String ?? ""
        XCTAssertTrue(
            initial.isEmpty || initial == input.placeholderValue,
            "unexpected initial field value: \(initial)"
        )
        input.tap()
        input.typeText("Ashnazg")
        XCTAssertEqual(input.value as? String, "Ashnazg")
        input.typeText("-fixture")
        XCTAssertEqual(input.value as? String, "Ashnazg-fixture")
    }

    /// Observed-prefix delivery mirror of `testTypesIntoTheIdentifiedField`
    /// (which stays as the bulk burst control): type the first grapheme
    /// once, wait for that exact observed prefix, then type the remainder
    /// once and assert the exact final readback. No replay anywhere.
    @MainActor
    func testTypesObservedPrefixIntoTheIdentifiedField() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launch()
        defer { app.terminate() }

        let query = app.textFields.matching(identifier: "fixture-input")
        let input = query.firstMatch
        XCTAssertTrue(input.waitForExistence(timeout: 10))
        XCTAssertEqual(query.count, 1)
        XCTAssertTrue(input.isHittable)
        let initial = input.value as? String ?? ""
        XCTAssertTrue(
            initial.isEmpty || initial == input.placeholderValue,
            "unexpected initial field value: \(initial)"
        )
        input.tap()
        input.typeText("A")
        let prefix = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value == %@", "A"), object: input
        )
        XCTAssertEqual(XCTWaiter.wait(for: [prefix], timeout: 5), .completed)
        XCTAssertEqual(input.value as? String, "A")
        input.typeText("shnazg-fixture")
        let full = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value == %@", "Ashnazg-fixture"), object: input
        )
        XCTAssertEqual(XCTWaiter.wait(for: [full], timeout: 5), .completed)
        XCTAssertEqual(input.value as? String, "Ashnazg-fixture")
    }
}
