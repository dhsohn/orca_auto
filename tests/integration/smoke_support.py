from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from orca_auto.cli import main as cli_main
from orca_auto.core.admission import list_slots
from orca_auto.flow.engines.crest import queue_runtime as crest_queue_runtime
from orca_auto.flow.engines.xtb import queue_runtime as xtb_queue_runtime
from orca_auto.flow.runtime import (
    TERMINAL_WORKFLOW_STATUSES,
    advance_workflow_registry_once,
    workflow_needs_terminal_sync,
)
from orca_auto.flow.state import load_workflow_payload, workflow_has_active_downstream
from orca_auto.orca.config import load_config as load_orca_config
from orca_auto.orca.queue import worker as orca_queue_worker


def write_h2(path: Path, *, comment: str, bond: float = 0.74) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"2\n{comment}\nH 0.0 0.0 0.0\nH 0.0 0.0 {bond:.2f}\n",
        encoding="utf-8",
    )


def write_fake_orca(
    path: Path,
    *,
    mode: Literal["success", "nonconverged"] = "success",
    scan_profile: Literal["barrier", "barrierless"] = "barrier",
) -> None:
    script = f"""\
    #!/usr/bin/env python3
    import sys
    from pathlib import Path

    inp = Path(sys.argv[1]).resolve()
    inp_text = inp.read_text(encoding="utf-8")
    is_scan = "%geom" in inp_text and "Scan" in inp_text

    if is_scan:
        energies = (
            [-100.0, -99.5, -100.2, -100.3]
            if {scan_profile!r} == "barrier"
            else [-100.0, -100.1, -100.2, -100.3]
        )
        for index in range(1, 5):
            (Path.cwd() / f"{{inp.stem}}.{{index:03d}}.xyz").write_text(
                f"2\\nscan point {{index}}\\nH 0 0 0\\nH 0 0 0.74\\n",
                encoding="utf-8",
            )
        print("RELAXED SURFACE SCAN RESULTS")
        print("The Calculated Surface using the 'Actual Energy'")
        for coordinate, energy in zip((0.7, 0.8, 0.9, 1.0), energies, strict=True):
            print(f"   {{coordinate:.8f}} {{energy:.8f}}")
        print("The Calculated Surface using the SCF energy")
        print("   0.70000000 -101.00000000")

    print("Program Version 6.0.1 - RELEASE -")
    for line_number, line in enumerate(inp_text.splitlines(), start=1):
        print(f"| {{line_number:2d}}> {{line}}")
    print("CARTESIAN COORDINATES (ANGSTROEM)")
    print("---------------------------------")
    print(" H 0.000000 0.000000 0.000000")
    print(" H 0.000000 0.000000 0.740000")
    print("")
    print("FINAL SINGLE POINT ENERGY -1.100000000000")
    if {mode!r} == "nonconverged" and "OptTS" not in inp_text:
        print("THE OPTIMIZATION DID NOT CONVERGE")
    elif "Opt" in inp_text:
        print("THE OPTIMIZATION HAS CONVERGED")
    if "OptTS" in inp_text:
        print("VIBRATIONAL FREQUENCIES")
        print("  1: -512.34 cm**-1")
        print("  2: 120.00 cm**-1")
    print("TOTAL RUN TIME: 0 days 0 hours 0 minutes 1 seconds")
    print("****ORCA TERMINATED NORMALLY****")
    raise SystemExit(0)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(script).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def configure_fake_orca(config_path: Path, executable: Path) -> None:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise AssertionError("shared smoke config must be a mapping")
    raw["orca"] = {
        "runtime": {"default_max_retries": 0},
        "paths": {"orca_executable": str(executable.resolve())},
    }
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def submit_public_workflow(
    input_dir: Path,
    config_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> dict[str, Any]:
    exit_code = cli_main(["run-dir", str(input_dir), "--config", str(config_path), "--json"])
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    assert payload.get("workflow_id")
    assert isinstance(payload.get("metadata"), dict)
    assert payload["metadata"].get("workspace_dir")
    return payload


def pump_workflow(
    *,
    workflow_root: Path,
    workspace_dir: Path,
    config_path: Path,
    admission_root: Path,
    max_cycles: int = 20,
) -> dict[str, Any]:
    crest_worker = crest_queue_runtime.QueueWorker(
        crest_queue_runtime.load_config(str(config_path)),
        str(config_path),
        max_concurrent=1,
    )
    xtb_worker = xtb_queue_runtime.QueueWorker(
        xtb_queue_runtime.load_config(str(config_path)),
        str(config_path),
        max_concurrent=1,
    )
    orca_worker = orca_queue_worker.QueueWorker(
        load_orca_config(str(config_path)),
        str(config_path),
        max_concurrent=1,
    )
    for worker in (crest_worker, xtb_worker, orca_worker):
        worker.poll_interval_seconds = 0.01

    payload: dict[str, Any] = {}
    for _cycle in range(max_cycles):
        advance_workflow_registry_once(
            workflow_root=workflow_root,
            shared_config=str(config_path),
            submit_ready=True,
            refresh_registry=False,
        )
        for worker in (crest_worker, xtb_worker, orca_worker):
            assert worker.run_once(idle_message=None, blocked_message=None) == 0
        payload = load_workflow_payload(workspace_dir)
        status = str(payload.get("status") or "").strip().lower()
        if status not in TERMINAL_WORKFLOW_STATUSES:
            continue
        if workflow_needs_terminal_sync(
            workspace_dir,
            load_workflow_payload_fn=load_workflow_payload,
            workflow_has_active_downstream_fn=workflow_has_active_downstream,
        ):
            continue
        break
    else:
        pytest.fail(
            f"workflow did not settle after {max_cycles} cycles: "
            f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        )

    assert list_slots(admission_root) == []
    return payload


def assert_workflow_report(workspace_dir: Path, payload: dict[str, Any]) -> str:
    workflow_id = str(payload.get("workflow_id") or "").strip()
    template_name = str(payload.get("template_name") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    assert workflow_id and template_name
    assert status in TERMINAL_WORKFLOW_STATUSES

    report_html = (workspace_dir / "workflow_report.html").read_text(encoding="utf-8")
    assert len(report_html) > 500
    assert workflow_id in report_html
    assert template_name in report_html
    assert f">{status}<" in report_html

    metadata = payload.get("metadata")
    workflow_error = metadata.get("workflow_error") if isinstance(metadata, dict) else None
    if status == "failed":
        assert isinstance(workflow_error, dict)
        assert str(workflow_error.get("scope") or "") in report_html
        assert str(workflow_error.get("reason") or "") in report_html
    return report_html


def assert_workflow_publications(workspace_dir: Path, payload: dict[str, Any]) -> None:
    status = str(payload.get("status") or "").strip().lower()
    workflow_id = str(payload.get("workflow_id") or "").strip()
    template_name = str(payload.get("template_name") or "").strip()
    report_html = assert_workflow_report(workspace_dir, payload)

    si_path = workspace_dir / "workflow_si.md"
    si_markdown = si_path.read_text(encoding="utf-8")
    assert len(si_markdown) > 100
    assert workflow_id in si_markdown
    assert template_name in si_markdown

    if status == "completed":
        assert "-1.100000" in si_markdown
        assert "-1.100000" in report_html
        assert "+0.00" in report_html
        assert "provenance missing or inconsistent" not in si_markdown


def assert_orca_job_publications(
    job_dir: Path,
    *,
    expected_status: str,
    expect_si: bool,
) -> dict[str, Any]:
    state = json.loads((job_dir / "job_state.json").read_text(encoding="utf-8"))
    status = state.get("status")
    assert isinstance(status, dict)
    assert status.get("state") == expected_status
    job = state.get("job")
    assert isinstance(job, dict)
    job_id = str(job.get("id") or "").strip()
    reason = str(status.get("reason") or "").strip()
    assert job_id and reason

    # Reports live inside the execution generation; the job root only keeps
    # the live state (and pre-relocation legacy report copies).
    engine_payload = state.get("engine_payload")
    assert isinstance(engine_payload, dict)
    provenance = engine_payload.get("execution_provenance")
    assert isinstance(provenance, dict)
    generation_dir = Path(str(provenance.get("execution_dir") or "").strip())
    assert generation_dir.is_dir()

    report_html = (generation_dir / "job_report.html").read_text(encoding="utf-8")
    assert len(report_html) > 500
    assert job_id in report_html
    assert f">{expected_status}<" in report_html
    assert reason in report_html
    assert "AnalyzerStatus." not in report_html
    assert not (job_dir / "job_report.html").exists()

    si_path = generation_dir / "si_block.md"
    if expect_si:
        si_markdown = si_path.read_text(encoding="utf-8")
        assert len(si_markdown) > 100
        assert "E(el)" in si_markdown
        assert "-1.100000 Eh" in si_markdown
    else:
        assert not si_path.exists()
    assert not (job_dir / "si_block.md").exists()
    return state


def orca_job_generation_dir(state: dict[str, Any]) -> Path:
    engine_payload = state.get("engine_payload")
    assert isinstance(engine_payload, dict)
    provenance = engine_payload.get("execution_provenance")
    assert isinstance(provenance, dict)
    generation_dir = Path(str(provenance.get("execution_dir") or "").strip())
    assert generation_dir.is_dir()
    return generation_dir


def orca_job_directories(payload: dict[str, Any]) -> list[Path]:
    directories: list[Path] = []
    for raw_stage in payload.get("stages", []):
        if not isinstance(raw_stage, dict):
            continue
        task = raw_stage.get("task")
        if not isinstance(task, dict) or str(task.get("engine") or "") != "orca":
            continue
        task_payload = task.get("payload")
        if not isinstance(task_payload, dict):
            continue
        reaction_dir = str(task_payload.get("reaction_dir") or "").strip()
        if reaction_dir:
            directories.append(Path(reaction_dir))
    return directories
