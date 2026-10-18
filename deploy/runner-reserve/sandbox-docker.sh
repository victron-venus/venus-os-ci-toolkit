#!/usr/bin/env bash
# Both daemons and their sockets remain inside the disposable gVisor sandbox.
set -euo pipefail

wait_for_service() {
  local pid=$1 seconds=$2 log=$3
  shift 3
  local deadline=$((SECONDS + seconds))
  while kill -0 "$pid" 2>/dev/null; do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    if ((SECONDS >= deadline)); then
      break
    fi
    sleep 1
  done
  tail -40 "$log" >&2
  return 1
}

cleanup_startup() {
  local pid alive deadline=$((SECONDS + 5))
  for pid in "${sandbox_docker_pid:-}" "${sandbox_containerd_pid:-}"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  while ((SECONDS < deadline)); do
    alive=false
    for pid in "${sandbox_docker_pid:-}" "${sandbox_containerd_pid:-}"; do
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then alive=true; fi
    done
    "$alive" || break
    sleep 0.1
  done
  for pid in "${sandbox_docker_pid:-}" "${sandbox_containerd_pid:-}"; do
    if [[ -n "$pid" ]]; then
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}

docker_api_ready() {
  local response
  response=$(timeout --signal=KILL 6s curl --unix-socket /var/run/docker.sock \
    --noproxy '*' --fail --silent --show-error --connect-timeout 2 --max-time 5 \
    http://localhost/_ping) || return 1
  [[ "$response" == OK ]]
}

docker_storage_driver() {
  local driver=${RUNNER_DOCKER_STORAGE_DRIVER:-vfs} filesystem
  case "$driver" in
    vfs) ;;
    overlay2)
      # Only the admitted per-pod disk mount can back nested overlay. The
      # sandbox root is itself overlay and is not a supported upper filesystem.
      filesystem=$(findmnt --noheadings --output FSTYPE --mountpoint /var/lib/docker) || return 1
      [[ "$filesystem" == 9p ]] || {
        printf 'Nested overlay requires the private Docker data mount\n' >&2
        return 1
      }
      ;;
    *) printf 'Unsupported Docker storage driver\n' >&2; return 1 ;;
  esac
  printf '%s' "$driver"
}

start_sandbox_docker() {
  local mtu=$1 storage_driver
  [[ "$mtu" =~ ^[0-9]+$ ]]
  storage_driver=$(docker_storage_driver)
  local runtime=/var/run/docker/containerd
  local socket="$runtime/containerd.sock"
  mkdir -p "$runtime"
  # Match Docker's managed configuration, but allow slow cold startup more than
  # its hardcoded 15 seconds. No host daemon or Kubernetes socket is used.
  cat > "$runtime/containerd.toml" <<'TOML'
version = 2
root = '/var/lib/docker/containerd/daemon'
state = '/var/run/docker/containerd/daemon'
disabled_plugins = ['io.containerd.grpc.v1.cri', 'io.containerd.cri.v1.images', 'io.containerd.cri.v1.runtime', 'io.containerd.podsandbox.controller.v1.podsandbox']
[grpc]
address = '/var/run/docker/containerd/containerd.sock'
max_recv_message_size = 16777216
max_send_message_size = 16777216
[debug]
address = '/var/run/docker/containerd/containerd-debug.sock'
TOML
  trap cleanup_startup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  containerd --config "$runtime/containerd.toml" > /tmp/containerd.log 2>&1 &
  sandbox_containerd_pid=$!
  wait_for_service "$sandbox_containerd_pid" 180 /tmp/containerd.log \
    timeout --signal=KILL 6s ctr --address "$socket" --connect-timeout 5s --timeout 5s version
  dockerd --containerd="$socket" --iptables=false --ip6tables=false \
    --feature embedded-containerd=false --feature containerd-snapshotter=false --storage-driver="$storage_driver" \
    --mtu="$mtu" --group=runner > /tmp/dockerd.log 2>&1 &
  sandbox_docker_pid=$!
  # The CLI's info command probes client plugins, which can exceed readiness
  # deadlines on a busy node even after the daemon has finished initialization.
  wait_for_service "$sandbox_docker_pid" 180 /tmp/dockerd.log docker_api_ready
  # The pod owns both healthy children from here; pod deletion reaps them.
  trap - EXIT INT TERM
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  start_sandbox_docker "$@"
fi
