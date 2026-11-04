#!/usr/bin/env python3
"""Assemble COPY-only OCI/Docker archives in one bounded, owned BuildKit solve."""

import os
from pathlib import Path
import re
import subprocess
import time
import uuid

import build_runner_image_artifacts as runner

require = runner.require


class AssemblyService:
    def __init__(self, supervisor, binaries, work, observe):
        self.sup, self.binaries, self.work, self.observe = supervisor, binaries, work, observe
        self.unit = "oci-assembly-" + uuid.uuid4().hex + ".service"
        self.description = "Owned OCI assembly " + self.unit
        self.group = "/system.slice/" + self.unit
        self.process = None
        self.started = False

    def state(self):
        command = ["systemctl", "show", self.unit, "--no-pager",
                   "--property=LoadState,ActiveState,Description,FragmentPath,ControlGroup,MainPID"]
        result = subprocess.run(command, env=self.sup.env, text=True, capture_output=True, timeout=15)
        fields = [line.split("=", 1) for line in result.stdout.splitlines() if "=" in line]
        data = dict(fields)
        if data.get("LoadState") == "not-found" and result.returncode in (0, 1):
            return None
        require(result.returncode == 0, "Cannot determine assembly unit ownership")
        return data

    def owned(self, state):
        require(state["Description"] == self.description
                and state["FragmentPath"] == "/run/systemd/transient/" + self.unit,
                "Assembly service ownership changed")

    def check(self):
        state = self.state()
        require(state is not None, "Assembly service disappeared")
        self.owned(state)
        require(state["ActiveState"] == "active" and state["ControlGroup"] == self.group,
                "Assembly service is not in its owned cgroup")
        pid = int(state["MainPID"])
        require(pid > 1, "Assembly service has no running process")
        before = runner.cgroup.process_identity(pid)
        require(before["cgroup"] == self.group or before["cgroup"].startswith(self.group + "/"),
                "Assembly process left its bounded service")
        if self.process is not None:
            require(all(before[key] == self.process[key] for key in ("pid", "starttime_ticks")),
                    "Assembly process identity changed")
        limits = {}
        runner.cgroup.read_limits(self.group, Path("/sys/fs/cgroup"), limits)
        require(limits == {"cpu.max": "200000 100000", "memory.max": str(4 * runner.GIB),
                           "memory.swap.max": "0"}, "Assembly cgroup limits differ")
        require(runner.cgroup.process_identity(pid) == before, "Assembly process changed during sample")
        self.process = before
        self.observe({"kind": "assembly-limits", "unit": self.unit, "process": before, "limits": limits})
        return before

    def start(self):
        require(self.state() is None, "Assembly unit already exists")
        self.started = True
        endpoint = "unix://" + str(self.work / "assembly.sock")
        command = ["sudo", "--", "systemd-run", "--quiet", "--collect", "--service-type=exec",
                   "--unit=" + self.unit, "--description=" + self.description,
                   "--property=CPUQuota=200%", "--property=CPUQuotaPeriodSec=100ms",
                   "--property=MemoryMax=" + str(4 * runner.GIB), "--property=MemorySwapMax=0",
                   "--property=TasksMax=256", "--property=RuntimeMaxSec=600", "--property=KillMode=control-group",
                   "--setenv=PATH=/usr/bin:/bin", str(self.binaries / "buildkitd"),
                   "--addr", endpoint, "--root", str(self.work / "assembly-state"),
                   "--group=" + str(os.getgid()),
                   "--oci-worker=true", "--containerd-worker=false", "--oci-worker-snapshotter=native",
                   "--oci-worker-net=host", "--oci-worker-binary=" + str(self.binaries / "runc")]
        self.sup.run(command, seconds=30)
        self.check()
        deadline = time.monotonic() + 60
        client = [str(self.binaries / "buildctl"), "--addr", endpoint]
        while time.monotonic() < deadline:
            self.check()
            result = subprocess.run([*client, "debug", "workers"], env=self.sup.env,
                                    capture_output=True, timeout=10)
            if result.returncode == 0:
                return client
            time.sleep(1)
        raise ValueError("Assembly BuildKit readiness timed out")

    def stop(self):
        if not self.started:
            return {"started": False}
        state = self.state()
        if state is not None:
            self.owned(state)
            # Cleanup bypasses ordinary disk admission so disk exhaustion cannot
            # prevent stopping this already identified service and all children.
            subprocess.run(["sudo", "--", "systemctl", "stop", self.unit], env=self.sup.env,
                           capture_output=True, timeout=30, check=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.state()
            group = Path("/sys/fs/cgroup") / self.group.lstrip("/")
            if state is None and not group.exists():
                self.started = False
                return {"started": True, "unit": self.unit, "service_removed": True,
                        "cgroup_removed": True, "process": self.process}
            if state is not None:
                self.owned(state)
            time.sleep(0.25)
        raise ValueError("Owned assembly service/cgroup cleanup did not complete")


def assemble(supervisor, binaries, context, work, tag, arch, observe, record, *, source_epoch):
    require(not any(char in str(work) + str(context) for char in (",", "\n", "\r")),
            "Exporter paths contain delimiters")
    recipe = (context / "Dockerfile").read_text()
    instructions = [line.strip() for line in recipe.splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    require(all(line.split()[0] in {"FROM", "COPY", "USER", "ENV", "VOLUME", "ENTRYPOINT"}
                for line in instructions) and not re.search(r"(?mi)^\s*#\s*syntax\s*=", recipe),
            "Assembly permits only the reviewed COPY-only recipe")
    require(arch in {"amd64", "arm64"}, "Native architecture required")
    require(re.fullmatch(r"[1-9]\d*", source_epoch, flags=re.ASCII), "Source commit timestamp required")
    service = AssemblyService(supervisor, binaries, work, observe)
    try:
        client = service.start()
        command = [*client, "build", "--frontend", "dockerfile.v0",
                   "--local", "context=" + str(context), "--local", "dockerfile=" + str(context),
                   "--opt", "force-network-mode=none", "--opt", "platform=linux/" + arch, "--no-cache",
                   "--opt", "build-arg:SOURCE_DATE_EPOCH=" + source_epoch,
                   "--output", "type=oci,source-date-epoch=" + source_epoch + ",dest=" + str(work / "builder.oci.tar"),
                   "--output", "type=docker,compression=uncompressed,force-compression=true,source-date-epoch="
                   + source_epoch + ",name=" + tag + ",dest=" + str(work / "builder.docker.tar")]
        record["assembly_command"] = command
        supervisor.run(command, seconds=360, inspect=service.check)
        service.check()
    finally:
        record["assembly_cleanup"] = service.stop()
        observe({"kind": "assembly-cleanup", **record["assembly_cleanup"]})
