import Foundation
import Virtualization
import Darwin
import CryptoKit

let stateMarker = ".ashnazg-owned-state-v1"

struct HelperOptions {
    var assets: URL?
    var stateDir: URL?
    var adbPort = 15555
    var agentAPK: URL?
    var agentSHA256: String?
    var agentPort = 17777
    var installPort = 17778
    var timeout = 300
    var validateOnly = false
    let diagnosticsEnabled: Bool

    init(arguments: [String], environment: [String: String] = ProcessInfo.processInfo.environment) throws {
        diagnosticsEnabled = environment["ASHNAZG_CI_DIAGNOSTICS"] == "1"
        var index = 0
        while index < arguments.count {
            let flag = arguments[index]
            if flag == "--validate-only" { validateOnly = true; index += 1; continue }
            guard ["--assets", "--state-dir", "--adb-port", "--timeout", "--agent-apk", "--agent-sha256", "--agent-port", "--install-port"].contains(flag), index + 1 < arguments.count else {
                throw AssetError.invalid("invalid argument near \(flag); usage: ashnazg-vm --assets DIR --state-dir DIR [--adb-port PORT] [--timeout SECONDS] [--agent-apk PATH --agent-sha256 HEX [--agent-port PORT --install-port PORT]] [--validate-only]")
            }
            let value = arguments[index + 1]
            switch flag {
            case "--assets": assets = URL(fileURLWithPath: value).standardizedFileURL
            case "--state-dir": stateDir = URL(fileURLWithPath: value).standardizedFileURL
            case "--adb-port": guard let parsed = Int(value), (1024...65535).contains(parsed) else { throw AssetError.invalid("--adb-port must be between 1024 and 65535") }; adbPort = parsed
            case "--timeout": guard let parsed = Int(value), (15...1800).contains(parsed) else { throw AssetError.invalid("--timeout must be between 15 and 1800 seconds") }; timeout = parsed
            case "--agent-apk": agentAPK = URL(fileURLWithPath: value).standardizedFileURL
            case "--agent-sha256": agentSHA256 = value
            case "--agent-port": guard let parsed = Int(value), (1024...65535).contains(parsed) else { throw AssetError.invalid("--agent-port must be between 1024 and 65535") }; agentPort = parsed
            case "--install-port": guard let parsed = Int(value), (1024...65535).contains(parsed) else { throw AssetError.invalid("--install-port must be between 1024 and 65535") }; installPort = parsed
            default: break
            }
            index += 2
        }
        guard assets != nil else { throw AssetError.invalid("missing --assets DIR; provide a verified kernel/rootfs bundle") }
        guard (agentAPK == nil) == (agentSHA256 == nil) else { throw AssetError.invalid("--agent-apk and --agent-sha256 must be provided together") }
        if let hash = agentSHA256 { guard isLowercaseSHA256(hash) else { throw AssetError.invalid("--agent-sha256 must be 64 lowercase hexadecimal characters") } }
        guard Set([adbPort, agentPort, installPort]).count == 3 else { throw AssetError.invalid("ADB, agent and install ports must be pairwise distinct") }
    }
}

struct StartupTimings: Encodable {
    var vm_start_ms: UInt64? = nil
    var readiness_ms: UInt64? = nil
    var total_startup_ms: UInt64
    var agent_startup_ms: UInt64? = nil
    private enum CodingKeys: String, CodingKey { case vm_start_ms, readiness_ms, total_startup_ms, agent_startup_ms }
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(vm_start_ms, forKey: .vm_start_ms)
        try container.encodeIfPresent(readiness_ms, forKey: .readiness_ms)
        try container.encode(total_startup_ms, forKey: .total_startup_ms)
        try container.encodeIfPresent(agent_startup_ms, forKey: .agent_startup_ms)
    }
}

struct RuntimeReceipt: Encodable {
    let schema_version = 1
    let status: String
    let architecture = "aarch64"
    let cpu_count: Int
    let memory_bytes: UInt64
    let adb_listen: String
    let android_serial: String?
    let manifest_version: String?
    let manifest_source_sha: String?
    let kernel_sha256: String?
    let rootfs_template_sha256: String?
    let timings: StartupTimings
    let error: String?
    let session_id: String?
    let agent_listen: String?
    let install_listen: String?
    let agent_apk_sha256: String?

