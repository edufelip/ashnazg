# Guest artifact notices

The Linux kernel is built from the unmodified Linux 6.12.117 source archive
published by kernel.org and is licensed under GPL-2.0-only. The exact upstream
source archive, build configuration, build provenance, and this notice are
included in each generated artifact bundle. No source changes are applied by
the build script.

The guest root filesystem starts with Alpine Linux 3.22.8 aarch64 minirootfs
and installs packages from the signed Alpine v3.22 repositories. Package names
and exact resolved versions are recorded in `provenance.json`. Alpine package
license and copyright files remain in `/usr/share/licenses` when supplied by
the package. The Alpine release source archive and its SHA256 are recorded in
`provenance.json`.

The Android guest image is `redroid/redroid` pinned by the immutable OCI index
digest `sha256:81889852444606824d33eb0232fa5813bce3f0993afe871785120af0d7c991ad`.
It is downloaded at guest first boot and is not redistributed in this bundle.
