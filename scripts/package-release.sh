#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# != 2 ]]; then
  echo 'Usage: scripts/package-release.sh BASE_VERSION CHANNEL' >&2
  exit 2
fi
python_bin="${RELEASE_PYTHON:-python3}"
exec "$python_bin" scripts/toolkit_source.py build "$1" "$2" release-dist
