#!/usr/bin/env bash
# Run the isolated unit suite locally through the repository's uv environment.

set -e

echo "Running unit test suite"
echo "======================="
echo

if ! command -v uv > /dev/null 2>&1; then
    echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi

echo "Preparing uv environment..."
uv venv -q
uv pip install -q -r requirements.txt pytest

echo "Running pytest..."
uv run --no-sync python -m pytest \
    tests/ \
    --ignore=tests/integration \
    -v \
    --cov=app \
    --cov-report=term-missing \
    --cov-report=html

echo
echo "Tests complete."
echo "Coverage report: htmlcov/index.html"