    private enum CodingKeys: String, CodingKey {
        case schema_version, status, architecture, cpu_count, memory_bytes, adb_listen, android_serial
        case manifest_version, manifest_source_sha, kernel_sha256, rootfs_template_sha256, timings, error
        case session_id, agent_listen, install_listen, agent_apk_sha256
    }
    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(schema_version, forKey: .schema_version); try container.encode(status, forKey: .status)
        try container.encode(architecture, forKey: .architecture); try container.encode(cpu_count, forKey: .cpu_count)
        try container.encode(memory_bytes, forKey: .memory_bytes); try container.encode(adb_listen, forKey: .adb_listen)
        try container.encodeIfPresent(android_serial, forKey: .android_serial); try container.encodeIfPresent(manifest_version, forKey: .manifest_version)
        try container.encodeIfPresent(manifest_source_sha, forKey: .manifest_source_sha); try container.encodeIfPresent(kernel_sha256, forKey: .kernel_sha256)
        try container.encodeIfPresent(rootfs_template_sha256, forKey: .rootfs_template_sha256); try container.encode(timings, forKey: .timings)
        try container.encodeIfPresent(error, forKey: .error); try container.encodeIfPresent(session_id, forKey: .session_id)
        try container.encodeIfPresent(agent_listen, forKey: .agent_listen); try container.encodeIfPresent(install_listen, forKey: .install_listen)
        try container.encodeIfPresent(agent_apk_sha256, forKey: .agent_apk_sha256)
    }

    init(status: String, adbListen: String, assets: ValidatedAssets?, cpuCount: Int = VZConfigFactory.cpuCount,
         memorySize: UInt64 = VZConfigFactory.memorySize, serial: String? = nil,
         timings: StartupTimings, error: String? = nil, sessionID: String? = nil,
         agentListen: String? = nil, installListen: String? = nil, agentAPKSHA256: String? = nil) {
        self.status = status; self.adb_listen = adbListen; self.cpu_count = cpuCount; self.memory_bytes = memorySize
        self.android_serial = serial; self.manifest_version = assets?.manifest.version
        self.manifest_source_sha = assets?.manifest.source_sha; self.kernel_sha256 = assets?.kernelSHA256
        self.rootfs_template_sha256 = assets?.rootfsSHA256; self.timings = timings; self.error = error
        self.session_id = sessionID; self.agent_listen = agentListen; self.install_listen = installListen; self.agent_apk_sha256 = agentAPKSHA256
    }
}

final class SerialLog {
    private let handle: FileHandle
    init(url: URL) throws {
        let fd = open(url.path, O_CREAT | O_APPEND | O_WRONLY | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw AssetError.invalid("cannot open serial.log: \(String(cString: strerror(errno)))") }
        handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    }
    func append(_ data: Data) { try? handle.write(contentsOf: data) }
}

final class OwnedState {
    let directory: URL
    let disk: URL
    let receipt: URL
    let serial: URL
    private var lockFD: Int32

    init(_ directory: URL, assets: ValidatedAssets) throws {
        self.directory = directory
        disk = directory.appendingPathComponent("rootfs.ext4")
        receipt = directory.appendingPathComponent("runtime.json")
        serial = directory.appendingPathComponent("serial.log")
        if let kind = lstatKind(directory.path) {
            guard kind == S_IFDIR else { throw AssetError.invalid("refusing state path that is not a real directory: \(directory.path)") }
        } else {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        }
        guard lstatKind(directory.path) == S_IFDIR else { throw AssetError.invalid("state path is not a real directory: \(directory.path)") }
        let existingNames = try FileManager.default.contentsOfDirectory(atPath: directory.path)
        if !existingNames.isEmpty {
            let existingMarker = directory.appendingPathComponent(stateMarker)
            try requireRegularFile(existingMarker)
            guard try String(contentsOf: existingMarker, encoding: .utf8) == "ashnazg-owned-state-v1\n" else {
                throw AssetError.invalid("refusing non-empty unowned state directory")
            }
        }
        guard chmod(directory.path, 0o700) == 0 else { throw AssetError.invalid("cannot restrict state directory permissions: \(String(cString: strerror(errno)))") }
        let lockURL = directory.appendingPathComponent(".ashnazg.lock")
        let fd = open(lockURL.path, O_CREAT | O_RDWR | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw AssetError.invalid("cannot open state lock safely: \(String(cString: strerror(errno)))") }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG, fchmod(fd, 0o600) == 0 else { Darwin.close(fd); throw AssetError.invalid("state lock is not a private regular file") }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { Darwin.close(fd); throw AssetError.invalid("state directory is already in use by another ashnazg-vm process") }
        lockFD = fd
        do { try initializeOwnedFiles(assets: assets) }
        catch { Darwin.close(lockFD); lockFD = -1; throw error }
    }

