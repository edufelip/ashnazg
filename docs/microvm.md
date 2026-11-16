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

## Opt-in authenticated agent session

The candidate helper can prepare the Android agent without invoking host ADB.
This path requires a guest built from the same candidate source containing the
new session service; the historical `7a76029` and PR #8 guest artifacts do not
contain that service. VM-only development remains available.

```sh
ashnazg-vm --assets /absolute/path/to/candidate/assets \
  --state-dir /absolute/path/to/new/private/state \
  --agent-apk /absolute/path/to/agent.apk --agent-sha256 EXPECTED_SHA256
```

The helper verifies the opened APK, sends its bounded upload over the owned
host-to-guest channel, and waits for an authenticated agent ping in the guest.
The guest uses its locked ADB to install the expected agent package. Agent RPC
opens the fixed device service through ADB Smart Sockets; it creates no shared
ADB forward and removes no other client's mappings.

After preparation, the helper owns loopback agent/install relays (defaults
`17777` and `17778`) and publishes `.ashnazg-session-v1.json` under its private,
exclusively locked state directory. That file contains the fresh credentials;
public `agent_ready` receipts contain only session, endpoint, artifact and timing
metadata. Existing credential files are refused. Shutdown closes the relays,
agent resources and owned credential before releasing state.

Host tests cover framing, ownership, cancellation, FIFO refusal, nonblocking
upload deadlines and relay half-close behavior. Live explicit-helper acceptance
is recorded below. The Rust `up` command and MCP
server do not yet expose this opt-in helper setup; automatic artifact acquisition
and native MCP installation are subsequent delivery steps.

The first matching hosted guest from source `4f130cc` passed its Linux build and
all Quality/ReDroid checks, but [native session acceptance failed](evidence/native-session-failure-4f130cc/receipt.json).
Android became ready after 110.043 seconds; the guest accepted the APK upload
header, then returned `backend_failed`. A separate diagnostic run reproduced
Android 15 rejecting the install command's trailing `-` as an unknown option.
With the same APK and `-S SIZE`, omitting the pathname returned `Success`.
The [pinned package-manager implementation](https://android.googlesource.com/platform/frameworks/base/+/cc8bb19595c661f5cf42f330457000e643c67d1b/services/core/java/com/android/server/pm/PackageManagerShellCommand.java)
selects stdin when no positional arguments remain. The diagnostic used host ADB;
it is not successful acceptance of the host-ADB-free session. The corrected
guest must be rebuilt and tested through that full path before qualification.

The rebuilt `2245f29` guest [prepared the agent successfully](evidence/native-session-diagnostic-2245f29/receipt.json),
then exposed two further issues. A restart rejected the private state directory
because Foundation's `/tmp` spelling differed from C `realpath`'s `/private/tmp`
spelling. The repaired check permits only exact macOS system aliases and rejects
custom symlinks and parent traversal before normalization. Separately, Android's
successful `Status: ok` launch output was rejected by a backtracking negative
lookahead. The launcher now relies on its existing exact-one-`ok` status check
and still rejects explicit error or exception lines.

An offline, readback-verified patch to the **writable diagnostic disk** passed
28 RPC checks and 7 MCP checks, including native, Compose and React Native
actions/screenshots. The runner used no host ADB; separate diagnostic commands
used it to reproduce the launch-output failure. The diagnostic helper exited
zero, removed its credential and closed all three listeners. The MCP checks
exclude native `app_install` wiring, which remains subsequent work; APK installs
were exercised through the helper's native installation channel. This modified
disk result does not qualify the immutable guest or a release. A rebuilt guest
and fresh cold/restart acceptance remain required.

The [subsequent immutable acceptance](evidence/native-session-acceptance-b8806d8/receipt.json)
uses the matching `b8806d8` hosted guest and APK artifacts without offline disk
patches. Both a fresh cold state and restart passed 28 RPC checks and 7 MCP
compatibility checks, including native, Compose and React Native actions and
screenshots. The runner invoked no host ADB. Invalid installation authentication
and a corrupt upload were rejected before a valid install/launch succeeded.
Both helpers exited zero, removed their credentials and closed all three owned
listeners. The receipt binds download digests, boot-asset/config verification,
driver and binary hashes, and relevant source-tree parity. Native MCP
`app_install`, default acquisition and automatic CLI session supervision remain
subsequent delivery steps; this is acceptance of the explicit helper session.

The [merge-head retest](evidence/native-session-acceptance-0155f64/receipt.json)
repeats cold and restart acceptance with the rebuilt `0155f64` guest after the
bounded ping-cancellation repair. Both runs again passed 28 RPC and 7 MCP
compatibility checks without host ADB or offline guest patches, including
upload rejection, installation and clean session teardown. The receipt retains
all 27 payload hashes and the matching artifact/config verification. Startup
timings come from the `agent_ready` receipt; the stopped receipt's
`total_startup_ms` includes elapsed test time and is recorded separately. These
remain individual observations rather than a comparative performance result.

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

The Rust [native session owner](native-session.md) now supervises explicit local
helper startup, authenticates the agent and supplies RPC/install clients. Its
[one-restart acceptance](evidence/native-owner-acceptance-5256b94/receipt.json)
passed three Rust factory installs, 28 RPC and seven MCP compatibility checks
against the existing pinned guest, then joined orderly shutdown without host
ADB. This does not yet expose automatic native sessions through CLI/MCP or
qualify fresh-state startup with the new supervisor.

Remaining v0.3 delivery work includes broader injected-failure and repeated-start coverage, default bundle acquisition, agent/MCP setup integration, and distribution packaging. The local acceptance above establishes the tested generated image, Android fixture paths, initial restart and owned shutdown behavior; it does not qualify those remaining installation and recovery flows. No kernel-size, RAM, idle-CPU, boot-time or zero-copy screenshot target is established by compiling the helper. GitHub-hosted macOS builds cannot replace the local VM acceptance run.

[iOS research](research/ios.md) remains a separate track; the Linux guest does not provide iOS compatibility.
