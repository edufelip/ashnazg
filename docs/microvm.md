# Apple Silicon Linux VM backend

This backend is under qualification. The native `ashnazg up` CLI and Swift Virtualization.framework helper have booted Android 15 on Apple Silicon macOS. Native fixture integration, a comparative benchmark and one restart have passed; a default-download bundle remains unqualified. The existing Android device and Linux container paths remain usable independently.

## Build and inputs

Build the helper on Apple Silicon macOS with the commands in [the helper guide](../platform/macos/README.md). Local ad-hoc signing uses only `com.apple.security.virtualization`; Developer ID signing and notarization are not qualified.

The guest uses a SHA-256-pinned Linux 6.12.117 ARM64 source and Alpine 3.22.8 rootfs. A reviewed [authenticated package lock](guest-packages.md) is mandatory. The package proposal workflow resolves and verifies signed packages natively on Linux ARM64; the current authenticated 133-package snapshot is recorded in `runtime/microvm/packages.lock.json`, with source and hosted evidence in the package guide. No moving repository is consulted by the qualified image build. If a locked upstream filename disappears or its bytes change, the build fails rather than selecting another version.

The native guest explicitly selects the `android15-64only` upstream image profile. The verbose boot diagnostic identified the first Android failure as an attempt to execute `/vendor/bin/boringssl_self_test32`, which fails with `Exec format error`: Apple Virtualization.framework's ARM64 guest cannot execute AArch32 binaries even with kernel `CONFIG_COMPAT=y`. Android's `reboot_on_failure` then shuts down before Binderfs setup, so the later Binder `ENOENT` is secondary and is not addressed by changing Binder injection. This profile avoids that 32-bit userspace path; the cold-boot evidence below records a successful Android readiness result.

The profile is pinned to Docker Hub's `15.0.0_64only-latest` index, `sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee`, with amd64 manifest `sha256:58e548d78c4854f032912757dc0782d7a86b9d602ffc10b2f09fb9e86731adf3` and arm64 manifest `sha256:dc2024a999dd0acb1112a23cddc9537ec8cb60a0dbe5a87ce9fbd75a2f5e94d2`. These digests were checked against the [Docker Hub tags API](https://hub.docker.com/v2/repositories/redroid/redroid/tags/15.0.0_64only-latest). The ordinary Linux supervisor profile remains its existing Android 15 pin; the 64-only profile is an explicit selection and cannot be combined with a locally derived image provenance descriptor until that provenance contract covers it.

The required Quality workflow calls the MicroVM validation workflow. Native Swift checks run for every candidate. Guest-related changes require the actual Linux kernel/rootfs build; unrelated changes avoid rebuilding that unchanged guest. The image job cannot pass without its reviewed package lock. Build-only artifacts explicitly do not constitute VM boot evidence.

The image workflow produces a compressed bundle containing `manifest.json`, `kernel`, `rootfs.ext4`, source/config/notices and package-lock provenance. Extract it into a dedicated directory and verify its archive checksum. The helper verifies the fixed kernel/rootfs paths, file types, sizes and SHA-256 values before use. A manifest source SHA identifies its build revision; it is not a publisher signature.

## Complete package metadata

New guest builds read the installed APK database back from the composed ext4 image,
compare it with the build tree, and require complete identity/source metadata
mapping before packaging. The archive includes `runtime-installed.db` and
`runtime-components.json`; `provenance.json` records their hashes. The inventory
includes inherited Alpine packages as well as installed locked additions.

A [retained inspection](evidence/rootfs-inventory-7a76029/receipt.json) of the
previously accepted `7a76029` image maps 142 installed packages into 47 source
origin/build-commit groups. Of these, 132 match locked APK identities and 10 match
the verified base. `busybox-binsh` retains its exact `aarch64` base record while
the downloaded same-version candidate is `noarch`; the inventory explicitly
records that candidate as not installed. The downloaded APK staging directory
and package cache contain no retained APKs in this inspected image.

This is metadata evidence. Corresponding source archives and component notices
remain uncollected (`source_closure_status: not_collected`), so this inventory
does not qualify a public runtime bundle release. The updated builder still
requires a Linux CI run; the retained inspection does not establish a new image
build or boot.

## Local run

```sh
cargo build --release --locked
./target/release/ashnazg up --assets /absolute/path/to/extracted/assets \
  --vm-helper /absolute/path/to/ashnazg-vm --state-dir /absolute/path/to/private/test-state
```

The helper stays in the foreground. It allocates two virtual CPUs and 2 GiB RAM, boots a writable copy of the guest disk, and waits for the guest's Android readiness response over vsock. Its loopback ADB endpoint defaults to `127.0.0.1:15555`. Only after readiness should a caller connect ADB to that endpoint and run the existing project acceptance runners with an explicit `ANDROID_SERIAL`.

Guest Android diagnostics are opt-in. Local runs collect no private Android logs by default. To capture bounded failure diagnostics, set `ASHNAZG_CI_DIAGNOSTICS=1` in the host environment; the helper adds only the `ashnazg.diagnostics=1` kernel command-line token, and guest PID 1 passes the environment flag to the ReDroid supervisor only when that exact token is present. The supervisor first verifies its unique container ownership label, then runs read-only, fixed Docker `exec` probes for recent Android logcat, LMKD properties and processes, memory pressure, cgroups and mounts. Each probe is limited to five seconds and bounded output; collection has a separate 45-second cap. Diagnostic output is stored in the backend receipt and redacts token, password and secret values, including JSON and flag forms. Opting in can retain private Android logs in the output directory, so use it only for disposable test guests and handle the output as sensitive data.

