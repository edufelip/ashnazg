# Android image profiles

The ReDroid action keeps its upstream profile as the default. If
`image-provenance` is empty or omitted, the action starts the pinned upstream
ReDroid image and preserves the existing action arguments. Supplying
`image-provenance` opts into a local derived image whose provenance JSON was
written by `scripts/build_android_image.py`.

The Linux x64 acceptance workflow runs the profiles sequentially. It first
keeps the complete upstream integration and benchmark lane. It then builds a
local Android 15 image, starts a second ReDroid instance with that provenance,
waits up to 45 seconds for all three global animation scales to be `0`, recording
each observation before either
test harness can change device state, runs the preinstalled-agent integration
and framework fixtures, and collects a 100-sample benchmark (10 iterations across 10 paths). The derived
integration verifies that the system APK paths and pulled bytes match the build
APKs whose hashes the image builder records in provenance. Binder fixture cleanup happens after both profiles, and
artifacts for each run use separate directories.

## Build the local image

The builder uses the immutable ReDroid Android 15 index
`redroid/redroid@sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad`.
Build the agent and fixture APKs from their checked-in Gradle projects, then
provide their paths and the full 40-character source commit SHA:

```sh
cd agent-android
./gradlew --no-daemon :agent:assembleDebug :fixture:assembleDebug
cd ..
python3 scripts/build_android_image.py \
  --source-sha "$(git rev-parse HEAD)" \
  --version 0.2.0 \
  --platform linux/amd64 \
  --tag ashnazg-redroid:0.2.0 \
  --agent-apk agent-android/agent/build/outputs/apk/debug/agent-debug.apk \
  --fixture-apk agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk \
  --output artifacts/android-image/provenance.json
```

The `--platform` value can also be `linux/arm64`; the matching upstream
platform manifest digest is recorded in provenance. The version must be
`0.2.0`, the tag must be exactly `ashnazg-redroid:0.2.0`, APK inputs must be
regular non-symlink APK archives containing `AndroidManifest.xml`, and the
source SHA must match this checkout's full lowercase `HEAD` commit. Working
tree dirtiness is also recorded. The builder stages only its Dockerfile,
notices, init rc file, and the two explicitly supplied APKs into a fresh
temporary build context. It records APK SHA-256 values and verifies Docker's
post-build inspect result for image ID, local tag, platform, provenance labels,
and filesystem layer digests before writing the provenance JSON.

## Qualification limits

The workflow exercises two profiles: the unchanged pinned upstream profile
and the optional local derived profile. A successful image build alone does
not prove that Android boots, that APKs install or run, or that performance is
acceptable; those claims require the second runtime lane to complete. The
derived recipe adds the APKs under `/system/app`, disables boot animation, and
sets 720x1280 at 320 dpi and 60 fps with guest GPU and memfd enabled.

The maintainer HWC prebuilt `6248d6a` still has unresolved source and license
provenance. The image makes no HWC governor or original 1 Hz/60 Hz behavior
claim. No OCI publication or redistribution qualification is provided. Review
the attribution and license pointers in
[`runtime/redroid-image/NOTICES.md`](../runtime/redroid-image/NOTICES.md)
before any redistribution.
