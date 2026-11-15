# HWC ABI provenance and runtime boundary

This source implements the Android legacy HWC2 C HAL boundary used by the
Composer HIDL 2.1 service. The small declarations in
`include/ashnazg/hwc/aosp_hwc2_abi.h` are an ABI-only subset derived from these
Apache-2.0 AOSP files at the immutable Android 15 tag commits:

- `platform/hardware/libhardware` commit
  `8bb5acc281693ad3f19b778f00ca17cdfd7d56c9`:
  - [`include_all/hardware/hardware.h`](https://android.googlesource.com/platform/hardware/libhardware/+/8bb5acc281693ad3f19b778f00ca17cdfd7d56c9/include_all/hardware/hardware.h)
  - [`include_all/hardware/hwcomposer2.h`](https://android.googlesource.com/platform/hardware/libhardware/+/8bb5acc281693ad3f19b778f00ca17cdfd7d56c9/include_all/hardware/hwcomposer2.h)
  - [`include_all/hardware/hwcomposer_defs.h`](https://android.googlesource.com/platform/hardware/libhardware/+/8bb5acc281693ad3f19b778f00ca17cdfd7d56c9/include_all/hardware/hwcomposer_defs.h)
- `platform/hardware/interfaces` commit
  `6c612564e7935c6867899042aa97dfa411b5e286`:
  - [`graphics/composer/2.1/IComposer.hal`](https://android.googlesource.com/platform/hardware/interfaces/+/6c612564e7935c6867899042aa97dfa411b5e286/graphics/composer/2.1/IComposer.hal)
  - [`graphics/composer/2.1/IComposerCallback.hal`](https://android.googlesource.com/platform/hardware/interfaces/+/6c612564e7935c6867899042aa97dfa411b5e286/graphics/composer/2.1/IComposerCallback.hal)
  - [`graphics/composer/2.1/IComposerClient.hal`](https://android.googlesource.com/platform/hardware/interfaces/+/6c612564e7935c6867899042aa97dfa411b5e286/graphics/composer/2.1/IComposerClient.hal)
  - [`graphics/composer/2.4/IComposerCallback.hal`](https://android.googlesource.com/platform/hardware/interfaces/+/6c612564e7935c6867899042aa97dfa411b5e286/graphics/composer/2.4/IComposerCallback.hal)

The Android 15 framework control path was also checked at these source revisions:

- [`DisplayManagerShellCommand.java`](https://android.googlesource.com/platform/frameworks/base/+/cc8bb19595c661f5cf42f330457000e643c67d1b/services/core/java/com/android/server/display/DisplayManagerShellCommand.java) documents `cmd display set-user-preferred-display-mode WIDTH HEIGHT REFRESH-RATE [DISPLAY_ID]` as a user preference.
- [`SurfaceFlinger/DisplayHardware/HWC2.cpp`](https://android.googlesource.com/platform/frameworks/native/+/9224694e64b2464dcdc03fe42740d06c08b27a88/services/surfaceflinger/DisplayHardware/HWC2.cpp) falls back to legacy `setActiveConfig` when constrained HWC2.4 mode switching is unavailable.

The AOSP header declarations retain their upstream Apache License 2.0 terms.
The adapter and state machine are project-authored. No ReDroid/vendor HWC
binary is included, inspected, patched, or relabeled. The HWC ABI subset is
compiled by both the host and Android NDK compilers, with local structure-size
and close-pointer-offset assertions and per-function signature checks against the pinned AOSP declarations;
the NDK itself does not publish these platform HAL headers.

## What this prototype does

- Exports the legacy `hwcomposer` hardware module through the AOSP `HMI`
  symbol and reports HWC2 device API 2.0, the ABI consumed by Composer HIDL 2.1.
- Announces one primary physical headless sink with 720x1280, 320 dpi and two
  static configurations: config 0 has a 1,000,000,000 ns period; config 1 has
  a 16,666,667 ns period (approximately 60 Hz). The geometry matches the
  current ReDroid launch recipe; changing guest geometry requires changing this
  source and rebuilding.
- Routes every layer to SurfaceFlinger's client composition. It accepts the
  composed client target, duplicates and waits up to three seconds for its acquire fence, then
  releases it with an already-signaled `-1` present fence. On timeout it closes the duplicate and returns an error without claiming a present.
  It does not import, read, save, or display client-target pixels.
- Emits vsync callbacks only when enabled, paced to the selected static config,
  timestamped with `CLOCK_MONOTONIC`; changing config only happens through the
  standard HWC2 `setActiveConfig` call.
- Builds a self-contained source module for `arm64-v8a` and `x86_64` when an
  existing NDK and CMake are available.

## Mode-control boundary

Composer HIDL 2.1 `IComposerClient` exposes `getDisplayConfigs`,
`getDisplayAttribute`, `getActiveConfig`, `setActiveConfig`, and
`setVsyncEnabled`. This module gives SurfaceFlinger two truthful static modes
and honors whichever config SurfaceFlinger selects. Android's
`cmd display set-user-preferred-display-mode` is a framework preference that
feeds display-mode policy; it does not itself create an idle/active policy or
prove that a preferred mode was selected. HWC2.4 adds constrained mode changes,
`getDisplayVsyncPeriod`, and period-bearing vsync callbacks for implementations
that report that newer ABI. This module reports HWC2.0 and deliberately does
not claim those 2.4 behaviors.

There is no built-in activity governor in this prototype. In particular, it
will not switch to 1 Hz on idle or to 60 Hz on activity without an explicit
upstream caller making HWC2 `setActiveConfig` requests (or another correctly
integrated framework policy). The current `scripts/redroid.py` invocation also
passes `androidboot.redroid_fps=30`; its effect on the Composer display mode has
not been verified, so integration must resolve that possible 30 Hz conflict. The existing parent stage must first prove the
ReDroid image's actual Composer service/module loading path, install this
module in that path, boot the base container, and verify SurfaceFlinger sees
both configs and uses real client composition. Until those checks pass, this
source is a host-tested and NDK-compiled HAL prototype, not an Android runtime
or graphics acceptance claim.

The complete upstream Apache License 2.0 text is retained in `LICENSES/Apache-2.0.txt`; the ABI declaration header preserves the AOSP copyright and attribution.