The guest's Android readiness deadline remains 600 seconds. On an opt-in diagnostic run, allow the host controller time to collect failure evidence after that backend deadline, for example `ASHNAZG_CI_DIAGNOSTICS=1 ashnazg up --timeout 900 ...`. The extra host time does not extend functional readiness or turn an Android timeout, ANR or other hard failure into success. Diagnostics run before owned-container cleanup; a failed or exhausted probe is recorded as unavailable, and cleanup still proceeds.

State is private and exclusively locked. The helper refuses non-empty unowned directories, symlinks and persistent disks from a different template/source; it does not overwrite existing user data to adopt a new bundle. Use a new state directory for a changed image. Serial logs and runtime receipts record artifact identity, resource settings and monotonic startup timings without authentication tokens.

SIGINT/SIGTERM closes and drains owned relays, requests guest shutdown, and waits for guest cleanup and poweroff. Guest shutdown is bounded; an error or timeout forces a bounded VM stop and records a failed receipt. PID 1 terminates its owned Android supervisor and Docker service before syncing and powering off. It never exits into a kernel panic on an ordinary shutdown or startup failure.

## Recorded native cold boot

The clean `7a760294d3a485cc9f40308c791a58e160d620f1` source and its [hosted artifact build](https://github.com/edufelip/ashnazg/actions/runs/43771299927) were tested with an ad-hoc-signed helper on Apple Silicon macOS. Both artifact ZIP digests, the inner archive checksum, manifest asset hashes and every requested kernel setting were verified before creating a fresh state directory. The retained [verification record](evidence/microvm-cold-boot-7a76029/verification.json) includes the two download receipts, API digest comparisons and all 79 kernel assignments; these checks were repeated when recording the evidence. The generated kernel has PSI and FUSE built in, with PSI enabled by default.

The [runtime receipt](evidence/microvm-cold-boot-7a76029/runtime.json) records Android readiness in 103.971 seconds from VM startup and successful orderly shutdown. This includes a cold image pull; it is one observation, not a boot-time distribution. The serial log contains no observed LMKD exit cycles, and the opt-in guest receipt confirms `/proc/pressure/memory` and cgroup-v2 controllers are available.

Native, [Compose](evidence/microvm-cold-boot-7a76029/compose.png), and [React Native](evidence/microvm-cold-boot-7a76029/react-native.png) action/screenshot paths ran before the [integration failure](evidence/microvm-cold-boot-7a76029/integration-failure.json). The MCP install test then selected a locally signed fixture from the checkout instead of the hosted APK selected by `--root`. Android rejected it with `INSTALL_FAILED_UPDATE_INCOMPATIBLE`; the [evidence receipt](evidence/microvm-cold-boot-7a76029/receipt.json) records both certificate digests. The overall run failed, and neither benchmarks nor warm restart ran. T3's Device panel discovered this owned Android endpoint, but its screenshot request failed; the images linked here came from the project's integration runner.

## Native acceptance after the artifact-path repair

The [successful retest receipt](evidence/microvm-acceptance-7a76029/receipt.json) uses the same immutable `7a76029` guest, hosted APKs and host implementation, with harness repair `7cf39f6`. The repair makes both MCP install calls use the fixture selected by `--root`; it does not bypass Android signature verification. The receipt separately identifies the cold/restart checkout revisions and confirms the host/guest implementation paths are unchanged from the artifact source.

A new state directory passed [cold integration](evidence/microvm-acceptance-7a76029/cold-integration.json): 28 RPC checks and 10 MCP checks across native, Compose and React Native fixtures. The [benchmark](evidence/microvm-acceptance-7a76029/cold-benchmark.json) completed all ten paths with 100 successful samples and zero errors. The [cold runtime](evidence/microvm-acceptance-7a76029/cold-runtime.json) reached readiness in 108.923 seconds from VM startup, including a new image pull. Host manifest hashing is outside that VM-startup measurement. The acceptance driver observed 111.342 seconds cold and 33.452 seconds on restart from CLI process launch; those intervals include host setup and polling, while the runtime measurements begin later at VM startup.

A restart of the same owned disk passed [another 28 RPC and 10 MCP checks](evidence/microvm-acceptance-7a76029/warm-integration.json), reaching readiness in 24.460 seconds with cached images. Both runs returned helper exit code zero and a `stopped` runtime receipt after SIGTERM, guest-owned container cleanup and poweroff. A simultaneous launch was [refused by state ownership](evidence/microvm-acceptance-7a76029/state-lock-collision.json). The stopped guest's [container directory](evidence/microvm-acceptance-7a76029/shutdown-container-directory.txt) was empty, and a [post-shutdown forward query](evidence/microvm-acceptance-7a76029/post-shutdown-forwards.json) found no entry for the owned host serial.

The cold and restart screenshot sets are retained alongside the receipts. Native, Compose, React Native and MCP PNGs from the cold retest were visually inspected. These are one cold run and one restart, not a boot-time distribution or an idle-CPU measurement. The original failed run remains above as a separate result.

## Qualification still required

Remaining v0.3 delivery work includes broader injected-failure and repeated-start coverage, default bundle acquisition, agent/MCP setup integration, and distribution packaging. The local acceptance above establishes the tested generated image, Android fixture paths, initial restart and owned shutdown behavior; it does not qualify those remaining installation and recovery flows. No kernel-size, RAM, idle-CPU, boot-time or zero-copy screenshot target is established by compiling the helper. GitHub-hosted macOS builds cannot replace the local VM acceptance run.

[iOS research](research/ios.md) remains a separate track; the Linux guest does not provide iOS compatibility.
