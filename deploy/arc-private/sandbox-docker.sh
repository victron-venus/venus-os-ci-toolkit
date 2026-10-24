#!/usr/bin/env bash
# Private image wrapper; the public image keeps the original Docker bootstrap.
set -euo pipefail

private_tmpfs_storage() {
  local data=$1 filesystem entries stats block_size blocks inodes extra diagnostic
  local capacity inode_cap namespace profile memory_request memory_limit
  # Return explicitly: the shared daemon launcher invokes this gate inside
  # command substitution, where Bash may disable errexit.
  case "${RUNNER_PRIVATE_DOCKER_STORAGE:-}" in
    ci-tmpfs-2gi)
      capacity=2147483648; inode_cap=262144; namespace=arc-private-ci; profile=ci
      memory_request=3221225472; memory_limit=6442450944
      ;;
    ci-heavy-tmpfs-6gi)
      capacity=6442450944; inode_cap=524288; namespace=arc-private-ci; profile=ci
      memory_request=13958643712; memory_limit=17179869184
      ;;
    ci-bounded-heavy-v1-tmpfs-6gi)
      capacity=6442450944; inode_cap=524288; namespace=arc-private-ci; profile=ci
      memory_request=9663676416; memory_limit=12884901888
      ;;
    automation-tmpfs-3gi)
      capacity=3221225472; inode_cap=262144; namespace=arc-private-automation; profile=automation
      memory_request=5368709120; memory_limit=6442450944
      ;;
    *) printf 'Unsupported private tmpfs mode\n' >&2; return 1 ;;
  esac
  [[ "${RUNNER_DOCKER_STORAGE_DRIVER:-}" == overlayfs &&
     "$(id -u)" == 0 && "$(uname -r)" == *-gvisor &&
     "${RUNNER_PRIVATE_NAMESPACE:-}" == "$namespace" &&
     "${RUNNER_PRIVATE_PROFILE:-}" == "$profile" &&
     "${RUNNER_PRIVATE_MEMORY_LIMIT:-}" == "$memory_limit" &&
     "${RUNNER_PRIVATE_MEMORY_REQUEST:-}" =~ ^[1-9][0-9]{0,10}$ ]] || {
    printf 'Private tmpfs context or memory contract rejected\n' >&2; return 1;
  }
  if [[ "$RUNNER_PRIVATE_DOCKER_STORAGE" == ci-tmpfs-2gi ]]; then
    # Preserve the original standard-CI bootstrap range; admission selects 3Gi.
    ((RUNNER_PRIVATE_MEMORY_REQUEST >= memory_request && RUNNER_PRIVATE_MEMORY_REQUEST <= memory_limit)) || {
      printf 'Private tmpfs memory reservation is outside the admitted range\n' >&2; return 1;
    }
  elif [[ "$RUNNER_PRIVATE_MEMORY_REQUEST" != "$memory_request" ]]; then
    printf 'Private tmpfs requires its exact memory reservation\n' >&2; return 1
  fi
  [[ -d "$data" && ! -L "$data" ]] || return 1
  filesystem=$(findmnt --noheadings --output FSTYPE --mountpoint "$data") || return 1
  [[ "$filesystem" == 9p ]] || {
    printf 'Private tmpfs requires a fresh 9p Docker mount\n' >&2; return 1;
  }
  entries=$(find "$data" -mindepth 1 -maxdepth 1 -print -quit) || return 1
  [[ -z "$entries" ]] || {
    printf 'Private tmpfs refuses to hide existing Docker data\n' >&2; return 1;
  }
  # This is a guest mount, not a host tmpfs or an extra OCI/Kubernetes mount.
  # The cgroup memory limit remains authoritative, including inode metadata.
  mount -t tmpfs -o "size=$capacity,nr_inodes=$inode_cap,mode=0710" tmpfs "$data" || return 1
  # A mount-table query can include the covered 9p mount as well as tmpfs.
  # Read the visible filesystem type and bounds from one statfs result.
  stats=$(stat --file-system --format='%T %S %b %c' "$data") || return 1
  [[ "$stats" != *$'\n'* ]] || return 1
  read -r filesystem block_size blocks inodes extra <<< "$stats"
  printf -v diagnostic 'filesystem=%q block_size=%q blocks=%q inodes=%q' \
    "$filesystem" "$block_size" "$blocks" "$inodes"
  [[ "$filesystem" == tmpfs ]] || {
    printf 'Private tmpfs mount type verification failed: %s\n' "$diagnostic" >&2; return 1;
  }
  [[ "$block_size" =~ ^[1-9][0-9]{0,9}$ && "$blocks" =~ ^[1-9][0-9]{0,9}$ &&
     "$inodes" == "$inode_cap" && -z "$extra" ]] || {
    printf 'Private tmpfs statfs bounds rejected: %s\n' "$diagnostic" >&2; return 1;
  }
  ((block_size <= capacity && blocks <= capacity && capacity % block_size == 0 && blocks == capacity / block_size)) || {
    printf 'Private tmpfs capacity verification failed: %s\n' "$diagnostic" >&2; return 1;
  }
  printf 'PRIVATE_DOCKER_TMPFS_READY filesystem=tmpfs bytes=%s inodes=%s memory_request=%s memory_limit=%s\n' \
    "$capacity" "$inode_cap" "$RUNNER_PRIVATE_MEMORY_REQUEST" "$RUNNER_PRIVATE_MEMORY_LIMIT" >&2
  printf '%s' overlayfs
}

