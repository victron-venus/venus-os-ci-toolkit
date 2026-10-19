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
wait_for_guard() (
  # Preserve response bytes (including invalid NULs) without exposing them in logs.
  response=$(mktemp 2>/dev/null) || { printf '%s\n' 'Node guard response storage unavailable.' >&2; exit 1; }
  trap 'rm -f -- "$response"' EXIT
  printf '%s\n' 'Waiting for node guard admission (up to 60 attempts).' >&2
  for attempt in {1..60}; do
    if curl --fail --silent --connect-timeout 2 --max-time 3 --max-filesize 65536 \
        --cacert /opt/victron-ci-reserve/guard-ca.crt --output "$response" \
        "https://${RUNNER_RESERVE_NODE_IP}:19999/ready/${RUNNER_RESERVE_POD_UID}"; then
      if python3 -I -c '
import json, sys
try:
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        sys.exit(2)
    response = json.loads(raw)
except (ValueError, UnicodeError, RecursionError, OSError):
    sys.exit(2)
if not isinstance(response, dict):
    sys.exit(2)
sys.exit(0 if response.get("admitted") is True else 1)
' < "$response" 2>/dev/null; then
        printf 'Node guard admitted worker on attempt %s.\n' "$attempt" >&2
        exit 0
      else
        case "$?" in
          1) status=not-admitted ;;
          2) status=invalid-response ;;
          *) status=parser-failed ;;
        esac
      fi
    else
      status="request-failed-curl-$?"
    fi
    if [[ "$attempt" == 60 ]]; then
      printf 'Node guard admission failed after 60 attempts (%s).\n' "$status" >&2
      exit 1
    fi
    if (( attempt % 10 == 0 )); then
      printf 'Still waiting for node guard admission: %s/60 (%s).\n' "$attempt" "$status" >&2
    fi
    sleep 2
  done
)
wait_for_guard
mkdir -p /home/runner/_work "$RUNNER_TOOL_CACHE"
chown runner:runner /home/runner/_work "$RUNNER_TOOL_CACHE"
# Required by upstream gVisor's Docker-in-sandbox reference configuration.
interface=$(ip -o -4 route show default | awk '{print $5; exit}')
address=$(ip -o -4 addr show dev "$interface" | awk '{split($4,a,"/"); print a[1]; exit}')
# gVisor exposes link MTU through netlink, without Linux's /sys/class/net files.
mtu=$(ip -o link show dev "$interface" | awk '{for (i=1;i<NF;i++) if ($i=="mtu") {print $(i+1); exit}}')
[[ "$mtu" =~ ^[0-9]+$ ]]
printf 1 > /proc/sys/net/ipv4/ip_forward
for protocol in tcp udp; do
  iptables-legacy -t nat -A POSTROUTING -o "$interface" -p "$protocol" -j SNAT --to-source "$address"
done
/opt/victron-ci-reserve/sandbox-docker.sh "$mtu"
# Checkout/build files must remain readable by non-root users in built images.
# Bootstrap setup stayed private; each job has its own disposable filesystem.
umask 022
if [[ "${RUNNER_RESERVE_QUALIFICATION:-}" == 1 ]]; then
  # Keep timeout outside setpriv so its root supervisor can signal the probe's
  # process group after the UID drop. Do not use --foreground (it omits children).
  exec timeout --signal=TERM --kill-after=15s 600s \
    setpriv --reuid=runner --regid=runner --init-groups /opt/victron-ci-reserve/qualify.sh
fi
exec setpriv --reuid=runner --regid=runner --init-groups ./run.sh
