#!/usr/bin/env python3
"""Embed the reviewed report validator in the reusable uploader without checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner_selection import runner_labels  # pylint: disable=wrong-import-position
from workflow_hardening import harden_step  # pylint: disable=wrong-import-position

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / ".github/workflows/coverage-upload.yml"
SOURCE = ROOT / "scripts/coverage_reports.py"
DELIMITER = "TOOLKIT_COVERAGE_REPORTS_PY"
CODECOV_PACKAGE = "codecov/codecov-action"


def render(source):
    """Generate two trust-separated jobs with one canonical validation program."""
    if not source.endswith("\n") or DELIMITER in source.splitlines():
        raise ValueError("Validator needs a final newline and unique heredoc delimiter")
    pins = {
        p["packageName"]: p["digest"]
        for p in json.loads((ROOT / ".github/action-pins.json").read_text())
    }
    jobs = {}
    for mode in ("oidc", "tokenless"):
        fork = mode == "tokenless"
        setup = (
            "umask 077\n"
            f"cat > \"$RUNNER_TEMP/coverage_reports.py\" <<'{DELIMITER}'\n"
            + source
            + DELIMITER
            + "\n"
            + 'python3 -I "$RUNNER_TEMP/coverage_reports.py" prepare\n'
        )
        jobs[mode] = {
            "name": "Coverage / " + ("public fork" if fork else "OIDC"),
            # Unknown/non-PR events enter the OIDC-side preflight and fail before
            # checkout, artifact access or token issuance, rather than all-skipped green.
            "if": "${{ github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository }}"
            if fork
            else "${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}",
            "runs-on": runner_labels(),
            "timeout-minutes": 10,
            "permissions": {
                "contents": "read",
                **({} if fork else {"id-token": "write"}),
            },
            "env": {
                "COVERAGE_ARTIFACT": "${{ inputs.artifact-name }}",
                "COVERAGE_FILE": "${{ inputs.report-file }}",
                "COVERAGE_FORMAT": "${{ inputs.format }}",
                "COVERAGE_FLAGS": "${{ inputs.flags }}",
                "COVERAGE_MODE": mode,
            },
            "outputs": {"upload-outcome": "${{ steps.upload.outcome }}"},
            "steps": [
                harden_step(),
                {
                    "name": "Validate public event and report contract",
                    "id": "contract",
                    "shell": "bash",
                    "run": setup,
                },
                {
                    "name": "Download this attempt's report",
                    "uses": "actions/download-artifact@"
                    + pins["actions/download-artifact"],
                    "with": {
                        "name": "${{ steps.contract.outputs.artifact_name }}",
                        "path": "${{ runner.temp }}/coverage-report",
                    },
                },
                {
                    "name": "Validate mandatory report",
                    "shell": "bash",
                    "run": 'python3 -I "$RUNNER_TEMP/coverage_reports.py" report --directory "$RUNNER_TEMP/coverage-report"',
                },
                {
                    "name": "Checkout tested source as data",
                    "uses": "actions/checkout@" + pins["actions/checkout"],
                    "with": {
                        "repository": "${{ github.repository }}",
                        "ref": "${{ github.sha }}",
                        "persist-credentials": False,
                    },
                },
                {
                    "name": "Publish coverage",
                    "id": "upload",
                    "uses": CODECOV_PACKAGE + "@" + pins[CODECOV_PACKAGE],
                    # Optionality covers the entire official uploader step, including
                    # setup/integrity errors. The wrapper never executes unverified code.
                    "continue-on-error": True if fork else "${{ !inputs.required }}",
                    "with": {
                        "files": "${{ runner.temp }}/coverage-report/${{ steps.contract.outputs.report_file }}",
                        "flags": "${{ steps.contract.outputs.flags }}",
                        "disable_search": True,
                        "fail_ci_if_error": True,
                        "use_oidc": not fork,
                        "override_commit": "${{ steps.contract.outputs.commit }}",
                        "override_pr": "${{ steps.contract.outputs.pull_request }}",
                        "plugins": "noop",
                    },
                },
                {
                    "name": "Record publication outcome",
                    "if": "${{ always() }}",
                    "shell": "bash",
                    "env": {"UPLOAD_OUTCOME": "${{ steps.upload.outcome }}"},
                    "run": """python3 - <<'PYTHON'
import os
from pathlib import Path
outcome = os.environ['UPLOAD_OUTCOME'] or 'not-run'
if outcome not in {'success', 'failure', 'cancelled', 'skipped', 'not-run'}:
    raise SystemExit('Invalid uploader outcome')
message = 'Uploader step: ' + outcome + '. Provider processing/acceptance is not independently verified by this workflow.'
if outcome == 'failure':
    print('::warning::Coverage uploader failed; inspect its setup/integrity/authentication/transport logs. Local report validation remains mandatory.')
with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
    summary.write(message + '\\n')
PYTHON
""",
                },
            ],
        }
    workflow = {
        "name": "Shared coverage upload",
        "on": {
            "workflow_call": {
                "inputs": {
                    "artifact-name": {
                        "description": "Base artifact name; the producer must append a hyphen and the current run attempt",
                        "type": "string",
                        "required": True,
                    },
                    "report-file": {
                        "description": "Single relative report file inside the artifact",
                        "type": "string",
                        "default": "coverage.xml",
                    },
                    "format": {
                        "description": "Report format: cobertura, go or lcov",
                        "type": "string",
                        "required": True,
                    },
                    "flags": {
                        "description": "One stable alphanumeric coverage flag (hyphen/underscore allowed)",
                        "type": "string",
                        "required": True,
                    },
                    "required": {
                        "description": "Make trusted uploader-step failure blocking; fork publication remains informational",
                        "type": "boolean",
                        "default": False,
                    },
                },
                "outputs": {
                    "upload-outcome": {
                        "description": "Official uploader step outcome; success does not independently prove provider processing",
                        "value": "${{ jobs.oidc.outputs.upload-outcome || jobs.tokenless.outputs.upload-outcome }}",
                    }
                },
            }
        },
        "permissions": {"contents": "read"},
        "jobs": jobs,
    }

    # Literal blocks keep the embedded code reviewable and byte-equivalent.
    class Dumper(yaml.SafeDumper):
        pass

    def string(dumper, value):
        return dumper.represent_scalar(
            "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
        )

    Dumper.add_representer(str, string)
    rendered = (
        "# Generated by scripts/render_coverage_upload.py; edit scripts/coverage_reports.py or the renderer.\n"
        + "# Validator SHA-256: "
        + hashlib.sha256(source.encode()).hexdigest()
        + "\n"
        + "# Only uploader-step failures may be informational; artifact/report failures always fail.\n"
        + yaml.dump(workflow, Dumper=Dumper, sort_keys=False, width=1000)
    )
    references = {
        f"{pin['packageName']}@{pin['digest']}": pin["version"]
        for pin in json.loads((ROOT / ".github/action-pins.json").read_text())
    }
    for reference, version in references.items():
        rendered = rendered.replace(
            f"uses: {reference}\n", f"uses: {reference} # {version}\n"
        )
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render(SOURCE.read_text())
    if args.check:
        if not TARGET.is_file() or TARGET.read_text() != expected:
            parser.exit(
                1, "Coverage workflow differs: run scripts/render_coverage_upload.py\n"
            )
    else:
        TARGET.write_text(expected)
    print("Coverage uploader matches its canonical validator.")


if __name__ == "__main__":
    main()