    private func initializeOwnedFiles(assets: ValidatedAssets) throws {
        let marker = directory.appendingPathComponent(stateMarker)
        if lstatKind(marker.path) != nil {
            try requireRegularFile(marker)
            guard try String(contentsOf: marker, encoding: .utf8) == "ashnazg-owned-state-v1\n" else { throw AssetError.invalid("state ownership marker is invalid: \(marker.path)") }
        } else {
            let names = try FileManager.default.contentsOfDirectory(atPath: directory.path).filter { $0 != ".ashnazg.lock" }
            guard names.isEmpty else { throw AssetError.invalid("refusing non-empty unowned state directory: \(directory.path)") }
            let content = Data("ashnazg-owned-state-v1\n".utf8)
            let markerFD = open(marker.path, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, S_IRUSR | S_IWUSR)
            guard markerFD >= 0 else { throw AssetError.invalid("cannot create state ownership marker exclusively") }
            do {
                try content.withUnsafeBytes { raw in
                    var offset = 0
                    while offset < raw.count {
                        let amount = Darwin.write(markerFD, raw.baseAddress!.advanced(by: offset), raw.count - offset)
                        if amount < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) { continue }
                        guard amount > 0 else { throw AssetError.invalid("cannot write state ownership marker") }
                        offset += amount
                    }
                }
                guard fsync(markerFD) == 0 else { throw AssetError.invalid("cannot sync state ownership marker") }
                Darwin.close(markerFD)
            } catch { Darwin.close(markerFD); try? FileManager.default.removeItem(at: marker); throw error }
        }
        let identityURL = directory.appendingPathComponent("template.json")
        let diskURL = directory.appendingPathComponent("rootfs.ext4")
        let identity = TemplateIdentity(sourceSHA: assets.manifest.source_sha, rootfsSHA256: assets.rootfsSHA256)
        if lstatKind(identityURL.path) != nil {
            try requireRegularFile(identityURL)
            let existing = try JSONDecoder().decode(TemplateIdentity.self, from: Data(contentsOf: identityURL))
            guard existing == identity else { throw AssetError.invalid("persistent disk belongs to a different artifact template/source; refusing to alter user data") }
            if lstatKind(diskURL.path) != nil { try requireRegularFile(diskURL) }
            else { try FileManager.default.copyItem(at: assets.rootfs, to: diskURL) }
        } else {
            guard lstatKind(diskURL.path) == nil else { throw AssetError.invalid("persistent disk exists without template provenance; refusing to overwrite or adopt it") }
            let names = try FileManager.default.contentsOfDirectory(atPath: directory.path).filter { $0 != stateMarker && $0 != ".ashnazg.lock" }
            guard names.isEmpty else { throw AssetError.invalid("refusing unowned state content: \(names.joined(separator: ", "))") }
            let identityData = try JSONEncoder.sorted.encode(identity)
            try identityData.write(to: identityURL, options: .atomic)
            guard chmod(identityURL.path, 0o600) == 0 else { throw AssetError.invalid("cannot restrict template identity permissions") }
            try FileManager.default.copyItem(at: assets.rootfs, to: diskURL)
            guard chmod(diskURL.path, 0o600) == 0 else { throw AssetError.invalid("cannot restrict persistent disk permissions") }
        }
        for file in [identityURL, diskURL, directory.appendingPathComponent("runtime.json"), directory.appendingPathComponent("serial.log")] where lstatKind(file.path) != nil {
            try requireRegularFile(file)
            guard chmod(file.path, 0o600) == 0 else { throw AssetError.invalid("cannot restrict private state file permissions: \(file.lastPathComponent)") }
        }
    }

    func writeReceipt(_ receiptValue: RuntimeReceipt) throws {
        let data = try JSONEncoder.sorted.encode(receiptValue)
        let temporary = directory.appendingPathComponent("runtime.json.tmp")
        if lstatKind(temporary.path) != nil { try requireRegularFile(temporary); try FileManager.default.removeItem(at: temporary) }
        var fd = open(temporary.path, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, S_IRUSR | S_IWUSR)
        guard fd >= 0 else { throw AssetError.invalid("cannot create runtime receipt safely: \(String(cString: strerror(errno)))") }
        do {
            try data.withUnsafeBytes { raw in
                var offset = 0
                while offset < raw.count {
                    let n = Darwin.write(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
                    if n < 0 && errno == EINTR { continue }
                    guard n > 0 else { throw AssetError.invalid("cannot write runtime receipt: \(String(cString: strerror(errno)))") }
                    offset += n
                }
            }
            guard fsync(fd) == 0 else { throw AssetError.invalid("cannot sync runtime receipt") }
            Darwin.close(fd); fd = -1
            guard rename(temporary.path, receipt.path) == 0 else { throw AssetError.invalid("cannot atomically replace runtime receipt: \(String(cString: strerror(errno)))") }
        } catch { if fd >= 0 { Darwin.close(fd) }; try? FileManager.default.removeItem(at: temporary); throw error }
    }

    func close() { if lockFD >= 0 { flock(lockFD, LOCK_UN); Darwin.close(lockFD); lockFD = -1 } }
    deinit { close() }
}

private struct TemplateIdentity: Codable, Equatable {
    let sourceSHA: String
    let rootfsSHA256: String
}

extension JSONEncoder {
    static var sorted: JSONEncoder { let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]; return encoder }
}

struct StartupDeadline {
    private let end: UInt64
    init(after duration: Duration) {
        let components = duration.components
        let ns = UInt64(max(0, components.seconds)) &* 1_000_000_000 &+ UInt64(max(0, components.attoseconds / 1_000_000_000))
        end = DispatchTime.now().uptimeNanoseconds &+ ns
    }
    var isExpired: Bool { DispatchTime.now().uptimeNanoseconds >= end }
    var remainingMilliseconds: Int {
        let now = DispatchTime.now().uptimeNanoseconds
        guard end > now else { return 0 }
        return Int(min((end - now + 999_999) / 1_000_000, UInt64(Int32.max)))
    }
    func pollReadable(_ fd: Int32, signals: SignalController? = nil) throws {
        try pollFor(fd, event: Int16(POLLIN), signals: signals)
    }
    func pollWritable(_ fd: Int32, signals: SignalController? = nil) throws {
        try pollFor(fd, event: Int16(POLLOUT), signals: signals)
    }
    private func pollFor(_ fd: Int32, event: Int16, signals: SignalController?) throws {
        while true {
            try signals?.throwIfSignalled()
            let remaining = remainingMilliseconds
            guard remaining > 0 else { throw AssetError.invalid("startup deadline expired") }
            var item = pollfd(fd: fd, events: event, revents: 0)
            let result = poll(&item, 1, Int32(min(remaining, 100)))
            if result < 0 && errno == EINTR { continue }
            if result > 0 { return }
            if result == 0 { continue }
            throw AssetError.invalid("guest readiness socket poll failed: \(String(cString: strerror(errno)))")
        }
    }
}

