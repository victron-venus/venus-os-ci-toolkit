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
import re
import ssl
import subprocess
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


def discover():
    """Avoid expensive sandbox inspection RPCs on the admission path."""
    pods = json.loads(command("/opt/victron-ci-reserve/crictl", "--runtime-endpoint",
                              "unix:///run/k3s/containerd/containerd.sock", "pods",
                              "--label", LABEL + "=worker", "-o", "json"))["items"]
    links = {v["ifname"]: v.get("address") for v in json.loads(command("ip", "-j", "link"))}
    result = []
    for pod in pods:
        if (pod["metadata"]["namespace"] not in NAMESPACES
                or pod.get("labels", {}).get(LABEL) != "worker"
                or pod.get("state") != "SANDBOX_READY"):
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", pod["id"]):
            raise ValueError("Invalid sandbox identity")
        path = Path("/var/lib/cni/results") / ("cbr0-" + pod["id"] + "-eth0")
        if path.is_symlink():
            raise ValueError("Refusing a CNI cache symlink")
        result.append(worker_from_cache(pod, json.loads(path.read_text()), links))
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


class Guard:
    """Expose readiness only after the corresponding firewall transaction succeeds."""
    def __init__(self, config):
        self.config = config
        self.admitted = {}
        self.lock = threading.Lock()

    def sync(self):
        workers = discover()
        check = subprocess.run(["nft", "list", "table", "bridge", TABLE],
                               capture_output=True, check=False, timeout=15)
        rendered = rules(workers, self.config["node_ip"], self.config["dns_ip"],
                         self.config.get("extra_denied", []), exists=check.returncode == 0)
        # A single nftables transaction has no delete/recreate exposure window.
        # Reconcile even unchanged workers so a cleared table cannot stay stale.
        with self.lock:
            self.admitted = {}
        command("nft", "-f", "-", input_text=rendered)
        with self.lock:
            self.admitted = {row["ip"]: row["uid"] for row in workers}

    def loop(self):
        while True:
            try:
                self.sync()
            except (OSError, ValueError, KeyError, IndexError, TypeError,
                    StopIteration, RuntimeError, subprocess.TimeoutExpired):
                # Keep the last firewall rules; never admit new workers from stale state.
                with self.lock:
                    self.admitted = {}
                print("Node guard unavailable; new workers remain stopped", flush=True)
            time.sleep(2)


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
                           guard.admitted.get(self.client_address[0]) == uid)
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
    ReadinessServer((str(ipaddress.IPv4Address(config["node_ip"])), PORT),
                    handler(guard), tls_context=context).serve_forever()


if __name__ == "__main__":
    main()
