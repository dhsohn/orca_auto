#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if sudo -n true >/dev/null 2>&1; then
  echo "[bootstrap] Installing base packages..."
  sudo apt-get update -y
  sudo apt-get install -y \
    ca-certificates \
    curl \
    python3 \
    python3-pip \
    python3-venv
else
  echo "[bootstrap] Sudo not available. Skipping apt package installation."
fi

chmod +x "$ROOT/scripts/"*.sh

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" - <<'PY' >/dev/null 2>&1; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1; then
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  echo "[bootstrap] ERROR: orca_auto requires Python 3.11 or newer."
  echo "[bootstrap] Install Python 3.11+ or rerun with PYTHON_BIN=/path/to/python3.11."
  exit 1
fi

echo "[bootstrap] Using Python: $("$PYTHON_BIN" -c 'import sys; print(sys.executable)')"

echo "[bootstrap] Checking ORCA Linux binary..."
ORCA_BIN="${ORCA_BIN:-$HOME/opt/orca/orca}"
if [[ -x "$ORCA_BIN" ]]; then
  echo "[bootstrap] ORCA binary found: $ORCA_BIN"
else
  echo "[bootstrap] WARNING: ORCA binary not found at $ORCA_BIN"
  echo "[bootstrap] Set ORCA_BIN env var or install ORCA to ~/opt/orca/"
  echo "[bootstrap] Required dependencies: OpenMPI, BLAS/LAPACK"
fi

echo "[bootstrap] Preparing Python virtual environment..."
if ! "$PYTHON_BIN" -m venv .venv >/dev/null 2>&1; then
  echo "[bootstrap] $PYTHON_BIN -m venv unavailable, falling back to virtualenv..."
  "$PYTHON_BIN" -m pip install --user virtualenv
  "$PYTHON_BIN" -m virtualenv .venv
fi
VENV_PY="$ROOT/.venv/bin/python"
"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -e .

CONFIG="$ROOT/config/orca_auto.yaml"
if [[ ! -f "$CONFIG" ]]; then
  cp "$ROOT/config/orca_auto.yaml.example" "$CONFIG"
  echo "[bootstrap] Created config/orca_auto.yaml from example template."
  echo "[bootstrap] Edit config/orca_auto.yaml and replace /path/to/... placeholders before first run."
fi
chmod 600 "$CONFIG"
echo "[bootstrap] Secured config/orca_auto.yaml permissions to 600."

echo "[bootstrap] Done."
echo "[bootstrap] Next: source .venv/bin/activate"
echo "[bootstrap] Example: orca_auto init"
echo "[bootstrap] Systemd services are not installed by bootstrap."
echo "[bootstrap] After config is ready: orca_auto systemd install --user \"$(whoami)\" --repo \"$ROOT\""
