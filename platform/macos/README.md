# macOS VM helper

Build and run asset/configuration checks on Apple Silicon macOS:

```sh
swift test --package-path platform/macos
swift build -c release --package-path platform/macos
codesign --force --sign - --entitlements platform/macos/entitlements.plist platform/macos/.build/release/ashnazg-vm
```

The ad-hoc signature enables local entitlement testing only. Distribution signing, Developer ID and notarization have not been qualified. Local Android fixture integration, comparative benchmarking, restart and graceful VM shutdown are recorded in the [backend guide](../../docs/microvm.md). Default bundle acquisition and agent/MCP setup remain pending. Pass a real artifact directory with `manifest.json`, `kernel`, and `rootfs.ext4`; Task 1 does not fetch assets.
