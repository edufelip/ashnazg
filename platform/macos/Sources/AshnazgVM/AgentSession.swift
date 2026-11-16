import Foundation
import CryptoKit
import Security
import Darwin
import CoreFoundation

func isLowercaseSHA256(_ value: String) -> Bool {
    value.utf8.count == 64 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}

struct OpenedAgentAPK {
    let fd: Int32
    let size: Int
    let sha256: String
    init(path: URL, expectedSHA256: String) throws {
        guard isLowercaseSHA256(expectedSHA256) else { throw AssetError.invalid("invalid agent APK digest") }
        let descriptor = open(path.path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw AssetError.invalid("cannot open agent APK safely") }
        var info = stat()
        guard fstat(descriptor, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              (1...(128 * 1024 * 1024)).contains(Int(info.st_size)) else {
            Darwin.close(descriptor); throw AssetError.invalid("agent APK must be a regular file from 1 through 128 MiB")
        }
        do {
            let digest = try Self.hash(fd: descriptor, size: Int(info.st_size))
            var after = stat()
            guard fstat(descriptor, &after) == 0, after.st_dev == info.st_dev, after.st_ino == info.st_ino,
                  after.st_size == info.st_size, after.st_mtimespec.tv_sec == info.st_mtimespec.tv_sec, after.st_mtimespec.tv_nsec == info.st_mtimespec.tv_nsec,
                  digest == expectedSHA256 else { throw AssetError.invalid("agent APK changed or does not match its expected digest") }
            fd = descriptor; size = Int(info.st_size); sha256 = digest
        } catch { Darwin.close(descriptor); throw error }
    }
    func close() { Darwin.close(fd) }
    private static func hash(fd: Int32, size: Int) throws -> String {
        var hasher = SHA256(); var offset: off_t = 0; var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while offset < off_t(size) {
            let wanted = min(buffer.count, size - Int(offset))
            let n = pread(fd, &buffer, wanted, offset)
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw AssetError.invalid("cannot read agent APK") }
            hasher.update(data: Data(buffer[0..<n])); offset += off_t(n)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

struct AgentSessionCredentials: Encodable {
    let schema_version = 1
    let session_id: String
    let helper_pid: Int32
    let manifest_source_sha: String
    let agent_apk_sha256: String
    let rpc_token: String
    let install_token: String
    let agent_listen: String
    let install_listen: String
}

final class OwnedAgentCredential {
    private let directoryFD: Int32
    private let fileName = ".ashnazg-session-v1.json"
    private var fileDevice: dev_t = 0
    private var fileInode: ino_t = 0
    private var closed = false

    static func requireAbsent(stateDirectory: URL) throws {
        let fd = open(stateDirectory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw AssetError.invalid("cannot inspect private session credential path safely") }
        defer { Darwin.close(fd) }
        var info = stat()
        let result = ".ashnazg-session-v1.json".withCString { fstatat(fd, $0, &info, AT_SYMLINK_NOFOLLOW) }
        guard result != 0, errno == ENOENT else { throw AssetError.invalid("refusing a pre-existing session credential") }
    }

    init(stateDirectory: URL, credentials: AgentSessionCredentials) throws {
        let path = stateDirectory.path
        var directoryInfo = stat()
        let real = realpath(path, nil)
        defer { if let real { free(real) } }
        // Foundation may use macOS's /tmp and /var aliases for their /private
        // targets. Permit only those exact system-prefix identities. Reject
        // traversal before normalization so a custom symlink cannot be erased.
        guard let real,
              !path.split(separator: "/").contains(".."),
              (String(cString: real) == path ||
               ((path.hasPrefix("/tmp/") || path.hasPrefix("/var/")) && String(cString: real) == "/private" + path)),
              lstat(path, &directoryInfo) == 0, directoryInfo.st_mode & S_IFMT == S_IFDIR,
              directoryInfo.st_uid == getuid(), directoryInfo.st_mode & 0o7777 == 0o700 else {
            throw AssetError.invalid("state directory is not canonical, private, current-user-owned and non-symlink")
        }
        let dir = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard dir >= 0 else { throw AssetError.invalid("cannot open state directory safely") }
        var heldInfo = stat()
        guard fstat(dir, &heldInfo) == 0, heldInfo.st_dev == directoryInfo.st_dev, heldInfo.st_ino == directoryInfo.st_ino else {
            Darwin.close(dir); throw AssetError.invalid("state directory changed during credential setup")
        }
        directoryFD = dir
        let temporary = ".ashnazg-session-v1.json.tmp-\(UUID().uuidString.lowercased())"
        let fd = temporary.withCString { openat(dir, $0, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0o600) }
        guard fd >= 0 else { Darwin.close(dir); throw AssetError.invalid("cannot create private session credential") }
        var createdIdentity = stat()
        do {
            let bytes = try JSONEncoder.sorted.encode(credentials)
            try bytes.withUnsafeBytes { raw in
                var offset = 0
                while offset < raw.count {
                    let n = Darwin.write(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
                    if n < 0 && errno == EINTR { continue }
                    guard n > 0 else { throw AssetError.invalid("cannot write private session credential") }
                    offset += n
                }
            }
            guard fsync(fd) == 0 else { throw AssetError.invalid("cannot sync private session credential") }
            var info = stat()
            guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG, info.st_uid == getuid(), info.st_mode & 0o777 == 0o600 else {
                throw AssetError.invalid("session credential permissions are unsafe")
            }
            createdIdentity = info
            let promoteResult = temporary.withCString { source in fileName.withCString { target in linkat(dir, source, dir, target, 0) } }
            guard promoteResult == 0 else { throw AssetError.invalid("refusing an existing session credential or unsafe credential path") }
            self.fileDevice = info.st_dev; self.fileInode = info.st_ino
            _ = temporary.withCString { unlinkat(dir, $0, 0) }
            guard fsync(dir) == 0 else { throw AssetError.invalid("cannot sync session credential directory") }
            Darwin.close(fd)
        } catch {
            Darwin.close(fd)
            _ = temporary.withCString { unlinkat(dir, $0, 0) }
            var current = stat()
            if fileName.withCString({ fstatat(dir, $0, &current, AT_SYMLINK_NOFOLLOW) }) == 0,
               current.st_dev == createdIdentity.st_dev, current.st_ino == createdIdentity.st_ino {
                _ = fileName.withCString { unlinkat(dir, $0, 0) }
            }
            Darwin.close(dir)
            throw error
        }
    }

    func removeOwned() throws {
        guard !closed else { return }
        closed = true
        var current = stat()
        let result = fileName.withCString { fstatat(directoryFD, $0, &current, AT_SYMLINK_NOFOLLOW) }
        if result == 0 {
            guard current.st_mode & S_IFMT == S_IFREG, current.st_dev == fileDevice, current.st_ino == fileInode else {
                Darwin.close(directoryFD); throw AssetError.invalid("session credential was replaced; preserving observed replacement")
            }
            let removed = fileName.withCString { unlinkat(directoryFD, $0, 0) }
            guard removed == 0 else { Darwin.close(directoryFD); throw AssetError.invalid("cannot remove owned session credential") }
            _ = fsync(directoryFD)
        } else if errno != ENOENT { Darwin.close(directoryFD); throw AssetError.invalid("cannot inspect session credential during cleanup") }
        Darwin.close(directoryFD)
    }
    deinit { if !closed { try? removeOwned() } }
}

extension AgentSessionCredentials: CustomStringConvertible {
    var description: String { "AgentSessionCredentials(session_id: \(session_id), credentials: <redacted>)" }
}

struct AgentSessionIdentity {
    let sessionID: String
    let rpcToken: String
    let installToken: String
}
extension AgentSessionIdentity: CustomStringConvertible {
    var description: String { "AgentSessionIdentity(sessionID: \(sessionID), credentials: <redacted>)" }
}

func makeAgentSessionIdentity(randomBytes: (Int) throws -> Data = secureRandomBytes) throws -> AgentSessionIdentity {
    let uuid = UUID().uuidString.lowercased()
    let rpc = try randomBytes(32), install = try randomBytes(32)
    guard rpc.count == 32, install.count == 32, rpc != install else { throw AssetError.invalid("cryptographic session randomness failed") }
    return AgentSessionIdentity(sessionID: uuid, rpcToken: hex(rpc), installToken: hex(install))
}

private func secureRandomBytes(_ count: Int) throws -> Data {
    var bytes = [UInt8](repeating: 0, count: count)
    guard SecRandomCopyBytes(kSecRandomDefault, count, &bytes) == errSecSuccess else { throw AssetError.invalid("cryptographic session randomness unavailable") }
    return Data(bytes)
}
private func hex(_ data: Data) -> String { data.map { String(format: "%02x", $0) }.joined() }

func parseAgentAck(_ data: Data) throws {
    let object = try strictResponseObject(data, keys: ["schema_version", "status"])
    guard exactInteger(object["schema_version"]) == 1, object["status"] as? String == "ready_for_payload" else {
        throw AssetError.invalid("guest returned an invalid agent upload acknowledgement")
    }
}

func parseAgentReadyResponse(_ data: Data, expectedSessionID: String) throws {
    let object = try strictResponseObject(data, keys: ["schema_version", "status", "session_id"])
    guard exactInteger(object["schema_version"]) == 1,
          object["status"] as? String == "agent_ready",
          object["session_id"] as? String == expectedSessionID,
          UUID(uuidString: expectedSessionID)?.uuidString.lowercased() == expectedSessionID else {
        throw AssetError.invalid("guest returned an invalid agent readiness response")
    }
}

private func strictResponseObject(_ data: Data, keys: Set<String>) throws -> [String: Any] {
    guard data.count <= 4096, let line = String(data: data, encoding: .utf8), line.hasSuffix("\n"),
          !line.utf8.contains(92), // Fixed ASCII protocol fields need no JSON escapes.
          !line.dropLast().contains("\n"),
          let object = try JSONSerialization.jsonObject(with: Data(line.dropLast().utf8), options: [.fragmentsAllowed]) as? [String: Any],
          Set(object.keys) == keys else { throw AssetError.invalid("guest returned a malformed session response") }
    for key in keys where line.components(separatedBy: "\"\(key)\"").count != 2 {
        throw AssetError.invalid("guest returned a malformed session response")
    }
    return object
}

private func exactInteger(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
          !["f", "d"].contains(String(cString: number.objCType)) else { return nil }
    return number.intValue
}

func sendAgentPrepare(fd: Int32, apk: OpenedAgentAPK, identity: AgentSessionIdentity,
                      wholeDeadline: StartupDeadline, signals: SignalController? = nil) throws {
    let previousFlags = fcntl(fd, F_GETFL)
    guard previousFlags >= 0, fcntl(fd, F_SETFL, previousFlags | O_NONBLOCK) == 0 else {
        throw AssetError.invalid("cannot bound agent preparation socket operations")
    }
    defer { _ = fcntl(fd, F_SETFL, previousFlags) }
    let uploadDeadline = StartupDeadline(after: .milliseconds(min(30_000, wholeDeadline.remainingMilliseconds)))
    let header: [String: Any] = ["schema_version": 1, "op": "prepare_agent", "session_id": identity.sessionID,
                                 "rpc_token": identity.rpcToken, "install_token": identity.installToken,
                                 "apk_size": apk.size, "apk_sha256": apk.sha256]
    var headerData = try JSONSerialization.data(withJSONObject: header, options: [.sortedKeys]); headerData.append(10)
    guard headerData.count <= 4096 else { throw AssetError.invalid("agent preparation header exceeded its bound") }
    try writeAll(fd, headerData, deadline: uploadDeadline, signals: signals)
    try parseAgentAck(readLine(fd, maxBytes: 4096, deadline: uploadDeadline, signals: signals))
    var offset: off_t = 0; var bytes = [UInt8](repeating: 0, count: 64 * 1024)
    while offset < off_t(apk.size) {
        try signals?.throwIfSignalled()
        guard wholeDeadline.remainingMilliseconds > 0 else { throw AssetError.invalid("agent preparation deadline expired") }
        try uploadDeadline.pollWritable(fd, signals: signals)
        let wanted = min(bytes.count, apk.size - Int(offset))
        let n = pread(apk.fd, &bytes, wanted, offset)
        if n < 0 && errno == EINTR { continue }
        guard n > 0 else { throw AssetError.invalid("agent APK changed or became unreadable during upload") }
        try writeAll(fd, Data(bytes[0..<n]), deadline: uploadDeadline, signals: signals)
        offset += off_t(n)
    }
    guard Darwin.shutdown(fd, SHUT_WR) == 0 else { throw AssetError.invalid("cannot finish agent APK upload") }
    try parseAgentReadyResponse(readLine(fd, maxBytes: 4096, deadline: wholeDeadline, signals: signals), expectedSessionID: identity.sessionID)
}

private final class OwnedPreparationSocket: @unchecked Sendable {
    let fd: Int32
    private let lock = NSLock()
    private var finished = false
    init(borrowing descriptor: Int32) throws {
        let copy = dup(descriptor)
        guard copy >= 0 else { throw AssetError.invalid("cannot retain agent preparation socket") }
        guard fcntl(copy, F_SETFD, FD_CLOEXEC) == 0 else {
            Darwin.close(copy); throw AssetError.invalid("cannot protect agent preparation socket")
        }
        fd = copy
    }
    func cancel() {
        lock.lock(); defer { lock.unlock() }
        if !finished { Darwin.shutdown(fd, SHUT_RDWR) }
    }
    func finish() {
        lock.lock(); defer { lock.unlock() }
        guard !finished else { return }
        finished = true
        Darwin.close(fd)
    }
}

func prepareAgentSession(fd: Int32, apk: OpenedAgentAPK, identity: AgentSessionIdentity,
                         wholeDeadline: StartupDeadline, signals: SignalController? = nil) async throws {
    try Task.checkCancellation()
    let owned = try OwnedPreparationSocket(borrowing: fd)
    defer { owned.finish() }
    let transfer = Task.detached(priority: .userInitiated) {
        try Task.checkCancellation()
        try sendAgentPrepare(fd: owned.fd, apk: apk, identity: identity,
                             wholeDeadline: wholeDeadline, signals: signals)
        try Task.checkCancellation()
    }
    do {
        try await withTaskCancellationHandler(operation: {
            try await transfer.value
        }, onCancel: {
            transfer.cancel()
            owned.cancel()
        })
    } catch {
        try Task.checkCancellation()
        throw error
    }
}
