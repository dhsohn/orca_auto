#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
cd -- "${REPO_ROOT}"

exec "${PYTHON:-python3}" -m orca_auto.cli smoke --repo-root "${REPO_ROOT}" "$@"
