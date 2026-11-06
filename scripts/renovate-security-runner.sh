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
# Explicit Node flags are not exported into package-manager subprocesses.
exec /usr/local/renovate/node --use-openssl-ca "${RENOVATE_NODE_ARGS[@]}" \
  --import "${compat_dir%/}/renovate-security-compat.mjs" \
  /usr/local/renovate/dist/renovate.js "$@"
