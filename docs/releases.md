# Releases

## v0.1.0 package contents

The v0.1.0 release workflow produces two deterministic ZIP archives, one built on the current Linux runner and one on the current macOS runner. Each archive's filename and `manifest.json` state the runner's actual OS and CPU architecture; the build does not cross-compile or claim support for other host architectures.

Each ZIP contains:

- `bin/ashnazg`: the Rust host executable built in release mode for the named platform.
- `android/agent-instrumentation.apk` and `android/fixture-instrumentation.apk`: the agent instrumentation package and its Android fixture app, built from this commit. These are not unrelated Compose or React Native sample APKs.
- `LICENSE` and `README-SETUP.md` with installation guidance.
- `manifest.json` with version, exact source commit, actual runner platform, file sizes, and per-file SHA-256 values.
- `SHA256SUMS` with SHA-256 values for the packaged payload and manifest.

The APKs use a generated Android debug signing key; they are not production releases. The host executables are not Developer ID signed or notarized. No publisher signing identity or cross-architecture compatibility is claimed. They require Android 14 (API 34) or later and are intended for the Ashnazg agent/test setup.

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

## Creating a release

Pull requests affecting packaging build both host archives as downloadable CI artifacts, with publication disabled. The `Release` workflow publishes on a `v*` tag push or manual dispatch on `main` with an existing tag name. It accepts only `v0.1.0`, verifies the tag points to the exact workflow commit, confirms that commit is an ancestor of `main`, and requires the latest matching `Quality` workflow run to have completed successfully from a push to `main` for the same full commit SHA. It builds both native packages and the required Android APKs, then publishes only after every package job succeeds. Manual dispatch does not move or create tags.

The package assembly script takes explicit paths for every input, rejects missing/non-regular inputs and invalid provenance, uses normalized archive paths and fixed ZIP timestamps, and emits checksums. The release workflow must not be used to present the debug APKs or unsigned host executables as signed artifacts.

The release also includes an external `SHA256SUMS` for the ZIP downloads; the checksum file inside each ZIP covers its payload and manifest. Reproducible assembly means identical input files produce identical archive bytes; it does not claim independent Gradle/Cargo builds are bit-for-bit reproducible.
