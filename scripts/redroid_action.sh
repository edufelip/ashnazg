#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'redroid action: %s\n' "$1" >&2
  exit 2
}

command_text=${ASHNAZG_REDROID_COMMAND-}
output_dir=${ASHNAZG_REDROID_OUTPUT_DIRECTORY-artifacts/redroid}
adb_port=${ASHNAZG_REDROID_ADB_PORT-5555}
boot_timeout=${ASHNAZG_REDROID_BOOT_TIMEOUT-180}
image_provenance=${ASHNAZG_REDROID_IMAGE_PROVENANCE-}

[[ -n "$command_text" ]] || fail 'command input is required'
[[ -n "$output_dir" ]] || fail 'output-directory must not be empty'
[[ "$output_dir" != *$'\n'* && "$output_dir" != *$'\r'* ]] || fail 'output-directory must be a single path'
trimmed_output=$output_dir
while [[ "$trimmed_output" != / && "$trimmed_output" == */ ]]; do
  trimmed_output=${trimmed_output%/}
done
[[ "$trimmed_output" != / ]] || fail 'output-directory must not be the filesystem root'
case "/$output_dir/" in
  */../*) fail 'output-directory must not contain parent-directory components' ;;
esac

[[ "$adb_port" =~ ^[1-9][0-9]{0,4}$ ]] || fail 'adb-port must be a decimal integer from 1 through 65535'
(( 10#$adb_port <= 65535 )) || fail 'adb-port must be a decimal integer from 1 through 65535'
[[ "$boot_timeout" =~ ^[1-9][0-9]{0,3}$ ]] || fail 'boot-timeout must be a decimal integer from 1 through 3600 seconds'
(( 10#$boot_timeout <= 3600 )) || fail 'boot-timeout must be a decimal integer from 1 through 3600 seconds'

umask 077
command_file=$(mktemp "${TMPDIR:-/tmp}/ashnazg-redroid-command.XXXXXXXX") || fail 'could not create the temporary command file'
supervisor_pid=
cleanup() {
  rm -f -- "$command_file"
}
forward_signal() {
  local signal_name=$1 exit_code=$2
  trap '' HUP INT TERM
  if [[ -n "$supervisor_pid" ]]; then
    kill -s "$signal_name" "$supervisor_pid" 2>/dev/null || true
    wait "$supervisor_pid" || true
  fi
  exit "$exit_code"
}
trap cleanup EXIT
trap 'forward_signal HUP 129' HUP
trap 'forward_signal INT 130' INT
trap 'forward_signal TERM 143' TERM
printf '%s' "$command_text" > "$command_file"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
runtime_args=(run
  --output-dir "$output_dir"
  --adb-port "$adb_port"
  --timeout "$boot_timeout")
if [[ -n "$image_provenance" ]]; then
  runtime_args+=(--image-provenance "$image_provenance")
fi
python3 "$script_dir/redroid.py" "${runtime_args[@]}" \
  -- bash --noprofile --norc -e -o pipefail "$command_file" &
supervisor_pid=$!
status=0
wait "$supervisor_pid" || status=$?
supervisor_pid=
exit "$status"
