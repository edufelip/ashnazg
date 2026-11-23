# Releases

## v0.3.1 experimental native runtime

The `v0.3.1` prerelease builds on [v0.3.0](https://github.com/edufelip/ashnazg/releases/tag/v0.3.0) (whose released macOS host passed [public cold acquisition and a network-denied warm restart](evidence/public-runtime-0.3.0/README.md)) by bundling the XcodeGen iOS Simulator fixture sources (`platform/ios/`) into macOS release ZIPs and updating executable-relative fixture discovery. The candidate workflow builds the host package and native runtime from one exact main commit and publishes the accepted bytes unchanged.

The candidate contains Linux x86_64 and macOS ARM64 host ZIPs, a Linux ARM64 guest archive, the ad-hoc signed macOS ARM64 `ashnazg-vm` helper, agent/fixture APKs, a source-and-notice sidecar, the schema-1 runtime catalog, `candidate.json`, and download `SHA256SUMS`. The macOS ZIP also includes `bin/ashnazg-vm` and the XcodeGen iOS Simulator fixture sources under `platform/ios/` so `bin/ashnazg mcp --ios-fixture` and `bin/ashnazg mcp --ios-app` work from an extracted release archive without setting `ASHNAZG_IOS_FIXTURE_SOURCE` (provided `xcodebuild`, `xcrun simctl`, and `xcodegen` are installed). Both ZIPs include original host-dependency notice texts in `THIRD-PARTY-NOTICES.tar.xz` alongside the project MIT license. Catalog artifacts and the host build stamp identify the same full source SHA; generated catalogs are release outputs rather than committed source inputs.

On Apple Silicon macOS, the released `bin/ashnazg mcp` selects its owned VM when `ASHNAZG_TOKEN` is absent. It downloads missing catalog-bound artifacts to a private cache, starts the helper, installs the agent through guest ADB, and serves MCP. Host Android SDK tools are not required for this path. An explicitly supplied `ASHNAZG_TOKEN` retains the external Android setup below. Linux packages continue to use an explicitly configured external Android device.

The helper carries the `com.apple.security.virtualization` entitlement and an ad-hoc signature. Host/helper binaries are not Developer ID signed or notarized; ordinary macOS security approval may be required. Debug-signed Android instrumentation and the experimental guest are development tools. SHA-256 verification supplies download integrity, not an independent publisher signature or intrinsic binary-to-source proof.

The source sidecar retains upstream package sources, pinned packaging recipes/support files, license/notice material, project sources and kernel configuration/source, with a manifest binding the installed-package inventory to the prepared guest. ReDroid is downloaded at first boot and its OCI image is excluded from the distributed guest/source assets. Declared package license values and retained source evidence do not constitute a legal-clearance claim.

### Native macOS ARM64 setup

Download and verify the host ZIP in a new directory:

```sh
curl -fLO https://github.com/edufelip/ashnazg/releases/download/v0.3.1/ashnazg-v0.3.1-macos-aarch64.zip
curl -fLO https://github.com/edufelip/ashnazg/releases/download/v0.3.1/SHA256SUMS
awk '$2 == "ashnazg-v0.3.1-macos-aarch64.zip" {print}' SHA256SUMS | shasum -a 256 -c -
mkdir ashnazg-v0.3.1
unzip ashnazg-v0.3.1-macos-aarch64.zip -d ashnazg-v0.3.1
```

Configure your MCP client with the extracted executable's absolute path:

```json
{
  "mcpServers": {
    "ashnazg": {
      "command": "/absolute/path/ashnazg-v0.3.1/bin/ashnazg",
      "args": ["mcp"]
    }
  }
}
```

Leave `ASHNAZG_TOKEN` absent to select the owned VM. First use downloads the matching runtime and pulls its pinned Android image; later sessions reuse the verified cache and persistent disk. Host ADB, aapt, and Android SDK setup are not required. MCP initialization completes before the VM is ready; a still-starting tool response can be retried. Stdio carries protocol messages, not an interactive prompt. Close an existing native session before starting another on the default ports. Ordinary macOS security approval may be required for the ad-hoc signed, non-notarized binaries.

### Prepare, qualify, publish

1. Merge the reviewed packaging change and require the exact main commit's `Quality` workflow to pass.
2. Dispatch `Release` on main with `operation=prepare`. This builds a single matching set, verifies it, and retains `release-prepared-v0.3.1-<source-sha>` for 30 days. Preparation cannot publish.
3. Download that candidate, verify `SHA256SUMS` and run `scripts/runtime_release.py verify`. Run the real CLI/VM acceptance against its unchanged bytes. Retain `docs/evidence/runtime-release-0.3.1/receipt.json` and supporting evidence on main, including source SHA, prepare run ID, candidate hash and passed EOF, idle SIGTERM and in-flight SIGTERM modes.
4. Create `v0.3.1` at the candidate's exact source commit. Tag pushes do not rebuild or publish.
5. Dispatch `Release` on main with `operation=publish`, the successful `prepared_run_id`, and `acceptance_commit`. The workflow checks the source/main/Quality/tag binding, verifies the retained acceptance-to-candidate hash and republishes those exact prepared bytes as an experimental prerelease. It refuses an existing release rather than replacing accepted assets.
6. Verify the public catalog/assets, then run production cold acquisition in a fresh owned home and warm offline reuse separately. Publication, CLI/VM acceptance and clean-machine behavior require their own receipts.


## v0.1.0 package contents

The v0.1.0 release workflow produces two deterministic ZIP archives, one built on the current Linux runner and one on the current macOS runner. Each archive's filename and `manifest.json` state the runner's actual OS and CPU architecture; the build does not cross-compile or claim support for other host architectures.

Each ZIP contains:

- `bin/ashnazg`: the Rust host executable built in release mode for the named platform.
- `android/agent-instrumentation.apk` and `android/fixture-instrumentation.apk`: the agent instrumentation package and its Android fixture app, built from this commit. These are not unrelated Compose or React Native sample APKs.
- `LICENSE` and `README-SETUP.md` with installation guidance.
- `manifest.json` with version, exact source commit, actual runner platform, file sizes, and per-file SHA-256 values.
- `SHA256SUMS` with SHA-256 values for the packaged payload and manifest.

The APKs use a generated Android debug signing key; they are not production releases. The host executables are not Developer ID signed or notarized. No publisher signing identity or cross-architecture compatibility is claimed. They require Android 14 (API 34) or later and are intended for the Ashnazg agent/test setup.

The historical 0.1.0 package does not carry a matching native runtime distribution. Use the 0.3.1 candidate/release process above for the native path.

## Installing

1. Download the ZIP matching the host operating system and architecture shown in its filename and manifest, then extract it.
2. Verify the checksums from the extracted directory with `sha256sum -c SHA256SUMS` on Linux or `shasum -a 256 -c SHA256SUMS` on macOS.
3. Run `bin/ashnazg --help` on the matching host.
4. Select one Android 14 or later device explicitly with `ANDROID_SERIAL`, then install the fixture and agent APKs. A previous development build may use a different debug certificate; Android rejects an incompatible update. Use a fresh test device or remove only the old Ashnazg test packages before reinstalling.

```sh
export ANDROID_SERIAL=emulator-5554  # Replace with your test device from adb devices.
adb -s "$ANDROID_SERIAL" install -r android/fixture-instrumentation.apk
adb -s "$ANDROID_SERIAL" install -r android/agent-instrumentation.apk
```

5. Stop other tools using the selected device's UiAutomation connection. In a dedicated terminal, generate a fresh token and forward the guest socket without replacing an existing forward. Python 3 is used only to generate the token in this example.

```sh
export ASHNAZG_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
adb -s "$ANDROID_SERIAL" forward --no-rebind tcp:7777 tcp:7777
adb -s "$ANDROID_SERIAL" shell am instrument -w -e token "$ASHNAZG_TOKEN" \
  dev.ashnazg.agent/dev.ashnazg.agent.AgentInstrumentation
```

The instrumentation command stays running. Configure your MCP client to launch the absolute extracted `bin/ashnazg mcp` path with the same `ASHNAZG_TOKEN` and `ANDROID_SERIAL` environment values. Stdio carries MCP protocol messages, so an interactive shell is not an MCP client. The host connects to loopback port 7777; keep ADB forwards and tokens private. When finished, stop the instrumentation terminal and remove the forward you created with `adb -s "$ANDROID_SERIAL" forward --remove tcp:7777`.

For HTTP transport, tool schemas and limitations, see the [source API guide](https://github.com/edufelip/ashnazg/blob/main/docs/api.md). Package provenance is recorded in `manifest.json`; use its `source_sha` to inspect the exact source revision. On macOS, locally built unsigned binaries may require the normal system security approval before launch.

## Historical v0.1.0 release

The earlier v0.1.0 release shipped only host ZIPs and APKs with external-device setup. It did not provide the matching native runtime catalog, helper or guest. The version 0.3.1 workflow replaces that publication path; existing v0.1.0 release assets remain unchanged.
