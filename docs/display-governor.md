# Experimental display probe

This is a bounded experiment for learning whether the platform SurfaceFlinger
display policy can serve as a governor control point. It is a buildable
read-only-by-default probe, not a governor, product feature, or performance
result. Runtime operation remains **unavailable until a disposable owned Linux
guest proves the bridge and the live display behavior**.

The probe is a one-shot Java `app_process` program. It caches reflection handles
once, outside any rendering or frame loop. The pinned Android API evidence is
from source base commit
`cc8bb19595c661f5cf42f330457000e643c67d1b`: `SurfaceControl` exposes
`getDynamicDisplayInfo(long)`, active and supported display mode data, and
`setDesiredDisplayModeSpecs` / `getDesiredDisplayModeSpecs`. The companion
`com.android.server.display.DisplayControl` exposes physical display IDs and
tokens through its `android_servers` JNI registration. This is a hidden,
version-sensitive platform API and is not a production contract.

## Build

The script uses only an already installed JDK and Android SDK platform 35 with
build-tools 35.0.0. It does not download or install tooling.

```sh
scripts/build_display_probe.sh --output /tmp/display-probe.jar
```

The script also writes `/tmp/display-probe.provenance.txt` beside the jar. It
records the pinned API source hashes, compiled probe source hash, SDK `android.jar`
hash, D8 version and hash, Java compiler version, and output jar hash.

## CLI behavior

Default invocation is read-only inspection:

```sh
adb shell 'CLASSPATH=/data/local/tmp/display-probe.jar app_process /system/bin com.ashnazg.displayprobe.DisplayProbe --inspect'
```

The app_process must be able to read `SYSTEMSERVERCLASSPATH` and use
`ClassLoaderFactory`. The bridge follows scrcpy's `DisplayControl` wrapper:
load the system-server classpath, resolve
`com.android.server.display.DisplayControl`, then invoke
`Runtime.loadLibrary0(..., "android_servers")` for its native registration.
The probe fails closed with an initialization error if those steps fail; it
does not switch to shell tools, transaction 1035, or another API.

The optional qualification mode requires all of the following before it makes a
mode request:

- Explicit `--disposable-guest` acknowledgement and process UID 0 or 1000.
- Exactly one physical display, obtained through `DisplayControl`.
- Cached framework API shape checks for the pinned hidden methods, classes,
  constructors, and fields.
- Exactly one 720x1280 mode at 1 Hz and one at 60 Hz, with distinct mode IDs.
- A timeout between 100 and 5000 ms (default 2000 ms) for each absolute wait.

On a qualifying guest, run it like this:

```sh
adb shell 'CLASSPATH=/data/local/tmp/display-probe.jar app_process /system/bin com.ashnazg.displayprobe.DisplayProbe --qualify-modes --disposable-guest --timeout-ms 2000'
```

For each target, it requests matching physical **and** render ranges (`1..1`,
then `60..60`) through the real `setDesiredDisplayModeSpecs` API. It considers a
request observed only after `getDynamicDisplayInfo` reports the expected
`activeDisplayModeId` before the absolute deadline. The setter's boolean return
is included as metadata and does not prove a switch. The probe captures the
original desired specs and attempts to restore them in `finally`; it reports
restore readback success separately using `getDesiredDisplayModeSpecs` equality.
The guest must still be destroyed after qualification, including when restore
readback fails.

Each output line is capped, the total output is capped at 8 KiB, the physical
display list at 16 entries, and each supported mode list at 64 entries. No
display selector, arbitrary rate/range, shell-command option, or fallback API
exists.

## Evidence and limits

Host tests cover CLI gates and bounds, mode-table validation, and absolute
deadline behavior. A local build proves javac/D8 compilation only. It does not
prove hidden API access, `android_servers` JNI initialization, permissions,
mode switching, or restoration on the target guest. The probe has not yet been
run on an owned Linux guest; do not enable it as a runtime governor until that
separate qualification records the real API call and observed display state.

This experiment is outside the existing HWC path and does not modify it. There
is no frame hot loop, CPU governor call, scheduler tuning, power claim, latency
claim, or performance claim. A governor and any agent/native integration require
a separate task after platform control is proven.

## Source references

- [Pinned `SurfaceControl.java`](https://android.googlesource.com/platform/frameworks/base/+/cc8bb19595c661f5cf42f330457000e643c67d1b/core/java/android/view/SurfaceControl.java)
- [Pinned `DisplayControl.java`](https://android.googlesource.com/platform/frameworks/base/+/cc8bb19595c661f5cf42f330457000e643c67d1b/services/core/java/com/android/server/display/DisplayControl.java)
- [scrcpy `DisplayControl` reflection and JNI-loading wrapper](https://github.com/Genymobile/scrcpy/blob/master/server/src/main/java/com/genymobile/scrcpy/wrappers/DisplayControl.java)
