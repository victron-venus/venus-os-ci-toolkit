"""Tests for the shared documentation checker.

Each check is exercised against a temporary repository so a regression in the
rule is caught here rather than in a consumer repository.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_docs', ROOT / 'scripts' / 'check_docs.py')
check_docs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_docs)


class Repository:
    """A throwaway git repository with a documentation tree."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        subprocess.run(['git', 'add', name], cwd=self.root, check=True)
        return path

    def run(self, check: str) -> list[str]:
        files = check_docs.tracked(self.root)
        return check_docs.CHECKS[check](self.root, files)

    def close(self) -> None:
        self.temp.cleanup()


class CheckDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repository()

    def tearDown(self) -> None:
        self.repo.close()

    def test_links_reports_a_missing_target(self):
        self.repo.write('docs/a.md', 'See [missing](b.md).\n')
        self.assertTrue(self.repo.run('links'))

    def test_links_accepts_a_sibling_and_ignores_fenced_code(self):
        self.repo.write('docs/a.md', 'See [ok](b.md).\n\n```sh\n[x](nope.md)\n```\n')
        self.repo.write('docs/b.md', '# B\n')
        self.assertEqual(self.repo.run('links'), [])

    def test_anchors_report_a_missing_fragment(self):
        self.repo.write('docs/a.md', 'See [x](b.md#absent).\n')
        self.repo.write('docs/b.md', '# Present\n')
        self.assertTrue(self.repo.run('anchors'))

    def test_anchors_accept_a_heading_slug(self):
        self.repo.write('docs/a.md', 'See [x](b.md#present).\n')
        self.repo.write('docs/b.md', '# Present\n')
        self.assertEqual(self.repo.run('anchors'), [])

    def test_svg_rejects_malformed_xml(self):
        self.repo.write('docs/a.svg', '<svg><rect></svg>')
        self.assertTrue(self.repo.run('svg'))

    def test_index_reports_an_orphan_document(self):
        self.repo.write('docs/README.md', '# Docs\n\n- [A](a.md)\n')
        self.repo.write('docs/a.md', '# A\n')
        self.repo.write('docs/orphan.md', '# Orphan\n')
        errors = self.repo.run('index')
        self.assertEqual(len(errors), 1)
        self.assertIn('orphan', errors[0])

    def test_index_ignores_shared_boilerplate(self):
        self.repo.write('docs/README.md', '# Docs\n')
        self.repo.write('docs/quality.md', '# Quality\n')
        self.assertEqual(self.repo.run('index'), [])

    def test_fences_report_an_unbalanced_block(self):
        self.repo.write('docs/a.md', '```sh\necho hi\n')
        self.assertTrue(self.repo.run('fences'))

    def test_mermaid_reports_a_block_without_a_type(self):
        self.repo.write('docs/a.md', '```mermaid\nA --> B\n```\n')
        self.assertTrue(self.repo.run('mermaid'))

    def test_mermaid_accepts_a_known_diagram_type(self):
        self.repo.write('docs/a.md', '```mermaid\nflowchart LR\nA --> B\n```\n')
        self.assertEqual(self.repo.run('mermaid'), [])

    def test_version_reports_a_disagreement(self):
        self.repo.write('version', '0.4.1b2\n')
        self.repo.write('pyproject.toml', '[project]\nversion = "0.4.1b3"\n')
        self.assertTrue(self.repo.run('version'))

    def test_version_accepts_matching_files(self):
        self.repo.write('version', '0.4.1b3\n')
        self.repo.write('pyproject.toml', '[project]\nversion = "0.4.1b3"\n')
        self.assertEqual(self.repo.run('version'), [])

    def test_version_compares_a_beta_tag_without_a_false_positive(self):
        self.repo.write('version', '0.4.1b3\n')
        self.repo.write('pyproject.toml', '[project]\nversion = "0.4.1b3"\n')
        subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t',
                        'commit', '-qm', 'x'], cwd=self.repo.root, check=True)
        subprocess.run(['git', 'tag', 'v0.4.1-beta.3'], cwd=self.repo.root, check=True)
        self.assertEqual(self.repo.run('version'), [])


if __name__ == '__main__':
    unittest.main()
