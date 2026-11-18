# Meer Android runtime comparison

On this Intel Core i5-12600K Linux host, ReDroid completed the Meer login → Search → Home journey in **5.87 s** median versus **10.18 s** for the Android Studio AVD. The median sampled runtime process memory was **1.81 GiB** versus **3.70 GiB**. ReDroid had lower median journey time in 4/4 pairs and lower median memory in 4/4.

| Metric | ReDroid | Android Studio AVD | ReDroid difference |
|---|---:|---:|---:|
| Full journey p50 (s) | 5.87 | 10.18 | 42.3% lower |
| Full journey p95 (s) | 6.10 | 11.07 | 44.9% lower |
| Login helper to Home p50 (s) | 2.43 | 4.10 | 40.8% lower |
| Search helper including feed readiness p50 (s) | 2.71 | 4.95 | 45.4% lower |
| Post-trial process PSS + SwapPSS p50 (GiB) | 1.81 | 3.70 | 51.0% lower |

| Pair | Order | Journey p50 (s), ReDroid / AVD | Time reduction | Memory p50 (GiB), ReDroid / AVD | Memory reduction |
|---:|---|---:|---:|---:|---:|
| 1 | AVD → ReDroid | 6.05 / 10.01 | 39.6% | 1.81 / 3.72 | 51.3% |
| 2 | ReDroid → AVD | 5.87 / 10.08 | 41.8% | 1.82 / 3.70 | 50.8% |
| 3 | AVD → ReDroid | 5.88 / 10.25 | 42.6% | 1.80 / 3.69 | 51.2% |
| 4 | ReDroid → AVD | 5.74 / 10.41 | 44.8% | 1.82 / 3.70 | 50.9% |

## Conditions

- 4 separate AVD boots and 4 disposable ReDroid containers ran serially in alternating order. Each boot ran one excluded warm-up followed by five measured journeys. There were 20 measured journeys per runtime. The median and nearest-rank p95 are descriptive statistics across those measurements.
- The timers start within the existing Detox test body. The body includes the login reveal and Search feed waits, hardware input through Detox, the original assertions and return to Home. Detox startup, APK installation and test setup are outside this interval. The corrected `feed-ready-stock30` profile dispatched the original single taps.
- Both used Android 15 (API 35), x86_64, a 720×1280 display at 320 dpi, SwiftShader software rendering, animation scales of zero and location disabled. Device logs show both requested the same São Paulo mock feed coordinates. The frozen Meer app APK, test APK, eager Metro bundle and dataset share the same [input identities](artifacts.json).
- The AVD used KVM hardware virtualization. Its QEMU tree was pinned to host CPUs 14–15. Docker used the same cpuset and a two-CPU quota. ReDroid had a 2,593,198,080-byte cgroup memory limit. Android reported host topology on ReDroid and two vCPUs and about 2.42 GiB on AVD. ReDroid and Google APIs images and SwiftShader versions differ. The measured comparison covers these deployments on the same host.
- Memory is PSS + SwapPSS from `/proc/<pid>/smaps_rollup` immediately after each completed trial and its Detox teardown, allowing up to four reads to obtain a complete sweep. Both runtimes used the same [collector](pss.c). The AVD scope includes QEMU and descendants. The ReDroid scope includes all guest-visible processes, sampled with the collector inside the disposable container. The collector verifies process identities and retries incomplete sweeps. Shared ADB, Docker, Metro, mock API, filesystem cache outside the mappings, and host-side monitoring are outside the measured footprint. These are sampled endpoints.

[Summary and per-trial values](summary.json) · [Benchmark protocol](plan.json)

The per-arm compressed JSON files retain the timing receipts, raw process counters, and incomplete memory reads. Run `python3 audit.py .` from this directory to recompute the pooled statistics. The [cleanup receipt](cleanup.json) records the restored AVD settings and disposable guest removal.
