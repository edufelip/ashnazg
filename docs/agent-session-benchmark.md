# Android MCP session benchmark

Run both Android runtimes serially on the same Linux x86_64 host. The workload starts an owned guest, opens the native fixture through the Android test harness, and measures the same three public MCP tools in each arm. The fixture returns a semantic counter and draws the same count as a machine-readable marker. It accepts one tap per trial and observes the updated marker through a PNG. Failed mutations are retained without replay.

Use the [paired Meer login, Search and Home benchmark](evidence/meer-runtime-comparison-2026-11-18/README.md) for real-app journey latency. That workload uses Detox and a frozen mock dataset. Its timings are separate because they start inside the test body. Its old endpoint memory samples use the process scopes and sweep times documented there.

## Run it

Use a Linux host with KVM, local Docker, cgroup v2 with memory and CPU accounting in the Docker and systemd user scopes, Binderfs, adb, an installed Android SDK Emulator with an API 35 x86_64 AVD template, and Python with Pillow. Reserve separate unused local ports for both guests. Keep host load stable while the comparison runs.

Build the fixture, agent and private Binder bootstrap on the benchmark checkout. Supply the same built MCP binary and APKs to both arms.

```sh
cargo build --release --locked
(cd agent-android && ./gradlew :agent:assembleDebug :fixture:assembleDebug)
mkdir -p artifacts
cc -static -O2 -Wall -Wextra -Werror runtime/redroid/binder_bootstrap.c \
  -o artifacts/agent-benchmark-binder
python3 -m scripts.agent_session_benchmark pair \
  --binary target/release/ashnazg \
  --agent agent-android/agent/build/outputs/apk/debug/agent-debug.apk \
  --fixture agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk \
  --binder-bootstrap artifacts/agent-benchmark-binder \
  --emulator "$ANDROID_HOME/emulator/emulator" \
  --avd-template "$HOME/.android/avd/Comparison_API35.avd" \
  --avd-port 5570 --redroid-port 15583 \
  --first avd --pairs 2 --output artifacts/agent-session-pair
python3 -m scripts.agent_session_report artifacts/agent-session-pair
```

Cold means a fresh runtime data directory with APK installation included. Warm means a new runtime process with the same user data and the same installed APKs. The AVD takes full boots in both modes. Its Quick Boot snapshots are disabled. Fresh pairs get distinct ReDroid state directories and private AVD homes. The operator's configured AVD is read only as a template.

Both guests flush their data before release. The AVD uses the emulator console
for orderly shutdown, with an owned systemd scope as the cleanup fallback.
Warm admission checks both previously installed packages and the cold run's
state marker before timing a usable MCP snapshot.

Both guests must report Android API 35, x86_64, 720×1280 at 320 dpi, and disabled animation scales. Both are configured for 60 Hz and software rendering. The Android system images, graphics implementations and virtual hardware can differ. The monitor confirms the same available host CPUs, a two-CPU quota, the same 8 GiB host cgroup limit and swap disabled. A hit on the memory limit invalidates that trial. The AVD has its own guest RAM setting. Host cgroup limits do not determine the memory or CPU topology reported by either guest.

## Interpret the receipts

- **Startup** runs from invocation to the first validated MCP snapshot of the fixture. It includes the readiness gate, setup and MCP initialization. The receipt also gives the guest readiness time.
- **Action and screenshot** report the single MCP tap round trip and the delay from that response to the first PNG whose pixels decode to the updated counter. Read-only screenshot retries are recorded. Action replays are prohibited.
- **Memory** uses host cgroup v2 `memory.current`, `memory.swap.current`, and `memory.stat` for the Docker guest scope and for the AVD's emulator scope. The host-side MCP client, adb and shared services are outside both scopes. The counters include charged page cache and kernel memory, with anonymous and file memory reported separately at idle. Collect 200 ms samples within the same three-second idle, interaction and post-agent windows. The sampled interaction maximum is distinct from a true peak measurement.
- **Shutdown** reports MCP/agent teardown plus runtime cleanup, excluding the inserted post-agent memory window. After runtime teardown, the receipt records whether the owned cgroup disappeared at fixed offsets. A removed cgroup has no memory reading.
- **Reporting** keeps raw sample timestamps, screenshot hashes and attempt timings, workload controls, and failed or missing runs. The report groups only successful pairs and exits nonzero for an incomplete comparison. Treat small-sample results as descriptive for these versions, settings and host.

The run directory also contains the raw fixture PNGs and runtime-specific boot logs. Keep local logs private and review their contents before publishing evidence.

The [November 19 run](evidence/agent-session-comparison-2026-11-19/README.md)
contains two opposite-order pairs, their raw receipts, and representative PNGs.
