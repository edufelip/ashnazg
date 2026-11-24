# Packaged CLI and VM acceptance — v0.3.1 candidate REJECTED

**Verdict: `accepted == false`. The v0.3.1 release candidate (prepared run
[45323159437](https://github.com/edufelip/ashnazg/actions/runs/45323159437), source
`1f4f4d1749b13921dce937d3b25dd93efa5092ae`) must not be tagged, published, or released.**
All three prepublication modes (`eof`, `sigterm`, `sigterm-inflight`) failed identically:
the native CLI started, negotiated MCP `initialize` and `tools/list`, launched its helper,
and then the virtual machine stopped before guest readiness. No mode reached
`app_install`, so no mode produced a session credential, tool call, screenshot, or
shutdown observation; `005-app_install.request.json` therefore does not exist for any
mode in this directory.

## Candidate intake verification (passed)

The artifact `release-prepared-v0.3.1-1f4f4d1749b13921dce937d3b25dd93efa5092ae`
(id `14890215083`, zip SHA-256 `ae9c91583eedacd07fa7e89de75a59989daf731d6fee778b8488ab64ed4758d9`,
matching the GitHub-reported digest after a fresh download) contains the expected 10
files. `shasum -a 256 -c SHA256SUMS` passed for every member, and
`python3 scripts/runtime_release.py verify --directory <candidate> --source-sha 1f4f4d1749b13921dce937d3b25dd93efa5092ae --version 0.3.1`
exited 0 against the trusted script at the source commit
(`scripts/runtime_release.py` SHA-256 `2213fb2db75de71a9b3891655f531150f16dd2a34fd7d8c91bd04820bd6c0ca9`).
See [prepared-artifact.json](prepared-artifact.json).

The macOS package `ashnazg-v0.3.1-macos-aarch64.zip` verified against its own
`SHA256SUMS`. The packaged `bin/ashnazg-vm` carries an ad-hoc signature
(`codesign --verify --strict` passed) with the `com.apple.security.virtualization`
entitlement set to true, and both packaged binaries link only Apple system libraries
(see [local-preflight.json](local-preflight.json)). The five packaged `platform/ios/`
sources match the source commit `1f4f4d1749b13921dce937d3b25dd93efa5092ae` byte for byte
(5/5). The candidate was staged offline into a private catalog-verified cache
([staging-receipt.json](staging-receipt.json), catalog SHA-256
`453c753406a4c3bcaf0fea6797acea3839c8b859ad869dc697b0a6236fa2a16a`), and
[guest/manifest.json](guest/manifest.json) binds the booted rootfs
(template SHA-256 `37ee21e3123f43306194f09cc69ef3c7be691087f12cba7d451cd4658523d943`)
to the candidate manifest, proving the failure below comes from the candidate's own
bytes and not a staging error.

## Why every mode failed

Each mode's `runtime.json` records `"status": "failed"` with
`"error": "virtual machine stopped before guest readiness"` roughly 6–7.5 seconds after
helper launch. The guest serial console shows the kernel booting, DHCP completing, and
then the guest's own services crashing at import time:

```
Traceback (most recent call last):
  File "/usr/local/lib/ashnazg/scripts/redroid.py", line 27, in <module>
    from session_data import SessionData
ModuleNotFoundError: No module named 'session_data'
Ashnazg guest services exited with status 1; powering off
```

Root cause: commit `d1f69c7a501456f58f091bd79ab7b471de082683` ("Ship owned Linux
development sessions and private Binderfs Action input") added `scripts/session_data.py`
and made `scripts/redroid.py` import it, but `scripts/build_microvm.sh` still installs
only `redroid.py`, `ci_emulator.py`, and `linux_binder.py` into the guest rootfs
(`/usr/local/lib/ashnazg/scripts/`). The v0.3.1 guest image was therefore built without
`session_data.py`, and the guest cannot reach readiness. This is a candidate defect that
the guest build job cannot catch because it never boots the VM; it only surfaces in this
native acceptance. A corrected candidate needs `session_data.py` added to the guest
installs, a rebuilt guest image, and a re-prepared candidate — none of which acceptance
may do to the bytes under test.

## iOS fixture verification (passed, independent of the microvm)

`platform/ios/verify_mcp_fixture.py` (run with `env -u ASHNAZG_IOS_FIXTURE_SOURCE` from
the clean worktree at the source commit; running from `/tmp` cannot work because the
verifier resolves `source_sha` with `git rev-parse HEAD` in the working directory) drove
the unchanged packaged `bin/ashnazg mcp --ios-fixture` to a passing receipt: all three
iOS-mode tools verified, tap, append typing, secure-field redaction of the synthetic
sentinel, clean EOF exit 0, `work_dir_removed == true`, `simulator_deleted == true`
([ios-fixture/receipt.json](ios-fixture/receipt.json)). This path never boots the
Android microvm, so it is unaffected by the rootfs defect.

## Secret scan

The failed runs never created a session credential file
(`.ashnazg-session-v1.json`), so no live `rpc_token`/`install_token` values existed to
leak; the driver still scanned every retained byte of each mode's outputs, its own
stdout/stderr captures, and the rewritten `runtime.json`/`serial.log` diagnostics, and
found no matching secret. The iOS verifier's synthetic fixture sentinel is confirmed
absent from its retained receipt. No unredacted secret is committed here, and no binary
artifact, screenshot, or serial log is committed.

## Publish gate

`.github/workflows/release.yml` (lines 361–367) requires `schema_version == 1`,
`accepted is True`, the matching `source_sha`, `prepared_run_id == 45323159437`,
`candidate_sha256 == a9442df9f1c69cd1456b16a5fef6c3b6d2915d48cd8ea22a20acfc1efb8b52ed`
(the SHA-256 of `candidate.json`), and `passed_modes ⊇ {eof, sigterm, sigterm-inflight}`.
This receipt satisfies the schema/version/source/run/candidate checks and deliberately
fails the `accepted` and `passed_modes` checks, so publication stays blocked. The
`runs` entry for each mode records `passed: false` with the error strings; the
`sigterm-inflight` run never reached an in-flight request, so its `inflight` observation
is null rather than the v0.3.0-style pending-reply claim.
