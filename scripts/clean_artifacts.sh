#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

find . \( -path ./.git -o -path ./.venv \) -prune -o \
  \( -type d \( \
    -name '__pycache__' -o \
    -name '.pytest_cache' -o \
    -name '.mypy_cache' -o \
    -name '.ruff_cache' -o \
    -name 'htmlcov' -o \
    -name '*.egg-info' \
  \) -prune -exec rm -rf {} + \)

rm -f .coverage .coverage.*
rm -rf build dist

# Git cannot track an empty directory, so a package deleted in a commit leaves
# its directory behind in every existing checkout. Python then imports it as a
# namespace package, which makes a removed module look importable. Prune the
# leftovers under the source and test trees; both loops run because emptying a
# leaf can leave its parent empty in turn.
while find src tests -type d -empty -print -quit | grep -q .; do
  find src tests -type d -empty -delete
done
