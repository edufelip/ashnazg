// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "AshnazgVM",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "ashnazg-vm", targets: ["AshnazgVM"])],
    targets: [
        .executableTarget(name: "AshnazgVM", path: "Sources/AshnazgVM"),
        .testTarget(name: "AshnazgVMTests", dependencies: ["AshnazgVM"], path: "Tests/AshnazgVMTests"),
    ],
    swiftLanguageModes: [.v5]
)
