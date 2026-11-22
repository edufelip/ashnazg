# Live iOS Simulator fixture MCP acceptance

On an Apple Silicon MacBook Air, Xcode 26.6 and the iOS 26.5 Simulator ran the
source-built `ashnazg mcp --ios-fixture` stdio session against the project-owned
SwiftUI fixture. The MCP client discovered the three UI tools, selected
`fixture-increment` from the initial accessibility snapshot, and sent one tap.
The post-tap snapshot reported `Count: 1` with sequence 3. The next PNG reported
the same count with sequence 4 and differed from the initial image. The test
also checked unsupported typing and the absent Android install tool.

The [combined-revision receipt](ecc2fda-receipt.json) records the exact
`ecc2fdafad97297cd8b9f8ae4a80ed3a8e238465` build, its 63.172-second run, PNG
dimensions and hashes, and EOF shutdown with exit code zero. The
owned iPhone 17 Pro Simulator and work directory were verified absent. An earlier [adapter-only revision](b07b275-receipt.json) also passed. An additional
attempt at that earlier source failed during the Simulator's installation stage
before any MCP tool call. Its original diagnostics were retained on the Mac.

The [fixture guide](../../../platform/ios/README.md) documents the command and
the available operations for this source experiment.
