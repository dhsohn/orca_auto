"""Pre-migration disk inspection (deleted after use)."""

import json
from pathlib import Path

home = Path.home()
wf_root = home / "workflow_runs"
runs_root = home / "orca_runs"
outputs_root = home / "orca_outputs"

print("=== workflow statuses ===")
for wf in sorted(wf_root.glob("wf_*/workflow.json")):
    try:
        d = json.loads(wf.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  {wf.parent.name}: <unreadable: {exc}>")
        continue
    print(f"  {wf.parent.name}: status={d.get('status')} type={d.get('workflow_type', '')}")

print()
print("=== workflow_runs top-level files ===")
for f in sorted(wf_root.iterdir()):
    if f.is_file():
        print(f"  {f.name} ({f.stat().st_size}B)")

print()
print("=== workflow_runs top-level dirs (non-wf) ===")
for f in sorted(wf_root.iterdir()):
    if f.is_dir() and not f.name.startswith("wf_"):
        print(f"  {f.name}/")

print()
print("=== orca_runs top-level files ===")
for f in sorted(runs_root.iterdir()):
    if f.is_file():
        print(f"  {f.name} ({f.stat().st_size}B)")

print()
print("=== orca_runs non-run dirs ===")
for name in ("logs", "index", ".admission", "admission"):
    p = runs_root / name
    if p.exists():
        n = sum(1 for _ in p.iterdir())
        print(f"  {name}/ ({n} entries)")

print()
print("=== orca queue.json pending/running? ===")
qp = runs_root / "queue.json"
if qp.exists():
    q = json.loads(qp.read_text(encoding="utf-8"))
    entries = q if isinstance(q, list) else q.get("entries", [])
    by_status: dict[str, int] = {}
    for e in entries:
        s = str(e.get("status", "?"))
        by_status[s] = by_status.get(s, 0) + 1
    print(f"  {by_status}")
else:
    print("  (no queue.json)")

print()
print("=== workflow root queue.json? ===")
qp = wf_root / "queue.json"
if qp.exists():
    q = json.loads(qp.read_text(encoding="utf-8"))
    entries = q if isinstance(q, list) else q.get("entries", [])
    by_status = {}
    for e in entries:
        s = str(e.get("status", "?"))
        by_status[s] = by_status.get(s, 0) + 1
    print(f"  {by_status}")
else:
    print("  (no queue.json)")

print()
print("=== stage queue.json inside workspaces (pending/running only) ===")
active = 0
for qp in wf_root.glob("wf_*/**/queue.json"):
    try:
        q = json.loads(qp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        continue
    entries = q if isinstance(q, list) else q.get("entries", [])
    for e in entries:
        s = str(e.get("status", "")).lower()
        if s in {"pending", "running"}:
            active += 1
            print(f"  {qp.parent.relative_to(wf_root)}: {s}")
print(f"  active stage entries: {active}")

print()
print("=== index JSONL organized_output_dir usage ===")
for idx_dir in (runs_root / "index", outputs_root / "index", wf_root / "index"):
    if not idx_dir.exists():
        continue
    for f in idx_dir.iterdir():
        if f.suffix != ".jsonl":
            continue
        total = 0
        organized = 0
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            total += 1
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if str(rec.get("organized_output_dir", "")).strip():
                organized += 1
        print(f"  {f}: {total} records, {organized} with organized_output_dir")

print()
print("=== orca_outputs sizes ===")
if outputs_root.exists():
    for sub in sorted(outputs_root.iterdir()):
        if sub.is_dir():
            n = sum(1 for _ in sub.rglob("*") if _.is_file())
            print(f"  {sub.name}/: {n} files")
