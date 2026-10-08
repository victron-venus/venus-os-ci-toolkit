"""Exercise report, event, permission and generated-code boundaries offline."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import coverage_reports as reports  # noqa: E402 - Import the source scripts after adding their directory.
import render_coverage_upload as renderer  # noqa: E402 - Import the source scripts after adding their directory.

XML = '<coverage><packages><package><classes><class filename="src/main.py"><lines><line number="1" hits="0"/><line number="2" hits="3"/></lines></class></classes></package></packages></coverage>'
GO = "mode: atomic\nexample.org/app/main.go:1.1,3.2 2 0\nexample.org/app/main.go:4.1,6.2 1 3\n"
LCOV = "TN:\nSF:src/main.ts\nFN:1,hello\nFNDA:1,hello\nDA:1,0\nDA:2,3\nLF:2\nLH:1\nend_of_record\n"


def context(event_name="push", *, fork=False):
    environment = {
        "COVERAGE_ARTIFACT": "coverage-python",
        "COVERAGE_FILE": "coverage.xml",
        "COVERAGE_FORMAT": "cobertura",
        "COVERAGE_FLAGS": "python",
        "COVERAGE_MODE": "tokenless" if fork else "oidc",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
    }
    event = {
        "repository": {
            "full_name": "example/project",
            "private": False,
            "default_branch": "main",
        },
        "after": "a" * 40,
        "deleted": False,
        "pull_request": {
            "number": 42,
            "base": {"repo": {"full_name": "example/project"}},
            "head": {
                "sha": "b" * 40,
                "repo": {"full_name": "other/project" if fork else "example/project"},
            },
        },
        "merge_group": {"head_sha": "a" * 40, "base_ref": "refs/heads/main"},
    }
    return environment, event


class CoverageContextTests(unittest.TestCase):
    def test_supported_trusted_events_and_attempt_suffix(self):
        for name in [
            "push",
            "workflow_dispatch",
            "schedule",
            "merge_group",
            "pull_request",
        ]:
            with self.subTest(name=name):
                result = reports.configuration(*context(name))
                self.assertEqual(result["artifact_name"], "coverage-python-2")
                self.assertEqual(result["mode"], "oidc")
                self.assertEqual(result["tested_sha"], "a" * 40)
                self.assertEqual(
                    result["commit"], ("b" if name == "pull_request" else "a") * 40
                )

    def test_fork_commit_is_distinct_from_tested_merge(self):
        result = reports.configuration(*context("pull_request", fork=True))
        self.assertEqual(result["mode"], "tokenless")
        self.assertEqual(result["pull_request"], "42")
        self.assertNotEqual(result["commit"], result["tested_sha"])

    def test_invalid_inputs_fail_closed(self):
        invalid = {
            "COVERAGE_ARTIFACT": ["", "../report", "x\nmode=oidc", "x" * 101],
            "COVERAGE_FILE": [
                "",
                "../coverage.xml",
                "/tmp/report",
                "a//b",
                "a/./b",
                "a/../b",
                "*.xml",
                "x,y",
                "x\\y",
                "file\nother",
            ],
            "COVERAGE_FORMAT": ["xml", "", "lcov\n"],
            "COVERAGE_FLAGS": ["", "a,b", "x\ny", "../flag"],
            "GITHUB_RUN_ATTEMPT": ["0", "-1", "1\n", "one", "1٢"],
            "GITHUB_RUN_ID": ["0", "-1", "1\n", "1٢"],
            "GITHUB_SHA": ["a" * 39, "f" * 41, "g" * 40],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    env, event = context()
                    env[key] = value
                    with self.assertRaises(ValueError):
                        reports.configuration(env, event)

    def test_unsafe_event_and_repository_contexts(self):
        for name in [
            "pull_request_target",
            "workflow_run",
            "issue_comment",
            "workflow_call",
            "release",
        ]:
            env, event = context(name)
            with self.subTest(event=name), self.assertRaises(ValueError):
                reports.configuration(env, event)
        for key, value in [
            ("private", True),
            ("private", None),
            ("full_name", "other/project"),
        ]:
            env, event = context()
            event["repository"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                reports.configuration(env, event)
        for event_name in ["push", "schedule", "workflow_dispatch"]:
            env, event = context(event_name)
            env["GITHUB_REF"] = "refs/heads/topic"
            with self.subTest(event=event_name), self.assertRaises(ValueError):
                reports.configuration(env, event)

    def test_mismatched_push_pr_and_merge_group(self):
        cases = [
            ("push", ("after",), "c" * 40),
            ("push", ("deleted",), True),
            (
                "pull_request",
                ("pull_request", "base", "repo", "full_name"),
                "other/repo",
            ),
            ("pull_request", ("pull_request", "head", "sha"), "bad"),
            ("pull_request", ("pull_request", "head", "repo", "full_name"), ""),
            ("pull_request", ("pull_request", "number"), True),
            ("merge_group", ("merge_group", "head_sha"), "c" * 40),
            ("merge_group", ("merge_group", "base_ref"), "refs/heads/topic"),
        ]
        for name, keys, value in cases:
            env, event = context(name)
            target = event
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = value
            with self.subTest(name=name, keys=keys), self.assertRaises(ValueError):
                reports.configuration(env, event)

    def test_cli_rejects_wrong_trust_job_before_outputs_even_optimized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, event = context("pull_request", fork=True)
            env.update(
                COVERAGE_MODE="oidc",
                GITHUB_EVENT_PATH=str(root / "event.json"),
                GITHUB_OUTPUT=str(root / "output"),
            )
            (root / "event.json").write_text(json.dumps(event))
            for option in [[], ["-O"]]:
                result = subprocess.run(
                    [
                        sys.executable,
                        *option,
                        str(ROOT / "scripts/coverage_reports.py"),
                        "prepare",
                    ],
                    env={**os.environ, **env},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Wrong upload trust job", result.stderr)
                self.assertFalse((root / "output").exists())


class CoverageReportTests(unittest.TestCase):
    def test_cobertura_generator_headers_and_comments(self):
        # Istanbul and gcovr emit this external DTD; coverage.py emits comments.
        # See their cobertura/index.js, cobertura/write.py and xmlreport.py.
        headers = [
            '<!DOCTYPE coverage SYSTEM "http://cobertura.sourceforge.net/xml/coverage-04.dtd">',
            "<!DOCTYPE coverage SYSTEM 'http://cobertura.sourceforge.net/xml/coverage-04.dtd'>",
            '<!DOCTYPE coverage PUBLIC "-//Cobertura//DTD Coverage 04//EN" "coverage-04.dtd">',
            (
                "<!-- Generated by coverage.py: https://coverage.readthedocs.io/en/coverage-5.0a2 -->\n"
                "<!-- Based on https://raw.githubusercontent.com/cobertura/web/master/htdocs/xml/coverage-04.dtd -->"
            ),
            '<!-- These are comments, not declarations: <!DOCTYPE coverage> <!ENTITY x "y"> -->',
        ]
        for header in headers:
            raw = '<?xml version="1.0"?>\n' + header + "\n" + XML
            with self.subTest(header=header):
                self.assertEqual(reports.cobertura(raw), 2)

    def test_cobertura_external_dtd_never_reads_network_or_files(self):
        for location in ["https://invalid.example/coverage.dtd", "file:///etc/passwd"]:
            raw = f'<!DOCTYPE coverage SYSTEM "{location}">' + XML
            with (
                self.subTest(location=location),
                patch(
                    "builtins.open", side_effect=AssertionError("unexpected file read")
                ),
                patch(
                    "socket.socket", side_effect=AssertionError("unexpected network")
                ),
            ):
                self.assertEqual(reports.cobertura(raw), 2)

    def test_cobertura_dtd_and_entity_attacks_fail_closed(self):
        external = '<!DOCTYPE coverage SYSTEM "https://invalid.example/coverage.dtd">'
        cases = [
            "<!DOCTYPE coverage []>" + XML,
            '<!DOCTYPE coverage SYSTEM "coverage.dtd" []>' + XML,
            '<!DOCTYPE coverage [<!ENTITY x "expanded">]>' + XML,
            '<!DOCTYPE coverage [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + XML,
            '<!DOCTYPE coverage [<!ENTITY % x SYSTEM "https://invalid.example/evil">%x;]>'
            + XML,
            '<!ENTITY x "expanded">' + XML,
            external + external + XML,
            '<!DOCTYPE other SYSTEM "coverage.dtd">' + XML,
            "<!DOCTYPE coverage>" + XML,
            '<!DOCTYPE coverage SYSTEM "">' + XML,
            external + XML.replace('filename="src/main.py"', 'filename="&external;"'),
            external + XML.replace("<lines>", "<lines>&external;"),
            XML.replace("<coverage>", '<coverage xmlns="urn:not-cobertura">'),
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                reports.cobertura(raw)

    def test_real_nonempty_formats_allow_zero_hits(self):
        for format_name, raw, count in [
            ("cobertura", XML, 2),
            ("go", GO, 2),
            ("lcov", LCOV, 2),
        ]:
            with (
                self.subTest(format=format_name),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "nested/report"
                path.parent.mkdir()
                path.write_text(raw)
                result = reports.validate_report(
                    Path(directory), "nested/report", format_name
                )
                self.assertEqual(result["measured"], count)
                self.assertEqual(result["bytes"], len(raw.encode()))
                self.assertEqual(len(result["sha256"]), 64)

    def test_empty_malformed_and_nonfinite_reports(self):
        cases = [
            ("cobertura", "<coverage/>"),
            ("cobertura", XML.replace('hits="0"', 'hits="NaN"')),
            ("cobertura", XML.replace('hits="0"', 'hits="٢"')),
            ("cobertura", XML.replace('number="1"', 'number="0"')),
            ("cobertura", '<!DOCTYPE coverage [<!ENTITY x "bad">]>' + XML),
            ("cobertura", '<!ENTITY x SYSTEM "file:///etc/passwd">' + XML),
            ("go", "mode: atomic\n"),
            ("go", GO.replace("mode: atomic", "mode: invalid")),
            ("go", GO.replace("1.1,3.2", "3.2,1.1")),
            ("go", GO.replace("2 0", "2 -1")),
            ("go", GO.replace("2 0", "٢ 0")),
            ("go", GO + GO.splitlines()[1].replace("2 0", "3 0") + "\n"),
            ("go", "mode: atomic\nempty.go:1.1,1.1 0 0\n"),
            ("lcov", "TN:\n"),
            ("lcov", "SF:src/a.ts\nend_of_record\n"),
            ("lcov", LCOV.replace("end_of_record\n", "")),
            ("lcov", LCOV.replace("DA:1,0", "DA:1,NaN")),
            ("lcov", LCOV.replace("DA:1,0", "DA:1,٢")),
            ("lcov", LCOV.replace("LF:2", "LF:3")),
            ("lcov", LCOV.replace("LH:1", "LH:2")),
            ("lcov", LCOV.replace("DA:2,3", "DA:1,3")),
        ]
        for format_name, raw in cases:
            with (
                self.subTest(format=format_name, raw=raw),
                tempfile.TemporaryDirectory() as directory,
            ):
                (Path(directory) / "report").write_text(raw)
                with self.assertRaises(ValueError):
                    reports.validate_report(directory, "report", format_name)

    def test_valid_empty_blocks_files_and_repeated_samples(self):
        # Go's own profile parser merges repeated coordinates when NumStmt agrees.
        go = GO + "empty.go:1.1,1.1 0 0\n" + GO.splitlines()[1] + "\n"
        self.assertEqual(reports.go_profile(go), 2)
        empty_lcov = "SF:src/empty.ts\nLF:0\nLH:0\nend_of_record\n"
        self.assertEqual(reports.lcov(empty_lcov + LCOV), 2)
        # Separate LCOV test-name records may measure the same source file.
        self.assertEqual(reports.lcov(LCOV + LCOV), 4)

    def test_only_one_confined_regular_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "report"
            with self.assertRaises(ValueError):
                reports.validate_report(root, "report", "go")
            path.write_text(GO)
            (root / "extra").write_text("extra")
            with self.assertRaises(ValueError):
                reports.validate_report(root, "report", "go")
            (root / "extra").unlink()
            path.unlink()
            path.symlink_to("missing")
            with self.assertRaises(ValueError):
                reports.validate_report(root, "report", "go")
            path.unlink()
            path.write_text("")
            with self.assertRaises(ValueError):
                reports.validate_report(root, "report", "go")
            path.write_text(GO)
            with (
                patch.object(reports, "MAX_REPORT_BYTES", 4),
                self.assertRaises(ValueError),
            ):
                reports.validate_report(root, "report", "go")
            path.write_bytes(b"\xff\x00")
            with self.assertRaises(ValueError):
                reports.validate_report(root, "report", "go")


class CoverageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "scripts/coverage_reports.py").read_text()
        self.workflow = yaml.safe_load(renderer.render(self.source))
        for job in self.workflow["jobs"].values():
            self.assertEqual(job["steps"][0], renderer.harden_step())
            # Keep the ordering contract for validation, download and checkout
            # independent of the verified runner protection that precedes it.
            job["steps"] = job["steps"][1:]

    def test_generated_bytes_and_embedded_program_match(self):
        self.assertEqual(
            renderer.render(self.source),
            (ROOT / ".github/workflows/coverage-upload.yml").read_text(),
        )
        for job in self.workflow["jobs"].values():
            run = next(step["run"] for step in job["steps"] if "run" in step)
            embedded = run.split(renderer.DELIMITER + "'\n", 1)[1].split(
                renderer.DELIMITER + "\n", 1
            )[0]
            self.assertEqual(embedded, self.source)
        with self.assertRaises(ValueError):
            renderer.render(self.source + renderer.DELIMITER + "\n")

    def test_embedded_preflight_and_report_execute_without_checkout(self):
        for mode, job in self.workflow["jobs"].items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env, event = context("pull_request", fork=mode == "tokenless")
                env.update(
                    RUNNER_TEMP=str(root),
                    GITHUB_EVENT_PATH=str(root / "event.json"),
                    GITHUB_OUTPUT=str(root / "output"),
                    GITHUB_STEP_SUMMARY=str(root / "summary"),
                )
                (root / "event.json").write_text(json.dumps(event))
                report_root = root / "coverage-report"
                report_root.mkdir()
                (report_root / "coverage.xml").write_text(XML)
                for index in [0, 2]:
                    result = subprocess.run(
                        [
                            "bash",
                            "-e",
                            "-o",
                            "pipefail",
                            "-c",
                            job["steps"][index]["run"],
                        ],
                        cwd=root,
                        env={**os.environ, **env},
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                output = (root / "output").read_text()
                self.assertIn("artifact_name=coverage-python-2\n", output)
                self.assertIn("mode=" + mode + "\n", output)
                self.assertIn("Tested checkout", (root / "summary").read_text())
                (report_root / "coverage.xml").write_text("<coverage/>")
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(root / "coverage_reports.py"),
                        "report",
                        "--directory",
                        str(report_root),
                    ],
                    env={**os.environ, **env},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_permissions_are_isolated_and_reports_never_optional(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(self.workflow["jobs"]), {"oidc", "tokenless"})
        for mode, job in self.workflow["jobs"].items():
            expected = {
                "contents": "read",
                **({"id-token": "write"} if mode == "oidc" else {}),
            }
            self.assertEqual(job["permissions"], expected)
            self.assertNotIn("continue-on-error", job)
            self.assertEqual(job["env"]["COVERAGE_MODE"], mode)
            for step in job["steps"]:
                if step.get("id") != "upload":
                    self.assertNotIn("continue-on-error", step)
            upload = next(s for s in job["steps"] if s.get("id") == "upload")
            pins = {
                p["packageName"]: p["digest"]
                for p in json.loads((ROOT / ".github/action-pins.json").read_text())
            }
            self.assertEqual(
                upload["uses"],
                renderer.CODECOV_PACKAGE + "@" + pins[renderer.CODECOV_PACKAGE],
            )
            self.assertIs(upload["with"]["use_oidc"], mode == "oidc")
            self.assertIs(upload["with"]["fail_ci_if_error"], True)
            self.assertIs(upload["with"]["disable_search"], True)
            self.assertFalse(
                set(upload["with"]) & {"token", "binary", "skip_validation", "url"}
            )
            self.assertEqual(
                upload["continue-on-error"],
                "${{ !inputs.required }}" if mode == "oidc" else True,
            )
            self.assertEqual(
                job["outputs"]["upload-outcome"], "${{ steps.upload.outcome }}"
            )

    def test_artifact_pin_checkout_order_and_source_attribution(self):
        pins = {
            p["packageName"]: p["digest"]
            for p in json.loads((ROOT / ".github/action-pins.json").read_text())
        }
        for job in self.workflow["jobs"].values():
            steps = job["steps"]
            self.assertEqual(
                steps[1]["uses"],
                "actions/download-artifact@" + pins["actions/download-artifact"],
            )
            self.assertEqual(
                steps[1]["with"],
                {
                    "name": "${{ steps.contract.outputs.artifact_name }}",
                    "path": "${{ runner.temp }}/coverage-report",
                },
            )
            self.assertEqual(
                steps[3]["uses"], "actions/checkout@" + pins["actions/checkout"]
            )
            self.assertEqual(
                steps[3]["with"],
                {
                    "repository": "${{ github.repository }}",
                    "ref": "${{ github.sha }}",
                    "persist-credentials": False,
                },
            )
            self.assertIn("report --directory", steps[2]["run"])
            self.assertEqual(
                steps[4]["with"]["override_commit"],
                "${{ steps.contract.outputs.commit }}",
            )
            self.assertEqual(
                steps[4]["with"]["override_pr"],
                "${{ steps.contract.outputs.pull_request }}",
            )
            self.assertIn("not independently verified", steps[5]["run"])
            self.assertIn("::warning::", steps[5]["run"])


if __name__ == "__main__":
    unittest.main()
