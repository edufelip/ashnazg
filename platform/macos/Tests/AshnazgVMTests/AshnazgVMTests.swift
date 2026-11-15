import Foundation
import XCTest
import CryptoKit
import Virtualization
@testable import AshnazgVM

final class AshnazgVMTests: XCTestCase {
    func testArgumentBounds() throws {
        XCTAssertThrowsError(try HelperOptions(arguments: ["--assets", "/tmp/a", "--adb-port", "80"]))
        XCTAssertThrowsError(try HelperOptions(arguments: ["--assets", "/tmp/a", "--timeout", "2"]))
        XCTAssertNoThrow(try HelperOptions(arguments: ["--assets", "/tmp/a", "--adb-port", "5555", "--timeout", "300"]))
    }

    func testVMResourceBoundsUseSupportedAlignedDefaults() throws {
        XCTAssertNoThrow(try VZConfigFactory.validateResourceBounds(cpuCount: VZConfigFactory.cpuCount, memorySize: VZConfigFactory.memorySize))
        XCTAssertThrowsError(try VZConfigFactory.validateResourceBounds(cpuCount: VZVirtualMachineConfiguration.maximumAllowedCPUCount + 1, memorySize: VZConfigFactory.memorySize))
        XCTAssertThrowsError(try VZConfigFactory.validateResourceBounds(cpuCount: VZConfigFactory.cpuCount, memorySize: VZVirtualMachineConfiguration.minimumAllowedMemorySize - 1))
    }

