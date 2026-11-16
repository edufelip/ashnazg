# macOS VM helper

Build and run asset/configuration checks on Apple Silicon macOS:

```sh
swift test --package-path platform/macos
swift build -c release --package-path platform/macos
codesign --force --sign - --entitlements platform/macos/entitlements.plist platform/macos/.build/release/ashnazg-vm
```

The ad-hoc signature enables local entitlement testing only. Distribution signing, Developer ID and notarization have not been qualified. Local Android fixture integration, comparative benchmarking, restart and graceful VM shutdown are recorded in the [backend guide](../../docs/microvm.md). Default bundle acquisition and release-resolver integration remain pending. Pass a real artifact directory with `manifest.json`, `kernel`, and `rootfs.ext4`; the helper does not fetch assets.

An explicit development APK can opt the helper into an authenticated native-agent session:

```sh
ashnazg-vm --assets DIR --state-dir DIR \
  --agent-apk /absolute/path/ashnazg-agent.apk \
  --agent-sha256 LOWERCASE_SHA256
```

Both agent flags are required together. Optional `--agent-port` and `--install-port` select the host loopback listeners (defaults `17777` and `17778`); they must differ from one another and from `--adb-port`. Without an APK, the existing VM-only ready behavior remains in effect. With an APK, the helper verifies the opened file, prepares it through the fixed guest control protocol, then publishes a private `.ashnazg-session-v1.json` credential and an `agent_ready` receipt after both relays bind. The receipt contains session/endpoints and artifact identity, never tokens. The private credential protects against other users through its owner-only file and directory permissions; another process running as the same user can read the helper's memory and files, so it is outside that confidentiality boundary. This explicit helper path does not yet provide zero-setup release acquisition or Rust CLI integration.

The explicit helper session passed matching cold/restart guest acceptance, recorded in the [backend guide](../../docs/microvm.md). Use a guest built with the session service; the historical VM-only artifact lacks the preparation protocol. Default acquisition and automatic MCP setup remain separate work; the [acquisition primitives](../../docs/runtime-acquisition.md) do not yet provide that CLI flow.
