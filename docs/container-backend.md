# Linux ReDroid backend

The container backend is an additional Linux acceptance lane. The existing Android emulator lane remains in `.github/workflows/quality.yml`; ReDroid does not replace it.

## Requirements

- Linux x86_64 or ARM64 with a local Unix-socket Docker daemon, the Docker Buildx plugin, and a Binder-capable host kernel. The hosted acceptance lane initially validates x86_64; ARM64 still needs its own runtime proof.
- Python 3.10 or newer, Android `adb` platform tools, and permission to access the Binder device nodes.
- The action caller must build the Ashnazg host and Android agent/fixture APKs before invoking the action.
- A command may use project scripts and artifacts in the caller's workspace. It runs only after the container is ready and receives the selected guest as `ANDROID_SERIAL`.

The action inputs are `command` (required multiline shell), `output-directory` (`artifacts/redroid`), `adb-port` (`5555`), and `boot-timeout` (`180` seconds). Port and timeout are range checked; the output directory cannot be empty, the filesystem root, or contain parent-directory path components. Input values are passed to the supervisor as distinct arguments. The command is intentionally interpreted by Bash and stored in a mode-0600 owned temporary file, removed on ordinary exit, failure, or handled signal.

## Security and ownership

Only the explicit command input is executed as shell code. The action does not interpolate paths or numeric inputs into shell source. The supervisor is responsible for uniquely identifying and cleaning up only its own container and forwarding resources; it must not clean up foreign containers, devices, or host resources. ADB is expected to bind to host loopback. The workflow grants `contents: read`, pins every referenced GitHub Action to a commit, and uses only disposable hosted runners for its Binder probe. It may load `binder_linux` only when the module is already available; it does not install kernels, restart Docker, or change the runner image.

## Provenance and validation

The supervisor writes a JSON receipt under the requested output directory. Bounded device/container diagnostics are collected only with explicit `ASHNAZG_CI_DIAGNOSTICS=1`; the disposable CI lane opts in. Child output goes to the caller's terminal or CI log. The boot timeout is independent of the command deadline (2400 seconds by default). Direct CLI callers may set `--command-timeout`; zero explicitly permits a persistent service, and is not the default for CI acceptance. Owned child process groups, ADB connections, containers and anonymous data volumes are cleaned on failure and handled signals; a cleanup failure makes an otherwise successful run fail. The workflow also retains source and runner metadata, Binder probe output, integration receipts and images, benchmark samples, the built host, and APKs even when the job fails. Pull requests check out the exact PR head SHA; pushes check out the triggering commit. Missing Binder support is a failure, not a skipped lane. The integration command runs the existing complete project integration runner with real Compose and React Native fixtures. The benchmark uses ten iterations across its ten measured paths (100 samples total); existing assertions are unchanged.

The default Android 15 source is the immutable upstream image index `redroid/redroid@sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad`. The plan's expected platform manifests are amd64 `sha256:25879821f13ffd38d5d6047d832379b96cf72ec520508b45036dca25cdc3737f` and arm64 `sha256:b21c57533662275928832602f7c09b83cb198aa9b271135ae49d978857d6795f`. The runtime must verify the resolved platform digest and record it in its receipt. The GitHub lane is x86_64 only.

ReDroid is a third-party project. Review the [upstream documentation and third-party license notices](https://github.com/remote-android/redroid-doc/blob/master/README.md), [container source](https://github.com/remote-android/redroid), and [published image](https://hub.docker.com/r/redroid/redroid). This initial backend pass does not republish or derive an image. The original v0.2 derived-image and dynamic-governor requirements remain outstanding until separately implemented and verified. The configured 720×1280, 320 dpi, software-rendered, 30 fps display and memfd settings are static configuration; a dynamic VSync governor and derived image remain future work. The receipt records requested display settings separately from observed behavior and retains raw Docker resource samples or explicit unavailable errors. No boot, CPU, memory, or latency target is considered met until a hosted receipt measures it.
