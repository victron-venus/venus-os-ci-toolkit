"""The local review artifact contains full proofs, never image payloads."""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import stage_runner_evidence as evidence


class RunnerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'output'
        for name in evidence.FILES:
            (self.source / name).write_bytes(b'{"full":"original proof"}\n')

    def test_exact_proofs_and_annotated_log_tail_exclude_images(self):
        (self.source / 'image.oci.tar').write_bytes(b'not evidence')
        (self.source / 'unexpected.json').write_bytes(b'not allowlisted')
        log = b'x' * (evidence.LOG_LIMIT + 20)
        (self.source / 'build.log').write_bytes(log)
        result = evidence.stage(self.source, self.output, True)
        self.assertTrue(result['proofs_complete'])
        self.assertEqual(set(p.name for p in self.output.iterdir()),
                         set(evidence.FILES) | {'evidence-inventory.json'})
        for name, proof in result['files'].items():
            actual = (self.output / name).read_bytes()
            self.assertEqual(proof['sha256'], hashlib.sha256(actual).hexdigest())
            self.assertEqual(actual, (self.source / name).read_bytes()[proof['offset']:])
        tail = result['files']['build.log']
        self.assertEqual((tail['offset'], tail['bytes'], tail['original_bytes']),
                         (20, evidence.LOG_LIMIT, len(log)))
        self.assertFalse(tail['complete'])
        self.assertTrue(result['files']['scan.json']['complete'])

    def test_missing_proof_is_only_permitted_for_failed_build(self):
        (self.source / 'scan.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing proof'):
            evidence.stage(self.source, self.output, True)
        self.assertFalse(self.output.exists())
        result = evidence.stage(self.source, self.output, False)
        self.assertFalse(result['proofs_complete'])
        self.assertEqual(result['missing'], ['scan.json'])

    def test_missing_receipt_is_never_publishable(self):
        (self.source / 'build-receipt.json').unlink()
        with self.assertRaisesRegex(ValueError, 'receipt is required'):
            evidence.stage(self.source, self.output, False)
        self.assertFalse(self.output.exists())

    def test_special_files_are_rejected_without_reading(self):
        path = self.source / 'scan.json'
        path.unlink()
        for kind in ('symlink', 'fifo', 'directory'):
            with self.subTest(kind=kind):
                if kind == 'symlink':
                    path.symlink_to(self.source / 'build.log')
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    path.mkdir()
                with self.assertRaises((ValueError, OSError)):
                    evidence.stage(self.source, self.output, False)
                self.assertFalse(self.output.exists())
                if kind == 'directory':
                    path.rmdir()
                else:
                    path.unlink()

    def test_individual_and_total_caps_fail_without_partial_output(self):
        with patch.dict(evidence.FILES, {'scan.json': 1}), \
             self.assertRaisesRegex(ValueError, 'byte limit'):
            evidence.stage(self.source, self.output, True)
        self.assertFalse(self.output.exists())
        with patch.object(evidence, 'TOTAL_LIMIT', 1), \
             self.assertRaisesRegex(ValueError, 'byte limit'):
            evidence.stage(self.source, self.output, True)
        self.assertFalse(self.output.exists())

    def test_owned_output_only_and_changed_file_rejected(self):
        self.output.mkdir()
        keep = self.output / 'keep'
        keep.write_text('existing user data')
        with self.assertRaises(FileExistsError):
            evidence.stage(self.source, self.output, True)
        self.assertEqual(keep.read_text(), 'existing user data')
        destination = self.root / 'changed'
        with patch.object(evidence.oci, 'identity', side_effect=[1, 2]), \
             self.assertRaisesRegex(ValueError, 'changed during'):
            evidence.stage(self.source, destination, True)
        self.assertFalse(destination.exists())

    def test_workflow_stages_both_results_and_retains_full_artifacts(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    '.github/workflows/runner-image-artifacts.yml').read_text()
        for kind in ('public', 'private'):
            self.assertIn('id: build_' + kind, workflow)
            self.assertIn('steps.build_' + kind + ".outcome == 'success'", workflow)
            self.assertIn('stage_runner_evidence.py ' + kind, workflow)
            self.assertIn('name: runner-image-evidence-' + kind, workflow)
            self.assertIn('name: runner-image-' + kind + '-${{ github.run_id }}', workflow)
        self.assertEqual(workflow.count('compression-level: 9'), 2)
