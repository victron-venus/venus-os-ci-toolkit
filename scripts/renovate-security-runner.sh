#!/bin/bash
# Keep the official image's environment/tool initialization and bundled Node.
set -eo pipefail
if [[ -f /usr/local/etc/env && -z ${CONTAINERBASE_ENV+x} ]]; then
  # shellcheck source=/dev/null
  . /usr/local/etc/env
fi
if [[ ! -d /tmp/containerbase ]]; then
  containerbase-cli init tool all
fi
compat_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
run_renovate() {
  # Explicit Node flags are not exported into package-manager subprocesses.
  exec /usr/local/renovate/node --use-openssl-ca "${RENOVATE_NODE_ARGS[@]}" \
    --import "${compat_dir%/}/renovate-completion-compat.mjs" \
    --import "${compat_dir%/}/renovate-security-compat.mjs" \
    /usr/local/renovate/dist/renovate.js "$@"
}
# Existing credential-free container contract probes do not process a roster.
if [[ $# -eq 1 && ( "$1" == --version || "$1" == --help ) ]]; then
  run_renovate "$@"
fi
completion_dir=$(umask 077; mktemp -d /tmp/renovate-completion.XXXXXXXX)
renovate_pid=''
cleanup_completion() {
  rm -f -- "$completion_dir/run.jsonl"
  rmdir -- "$completion_dir"
}
terminate_renovate() {
  trap - TERM INT
  if [[ -n "$renovate_pid" ]]; then
    kill -s "$1" "$renovate_pid" 2>/dev/null || true
  fi
  exit "$2"
}
trap cleanup_completion EXIT
trap 'terminate_renovate TERM 143' TERM
trap 'terminate_renovate INT 130' INT
export LOG_FILE="$completion_dir/run.jsonl"
export LOG_FILE_FORMAT=json
export LOG_FILE_LEVEL=info
# The workflow alone selects dry-run mode. Unexpected CLI overrides cannot
# return success and accidentally earn a production marker.
for argument in "$@"; do
  if [[ "$argument" == --dry-run* ]]; then
    echo 'Unexpected dry-run command override; no production receipt.' >&2
    exit 1
  fi
done
set +e
run_renovate "$@" &
renovate_pid=$!
wait "$renovate_pid"
renovate_status=$?
renovate_pid=''
set -e
if [[ "$renovate_status" -ne 0 ]]; then
  exit "$renovate_status"
fi
case ${RENOVATE_DRY_RUN:-} in
  ''|false)
    /usr/local/renovate/node "${compat_dir%/}/verify-renovate-completion.cjs" \
      "$LOG_FILE" /github-action/renovate-repositories.json
    ;;
  full|lookup|extract)
    echo 'Dry run finished; no production completion receipt.'
    ;;
  *)
    echo 'Unsupported dry-run mode; no production receipt.' >&2
    exit 1
    ;;
esac
