#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ $# -gt 0 ]]; then
  WORKDIR="$1"
else
  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/orca_auto_fake_smoke.XXXXXX")"
fi

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="${PYTHON_BIN:-python3}"
fi

mkdir -p "$WORKDIR"

PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" - "$ROOT" "$WORKDIR" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from orca_auto.cli import main as cli_main
from orca_auto.core.queue.types import QueueStatus
from orca_auto.orca.config import load_config
from orca_auto.orca.queue.adapter import list_queue, queue_entry_reaction_dir
from orca_auto.orca.queue.worker import QueueWorker
from orca_auto.orca.state import load_report_json, load_state

repo_root = Path(sys.argv[1]).resolve()
workdir = Path(sys.argv[2]).resolve()
allowed_root = workdir / "orca_runs"
admission_root = workdir / "admission"
bin_dir = workdir / "bin"
reaction_dir = allowed_root / "demo_project" / "water_opt"

for path in (allowed_root, admission_root, bin_dir, reaction_dir):
    path.mkdir(parents=True, exist_ok=True)

fake_orca = bin_dir / "fake_orca.py"
fake_orca.write_text(
    "\n".join(
        [
            "#!/usr/bin/env python3",
            "import sys",
            "from pathlib import Path",
            "inp_name = Path(sys.argv[1]).name if len(sys.argv) > 1 else '<missing>'",
            "print(f'Fake ORCA consumed {inp_name}')",
            "print('FINAL SINGLE POINT ENERGY     -1.000000000000')",
            "print('TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds')",
            "print('****ORCA TERMINATED NORMALLY****')",
            "raise SystemExit(0)",
            "",
        ]
    ),
    encoding="utf-8",
)
fake_orca.chmod(0o755)

config_path = workdir / "orca_auto.yaml"
config_path.write_text(
    json.dumps(
        {
            "runs_root": str(allowed_root),
            "scheduler": {
                "max_active_simulations": 1,
                "admission_root": str(admission_root),
            },
            "orca": {
                "runtime": {
                    "default_max_retries": 0,
                },
                "paths": {"orca_executable": str(fake_orca)},
            },
            "telegram": {"bot_token": "", "chat_id": ""},
        },
        indent=2,
    ),
    encoding="utf-8",
)

selected_inp = reaction_dir / "water_opt.inp"
selected_inp.write_text(
    (repo_root / "examples" / "fake_orca_smoke" / "water_opt.inp").read_text(
        encoding="utf-8"
    ),
    encoding="utf-8",
)

submit_rc = cli_main(["run-dir", str(reaction_dir), "--config", str(config_path)])
if submit_rc != 0:
    raise SystemExit(f"orca_auto run-dir returned {submit_rc}")

matches = [
    entry
    for entry in list_queue(allowed_root)
    if queue_entry_reaction_dir(entry) == str(reaction_dir.resolve())
]
if len(matches) != 1:
    raise SystemExit(f"expected one queue entry for {reaction_dir}, found {len(matches)}")
if matches[0].status != QueueStatus.PENDING:
    raise SystemExit(f"expected pending queue entry, got {matches[0].status}")

worker = QueueWorker(load_config(str(config_path)), str(config_path), max_concurrent=1)
worker.poll_interval_seconds = 0.01
worker_rc = worker.run_once(idle_message=None, blocked_message=None)
if worker_rc != 0:
    raise SystemExit(f"QueueWorker.run_once returned {worker_rc}")

completed = [
    entry
    for entry in list_queue(allowed_root)
    if queue_entry_reaction_dir(entry) == str(reaction_dir.resolve())
]
if len(completed) != 1:
    raise SystemExit(f"expected one completed queue entry, found {len(completed)}")
if completed[0].status != QueueStatus.COMPLETED:
    raise SystemExit(f"expected completed queue entry, got {completed[0].status}")

state = load_state(reaction_dir)
if state is None or state.get("status") != "completed":
    raise SystemExit(f"job_state.json did not complete: {state}")

report = load_report_json(reaction_dir)
if report is None or report.get("status", {}).get("state") != "completed":
    raise SystemExit(f"job_report.json did not complete: {report}")

artifacts = report.get("artifacts")
if not isinstance(artifacts, dict):
    raise SystemExit(f"job_report.json artifacts are invalid: {artifacts}")
out_path_text = artifacts.get("last_out_path")
if not isinstance(out_path_text, str) or not out_path_text.strip():
    raise SystemExit(f"job_report.json has no last_out_path: {artifacts}")
out_path = Path(out_path_text).resolve()
try:
    out_path.relative_to(reaction_dir.resolve())
except ValueError as exc:
    raise SystemExit(f"reported ORCA output escaped the job directory: {out_path}") from exc
if not out_path.is_file():
    raise SystemExit(f"reported ORCA output is missing: {out_path}")
if "****ORCA TERMINATED NORMALLY****" not in out_path.read_text(encoding="utf-8"):
    raise SystemExit(f"normal termination marker missing from {out_path}")

print("fake ORCA smoke passed")
print(f"workdir: {workdir}")
print(f"state: {reaction_dir / 'job_state.json'}")
print(f"report: {reaction_dir / 'job_report.json'}")
PY
