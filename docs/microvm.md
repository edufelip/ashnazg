# Apple Silicon Linux VM backend

This backend is under qualification. The source provides a native `ashnazg up` CLI and Swift Virtualization.framework helper; it does not yet provide a qualified default-download bundle or prove Android boot on macOS. The existing Android device and Linux container paths remain usable independently.

## Build and inputs

Build the helper on Apple Silicon macOS with the commands in [the helper guide](../platform/macos/README.md). Local ad-hoc signing uses only `com.apple.security.virtualization`; Developer ID signing and notarization are not qualified.

The guest uses a SHA-256-pinned Linux 6.12.117 ARM64 source and Alpine 3.22.8 rootfs. A reviewed [authenticated package lock](guest-packages.md) is mandatory. The package proposal workflow resolves and verifies signed packages natively on Linux ARM64; review and commit its generated `runtime/microvm/packages.lock.json` before building the VM image. No moving repository is consulted by the qualified image build. If a locked upstream filename disappears or its bytes change, the build fails rather than selecting another version.

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
