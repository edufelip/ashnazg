# Apple Silicon Linux VM backend

This backend is under qualification. The source provides a native `ashnazg up` CLI and Swift Virtualization.framework helper; it does not yet provide a qualified default-download bundle or prove Android boot on macOS. The existing Android device and Linux container paths remain usable independently.

## Build and inputs

Build the helper on Apple Silicon macOS with the commands in [the helper guide](../platform/macos/README.md). Local ad-hoc signing uses only `com.apple.security.virtualization`; Developer ID signing and notarization are not qualified.

The guest uses a SHA-256-pinned Linux 6.12.117 ARM64 source and Alpine 3.22.8 rootfs. A reviewed [authenticated package lock](guest-packages.md) is mandatory. The package proposal workflow resolves and verifies signed packages natively on Linux ARM64; the current authenticated 133-package snapshot is recorded in `runtime/microvm/packages.lock.json`, with source and hosted evidence in the package guide. No moving repository is consulted by the qualified image build. If a locked upstream filename disappears or its bytes change, the build fails rather than selecting another version.

The native guest explicitly selects the `android15-64only` upstream image profile. The verbose boot diagnostic identified the first Android failure as an attempt to execute `/vendor/bin/boringssl_self_test32`, which fails with `Exec format error`: Apple Virtualization.framework's ARM64 guest cannot execute AArch32 binaries even with kernel `CONFIG_COMPAT=y`. Android's `reboot_on_failure` then shuts down before Binderfs setup, so the later Binder `ENOENT` is secondary and is not addressed by changing Binder injection. This profile is intended to avoid that 32-bit userspace path; a successful guest runtime boot has not yet been verified.

The profile is pinned to Docker Hub's `15.0.0_64only-latest` index, `sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee`, with amd64 manifest `sha256:58e548d78c4854f032912757dc0782d7a86b9d602ffc10b2f09fb9e86731adf3` and arm64 manifest `sha256:dc2024a999dd0acb1112a23cddc9537ec8cb60a0dbe5a87ce9fbd75a2f5e94d2`. These digests were checked against the [Docker Hub tags API](https://hub.docker.com/v2/repositories/redroid/redroid/tags/15.0.0_64only-latest). The ordinary Linux supervisor profile remains its existing Android 15 pin; the 64-only profile is an explicit selection and cannot be combined with a locally derived image provenance descriptor until that provenance contract covers it.

The required Quality workflow calls the MicroVM validation workflow. Native Swift checks run for every candidate. Guest-related changes require the actual Linux kernel/rootfs build; unrelated changes avoid rebuilding that unchanged guest. The image job cannot pass without its reviewed package lock. Build-only artifacts explicitly do not constitute VM boot evidence.

The image workflow produces a compressed bundle containing `manifest.json`, `kernel`, `rootfs.ext4`, source/config/notices and package-lock provenance. Extract it into a dedicated directory and verify its archive checksum. The helper verifies the fixed kernel/rootfs paths, file types, sizes and SHA-256 values before use. A manifest source SHA identifies its build revision; it is not a publisher signature.

## Local run

```sh
cargo build --release --locked
./target/release/ashnazg up --assets /absolute/path/to/extracted/assets \
  --vm-helper /absolute/path/to/ashnazg-vm --state-dir /absolute/path/to/private/test-state
```

The helper stays in the foreground. It allocates two virtual CPUs and 2 GiB RAM, boots a writable copy of the guest disk, and waits for the guest's Android readiness response over vsock. Its loopback ADB endpoint defaults to `127.0.0.1:15555`. Only after readiness should a caller connect ADB to that endpoint and run the existing project acceptance runners with an explicit `ANDROID_SERIAL`.

State is private and exclusively locked. The helper refuses non-empty unowned directories, symlinks and persistent disks from a different template/source; it does not overwrite existing user data to adopt a new bundle. Use a new state directory for a changed image. Serial logs and runtime receipts record artifact identity, resource settings and monotonic startup timings without authentication tokens.

SIGINT/SIGTERM closes and drains owned relays, requests guest shutdown, and waits for guest cleanup and poweroff. Guest shutdown is bounded; an error or timeout forces a bounded VM stop and records a failed receipt. PID 1 terminates its owned Android supervisor and Docker service before syncing and powering off. It never exits into a kernel panic on an ordinary shutdown or startup failure.

## Qualification still required

A complete v0.3 delivery requires a successful generated image, actual local VM boot, Android native/Compose/React Native acceptance and screenshots through the ADB bridge, repeated stop/restart and injected failure checks, measured startup/resource receipts, and default bundle acquisition/agent setup integration. No kernel-size, RAM, idle-CPU, boot-time or zero-copy screenshot target is established by compiling the helper. GitHub-hosted macOS builds cannot replace the local VM acceptance run.

[iOS research](research/ios.md) remains a separate track; the Linux guest does not provide iOS compatibility.