final class SignalController {
    private let lock = NSLock()
    private var received = false
    private var sources: [DispatchSourceSignal] = []
    init() {
        for number in [SIGINT, SIGTERM] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .global(qos: .userInitiated))
            source.setEventHandler { [weak self] in self?.lock.lock(); self?.received = true; self?.lock.unlock() }
            source.resume(); sources.append(source)
        }
    }
    var isSignalled: Bool { lock.lock(); defer { lock.unlock() }; return received }
    func throwIfSignalled() throws { if isSignalled { throw AssetError.invalid("shutdown requested by SIGINT/SIGTERM") } }
    func wait() async {
        while !isSignalled { try? await Task.sleep(for: .milliseconds(100)) }
    }
}

enum VZConfigFactory {
    static let cpuCount = 2
    static let memorySize: UInt64 = 2 * 1024 * 1024 * 1024
    static func validateResourceBounds(cpuCount: Int, memorySize: UInt64) throws {
        guard cpuCount >= VZVirtualMachineConfiguration.minimumAllowedCPUCount,
              cpuCount <= VZVirtualMachineConfiguration.maximumAllowedCPUCount else { throw AssetError.invalid("CPU count is outside Virtualization.framework limits") }
        guard memorySize >= VZVirtualMachineConfiguration.minimumAllowedMemorySize,
              memorySize <= VZVirtualMachineConfiguration.maximumAllowedMemorySize,
              memorySize.isMultiple(of: 1024 * 1024) else { throw AssetError.invalid("memory size is outside Virtualization.framework limits or is not 1 MiB aligned") }
    }
    static func kernelCommandLine(diagnosticsEnabled: Bool) -> String {
        var commandLine = "console=hvc0 root=/dev/vda rw init=/sbin/ashnazg-init"
        if diagnosticsEnabled { commandLine += " ashnazg.diagnostics=1" }
        return commandLine
    }
    static func make(assets: ValidatedAssets, disk: URL, serialAttachment: VZSerialPortAttachment,
                     diagnosticsEnabled: Bool = false) throws -> VZVirtualMachineConfiguration {
        guard VZVirtualMachine.isSupported else { throw AssetError.invalid("Apple Virtualization.framework is unavailable on this host") }
        try validateResourceBounds(cpuCount: cpuCount, memorySize: memorySize)
        let configuration = VZVirtualMachineConfiguration()
        configuration.cpuCount = cpuCount; configuration.memorySize = memorySize
        let bootLoader = VZLinuxBootLoader(kernelURL: assets.kernel)
        bootLoader.commandLine = kernelCommandLine(diagnosticsEnabled: diagnosticsEnabled)
        configuration.bootLoader = bootLoader; configuration.platform = VZGenericPlatformConfiguration()
        let serialPort = VZVirtioConsoleDeviceSerialPortConfiguration(); serialPort.attachment = serialAttachment
        configuration.serialPorts = [serialPort]
        let diskAttachment = try VZDiskImageStorageDeviceAttachment(url: disk, readOnly: false)
        configuration.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: diskAttachment)]
        let network = VZVirtioNetworkDeviceConfiguration(); network.attachment = VZNATNetworkDeviceAttachment()
        configuration.networkDevices = [network]; configuration.socketDevices = [VZVirtioSocketDeviceConfiguration()]
        try configuration.validate()
        return configuration
    }
}

func parseReadyResponse(_ data: Data) throws -> String {
    guard data.count <= 4096, let line = String(data: data, encoding: .utf8), line.hasSuffix("\n"),
          let json = try JSONSerialization.jsonObject(with: Data(line.dropLast().utf8)) as? [String: Any],
          json["schema_version"] as? Int == 1, json["status"] as? String == "ready",
          let serial = json["android_serial"] as? String, serial == "127.0.0.1:5555" else { throw AssetError.invalid("guest returned an invalid readiness response") }
    return serial
}

func writeAll(_ fd: Int32, _ data: Data, deadline: StartupDeadline? = nil, signals: SignalController? = nil) throws {
    try data.withUnsafeBytes { raw in
        var offset = 0
        while offset < raw.count {
            try signals?.throwIfSignalled()
            if let deadline { try deadline.pollWritable(fd, signals: signals) }
            let amount = Darwin.write(fd, raw.baseAddress!.advanced(by: offset), raw.count - offset)
            if amount < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) { continue }
            guard amount > 0 else { throw AssetError.invalid("socket write failed: \(String(cString: strerror(errno)))") }
            offset += amount
        }
    }
}

func readLine(_ fd: Int32, maxBytes: Int = 4096, deadline: StartupDeadline, signals: SignalController? = nil) throws -> Data {
    var result = Data()
    while result.count < maxBytes {
        try deadline.pollReadable(fd, signals: signals)
        var byte: UInt8 = 0
        let amount = Darwin.read(fd, &byte, 1)
        if amount < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) { continue }
        guard amount == 1 else { throw AssetError.invalid("guest readiness connection closed before newline") }
        result.append(byte)
        if byte == 10 { return result }
    }
    throw AssetError.invalid("guest readiness response exceeded 4096 bytes")
}

