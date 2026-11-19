import Dispatch
import UIKit
import XCTest

final class SimulatorProbeTests: XCTestCase {
    @MainActor
    func testObserveTapObserveAndCapture() throws {
        continueAfterFailure = false
        let app = XCUIApplication()
        let firstUsableStart = DispatchTime.now().uptimeNanoseconds
        app.launch()
        defer { app.terminate() }

        let countQuery = app.staticTexts.matching(identifier: "fixture-count")
        let count = countQuery.firstMatch
        XCTAssertTrue(count.waitForExistence(timeout: 10))
        XCTAssertEqual(countQuery.count, 1)
        XCTAssertEqual(count.label, "Count: 0")
        let buttonQuery = app.buttons.matching(identifier: "fixture-increment")
        XCTAssertEqual(buttonQuery.count, 1)
        let button = buttonQuery.firstMatch
        XCTAssertTrue(button.isEnabled && button.isHittable)
        let countRect = count.frame
        let buttonRect = button.frame
        let windowRect = app.windows.firstMatch.frame
        XCTAssertTrue(
            buttonRect.width > 0 && buttonRect.height > 0
                && buttonRect.minX.isFinite && buttonRect.minY.isFinite
                && buttonRect.maxX.isFinite && buttonRect.maxY.isFinite)
        XCTAssertTrue(windowRect.contains(buttonRect), "button not visible in window")
        XCTAssertTrue(
            countRect.width > 0 && countRect.height > 0 && windowRect.contains(countRect),
            "counter not visible in window"
        )
        let firstUsableEnd = DispatchTime.now().uptimeNanoseconds
        let before = app.screenshot()
        let beforePNG = before.pngRepresentation
        let beforeImage = try XCTUnwrap(before.image.cgImage)

        let tapStart = DispatchTime.now().uptimeNanoseconds
        button.tap()
        let changed = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label == %@", "Count: 1"), object: count
        )
        XCTAssertEqual(XCTWaiter.wait(for: [changed], timeout: 5), .completed)
        let after = app.screenshot()
        XCTAssertNotEqual(beforePNG, after.pngRepresentation)
        let afterImage = try XCTUnwrap(after.image.cgImage)
        XCTAssertEqual(afterImage.width, beforeImage.width)
        XCTAssertEqual(afterImage.height, beforeImage.height)
        let tapEnd = DispatchTime.now().uptimeNanoseconds

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

        // Keep the two in-run intervals independent of attachment export.
        let boundaries: [String: Any] = [
            "schema": "simulator-probe-boundaries",
            "version": 1,
            "clock": "dispatch-uptime-ns",
            "firstUsableStartUptimeNs": Int(firstUsableStart),
            "firstUsableEndUptimeNs": Int(firstUsableEnd),
            "tapStartUptimeNs": Int(tapStart),
            "tapEndUptimeNs": Int(tapEnd),
            "buttonRect": [buttonRect.minX, buttonRect.minY, buttonRect.maxX, buttonRect.maxY],
            "countRect": [countRect.minX, countRect.minY, countRect.maxX, countRect.maxY],
            "windowRect": [windowRect.minX, windowRect.minY, windowRect.maxX, windowRect.maxY],
            "preCapture": "before-increment",
            "postCapture": "after-increment",
            "captureExtent": "app",
            "capturePixels": [afterImage.width, afterImage.height],
        ]
        let record = try JSONSerialization.data(withJSONObject: boundaries, options: [.sortedKeys])
        let boundariesAttachment = XCTAttachment(data: record, uniformTypeIdentifier: "public.json")
        boundariesAttachment.name = "fixture-observation-boundaries"
        boundariesAttachment.lifetime = .keepAlways
        add(boundariesAttachment)
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
