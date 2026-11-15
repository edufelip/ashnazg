#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SOURCE_DIR="$REPO_ROOT/runtime/display-governor/probe/src"
readonly PLATFORM_API_BASE="cc8bb19595c661f5cf42f330457000e643c67d1b"
readonly SURFACE_CONTROL_SHA256="d245215beb63cf01e29bde720a1ed4b643a0e304afdc7f514559673eb2f98bce"
readonly DISPLAY_CONTROL_SHA256="66810531696a07fa11464c5c515e6727664ccbc50de7c5c688f52243e774c718"

usage() {
  cat <<'EOF'
Usage: scripts/build_display_probe.sh [--output PATH]

Builds the one-shot DisplayProbe DEX jar with the installed Android SDK platform 35
and build-tools 35.0.0. It never downloads or installs tools.
EOF
}

output_path="$REPO_ROOT/runtime/display-governor/probe/build/display-probe.jar"
while (($#)); do
  case "$1" in
    --help)
      usage
      exit 0
      ;;
    --output)
      if (($# < 2)) || [[ -z "$2" ]]; then
        echo "error: --output requires a path" >&2
        exit 2
      fi
      output_path="$2"
      shift 2
      ;;
    *)
      echo "error: unsupported argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -n "${ANDROID_HOME:-}" ]]; then
  sdk_root="$ANDROID_HOME"
elif [[ -n "${ANDROID_SDK_ROOT:-}" ]]; then
  sdk_root="$ANDROID_SDK_ROOT"
else
  sdk_root="$HOME/Library/Android/sdk"
fi

android_jar="$sdk_root/platforms/android-35/android.jar"
d8="$sdk_root/build-tools/35.0.0/d8"
if [[ ! -f "$android_jar" || ! -x "$d8" ]]; then
  echo "error: SDK platform 35 and build-tools 35.0.0 are required under $sdk_root" >&2
  exit 2
fi
javac_bin="$(command -v javac || true)"
jar_bin="$(command -v jar || true)"
if [[ -z "$javac_bin" || -z "$jar_bin" ]]; then
  echo "error: installed JDK with javac and jar is required" >&2
  exit 2
fi
if [[ ! -d "$SOURCE_DIR" || ! -f "$SOURCE_DIR/com/ashnazg/displayprobe/DisplayProbe.java" ]]; then
  echo "error: probe Java sources are missing" >&2
  exit 2
fi

temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/display-probe.XXXXXX")"
trap 'rm -rf "$temporary_dir"' EXIT
mkdir -p "$temporary_dir/classes" "$temporary_dir/dex"
sources=("$SOURCE_DIR"/com/ashnazg/displayprobe/*.java)
if ((${#sources[@]} == 0)); then
  echo "error: no probe Java sources found" >&2
  exit 2
fi

"$javac_bin" --release 8 -classpath "$android_jar" -d "$temporary_dir/classes" "${sources[@]}"
"$jar_bin" cf "$temporary_dir/program.jar" -C "$temporary_dir/classes" .
"$d8" --lib "$android_jar" --min-api 29 --output "$temporary_dir/dex" "$temporary_dir/program.jar"
(cd "$temporary_dir/dex" && "$jar_bin" cf "$temporary_dir/display-probe.jar" classes.dex)

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}
source_hashes="$temporary_dir/source-hashes.txt"
: > "$source_hashes"
for source in "${sources[@]}"; do
  printf '%s  %s\n' "$(hash_file "$source")" "${source#"$REPO_ROOT"/}" >> "$source_hashes"
done
source_sha256="$(hash_file "$source_hashes")"
if [[ "$output_path" == *.jar ]]; then
  provenance_path="${output_path%.jar}.provenance.txt"
else
  provenance_path="${output_path}.provenance.txt"
fi
mkdir -p "$(dirname "$output_path")"
cp "$temporary_dir/display-probe.jar" "$output_path"
{
  printf 'probe_source_sha256=%s\n' "$source_sha256"
  printf 'platform_api_source_base_commit=%s\n' "$PLATFORM_API_BASE"
  printf 'surfacecontrol_api_source_sha256=%s\n' "$SURFACE_CONTROL_SHA256"
  printf 'displaycontrol_api_source_sha256=%s\n' "$DISPLAY_CONTROL_SHA256"
  printf 'android_sdk_platform=35\n'
  printf 'android_jar_sha256=%s\n' "$(hash_file "$android_jar")"
  printf 'android_build_tools=35.0.0\n'
  printf 'd8_version=%s\n' "$("$d8" --version | head -1)"
  printf 'd8_sha256=%s\n' "$(hash_file "$d8")"
  printf 'javac_version=%s\n' "$("$javac_bin" -version 2>&1)"
  printf 'jar_sha256=%s\n' "$(hash_file "$output_path")"
} > "$provenance_path"
printf 'Built %s\nProvenance %s\n' "$output_path" "$provenance_path"