func configureNoSIGPIPE(_ fd: Int32) {
    var enabled: Int32 = 1
    _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &enabled, socklen_t(MemoryLayout<Int32>.size))
}

final class RelayServer {
    private let vm: VZVirtualMachine
    private let port: Int
    private let guestPort: UInt32
    private let queue = DispatchQueue(label: "dev.ashnazg.adb-relay", qos: .userInitiated)
    private let activeLock = NSLock()
    private let listenerLock = NSLock()
    private let drained = DispatchSemaphore(value: 0)
    private var activeConnections = 0
    private var listener: Int32 = -1
    private var activeFDs = Set<Int32>()
    private var stopped = false
    private var shuttingDown = false
    init(vm: VZVirtualMachine, port: Int, guestPort: UInt32 = 15555) { self.vm = vm; self.port = port; self.guestPort = guestPort }
    func start() throws {
        listener = socket(AF_INET, SOCK_STREAM, 0)
        guard listener >= 0 else { throw AssetError.invalid("cannot create ADB listener socket") }
        configureNoSIGPIPE(listener)
        var address = sockaddr_in(); address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size); address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(port).bigEndian; address.sin_addr = in_addr(s_addr: in_addr_t(0x7f000001).bigEndian)
        let bound = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(listener, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }
        guard bound == 0, listen(listener, 16) == 0 else { close(); throw AssetError.invalid("cannot bind 127.0.0.1:\(port): \(String(cString: strerror(errno)))") }
        let listeningFD = listener; queue.async { self.acceptLoop(listeningFD) }
    }
    func close() {
        listenerLock.lock(); stopped = true; let fd = listener; listener = -1; listenerLock.unlock()
        if fd >= 0 { Darwin.shutdown(fd, SHUT_RDWR); Darwin.close(fd) }
        activeLock.lock(); shuttingDown = true
        for active in activeFDs { Darwin.shutdown(active, SHUT_RDWR) }
        let empty = activeConnections == 0
        activeLock.unlock()
        if empty { drained.signal() }
    }
    func waitForDrain() async -> Bool {
        await waitForDrainSignal(drained, seconds: 35)
    }
    private func isStopped() -> Bool { listenerLock.lock(); defer { listenerLock.unlock() }; return stopped }
    private func register(_ fd: Int32) -> Bool {
        activeLock.lock(); defer { activeLock.unlock() }
        guard !shuttingDown else { Darwin.shutdown(fd, SHUT_RDWR); return false }
        activeFDs.insert(fd); return true
    }
    private func admitClient(_ fd: Int32) -> Bool {
        activeLock.lock(); defer { activeLock.unlock() }
        guard !shuttingDown, activeConnections < 16 else { return false }
        activeFDs.insert(fd)
        activeConnections += 1
        return true
    }
    private func unregister(_ fd: Int32) { activeLock.lock(); activeFDs.remove(fd); activeLock.unlock() }
    private func connectionFinished(_ fd: Int32) {
        activeLock.lock(); activeFDs.remove(fd); activeConnections -= 1
        let empty = shuttingDown && activeConnections == 0; activeLock.unlock()
        if empty { drained.signal() }
    }
    private func acceptLoop(_ listeningFD: Int32) {
        while !isStopped() {
            let client = accept(listeningFD, nil, nil)
            if client < 0 { if errno == EINTR { continue }; break }
            configureNoSIGPIPE(client)
            guard admitClient(client) else { Darwin.close(client); continue }
            Task { await self.forward(client) }
        }
    }
    @MainActor private func forward(_ client: Int32) async {
        defer { connectionFinished(client); Darwin.shutdown(client, SHUT_RDWR); Darwin.close(client) }
        guard !isStopped() else { return }
        guard let socketDevice = vm.socketDevices.first as? VZVirtioSocketDevice else { return }
        do {
            let connection = try await connect(to: socketDevice, port: guestPort, deadline: StartupDeadline(after: .seconds(30)))
            defer { connection.close() }
            let guestFD = connection.fileDescriptor; configureNoSIGPIPE(guestFD)
            guard register(guestFD) else { connection.close(); return }
            defer { unregister(guestFD) }
            let a = DispatchGroup(), b = DispatchGroup(); a.enter(); b.enter()
            DispatchQueue.global().async { Self.copy(client, guestFD); a.leave() }
            DispatchQueue.global().async { Self.copy(guestFD, client); b.leave() }
            await withCheckedContinuation { continuation in
                DispatchQueue.global().async {
                    _ = a.wait(timeout: .distantFuture)
                    _ = b.wait(timeout: .distantFuture)
                    continuation.resume()
                }
            }
        } catch { FileHandle.standardError.write(Data("agent relay failed\n".utf8)) }
    }
    private static func copy(_ source: Int32, _ destination: Int32) {
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            let count = Darwin.read(source, &buffer, buffer.count)
            if count < 0 && errno == EINTR { continue }
            if count == 0 { _ = Darwin.shutdown(destination, SHUT_WR); return }
            guard count > 0 else { _ = Darwin.shutdown(destination, SHUT_WR); return }
            let sentAll = buffer.withUnsafeBytes { raw -> Bool in
                var offset = 0
                while offset < count {
                    let sent = Darwin.write(destination, raw.baseAddress!.advanced(by: offset), count - offset)
                    if sent < 0 && errno == EINTR { continue }
                    guard sent > 0 else { return false }; offset += sent
                }
                return true
            }
            if !sentAll { _ = Darwin.shutdown(destination, SHUT_WR); return }
        }
    }
    static func bridge(_ source: Int32, _ destination: Int32) {
        let a = DispatchGroup(), b = DispatchGroup(); a.enter(); b.enter()
        DispatchQueue.global().async { Self.copy(source, destination); a.leave() }
        DispatchQueue.global().async { Self.copy(destination, source); b.leave() }
        _ = a.wait(timeout: .distantFuture); _ = b.wait(timeout: .distantFuture)
    }
}

