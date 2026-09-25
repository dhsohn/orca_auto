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

# Interpreter discovery for (re)creating the venv. PYTHON_BIN is an explicit
# override and is used as given. Otherwise PATH is searched first, then a few
# common install locations, so the gate also works from a minimal PATH (for
# example /usr/bin:/bin) where the system python3 may be too old.
PYTHON_NAMES=(python3.13 python3.12 python3.11 python3)
PYTHON_DIRS=(
  "${HOME:+$HOME/.local/bin}"
  "${HOME:+$HOME/miniconda3/bin}"
  "${HOME:+$HOME/anaconda3/bin}"
  /usr/local/bin
  /opt/homebrew/bin
  /opt/conda/bin
)
PYTHON=""
PYTHON_TRIED=()

python_is_suitable() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys

if sys.version_info < (3, 11):
    raise SystemExit(1)
import ensurepip  # noqa: F401  (python -m venv needs it to bootstrap pip)
import venv  # noqa: F401
PY
}

try_python() {
  local candidate="$1"
  local seen
  for seen in ${PYTHON_TRIED[@]+"${PYTHON_TRIED[@]}"}; do
    [[ "$seen" == "$candidate" ]] && return 1
  done
  PYTHON_TRIED+=("$candidate")
  if python_is_suitable "$candidate"; then
    PYTHON="$candidate"
    return 0
  fi
  return 1
}

find_python() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    PYTHON="$PYTHON_BIN"
    return 0
  fi

  local name dir resolved
  for name in python "${PYTHON_NAMES[@]}"; do
    if resolved="$(command -v "$name" 2>/dev/null)"; then
      try_python "$resolved" && return 0
    fi
  done
  for dir in "${PYTHON_DIRS[@]}"; do
    [[ -n "$dir" && -d "$dir" ]] || continue
    for name in "${PYTHON_NAMES[@]}"; do
      if [[ -x "$dir/$name" && ! -d "$dir/$name" ]]; then
        try_python "$dir/$name" && return 0
      fi
    done
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

  find_python || {
    echo "[check] ERROR: Python 3.11 or newer (with the venv module) is required." >&2
    if ((${#PYTHON_TRIED[@]})); then
      echo "[check] Rejected interpreters:" >&2
      printf '[check]   %s\n' "${PYTHON_TRIED[@]}" >&2
    else
      echo "[check] No python interpreter was found on PATH or in common locations." >&2
    fi
    echo "[check] Searched PATH ($PATH) and: ${PYTHON_DIRS[*]}" >&2
    echo "[check] Set PYTHON_BIN=/path/to/python3.11 and rerun." >&2
    exit 1
  }
  echo "[check] Bootstrap interpreter: $PYTHON"
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

# Fail closed unless the gate imports this checkout's package: an exported
# PYTHONPATH or a venv installed from another tree would otherwise lint and
# test different code. Probe from / so the current directory cannot shadow it.
echo "[check] Package provenance"
if ! EXPECTED_PACKAGE="$(realpath -m -- "$ROOT/src/orca_auto/__init__.py")"; then
  echo "[check] ERROR: Cannot resolve expected package path under $ROOT/src" >&2
  exit 1
fi
if ! IMPORTED_PACKAGE="$(cd / && "$VENV_PY" -c 'import os, orca_auto; print(os.path.realpath(orca_auto.__file__))' 2>&1)"; then
  IMPORTED_PACKAGE="(import failed) $IMPORTED_PACKAGE"
fi
if [[ "$IMPORTED_PACKAGE" != "$EXPECTED_PACKAGE" ]]; then
  echo "[check] ERROR: orca_auto is not imported from this checkout." >&2
  echo "[check] Imported: $IMPORTED_PACKAGE" >&2
  echo "[check] Expected: $EXPECTED_PACKAGE" >&2
  if [[ -n "${PYTHONPATH:-}" ]]; then
    echo "[check] PYTHONPATH is set: $PYTHONPATH" >&2
  fi
  echo "[check] Unset PYTHONPATH, or recreate the virtual environment for this checkout, and rerun." >&2
  exit 1
fi

echo "[check] Ruff"
"$VENV_PY" -m ruff check .

echo "[check] Ruff format"
"$VENV_PY" -m ruff format --check .

echo "[check] mypy"
"$VENV_PY" -m mypy

echo "[check] import-linter"
"$VENV_PY" scripts/check_imports.py

echo "[check] docs parity"
"$VENV_PY" scripts/check_docs_parity.py

echo "[check] pytest"
"$VENV_PY" -m pytest --cov --cov-report=term-missing -q "$@"
