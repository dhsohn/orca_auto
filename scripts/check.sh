#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV_DIR="${ORCA_AUTO_VENV:-$ROOT/.venv}"
VENV_PY="$VENV_DIR/bin/python"

find_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    printf '%s\n' "$PYTHON_BIN"
    return 0
  fi

  for candidate in python python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" - <<'PY' >/dev/null 2>&1; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

venv_is_usable() {
  [[ -x "$VENV_PY" ]] || return 1
  "$VENV_PY" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

PYTHON="$(find_python)" || {
  echo "[check] ERROR: Python 3.11 or newer is required." >&2
  echo "[check] Set PYTHON_BIN=/path/to/python3.11 and rerun." >&2
  exit 1
}

if ! venv_is_usable; then
  if [[ -e "$VENV_DIR" || -L "$VENV_DIR" ]]; then
    echo "[check] Recreating unusable virtual environment: $VENV_DIR"
    rm -rf "$VENV_DIR"
  else
    echo "[check] Creating virtual environment: $VENV_DIR"
  fi
  "$PYTHON" -m venv "$VENV_DIR"
fi

echo "[check] Using Python: $("$VENV_PY" -c 'import sys; print(sys.executable)')"
if [[ "${ORCA_AUTO_CHECK_SKIP_INSTALL:-0}" != "1" ]]; then
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -c constraints-dev.txt -e '.[dev]'
fi

echo "[check] Ruff"
"$VENV_PY" -m ruff check .

echo "[check] Ruff format"
"$VENV_PY" -m ruff format --check .

echo "[check] mypy"
"$VENV_PY" -m mypy

echo "[check] pytest"
"$VENV_PY" -m pytest --cov --cov-report=term-missing -q "$@"
