#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DEFAULT_VENV_DIR="$ROOT/.venv"
RAW_VENV_DIR="${ORCA_AUTO_VENV:-$DEFAULT_VENV_DIR}"
if ! LEXICAL_VENV_DIR="$(realpath -ms -- "$RAW_VENV_DIR")"; then
  echo "[check] ERROR: Cannot normalize virtual environment path: $RAW_VENV_DIR" >&2
  exit 1
fi
if ! VENV_DIR="$(realpath -m -- "$RAW_VENV_DIR")"; then
  echo "[check] ERROR: Cannot resolve virtual environment path: $RAW_VENV_DIR" >&2
  exit 1
fi
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
  local marker="$VENV_DIR/pyvenv.cfg"
  [[ -d "$VENV_DIR" && -f "$marker" && -s "$marker" && ! -L "$marker" ]] || return 1
  [[ -x "$VENV_PY" ]] || return 1
  "$VENV_PY" - "$VENV_DIR" <<'PY' >/dev/null 2>&1
import os
import sys

expected = os.path.realpath(sys.argv[1])
prefix = os.path.realpath(sys.prefix)
base_prefix = os.path.realpath(sys.base_prefix)
valid = sys.version_info >= (3, 11) and prefix != base_prefix and prefix == expected
raise SystemExit(0 if valid else 1)
PY
}

repo_default_venv_is_repairable() {
  [[ "$RAW_VENV_DIR" == "$DEFAULT_VENV_DIR" ]] || return 1
  [[ "$LEXICAL_VENV_DIR" == "$DEFAULT_VENV_DIR" && ! -L "$RAW_VENV_DIR" ]] || return 1
  [[ -d "$VENV_DIR" && ! -L "$VENV_DIR" && -O "$VENV_DIR" ]] || return 1

  local marker="$VENV_DIR/pyvenv.cfg"
  [[ -f "$marker" && -s "$marker" && ! -L "$marker" && -O "$marker" ]]
}

if [[ "$LEXICAL_VENV_DIR" == "$DEFAULT_VENV_DIR" && -L "$LEXICAL_VENV_DIR" ]]; then
  echo "[check] ERROR: Refusing symlinked repository virtual environment: $RAW_VENV_DIR" >&2
  echo "[check] Point ORCA_AUTO_VENV at the real virtual environment directory instead." >&2
  exit 1
fi

# A usable venv needs no bootstrap interpreter, so the gate completes on a
# minimal PATH; find_python runs only when the venv must be (re)created.
if ! venv_is_usable; then
  recreate_venv=0
  if [[ -e "$VENV_DIR" || -L "$VENV_DIR" ]]; then
    if ! repo_default_venv_is_repairable; then
      echo "[check] ERROR: Refusing to replace unsafe virtual environment target: $VENV_DIR" >&2
      echo "[check] Automatic repair is limited to the owned, non-symlinked" >&2
      echo "[check] repository venv at $DEFAULT_VENV_DIR with an owned pyvenv.cfg marker." >&2
      echo "[check] Repair or remove this target manually, or choose a path that does not exist." >&2
      echo "[check] ORCA_AUTO_VENV must name a virtual environment (a pyvenv.cfg beside bin/python);" >&2
      echo "[check] a conda environment or a bare interpreter tree is not accepted." >&2
      exit 1
    fi
    recreate_venv=1
  fi

  PYTHON="$(find_python)" || {
    echo "[check] ERROR: Python 3.11 or newer is required." >&2
    echo "[check] Set PYTHON_BIN=/path/to/python3.11 and rerun." >&2
    exit 1
  }
  if [[ "$recreate_venv" == "1" ]]; then
    echo "[check] Recreating unusable virtual environment: $VENV_DIR"
    rm -rf -- "$VENV_DIR"
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

echo "[check] import-linter"
"$VENV_DIR/bin/lint-imports"

echo "[check] pytest"
"$VENV_PY" -m pytest --cov --cov-report=term-missing -q "$@"
