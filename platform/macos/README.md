# macOS VM helper

Build and run asset/configuration checks on Apple Silicon macOS:

```sh
swift test --package-path platform/macos
swift build -c release --package-path platform/macos
codesign --force --sign - --entitlements platform/macos/entitlements.plist platform/macos/.build/release/ashnazg-vm
```

The ad-hoc signature enables local entitlement testing only. Distribution signing, Developer ID, notarization and a VM boot have not been qualified. Pass a real artifact directory with `manifest.json`, `kernel`, and `rootfs.ext4`; Task 1 does not fetch assets.