private_start_docker() {
  case "${RUNNER_PRIVATE_DOCKER_STORAGE:-disk}" in
    disk) exec /opt/victron-ci-reserve/sandbox-docker-base.sh "$@" ;;
    ci-tmpfs-2gi|ci-heavy-tmpfs-6gi|ci-bounded-heavy-v1-tmpfs-6gi|automation-tmpfs-3gi)
      # Reuse readiness, lifecycle and daemon configuration without modifying
      # the public helper. Only this private mode replaces its storage gate.
      # shellcheck source-path=SCRIPTDIR
      # shellcheck source=../runner-reserve/sandbox-docker.sh
      source /opt/victron-ci-reserve/sandbox-docker-base.sh
      # Experimental: use the containerd image store only in these explicit private tmpfs modes.
      local containerd_binary dockerd_binary
      containerd_binary=$(type -P containerd) || return 1
      dockerd_binary=$(type -P dockerd) || return 1
      [[ "$containerd_binary" == /* && -x "$containerd_binary" ]] || return 1
      [[ "$dockerd_binary" == /* && -x "$dockerd_binary" ]] || return 1
      # The shared launcher backgrounds both functions. Change only the child
      # masks: bootstrap files remain private, but new snapshot roots need 0755.
      containerd() {
        umask 022
        exec "$containerd_binary" "$@"
      }
      dockerd() {
        local -a adapted=("$@")
        local i changed=0 drivers=0
        for ((i=0; i<${#adapted[@]}; i++)); do
          case "${adapted[i]}" in
            --feature)
              if [[ "${adapted[i+1]:-}" == containerd-snapshotter=* ]]; then
                [[ "${adapted[i+1]}" == containerd-snapshotter=false && "$changed" == 0 ]] || return 1
                adapted[i+1]=containerd-snapshotter=true
                changed=1
              fi
              ;;
            --feature=containerd-snapshotter=*|--storage-driver)
              printf 'Experimental image-store argument form rejected\n' >&2; return 1 ;;
            --storage-driver=*)
              [[ "${adapted[i]}" == --storage-driver=overlayfs ]] || return 1
              drivers=$((drivers + 1))
              ;;
          esac
        done
        [[ "$changed" == 1 && "$drivers" == 1 ]] || {
          printf 'Experimental image-store daemon contract rejected\n' >&2; return 1;
        }
        umask 022
        exec "$dockerd_binary" "${adapted[@]}"
      }
      docker_storage_driver() { private_tmpfs_storage /var/lib/docker; }
      start_sandbox_docker "$@"
      ;;
    *) printf 'Unsupported private Docker storage mode\n' >&2; return 1 ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  private_start_docker "$@"
fi
