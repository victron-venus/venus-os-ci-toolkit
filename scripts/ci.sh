#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/check_test_environment.py
python3 -m unittest discover -s tests -p "test_*.py" -v
python3 scripts/actionlint_compat.py
python3 -m ruff check --config ruff.toml .
