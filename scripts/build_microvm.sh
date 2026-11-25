#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1

readonly KERNEL_VERSION="6.12.117"
readonly KERNEL_URL="https://www.kernel.org/pub/linux/kernel/v6.x/linux-${KERNEL_VERSION}.tar.xz"
readonly KERNEL_CHECKSUMS_URL="https://www.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc"
readonly KERNEL_SHA256="74e99aa5e60e242e0a165fed8cb2ea0d054bf7a401f01861d403d75733456613"
readonly ALPINE_VERSION="3.22.8"
readonly ALPINE_URL="https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/aarch64/alpine-minirootfs-${ALPINE_VERSION}-aarch64.tar.gz"
readonly ALPINE_CHECKSUM_URL="${ALPINE_URL}.sha256"
readonly ALPINE_SHA256="36091efacd083c1cf5a8c837272530bff4fc72db1fbfdef559f52de0d890bca4"
readonly REDROID_INDEX="sha256:b51bde9cef80f7bd7581148192f2b2f4d41f23c6344cfe88eceeb8ddd67490ee"
readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

OUTPUT_DIR=""
WORK_DIR=""
SOURCE_SHA="${SOURCE_SHA:-${GITHUB_SHA:-}}"
JOBS="${BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

usage() {
  cat <<'EOF'
Usage: scripts/build_microvm.sh --output-dir DIR [--work-dir DIR] [--source-sha SHA]

Builds Linux 6.12.117 and an Alpine 3.22.8 ARM64 ext4 guest template. Requires
a clean Linux aarch64 checkout, root privileges for chroot/mount, and the usual
kernel build tools, Docker-free Alpine APK access, xz, tar, and e2fsprogs.
This builds guest artifacts only; it does not boot a guest or validate Android.
EOF
}

while (($#)); do
  case "$1" in
    --output-dir) OUTPUT_DIR="${2:?missing path}"; shift 2 ;;
    --work-dir) WORK_DIR="${2:?missing path}"; shift 2 ;;
    --source-sha) SOURCE_SHA="${2:?missing SHA}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$OUTPUT_DIR" ]] || { echo "--output-dir is required" >&2; exit 2; }
[[ "$(uname -s)" == Linux ]] || { echo "guest build requires Linux; macOS is not supported" >&2; exit 1; }
case "$(uname -m)" in aarch64|arm64) ;; *) echo "native aarch64 Linux builder required; got $(uname -m)" >&2; exit 1 ;; esac
[[ $EUID -eq 0 ]] || { echo "guest build requires root for chroot and ext4 population" >&2; exit 1; }
[[ "$SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "source SHA must be a real 40-character lowercase Git commit" >&2; exit 1; }
[[ "$(git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" rev-parse HEAD)" == "$SOURCE_SHA" ]] || {
  echo "source SHA does not match the checked out commit" >&2; exit 1;
}
if [[ -n "$(git -c safe.directory="$REPO_ROOT" -C "$REPO_ROOT" status --porcelain --untracked-files=all)" ]]; then
  echo "refusing provenance for a dirty source checkout" >&2; exit 1
fi
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo "BUILD_JOBS must be positive" >&2; exit 2; }
[[ -f "$REPO_ROOT/runtime/microvm/packages.lock.json" ]] || {
  echo "missing reviewed packages.lock.json; run the Authenticated guest package proposal workflow, review its artifact, and commit the lock before building" >&2
  exit 1
}

for tool in curl sha256sum tar xz make gcc g++ bc bison flex perl openssl rsync python3 git chroot mount mountpoint umount mke2fs; do
  command -v "$tool" >/dev/null || { echo "required build tool is missing: $tool" >&2; exit 1; }
done

OUTPUT_DIR="$(mkdir -p "$OUTPUT_DIR" && cd "$OUTPUT_DIR" && pwd -P)"
if [[ -z "$WORK_DIR" ]]; then WORK_DIR="$(mktemp -d /tmp/ashnazg-microvm-build.XXXXXX)"; fi
mkdir -p "$WORK_DIR" "$OUTPUT_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd -P)"
case "$WORK_DIR" in
  /tmp/ashnazg-microvm-*) ;;
  *) echo "--work-dir must be a dedicated /tmp/ashnazg-microvm-* build directory" >&2; exit 1 ;;
