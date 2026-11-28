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
from unittest import mock

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

    def commit(self) -> str:
        subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t',
                        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'],
                       cwd=self.root, check=True)
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root,
                                       text=True).strip()


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

    def test_anchors_do_not_accept_headings_in_examples(self):
        for fence in ('```', '~~~', '````'):
            with self.subTest(fence=fence):
                self.repo.write('a.md', f'[missing](#fake)\n{fence}md\n# Fake\n{fence}\n')
                self.assertTrue(self.repo.run('anchors'))

    def test_anchors_accept_duplicate_heading_suffixes_and_closing_hashes(self):
        self.repo.write('a.md', '# Same ###\n# Same\n[x](#same)\n[y](#same-1)\n')
        self.assertEqual(self.repo.run('anchors'), [])

    def test_links_and_anchors_resolve_root_directory_and_query_links(self):
        self.repo.write('README.md', '# Home\n')
        self.repo.write('docs/README.md', '# Docs\n')
        self.repo.write('docs/a.md', '[home](/README.md#home)\n[docs](./?view=1#docs)\n')
        self.assertEqual(self.repo.run('links'), [])
        self.assertEqual(self.repo.run('anchors'), [])

    def test_links_accept_a_directory_without_a_readme(self):
        # GitHub serves a directory listing at the directory path, so a link to a
        # source directory is not broken merely because it has no README.
        self.repo.write('docs/README.md', '# Docs\n')
        (self.repo.root / 'src' / 'package').mkdir(parents=True)
        subprocess.run(['git', 'add', 'src'], cwd=self.repo.root, check=True)
        self.repo.write('docs/a.md', 'See [the source](../src/package).\n')
        self.assertEqual(self.repo.run('links'), [])

    def test_links_still_reject_a_missing_directory(self):
        self.repo.write('docs/README.md', '# Docs\n')
        self.repo.write('docs/a.md', 'See [the source](../src/absent).\n')
        self.assertTrue(self.repo.run('links'))

    def test_links_reject_existing_targets_outside_the_repository(self):
        outside = self.repo.root.parent / (self.repo.root.name + '-outside.md')
        outside.write_text('# Outside\n')
        try:
            self.repo.write('a.md', f'[escape](../{outside.name})\n')
            self.assertTrue(self.repo.run('links'))
        finally:
            outside.unlink()

    def test_links_ignore_vendored_third_party_trees(self):
        # A vendor checkout is upstream's file; its links are not ours to fix.
        self.repo.write('docs/README.md', '# Docs\n')
        self.repo.write('vendor/crate/README.md', '[missing](../CHANGELOG.md)\n')
        self.assertEqual(self.repo.run('links'), [])

    def test_links_resolve_site_routes_to_their_page_source(self):
        # A published site links pages by route; /getting-started is not a
        # filesystem path but the page exists.
        self.repo.write('docs/src/pages/getting-started/index.md', '# Start\n')
        self.repo.write('docs/src/pages/index.md', '[start](/getting-started)\n')
        self.assertEqual(self.repo.run('links'), [])

    def test_links_still_reject_a_route_with_no_page(self):
        self.repo.write('docs/src/pages/index.md', '[gone](/nowhere)\n')
        self.assertTrue(self.repo.run('links'))

    def test_routes_keep_dotted_slugs_and_exact_files(self):
        self.repo.write('site/pages/guide.md', '# Guide\n')
        self.repo.write('site/pages/guide.v2.md', '# Guide v2\n')
        self.repo.write('site/pages/guide.html', '<h1>Guide</h1>\n')
        self.repo.write('downloads/manual.txt', 'Manual\n')
        self.repo.write('README.md', '[missing text file](/guide.txt)\n'
                        '[dotted slug](/guide.v2#guide-v2)\n'
                        '[HTML page](/guide.html)\n'
                        '[download](/downloads/manual.txt)\n')
        self.assertEqual(self.repo.run('links'), ['README.md: broken link /guide.txt'])
        self.assertEqual(self.repo.run('anchors'), [])

    def test_routes_project_generated_html_with_markdown_anchors(self):
        self.repo.write('site/pages/generated.md', '# Published heading\n')
        self.repo.write('site/pages/literal.md', '# Projected heading\n')
        self.repo.write('site/pages/literal.html.md', '# Literal heading\n')
        self.repo.write('README.md', '[generated](/generated.html#published-heading)\n'
                        '[literal slug](/literal.html#literal-heading)\n'
                        '[missing](/generated.html#absent)\n')
        self.assertEqual(self.repo.run('links'), [])
        self.assertEqual(self.repo.run('anchors'), ['README.md: missing anchor #absent'])

    def test_routes_discover_bounded_source_roots_once_per_check(self):
        self.repo.write('docs/site/src/pages/valid.md', '# Valid\n')
        self.repo.write('docs/site/src/extra/pages/too-deep.md', '# Too deep\n')
        self.repo.write('site/node_modules/addon/pages/vendor-page.md', '# Vendor page\n')
        self.repo.write('README.md', '[valid](/valid)\n[deep](/too-deep)\n'
                        '[vendor](/vendor-page)\n')
        with mock.patch.object(check_docs, 'page_roots', wraps=check_docs.page_roots) as discover:
            self.assertEqual(self.repo.run('links'), ['README.md: broken link /too-deep',
                                                      'README.md: broken link /vendor-page'])
            discover.assert_called_once_with(self.repo.root)
        self.repo.write('site/pages/vendor-page.md', '# Project page\n')
        self.assertEqual(self.repo.run('links'), ['README.md: broken link /too-deep'])

    def test_anchors_follow_github_heading_slugging(self):
        # GitHub turns every space into a hyphen, so "Intercom / Drop In" is
        # #intercom--drop-in, not #intercom-drop-in.
        self.repo.write('docs/a.md', '### Intercom / Drop In\n\nSee [it](#intercom--drop-in).\n')
        self.assertEqual(self.repo.run('anchors'), [])
        self.repo.write('docs/a.md', '### Intercom / Drop In\n\nSee [it](#intercom-drop-in).\n')
        self.assertTrue(self.repo.run('anchors'))

    def test_anchors_keep_the_text_inside_inline_code(self):
        # A heading written as ``### `python-minimal/` `` slugs to its contents,
        # so stripping code spans for link discovery must not affect anchors.
        self.repo.write('docs/a.md', '### `python-minimal/`\n\nSee [it](#python-minimal).\n')
        self.assertEqual(self.repo.run('anchors'), [])

    def test_links_ignore_html_examples_inside_inline_code(self):
        # A document explaining HTML must be able to show ``<a href="...">``.
        self.repo.write('docs/a.md', 'Use `<a href="...">` for navigation.\n')
        self.assertEqual(self.repo.run('links'), [])

    def test_links_ignore_rustdoc_intra_doc_links(self):
        self.repo.write('docs/a.md', 'See [`Variant`](struct@Variant) and [`T`](fn@run).\n')
        self.assertEqual(self.repo.run('links'), [])

    def test_templates_keep_the_consumer_anchor_exemption(self):
        self.repo.write('templates/a.md', '[consumer](#consumer-heading)\n')
        self.assertEqual(self.repo.run('links'), [])
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

    def test_index_follows_nested_navigation_and_cycles(self):
        self.repo.write('docs/README.md', '[Guide](guide/)\n')
        self.repo.write('docs/guide/README.md', '[Details](details.md)\n')
        self.repo.write('docs/guide/details.md', '[Back](/docs/README.md)\n')
        self.assertEqual(self.repo.run('index'), [])

    def permalink_fixture(self):
        self.repo.write('docs/README.md', '# Docs\n')
        self.repo.write('docs/page.md', '# Page\n')
        revision = self.repo.commit()
        subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/org/repo.git'],
                       cwd=self.repo.root, check=True)
        return f'https://github.com/org/repo/blob/{revision}/docs/page.md'

    def test_index_proves_same_repository_permalink_with_bounded_git_reads(self):
        link = self.permalink_fixture()
        self.repo.write('docs/README.md', f'[Page]({link})\n' * 500)
        with mock.patch.object(check_docs.subprocess, 'check_output',
                               wraps=subprocess.check_output) as calls:
            self.assertEqual(self.repo.run('index'), [])
        self.assertLessEqual(calls.call_count, 5)  # tracked() plus four batched reads
        for call in calls.call_args_list:
            if '--no-lazy-fetch' in call.args[0]:
                self.assertEqual(call.kwargs['env']['GIT_NO_REPLACE_OBJECTS'], '1')
        subprocess.run(['git', 'remote', 'set-url', 'origin', 'git@github.com:org/repo.git'],
                       cwd=self.repo.root, check=True)
        self.assertEqual(self.repo.run('index'), [])

    def test_index_refuses_foreign_unknown_and_mutable_permalinks(self):
        link = self.permalink_fixture()
        revision = link.split('/')[6]
        for bad in (link.replace('/org/repo/', '/other/repo/'),
                    link.replace(revision, 'f' * 40), link.replace(revision, 'main'),
                    link.replace('https:', 'http:'),
                    link.replace('/docs/page.md', '/docs/../docs/page.md')):
            with self.subTest(link=bad):
                self.repo.write('docs/README.md', f'[Page]({bad})\n')
                self.assertTrue(self.repo.run('index'))

    def test_index_refuses_equal_size_dirty_or_stale_permalink_content(self):
        link = self.permalink_fixture()
        self.repo.write('docs/README.md', f'[Page]({link})\n')
        self.repo.write('docs/page.md', '# Rage\n')  # Same length as the committed page.
        self.assertTrue(self.repo.run('index'))
        self.repo.commit()
        self.assertTrue(self.repo.run('index'))

    def test_self_permalink_never_proves_an_untracked_or_symlinked_page(self):
        link = self.permalink_fixture()
        self.repo.write('docs/README.md', f'[Page]({link})\n')
        page = self.repo.root / 'docs/page.md'
        documents = {(self.repo.root / 'docs/README.md').resolve(): f'[Page]({link})',
                     page.resolve(): '# Page\n'}
        subprocess.run(['git', 'rm', '--cached', '-q', 'docs/page.md'],
                       cwd=self.repo.root, check=True)
        self.assertEqual(check_docs.immutable_self_links(self.repo.root, documents), {})
        page.unlink()
        other = self.repo.root / 'docs/other.md'
        other.write_text('# Page\n')
        page.symlink_to(other)
        subprocess.run(['git', 'add', 'docs/page.md'], cwd=self.repo.root, check=True)
        documents[page.resolve()] = '# Page\n'
        self.assertEqual(check_docs.immutable_self_links(self.repo.root, documents), {})

    def test_self_permalink_refuses_a_historical_symlink_blob(self):
        self.repo.write('docs/README.md', '# Docs\n')
        target = self.repo.root / 'docs/page.md'
        target.symlink_to('note')
        subprocess.run(['git', 'add', 'docs/page.md'], cwd=self.repo.root, check=True)
        revision = self.repo.commit()
        target.unlink()
        self.repo.write('docs/page.md', 'note')  # Exactly the historical symlink bytes.
        subprocess.run(['git', 'remote', 'add', 'origin', 'https://github.com/org/repo.git'],
                       cwd=self.repo.root, check=True)
        self.repo.write('docs/README.md', f'[Page](https://github.com/org/repo/blob/{revision}/docs/page.md)\n')
        self.assertTrue(self.repo.run('index'))

    def test_fences_report_an_unbalanced_block(self):
        self.repo.write('docs/a.md', '```sh\necho hi\n')
        self.assertTrue(self.repo.run('fences'))

    def test_fences_require_a_matching_marker_and_length(self):
        for text in ('````md\n```\n', '~~~md\n```\n', '```sh\n```not-a-close\n'):
            with self.subTest(text=text):
                self.repo.write('a.md', text)
                self.assertTrue(self.repo.run('fences'))

    def test_long_fenced_examples_do_not_create_links_or_mermaid_blocks(self):
        self.repo.write('a.md', '````md\n```mermaid\n[example](missing.md)\n```\n````\n')
        self.assertEqual(self.repo.run('links'), [])
        self.assertEqual(self.repo.run('mermaid'), [])
        self.assertEqual(self.repo.run('fences'), [])

    def test_mermaid_reports_a_block_without_a_type(self):
        self.repo.write('docs/a.md', '```mermaid\nA --> B\n```\n')
        self.assertTrue(self.repo.run('mermaid'))

    def test_mermaid_accepts_a_known_diagram_type(self):
        self.repo.write('docs/a.md', '```mermaid\nflowchart LR\nA --> B\n```\n')
        self.assertEqual(self.repo.run('mermaid'), [])

    def test_mermaid_rejects_a_diagram_name_prefix(self):
        self.repo.write('a.md', '~~~mermaid\ngraphical nonsense\n~~~\n')
        self.assertTrue(self.repo.run('mermaid'))

    def test_version_reports_a_disagreement(self):
        self.repo.write('version', '0.4.1b2\n')
        self.repo.write('pyproject.toml', '[project]\nversion = "0.4.1b3"\n')
        self.assertTrue(self.repo.run('version'))

    def test_version_accepts_matching_files(self):
        self.repo.write('version', '0.4.1b3\n')
        self.repo.write('pyproject.toml', '[project]\nversion = "0.4.1b3"\n')
        self.assertEqual(self.repo.run('version'), [])

    def test_version_reads_project_metadata_not_an_unrelated_tool(self):
        self.repo.write('version', '1.0.0\n')
        self.repo.write('pyproject.toml', "[tool.example]\nversion = '2.0.0'\n"
                        "[project]\nversion = '1.0.0'\n")
        self.assertEqual(self.repo.run('version'), [])

    def test_version_reports_invalid_project_metadata(self):
        self.repo.write('pyproject.toml', '[project\n')
        self.assertIn('invalid TOML', self.repo.run('version')[0])

    def test_version_does_not_accept_a_different_prerelease_tag(self):
        self.repo.write('version', '0.4.1b3\n')
        self.repo.write('pyproject.toml', '[project]\nversion = "0.4.1b3"\n')
        subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t',
                        'commit', '-qm', 'x'], cwd=self.repo.root, check=True)
        subprocess.run(['git', 'tag', 'v0.4.1-beta.2'], cwd=self.repo.root, check=True)
        self.assertTrue(self.repo.run('version'))

    def test_version_compares_a_beta_tag_without_a_false_positive(self):
        self.repo.write('version', '0.4.1b3\n')
        self.repo.write('pyproject.toml', '[project]\nversion = "0.4.1b3"\n')
        subprocess.run(['git', '-c', 'user.email=t@example.com', '-c', 'user.name=t',
                        'commit', '-qm', 'x'], cwd=self.repo.root, check=True)
        subprocess.run(['git', 'tag', 'v0.4.1-beta.3'], cwd=self.repo.root, check=True)
        self.assertEqual(self.repo.run('version'), [])


if __name__ == '__main__':
    unittest.main()