    func testManifestHashesAndRejectsSymlinkAssets() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let kernel = Data("kernel bytes".utf8), disk = Data("disk bytes".utf8)
        try kernel.write(to: root.appendingPathComponent("kernel")); try disk.write(to: root.appendingPathComponent("rootfs.ext4"))
        let manifest: [String: Any] = ["schema_version": 1, "version": "0.3.0", "source_sha": String(repeating: "a", count: 40), "architecture": "aarch64", "files": ["kernel": ["sha256": Self.sha(kernel), "size": kernel.count], "rootfs.ext4": ["sha256": Self.sha(disk), "size": disk.count]], "guest": ["kernel_version": "6.12.117", "base": "alpine-3.22.8-aarch64", "redroid_index": "sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad"]]
        try JSONSerialization.data(withJSONObject: manifest).write(to: root.appendingPathComponent("manifest.json"))
        XCTAssertNoThrow(try ValidatedAssets.load(from: root))
        try Data("tampered".utf8).write(to: root.appendingPathComponent("kernel"))
        XCTAssertThrowsError(try ValidatedAssets.load(from: root))
    }

    func testOwnedStateRefusesPreexistingUnownedContent() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try Data("keep".utf8).write(to: root.appendingPathComponent("user-data"))
        let template = root.appendingPathComponent("template.ext4")
        try Data("image".utf8).write(to: template)
        let originalMode = try FileManager.default.attributesOfItem(atPath: root.path)[.posixPermissions] as? NSNumber
        XCTAssertThrowsError(try OwnedState(root, assets: try makeAssets(root: root, disk: Data("image".utf8))))
        XCTAssertEqual(try FileManager.default.attributesOfItem(atPath: root.path)[.posixPermissions] as? NSNumber, originalMode)
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent(".ashnazg.lock").path))
        XCTAssertEqual(try Data(contentsOf: root.appendingPathComponent("user-data")), Data("keep".utf8))
    }

    func testOwnedStateExclusivelyLocksForItsLifetime() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let assets = try makeAssets(root: root, disk: Data("image".utf8))
        let statePath = root.appendingPathComponent("state")
        let first = try OwnedState(statePath, assets: assets)
        XCTAssertThrowsError(try OwnedState(statePath, assets: assets))
        let mode = try FileManager.default.attributesOfItem(atPath: statePath.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o700)
        let lockMode = try FileManager.default.attributesOfItem(atPath: statePath.appendingPathComponent(".ashnazg.lock").path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(lockMode?.intValue, 0o600)
        withExtendedLifetime(first) {}
    }

    func testOwnedStateRefusesChangedTemplateWithoutDeletingPersistentDisk() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let assets = try makeAssets(root: root, disk: Data("image".utf8))
        let statePath = root.appendingPathComponent("state")
        let first = try OwnedState(statePath, assets: assets)
        first.close()
        let manifestURL = assets.directory.appendingPathComponent("manifest.json")
        var manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: manifestURL)) as? [String: Any])
        manifest["source_sha"] = String(repeating: "b", count: 40)
        try JSONSerialization.data(withJSONObject: manifest).write(to: manifestURL)
        let changed = try ValidatedAssets.load(from: assets.directory)
        XCTAssertThrowsError(try OwnedState(statePath, assets: changed))
        XCTAssertEqual(try Data(contentsOf: statePath.appendingPathComponent("rootfs.ext4")), Data("image".utf8))
    }

    func testStartupDeadlineUsesMonotonicClockAndBoundsReads() throws {
        let deadline = StartupDeadline(after: .milliseconds(30))
        XCTAssertThrowsError(try deadline.pollReadable(-1))
        XCTAssertTrue(deadline.isExpired)
    }

    func testReadinessLineCannotExtendWholeStartupDeadlineByteByByte() throws {
        var sockets: [Int32] = [0, 0]
        XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &sockets), 0)
        let writerFD = sockets[1]
        configureNoSIGPIPE(writerFD)
        let writer = DispatchGroup(); writer.enter()
        defer {
            Darwin.shutdown(sockets[0], SHUT_RDWR); Darwin.shutdown(writerFD, SHUT_RDWR)
            XCTAssertEqual(writer.wait(timeout: .now() + 2), .success)
            Darwin.close(sockets[0]); Darwin.close(writerFD)
        }
        DispatchQueue.global().async {
            defer { writer.leave() }
            for byte in Data("{\"x\":1}\n".utf8) {
                var value = byte
                if Darwin.write(writerFD, &value, 1) != 1 { break }
                usleep(15_000)
            }
        }
        let deadline = StartupDeadline(after: .milliseconds(40))
        XCTAssertThrowsError(try readLine(sockets[0], deadline: deadline))
    }

    @MainActor func testDrainingAllowsQueuedMainActorTasksToComplete() async {
        let signal = DispatchSemaphore(value: 0)
        Task { @MainActor in signal.signal() }
        let result = await waitForDrainSignal(signal, seconds: 1)
        XCTAssertTrue(result)
    }

    func testShutdownAcknowledgementRequiresExactContract() throws {
        XCTAssertNoThrow(try parseShutdownResponse(Data("{\"schema_version\":1,\"status\":\"stopping\"}\n".utf8)))
        XCTAssertThrowsError(try parseShutdownResponse(Data("{\"status\":\"ready\"}\n".utf8)))
    }

    func testRelaySocketWriteUsesConnectionLocalSIGPIPESuppression() throws {
        var sockets: [Int32] = [0, 0]
        XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &sockets), 0)
        configureNoSIGPIPE(sockets[0])
        Darwin.close(sockets[1])
        var byte: UInt8 = 1
        XCTAssertEqual(Darwin.write(sockets[0], &byte, 1), -1)
        XCTAssertEqual(errno, EPIPE)
        Darwin.close(sockets[0])
    }

    func testReadinessRequiresExactGuestContract() throws {
        let valid = Data("{\"schema_version\":1,\"status\":\"ready\",\"android_serial\":\"127.0.0.1:5555\"}\n".utf8)
        XCTAssertEqual(try parseReadyResponse(valid), "127.0.0.1:5555")
        XCTAssertThrowsError(try parseReadyResponse(Data("{\"status\":\"ready\"}\n".utf8)))
        XCTAssertThrowsError(try parseReadyResponse(Data(repeating: 65, count: 4097)))
    }

    private static func sha(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    private func makeAssets(root: URL, disk: Data) throws -> ValidatedAssets {
        let assetDirectory = root.appendingPathComponent("assets-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: assetDirectory, withIntermediateDirectories: true)
        let kernel = Data("kernel bytes".utf8)
        try kernel.write(to: assetDirectory.appendingPathComponent("kernel"))
        try disk.write(to: assetDirectory.appendingPathComponent("rootfs.ext4"))
        let manifest: [String: Any] = ["schema_version": 1, "version": "0.3.0", "source_sha": String(repeating: "a", count: 40), "architecture": "aarch64", "files": ["kernel": ["sha256": Self.sha(kernel), "size": kernel.count], "rootfs.ext4": ["sha256": Self.sha(disk), "size": disk.count]], "guest": ["kernel_version": "6.12.117", "base": "alpine-3.22.8-aarch64", "redroid_index": "sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad"]]
        try JSONSerialization.data(withJSONObject: manifest).write(to: assetDirectory.appendingPathComponent("manifest.json"))
        return try ValidatedAssets.load(from: assetDirectory)
    }
}