esac
[[ ! -L "$WORK_DIR/.ashnazg-build-owner" ]] || { echo "refusing symlinked build marker" >&2; exit 1; }
if [[ -f "$WORK_DIR/.ashnazg-build-owner" ]]; then
  [[ "$(cat "$WORK_DIR/.ashnazg-build-owner")" == ashnazg-microvm-build-v1 ]] || { echo "invalid build ownership marker" >&2; exit 1; }
else
  for cached in "$WORK_DIR"/* "$WORK_DIR"/.[!.]*; do
    [[ -e "$cached" || -L "$cached" ]] || continue
    [[ ! -L "$cached" ]] || { echo "refusing symlinked build cache" >&2; exit 1; }
    case "$(basename "$cached")" in
      linux-6.12.117|kernel-build|linux-6.12.117.tar.xz|alpine-minirootfs-3.22.8-aarch64.tar.gz) ;;
      *) echo "refusing non-empty unowned build directory: $WORK_DIR" >&2; exit 1 ;;
    esac
  done
  (set -o noclobber; printf 'ashnazg-microvm-build-v1\n' > "$WORK_DIR/.ashnazg-build-owner")
fi
for child in rootfs assets kernel-build "linux-$KERNEL_VERSION"; do
  [[ ! -L "$WORK_DIR/$child" ]] || { echo "refusing symlinked build child: $child" >&2; exit 1; }
done
readonly KERNEL_ARCHIVE="$WORK_DIR/linux-${KERNEL_VERSION}.tar.xz"
readonly ALPINE_ARCHIVE="$WORK_DIR/alpine-minirootfs-${ALPINE_VERSION}-aarch64.tar.gz"
readonly KERNEL_SOURCE="$WORK_DIR/linux-${KERNEL_VERSION}"
readonly KERNEL_BUILD="$WORK_DIR/kernel-build"
readonly ROOTFS="$WORK_DIR/rootfs"
readonly RAW_ASSETS="$WORK_DIR/assets"
readonly CONFIG_FRAGMENT="$REPO_ROOT/runtime/microvm/kernel.config"

cleanup_mounts() {
  for target in "$ROOTFS/dev/pts" "$ROOTFS/dev" "$ROOTFS/proc" "$ROOTFS/sys"; do
    mountpoint -q "$target" && umount -lf "$target" || true
  done
}
trap cleanup_mounts EXIT

fetch_checked() {
  local url="$1" destination="$2" expected="$3"
  if [[ ! -f "$destination" ]] || [[ "$(sha256sum "$destination" | cut -d' ' -f1)" != "$expected" ]]; then
    rm -f "$destination"
    curl --fail --location --retry 3 --retry-all-errors --output "$destination" "$url"
  fi
  printf '%s  %s\n' "$expected" "$destination" | sha256sum --check --status || {
    echo "upstream checksum mismatch for $url" >&2; rm -f "$destination"; exit 1;
  }
}

fetch_checked "$KERNEL_URL" "$KERNEL_ARCHIVE" "$KERNEL_SHA256"
fetch_checked "$ALPINE_URL" "$ALPINE_ARCHIVE" "$ALPINE_SHA256"
kernel_metadata_sha="$(curl --fail --location --retry 3 --retry-all-errors --silent "$KERNEL_CHECKSUMS_URL" | awk -v file="linux-${KERNEL_VERSION}.tar.xz" '$2 == file { print $1 }')"
alpine_metadata_sha="$(curl --fail --location --retry 3 --retry-all-errors --silent "$ALPINE_CHECKSUM_URL" | awk '{ print $1; exit }')"
[[ "$kernel_metadata_sha" == "$KERNEL_SHA256" ]] || { echo "kernel.org checksum metadata does not match the pinned source digest" >&2; exit 1; }
[[ "$alpine_metadata_sha" == "$ALPINE_SHA256" ]] || { echo "Alpine checksum metadata does not match the pinned minirootfs digest" >&2; exit 1; }
# Neither source nor executable make output is trusted from a restored cache.
# Reconstruct both from the checksum-verified archive for every build.
rm -rf "$KERNEL_SOURCE" "$KERNEL_BUILD"
tar -xJf "$KERNEL_ARCHIVE" -C "$WORK_DIR"
mkdir -p "$KERNEL_BUILD"
make -C "$KERNEL_SOURCE" O="$KERNEL_BUILD" ARCH=arm64 defconfig
"$KERNEL_SOURCE/scripts/kconfig/merge_config.sh" -m -O "$KERNEL_BUILD" \
  "$KERNEL_BUILD/.config" "$CONFIG_FRAGMENT"
make -C "$KERNEL_SOURCE" O="$KERNEL_BUILD" ARCH=arm64 olddefconfig
python3 "$REPO_ROOT/runtime/microvm/kernel_config.py" \
  --fragment "$CONFIG_FRAGMENT" --generated "$KERNEL_BUILD/.config"
[[ "$(make -s -C "$KERNEL_SOURCE" O="$KERNEL_BUILD" ARCH=arm64 kernelversion)" == "$KERNEL_VERSION" ]] || {
  echo "kernel source version is not $KERNEL_VERSION" >&2; exit 1;
}
make -C "$KERNEL_SOURCE" O="$KERNEL_BUILD" ARCH=arm64 -j"$JOBS" Image

mkdir -p "$RAW_ASSETS"
install -m 0644 "$KERNEL_BUILD/arch/arm64/boot/Image" "$RAW_ASSETS/kernel"
rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"
tar -xzf "$ALPINE_ARCHIVE" -C "$ROOTFS"
# No ambient repository resolution is permitted for a qualified build.
: > "$ROOTFS/etc/apk/repositories"
rm -f "$ROOTFS/etc/resolv.conf"
cp -L /etc/resolv.conf "$ROOTFS/etc/resolv.conf"
mkdir -p "$ROOTFS/dev" "$ROOTFS/proc" "$ROOTFS/sys" "$ROOTFS/dev/pts"
mount --bind /dev "$ROOTFS/dev"
mount -t proc proc "$ROOTFS/proc"
mount -t sysfs sysfs "$ROOTFS/sys"
mount --bind /dev/pts "$ROOTFS/dev/pts"
mkdir -p "$ROOTFS/tmp/ashnazg-packages"
python3 "$REPO_ROOT/scripts/guest_packages.py" fetch \
  --lock "$REPO_ROOT/runtime/microvm/packages.lock.json" --output-dir "$ROOTFS/tmp/ashnazg-packages"
locked_apks=()
for apk in "$ROOTFS/tmp/ashnazg-packages"/*.apk; do locked_apks+=("/tmp/ashnazg-packages/$(basename "$apk")"); done
chroot "$ROOTFS" /sbin/apk --no-network add "${locked_apks[@]}"
chroot "$ROOTFS" /sbin/apk info --installed android-tools ca-certificates docker docker-cli-buildx iproute2 iptables python3 util-linux
cp "$REPO_ROOT/runtime/microvm/packages.lock.json" "$RAW_ASSETS/packages.lock.json"
rm -rf "$ROOTFS/tmp/ashnazg-packages"
rm -f "$ROOTFS/etc/resolv.conf"
: > "$ROOTFS/etc/resolv.conf"
install -m 0755 "$REPO_ROOT/runtime/microvm/guest-init.py" "$ROOTFS/sbin/ashnazg-init"
mkdir -p "$ROOTFS/usr/local/bin" "$ROOTFS/etc/profile.d" "$ROOTFS/var/log" "$ROOTFS/usr/share/licenses/ashnazg"
install -m 0644 "$REPO_ROOT/LICENSE" "$ROOTFS/usr/share/licenses/ashnazg/LICENSE"
install -m 0755 "$REPO_ROOT/runtime/microvm/guest_bridge.py" "$ROOTFS/usr/local/bin/guest_bridge.py"
mkdir -p "$ROOTFS/usr/local/lib/ashnazg/scripts" "$ROOTFS/var/lib/ashnazg/redroid" "$ROOTFS/run" "$ROOTFS/dev/binderfs"
for script in redroid.py ci_emulator.py linux_binder.py session_data.py; do
  install -m 0644 "$REPO_ROOT/scripts/$script" "$ROOTFS/usr/local/lib/ashnazg/scripts/$script"
done
mkdir -p "$ROOTFS/usr/local/lib/ashnazg/runtime/microvm"
for module in apk_stream agent_session agent_backend control_dispatch adb_agent_stream; do
  install -m 0644 "$REPO_ROOT/runtime/microvm/$module.py" "$ROOTFS/usr/local/lib/ashnazg/runtime/microvm/$module.py"
done
# Verify imports and standalone guest entrypoints with the actual guest interpreter, without starting ADB or a VM.
chroot "$ROOTFS" /usr/bin/env PYTHONPATH=/usr/local/lib/ashnazg PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -c 'from scripts import linux_binder, redroid, ci_emulator, session_data; from runtime.microvm import apk_stream, agent_session, agent_backend, control_dispatch, adb_agent_stream'
chroot "$ROOTFS" /usr/bin/env PYTHONPATH=/usr/local/lib/ashnazg PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 /usr/local/lib/ashnazg/scripts/redroid.py --help >/dev/null
chroot "$ROOTFS" /usr/bin/env PYTHONPATH=/usr/local/lib/ashnazg PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 /usr/local/bin/guest_bridge.py --help >/dev/null

cat > "$ROOTFS/etc/profile.d/ashnazg.sh" <<'EOF'
export PYTHONPATH=/usr/local/lib/ashnazg
EOF
cat > "$ROOTFS/etc/issue" <<'EOF'
Ashnazg Linux 6.12.117 / Alpine 3.22.8 ARM64 guest\n
EOF
cleanup_mounts

truncate -s 4G "$RAW_ASSETS/rootfs.ext4"
mke2fs -q -t ext4 -F -m 0 -d "$ROOTFS" "$RAW_ASSETS/rootfs.ext4"
cp "$REPO_ROOT/runtime/microvm/NOTICES.md" "$RAW_ASSETS/NOTICES.md"
cp "$CONFIG_FRAGMENT" "$RAW_ASSETS/kernel.config"
cp "$KERNEL_BUILD/.config" "$RAW_ASSETS/kernel.generated.config"
cp "$KERNEL_ARCHIVE" "$RAW_ASSETS/linux-${KERNEL_VERSION}.tar.xz"

python3 - "$RAW_ASSETS" "$SOURCE_SHA" "$KERNEL_URL" "$KERNEL_SHA256" "$KERNEL_CHECKSUMS_URL" \
  "$ALPINE_URL" "$ALPINE_SHA256" "$ALPINE_CHECKSUM_URL" "$REDROID_INDEX" \
  "$KERNEL_SOURCE" "$KERNEL_BUILD" "$ROOTFS" \
  "$REPO_ROOT/runtime/microvm/guest-init.py" "$REPO_ROOT/runtime/microvm/guest_bridge.py" \
  "$REPO_ROOT/scripts/redroid.py" "$REPO_ROOT/scripts/ci_emulator.py" \
  "$REPO_ROOT" "$ALPINE_ARCHIVE" <<'PYTHON'
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

assets = Path(sys.argv[1])
source_sha, kernel_url, kernel_sha, kernel_checksums_url = sys.argv[2:6]
alpine_url, alpine_sha, alpine_checksum_url, redroid_index = sys.argv[6:10]
kernel_source, kernel_build, rootfs = map(Path, sys.argv[10:13])
guest_init, guest_bridge, redroid_supervisor, readiness_parser = map(Path, sys.argv[13:17])
repo_root, alpine_archive = map(Path, sys.argv[17:19])
sys.path.insert(0, str(repo_root))
from scripts.runtime_inventory import build_inventory

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

files = {name: {"sha256": sha256(assets / name), "size": (assets / name).stat().st_size}
         for name in ("kernel", "rootfs.ext4")}
# Read back from the composed image, then compare with the source tree. debugfs
# is read-only without -w; an extraction error also fails the equality/parser checks.
installed_db = subprocess.run(
    ["debugfs", "-R", "cat /lib/apk/db/installed", str(assets / "rootfs.ext4")],
    check=True, capture_output=True,
).stdout
if installed_db != (rootfs / "lib/apk/db/installed").read_bytes():
    raise RuntimeError("composed rootfs package database differs from build tree")
inventory = build_inventory(
    installed_db=installed_db, base_archive=alpine_archive.read_bytes(),
    package_lock=(assets / "packages.lock.json").read_bytes(),
    rootfs_sha256=files["rootfs.ext4"]["sha256"], source_sha=source_sha,
)
if inventory["metadata_status"] != "complete":
    raise RuntimeError("rootfs component metadata is incomplete: " + json.dumps(inventory["issues"], sort_keys=True))
(assets / "runtime-installed.db").write_bytes(installed_db)
(assets / "runtime-components.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
component_inventory = {
    "name": "runtime-components.json", "sha256": sha256(assets / "runtime-components.json"),
    "installed_db_name": "runtime-installed.db", "installed_db_sha256": sha256(assets / "runtime-installed.db"),
    "metadata_status": inventory["metadata_status"],
    "source_closure_status": inventory["source_closure_status"],
}
apk_versions = subprocess.run(
    ["chroot", str(rootfs), "/sbin/apk", "info", "-v"],
    check=True, text=True, capture_output=True,
).stdout.splitlines()
apk_indexes = sorted(Path(rootfs / "var/cache/apk").glob("*.tar.gz"))
provenance = {
    "schema_version": 1,
    "source_sha": source_sha,
    "builder": {"os": platform.platform(), "architecture": platform.machine(),
                 "compiler": subprocess.run(["gcc", "--version"], check=True, text=True,
                                             capture_output=True).stdout.splitlines()[0]},
    "upstreams": {
        "kernel": {"version": "6.12.117", "url": kernel_url, "sha256": kernel_sha,
                   "sha256_metadata_url": kernel_checksums_url,
                   "archive_verified": True,
                   "source_bundle_sha256": sha256(assets / "linux-6.12.117.tar.xz")},
        "alpine_minirootfs": {"version": "3.22.8", "url": alpine_url, "sha256": alpine_sha,
                              "sha256_metadata_url": alpine_checksum_url,
                              "archive_verified": True},
        "redroid_oci_index": redroid_index,
    },
    "kernel_config_fragment_sha256": sha256(assets / "kernel.config"),
    "kernel_config_sha256": sha256(kernel_build / ".config"),
    "kernel_config": (kernel_build / ".config").read_text(),
    "guest_code_sha256": {
        "pid1": sha256(guest_init), "vsock_bridge": sha256(guest_bridge),
        "redroid_supervisor": sha256(redroid_supervisor),
        "readiness_parser": sha256(readiness_parser),
        "linux_binder": sha256(repo_root / "scripts/linux_binder.py"),
        "session_data": sha256(repo_root / "scripts/session_data.py"),
        **{name: sha256(repo_root / "runtime/microvm" / (name + ".py"))
           for name in ("apk_stream", "agent_session", "agent_backend", "control_dispatch", "adb_agent_stream")},
    },
    "package_lock_sha256": sha256(assets / "packages.lock.json"),
    "apk_indexes": [{"name": item.name, "sha256": sha256(item)} for item in apk_indexes],
    "packages": sorted(apk_versions),
    "component_inventory": component_inventory,
    "files": files,
    "runtime_boot_validated": False,
}
(assets / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
manifest = {
    "schema_version": 1,
    "version": "0.3.2",
    "source_sha": source_sha,
    "architecture": "aarch64",
    "files": files,
    "guest": {"kernel_version": "6.12.117", "base": "alpine-3.22.8-aarch64",
              "redroid_index": redroid_index},
}
(assets / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PYTHON

python3 - "$REPO_ROOT" "$RAW_ASSETS" <<'PYTHON'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from runtime.microvm.manifest import validate_manifest
validate_manifest(Path(sys.argv[2]), expected_architecture="aarch64")
PYTHON

tar -C "$RAW_ASSETS" -cJf "$OUTPUT_DIR/microvm-assets.tar.xz" \
  kernel rootfs.ext4 manifest.json provenance.json kernel.config kernel.generated.config \
  NOTICES.md packages.lock.json runtime-components.json runtime-installed.db linux-${KERNEL_VERSION}.tar.xz
(cd "$OUTPUT_DIR" && sha256sum microvm-assets.tar.xz > microvm-assets.tar.xz.sha256)
printf 'Built guest artifact bundle: %s/microvm-assets.tar.xz\n' "$OUTPUT_DIR"
printf 'Build output is not runtime boot validation.\n'
