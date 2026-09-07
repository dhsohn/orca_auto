#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

pattern='def test_.*(delegat|forward|uses_.*helper|passthrough|reexports)'

if ! command -v rg >/dev/null 2>&1; then
  echo "[audit] ERROR: ripgrep (rg) is required." >&2
  exit 1
fi

scan_status=0
matches="$(rg -n "$pattern" tests)" || scan_status=$?
if (( scan_status > 1 )); then
  echo "[audit] ERROR: structural-test search failed (status $scan_status)." >&2
  exit "$scan_status"
fi
count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l | tr -d ' ')"

echo "[audit] structural-test-name matches: $count"
if [[ -n "$matches" ]]; then
  printf '%s\n' "$matches"
fi