func waitForDrainSignal(_ signal: DispatchSemaphore, seconds: Double) async -> Bool {
    await withCheckedContinuation { continuation in
        DispatchQueue.global(qos: .userInitiated).async {
            continuation.resume(returning: signal.wait(timeout: .now() + seconds) == .success)
        }
    }
}

final class VMMonitor: NSObject, VZVirtualMachineDelegate {
    private let stoppedSignal: DispatchSemaphore
    private let lock = NSLock()
    private var recordedError: String?
    private var guestStopped = false
    init(stoppedSignal: DispatchSemaphore) { self.stoppedSignal = stoppedSignal }
    var error: String? { lock.lock(); defer { lock.unlock() }; return recordedError }
    var stoppedByGuest: Bool { lock.lock(); defer { lock.unlock() }; return guestStopped }
    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) { lock.lock(); recordedError = error.localizedDescription; lock.unlock(); stoppedSignal.signal() }
    func guestDidStop(_ virtualMachine: VZVirtualMachine) { lock.lock(); guestStopped = true; lock.unlock(); stoppedSignal.signal() }
}

@MainActor
func runHelper(_ options: HelperOptions) async throws {
    guard let assetsPath = options.assets else { throw AssetError.invalid("missing assets") }
    let assets = try ValidatedAssets.load(from: assetsPath)
    guard let statePath = options.stateDir else { throw AssetError.invalid("missing --state-dir DIR") }
    if options.validateOnly {
        let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("ashnazg-validate-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: temporary) }
        let disk = temporary.appendingPathComponent("rootfs.ext4"); try FileManager.default.copyItem(at: assets.rootfs, to: disk)
        let configuration = try VZConfigFactory.make(assets: assets, disk: disk, serialAttachment: VZFileHandleSerialPortAttachment(fileHandleForReading: nil, fileHandleForWriting: nil), diagnosticsEnabled: options.diagnosticsEnabled)
        try configuration.validate(); print("{\"status\":\"validated\",\"architecture\":\"aarch64\"}"); return
    }
    let start = DispatchTime.now().uptimeNanoseconds
    let deadline = StartupDeadline(after: .seconds(options.timeout))
    let signals = SignalController()
    let state = try OwnedState(statePath, assets: assets)
    if options.agentAPK != nil { try OwnedAgentCredential.requireAbsent(stateDirectory: state.directory) }
    var timings = StartupTimings(total_startup_ms: 0)
    var readySerial: String?
    var sessionID: String?
    let receipt = { (status: String, serial: String?, error: String?) -> RuntimeReceipt in
        timings.total_startup_ms = (DispatchTime.now().uptimeNanoseconds - start) / 1_000_000
        let optedIn = options.agentAPK != nil
        return RuntimeReceipt(status: status, adbListen: "127.0.0.1:\(options.adbPort)", assets: assets, serial: serial, timings: timings, error: error,
                              sessionID: status == "agent_ready" || status == "stopped" ? sessionID : nil,
                              agentListen: optedIn && (status == "agent_ready" || status == "stopped") ? "127.0.0.1:\(options.agentPort)" : nil,
                              installListen: optedIn && (status == "agent_ready" || status == "stopped") ? "127.0.0.1:\(options.installPort)" : nil,
                              agentAPKSHA256: optedIn ? options.agentSHA256 : nil)
    }
    var vm: VZVirtualMachine?
    var relays: [RelayServer] = []
    var credential: OwnedAgentCredential?
    var monitor: VMMonitor?
    var openedAPK: OpenedAgentAPK?
    var readHandle: FileHandle?
    var writeHandle: FileHandle?
    do {
        if let path = options.agentAPK, let hash = options.agentSHA256 { openedAPK = try OpenedAgentAPK(path: path, expectedSHA256: hash) }
        try state.writeReceipt(receipt("starting", nil, nil))
        let log = try SerialLog(url: state.serial)
        let pipeHandles = Pipe().fileHandlePair
        readHandle = pipeHandles.0; writeHandle = pipeHandles.1
        pipeHandles.0.readabilityHandler = { handle in let data = handle.availableData; if !data.isEmpty { log.append(data) } }
        let attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: nil, fileHandleForWriting: pipeHandles.1)
        try signals.throwIfSignalled()
        let configuration = try VZConfigFactory.make(assets: assets, disk: state.disk, serialAttachment: attachment, diagnosticsEnabled: options.diagnosticsEnabled)
        vm = VZVirtualMachine(configuration: configuration)
        let machine = vm!
        let stopped = DispatchSemaphore(value: 0); let activeMonitor = VMMonitor(stoppedSignal: stopped); monitor = activeMonitor; machine.delegate = activeMonitor
        let vmStartStart = DispatchTime.now().uptimeNanoseconds
        try await startVM(machine, deadline: deadline, signals: signals)
        timings.vm_start_ms = (DispatchTime.now().uptimeNanoseconds - vmStartStart) / 1_000_000
        guard let socket = machine.socketDevices.first as? VZVirtioSocketDevice else { throw AssetError.invalid("VM has no virtio socket device") }
        let readyStart = DispatchTime.now().uptimeNanoseconds
        var androidSerial: String?
        var lastError = "guest readiness timeout"
        while androidSerial == nil {
            try signals.throwIfSignalled()
            guard !deadline.isExpired else { throw AssetError.invalid("guest did not become ready within \(options.timeout)s: \(lastError)") }
            if machine.state == .error || machine.state == .stopped { throw AssetError.invalid(activeMonitor.error ?? "virtual machine stopped before guest readiness") }
            do {
                let connection = try await connect(to: socket, deadline: deadline, signals: signals)
                defer { connection.close() }
                let fd = connection.fileDescriptor; configureNoSIGPIPE(fd)
                try writeAll(fd, Data("{\"method\":\"status\"}\n".utf8), deadline: deadline, signals: signals)
                androidSerial = try parseReadyResponse(readLine(fd, deadline: deadline, signals: signals))
            } catch {
                try signals.throwIfSignalled()
                guard !deadline.isExpired else { throw AssetError.invalid("guest did not become ready within \(options.timeout)s: \(error.localizedDescription)") }
                lastError = error.localizedDescription
                try await Task.sleep(for: .milliseconds(min(250, deadline.remainingMilliseconds)))
            }
        }
        timings.readiness_ms = (DispatchTime.now().uptimeNanoseconds - readyStart) / 1_000_000
        guard let serial = androidSerial else { throw AssetError.invalid("guest readiness ended without a serial") }
        readySerial = serial
        let server = RelayServer(vm: machine, port: options.adbPort); try server.start(); relays.append(server)
        var finalStatus = "ready"
        if options.agentAPK != nil {
            let agentStart = DispatchTime.now().uptimeNanoseconds
            guard let apk = openedAPK else { throw AssetError.invalid("agent APK was not safely opened") }
            let identity = try makeAgentSessionIdentity(); sessionID = identity.sessionID
            let preparationBudget = min(180_000, deadline.remainingMilliseconds)
            guard preparationBudget > 0 else { throw AssetError.invalid("agent preparation deadline expired") }
            let preparationDeadline = StartupDeadline(after: .milliseconds(preparationBudget))
            let control = try await connect(to: socket, port: 10777, deadline: preparationDeadline, signals: signals)
            do { configureNoSIGPIPE(control.fileDescriptor); try await prepareAgentSession(fd: control.fileDescriptor, apk: apk, identity: identity, wholeDeadline: preparationDeadline, signals: signals) }
            catch { control.close(); throw error }
            control.close()
            let agentRelay = RelayServer(vm: machine, port: options.agentPort, guestPort: 17777)
            try agentRelay.start(); relays.append(agentRelay)
            let installRelay = RelayServer(vm: machine, port: options.installPort, guestPort: 17778)
            try installRelay.start(); relays.append(installRelay)
            let credentialValue = AgentSessionCredentials(session_id: identity.sessionID, helper_pid: getpid(),
                                                          manifest_source_sha: assets.manifest.source_sha,
                                                          agent_apk_sha256: apk.sha256, rpc_token: identity.rpcToken,
                                                          install_token: identity.installToken,
                                                          agent_listen: "127.0.0.1:\(options.agentPort)",
                                                          install_listen: "127.0.0.1:\(options.installPort)")
            credential = try OwnedAgentCredential(stateDirectory: state.directory, credentials: credentialValue)
            timings.agent_startup_ms = (DispatchTime.now().uptimeNanoseconds - agentStart) / 1_000_000
            finalStatus = "agent_ready"
        }
        try state.writeReceipt(receipt(finalStatus, serial, nil))
        print(try JSONEncoder.sorted.encode(receipt(finalStatus, serial, nil)).asUTF8()); fflush(stdout)
        while !signals.isSignalled {
            if let failure = activeMonitor.error { throw AssetError.invalid("virtual machine stopped with an error: \(failure)") }
            if activeMonitor.stoppedByGuest { throw AssetError.invalid("guest shut down while ash nazg was supervising it") }
            try await Task.sleep(for: .milliseconds(100))
        }
        for relay in relays { relay.close() }
        for relay in relays { guard await relay.waitForDrain() else { throw AssetError.invalid("runtime relay did not drain during shutdown") } }
        relays.removeAll()
        try await shutdownGuest(machine, monitor: activeMonitor)
        try credential?.removeOwned(); credential = nil
        openedAPK?.close(); openedAPK = nil
        try state.writeReceipt(receipt("stopped", serial, nil))
        readHandle?.readabilityHandler = nil; try? writeHandle?.close(); try? readHandle?.close(); state.close()
    } catch {
        for relay in relays { relay.close() }
        for relay in relays { _ = await relay.waitForDrain() }
        relays.removeAll()
        if let machine = vm, let activeMonitor = monitor, machine.canStop, activeMonitor.error == nil, !activeMonitor.stoppedByGuest {
            try? await shutdownGuest(machine, monitor: activeMonitor)
        }
        try? credential?.removeOwned(); credential = nil
        openedAPK?.close(); openedAPK = nil
        readHandle?.readabilityHandler = nil; try? writeHandle?.close(); try? readHandle?.close()
        if let machine = vm, machine.canStop { try? await stopVM(machine) }
        try? state.writeReceipt(receipt("failed", readySerial, error.localizedDescription))
        state.close()
        throw error
    }
}

