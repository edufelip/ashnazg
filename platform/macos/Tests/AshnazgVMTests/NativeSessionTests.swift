import Foundation
import XCTest
import CryptoKit
import Darwin
@testable import AshnazgVM

final class NativeSessionTests: XCTestCase {
    func testAgentArgumentsArePairedAndPortsAreDistinct() throws {
        XCTAssertThrowsError(try HelperOptions(arguments: ["--assets", "/tmp/a", "--agent-apk", "/tmp/a.apk"]))
        XCTAssertThrowsError(try HelperOptions(arguments: ["--assets", "/tmp/a", "--agent-apk", "/tmp/a.apk", "--agent-sha256", String(repeating: "A", count: 64)]))
        XCTAssertThrowsError(try HelperOptions(arguments: ["--assets", "/tmp/a", "--agent-apk", "/tmp/a.apk", "--agent-sha256", String(repeating: "a", count: 64), "--install-port", "15555"]))
        XCTAssertThrowsError(try HelperOptions(arguments: ["--assets", "/tmp/a", "--adb-port", "17777"]))
        XCTAssertThrowsError(try HelperOptions(arguments: ["--assets", "/tmp/a", "--agent-port", "80"]))
        let vmOnly = try HelperOptions(arguments: ["--assets", "/tmp/a"])
        XCTAssertNil(vmOnly.agentAPK)
        let opted = try HelperOptions(arguments: ["--assets", "/tmp/a", "--agent-apk", "/tmp/a.apk", "--agent-sha256", String(repeating: "a", count: 64)])
        XCTAssertEqual(opted.agentPort, 17777)
        XCTAssertEqual(opted.installPort, 17778)
    }

    func testPrepareResponsesAreStrictAndRejectBooleanSchema() throws {
        let session = "6f6f1234-1234-4234-8234-123456789abc"
        XCTAssertNoThrow(try parseAgentReadyResponse(Data("{\"schema_version\":1,\"status\":\"agent_ready\",\"session_id\":\"\(session)\"}\n".utf8), expectedSessionID: session))
        XCTAssertThrowsError(try parseAgentReadyResponse(Data("{\"schema_version\":true,\"status\":\"agent_ready\",\"session_id\":\"\(session)\"}\n".utf8), expectedSessionID: session))
        XCTAssertThrowsError(try parseAgentReadyResponse(Data("{\"schema_version\":1,\"status\":\"agent_ready\",\"session_id\":\"\(session)\",\"error\":\"no\"}\n".utf8), expectedSessionID: session))
        XCTAssertThrowsError(try parseAgentReadyResponse(Data("{\"schema_version\":1,\"status\":\"agent_ready\",\"session_id\":\"not-a-uuid\"}\n".utf8), expectedSessionID: session))
        XCTAssertThrowsError(try parseAgentAck(Data("{\"schema_version\":1,\"schema_version\":1,\"status\":\"ready_for_payload\"}\n".utf8)))
    }

    func testRandomTokensAreDistinctLowercaseAndRedactedFromDescription() throws {
        var values = [Data(repeating: 0x11, count: 32), Data(repeating: 0x22, count: 32)]
        let identity = try makeAgentSessionIdentity { _ in values.removeFirst() }
        XCTAssertEqual(identity.rpcToken, String(repeating: "11", count: 32))
        XCTAssertEqual(identity.installToken, String(repeating: "22", count: 32))
        XCTAssertNotEqual(identity.rpcToken, identity.installToken)
        XCTAssertFalse(String(describing: identity).contains(identity.rpcToken))
        XCTAssertThrowsError(try makeAgentSessionIdentity { _ in Data(repeating: 1, count: 32) })
    }

