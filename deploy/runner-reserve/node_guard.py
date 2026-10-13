#!/usr/bin/env python3
"""Fence sandbox workers before they can register with GitHub.

Runs on the node, never in a runner. Rules match the host veth and source MAC/IP, so a sandbox with Docker
networking cannot spoof its way out through ARP, IPv6 or source-IP changes.
Only the dedicated reserve workers are affected; private runners are excluded.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import re
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from socketserver import TCPServer, ThreadingMixIn
from pathlib import Path

TABLE = "victron_ci_reserve"
LABEL = "runner-reserve.github.io/role"
NAMESPACES = {"runner-reserve-ci", "runner-reserve-automation", "runner-reserve-release"}
DENIED = tuple(json.loads(Path(__file__).with_name("network-policy.json").read_text())["denied_networks"])
PORT = 19999
CNI_RESULTS = Path("/var/lib/cni/results")
NETWORK_DEVICES = Path("/sys/class/net")
SYNC_ERRORS = (OSError, ValueError, KeyError, IndexError, TypeError,
               StopIteration, RuntimeError, subprocess.TimeoutExpired)


def command(*args, input_text=None):
    """Never use a shell or emit runtime payloads/credentials on failure."""
    result = subprocess.run(args, input=input_text, text=True, capture_output=True,
                            timeout=15, check=False)
    if result.returncode:
        raise RuntimeError("Node guard command failed: " + args[0])
    return result.stdout


def worker_from_cache(pod, cache, links):
    """Bind CRI identity to CNI-owned network data and a currently existing veth."""
    if (cache.get("kind") != "cniCacheV1" or cache.get("containerId") != pod["id"]
            or cache.get("ifName") != "eth0"):
        raise ValueError("CNI cache does not match the worker")
    result = cache["result"]
    addresses = [v for v in result["ips"] if ":" not in v["address"]]
    if len(addresses) != 1:
        raise ValueError("Worker needs one IPv4 address")
    address = str(ipaddress.IPv4Interface(addresses[0]["address"]).ip)
    index = addresses[0]["interface"]
    if type(index) is not int or not 0 <= index < len(result["interfaces"]):
        raise ValueError("Invalid CNI interface index")
    nic = result["interfaces"][index]
    if nic["name"] != "eth0" or not re.fullmatch(
            r"/(?:var/)?run/netns/cni-[A-Za-z0-9-]+", nic.get("sandbox", "")):
        raise ValueError("Unexpected worker network namespace")
    peers = [v for v in result["interfaces"] if v["name"].startswith("veth")]
    if len(peers) != 1 or links.get(peers[0]["name"]) != peers[0]["mac"]:
        raise ValueError("Worker veth is absent or has changed")
    return {"uid": pod["metadata"]["uid"], "ip": address,
            "interface": peers[0]["name"], "mac": nic["mac"]}


def cached_worker(pod):
    """Read only local CNI/sysfs state; never spawn a process for identity checks."""
    sandbox_id = pod["id"]
    if not re.fullmatch(r"[0-9a-f]{64}", sandbox_id):
        raise ValueError("Invalid sandbox identity")
    path = CNI_RESULTS / ("cbr0-" + sandbox_id + "-eth0")
    if path.is_symlink():
        raise ValueError("Refusing a CNI cache symlink")
    cache = json.loads(path.read_text())
    peers = [v for v in cache["result"]["interfaces"] if v["name"].startswith("veth")]
    if len(peers) != 1 or not re.fullmatch(r"veth[A-Za-z0-9]{1,11}", peers[0]["name"]):
        raise ValueError("Invalid worker interface")
    interface = peers[0]["name"]
    device = NETWORK_DEVICES / interface
    mac = (device / "address").read_text().strip()
    worker = worker_from_cache(pod, cache, {interface: mac})
    # ifindex changes when an interface name is reused, even with the same MAC.
    return {**worker, "sandbox_id": sandbox_id,
            "host_mac": mac, "ifindex": int((device / "ifindex").read_text())}


def validate_workers(workers):
    """A stale CRI snapshot must not admit a different pod that reused its IP/veth."""
    for worker in workers:
        pod = {"id": worker["sandbox_id"], "metadata": {"uid": worker["uid"]}}
        if cached_worker(pod) != worker:
            raise ValueError("Worker network identity has changed")


def discover():
    """Only the periodic discovery pass consults the container runtime."""
    pods = json.loads(command("/opt/victron-ci-reserve/crictl", "--runtime-endpoint",
                              "unix:///run/k3s/containerd/containerd.sock", "pods",
                              "--label", LABEL + "=worker", "-o", "json"))["items"]
    result = []
    for pod in pods:
        if (pod["metadata"]["namespace"] not in NAMESPACES
                or pod.get("labels", {}).get(LABEL) != "worker"
                or pod.get("state") != "SANDBOX_READY"):
            continue
        result.append(cached_worker(pod))
    return sorted(result, key=lambda row: row["uid"])


def rules(workers, node_ip, dns_ip, extra_denied=(), *, exists=False):
    """Atomically replace only our bridge table, before routing/service DNAT.

    Binding rules to the veth also protects against crafted ARP, IPv6 and source
    address packets from the Docker-capable sandbox. No source-IP-only trust.
    """
    node_ip = str(ipaddress.IPv4Address(node_ip))
    dns_ip = str(ipaddress.IPv4Address(dns_ip))
    denied = [str(ipaddress.IPv4Network(cidr)) for cidr in (*DENIED, *extra_denied)]
    result = [f"delete table bridge {TABLE}"] if exists else []
    result += [f"table bridge {TABLE} {{", "chain ingress {",
               "type filter hook prerouting priority -350; policy accept;"]
    for worker in workers:
        address = str(ipaddress.IPv4Address(worker["ip"]))
        interface, mac = worker["interface"], worker["mac"]
        if not re.fullmatch(r"veth[A-Za-z0-9]{1,11}", interface):
            raise ValueError("Invalid worker interface")
        if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            raise ValueError("Invalid worker MAC")
        match = f'iifname "{interface}"'
        result += [f"{match} ether saddr != {mac} counter drop",
                   f"{match} ether type arp arp saddr ip != {address} counter drop",
                   f"{match} ether type arp counter accept",
                   f"{match} ether type != ip counter drop",
                   f"{match} ip saddr != {address} counter drop",
                   f"{match} ip daddr {node_ip} tcp dport {PORT} counter accept"]
        for protocol in ("udp", "tcp"):
            result.append(f"{match} ip daddr {dns_ip} {protocol} dport 53 counter accept")
        result += [f"{match} ip daddr {{ {', '.join(denied)} }} counter drop",
                   f"{match} tcp dport {{ 80, 443 }} counter accept",
                   f"{match} counter drop"]
    return "\n".join([*result, "}", "}", ""])


def canonical_ruleset(payload, *, echoed=False):
    """Ignore counters/handles, but retain rule order and every filtering attribute."""
    def stable(value):
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "handle":
                    continue
                if key == "counter" and isinstance(item, dict):
                    item = {k: v for k, v in item.items() if k not in {"packets", "bytes"}}
                result[key] = stable(item)
            return result
        return value

    objects = []
    for item in json.loads(payload)["nftables"]:
        if "metainfo" in item:
            continue
        if echoed:
            if "delete" in item:
                continue
            item = item.get("add", item)
        objects.append(stable(item))
    if not objects:
        raise ValueError("Empty firewall snapshot")
    return objects


def firewall_snapshot():
    result = subprocess.run(["nft", "--json", "--stateless", "list", "table", "bridge", TABLE],
                            text=True, capture_output=True, check=False, timeout=15)
    return canonical_ruleset(result.stdout) if result.returncode == 0 else None


def apply_firewall(rendered):
    # nft 1.0.x may consume stdin while detecting JSON input. A private file
    # keeps text input and JSON echo reliable without invoking a shell.
    with tempfile.NamedTemporaryFile(mode="w", prefix="reserve-guard-", suffix=".nft") as source:
        source.write(rendered)
        source.flush()
        return command("nft", "--json", "--stateless", "--echo", "-f", source.name)


def interval(config, key, default, maximum):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid guard interval: " + key)
    if not math.isfinite(value) or not 1 <= value <= maximum:
        raise ValueError("Invalid guard interval: " + key)
    return value


class Guard:
    """Check local identity frequently; reconcile expensive state only when needed."""
    def __init__(self, config):
        self.config = config
        self.poll_interval = interval(config, "identity_check_interval_seconds", 2, 10)
        self.discovery_interval = interval(config, "discovery_interval_seconds", 15, 300)
        self.firewall_interval = interval(config, "firewall_check_interval_seconds", 10, 60)
        self.admitted = {}
        self.admitted_until = 0
        self.workers = None
        self.firewall = None
        self.next_discovery = self.next_firewall = 0
        self.lock = threading.Lock()

    def revoke(self):
        with self.lock:
            self.admitted = {}
            self.admitted_until = 0

    def sync(self):
        try:
            self._sync()
        except SYNC_ERRORS:
            self.revoke()
            # Recover only from a fresh CRI snapshot, never from cached admission.
            self.next_discovery = self.next_firewall = 0
            raise

    def _sync(self):
        now = time.monotonic()
        workers = self.workers
        if workers is None or now >= self.next_discovery:
            workers = discover()
            self.next_discovery = time.monotonic() + self.discovery_interval
        elif now >= self.next_firewall:
            validate_workers(workers)
        changed = workers != self.workers
        if changed or now >= self.next_firewall:
            actual = firewall_snapshot()
            if changed or self.firewall is None or actual != self.firewall:
                self.revoke()
                rendered = rules(workers, self.config["node_ip"], self.config["dns_ip"],
                                 self.config.get("extra_denied", []), exists=actual is not None)
                # Echo comes from our own atomic transaction, not an untrusted
                # post-write listing that might already contain external drift.
                installed = apply_firewall(rendered)
                expected = canonical_ruleset(installed, echoed=True)
                if firewall_snapshot() != expected:
                    raise RuntimeError("Firewall changed during reconciliation")
                self.firewall = expected
            self.next_firewall = time.monotonic() + self.firewall_interval
        # CRI/nft operations can take seconds under load. Validate identities
        # again before acknowledging them, including after a firewall repair.
        validate_workers(workers)
        self.workers = workers
        with self.lock:
            self.admitted = {row["ip"]: row["uid"] for row in workers}
            # A stalled/crashed reconciliation thread cannot admit indefinitely.
            self.admitted_until = time.monotonic() + self.poll_interval * 2 + 1

    def loop(self):
        while True:
            try:
                self.sync()
            except SYNC_ERRORS:
                print("Node guard unavailable; new workers remain stopped", flush=True)
            time.sleep(self.poll_interval)


class ReadinessServer(ThreadingMixIn, TCPServer):
    """Bound both concurrent connections and time spent reading each request."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, tls_context, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        self.tls_context = tls_context
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(2)
        try:
            request = self.tls_context.wrap_socket(request, server_side=True)
            return request, address
        except Exception:
            request.close()
            raise

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(2)
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def handler(guard):
    """Read-only endpoint, bound to the node's private address; no mutating API."""
    class Readiness(BaseHTTPRequestHandler):
        def do_GET(self):  # pylint: disable=invalid-name
            uid = self.path.removeprefix("/ready/")
            with guard.lock:
                allowed = (self.path.startswith("/ready/") and
                           guard.admitted.get(self.client_address[0]) == uid and
                           time.monotonic() < guard.admitted_until)
            self.send_response(200 if allowed else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"admitted": allowed, "guard_version": 1}).encode())

        def log_message(self, *_args):
            # Health probes have no useful request payload to retain; sync errors are logged.
            pass
    return Readiness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config["tls_certificate"], config["tls_private_key"])
    guard = Guard(config)
    threading.Thread(target=guard.loop, daemon=True).start()
    server = ReadinessServer((str(ipaddress.IPv4Address(config["node_ip"])), PORT),
                             handler(guard), tls_context=context)
    # S5332 does not follow get_request's mandatory TLS wrapper in older analyzers.
    # TLSAdmissionTests checks trust, hostname, pod identity and rejects plaintext.
    server.serve_forever()  # NOSONAR(S5332) TLS-only socket; rejects plaintext.


if __name__ == "__main__":
    main()