func parseShutdownResponse(_ data: Data) throws {
    guard data.count <= 4096,
          let json = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          json["schema_version"] as? Int == 1, json["status"] as? String == "stopping" else {
        throw AssetError.invalid("guest did not acknowledge orderly shutdown")
    }
}

@MainActor
private func shutdownGuest(_ machine: VZVirtualMachine, monitor: VMMonitor) async throws {
    let deadline = StartupDeadline(after: .seconds(45))
    guard let socket = machine.socketDevices.first as? VZVirtioSocketDevice else {
        throw AssetError.invalid("guest shutdown socket is unavailable")
    }
    let connection = try await connect(to: socket, deadline: deadline)
    defer { connection.close() }
    let fd = connection.fileDescriptor
    configureNoSIGPIPE(fd)
    try writeAll(fd, Data("{\"method\":\"shutdown\"}\n".utf8), deadline: deadline)
    try parseShutdownResponse(readLine(fd, deadline: deadline))
    while !deadline.isExpired {
        if let failure = monitor.error { throw AssetError.invalid("guest shutdown failed: \(failure)") }
        if monitor.stoppedByGuest && machine.state == .stopped { return }
        try await Task.sleep(for: .milliseconds(100))
    }
    throw AssetError.invalid("guest did not finish cleanup and power off within 45 seconds; forcing VM stop")
}

