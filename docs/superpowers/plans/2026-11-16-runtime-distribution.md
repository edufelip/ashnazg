# Runtime Distribution and Real CLI Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Publish a matching experimental 0.3.0 runtime and demonstrate the production CLI acquiring and operating its owned Android VM.

**Architecture:** Prepare one immutable artifact set from a full main source SHA. Bind the stamped host, guest manifest, signed helper, APKs, source/notice sidecar and catalog to that SHA. Qualify those exact bytes locally, publish the same candidate bytes, then repeat production acquisition from the public GitHub release.

**Tech Stack:** Rust, Swift Virtualization.framework, Python 3, GitHub Actions/Releases, Linux ARM64 and Alpine package/source metadata.

## Parallel ownership and integration rules

- Release packaging lane owns `scripts/package_release.py`, its tests, and version coordination in Cargo files. Root owns workflow wiring and release documentation.
- Source lane owns a deterministic source sidecar producer, tests, and checked input lock. It must account for every actual installed origin/commit pair, inherited packages, source archives, recipes, notices, project code and kernel source. It must refuse inventory mismatch and missing or changed inputs.
- Acceptance lane owns a CLI acceptance driver and driver tests. It must use the actual production executable and retain request/result, readiness and cleanup evidence without retaining secrets.
- Root integrates lane commits, reviews boundaries, builds matching candidates, owns the VM, merges green changes and publishes. Only root may launch a VM, dispatch hosted builds, tag or publish; delegates use fixtures and read-only research.

## Release candidate contract

- [x] Set Cargo and package version to `0.3.0`; preserve the runtime manifest's existing `0.3.0` contract.
- [x] Package the macOS ARM64 helper with Virtualization.framework entitlements and an ad-hoc signature verified after signing. Describe Developer ID/notarization availability accurately.
- [x] Prepare guest, helper, agent and fixture with the catalog's exact role names. Record full source SHA, byte sizes and SHA-256 values using `scripts/runtime_catalog.py`.
- [x] Publish the complete source/notice sidecar as `ashnazg-runtime-sources-v0.3.0-001.tar.xz`; include its digest in the catalog and external download checksums.
- [x] Preserve existing read-only PR validation and exact-source Quality/main/tag publication checks. Separate candidate preparation from publication so accepted bytes are reused.

## Source sidecar

- [x] Normalize retained authenticated recipe/source evidence into checked URL, filename, size, SHA-256 and declared SHA-512 records without temporary paths.
- [x] Compare the prepared guest's `runtime-components.json` origin/commit groups with the lock before assembly; reject missing, extra or changed groups.
- [x] Include all exact source inputs, all recipe/support files, notice evidence and supplemental texts, package inventory, kernel configuration/source and project license/source. Retain original bytes and map them in a deterministic manifest.
- [x] Use bounded downloads and regular-file archive members with fixed metadata. Test missing/corrupt inputs, inventory mismatches, unsafe names and deterministic assembly.

## Real CLI acceptance

- [x] Build a production source-stamped host and source-matching helper/guest/APKs; verify upstream artifact receipts and every candidate hash before use.
- [x] Use an owned private HOME/cache/state. Stage a catalog-verified candidate cache for prepublication CLI acceptance without adding a production URL override or fabricating source identity.
- [x] Launch the actual `ashnazg mcp` process with no host ADB/aapt or external guest credentials. Require initialize/tools-list before readiness, then exercise all five tools, inferred install/launch, changed UI and decoded screenshot freshness.
- [x] Exercise normal EOF, idle SIGTERM and in-flight cancellation. Retain helper/bridge process, credential, listener and persistent-state cleanup evidence; never signal unowned processes or remove unowned paths.
- [x] Repeat warm startup and record acquisition, ready and shutdown intervals separately. Publish no performance improvement claim from these observations.
- [x] After publication, start with a fresh cache using the public catalog and assets, repeat the real CLI checks, and verify offline warm reuse separately.

## Verification and delivery

- [x] Run focused red/green lane tests, combined Rust/Python/Swift checks, actionlint, formatting and diff checks.
- [x] Obtain fresh independent reviews for packaging, source binding and acceptance evidence; repair concrete findings and rerun affected checks.
- [x] Open a draft PR, inspect exact-head CI and feedback, merge when all applicable checks pass, and prepare candidates from the chosen validated main SHA.
- [x] Retain acceptance receipts and artifact hashes, create the pinned release tag and publish the accepted bytes as an experimental prerelease.
- [x] Verify public asset URLs, hashes and real cold acquisition. Report actual platform/signing and runtime limitations alongside measured results.
