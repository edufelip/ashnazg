# Real app development acceptance

The first selected product app is GuiaBrechó / Meer, at `~/Projects/meer-mono/apps/mobile`. A frozen Meer DEV APK is the workload for the Linux MCP, viewer, session persistence, and AVD comparison runs.

## Before selecting an app

- [ ] Select the app and package, build variant, source/build provenance, expected entry point, and daily usability journey.
- [ ] Identify the developer-server command, readiness signal, host port, and required device-visible port. Do not add credentials to this checklist.
- [ ] Choose the intended physical device or emulator and copy its serial explicitly.
- [ ] Record an APK SHA-256 and build identifier before install.

## Prepare device

Use `python3 scripts/dev_app.py --apk <path.apk> --serial <adb-serial> [--server-port <port>]`. The command requires an online explicit serial (or `ANDROID_SERIAL`), installs with `adb install -r` to preserve existing app data, and optionally creates localhost reverse mappings. It never uninstalls or clears app data. It prints a checklist to stdout by default; use `--checklist <new-path>` to save a new run-specific copy. Existing files are refused.

Start and verify the app's developer server separately using the selected app's documented command. Reverse mappings use `--no-rebind` and refuse conflicting pre-existing endpoints; on setup failure, cleanup removes only mappings created by that invocation. Record any mapping that pre-existed and do not remove it during manual cleanup.

## Evidence to collect during the milestone

- [ ] Date/time, operator, device model, Android version, explicitly selected serial, and connection type.
- [ ] App/package/version, build variant and identifier, APK path and SHA-256.
- [ ] Developer-server command, readiness output, host/port, active reverse mappings, and cleanup outcome.
- [ ] Baseline app-data state and post-update preservation result.
- [ ] Launch evidence and the agreed daily usability journey, with screenshots/recording and observed timings or failures.
- [ ] Relevant app/server/device logs with credentials and personal data removed.
- [ ] Issues, reproducible steps, and unresolved gaps; distinguish local preparation from completed real-app proof.

## Acceptance status

- [Packaged Linux](evidence/packaged-linux-development-2026-11-18/README.md) verified an explicit install, Metro and mock API connectivity, login-field input with semantic readback, PNG, logs, and scrcpy reconnect.
- [Paired Android benchmark](evidence/meer-runtime-comparison-2026-11-18/README.md) verified the mock login, Search and Home journey on both Android runtimes.
- Native Ashnazg v0.3.0 on Apple Silicon passed the mock login, Search and return-to-Home Detox journey with the ARM64 Meer DEV and AndroidTest APKs. Detox attached to the owned guest's loopback relay through a private ADB server. The run used the login-button and Home feed readiness checks from the [Linux CI pilot](evidence/meer-ci-pilot-2026-11-18/README.md).
- [Packaged persistence](evidence/packaged-linux-development-2026-11-18/meer-persistence.json) verified authenticated Home after a fresh guest boot and reinstall of the same APK. A [forward upgrade](evidence/packaged-linux-development-2026-11-18/meer-upgrade.json) from Android version code 1 to 2 preserved the mock-authenticated session across guest restart and APK replacement.
- [scrcpy input](evidence/packaged-linux-development-2026-11-18/scrcpy-input.json) verified one operator click in the Linux viewer after reconnect on the native fixture.

## Meer qualification target

The existing `.detoxrc.js` builds `:app:assembleDevDebug` and
`:app:assembleDevDebugAndroidTest`, with the application APK at
`android/app/build/outputs/apk/dev/debug/app-dev-debug.apk`. Its Android device
configuration uses `android.emulator` and `GuiaBrecho_API34`, not an attached
ReDroid serial. Running that unchanged validates the AVD, not Ashnazg. Do not
silently replace that device configuration or call its green result ReDroid proof.

Use the mock backend and journeys from `e2e/smoke.e2e.js` and
`e2e/authFlows.e2e.js` to qualify the same app APK on an explicitly selected
Ashnazg guest. Record `E2E_PLATFORM=android` for any reused evidence reporter.
Read the app's `scripts/run-e2e-mock.js` before starting mock/Metro services;
use isolated ports and never adopt an occupied service from another checkout.
The prepared app's developer-server port must be reversed with this helper,
not a host-to-device `adb forward`.

For the attached-device Meer DEV run, use `http://localhost:<port>` in the app's
mock API and Expo dev-client URLs, and advertise `localhost` from Metro. The
Android debug network policy allows that hostname. Expo reports a cleartext
HTTP error if the dev-client URL uses `127.0.0.1`. The host services can still
listen on IPv4 loopback.

Required acceptance rows: install/update and retained app data; mock login;
home/search/detail navigation; text input and keyboard; scrolling; mock API
network access; Metro connectivity; MCP snapshot/action/screenshot/logs while a
viewer is attached; viewer reconnect; shutdown cleanup. Deterministic mock login
does not establish production authentication or third-party SDK compatibility.
