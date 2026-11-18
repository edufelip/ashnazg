# Replay the Meer DEV APK upgrade

Use an isolated Meer checkout and an owned Linux x86_64 Ashnazg guest. The
[upgrade receipt](meer-upgrade.json) identifies both builds by source SHA and
APK hash. Build A used Meer `a4c2f58f2cceb016136254b58f04d5b322cfa30a` with
Android version code 1. Build B and the Detox runner used Meer
`0e519b9317bf7175755f168a18247b230414a66a` with Android version code 2 for the
app APK. Both app builds used the same DEV application ID and signing
certificate. Stage both APK pairs separately and verify the package versions
and signer before installing.

Build the DEV app and AndroidTest APKs from each respective Meer checkout
with its installed dependencies, Android SDK, DEV Firebase configuration
and matching debug keystore. For build B, run from `apps/mobile/android`:

```sh
./gradlew :app:assembleDevDebug :app:assembleDevDebugAndroidTest \
  -PCI_VERSION_CODE=2 -PreactNativeArchitectures=x86_64 --offline --max-workers=2
```

Use the same command with `-PCI_VERSION_CODE=1` for build A. Compare output
SHA-256 values with the receipt when reusing the original binaries. Record
new hashes for rebuilt APKs.

1. Start a packaged Ashnazg session with private Binderfs, a new
   `--state-dir`, and ADB port `15576`. From the pinned Meer runner's
   `apps/mobile` directory, set `ANDROID_SERIAL=127.0.0.1:15576` and run
   `python3 scripts/ci/android-runtime-pilot.py --runtime redroid --output "$LOGIN_RECEIPTS"`
   with the build-A binaries at the runner's configured paths. This is the
   existing mock login journey. Record Android's installed version code,
   first-install time and last-update time, then stop the guest.
2. Restart Ashnazg using the same state directory. Confirm Android still
   reports build A and its original timestamps. Run Ashnazg's
   `python3 scripts/dev_app.py --apk "$BUILD_B_APK" --serial "$ANDROID_SERIAL"`,
   then replace the AndroidTest APK with
   `adb -s "$ANDROID_SERIAL" install -r -t "$BUILD_B_TEST_APK"`. Check that
   Android reports version code 2 and the same first-install timestamp.
3. Use the pinned pilot runner's Metro and mock API environment, both reverse
   mappings, and its `android.attached` Detox config restricted to the owned
   serial. Point the app and test APK entries to build B. Use the pinned
   `e2e/jest.config.js` as the base for a scratch Jest config, set its
   `rootDir` to the pinned `apps/mobile` path and add the scratch spec
   directory to `roots`. Set `testMatch` to `['**/resume.e2e.js']` and append
   `<rootDir>/e2e/pilot/ready-input.cjs` to `setupFilesAfterEnv`. Copy the
   [recorded resume spec](meer-resume.e2e.js) to the scratch directory as
   `resume.e2e.js`. The spec loads the launch helper from the working
   directory, so invoke Detox from that same `apps/mobile` directory:

   ```sh
   yarn detox test --config-path "$DETOX_CONFIG" -c pilot --reuse \
     --testPathPatterns resume.e2e.js --testNamePattern \
     '^Packaged persistence retains authenticated Meer home without signing in again$' \
     --retries 0 --artifacts-location "$DETOX_ARTIFACTS"
   ```

Require a zero exit code and exactly one passing test. The recorded spec
asserts that Home's Search control appears and the login form is absent.
It leaves account identity and DEV auth seeding unverified. Stop the owned
services and guest and confirm their connections are gone.
