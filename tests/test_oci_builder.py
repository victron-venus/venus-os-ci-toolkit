"""Host-independent security contracts for native builder qualification."""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qualify_oci_builder as builder  # noqa: E402
import oci_builder_assembly as assembly  # noqa: E402


def sha(value):
    return hashlib.sha256(value).hexdigest()


def packed(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def document(value):
    return json.dumps(value, sort_keys=True).encode()


def exports(directory, *, arch="amd64", wrong_diff=False, docker_content=None):
    payload = b"known payload\n"
    layer = packed({"proof": payload})
    config = document({"os": "linux", "architecture": arch, "config": {},
                       "rootfs": {"type": "layers", "diff_ids": ["sha256:" + ("0" * 64 if wrong_diff else sha(layer))]}})
    manifest = document({"schemaVersion": 2,
                         "config": {"mediaType": builder.runner.oci.CONFIG, "digest": "sha256:" + sha(config),
                                    "size": len(config)},
                         "layers": [{"mediaType": builder.runner.oci.LAYER, "digest": "sha256:" + sha(layer),
                                     "size": len(layer)}]})
    index = document({"schemaVersion": 2, "manifests": [{"mediaType": builder.runner.oci.MANIFEST,
                      "digest": "sha256:" + sha(manifest), "size": len(manifest)}]})
    oci_path = directory / "image.oci.tar"
    oci_path.write_bytes(packed({"oci-layout": document({"imageLayoutVersion": "1.0.0"}), "index.json": index,
                                "blobs/sha256/" + sha(config): config, "blobs/sha256/" + sha(layer): layer,
                                "blobs/sha256/" + sha(manifest): manifest}))
    docker_path = directory / "image.docker.tar"
    docker_path.write_bytes(packed({"manifest.json": document([{"Config": "config.json", "RepoTags": ["local/proof:test"],
                                                               "Layers": ["layer.tar"]}]),
                                   "config.json": config,
                                   "layer.tar": layer if docker_content is None else docker_content}))
    return oci_path, docker_path, {"proof": sha(payload)}


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_manual_exact_native_admission(self):
        env = {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux", "RUNNER_ENVIRONMENT": "github-hosted",
               "RUNNER_ARCH": "ARM64", "GITHUB_EVENT_NAME": "workflow_dispatch",
               "GITHUB_REPOSITORY": "victron-venus/venus-os-ci-toolkit", "GITHUB_REF": "refs/heads/main",
               "GITHUB_SHA": "a" * 40, "REVIEWED_COMMIT": "a" * 40, "GITHUB_RUN_ID": "123",
               "GITHUB_RUN_ATTEMPT": "1"}
        self.assertEqual(builder.admit(env), ("arm64", "a" * 40))
        for key, value in (("RUNNER_ENVIRONMENT", "self-hosted"), ("GITHUB_EVENT_NAME", "pull_request"),
                           ("GITHUB_REF", "refs/heads/other"), ("REVIEWED_COMMIT", "b" * 40),
                           ("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_REPOSITORY", "other/repo"),
                           ("GITHUB_RUN_ID", "1\u0661")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                builder.admit({**env, key: value})

    def test_unreviewed_policy_stops_before_host_access(self):
        root = self.root / ".github/oci-tools"
        root.mkdir(parents=True)
        (root / "builder-qualification.json").write_text('{"schema":1,"reviewed":false}')
        with patch.object(builder, "ROOT", self.root), patch.object(builder, "admit") as admission:
            with self.assertRaisesRegex(ValueError, "reviewed image recipe"):
                builder.main()
        admission.assert_not_called()

    def test_same_solve_oci_and_docker_identity(self):
        for arch in ("amd64", "arm64"):
            oci, docker, expected = exports(self.root, arch=arch)
            proof = builder.verify_oci(oci, arch, expected, lambda: None)
            bridge = builder.verify_docker_export(docker, proof, "local/proof:test")
            self.assertEqual(bridge["config"], proof["config"])
            self.assertTrue(bridge["same_solve"])

    def test_oci_rejects_platform_diffid_and_final_payload(self):
        for case in ("platform", "diffid", "payload"):
            oci, _, expected = exports(self.root, wrong_diff=case == "diffid")
            if case == "payload":
                expected = {"proof": "0" * 64}
            with self.subTest(case=case), self.assertRaises(ValueError):
                builder.verify_oci(oci, "arm64" if case == "platform" else "amd64", expected, lambda: None)

    def test_docker_export_rejects_changed_layer_tag_or_config(self):
        oci, docker, expected = exports(self.root, docker_content=packed({"proof": b"changed"}))
        proof = builder.verify_oci(oci, "amd64", expected, lambda: None)
        with self.assertRaisesRegex(ValueError, "layer bytes"):
            builder.verify_docker_export(docker, proof, "local/proof:test")
        _, docker, _ = exports(self.root)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            builder.verify_docker_export(docker, proof, "other/tag")
        with self.assertRaisesRegex(ValueError, "configuration bytes"):
            builder.verify_docker_export(docker, {**proof, "config": "sha256:" + "0" * 64}, "local/proof:test")

    def test_docker_export_rejects_regular_file_path_alias(self):
        oci, docker, expected = exports(self.root)
        proof = builder.verify_oci(oci, "amd64", expected, lambda: None)
        with tarfile.open(docker, "r:") as archive:
            files = {entry.name: archive.extractfile(entry).read() for entry in archive}
        files["config.json/"] = files.pop("config.json")
        docker.write_bytes(packed(files))
        with self.assertRaisesRegex(ValueError, "regular-file"):
            builder.verify_docker_export(docker, proof, "local/proof:test")

    def test_import_refuses_existing_or_ambiguous_tag_before_loading(self):
        cases = [(0, '[{"Id":"existing"}]', ''), (1, '[]', 'daemon unavailable'),
                 (1, '[]', 'Error: No such image: another/tag'), (125, '', 'error')]
        for code, output, error in cases:
            supervisor, record = Mock(), {}
            result = subprocess.CompletedProcess([], code, output, error)
            with patch.object(builder.subprocess, "run", return_value=result), self.assertRaisesRegex(ValueError, "proven absent"):
                builder.begin_import(supervisor, "local/proof:test", self.root / "archive.tar", record)
            supervisor.run.assert_not_called()
            self.assertNotIn("import_started", record)
        supervisor = Mock()
        with patch.object(builder.subprocess, "run") as execute, self.assertRaisesRegex(ValueError, "prior tag absence"):
            builder.remove_import(supervisor, "local/proof:test", "sha256:" + "a" * 64)
        execute.assert_not_called()

    def test_import_records_absence_before_a_load_timeout(self):
        supervisor, record = Mock(), {}
        supervisor.run.side_effect = TimeoutError("fixture timeout after import begins")
        result = subprocess.CompletedProcess([], 1, '[]', 'Error response from daemon: No such image: local/proof:test\n')
        with patch.object(builder.subprocess, "run", return_value=result), self.assertRaises(TimeoutError):
            builder.begin_import(supervisor, "local/proof:test", self.root / "archive.tar", record)
        self.assertTrue(record["import_tag_absent_before_load"])
        self.assertTrue(record["import_started"])

    def test_final_image_scan_requires_exact_image_and_complete_inventory(self):
        image = "sha256:" + "a" * 64
        good = {"ArtifactType": "container_image", "Metadata": {"ImageID": image},
                "Results": [{"Class": "os-pkgs", "Packages": [{"Name": "base"}]},
                            {"Type": "gobinary", "Target": "usr/bin/buildkitd", "Packages": [{"Name": "main"}]}]}
        self.assertTrue(builder.check_scan(good, image, {"buildkitd"})["passed"])
        bad_image = copy.deepcopy(good)
        bad_image["Metadata"]["ImageID"] = "sha256:" + "b" * 64
        bad_inventory = copy.deepcopy(good)
        bad_inventory["Results"] = good["Results"][:1]
        bad_finding = copy.deepcopy(good)
        bad_finding["Results"][0]["Vulnerabilities"] = [{"Severity": "HIGH"}]
        for value in (bad_image, bad_inventory, bad_finding):
            with self.assertRaises(ValueError):
                builder.check_scan(value, image, {"buildkitd"})

    def test_assembly_requires_real_limits_and_owned_process(self):
        supervisor = Mock()
        service = assembly.AssemblyService(supervisor, self.root, self.root, lambda value: None)
        state = {"Description": service.description, "FragmentPath": "/run/systemd/transient/" + service.unit,
                 "ActiveState": "active", "ControlGroup": service.group, "MainPID": "123"}
        process = {"pid": 123, "starttime_ticks": "77", "cgroup": service.group + "/init"}
        limits = {"cpu.max": "200000 100000", "memory.max": str(4 * builder.runner.GIB), "memory.swap.max": "0"}
        def read_limits(group, path, output):
            output.update(limits)
        with patch.object(service, "state", return_value=state), \
             patch.object(assembly.runner.cgroup, "process_identity", return_value=process), \
             patch.object(assembly.runner.cgroup, "read_limits", side_effect=read_limits):
            self.assertEqual(service.check(), process)
            limits["memory.max"] = "max"
            with self.assertRaisesRegex(ValueError, "limits differ"):
                service.check()
        with patch.object(service, "state", return_value={**state, "Description": "foreign"}), \
             patch.object(assembly.subprocess, "run") as execute:
            service.started = True
            with self.assertRaisesRegex(ValueError, "ownership"):
                service.stop()
        execute.assert_not_called()

    def test_assembly_rejects_run_before_starting_service(self):
        (self.root / "Dockerfile").write_text("FROM scratch\nRUN true\n")
        supervisor, observe = Mock(), Mock()
        with patch.object(assembly, "AssemblyService") as service, self.assertRaisesRegex(ValueError, "COPY-only"):
            assembly.assemble(supervisor, self.root, self.root, self.root, "local/proof:test", "amd64", observe, {}, source_epoch="1")
        service.assert_not_called()

    def test_assembly_rejects_external_frontend_directives(self):
        for directive in ("#syntax=remote/frontend", "  # Syntax = remote/frontend"):
            (self.root / "Dockerfile").write_text(directive + "\nFROM scratch\n")
            supervisor, observe = Mock(), Mock()
            with patch.object(assembly, "AssemblyService") as service, self.assertRaisesRegex(ValueError, "COPY-only"):
                assembly.assemble(supervisor, self.root, self.root, self.root, "local/proof:test", "amd64", observe, {}, source_epoch="1")
            service.assert_not_called()

    def test_original_notices_are_bound_and_preserved(self):
        files = {name: b"public license" for name in (
            "output/evidence/buildkit/UPSTREAM-LICENSE", "output/evidence/runc/UPSTREAM-LICENSE",
            "output/evidence/buildkit/notices/Go-LICENSE", "output/evidence/native-notices/libseccomp2/copyright")}
        archive = self.root / "original.tar.gz"
        archive.write_bytes(gzip.compress(packed(files), mtime=0))
        (self.root / "LICENSE").write_text("project license")
        metadata = {"archive": archive.name, "archive_sha256": sha(archive.read_bytes())}
        context = self.root / "context"
        context.mkdir()
        with patch.object(builder, "ROOT", self.root):
            copied = builder.prepare_notices(self.root, context, metadata)
        self.assertEqual(len(copied), len(files) + 1)
        self.assertIn("usr/share/licenses/oci-tools/project-LICENSE", copied)
        changed = self.root / "changed-context"
        changed.mkdir()
        with self.assertRaisesRegex(ValueError, "archive changed"):
            builder.prepare_notices(self.root, changed, {**metadata, "archive_sha256": "0" * 64})
        self.assertFalse((changed / "licenses").exists())

    def test_assembly_start_failure_still_cleans_owned_service(self):
        (self.root / "Dockerfile").write_text("FROM scratch\nCOPY file /file\n")
        service = Mock()
        service.start.side_effect = ValueError("fixture startup failed")
        service.stop.return_value = {"service_removed": True}
        supervisor, observe = Mock(), Mock()
        with patch.object(assembly, "AssemblyService", return_value=service), self.assertRaisesRegex(ValueError, "startup"):
            assembly.assemble(supervisor, self.root, self.root, self.root, "local/proof:test", "arm64", observe, {}, source_epoch="1")
        service.stop.assert_called_once_with()

    def test_both_exports_share_one_solve_and_source_timestamp(self):
        (self.root / "Dockerfile").write_text("FROM scratch\nCOPY file /file\n")
        service = Mock()
        service.start.return_value = ["/accepted/buildctl", "--addr", "unix:///owned/socket"]
        service.stop.return_value = {"service_removed": True}
        supervisor = Mock()
        with patch.object(assembly, "AssemblyService", return_value=service):
            assembly.assemble(supervisor, self.root, self.root, self.root, "local/proof:test", "amd64",
                              Mock(), {}, source_epoch="1793734000")
        supervisor.run.assert_called_once()
        command = supervisor.run.call_args.args[0]
        outputs = [command[index + 1] for index, value in enumerate(command) if value == "--output"]
        self.assertEqual(len(outputs), 2)
        self.assertTrue(all("source-date-epoch=1793734000" in value for value in outputs))
        self.assertTrue(any("type=docker,compression=uncompressed,force-compression=true" in value for value in outputs))
        self.assertIn("build-arg:SOURCE_DATE_EPOCH=1793734000", command)

    def test_assembly_rejects_unicode_timestamp_digits(self):
        (self.root / "Dockerfile").write_text("FROM scratch\n")
        supervisor, observe = Mock(), Mock()
        with patch.object(assembly, "AssemblyService") as service, self.assertRaisesRegex(ValueError, "timestamp"):
            assembly.assemble(supervisor, self.root, self.root, self.root, "local/proof:test", "amd64",
                              observe, {}, source_epoch="1\u0661")
        service.assert_not_called()
