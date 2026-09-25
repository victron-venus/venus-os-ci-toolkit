#!/usr/bin/env bash
set -euo pipefail
# Run at the operator-reviewed main SHA, with no application/signing secrets.
pool=${1:?expected pool: linux, kvm or release}
case "$pool" in linux|kvm|release) ;; *) exit 2 ;; esac
[[ "$(uname -m)" == x86_64 ]]
[[ "$(id -u)" == 1001 ]]
[[ ! -S /var/run/docker.sock ]]
[[ ! -S /run/containerd/containerd.sock ]]
[[ ! -e /var/run/secrets/kubernetes.io/serviceaccount/token ]]
for tool in bash python3 pip3 git gh curl jq unzip tar gzip ffmpeg java node npm timeout free; do
    command -v "$tool" >/dev/null
done
python3 -m pip --version
java -version
node --version
[[ -d "${RUNNER_TEMP:?Run this as an Actions job}" ]]
for directory in "$RUNNER_TEMP" "$RUNNER_TOOL_CACHE" "$ANDROID_HOME"; do
    [[ -w "$directory" ]]
done
scratch=$(mktemp -d "$RUNNER_TEMP/ottplay-smoke.XXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
python3 -m venv "$scratch/venv"
"$scratch/venv/bin/python" -m pip --version
curl --fail --silent --show-error --max-time 20 -o /dev/null https://api.github.com/meta
curl --fail --silent --show-error --max-time 20 -o /dev/null https://registry.npmjs.org/playwright
curl --fail --silent --show-error --max-time 20 -o /dev/null https://dl.google.com/android/repository/repository2-1.xml
if [[ "$pool" == kvm ]]; then
    [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]
    python3 - <<'PY'
import fcntl
import os
fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)
try:
    assert fcntl.ioctl(fd, 0xAE00, 0) == 12, 'KVM_GET_API_VERSION failed'
finally:
    os.close(fd)
PY
    # The workflow installs its pinned SDK before this check.
    "$ANDROID_HOME/emulator/emulator" -accel-check
fi
printf 'Pool %s runtime checks passed\n' "$pool"
