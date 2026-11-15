# Experimental ReDroid HWC runtime

The existing ReDroid image and CI lanes remain the defaults. This separate,
local-only recipe builds an experimental derived image with the project HWC2
module and the same Agent and Fixture APK bytes. The image keeps the pinned
Android 15 ReDroid base, upstream `/init` entrypoint, and the known 720x1280,
320 dpi, 60 fps, guest-GPU and memfd boot arguments.

## HAL layout and loading

The inspected ReDroid prebuilt makefile declares `hwcomposer.redroid` with relative
directory `hw`, `LOCAL_MULTILIB := both`, and
`LOCAL_PROPRIETARY_MODULE := true`; that is the vendor module location for
64-bit builds. Android's legacy hardware module loader resolves the
`hwcomposer` class plus hardware variant to `hwcomposer.<variant>.so`, searching
the 64-bit HAL directories. Those sources make
`/vendor/lib64/hw/hwcomposer.redroid.so` the expected path for ReDroid's vendor
module. The actual pinned image layout is still checked at build time: the
builder creates a uniquely named stopped container from the pinned base and
copies only the two allowlisted candidates to inspect them. It accepts exactly
one existing path with a 64-bit ELF matching the requested architecture and
fails closed for a missing, ambiguous, or unsupported layout. It does not
assume that copying the file proves it loaded.

The Android loader's selected module is qualified separately. The acceptance
runner requires the loaded HWC dump marker emitted through SurfaceFlinger and
compares the module pulled from the guest with the image's HWC SHA-256 label.
It also captures mode telemetry for the module's two static configurations.
The system path remains a candidate only if it is present in the pinned base;
if runtime evidence shows another path, update the qualification contract and
allowlist together before changing this recipe.

Source pointers:

- [ReDroid `device_redroid-prebuilts/Android.mk`](https://github.com/remote-android/device_redroid-prebuilts/blob/master/Android.mk): names the module, relative `hw` directory, multilib behavior and proprietary/vendor installation.
- [AOSP `hardware.c` at the Android 15 libhardware source revision](https://android.googlesource.com/platform/hardware/libhardware/+/8bb5acc281693ad3f19b778f00ca17cdfd7d56c9/hardware.c): the loader's module naming and hardware-library search behavior.
- [ReDroid image documentation](https://github.com/remote-android/redroid-doc/blob/master/README.md): Android 15 and amd64/arm64 image support.
- [The HWC ABI provenance note](../runtime/hwc/UPSTREAM.md): pinned AOSP HWC ABI and Composer interface sources.

The base image itself is fixed by the index and per-platform digests in
[`scripts/build_android_image.py`](../scripts/build_android_image.py). The
ReDroid makefile link above is the upstream `master` view; the published image
metadata available in this checkout does not identify the exact source commit
used to produce the pinned OCI digest. Treat that makefile as source evidence
for the intended vendor path, not proof of the pinned image's contents. The
runtime probe is authoritative for files actually present in that immutable
image; the AOSP loader source is pinned by the HWC ABI provenance note.

## Build locally

Build both NDK variants with an already installed Android NDK and CMake. This
does not install tools:

```sh
scripts/build_hwc.sh --output-dir artifacts/hwc/modules
```

The output paths are
`artifacts/hwc/modules/arm64-v8a/hwcomposer.default.so` and
`artifacts/hwc/modules/x86_64/hwcomposer.default.so`. Build the APKs with the
checked-in Gradle projects, then build the image for the Android ABI that
matches the chosen module:

```sh
cd agent-android
./gradlew --no-daemon :agent:assembleDebug :fixture:assembleDebug
cd ..
python3 scripts/build_hwc_image.py \
  --source-sha "$(git rev-parse HEAD)" \
  --version 0.2.0 \
  --platform linux/amd64 \
  --tag ashnazg-redroid:0.2.0 \
  --agent-apk agent-android/agent/build/outputs/apk/debug/agent-debug.apk \
  --fixture-apk agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk \
  --hwc-module artifacts/hwc/modules/x86_64/hwcomposer.default.so \
  --output artifacts/hwc/provenance.json
```

For `linux/arm64`, use the `arm64-v8a` module. The builder requires the
existing recipe version and tag so the unchanged `scripts/redroid.py`
provenance verifier accepts the image. It requires a clean checkout whose full
source SHA matches `HEAD`, regular APKs with manifests, a matching 64-bit ELF,
Docker access to the pinned base, and every declared recipe input. It writes
the existing schema 1 provenance shape; no HWC-specific top-level fields are
added. The original APK hashes remain in their existing fields and labels.

The image's extra labels are:

- `org.ashnazg.hwc-sha256`: SHA-256 of the selected module bytes.
- `org.ashnazg.hwc-paths`: JSON array containing the single probed guest path.
- `org.ashnazg.hwc-profile`: experimental static-config/client-composition
  profile; it explicitly records that there is no governor.
- `org.ashnazg.hwc-context-sha256`: JSON object hashing every staged Docker
  context file, including Dockerfile, notices, init rc, both APKs and module.

The fresh Docker inspect is recorded in `image_inspect`, including all labels.
The existing runner then validates that full inspect and launches the
immutable image ID from provenance rather than resolving its mutable tag.

## Runtime qualification

The dedicated [`hwc.yml`](../.github/workflows/hwc.yml) workflow runs on
relevant pull requests or manual dispatch. It runs host ABI/state tests and
builds both NDK variants, builds the Rust target, APKs and framework fixtures,
provisions its owned Binder fixture, and builds only a local x64 derived image.
The running action uses the existing `image-provenance` input. Before normal
integration begins, `scripts/hwc_acceptance.py` must verify the loaded module
marker and byte hash and record HWC mode telemetry. It then runs preinstalled
Agent/framework integration and a 10-iteration benchmark into separate HWC
artifacts. Binder cleanup runs even on failure. No OCI image is published,
and the default upstream and derived image workflows are unchanged.

The prototype reports two static configurations, supports client composition,
and does not implement an activity governor. Runtime acceptance does not make
a performance or power claim and does not establish redistribution clearance.
The source and upstream component licenses remain applicable; see
[`runtime/hwc-image/NOTICES.md`](../runtime/hwc-image/NOTICES.md).