    func testAgentAPKRequiresNoFollowRegularBoundedFileAndExpectedHash() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let apk = root.appendingPathComponent("agent.apk"), bytes = Data("apk fixture".utf8)
        try bytes.write(to: apk)
        let digest = Self.sha(bytes)
        let opened = try OpenedAgentAPK(path: apk, expectedSHA256: digest)
        XCTAssertEqual(opened.size, bytes.count); XCTAssertEqual(opened.sha256, digest); opened.close()
        XCTAssertThrowsError(try OpenedAgentAPK(path: apk, expectedSHA256: String(repeating: "0", count: 64)))
        let link = root.appendingPathComponent("link.apk")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: apk)
        XCTAssertThrowsError(try OpenedAgentAPK(path: link, expectedSHA256: digest))
        let empty = root.appendingPathComponent("empty.apk"); try Data().write(to: empty)
        XCTAssertThrowsError(try OpenedAgentAPK(path: empty, expectedSHA256: Self.sha(Data())))
        let oversized = root.appendingPathComponent("oversized.apk")
        let oversizedFD = open(oversized.path, O_CREAT | O_WRONLY, 0o600); XCTAssertGreaterThanOrEqual(oversizedFD, 0)
        XCTAssertEqual(ftruncate(oversizedFD, off_t(128 * 1024 * 1024 + 1)), 0); Darwin.close(oversizedFD)
        XCTAssertThrowsError(try OpenedAgentAPK(path: oversized, expectedSHA256: String(repeating: "0", count: 64)))
    }

    func testPrepareWaitsForACKThenSendsExactBodyAndHalfCloses() throws {
        let bytes = Data("known apk".utf8), digest = Self.sha(bytes)
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("agent.apk"); try bytes.write(to: path)
        let apk = try OpenedAgentAPK(path: path, expectedSHA256: digest); defer { apk.close() }
        let id = AgentSessionIdentity(sessionID: "6f6f1234-1234-4234-8234-123456789abc", rpcToken: String(repeating: "1", count: 64), installToken: String(repeating: "2", count: 64))
        var pair: [Int32] = [0, 0]; XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &pair), 0)
        for fd in pair { configureNoSIGPIPE(fd) }
        defer { Darwin.close(pair[0]); Darwin.close(pair[1]) }
        let clientFD = pair[0], peerFD = pair[1]
        let finished = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            defer { finished.signal() }
            let header: String
            do { header = try self.readSocketLine(peerFD) }
            catch { return }
            guard header.contains("prepare_agent"), !header.contains("\"bytes\"") else { return }
            try? self.writeSocket(Data("{\"schema_version\":1,\"status\":\"ready_for_payload\"}\n".utf8), to: peerFD)
            guard (try? self.readSocketBytes(peerFD, count: bytes.count)) == bytes else { return }
            var eof: UInt8 = 0
            guard Darwin.read(peerFD, &eof, 1) == 0 else { return }
            try? self.writeSocket(Data("{\"schema_version\":1,\"status\":\"agent_ready\",\"session_id\":\"\(id.sessionID)\"}\n".utf8), to: peerFD)
        }
        let deadline = StartupDeadline(after: .seconds(3))
        XCTAssertNoThrow(try sendAgentPrepare(fd: clientFD, apk: apk, identity: id, wholeDeadline: deadline))
        XCTAssertEqual(finished.wait(timeout: .now() + 1), .success)
    }

    func testPrepareRejectingACKNeverSendsPayload() throws {
        let bytes = Data("apk".utf8), root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("agent.apk"); try bytes.write(to: path)
        let apk = try OpenedAgentAPK(path: path, expectedSHA256: Self.sha(bytes)); defer { apk.close() }
        let identity = AgentSessionIdentity(sessionID: "6f6f1234-1234-4234-8234-123456789abc", rpcToken: String(repeating: "1", count: 64), installToken: String(repeating: "2", count: 64))
        var pair: [Int32] = [0, 0]; XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &pair), 0)
        for fd in pair { configureNoSIGPIPE(fd) }
        defer { Darwin.close(pair[0]); Darwin.close(pair[1]) }
        let peer = pair[1]
        let finished = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            defer { finished.signal() }
            _ = try? self.readSocketLine(peer)
            try? self.writeSocket(Data("{\"schema_version\":1,\"status\":\"no\"}\n".utf8), to: peer)
            var item = pollfd(fd: peer, events: Int16(POLLIN), revents: 0)
            XCTAssertEqual(poll(&item, 1, 100), 0, "unexpected APK payload before a valid ACK")
        }
        XCTAssertThrowsError(try sendAgentPrepare(fd: pair[0], apk: apk, identity: identity, wholeDeadline: StartupDeadline(after: .seconds(2))))
        XCTAssertEqual(finished.wait(timeout: .now() + 1), .success)
    }

    func testCredentialIsPrivateNoClobberAndOwnedCleanupPreservesReplacement() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        XCTAssertEqual(chmod(root.path, 0o700), 0)
        defer { try? FileManager.default.removeItem(at: root) }
        let stateDirectory = try canonicalURL(root)
        let value = credentials()
        XCTAssertFalse(String(describing: value).contains(value.rpc_token))
        try OwnedAgentCredential.requireAbsent(stateDirectory: stateDirectory)
        let credential = try OwnedAgentCredential(stateDirectory: stateDirectory, credentials: value)
        let file = root.appendingPathComponent(".ashnazg-session-v1.json")
        let mode = try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber
        XCTAssertEqual(mode?.intValue, 0o600)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), Set(["schema_version", "session_id", "helper_pid", "manifest_source_sha", "agent_apk_sha256", "rpc_token", "install_token", "agent_listen", "install_listen"]))
        XCTAssertThrowsError(try OwnedAgentCredential(stateDirectory: stateDirectory, credentials: value))
        XCTAssertThrowsError(try OwnedAgentCredential.requireAbsent(stateDirectory: stateDirectory))
        try FileManager.default.removeItem(at: file)
        try Data("replacement".utf8).write(to: file)
        XCTAssertThrowsError(try credential.removeOwned())
        XCTAssertEqual(try Data(contentsOf: file), Data("replacement".utf8))
    }

    func testCredentialCleanupRemovesOwnedFile() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true); XCTAssertEqual(chmod(root.path, 0o700), 0)
        defer { try? FileManager.default.removeItem(at: root) }
        let credential = try OwnedAgentCredential(stateDirectory: canonicalURL(root), credentials: credentials())
        try credential.removeOwned()
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent(".ashnazg-session-v1.json").path))
    }

    func testCredentialRejectsSymlinkAndUnsafeDirectoryMode() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let target = root.appendingPathComponent("target")
        try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true); XCTAssertEqual(chmod(target.path, 0o700), 0)
        defer { try? FileManager.default.removeItem(at: root) }
        let alias = root.appendingPathComponent("alias"); try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: target)
        XCTAssertThrowsError(try OwnedAgentCredential(stateDirectory: alias, credentials: credentials()))
        XCTAssertEqual(chmod(target.path, 0o755), 0)
        XCTAssertThrowsError(try OwnedAgentCredential(stateDirectory: canonicalURL(target), credentials: credentials()))
    }

    func testPublicReceiptsNeverContainAgentTokensAndVMOnlyShapeIsUnchanged() throws {
        let receipt = RuntimeReceipt(status: "agent_ready", adbListen: "127.0.0.1:15555", assets: nil,
                                    serial: "127.0.0.1:5555", timings: StartupTimings(total_startup_ms: 22, agent_startup_ms: 11),
                                    sessionID: "6f6f1234-1234-4234-8234-123456789abc", agentListen: "127.0.0.1:17777",
                                    installListen: "127.0.0.1:17778", agentAPKSHA256: String(repeating: "b", count: 64))
        let json = try JSONEncoder.sorted.encode(receipt).asUTF8()
        XCTAssertFalse(json.contains(String(repeating: "1", count: 64)))
        XCTAssertFalse(json.contains(String(repeating: "2", count: 64)))
        XCTAssertTrue(json.contains("agent_startup_ms"))
        let vmOnly = RuntimeReceipt(status: "ready", adbListen: "127.0.0.1:15555", assets: nil,
                                    timings: StartupTimings(total_startup_ms: 5))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder.sorted.encode(vmOnly)) as? [String: Any])
        XCTAssertNil(object["session_id"]); XCTAssertNil(object["agent_listen"]); XCTAssertNil(object["install_listen"]); XCTAssertNil(object["agent_apk_sha256"])
    }

    func testPrepareACKTimeoutUsesOriginalBoundAndSendsNoPayload() throws {
        let bytes = Data("apk".utf8), root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let path = root.appendingPathComponent("agent.apk"); try bytes.write(to: path)
        let apk = try OpenedAgentAPK(path: path, expectedSHA256: Self.sha(bytes)); defer { apk.close() }
        let identity = AgentSessionIdentity(sessionID: "6f6f1234-1234-4234-8234-123456789abc", rpcToken: String(repeating: "1", count: 64), installToken: String(repeating: "2", count: 64))
        var pair: [Int32] = [0, 0]; XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &pair), 0)
        for fd in pair { configureNoSIGPIPE(fd) }
        defer { Darwin.close(pair[0]); Darwin.close(pair[1]) }
        let peer = pair[1], elapsed = DispatchTime.now().uptimeNanoseconds
        let peerDone = DispatchSemaphore(value: 0)
        DispatchQueue.global().async { _ = try? self.readSocketLine(peer); peerDone.signal() }
        XCTAssertThrowsError(try sendAgentPrepare(fd: pair[0], apk: apk, identity: identity, wholeDeadline: StartupDeadline(after: .milliseconds(70))))
        XCTAssertLessThan(DispatchTime.now().uptimeNanoseconds - elapsed, 500_000_000)
        Darwin.shutdown(pair[0], SHUT_RDWR)
        XCTAssertEqual(peerDone.wait(timeout: .now() + 1), .success)
    }

    func testRelayPreservesHalfCloseUntilOppositeReplyIsDrained() throws {
        var clientPair: [Int32] = [0, 0], guestPair: [Int32] = [0, 0]
        XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &clientPair), 0)
        XCTAssertEqual(socketpair(AF_UNIX, SOCK_STREAM, 0, &guestPair), 0)
        for fd in clientPair + guestPair { configureNoSIGPIPE(fd) }
        defer { for fd in clientPair + guestPair { Darwin.shutdown(fd, SHUT_RDWR); Darwin.close(fd) } }
        let client = clientPair[0], clientApp = clientPair[1]
        let guest = guestPair[0], guestApp = guestPair[1]
        let work = DispatchGroup(); work.enter()
        DispatchQueue.global().async { RelayServer.bridge(client, guest); work.leave() }
        try writeSocket(Data("request".utf8), to: clientApp)
        XCTAssertEqual(Darwin.shutdown(clientApp, SHUT_WR), 0)
        XCTAssertEqual(try readSocketBytes(guestApp, count: 7), Data("request".utf8))
        var byte: UInt8 = 0
        XCTAssertEqual(Darwin.read(guestApp, &byte, 1), 0, "guest must observe propagated request EOF")
        try writeSocket(Data("reply\n".utf8), to: guestApp)
        XCTAssertEqual(try readSocketLine(clientApp), "reply")
        XCTAssertEqual(Darwin.shutdown(guestApp, SHUT_WR), 0)
        XCTAssertEqual(work.wait(timeout: .now() + 2), .success)
    }

    private func writeSocket(_ data: Data, to fd: Int32) throws {
        try data.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let n = Darwin.write(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
                if n < 0 && errno == EINTR { continue }
                guard n > 0 else { throw NSError(domain: "socket", code: Int(errno)) }
                offset += n
            }
        }
    }
    private func readSocketLine(_ fd: Int32) throws -> String {
        var bytes = Data()
        while bytes.count < 4096 {
            var byte: UInt8 = 0
            let n = Darwin.read(fd, &byte, 1)
            if n < 0 && errno == EINTR { continue }
            guard n == 1 else { throw NSError(domain: "socket", code: Int(errno)) }
            if byte == 10 { return String(decoding: bytes, as: UTF8.self) }
            bytes.append(byte)
        }
        throw NSError(domain: "socket", code: 1)
    }
    private func readSocketBytes(_ fd: Int32, count: Int) throws -> Data {
        var bytes = Data(count: count)
        try bytes.withUnsafeMutableBytes { raw in
            var offset = 0
            while offset < count {
                let n = Darwin.read(fd, raw.baseAddress!.advanced(by: offset), count - offset)
                if n < 0 && errno == EINTR { continue }
                guard n > 0 else { throw NSError(domain: "socket", code: Int(errno)) }
                offset += n
            }
        }
        return bytes
    }
    private func credentials() -> AgentSessionCredentials {
        AgentSessionCredentials(session_id: "6f6f1234-1234-4234-8234-123456789abc", helper_pid: getpid(), manifest_source_sha: String(repeating: "a", count: 40), agent_apk_sha256: String(repeating: "b", count: 64), rpc_token: String(repeating: "1", count: 64), install_token: String(repeating: "2", count: 64), agent_listen: "127.0.0.1:17777", install_listen: "127.0.0.1:17778")
    }
    private func canonicalURL(_ url: URL) throws -> URL {
        var info = stat(); guard lstat(url.path, &info) == 0 else { throw NSError(domain: "path", code: Int(errno)) }
        guard let path = realpath(url.path, nil) else { throw NSError(domain: "path", code: Int(errno)) }
        defer { free(path) }; return URL(fileURLWithPath: String(cString: path))
    }
    private static func sha(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
}
