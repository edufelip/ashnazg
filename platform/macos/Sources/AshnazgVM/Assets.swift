import CryptoKit
import Foundation

struct AssetManifest: Decodable {
    struct File: Decodable {
        let sha256: String
        let size: UInt64
    }
    struct Guest: Decodable {
        let kernel_version: String
        let base: String
        let redroid_index: String
    }
    let schema_version: Int
    let version: String
    let source_sha: String
    let architecture: String
    let files: [String: File]
    let guest: Guest
}

enum AssetError: Error, LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        switch self { case .invalid(let message): return message }
    }
}

struct ValidatedAssets {
    let directory: URL
    let manifest: AssetManifest
    let kernel: URL
    let rootfs: URL
    let kernelSHA256: String
    let rootfsSHA256: String

    static func load(from directory: URL) throws -> ValidatedAssets {
        var isDirectory: ObjCBool = false
        guard directory.standardizedFileURL.path == directory.resolvingSymlinksInPath().path,
              lstatKind(directory.path) == S_IFDIR else {
            throw AssetError.invalid("asset directory is missing or is a symlink: \(directory.path)")
        }
        guard FileManager.default.fileExists(atPath: directory.path, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw AssetError.invalid("asset path is not a directory: \(directory.path)")
        }
        let manifestURL = directory.appendingPathComponent("manifest.json")
        try requireRegularFile(manifestURL)
        let manifest: AssetManifest
        do { manifest = try JSONDecoder().decode(AssetManifest.self, from: Data(contentsOf: manifestURL)) }
        catch { throw AssetError.invalid("cannot parse manifest.json: \(error.localizedDescription)") }
        guard manifest.schema_version == 1 else { throw AssetError.invalid("unsupported manifest schema_version; expected 1") }
        guard manifest.version == "0.3.1",
              manifest.guest.kernel_version == "6.12.117",
              manifest.guest.base == "alpine-3.22.8-aarch64",
              manifest.guest.redroid_index == "sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee" else {
            throw AssetError.invalid("manifest guest metadata does not match the pinned v0.3 Android 15 artifact contract")
        }
        guard manifest.architecture == "aarch64", manifest.source_sha.range(of: "^[0-9a-f]{40}$", options: .regularExpression) != nil else {
            throw AssetError.invalid("manifest architecture/source_sha is invalid; expected aarch64 and a 40-character lowercase commit SHA")
        }
        guard Set(manifest.files.keys) == Set(["kernel", "rootfs.ext4"]) else {
            throw AssetError.invalid("manifest files must contain exactly kernel and rootfs.ext4")
        }
        let kernel = directory.appendingPathComponent("kernel")
        let rootfs = directory.appendingPathComponent("rootfs.ext4")
        try verify(kernel, file: manifest.files["kernel"]!)
        try verify(rootfs, file: manifest.files["rootfs.ext4"]!)
        return ValidatedAssets(directory: directory, manifest: manifest, kernel: kernel, rootfs: rootfs,
                               kernelSHA256: manifest.files["kernel"]!.sha256,
                               rootfsSHA256: manifest.files["rootfs.ext4"]!.sha256)
    }

    private static func verify(_ url: URL, file: AssetManifest.File) throws {
        try requireRegularFile(url)
        let values = try url.resourceValues(forKeys: [.fileSizeKey])
        guard UInt64(values.fileSize ?? -1) == file.size else { throw AssetError.invalid("asset size mismatch: \(url.lastPathComponent)") }
        guard file.sha256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
            throw AssetError.invalid("manifest SHA-256 is invalid for \(url.lastPathComponent)")
        }
        let digest = SHA256.hash(data: try Data(contentsOf: url, options: .mappedIfSafe)).map { String(format: "%02x", $0) }.joined()
        guard digest == file.sha256 else { throw AssetError.invalid("asset SHA-256 mismatch: \(url.lastPathComponent)") }
    }
}

func lstatKind(_ path: String) -> mode_t? {
    var info = stat()
    guard lstat(path, &info) == 0 else { return nil }
    return info.st_mode & S_IFMT
}

func requireRegularFile(_ url: URL) throws {
    guard lstatKind(url.path) == S_IFREG else {
        throw AssetError.invalid("required file is missing, not regular, or is a symlink: \(url.path)")
    }
}
