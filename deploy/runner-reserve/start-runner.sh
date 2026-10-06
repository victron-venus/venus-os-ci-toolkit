#!/usr/bin/env bash
# Runs only inside the mandatory gVisor RuntimeClass, with no host sockets.
set -euo pipefail
umask 077
[[ "$(id -u)" == 0 ]]
[[ "$(uname -r)" == *-gvisor ]]
[[ -n "${RUNNER_RESERVE_NODE_IP:-}" && -n "${RUNNER_RESERVE_POD_UID:-}" ]]
[[ -n "${ACTIONS_RUNNER_INPUT_JITCONFIG:-}" || "${RUNNER_RESERVE_QUALIFICATION:-}" == 1 ]]
[[ ! -e /home/runner/.runner ]]
# The node must acknowledge this exact pod after committing its network fence.
for attempt in {1..60}; do
  if curl --fail --silent --connect-timeout 2 --max-time 3 \
      --cacert /opt/victron-ci-reserve/guard-ca.crt \
      "https://${RUNNER_RESERVE_NODE_IP}:19999/ready/${RUNNER_RESERVE_POD_UID}" |
      python3 -c 'import json,sys; assert json.load(sys.stdin)["admitted"] is True'; then
    break
  fi
  [[ "$attempt" != 60 ]] || exit 1
  sleep 2
done
cp -R -P --no-preserve=ownership,timestamps /opt/actions-runner/. /home/runner/
mkdir -p /home/runner/_work "$RUNNER_TOOL_CACHE"
chown -R runner:runner /home/runner "$RUNNER_TOOL_CACHE"
# Required by upstream gVisor's Docker-in-sandbox reference configuration.
interface=$(ip -o -4 route show default | awk '{print $5; exit}')
address=$(ip -o -4 addr show dev "$interface" | awk '{split($4,a,"/"); print a[1]; exit}')
mtu=$(cat "/sys/class/net/$interface/mtu")
printf 1 > /proc/sys/net/ipv4/ip_forward
for protocol in tcp udp; do
  iptables-legacy -t nat -A POSTROUTING -o "$interface" -p "$protocol" -j SNAT --to-source "$address"
done
dockerd --iptables=false --ip6tables=false --feature containerd-snapshotter=false \
  --storage-driver=vfs --mtu="$mtu" --group=runner > /tmp/dockerd.log 2>&1 &
for attempt in {1..60}; do
  docker info >/dev/null 2>&1 && break
  [[ "$attempt" != 60 ]] || { tail -30 /tmp/dockerd.log; exit 1; }
  sleep 1
done
if [[ "${RUNNER_RESERVE_QUALIFICATION:-}" == 1 ]]; then
  exec setpriv --reuid=runner --regid=runner --init-groups /opt/victron-ci-reserve/qualify.sh
fi
exec setpriv --reuid=runner --regid=runner --init-groups ./run.sh
