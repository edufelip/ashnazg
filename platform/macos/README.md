# macOS VM helper

Build and run asset/configuration checks on Apple Silicon macOS:

```sh
swift test --package-path platform/macos
swift build -c release --package-path platform/macos
codesign --force --sign - --entitlements platform/macos/entitlements.plist platform/macos/.build/release/ashnazg-vm
```

The ad-hoc signature enables the virtualization entitlement; Developer ID signing and notarization have not been qualified. The helper is included in the [public experimental runtime](../../docs/releases.md), whose packaged MCP host handles acquisition and session startup. Direct helper invocation still requires a real artifact directory with `manifest.json`, `kernel`, and `rootfs.ext4`; the helper itself does not fetch assets. Source-specific integration and benchmark history is recorded in the [backend guide](../../docs/microvm.md).

An explicit development APK can opt the helper into an authenticated native-agent session:

```sh
ashnazg-vm --assets DIR --state-dir DIR \
  --agent-apk /absolute/path/ashnazg-agent.apk \
  --agent-sha256 LOWERCASE_SHA256
```

Both agent flags are required together. Optional `--agent-port` and `--install-port` select the host loopback listeners (defaults `17777` and `17778`); they must differ from one another and from `--adb-port`. Without an APK, the existing VM-only ready behavior remains in effect. With an APK, the helper verifies the opened file, prepares it through the fixed guest control protocol, then publishes a private `.ashnazg-session-v1.json` credential and an `agent_ready` receipt after both relays bind. The receipt contains session/endpoints and artifact identity, never tokens. The private credential protects against other users through its owner-only file and directory permissions; another process running as the same user can read the helper's memory and files, so it is outside that confidentiality boundary. Use the packaged [native MCP CLI](../../docs/native-cli.md) for automatic release acquisition and session supervision.

The explicit helper session passed matching cold/restart guest acceptance, recorded in the [backend guide](../../docs/microvm.md). Use a guest built with the session service; the historical VM-only artifact lacks the preparation protocol. The packaged host's [public acquisition acceptance](../../docs/evidence/public-runtime-0.3.0/README.md) now covers default acquisition and automatic MCP setup separately.
