# Rootfs Component Inventory Implementation Plan

> **For agentic workers:** Use subagent-driven-development with isolated ownership. Root performs specification review and independent code review. No nested agents.

**Goal:** Produce an evidence-bound metadata inventory for every installed rootfs package, including inherited Alpine base packages.

**Architecture:** Parse the final installed APK database, the reviewed package lock, and the database from the checksum-verified base archive without executing package code. Preserve missing/unmapped metadata explicitly. This identifies source origins and build commits; fetching source archives and collecting license texts remain a separate closure stage.

**Tech Stack:** Python standard library, tarfile in read-only mode, deterministic JSON and unittest.

## Ownership and API

Create only `scripts/runtime_inventory.py` and `tests/test_runtime_inventory.py`.

```python
def build_inventory(*, installed_db: bytes, base_archive: bytes,
                    package_lock: bytes, rootfs_sha256: str,
                    source_sha: str) -> dict: ...
```

The CLI accepts `--installed-db`, `--base-archive`, `--package-lock`, `--rootfs-sha256`, and `--source-sha`. It reads bounded regular non-symlink input files, emits deterministic sorted JSON to stdout only, and returns nonzero for incomplete metadata/mapping while retaining the JSON evidence. It does not create directories, download anything, write publication files, invoke a shell, parse/evaluate APKBUILD, or claim corresponding-source completion.

## Inputs and parsing

- Bound final database and lock bytes to 2 MiB each and the compressed base archive to 20 MiB. Require valid lowercase source/rootfs digests.
- Validate lock schema 1, aarch64 platform, exact package identity/version/architecture and byte/hash fields, unique package names, and the base URL/hash metadata. Reuse the existing reviewed lock validator if it has no side effects; do not fetch anything.
- Require the base archive's SHA-256 to match the lock before opening it. Read only the unique regular member `lib/apk/db/installed` (an initial `./` is allowed), at most 2 MiB. Do not extract archive paths. Bound total archive inspection/decompression; reject duplicates, malformed/truncated archives and unsafe database members.
- APK database records are separated by blank lines. Package header fields precede the first `F:` directory/file section. Require unique `P`, `V`, and `A` identity fields. Reject duplicate identities and malformed package records. Preserve header metadata; file-list fields are not source metadata.
- Capture name (`P`), version (`V`), architecture (`A`), declared license (`L`), source origin (`o`), upstream URL (`U`), build time (`t`), and aports build commit (`c`). Missing or malformed source metadata must remain explicit, never synthesized from name/version. Keep raw header fields and input database hashes so a reviewer can trace the transformation.
- Treat a declared license string as metadata, not a license text or legal-clearance decision.

## Mapping and output

Output schema version 1 includes `source_sha`, `rootfs_sha256`, input hashes (`installed_db_sha256`, `base_archive_sha256`, `base_db_sha256`, `package_lock_sha256`), sorted `packages`, sorted `source_groups`, sorted `issues`, `metadata_status`, and `source_closure_status: "not_collected"`.

For each final installed package:

1. A matching name/version/architecture in the reviewed lock gets provenance kind `locked_apk`, with the lock's filename, size, SHA-256 and URL. A mismatching locked identity is an issue except for a same-version, different-architecture candidate whose installed identity and every raw package-header field exactly match the verified base record. In that case record provenance kind `base` and preserve the complete locked candidate with disposition `not_installed_base_retained`; never attribute the installed package to its downloaded candidate. Different versions or changed base metadata remain errors.
2. Otherwise, require an exact matching base package identity and captured source metadata before using provenance kind `base`, bound to the verified base archive digest.
3. A package found in neither matching input gets kind `unmapped` and an issue. Never assume an unlocked package came from the base.

Record locked packages absent from the final database as issues. Preserve explicit missing/invalid fields on package records. `metadata_status` is `complete` only when all final packages map, all locked identities are present or have an explicitly evidenced same-version retained-base disposition, and required source metadata is valid; otherwise it is `incomplete`. This status must never be named or interpreted as source/notice closure.

Group packages by the exact pair `(origin, aports_commit)` only when those fields are valid. Each group lists its actual binary package names/versions/architectures. Do not infer aports recipe paths, source URLs, patches, notice texts, source hashes, or archive availability yet.

The rootfs digest is a producer-supplied build binding. This parser does not inspect ext4 or independently prove that the supplied final database came from that rootfs; the builder must capture/bind those inputs from its actual build. Likewise, package source fields come from the final installed database; association with a lock entry checks identity and does not pretend to re-read its APK `.PKGINFO`.

## Tests and integration checkpoint

- [ ] Write unittest fixtures first for base archive hash/member/size refusal, valid base+lock union, lock upgrades over base, missing/changed/unmapped packages, malformed/duplicate database identities, file-list separation, incomplete source fields, strict lock fields/booleans, exact source grouping, deterministic output, and CLI exit/output behavior. Use small constructed tar fixtures; no real package downloads.
- [ ] Run `python3 -m unittest tests.test_runtime_inventory -v` before implementation and retain the expected failure.
- [ ] Implement with standard library only; do not modify the guest builder or existing package lock in this task.
- [ ] Re-run focused tests, existing package-lock tests, compilation and `git diff --check`; commit only the two owned files.
- [ ] Root compares the resulting inventory against the retained real `7a76029` rootfs and pinned base, then requests independent review before integrating it into the builder/release gate.

Root's preliminary read-only inspection found 142 final package records, 133 matching lock names/versions with one architecture mismatch, nine names absent from the lock, and 47 distinct origin/build-commit pairs. The verified minirootfs contains 16 package records; several are upgraded by locked packages. These are input observations to verify with the implementation, not hard-coded expected counts for future builds. The architecture mismatch is `busybox-binsh`: installed and base records are `aarch64`, while the verified locked APK metadata is `noarch` at the same version. This is not architecture equivalence. The builder removes its downloaded APK staging cache before composing the rootfs. No complete source archive or notice inventory has yet been assembled.
