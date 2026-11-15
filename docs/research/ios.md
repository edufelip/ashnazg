# iOS research boundary and experiment plan

Status: research only, reviewed against primary sources on 2026-11-15. No Ashnazg iOS backend, iOS runtime image, Linux iOS compatibility layer, or iOS acceptance run is delivered by this document.

## Recommended supported-platform track

Prototype a macOS-only backend using Xcode tooling and XCTest/XCUIAutomation, retaining the existing MCP tool concepts but defining platform-specific admission and error semantics explicitly. Apple documents `simctl` as an Xcode tool for Simulator control and `XCUIApplication` as a UI-test proxy for launching, monitoring, terminating and querying an application. These APIs establish a credible experiment surface; they do not prove Ashnazg has implemented a persistent all-app accessibility agent. [Xcode tool reference](https://developer.apple.com/documentation/xcode/xcode-command-line-tool-reference), [XCUIApplication](https://developer.apple.com/documentation/xcuiautomation/xcuiapplication).

A first experiment must use an app built for the iOS Simulator target and a dedicated simulator. It must record Xcode/macOS/runtime versions, exact fixture revision and simulator identifier. Launch/state waits must use observed app state with bounded deadlines. Do not infer iOS success from an Android build of shared React Native/Flutter code.

Proposed acceptance contract:

| Surface | Experiment required before claiming support |
| --- | --- |
| Snapshot/selectors | Query fixture elements through XCTest; verify stable IDs, text/state/bounds, stale references, ambiguous selectors and secure-field redaction. |
| Actions | Prove tap, long press, Unicode input/clear, scroll, Back-equivalent behavior and deep links individually; return explicit unsupported errors for actions without a platform mapping. |
| Screenshots/overlays | Capture actual Simulator/fixture images, correlate marks with the same element snapshot, test scale/orientation and secure content. No zero-copy claim without a measured implementation. |
| Logs/install/lifecycle | Verify package/runtime selection, bounded filters, ownership and cleanup; document signing/build-target requirements using real receipts. |
| Transport | Negotiate MCP against the same native host, authenticate any network endpoint and serialize mutations. Never replay ambiguous actions automatically. |
| Performance | Retain raw setup/snapshot/screenshot/action measurements and errors separately. A warm Simulator operation is not a cold runtime boot measurement. |

The prototype is complete only after these experiments have exact-source receipts and screenshots. This research phase performs no simulator operations and introduces no placeholder tools advertised as available.

## Separate Mach-O-on-Linux track

Darling describes itself as a Darwin/macOS environment for Linux with a Mach-O loader, userspace kernel server and framework reimplementations; its README describes GUI support as active development. That is evidence of prior art, not evidence of modern iOS/UIKit/SwiftUI compatibility. [Darling upstream](https://github.com/darlinghq/darling).

Shared ARM64 instruction-set support is insufficient to establish platform ABI, executable loading, dyld dependencies, Mach IPC, application services, graphics or framework behavior. The original blueprint's labels of solved CPU/loader or working iOS syscall support overstate the evidence available to this project. Treat every layer as an independent experiment.

Bounded research sequence:

1. Build a project-owned minimal ARM64 Mach-O command-line fixture with a pinned toolchain; inventory its load commands, platform target, dependencies and invoked services.
2. Demonstrate loading and one explicit syscall/ABI path on Linux with a captured source/build/runtime receipt; unsupported operations must fail visibly.
3. Independently validate Objective-C runtime and required Foundation behavior before any UI claim. A CLI hello-world cannot stand in for an iOS app.
4. Explore a project-owned graphics fixture only after those prerequisites. Identify the precise supported API subset and test results; do not infer Metal/UIKit support from another project's roadmap.
5. Reassess feasibility before committing to broad compatibility. No proprietary Apple runtime extraction or redistribution is part of this research delivery.

No Linux compatibility claim is established until each tested layer has reproducible evidence. The macOS Simulator track and Linux compatibility track remain separate deliverables with separate receipts.
