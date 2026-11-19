# Paired Android MCP session measurements

Two serial pairs on the same Linux x86_64 Intel Core i5-12600K host ran in
opposite orders. Each arm booted once with fresh data and once with the same data
and installed packages. All eight accepted trials observed one tap through MCP,
decoded the new counter from a fresh PNG, and verified removal of the owned
runtime cgroup. The cold and warm values below are medians across the two
independent pairs.

| Metric | ReDroid cold | AVD cold | ReDroid warm | AVD warm |
| --- | ---: | ---: | ---: | ---: |
| Launch to first usable MCP snapshot | 18.74 s | 69.26 s | 16.44 s | 41.67 s |
| Tap and fresh PNG | 771 ms | 1,471 ms | 774 ms | 1,709 ms |
| Idle cgroup memory | 1.35 GiB | 6.28 GiB | 1.13 GiB | 4.97 GiB |
| Idle anonymous memory | 0.72 GiB | 4.97 GiB | 0.62 GiB | 3.89 GiB |
| Post-agent cgroup memory | 1.23 GiB | 6.45 GiB | 1.04 GiB | 5.47 GiB |
| Shutdown | 1.76 s | 1.35 s | 1.65 s | 1.89 s |

The host scopes had access to the same 16 logical CPUs with the same two-CPU
quota, the same 8 GiB memory limit, and swap disabled. The eight admitted
trials recorded no memory-limit events at the end of the workload. Guest
images differ. ReDroid used Android 15 64-bit-only and the pinned private
Binderfs bootstrap. The AVD used Android Emulator 37.1.11.0 (15917651),
Google APIs Android 15, KVM and SwiftShader. The AVD cold log reported that
the emulator increased its guest RAM to 4 GiB. Both gave 720×1280 at 320 dpi.
Warm runs restarted each runtime with retained data and took a full boot.
The comparison does not measure the AVD Quick Boot snapshot path.

Both arms used the same agent APK, fixture APK and packaged CLI. The CLI
identifies source commit `3b331d72af2a772bcffc1f84be0c583cb6eb1829`; its Rust
sources and tool schema match the pre-benchmark base `66c2d45`.
The [manifest](manifest.json) pins the input hashes and source pair receipts.
See the [protocol](../../agent-session-benchmark.md) for timing boundaries,
memory accounting and reproduction. The [Meer benchmark](../meer-runtime-comparison-2026-11-18/README.md)
retains the separate real-app workload and its own performance evidence.

Early pilots remain outside the accepted sample. The 2.42 GiB AVD pilot
had an OOM kill. The 5 GiB pilot hit its cap during boot and was stopped.
A two-core pinning trial rejected the AVD after detecting expanded thread
affinity. A full-CPU-quota pilot caught the AVD missing its fixture package
after warm restart. Guest sync and orderly AVD shutdown corrected that in
both measured pairs. The first attempt to start the repaired run also
recorded an occupied AVD port from a prior interrupted pilot. Its owner
was cleaned up before the opposite-order confirmation run.

Recompute [the summary](summary.json) from the compressed trial receipts:

```sh
python3 -m scripts.agent_session_report docs/evidence/agent-session-comparison-2026-11-19
```

The first pair includes warm PNGs before and after the tap for
[ReDroid](screenshots/redroid-warm-before.png) ([after](screenshots/redroid-warm-after.png))
and [AVD](screenshots/avd-warm-before.png) ([after](screenshots/avd-warm-after.png)).