@MainActor
private func stopVM(_ machine: VZVirtualMachine) async throws {
    try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
        let gate = CompletionGate<Void>(continuation)
        machine.stop { error in
            if let error { gate.finish(.failure(error)) }
            else { gate.finish(.success(())) }
        }
        gate.monitor(deadline: StartupDeadline(after: .seconds(10)))
    }
}

@MainActor
private func startVM(_ vm: VZVirtualMachine, deadline: StartupDeadline, signals: SignalController) async throws {
    guard !deadline.isExpired else { throw AssetError.invalid("startup deadline expired before VM start") }
    try signals.throwIfSignalled()
    try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
        let gate = CompletionGate<Void>(continuation, onLateSuccess: {
            Task { @MainActor in if vm.canStop { try? await vm.stop() } }
        })
        vm.start { result in gate.finish(result) }
        gate.monitor(deadline: deadline, signals: signals)
    }
}

@MainActor
private func connect(to socket: VZVirtioSocketDevice, port: UInt32 = 10777, deadline: StartupDeadline, signals: SignalController? = nil) async throws -> VZVirtioSocketConnection {
    guard !deadline.isExpired else { throw AssetError.invalid("startup deadline expired before guest socket connection") }
    try signals?.throwIfSignalled()
    return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<VZVirtioSocketConnection, Error>) in
        let gate = CompletionGate<VZVirtioSocketConnection>(continuation)
        socket.connect(toPort: port) { result in gate.finish(result) }
        gate.monitor(deadline: deadline, signals: signals)
    }
}

private final class CompletionGate<Value> {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, Error>?
    private var finished = false
    private let onLateSuccess: (() -> Void)?
    init(_ continuation: CheckedContinuation<Value, Error>, onLateSuccess: (() -> Void)? = nil) {
        self.continuation = continuation; self.onLateSuccess = onLateSuccess
    }
    func finish(_ result: Result<Value, Error>) {
        lock.lock(); guard !finished else {
            lock.unlock()
            if case .success(let value) = result {
                if let socket = value as? VZVirtioSocketConnection { socket.close() }
                onLateSuccess?()
            }
            return
        }
        finished = true; let continuation = self.continuation; self.continuation = nil; lock.unlock(); continuation?.resume(with: result)
    }
    func monitor(deadline: StartupDeadline, signals: SignalController? = nil) {
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + .milliseconds(50)) { [weak self] in
            guard let self else { return }
            if signals?.isSignalled == true { self.finish(.failure(AssetError.invalid("shutdown requested by SIGINT/SIGTERM"))); return }
            if deadline.isExpired { self.finish(.failure(AssetError.invalid("startup deadline expired"))); return }
            self.lock.lock(); let done = self.finished; self.lock.unlock()
            if !done { self.monitor(deadline: deadline, signals: signals) }
        }
    }
}

extension Pipe { var fileHandlePair: (FileHandle, FileHandle) { (fileHandleForReading, fileHandleForWriting) } }
extension Data { func asUTF8() -> String { String(decoding: self, as: UTF8.self) } }
