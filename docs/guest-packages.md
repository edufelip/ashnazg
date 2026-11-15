# Authenticated guest package snapshot

The guest package set is resolved on native Linux ARM64 in the
`Authenticated guest package proposal` workflow. It starts from Alpine 3.22.8's
minirootfs with its shipped APK signing keys, checks the pinned archive SHA-256,
and asks Alpine's native `apk update` to authenticate the official v3.22
repository indexes. It uses `apk fetch --recursive --url` to list sources and a separate recursive fetch to download the eight
requested roots and their complete dependency closure, and performs an offline
`apk add --no-network` into the pinned base root with an empty repository file. That
native install checks package signatures and dependency satisfiability.

The workflow uploads a proposal `packages.lock.json`, a short summary, and
`index-proof.json` containing the pinned base source/hash and each authenticated
index source, filename, and SHA-256. A passing workflow creates an authenticated
package snapshot proposal only. It does not build a kernel, create a bootable
guest, or claim boot validation. The generated proposal is an artifact and does
not edit the repository or claim to be a reviewed lock.

## Use a reviewed lock

After maintainers review and add the generated lock to the repository, fetch its
exact APK bytes into a fresh directory and verify them later with:

```sh
python3 scripts/guest_packages.py fetch \
  --lock packages.lock.json \
  --output-dir /tmp/guest-apks

python3 scripts/guest_packages.py verify \
  --lock packages.lock.json \
  --directory /tmp/guest-apks
```

The lock records the actual package name, version, architecture, official
download URL, filename, byte size, and SHA-256 taken from the fetched APK and
its `.PKGINFO` control stream. `fetch` accepts only official HTTPS Alpine v3.22
ARM64 package URLs, refuses redirects, checks the exact size/hash and package
identity, and writes only into an empty directory. `verify` rejects missing,
extra, nested, symlinked, or modified APK files. Neither operation trusts
arbitrary package URLs or supports `--allow-untrusted`.

The lock contains filenames and hashes rather than APK payloads, so normal
source control need not redistribute the APK closure. Consumers download the
same bytes from their recorded official immutable package filenames and verify
them against the reviewed lock before use.

## Bootstrap boundary

`bootstrap --output-dir DIR` is intentionally limited to root on native Linux
aarch64 while `GITHUB_ACTIONS=true`. It owns a fresh temporary root filesystem
through Python's temporary-directory lifecycle; it does not remove a caller
selected path. On macOS, use the portable `fetch` and `verify` commands against
a reviewed lock. Do not infer APKINDEX authentication from an unauthenticated
macOS package-manager run.
