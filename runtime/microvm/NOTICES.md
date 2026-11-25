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

The Android guest uses the Android 15 64-bit-only `redroid/redroid` profile, pinned by the immutable OCI index
digest `sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee`.
It is downloaded at guest first boot and is not redistributed in this bundle.

The matching 0.3.2 release candidate includes a separate
`ashnazg-runtime-sources-v0.3.2-001.tar.xz` source and notice sidecar. Its manifest
binds the actual guest image and installed package database to the exact package
sources, pinned Alpine packaging recipes, original notice evidence, project
sources and locked Rust dependency sources. The runtime catalog records the
sidecar's size and SHA-256. Refer to the same source SHA when obtaining it;
historical collection hashes do not identify a newly built image.

Project runtime scripts and the macOS helper are covered by the following
project license, also installed at `/usr/share/licenses/ashnazg/LICENSE`:

MIT License

Copyright (c) 2026 Ashnazg contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
