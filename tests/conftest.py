"""Stop pytest once when a toolkit run lacks the hash-locked requirements.

Without this, each module that imports PyYAML, ruamel.yaml or PyNaCl reports its
own import error, which buries the one line that fixes the environment. Fixture
projects that live beneath the checkout root install their own dependencies, so
only a run started from the checkout root is checked here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import check_test_environment  # noqa: E402

if Path.cwd().resolve() == ROOT:
    if check_test_environment.missing_modules():
        check_test_environment.main()
        pytest.exit("Install the hash-locked test requirements and rerun.", returncode=2)
    check_test_environment.report_warnings()
