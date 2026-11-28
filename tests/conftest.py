"""Stop pytest before collection when the hash-locked test requirements are absent.

Without this, each module that imports PyYAML, ruamel.yaml or PyNaCl reports its
own import error, which buries the one line that fixes the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import check_test_environment  # noqa: E402

if check_test_environment.missing_modules():
    check_test_environment.main()
    pytest.exit("Install the hash-locked test requirements and rerun.", returncode=2)
