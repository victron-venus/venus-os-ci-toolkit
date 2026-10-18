#!/bin/bash
set -euo pipefail

# Fixture files are accepted only in read-only check mode, never daemon startup.
proc_cgroup=/proc/self/cgroup
proc_mounts=/proc/self/mountinfo
if [ "$#" -gt 0 ]; then
  [ "$1" = --check ] && { [ "$#" -eq 1 ] || [ "$#" -eq 3 ]; } || exit 2
  proc_cgroup=${2:-$proc_cgroup}
  proc_mounts=${3:-$proc_mounts}
fi
fail() { echo "Docker cgroup admission failed: $*" >&2; exit 1; }
controller_path() {
  awk -F: -v controller="$1" 'index("," $2 ",", "," controller ",") {print $3}' "$proc_cgroup"
}
controller_mount() {
  # mountinfo includes the subtree root; /proc/mounts does not.
  awk -v controller="$1" -v version="$2" '
    { for (i=7; i<=NF; i++) if ($i == "-") {
      if ($(i+1) == version && (version == "cgroup2" ||
          index("," $(i+3) ",", "," controller ","))) print $4, $5
      break
    }}' "$proc_mounts"
}
visible_path() {
  local root="$1" mount="$2" path="$3"
  if [ "$root" = / ]; then printf '%s%s' "$mount" "$path"
  elif [ "$path" = "$root" ]; then printf '%s' "$mount"
  elif [[ "$path" = "$root/"* ]]; then printf '%s%s' "$mount" "${path#"$root"}"
  else return 1
  fi
}
positive_integer() {
  [[ "$1" =~ ^[0-9]+$ ]] && [ "${#1}" -le 18 ] && [ "$1" -gt 0 ]
}
cpu_path=$(controller_path cpu)
memory_path=$(controller_path memory)
if [ -n "$cpu_path" ] || [ -n "$memory_path" ]; then
  # Hybrid hosts also expose 0::; the explicitly attached v1 controllers win.
  version=v1
  [ -n "$cpu_path" ] && [ -n "$memory_path" ] || fail 'CPU or memory controller is missing'
  read -r cpu_root cpu_mount < <(controller_mount cpu cgroup) || fail 'CPU mount is missing'
  read -r memory_root memory_mount < <(controller_mount memory cgroup) || fail 'memory mount is missing'
else
  version=v2
  cpu_path=$(awk -F: '$1 == "0" && $2 == "" {print $3}' "$proc_cgroup")
  memory_path=$cpu_path
  read -r cpu_root cpu_mount < <(controller_mount cpu cgroup2) || fail 'unified mount is missing'
  memory_root=$cpu_root
  memory_mount=$cpu_mount
fi
[ -n "$cpu_mount" ] && [ -n "$memory_mount" ] || fail 'controller mount is missing'
parent=${cpu_path%/*}
[ "$parent" = "${memory_path%/*}" ] || fail 'CPU and memory parents differ'
if ! [[ "$parent" =~ ^/kubepods\.slice(/kubepods-(burstable|besteffort)\.slice)?/kubepods(-burstable|-besteffort)?-pod[0-9a-fA-F_-]+\.slice$ || "$parent" =~ ^/kubepods/(burstable/|besteffort/)?pod[0-9a-fA-F-]+$ ]]; then
  fail 'expected a Kubernetes pod parent'
fi
# Legacy containerd can bind-mount only this container's cgroup. In that
# layout use its stricter limit, not an invisible pod or an unbounded root.
if ! cpu_directory=$(visible_path "$cpu_root" "$cpu_mount" "$parent") ||
   ! memory_directory=$(visible_path "$memory_root" "$memory_mount" "$parent"); then
  [ "$cpu_path" = "$memory_path" ] || fail 'container controllers differ'
  parent=$cpu_path
  [[ "${parent##*/}" =~ ^cri-containerd-[0-9a-f]+\.scope$ ]] || fail 'expected a containerd scope'
  cpu_directory=$(visible_path "$cpu_root" "$cpu_mount" "$parent") || fail 'CPU scope is invisible'
  memory_directory=$(visible_path "$memory_root" "$memory_mount" "$parent") || fail 'memory scope is invisible'
fi
if [ "$version" = v1 ]; then
  read -r quota < "$cpu_directory/cpu.cfs_quota_us"
  read -r period < "$cpu_directory/cpu.cfs_period_us"
  read -r memory < "$memory_directory/memory.limit_in_bytes"
  read -r hierarchy < "$memory_directory/memory.use_hierarchy"
  [ "$hierarchy" = 1 ] || fail 'hierarchical memory accounting is required'
else
  read -r quota period < "$cpu_directory/cpu.max"
  read -r memory < "$memory_directory/memory.max"
fi
if ! positive_integer "$quota" || ! positive_integer "$period"; then
  fail 'finite CPU quota and period are required'
fi
# v1's unlimited sentinel is near 2^63; the integer bound also rejects that.
positive_integer "$memory" || fail 'finite memory limit is required'
echo "Docker parent: $version $parent; CPU $quota/$period; memory $memory bytes"
[ "${1:-}" != --check ] || exit 0
exec dockerd --host=unix:///docker-socket/docker.sock --group=121 \
  --data-root=/docker-data --storage-driver=vfs --log-level=warn --mtu=1200 \
  --exec-opt=native.cgroupdriver=cgroupfs --cgroup-parent="$parent"
