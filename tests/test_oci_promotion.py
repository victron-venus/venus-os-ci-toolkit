"""Synthetic archive tests; no network, candidate execution or publication."""
import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_oci_promotion as promotion  # noqa: E402


def zipped(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.binaries = {name: (name + " fixture").encode() for name in promotion.TOOLS}
        self.hashes = {name: promotion.sha(data) for name, data in self.binaries.items()}
        self.lock = {"schema": 1, "reviewed": True, "repository": promotion.REPO,
                     "source": "a" * 40, "sources_sha256": "b" * 64, "qualification_run": 123,
                     "architectures": {arch: {
                         "binaries": self.hashes,
                         "candidate": {"id": 100, "sha256": "c" * 64, "bytes": 1024},
                         "runtime": {"id": 101, "sha256": "d" * 64, "bytes": 1024},
                     } for arch in ("amd64", "arm64")}}

    def candidate(self, mutate=None, extra=None):
        metadata = {"commit": self.lock["source"], "architecture": "amd64",
                    "sources_sha256": self.lock["sources_sha256"],
                    "release": False, "binaries": self.hashes}
        entries = {"output/candidate.json": json.dumps(metadata).encode(),
                   "output/evidence/security/gate.json": b'{"passed": true}'}
        entries.update({"output/bin/" + name: data for name, data in self.binaries.items()})
        if mutate:
            mutate(entries)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, value in entries.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
            if extra:
                archive.addfile(extra)
        return zipped({"candidate.tar.gz": stream.getvalue()})

    def test_accepts_only_complete_reviewed_two_architecture_lock(self):
        promotion.validate_lock(self.lock)
        for field, value in (("reviewed", False), ("source", "main"),
                             ("architectures", {}), ("repository", "other/repo")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                promotion.validate_lock(dict(self.lock, **{field: value}))

    def test_preserves_original_archive_and_exact_binary_bytes(self):
        data = self.candidate()
        payload, binaries = promotion.verify_candidate(data, self.lock, "amd64")
        self.assertEqual(payload, promotion.zip_members(data, promotion.MAX_ARCHIVE)["candidate.tar.gz"])
        self.assertEqual(binaries, self.binaries)

    def test_changed_binary_wrong_architecture_and_failed_gate_are_rejected(self):
        cases = [lambda e: e.update({"output/bin/runc": b"modified"}),
                 lambda e: e.update({"output/evidence/security/gate.json": b'{"passed": false}'})]
        for mutate in cases:
            with self.assertRaises(ValueError):
                promotion.verify_candidate(self.candidate(mutate), self.lock, "amd64")
        with self.assertRaises(ValueError):
            promotion.verify_candidate(self.candidate(), self.lock, "arm64")
        changed = copy.deepcopy(self.lock)
        changed["source"] = "f" * 40
        with self.assertRaises(ValueError):
            promotion.verify_candidate(self.candidate(), changed, "amd64")

    def test_links_paths_aliases_duplicates_and_special_modes_are_rejected(self):
        for name in ("output/../escape", "/outside", "output/bin//runc", "output/bin/runc",
                     "output/bin/runc/", "output/candidate.json/", "output/new-file/"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                promotion.verify_candidate(self.candidate(extra=tarfile.TarInfo(name)), self.lock, "amd64")
        directory = tarfile.TarInfo("output/bin/runc/")
        directory.type = tarfile.DIRTYPE
        with self.assertRaises(ValueError):
            promotion.verify_candidate(self.candidate(extra=directory), self.lock, "amd64")
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE):
            entry = tarfile.TarInfo("output/link")
            entry.type, entry.linkname = kind, "/outside"
            with self.assertRaises(ValueError):
                promotion.verify_candidate(self.candidate(extra=entry), self.lock, "amd64")
        entry = tarfile.TarInfo("output/setuid")
        entry.mode = 0o4755
        with self.assertRaises(ValueError):
            promotion.verify_candidate(self.candidate(extra=entry), self.lock, "amd64")

    def test_runtime_requires_both_container_modes_positive_build_and_confinement(self):
        result = {"commit": self.lock["source"], "architecture": "amd64", "passed": True,
                  "checks": ["baseline", "restricted", "buildkit-add-run-export-and-traversal"]}
        observed = {"outside_write": False, "timed_out": False}

        def evidence():
            return zipped({"runtime.json": json.dumps(result),
                           "runtime-logs/escape-result.json": json.dumps(observed)})

        promotion.verify_runtime(evidence(), self.lock["source"], "amd64")
        for field in ("outside_write", "timed_out"):
            observed[field] = True
            with self.assertRaises(ValueError):
                promotion.verify_runtime(evidence(), self.lock["source"], "amd64")
            observed[field] = False
        result["checks"] = ["baseline", "restricted"]
        with self.assertRaises(ValueError):
            promotion.verify_runtime(evidence(), self.lock["source"], "amd64")

    def test_artifact_binds_id_digest_bytes_source_and_run(self):
        data = b"fixed archive bytes"
        expected = self.lock["architectures"]["amd64"]["candidate"]
        expected.update(sha256=promotion.sha(data), bytes=len(data))
        info = {"id": expected["id"], "name": "oci-candidate-amd64", "expired": False,
                "digest": "sha256:" + expected["sha256"], "size_in_bytes": len(data),
                "workflow_run": {"id": 123, "head_sha": self.lock["source"]}}
        with patch.object(promotion, "api", return_value=info), \
                patch.object(promotion, "gh", return_value=data):
            self.assertEqual(promotion.artifact(self.lock, "amd64", "candidate"), data)
        mutations = [{"id": 101}, {"expired": True}, {"name": "oci-candidate-arm64"},
                     {"digest": "sha256:" + "e" * 64}, {"size_in_bytes": len(data) + 1},
                     {"workflow_run": {"id": 124, "head_sha": self.lock["source"]}},
                     {"workflow_run": {"id": 123, "head_sha": "f" * 40}}]
        for mutation in mutations:
            with self.subTest(mutation=mutation), \
                    patch.object(promotion, "api", return_value=dict(info, **mutation)), \
                    patch.object(promotion, "gh") as download, self.assertRaises(ValueError):
                try:
                    promotion.artifact(self.lock, "amd64", "candidate")
                finally:
                    download.assert_not_called()
        with patch.object(promotion, "api", return_value=info), \
                patch.object(promotion, "gh", return_value=b"changed"), self.assertRaises(ValueError):
            promotion.artifact(self.lock, "amd64", "candidate")

    def test_failed_or_wrong_run_is_rejected_before_artifact_download(self):
        run = {"head_sha": self.lock["source"], "run_attempt": 1, "event": "workflow_dispatch",
               "status": "completed", "conclusion": "success",
               "path": ".github/workflows/oci-tool-qualification.yml"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".github/oci-tools").mkdir(parents=True)
            (root / ".github/oci-tools/promotion.json").write_text(json.dumps(self.lock))
            for mutation in ({"conclusion": "failure"}, {"event": "pull_request"},
                             {"run_attempt": 2}, {"head_sha": "f" * 40}, {"path": "other.yml"}):
                with self.subTest(mutation=mutation), patch.object(promotion, "ROOT", root), \
                        patch.object(promotion, "hosted_arch"), \
                        patch.object(promotion.os, "geteuid", return_value=1001), \
                        patch.object(promotion, "api", return_value=dict(run, **mutation)), \
                        patch.object(promotion, "artifact") as download, self.assertRaises(ValueError):
                    try:
                        promotion.main()
                    finally:
                        download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
