#!/usr/bin/env bash
set -Eeuo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUTPUT_DIR=""
NDK_DIR="${ANDROID_NDK_HOME:-${ANDROID_NDK_ROOT:-}}"
CMAKE_BIN="${CMAKE:-}"

usage() {
  cat <<'HELP'
Usage: scripts/build_hwc.sh --output-dir DIR [--ndk DIR] [--cmake PATH]

Cross-compiles the source-owned legacy HWC2 HAL for arm64-v8a and x86_64
using an already-installed Android NDK. It does not download or install tools,
modify an Android image, or boot a container/device. Outputs one
hwcomposer.default.so per ABI under DIR/<abi>/.
HELP
}

while (($#)); do
  case "$1" in
    --output-dir) OUTPUT_DIR="${2:?missing path}"; shift 2 ;;
    --ndk) NDK_DIR="${2:?missing NDK path}"; shift 2 ;;
    --cmake) CMAKE_BIN="${2:?missing CMake path}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ -n "$OUTPUT_DIR" ]] || { echo "--output-dir is required" >&2; exit 2; }

if [[ -z "$NDK_DIR" ]]; then
  for sdk in "${ANDROID_SDK_ROOT:-}" "${ANDROID_HOME:-}" "$HOME/Library/Android/sdk" /opt/android-sdk /opt/android-sdk-linux; do
    [[ -d "$sdk/ndk" ]] || continue
    NDK_DIR="$(find "$sdk/ndk" -mindepth 2 -maxdepth 2 -name source.properties -print 2>/dev/null | sed 's#/source.properties$##' | LC_ALL=C sort -r | head -n 1)"
    [[ -n "$NDK_DIR" ]] && break
  done
fi
[[ -n "$NDK_DIR" && -f "$NDK_DIR/build/cmake/android.toolchain.cmake" ]] || {
  echo "Android NDK not found; install nothing automatically and pass --ndk to an existing NDK" >&2; exit 1;
}

if [[ -z "$CMAKE_BIN" ]]; then CMAKE_BIN="$(command -v cmake || true)"; fi
if [[ -z "$CMAKE_BIN" ]]; then
  for sdk in "${ANDROID_SDK_ROOT:-}" "${ANDROID_HOME:-}" "$HOME/Library/Android/sdk" /opt/android-sdk /opt/android-sdk-linux; do
    [[ -d "$sdk/cmake" ]] || continue
    CMAKE_BIN="$(find "$sdk/cmake" -type f -path '*/bin/cmake' -print 2>/dev/null | LC_ALL=C sort -r | head -n 1)"
    [[ -n "$CMAKE_BIN" ]] && break
  done
fi
[[ -n "$CMAKE_BIN" && -x "$CMAKE_BIN" ]] || {
  echo "CMake not found; pass --cmake for an existing CMake binary" >&2; exit 1;
}

OUTPUT_DIR="$(mkdir -p "$OUTPUT_DIR" && cd "$OUTPUT_DIR" && pwd -P)"
for abi in arm64-v8a x86_64; do
  build_dir="$OUTPUT_DIR/.build-$abi"
  "$CMAKE_BIN" -S "$REPO_ROOT/runtime/hwc" -B "$build_dir" \
    -DCMAKE_TOOLCHAIN_FILE="$NDK_DIR/build/cmake/android.toolchain.cmake" \
    -DANDROID_NDK="$NDK_DIR" -DANDROID_ABI="$abi" -DANDROID_PLATFORM=android-28 \
    -DANDROID_STL=c++_static -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release
  "$CMAKE_BIN" --build "$build_dir" --target ashnazg_hwc_hal --parallel
  mkdir -p "$OUTPUT_DIR/$abi"
  install -m 0755 "$build_dir/hwcomposer.default.so" "$OUTPUT_DIR/$abi/hwcomposer.default.so"
done
