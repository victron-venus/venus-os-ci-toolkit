#!/usr/bin/env bash
set -euo pipefail
umask 077
# HOME is a new emptyDir for each ARC job. No registration, source, cache or
# signing material survives the runner pod. ARC injects its own JIT config.
[[ "$(id -u)" == 1001 ]]
[[ ! -e /home/runner/.runner ]]
# fsGroup makes the root-owned mount writable, but does not transfer ownership.
# Do not preserve mount-root timestamps; keep file executable bits and symlinks.
cp -R -P --no-preserve=ownership,timestamps /opt/actions-runner/. /home/runner/
mkdir -p "$RUNNER_TOOL_CACHE" "$ANDROID_HOME" /home/runner/_work
cd /home/runner
exec ./run.sh
