# Native MCP restart evidence

The host library source is `da745f5c3ff474eb0ef3d4b02d3f7a3981604f38`.
The explicit test probe was built from a clean checkout and embeds that source
identity. Its runtime registry package identities/checksums match the host lock.
The existing guest bundle and ad-hoc-signed helper were reused; preflight records
bind the guest kernel/rootfs, helper and agent APK hashes. This is not a new guest
build or release acquisition.

`restart-normal` and `restart-sigterm` each passed 19 checks through 30 MCP tool
calls over authenticated loopback HTTP. Three framework fixtures were installed
and launched using inferred or explicit packages, exercised with tap and Unicode
input/clear, and captured as PNGs with semantic marks. No-launch installation,
invalid requests, navigation and logs were also checked. The environment receipt
records no host ADB/aapt on PATH and no guest token/serial environment settings.

Normal cancellation and SIGTERM to the acceptance controller both joined helper
shutdown, reaped the helper, removed credentials and closed the three helper
listeners plus HTTP. SIGTERM occurred after the request sequence while the server
remained active. In-flight cancellation, generic-I/O EOF and panic recovery remain
fixture tests. State and disk filesystem identities were preserved; disk bytes
are expected to change.

The helper ready intervals were 39.770 and 40.522 seconds. Controller totals were
49.824 and 49.509 seconds including checks and shutdown. These are two restarts,
not a cold-start distribution, benchmark or CPU result.

`initial-client-failure` retains an earlier attempt with zero completed checks and
clean shutdown. The local HTTP diagnostic captured an empty SSE retry event before
the JSON response. The acceptance client's decoder initially rejected that event;
its regression failed before the repair and passed afterward. The two repeats
used the corrected client without changing host or guest code. The local
transport diagnostic used the compatibility HTTP server, not another VM run.

Each run retains the driver, the exact client bytes it verifies and compiles,
probe source/lock/build receipt, protocol results, runtime receipts and serial
logs. Per-run receipts bind their payloads; the top receipt covers the complete
retained set. No credentials or private run configurations are included. The
bound driver reports scanning its three live credential values, but these files
cannot independently attest those secret values or the external runtime
environment. Actual stdio/CLI interruption, automatic setup, live acquisition,
distribution and performance remain outside this evidence.
