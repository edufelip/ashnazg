# Ubuntu x86_64 development session

The host needs Docker, Android platform-tools (`adb`), Java 17, Android SDK 35
platform/build-tools, Python 3, and an available `/dev/binder`. Install and
configure these prerequisites through your normal system process. `dev_session`
never installs packages, invokes sudo, loads kernel modules or provisions
host Binder devices.

From the checkout, validate prerequisites with:

```sh
export ANDROID_HOME="$HOME/Android/Sdk" # or your actual installed SDK directory
python3 scripts/dev_session.py check
```

Build the host MCP executable (`cargo build --release`) and start an everyday
session with:

```sh
python3 scripts/dev_session.py start --host-command target/release/ashnazg -- mcp
```

This foreground command uses the digest-pinned Android 15 (64-bit-only) ReDroid image and its existing
ownership-labeled container supervisor. It builds the Android agent and fixture
APKs with the checked-in Gradle wrapper, connects `adb` explicitly to
`127.0.0.1:<port>`, installs the APKs, allocates a host port for the guest agent,
starts instrumentation with a fresh token, and waits for the harness's
authenticated ping before launching MCP. MCP receives `ANDROID_SERIAL`,
`ASHNAZG_TOKEN`, and `ASHNAZG_AGENT_ADDR` in its environment.
Keep the terminal open; Ctrl-C or process exit stops the host child, agent
instrumentation/forward, and only the ReDroid container carrying this run's
ownership label. A conflicting port forward or unavailable host prerequisite
fails closed. The ReDroid boot deadline is configurable with `--timeout` and
the host ADB endpoint with `--adb-port`.

The host MCP binary must already be built, and Android SDK components must
already be installed. The first run may need network access to fetch the pinned
container image and Gradle dependencies. Kernel Binder support and actual
Docker/ReDroid startup depend on the host; `check` validates SDK directory, executable availability, Docker daemon access
and Buildx availability, not a live container boot.

## Use the published Linux package

Extract the verified Linux x86_64 release and use its directory in place of the
source build. This checks the executable and APK bytes against the extracted
release manifest. Use the [release checksums](releases.md) to verify the archive
before extraction.

```sh
python3 scripts/dev_session.py start \
  --release-dir /path/to/extracted-release \
  --adb-port 15575 -- mcp
```

Packaged startup uses Docker and adb. Java, Gradle, SDK build-tools, and Buildx
are only needed for the source-build workflow or separate developer tasks.

For a Binderfs-capable Linux x86_64 kernel, a trusted static launcher can create
the Binder devices inside each disposable guest. Compile it from this checkout
and supply it explicitly:

```sh
mkdir -p artifacts/development
cc -static -O2 -Wall -Wextra -Werror runtime/redroid/binder_bootstrap.c \
  -o artifacts/development/binder-bootstrap
python3 scripts/dev_session.py start \
  --release-dir /path/to/extracted-release \
  --binder-bootstrap artifacts/development/binder-bootstrap \
  --adb-port 15575 -- mcp
```

The launcher runs as PID 1 in the owned privileged container. It mounts
Binderfs there, allocates that guest's nodes, and hands off to Android init.
Use just the checked-out launcher source to build this executable. Docker,
a local daemon, and kernel Binderfs support are still required.

For simultaneous sessions, use this private Binderfs option and a distinct
`--adb-port` for each guest. If you retain data, use a separate `--state-dir`
per guest. ADB assigns each host agent relay a separate port, and
`ASHNAZG_AGENT_ADDR` in the child environment carries that address. Each
Android guest still uses its own internal agent port 7777.

Each invocation writes the existing ReDroid supervisor receipt to a unique
`artifacts/development/<run-id>/receipt.json` directory. These sessions are
**disposable by default**: container cleanup deletes the anonymous Android data
volume. To retain app data across invocations, select a private session directory:

```sh
python3 scripts/dev_session.py start \
  --state-dir "$HOME/.local/share/ashnazg/meer" \
  --host-command target/release/ashnazg -- mcp
```

The directory is created mode 0700 and must be owned by the current user. Its
manifest pins the image identity, architecture and display configuration. A
second supervisor cannot acquire its lock. On restart, the supervisor removes
stale containers referencing this data only after checking their data identity,
run ownership label and exact bind mount. It disconnects only their published
loopback ADB endpoints, then checks that the new endpoint is available. Unknown
containers and incompatible or missing data fail closed; data is never reset.
Container cleanup retains this bind-mounted data. Existing disposable CI behavior
is unchanged. Keep the directory on a local filesystem supporting flock, with the
local Docker daemon; do not copy a live session or alter its lock/manifest.

Invoking the command again recovers stale owned guests. In addition, startup
can retry transient Docker/ADB timeouts and an exhausted per-attempt boot deadline
before launching the host command. `dev_session start` defaults to
`--startup-attempts 2`; choose 1 to disable or 3 for one additional attempt.
The lower-level `redroid.py run` retains a single-attempt default.

All attempts share the original `--timeout` deadline: each attempt receives a share
of the remaining time. Intermediate failure diagnostics are capped to reserve up
to 25 seconds for owned cleanup and a share for subsequent attempts. Diagnostics
may be shortened or skipped; their elapsed time and cleanup consume the original
deadline. Very short budgets may still leave no time to retry. Final failure
diagnostics and cleanup retain separate bounded teardown budgets. Each retry requires
verified owned-container removal, retains the same locked persistent storage,
uses a fresh run/container identity, and checks endpoint availability again.
Configuration, storage, ownership, and active-port conflicts fail immediately.
Unverified cleanup prevents retry. The host command executes at most once;
a failure after it starts never causes automatic replay of login, install, tap,
or other app mutations. Agent readiness/restart escalation after host-command
launch remains a separate future stage.

Attempt records are written to `startup-attempt-N.json` and included in the final
receipt, with phase, identity, elapsed time and bounded error text. Diagnostic
write failures are reported without replacing the original startup exception.
A reclaimed TCP endpoint in TIME_WAIT is reusable; an active listener is still
refused. Stopped-container recovery also uses retained Docker port bindings to
remove its stale loopback ADB registration. To start fresh, choose
a different empty directory; retain the old one for inspection. There is no
reset/delete command. Runtime/image migration, agent-level retry escalation,
Android keystore retention and database crash consistency still require live
qualification. Preserving files cannot prevent server-side login expiry.

The development session requests 60 Hz. The lower-level `redroid.py run`
retains its previous default and accepts `--fps 30|60` and `--state-dir`.

For a shell in which to install a real app, start with
`--host-command /bin/bash -- -i` instead of MCP. The selected serial and fresh
agent token are inherited by commands in that shell. Keep the token out of logs.
