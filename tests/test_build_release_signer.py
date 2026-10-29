"""The downstream signer builder rejects unsafe inputs before execution."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_release_signer as builder


class BuildSignerTests(unittest.TestCase):
    def test_qualified_export_is_scoped_and_follows_successful_checks(self):
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / '.github/workflows/contract-tests.yml').read_text())
        steps = workflow['jobs']['release-signer-contract']['steps']
        receipt_step = next(s for s in steps if s.get('id') == 'signer-receipt')
        upload = steps[steps.index(receipt_step) + 1]
        self.assertIn('check_release_signer_tls.py', steps[steps.index(receipt_step) - 1]['run'])
        self.assertIn('check_release_signer.py', steps[steps.index(receipt_step) - 2]['run'])
        self.assertNotIn('if', receipt_step)
        self.assertNotIn('if', upload)
        self.assertEqual(upload['with']['retention-days'], 7)
        self.assertEqual(upload['with']['path'].splitlines(), [
            '${{ runner.temp }}/release-signer/cosign', '${{ runner.temp }}/release-signer/receipt.json'])
        code = receipt_step['run'].split("python3 - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for version in ('v4.1.6', 'v4.1.4'):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory, contextlib.chdir(root):
                binary = Path(directory) / 'release-signer/cosign'
                binary.parent.mkdir()
                binary.write_bytes(b'inert test payload')
                metadata = (f'{binary}: {builder.GO_VERSION}\n'
                            f'\tdep\tgithub.com/go-jose/go-jose/v4\t{version}\th1:fixture\n'
                            '\tbuild\tCGO_ENABLED=0\n\tbuild\tGOOS=linux\n\tbuild\tGOARCH=amd64\n')
                environment = {'RUNNER_TEMP': directory, 'GITHUB_OUTPUT': directory + '/outputs',
                               'GITHUB_REPOSITORY': 'example/toolkit', 'GITHUB_RUN_ID': '123',
                               'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_NAME': 'pull_request',
                               'GITHUB_SHA': 'b' * 40, 'PR_HEAD_SHA': 'a' * 40,
                               'UNRELATED_SECRET': 'must-never-be-exported'}
                with patch.dict(os.environ, environment), patch.object(subprocess, 'check_output',
                        side_effect=[metadata, builder.SOURCE, 'b' * 40]):
                    if version != 'v4.1.6':
                        with self.assertRaisesRegex(SystemExit, 'does not match'):
                            exec(compile(code, '<qualification receipt>', 'exec'), {})
                        self.assertFalse(binary.with_name('receipt.json').exists())
                        continue
                    exec(compile(code, '<qualification receipt>', 'exec'), {})
                text = binary.with_name('receipt.json').read_text()
                receipt = json.loads(text)
                self.assertNotIn(environment['UNRELATED_SECRET'], text)
                self.assertEqual(receipt['kind'], 'unsigned-ci-qualification-receipt')
                self.assertEqual(receipt['binary']['sha256'], hashlib.sha256(binary.read_bytes()).hexdigest())
                self.assertEqual(receipt['checkout_commit'], 'b' * 40)
                self.assertEqual(receipt['pull_request_head'], 'a' * 40)
                self.assertEqual(set(receipt['inputs']), {'.github/cosign/go.mod', '.github/cosign/go.sum',
                                 'scripts/build_release_signer.py', 'scripts/check_release_signer.py',
                                 'scripts/check_release_signer_tls.py'})
                self.assertEqual(Path(environment['GITHUB_OUTPUT']).read_text(), 'platform=linux-amd64\n')

    def test_environment_requires_verification_and_limits_workers(self):
        with patch.dict(os.environ, {"GONOSUMDB": "*", "GOPROXY": "http://example.invalid", "GODEBUG": "fips140=off", "GOOS": "windows", "GOARCH": "386", "CGO_ENABLED": "1", "CC": "/bad/compiler", "GOEXPERIMENT": "bad"}):
            env = builder.build_environment()
        self.assertEqual(env["GOSUMDB"], "sum.golang.org")
        self.assertEqual(env["GONOSUMDB"], "")
        self.assertEqual(env["GOVCS"], "*:off")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GODEBUG"], "fips140=on")
        self.assertEqual(env["GOMAXPROCS"], "2")
        self.assertEqual(env["GOFLAGS"], "-p=2")
        self.assertNotIn("CC", env)
        self.assertNotIn("GOEXPERIMENT", env)
        self.assertNotEqual(env["GOOS"], "windows")
        self.assertNotEqual(env["GOARCH"], "386")
        self.assertEqual(env["CGO_ENABLED"], "0")

    def test_bad_source_and_toolchain_never_execute_build(self):
        for responses, reason in [(["wrong"], "source commit"),
                                  ([builder.SOURCE, " M main.go"], "must be clean"),
                                  ([builder.SOURCE, "", "go1.27.2"], "Go 1.27.3")]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "src").mkdir()
                with patch.object(builder, "run", side_effect=responses), \
                     patch.object(builder.subprocess, "run") as execute, \
                     self.assertRaisesRegex(ValueError, reason):
                    builder.build(root / "src", root / "out/cosign")
                execute.assert_not_called()

    def test_failed_build_restores_original_module_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source"
            source.mkdir()
            for name in ("go.mod", "go.sum"):
                (source / name).write_text("original " + name)
            with patch.object(builder, "run", side_effect=[builder.SOURCE, "", builder.GO_VERSION]), \
                 patch.object(builder.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "go")), \
                 self.assertRaises(subprocess.CalledProcessError):
                builder.build(source, root / "out/cosign")
            for name in ("go.mod", "go.sum"):
                self.assertEqual((source / name).read_text(), "original " + name)

    def test_download_must_not_rewrite_reviewed_lock(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source"
            source.mkdir()
            for name in ("go.mod", "go.sum"):
                (source / name).write_text("original " + name)
            def mutate(_args, **_kwargs):
                (source / "go.sum").write_text("unreviewed download")
            with patch.object(builder, "run", side_effect=[builder.SOURCE, "", builder.GO_VERSION]), \
                 patch.object(builder.subprocess, "run", side_effect=mutate), \
                 self.assertRaisesRegex(ValueError, "changed the reviewed lock"):
                builder.build(source, root / "out/cosign")
            self.assertFalse((root / "out").exists())
            self.assertEqual((source / "go.sum").read_text(), "original go.sum")

    def test_output_inside_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaisesRegex(ValueError, "outside"):
            builder.build(Path(d), Path(d) / "cosign")


if __name__ == "__main__":
    unittest.main()
