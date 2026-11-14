#!/usr/bin/env python3
"""Bounded, installed-runtime checkpoint acceptance. See checkpoint_acceptance.md.

No engine is invoked here. Only the ordinary durable queue and run_once worker
may launch one. The supervisor and its independent watchdog cancel one private
queue ID; neither releases reservations nor signals engine processes itself.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import fcntl
import hashlib
import inspect
import json
import math
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

SESSION_SECONDS = 35 * 60
LAUNCH_SECONDS = 180
CLEANUP_SECONDS = 60
MAX_MATRIX = 8
MAX_RECOVERY_LAUNCHES = 6  # pre-default, post-default, post-explicit; two launches each
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_SCRIPT = Path(__file__).resolve()


def require(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    require(isinstance(value, dict), f"expected object: {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    # Only experiment-owned evidence uses this writer, never queue/state files.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    temporary.replace(path)


def identity(path: Path) -> dict[str, Any]:
    from orca_auto.orca.file_identity import file_content_identity

    result = file_content_identity(path)
    details = path.stat()
    return {
        **result,
        "mode": oct(stat.S_IMODE(details.st_mode)),
        "uid": details.st_uid,
        "device": details.st_dev,
        "inode": details.st_ino,
        "nlink": details.st_nlink,
        "mtime_ns": details.st_mtime_ns,
    }


def verified_file(descriptor: dict[str, Any]) -> Path:
    path = Path(descriptor["path"])
    require(path.is_absolute() and not path.is_symlink(), f"unsafe supplied file: {path}")
    require(path.is_file(), f"missing supplied file: {path}")
    require(identity(path)["sha256"] == descriptor["sha256"], f"hash mismatch: {path}")
    return path


def copy_verified(descriptor: dict[str, Any], target: Path) -> None:
    from orca_auto.core.confined_io import read_stable_regular_file

    payload = read_stable_regular_file(verified_file(descriptor), max_bytes=64 * 1024 * 1024)
    require(
        hashlib.sha256(payload).hexdigest() == descriptor["sha256"], "source changed before copy"
    )
    target.write_bytes(payload)


def scoped_calls(module: Any, names: set[str]) -> bool:
    calls = [
        node
        for node in ast.walk(ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names
    ]
    return {node.func.id for node in calls if isinstance(node.func, ast.Name)} == names and all(
        any(keyword.arg == "owned" for keyword in node.keywords) for node in calls
    )


def runtime_identity() -> dict[str, Any]:
    import orca_auto
    from orca_auto.orca import execution
    from orca_auto.orca.queue import worker

    package = Path(orca_auto.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(path.relative_to(package).as_posix().encode() + b"\0")
            digest.update(bytes.fromhex(identity(path)["sha256"]))
    scoped = scoped_calls(
        worker,
        {
            "reserve_slot",
            "list_slots",
            "reconcile_stale_slots",
            "recover_orphaned_engine_slots",
            "update_slot_metadata",
        },
    ) and scoped_calls(execution, {"activate_reserved_slot"})
    return {
        "python": identity(Path(sys.executable).resolve()),
        "python_version": sys.version,
        "prefix": str(Path(sys.prefix).resolve()),
        "package": str(package),
        "package_tree_sha256": digest.hexdigest(),
        "scoped_admission_calls": scoped,
        "installed_under_prefix": package.is_relative_to(Path(sys.prefix).resolve()),
    }


def _word_values(parsed: Any, name: str, key: str) -> list[str]:
    result = []
    for block in parsed.blocks_named(name):
        require(block.closed, f"unterminated %{name}")
        tokens = [token.value.lower() for row in block.rows for token in row.tokens]
        for index, token in enumerate(tokens):
            if token == key:
                index += 1
                if index < len(tokens) and tokens[index] == "=":
                    index += 1
                require(index < len(tokens), f"missing {key} value")
                result.append(tokens[index])
    return result


def validate_input(descriptor: dict[str, Any], *, diagnostic: bool) -> Path:
    from orca_auto.orca.input_blocks import parse_input
    from orca_auto.orca.input_references import orca_moinp_references, scan_orca_file_references
    from orca_auto.orca.resource_directives import resource_request_from_lines

    path = verified_file(descriptor)
    parsed = parse_input(path.read_text().splitlines())
    require(
        resource_request_from_lines(parsed) == {"max_cores": 4, "max_memory_gb": 8},
        "acceptance requires explicit four processes and 8 GiB request",
    )
    route = {token.value.lower() for row in parsed.route_tokens for token in row}
    require(
        "sp" in route
        and not (route & {"opt", "optts", "freq", "numfreq", "irc", "neb", "neb-ts", "noiter"}),
        "supply a reviewed single-point derivative without NoIter",
    )
    require(
        not parsed.blocks_named("geom")
        and not parsed.blocks_named("freq")
        and not parsed.blocks_named("neb"),
        "remove operation blocks in the reviewed SP derivative",
    )
    names = {item["name"] for item in descriptor["dependencies"]}
    require(len(names) == len(descriptor["dependencies"]), "duplicate dependency name")
    for item in descriptor["dependencies"]:
        require(
            Path(item["name"]).name == item["name"]
            and item["name"] not in {"probe.inp", "probe.gbw", "probe.moinp.gbw"},
            "unsafe/reserved dependency basename",
        )
        verified_file(item)
    refs = {ref.value for ref in scan_orca_file_references(parsed)}
    if diagnostic:
        require(
            [ref.value for ref in orca_moinp_references(parsed)] == ["probe.moinp.gbw"],
            "diagnostic needs exactly probe.moinp.gbw",
        )
        require("moread" in route, "diagnostic needs explicit route MORead")
        require(
            _word_values(parsed, "scf", "autostart") == ["false"],
            "diagnostic needs AutoStart false",
        )
        require(_word_values(parsed, "scf", "maxiter") == ["1"], "diagnostic needs MaxIter 1")
        require(
            not route.intersection({"pmodel", "patom", "hcore", "hueckel", "guessmix"})
            and _word_values(parsed, "scf", "guess") in ([], ["moread"]),
            "conflicting diagnostic initial guess",
        )
        refs.discard("probe.moinp.gbw")
    require(
        refs == names, "supply exactly the reviewed input dependency closure with plain basenames"
    )
    return path


def preflight(plan: dict[str, Any], family_name: str, action: str) -> dict[str, Any]:
    from orca_auto.core.engine_scratch import inspect_scratch_root
    from orca_auto.orca.config import load_config

    require(plan["stage"] in {"baseline", "candidate"}, "unknown runtime stage")
    require(
        (action == "matrix") == (plan["stage"] == "baseline"),
        "matrix is baseline-only; use production for the candidate without chmod interventions",
    )
    use_scratch = plan.get("use_scratch", True)
    require(type(use_scratch) is bool, "use_scratch must be boolean")
    require(use_scratch or plan["stage"] == "candidate", "baseline requires scratch")
    require(sys.platform == "linux" and sys.flags.isolated, "use Linux installed-runtime python -I")
    require(
        os.geteuid() != 0 and os.getuid() == os.geteuid(), "ordinary unprivileged user required"
    )
    status = Path("/proc/self/status").read_text()
    for field in ("CapEff", "CapPrm", "CapAmb"):
        match = re.search(rf"^{field}:\s*(\w+)", status, re.M)
        if match is None:
            raise ValueError(f"missing capability evidence: {field}")
        require(int(match.group(1), 16) == 0, f"permission comparison refuses {field} capabilities")
    require(
        hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"),
        "stable worker signalling unavailable",
    )
    require(shutil.which("strace"), "strace required for ordinary worker file-open tracing")
    import jsonschema  # noqa: F401 - receipt validation must be available before submission

    current = runtime_identity()
    require(
        current["installed_under_prefix"] and current["scoped_admission_calls"],
        "runtime lacks installed/scoped ownership proof",
    )
    require(
        current["prefix"] == str(Path(plan["runtime_prefix"]).resolve()), "wrong interpreter prefix"
    )
    require(
        current["package_tree_sha256"] == plan["runtime_tree_sha256"],
        "runtime package identity changed",
    )
    require(
        re.fullmatch(r"[0-9a-f]{40}", plan["source_revision"]),
        "owner must attest the source revision",
    )
    production = json.loads(
        subprocess.check_output(
            [
                str(Path(plan["production_runtime_prefix"]) / "bin/python"),
                "-I",
                str(_SCRIPT),
                "identity",
            ],
            text=True,
            timeout=30,
        )
    )
    require(
        production["scoped_admission_calls"] and production["installed_under_prefix"],
        "operational runtime lacks scoped ownership proof",
    )
    require(
        production["package_tree_sha256"] == plan["production_runtime_tree_sha256"],
        "operational runtime identity changed",
    )
    config_path = verified_file(plan["production_config"])
    cfg = load_config(str(config_path))
    engine = verified_file(plan["engine"])
    require(
        engine.resolve() == Path(cfg.paths.orca_executable).resolve(),
        "engine differs from production config",
    )
    mpi = verified_file(plan["mpi"])
    require(
        Path(shutil.which("mpirun") or "").resolve() == mpi.resolve(),
        "PATH mpirun differs from reviewed MPI",
    )
    require(len(os.sched_getaffinity(0)) >= 4, "fewer than four permitted CPUs")
    address_limit = resource.getrlimit(resource.RLIMIT_AS)[0]
    require(
        address_limit == resource.RLIM_INFINITY or address_limit >= 8 * 1024**3,
        "address-space limit below the recorded request",
    )
    cgroup_limits: dict[str, str] = {}
    cgroup = next(
        (
            line[3:]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        ),
        None,
    )
    if cgroup is not None:
        mount = Path("/sys/fs/cgroup")
        require(".." not in Path(cgroup).parts, "unresolved cgroup namespace path")
        current_group = mount / cgroup.lstrip("/")
        require(current_group.is_relative_to(mount), "invalid cgroup path")
        for group in (current_group, *current_group.parents):
            if not group.is_relative_to(mount):
                break
            for name in ("memory.max", "cpu.max"):
                path = group / name
                if path.is_file():
                    value = path.read_text().strip()
                    cgroup_limits[str(path)] = value
                    if name == "memory.max" and value != "max":
                        require(int(value) >= 8 * 1024**3, "cgroup memory limit below 8 GiB")
                    if name == "cpu.max":
                        quota, period = value.split()
                        require(
                            quota == "max" or int(quota) >= 4 * int(period),
                            "CPU quota below four cores",
                        )
    require(cfg.scratch.enabled, "incident reproduction requires operational RAM scratch")
    scratch = Path(cfg.scratch.root)
    reports = inspect_scratch_root(scratch, max_size_entries=0)
    require(
        all(report.state == "live" and not report.blocks_launch for report in reports),
        "scratch contains stale/unsafe/unresolved entries; stop for owner inspection",
    )
    root = Path(plan["experiment_root"])
    require(
        root.is_absolute() and root.resolve() == root and not root.is_symlink(),
        "unsafe experiment root",
    )
    operational = Path(cfg.runtime.allowed_root).resolve()
    require(
        not root.is_relative_to(operational) and not operational.is_relative_to(root),
        "experiment must be outside the operational runs tree",
    )
    require((operational / ".admission").is_dir(), "operational admission store missing")
    require(_NAME.fullmatch(plan["session"]), "unsafe session name")
    families = plan["families"]
    require(
        1 <= len(families) <= 2 and len({f["name"] for f in families}) == len(families),
        "one or two distinct families required",
    )
    family = next((f for f in families if f["name"] == family_name), None)
    if family is None or not _NAME.fullmatch(family_name):
        raise ValueError("family is missing or unsafe")
    # Review/provenance is supplied, not inferred from sizes or guessed GBW offsets.
    verified_file(family["reference_input"])
    verified_file(family["derivation_review"])
    if action in {"matrix", "production"}:
        validate_input(family["diagnostic"], diagnostic=True)
        for name in ("positive_donor", "preguess_donor"):
            verified_file(family[name])
    else:
        require(plan["stage"] == "candidate", "recovery acceptance requires the candidate runtime")
        require(family == families[0], "bounded recovery is limited to the first family")
        from orca_auto.orca.recovery_orbitals import recovery_guess_policy

        for name, policy in (
            ("recovery_default", "automatic"),
            ("recovery_explicit", "explicit_fresh_guess"),
        ):
            path = validate_input(family[name], diagnostic=False)
            require(
                recovery_guess_policy(path.read_text().splitlines()) == policy,
                f"unexpected {name} policy",
            )
        require(
            family["explicit_guess_output"] in {"MODEL POTENTIAL", "PATOM", "HUECKEL", "HCORE"},
            "supply an exact known engine initial-guess label",
        )
    return {
        "runtime": current,
        "production_runtime": production,
        "config": identity(config_path),
        "engine": identity(engine),
        "mpi": identity(mpi),
        "uid": os.getuid(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "limits": {
            name: resource.getrlimit(getattr(resource, name))
            for name in ("RLIMIT_AS", "RLIMIT_DATA", "RLIMIT_NPROC", "RLIMIT_NOFILE", "RLIMIT_CORE")
        },
        "scratch": [report.as_payload() for report in reports],
        "admission_limit": cfg.runtime.max_concurrent,
        "operational_root": str(operational),
        "scratch_root": str(scratch),
        "scratch_min_free_gb": cfg.scratch.min_free_gb,
        "use_scratch": use_scratch,
        "cgroup_limits": cgroup_limits,
    }


def own_slots(root: Path, queue_id: str = "", worker_pid: int = 0) -> list[Any]:
    from orca_auto.core.admission import list_all_slots, runs_root_ownership

    owns = runs_root_ownership(root)
    return [
        slot
        for slot in list_all_slots(root / ".admission")
        if owns(slot)
        or (queue_id and slot.queue_id == queue_id)
        or (worker_pid and slot.owner_pid == worker_pid)
    ]


def cleanup_complete(
    root: Path, queue_id: str, observed: list[dict[str, Any]], worker_pid: int = 0
) -> bool:
    from orca_auto.core.utils.process import process_group_exists, process_identity_alive

    if own_slots(root, queue_id, worker_pid):
        return False
    for slot in observed:
        if process_identity_alive(
            slot["owner_pid"], slot["process_start_ticks"], slot["owner_boot_id"]
        ):
            return False
        if slot["engine_pgid"] and process_group_exists(slot["engine_pgid"]):
            return False
    return True


def _cancel(root: Path, queue_id: str) -> None:
    from orca_auto.orca.queue.adapter import cancel, get_entry_by_id

    for _ in range(3):
        current = get_entry_by_id(root, queue_id)
        if current is None:
            raise ValueError("watchdog queue row disappeared")
        if current.status.value not in {"pending", "running"}:
            return
        if cancel(root, queue_id, expected_entry=current) is not None:
            return
    raise RuntimeError("cancellation fence changed repeatedly; retain ownership and inspect")


def _control(path: Path) -> dict[str, Any]:
    control = read_json(path)
    session = Path(control["session"])
    require(
        path.resolve().is_relative_to(session / "evidence"), "control outside evidence directory"
    )
    require(
        read_json(session / "session.json")["schema"] == "orca-checkpoint-acceptance-v1",
        "missing session marker",
    )
    from orca_auto.orca.config import load_config

    cfg = load_config(str(verified_file(control["config"])))
    require(
        Path(cfg.runtime.allowed_root) == session / "runs", "control config is not experiment-owned"
    )
    return control


def _shutdown_responsive_sleep(seconds: float, shutdown_requested: Callable[[], bool]) -> None:
    # SIGTERM sets the ordinary worker's flag; it does not interrupt time.sleep.
    # Preserve its poll cadence, but let normal shutdown start within one short
    # sleep quantum instead of waiting out the remainder of a five-second poll.
    deadline = time.monotonic() + seconds
    while not shutdown_requested():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def private_process(action: str, path: Path) -> int:
    from orca_auto.core.utils.process import (
        linux_boot_id,
        process_identity_alive,
        process_start_ticks,
    )

    control = _control(path)
    root = Path(control["session"]) / "runs"
    evidence = path.parent
    if action == "_worker":
        from orca_auto.orca.config import load_config
        from orca_auto.orca.queue.worker import OrcaQueueWorker

        write_json(
            evidence / "worker-pid.json",
            {
                "pid": os.getpid(),
                "ticks": process_start_ticks(os.getpid()),
                "boot": linux_boot_id(),
            },
        )
        queue_worker = OrcaQueueWorker(
            load_config(control["config"]["path"]),
            control["config"]["path"],
            sleep_fn=lambda seconds: _shutdown_responsive_sleep(
                seconds, lambda: queue_worker._shutdown_requested
            ),
        )
        return queue_worker.run_once()
    while not (evidence / "watchdog-done").exists():
        if time.monotonic() >= control["deadline"] or not process_identity_alive(
            control["supervisor_pid"], control["supervisor_ticks"], control["supervisor_boot"]
        ):
            write_json(
                evidence / "watchdog-fired.json",
                {"time": time.time(), "queue_id": control["queue_id"]},
            )
            write_json(
                Path(control["session"]) / "STOP.json",
                {"reason": "watchdog_fired", "queue_id": control["queue_id"]},
            )
            try:
                _cancel(root, control["queue_id"])
            except Exception as exc:  # noqa: BLE001 - a failed watchdog must leave a durable receipt
                write_json(evidence / "watchdog-unresolved.json", {"cancel_error": str(exc)})
                return 2
            end = time.monotonic() + CLEANUP_SECONDS
            observed: list[dict[str, Any]] = []
            while time.monotonic() < end:
                observed.extend(
                    dataclasses.asdict(slot) for slot in own_slots(root, control["queue_id"])
                )
                if (evidence / "watchdog-done").exists():
                    return 0
                pid_path = evidence / "worker-pid.json"
                worker = read_json(pid_path) if pid_path.exists() else None
                worker_dead = worker is not None and not process_identity_alive(
                    worker["pid"], worker["ticks"], worker["boot"]
                )
                if (
                    worker is not None
                    and worker_dead
                    and cleanup_complete(root, control["queue_id"], observed, worker["pid"])
                ):
                    write_json(
                        evidence / "watchdog-cleanup.json", {"resolved": True, "observed": observed}
                    )
                    return 2
                time.sleep(0.2)
            write_json(
                evidence / "watchdog-unresolved.json",
                {
                    "retained_slots": [dataclasses.asdict(s) for s in own_slots(root)],
                    "observed": observed,
                },
            )
            return 2
        time.sleep(0.1)
    return 0


def output_observation(payload: bytes, checkpoint_name: str = "probe.moinp.gbw") -> dict[str, Any]:
    # Independent narrow diagnostic, also usable with the unchanged baseline.
    lines = [
        line.strip()
        for line in payload.decode("utf-8", errors="replace").splitlines()
        if not line.lstrip().startswith(("|", "#"))
    ]
    guess = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(r"Finished Guess after\s+\d+(?:\.\d+)?\s+sec", line)
    ]
    scf = [
        i for i, line in enumerate(lines) if line.startswith("Iteration") and "Energy (Eh)" in line
    ]
    read = [
        i
        for i, line in enumerate(lines)
        if line.startswith("Guess MOs are being read from file:")
        and line.partition(":")[2].strip().strip('"') == checkpoint_name
    ]
    return {
        "moread_consumed_then_scf": any(r < g < s for r in read for g in guess for s in scf),
        "finished_guess": bool(guess),
        "startup_seen": "ORCA STARTUP CALCULATIONS" in lines,
        "scf_entered": bool(scf),
        "normal_termination": any("ORCA TERMINATED NORMALLY" in line for line in lines),
        "scf_converged": any("SCF CONVERGED AFTER" in line for line in lines),
        "energies_eh": [
            float(m.group(1))
            for line in lines
            if (m := re.fullmatch(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", line))
        ],
        "guess_labels": [
            line.partition(":")[2].strip() for line in lines if line.startswith("INITIAL GUESS:")
        ],
    }


def checkpoint_trace(observed: dict[str, Any], name: str) -> list[str]:
    return [
        line
        for trace in sorted(Path(observed["evidence_dir"]).glob("opens.*"))
        for line in trace.read_text(errors="replace").splitlines()
        if f'"{name}"' in line
    ]


def production_copy_receipt(
    snapshot: dict[str, Any], observed: dict[str, Any], *, use_scratch: bool
) -> dict[str, Any]:
    """Inspect the production boundary without changing a dependency or queue record."""
    from orca_auto.orca.engine_moinp import engine_moinp_sources
    from orca_auto.orca.execution_binding import verify_orca_execution_snapshot

    generation = Path(snapshot["execution_dir"])
    verify_orca_execution_snapshot(
        Path(snapshot["source_selected_inp"]).parent,
        snapshot,
        expected_selected_inp=snapshot["selected_inp"],
        expected_source_selected_inp=snapshot["source_selected_inp"],
        expected_selected_input_xyz=snapshot["selected_input_xyz"],
        expected_resource_request=snapshot["resource_request"],
        allow_runtime_outputs=True,
    )
    copies = engine_moinp_sources(Path(snapshot["selected_inp"]).read_text().splitlines())
    result = {}
    for target, source in copies.items():
        immutable = identity(generation / source)
        private = identity(generation / target)
        require(immutable["mode"] == "0o400", "bound checkpoint permission changed")
        require(private["mode"] == "0o600", "engine copy is not private writable 0600")
        require(
            private["nlink"] == immutable["nlink"] == 1
            and (private["device"], private["inode"]) != (immutable["device"], immutable["inode"]),
            "engine copy aliases its immutable source",
        )
        trace = checkpoint_trace(observed, target)
        opened = any("O_RDWR" in line and re.search(r"= \d+(?:\s|$)", line) for line in trace)
        require(opened, "no successful O_RDWR engine-copy open in the worker trace")
        require(not any("EACCES" in line for line in trace), "engine-copy access denied")
        live = [
            {**capture, **capture["copies"][target]}
            for directory, capture in observed.get("engine_copies", {}).items()
            if target in capture.get("copies", {})
            and capture.get("directory") == directory == capture.get("engine_cwd")
            and capture.get("execution_dir") == str(generation)
            and capture.get("engine_alive_before") is True
            and capture.get("engine_alive_after") is True
            and capture.get("engine", {}).get("engine_process_state") == "active"
            and type(capture["engine"].get("engine_pid")) is int
            and capture["engine"]["engine_pid"] > 0
            and type(capture["engine"].get("engine_process_start_ticks")) is int
            and capture["engine"]["engine_process_start_ticks"] > 0
            and bool(capture["engine"].get("engine_process_boot_id"))
            and (
                (capture.get("scratch_workspace") == directory and directory != str(generation))
                if use_scratch
                else (capture.get("scratch_workspace") is None and directory == str(generation))
            )
        ]
        require(
            any(
                item["immutable"]["mode"] == "0o400"
                and item["immutable"]["path"] == str(Path(item["directory"]) / source)
                and item["engine_copy"]["path"] == str(Path(item["directory"]) / target)
                and item["immutable"]["sha256"] == immutable["sha256"]
                and item["engine_copy"]["mode"] == "0o600"
                and item["engine_copy"]["nlink"] == item["immutable"]["nlink"] == 1
                and (item["engine_copy"]["device"], item["engine_copy"]["inode"])
                != (item["immutable"]["device"], item["immutable"]["inode"])
                for item in live
            ),
            "missing live engine-workspace immutable0400/private0600 independent-copy evidence; "
            "inconclusive",
        )
        result[target] = {
            "immutable": immutable,
            "engine_copy": private,
            "open_trace": trace,
            "live": live,
        }
    return result


def publication_valid(receipt: dict[str, Any], *, use_scratch: bool) -> bool:
    attempts = (receipt.get("state") or {}).get("attempts", [])
    if not receipt["machine_valid"] or len(attempts) != 1:
        return False
    scratch = attempts[0].get("scratch_provenance", {})
    if use_scratch:
        return bool(scratch.get("used") and scratch.get("publication_status") == "committed")
    return not scratch.get("used")


def interrupted_publication_valid(receipt: dict[str, Any], *, use_scratch: bool) -> bool:
    """An interrupted run has a publication record, not necessarily a machine receipt."""
    records = (receipt.get("state") or {}).get("scratch_publications", [])
    if not use_scratch:
        return records == []
    if len(records) != 1:
        return False
    record = records[0]
    publication = record.get("publication", {})
    published = publication.get("published_files", [])
    required = {"probe.out"} | {
        name for name in receipt["artifacts"] if Path(name).suffix == ".gbw"
    }
    return bool(
        record.get("attempt_index") == 1
        and record.get("inp_path") == receipt["snapshot"]["selected_inp"]
        and record.get("outcome") == "worker_shutdown"
        and record.get("stop_cause") == "worker_shutdown"
        and publication.get("used") is True
        and publication.get("publication_status") == "committed"
        and isinstance(published, list)
        and required.issubset(published)
    )


def _reject_stopped_session(session: Path) -> None:
    require(
        not (session / "STOP.json").exists(),
        "session stopped; no further launches",
    )


def _reject_launch_timeouts(observed: dict[str, Any]) -> None:
    require(
        not observed["watchdog_fired"] and not observed["supervisor_timeout_fired"],
        "timed out cell is inconclusive; stop this session",
    )


def _live_files(snapshot: dict[str, Any], scratch: Path) -> tuple[Path, Path | None]:
    from orca_auto.core.engine_scratch import inspect_scratch_root

    generation = Path(snapshot["execution_dir"])
    for report in inspect_scratch_root(scratch, max_size_entries=0):
        if (
            report.durable_dir == str(generation)
            and report.state == "live"
            and not report.blocks_launch
        ):
            return report.path / "probe.out", report.path
    return generation / "probe.out", None


def _capture_engine_copies(
    snapshot: dict[str, Any], directory: Path, workspace: Path | None, slots: list[dict[str, Any]]
) -> dict[str, Any]:
    from orca_auto.core.utils.process import process_identity_alive
    from orca_auto.orca.engine_moinp import engine_moinp_sources

    copies = engine_moinp_sources(Path(snapshot["selected_inp"]).read_text().splitlines())
    if not copies:
        return {}
    for slot in slots:
        if slot["engine_process_state"] != "active":
            continue
        engine = (
            slot["engine_pid"],
            slot["engine_process_start_ticks"],
            slot["engine_process_boot_id"],
        )
        if not process_identity_alive(*engine):
            continue
        try:
            cwd = Path(f"/proc/{slot['engine_pid']}/cwd").resolve(strict=True)
            if cwd != directory:
                continue
            captured = {
                target: {
                    "engine_copy": identity(directory / target),
                    "immutable": identity(directory / source),
                }
                for target, source in copies.items()
            }
            # Publication can race the polling loop. A live worker alone is
            # insufficient: the same engine must still occupy this directory.
            if (
                not process_identity_alive(*engine)
                or Path(f"/proc/{engine[0]}/cwd").resolve(strict=True) != directory
            ):
                continue
        except (OSError, ValueError):
            continue
        return {
            "directory": str(directory),
            "execution_dir": snapshot["execution_dir"],
            "scratch_workspace": str(workspace) if workspace else None,
            "engine_cwd": str(cwd),
            "engine": slot,
            "engine_alive_before": True,
            "engine_alive_after": True,
            "captured_at": time.time(),
            "copies": captured,
        }
    return {}


def run_worker(
    session: Path, queue_id: str, label: str, scratch: Path, *, phase: str = ""
) -> dict[str, Any]:
    from orca_auto.core.utils.process import linux_boot_id, process_start_ticks
    from orca_auto.orca.queue.adapter import get_entry_by_id

    _reject_stopped_session(session)
    root = session / "runs"
    evidence = session / "evidence" / label
    evidence.mkdir()
    state = read_json(session / "session.json")
    require(time.monotonic() + CLEANUP_SECONDS < state["deadline"], "session budget exhausted")
    bucket = "recovery_launches" if phase or "rebind" in label else "matrix_launches"
    limit = MAX_RECOVERY_LAUNCHES if bucket == "recovery_launches" else MAX_MATRIX
    require(state[bucket] < limit, "launch budget exhausted")
    state[bucket] += 1
    write_json(session / "session.json", state)
    deadline = min(time.monotonic() + LAUNCH_SECONDS, state["deadline"] - CLEANUP_SECONDS)
    control = {
        "session": str(session),
        "queue_id": queue_id,
        "deadline": deadline,
        "config": identity(session / "config.yaml"),
        "supervisor_pid": os.getpid(),
        "supervisor_ticks": process_start_ticks(os.getpid()),
        "supervisor_boot": linux_boot_id(),
    }
    control_path = evidence / "control.json"
    write_json(control_path, control)
    worker_argv = [sys.executable, "-I", str(_SCRIPT), "_worker", "--control", str(control_path)]
    trace_argv = [
        "strace",
        "-ff",
        "-ttt",
        "-s",
        "512",
        "-e",
        "trace=open,openat,close,execve,clone,fork,vfork",
        "-o",
        str(evidence / "opens"),
        *worker_argv,
    ]
    write_json(evidence / "commands.json", {"worker": trace_argv})
    observed: list[dict[str, Any]] = []
    scratch_seen: dict[str, Any] = {}
    engine_copies_seen: dict[str, Any] = {}
    triggered = False
    supervisor_timeout_fired = False
    worker_pid = 0
    worker_fd: int | None = None
    with (
        (evidence / "worker.log").open("wb") as log,
        (evidence / "watchdog.log").open("wb") as watchdog_log,
    ):
        watchdog = subprocess.Popen(
            [sys.executable, "-I", str(_SCRIPT), "_watchdog", "--control", str(control_path)],
            stdout=watchdog_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        proc = subprocess.Popen(
            trace_argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            while proc.poll() is None:
                pid_file = evidence / "worker-pid.json"
                if not worker_pid and pid_file.exists():
                    worker_identity = read_json(pid_file)
                    worker_pid = worker_identity["pid"]
                    with contextlib.suppress(ProcessLookupError):
                        worker_fd = os.pidfd_open(worker_pid)
                        require(
                            process_start_ticks(worker_pid) == worker_identity["ticks"],
                            "worker PID identity changed",
                        )
                current_slots = []
                for slot in own_slots(root, queue_id, worker_pid):
                    item = dataclasses.asdict(slot)
                    if slot.queue_id == queue_id:
                        current_slots.append(item)
                    if item not in observed:
                        observed.append(item)
                row = get_entry_by_id(root, queue_id)
                if row is None:
                    raise ValueError("queue row disappeared")
                snapshot = row.metadata["execution_snapshot"]
                output, workspace = _live_files(snapshot, scratch)
                directory = workspace or Path(snapshot["execution_dir"])
                if snapshot.get("version") == 4 and str(directory) not in engine_copies_seen:
                    capture = _capture_engine_copies(snapshot, directory, workspace, current_slots)
                    if capture:
                        engine_copies_seen[str(directory)] = capture
                if workspace and str(workspace) not in scratch_seen:
                    checkpoint = workspace / "probe.moinp.gbw"
                    if checkpoint.is_file():
                        try:
                            scratch_seen[str(workspace)] = identity(checkpoint)
                        except (OSError, ValueError) as exc:
                            scratch_seen[str(workspace)] = {
                                "path": str(checkpoint),
                                "capture_error": str(exc),
                            }
                    elif phase:
                        scratch_seen[str(workspace)] = {"path": str(workspace)}
                if output.is_file():
                    with output.open("rb") as handle:
                        payload = handle.read(16 * 1024 * 1024 + 1)
                    require(
                        len(payload) <= 16 * 1024 * 1024,
                        "live output exceeds diagnostic read budget",
                    )
                    seen = output_observation(payload)
                    reached = (
                        phase == "pre" and seen["startup_seen"] and not seen["finished_guess"]
                    ) or (phase == "post" and seen["finished_guess"])
                    if reached and not triggered and worker_fd is not None:
                        (evidence / "interruption-observed.out").write_bytes(payload)
                        signal.pidfd_send_signal(worker_fd, signal.SIGTERM)
                        triggered = True
                if time.monotonic() >= deadline:
                    if not supervisor_timeout_fired:
                        supervisor_timeout_fired = True
                        write_json(
                            evidence / "supervisor-timeout.json",
                            {"time": time.time(), "queue_id": queue_id},
                        )
                        write_json(
                            session / "STOP.json",
                            {
                                "reason": "supervisor_timeout",
                                "queue_id": queue_id,
                                "time": time.time(),
                            },
                        )
                    _cancel(root, queue_id)
                    break
                if watchdog.poll() is not None and not (evidence / "watchdog-done").exists():
                    raise RuntimeError("watchdog exited unexpectedly")
                time.sleep(0.05)
            end = min(time.monotonic() + CLEANUP_SECONDS, state["deadline"])
            while time.monotonic() < end:
                if proc.poll() is not None and cleanup_complete(
                    root, queue_id, observed, worker_pid
                ):
                    break
                time.sleep(0.2)
            require(
                proc.poll() is not None and cleanup_complete(root, queue_id, observed, worker_pid),
                "cleanup unresolved: retain evidence/reservations and stop; no further launches",
            )
            (evidence / "watchdog-done").touch()
            watchdog.wait(timeout=5)
        except BaseException:
            _cancel(root, queue_id)
            # The independent watchdog remains responsible for bounded observation.
            raise
        finally:
            if worker_fd is not None:
                os.close(worker_fd)
            write_json(
                evidence / "processes.json",
                {
                    "observed_slots": observed,
                    "scratch": scratch_seen,
                    "worker_pid": worker_pid,
                    "engine_copies": engine_copies_seen,
                },
            )
    return {
        "triggered": triggered,
        "worker_returncode": proc.returncode,
        "watchdog_fired": (evidence / "watchdog-fired.json").exists(),
        "supervisor_timeout_fired": supervisor_timeout_fired
        or (evidence / "supervisor-timeout.json").exists(),
        "observed_slots": observed,
        "scratch": scratch_seen,
        "engine_copies": engine_copies_seen,
        "evidence_dir": str(evidence),
    }


def submit(
    session: Path,
    family: str,
    label: str,
    descriptor: dict[str, Any],
    donor: dict[str, Any] | None = None,
):
    from orca_auto.cli import main as cli_main
    from orca_auto.orca.queue.adapter import list_queue

    _reject_stopped_session(session)
    root = session / "runs"
    require(
        not any(row.status.value in {"pending", "running"} for row in list_queue(root)),
        "one pending row at a time",
    )
    job = root / f"{family}-{label}"
    job.mkdir()
    copy_verified(descriptor, job / "probe.inp")
    for item in descriptor["dependencies"]:
        copy_verified(item, job / item["name"])
    if donor is not None:
        copy_verified(donor, job / "probe.moinp.gbw")
    argv = ["run-dir", str(job), "--input", "probe.inp", "--config", str(session / "config.yaml")]
    with (
        (session / "evidence" / f"{family}-{label}-submission.log").open("w") as log,
        contextlib.redirect_stdout(log),
        contextlib.redirect_stderr(log),
    ):
        require(cli_main(argv) == 0, "normal queue submission failed")
    rows = [row for row in list_queue(root) if row.status.value == "pending"]
    require(len(rows) == 1, "submission must produce exactly one pending row")
    row = rows[0]
    require(
        Path(row.metadata["execution_snapshot"]["source_selected_inp"]).parent == job,
        "unexpected queued job",
    )
    write_json(
        session / "evidence" / f"{family}-{label}-submission.json",
        {"argv": argv, "row": dataclasses.asdict(row)},
    )
    return row


def mode_intervention(snapshot: dict[str, Any], mode: int) -> dict[str, Any]:
    from orca_auto.orca.execution_binding import verify_orca_execution_snapshot

    matches = [
        item
        for item in snapshot["materialized_inputs"].values()
        if Path(item["path"]).name == "probe.moinp.gbw"
    ]
    require(len(matches) == 1, "missing materialized checkpoint")
    item = matches[0]
    path = verified_file(item)
    require(path.parent == Path(snapshot["execution_dir"]), "checkpoint outside generation")
    before = identity(path)
    require(before["mode"] == "0o400", "unexpected binding dependency mode")
    path.chmod(mode)
    after = identity(path)
    require(
        before["sha256"] == after["sha256"] and before["size_bytes"] == after["size_bytes"],
        "intervention changed bytes",
    )
    verify_orca_execution_snapshot(
        Path(snapshot["source_selected_inp"]).parent,
        snapshot,
        expected_selected_inp=snapshot["selected_inp"],
        expected_source_selected_inp=snapshot["source_selected_inp"],
        expected_selected_input_xyz=snapshot["selected_input_xyz"],
        expected_resource_request=snapshot["resource_request"],
    )
    return {"before": before, "after": after}


def result_receipt(session: Path, queue_id: str) -> dict[str, Any]:
    from orca_auto.machine_contracts import validate_machine_path
    from orca_auto.orca.input_references import orca_moinp_references
    from orca_auto.orca.queue.adapter import get_entry_by_id
    from orca_auto.orca.state_reading import load_state

    row = get_entry_by_id(session / "runs", queue_id)
    if row is None:
        raise ValueError("result queue row missing")
    snapshot = row.metadata["execution_snapshot"]
    generation = Path(snapshot["execution_dir"])
    output = generation / "probe.out"
    receipt: dict[str, Any] = {
        "row": dataclasses.asdict(row),
        "snapshot": snapshot,
        "state": load_state(generation),
        "machine_valid": False,
    }
    if output.exists():
        receipt["output"] = identity(output)
        refs = orca_moinp_references(Path(snapshot["selected_inp"]).read_text().splitlines())
        receipt["observation"] = output_observation(
            output.read_bytes(), refs[0].value if len(refs) == 1 else "probe.moinp.gbw"
        )
    machine = generation / "machine.json"
    if machine.exists():
        try:
            validate_machine_path(machine)
        except (OSError, ValueError) as exc:
            receipt["machine_error"] = str(exc)
        else:
            receipt["machine_valid"] = True
        receipt["machine"] = identity(machine)
    receipt["artifacts"] = {
        p.name: identity(p)
        for p in generation.iterdir()
        if p.is_file() and p.suffix in {".inp", ".gbw", ".out", ".xyz", ".json", ".html", ".md"}
    }
    observation = receipt.get("observation", {})
    receipt["completed_sp_evidence"] = bool(
        receipt["machine_valid"]
        and observation.get("normal_termination")
        and observation.get("scf_converged")
        and observation.get("energies_eh")
        and all(math.isfinite(value) for value in observation["energies_eh"])
    )
    return receipt


def production(session: Path, family: dict[str, Any], scratch: Path) -> None:
    """Two content controls through the unchanged production copy path; never chmod."""
    use_scratch = read_json(session / "session.json")["use_scratch"]
    input_hash = None
    for label, donor_name, positive in (
        ("V-private", "positive_donor", True),
        ("P-private", "preguess_donor", False),
    ):
        donor = verified_file(family[donor_name])
        donor_before = identity(donor)
        row = submit(session, family["name"], label, family["diagnostic"], family[donor_name])
        snapshot = row.metadata["execution_snapshot"]
        current_hash = snapshot["bound_selected_identity"]["sha256"]
        require(input_hash is None or input_hash == current_hash, "candidate bound inputs differ")
        input_hash = current_hash
        frozen = {
            item["path"]: identity(Path(item["path"]))
            for item in [
                snapshot["bound_selected_identity"],
                *snapshot["materialized_inputs"].values(),
            ]
        }
        observed = run_worker(session, row.queue_id, f"{family['name']}-{label}", scratch)
        receipt = result_receipt(session, row.queue_id)
        receipt.update(monitoring=observed, immutable_before=frozen, donor_before=donor_before)
        result_path = session / "evidence" / f"{family['name']}-{label}-result.json"
        write_json(result_path, receipt)
        _reject_launch_timeouts(observed)
        if receipt["row"]["status"] in {"pending", "running"}:
            _cancel(session / "runs", row.queue_id)
            raise ValueError("capacity refusal/deferral: inconclusive cell; no automatic retry")
        require(identity(donor) == donor_before, "original donor changed")
        for name, before in frozen.items():
            require(identity(Path(name)) == before, "immutable bound input changed")
        boundary = production_copy_receipt(snapshot, observed, use_scratch=use_scratch)
        require(len(boundary) == 1, "expected one engine MOInp copy")
        receipt["production_copy"] = boundary
        observation = receipt.get("observation", {})
        diagnostic = (
            bool(observation.get("moread_consumed_then_scf"))
            if positive
            else (not observation.get("finished_guess") and not observation.get("scf_entered"))
        )
        receipt["publication_valid"] = publication_valid(receipt, use_scratch=use_scratch)
        receipt["production_path_pass"] = bool(diagnostic and receipt["publication_valid"])
        receipt["acceptance_scope"] = "checkpoint_consumption_and_publication"
        write_json(result_path, receipt)
        require(
            receipt["row"]["status"] != "completed" or receipt["completed_sp_evidence"],
            "completed cell lacks validated SP evidence",
        )
        require(
            receipt["production_path_pass"], "production path acceptance failed; inspect evidence"
        )


def matrix(session: Path, family: dict[str, Any], scratch: Path) -> None:
    input_hash = None
    for label, donor_name, mode in (
        ("V400", "positive_donor", 0o400),
        ("V600", "positive_donor", 0o600),
        ("P400", "preguess_donor", 0o400),
        ("P600", "preguess_donor", 0o600),
    ):
        row = submit(session, family["name"], label, family["diagnostic"], family[donor_name])
        snapshot = row.metadata["execution_snapshot"]
        current_hash = snapshot["bound_selected_identity"]["sha256"]
        require(input_hash is None or input_hash == current_hash, "matrix bound inputs differ")
        input_hash = current_hash
        intervention = mode_intervention(snapshot, mode)
        observed = run_worker(session, row.queue_id, f"{family['name']}-{label}", scratch)
        receipt = result_receipt(session, row.queue_id)
        receipt.update(intervention=intervention, monitoring=observed)
        trace_lines: list[str] = []
        for trace in sorted(Path(observed["evidence_dir"]).glob("opens.*")):
            trace_lines.extend(
                line
                for line in trace.read_text(errors="replace").splitlines()
                if "probe.moinp.gbw" in line
            )
        receipt["checkpoint_open_trace"] = trace_lines
        scratch_modes = {item.get("mode") for item in observed["scratch"].values()}
        receipt["diagnostic_pass"] = bool(
            receipt.get("observation", {}).get("moread_consumed_then_scf")
            and receipt["machine_valid"]
            and oct(mode) in scratch_modes
            and not observed["watchdog_fired"]
            and not observed["supervisor_timeout_fired"]
        )
        # Publication is assessed separately: a mutable 0600 dependency may be rejected.
        receipt["publication_review_required"] = True
        write_json(session / "evidence" / f"{family['name']}-{label}-result.json", receipt)
        require(
            receipt["row"]["status"] != "completed" or receipt["completed_sp_evidence"],
            "completed cell lacks validated SP evidence",
        )
        if receipt["row"]["status"] in {"pending", "running"}:
            _cancel(session / "runs", row.queue_id)
            raise ValueError("capacity refusal/deferral: inconclusive cell; no automatic retry")
        _reject_launch_timeouts(observed)


def recovery(session: Path, family: dict[str, Any], scratch: Path) -> None:
    from orca_auto.orca.recovery_orbitals import checkpoint_output_readiness, recovery_guess_policy

    use_scratch = read_json(session / "session.json").get("use_scratch", True)
    for label, phase, source_name in (
        ("pre-default", "pre", "recovery_default"),
        ("post-default", "post", "recovery_default"),
        ("post-explicit", "post", "recovery_explicit"),
    ):
        row = submit(session, family["name"], label, family[source_name])
        first = run_worker(session, row.queue_id, f"{family['name']}-{label}", scratch, phase=phase)
        result_path = session / "evidence" / f"{family['name']}-{label}-result.json"
        evidence: dict[str, Any] = {"first": first}
        write_json(result_path, evidence)
        stopped = result_receipt(session, row.queue_id)
        evidence["stopped"] = stopped
        write_json(result_path, evidence)
        _reject_launch_timeouts(first)
        generation = Path(stopped["snapshot"]["execution_dir"])
        output = generation / "probe.out"
        ready = checkpoint_output_readiness(output.read_bytes()) if output.is_file() else None
        matched = (
            first["triggered"]
            and stopped["row"]["status"] == "pending"
            and not stopped.get("observation", {}).get("normal_termination")
        )
        matched = matched and ready is not None and ready.eligible == (phase == "post")
        if phase == "post":
            matched = matched and (generation / "probe.gbw").is_file()
        if not matched:
            evidence["verdict"] = "inconclusive_phase_miss"
            write_json(result_path, evidence)
            _cancel(session / "runs", row.queue_id)
            raise ValueError("interruption missed its intended phase; no automatic retry")
        stopped["publication_valid"] = interrupted_publication_valid(
            stopped, use_scratch=use_scratch
        )
        write_json(result_path, evidence)
        if not stopped["publication_valid"]:
            _cancel(session / "runs", row.queue_id)
            raise ValueError("interrupted publication missing or invalid; stop before rebind")
        frozen = {
            name: item
            for name, item in stopped["artifacts"].items()
            if Path(name).suffix in {".inp", ".gbw", ".out", ".xyz"}
        }
        second = run_worker(session, row.queue_id, f"{family['name']}-{label}-rebind", scratch)
        evidence["second"] = second
        write_json(result_path, evidence)
        final = result_receipt(session, row.queue_id)
        evidence["final"] = final
        write_json(result_path, evidence)
        _reject_launch_timeouts(second)
        snapshot = final["snapshot"]
        require(
            snapshot["generation_name"] != stopped["snapshot"]["generation_name"],
            "recovery did not create a fresh generation",
        )
        for name, before in frozen.items():
            require(identity(generation / name) == before, "prior scientific artifact changed")
        seed_expected = label == "post-default"
        require(
            bool(snapshot["recovery"]["checkpoint_role"]) == seed_expected,
            "unexpected recovery checkpoint selection",
        )
        bound = Path(snapshot["selected_inp"]).read_text()
        require(
            ("probe.moinp.gbw" in bound) == seed_expected,
            "bound input disagrees with recovery seed",
        )
        observation = final.get("observation", {})
        scientific = bool(final["completed_sp_evidence"])
        scientific = scientific and publication_valid(final, use_scratch=use_scratch)
        if seed_expected:
            scientific = scientific and bool(observation.get("moread_consumed_then_scf"))
            final["production_copy"] = production_copy_receipt(
                snapshot, second, use_scratch=use_scratch
            )
            require(len(final["production_copy"]) == 1, "missing automatic engine copy")
        if label == "post-explicit":
            require(
                recovery_guess_policy(bound.splitlines()) == "explicit_fresh_guess",
                "bound explicit guess changed",
            )
            scientific = scientific and family["explicit_guess_output"] in observation.get(
                "guess_labels", []
            )
        attempts = (final.get("state") or {}).get("attempts", [])
        scientific = scientific and len(attempts) == 1 and final["row"]["status"] == "completed"
        evidence.update(
            scientific_acceptance_pass=scientific,
            acceptance_scope=family.get("acceptance_scope", "family_science"),
        )
        write_json(result_path, evidence)
        require(
            scientific, "recovery lacked completed SP/scientific/receipt evidence; stop for review"
        )


def session_setup(plan: dict[str, Any], facts: dict[str, Any]) -> Path:
    from orca_auto.core.utils.process import linux_boot_id

    session = Path(plan["experiment_root"]) / plan["session"]
    require(not session.is_symlink() and session.resolve() == session, "unsafe session directory")
    session.mkdir(parents=True, exist_ok=True)
    marker = session / "session.json"
    require(
        not (session / "STOP.json").exists(),
        "session stopped; inspect retained evidence before a new experiment",
    )
    fingerprint = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    if marker.exists():
        require(read_json(marker)["plan_sha256"] == fingerprint, "session plan changed")
        require(read_json(marker)["boot_id"] == linux_boot_id(), "session crossed a reboot")
        require(
            identity(session / "config.yaml")["sha256"] == read_json(marker)["config_sha256"],
            "experiment config changed",
        )
        require(
            (session / "runs" / ".admission").resolve()
            == (Path(facts["operational_root"]) / ".admission").resolve(),
            "shared admission link changed",
        )
        require(time.monotonic() < read_json(marker)["deadline"], "session expired; do not reuse")
        return session
    require(
        not [p for p in session.iterdir() if p.name != "supervisor.lock"],
        "new session must be empty",
    )
    (session / "runs").mkdir()
    (session / "evidence").mkdir()
    (session / "runs" / ".admission").symlink_to(
        Path(facts["operational_root"]) / ".admission", target_is_directory=True
    )
    raw = yaml.safe_load(verified_file(plan["production_config"]).read_text())
    raw["runs_root"] = str(session / "runs")
    raw["messenger"] = {
        "provider": "discord",
        "discord": {"bot_token": "", "default_channel_id": ""},
    }
    # Keep the operational scheduler; only an explicit candidate plan can test
    # the supported no-scratch path in this experiment's own configuration.
    if not facts["use_scratch"]:
        runtime = raw.get("orca", {}).get("runtime", {})
        runtime.pop("scratch_root", None)
        runtime.pop("scratch_min_free_gb", None)
    (session / "config.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    os.chmod(session / "config.yaml", 0o600)
    write_json(
        marker,
        {
            "schema": "orca-checkpoint-acceptance-v1",
            "plan_sha256": fingerprint,
            "deadline": time.monotonic() + SESSION_SECONDS,
            "boot_id": linux_boot_id(),
            "config_sha256": identity(session / "config.yaml")["sha256"],
            "matrix_launches": 0,
            "recovery_launches": 0,
            "use_scratch": facts["use_scratch"],
        },
    )
    write_json(
        session / "evidence" / "preflight.json",
        {"facts": facts, "source_revision_owner_attestation": plan["source_revision"]},
    )
    return session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "identity",
            "preflight",
            "matrix",
            "production",
            "recovery",
            "_worker",
            "_watchdog",
        ],
    )
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--family")
    parser.add_argument(
        "--for",
        dest="preflight_for",
        choices=["matrix", "production", "recovery"],
        default="matrix",
    )
    parser.add_argument("--control", type=Path)
    args = parser.parse_args(argv)
    if args.action == "identity":
        print(json.dumps(runtime_identity(), indent=2))
        return 0
    if args.action.startswith("_"):
        return private_process(args.action, args.control)
    require(
        args.plan and args.family,
        "--plan and --family are required; donors/inputs are never invented",
    )
    plan = read_json(args.plan)
    require(plan["schema"] == "orca-checkpoint-acceptance-v1", "unknown plan schema")
    action = args.preflight_for if args.action == "preflight" else args.action
    facts = preflight(plan, args.family, action)
    if args.action == "preflight":
        print(json.dumps(facts, indent=2))
        return 0
    session = Path(plan["experiment_root"]) / plan["session"]
    require(not session.is_symlink() and session.resolve() == session, "unsafe session directory")
    session.mkdir(parents=True, exist_ok=True)
    with (session / "supervisor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session_setup(plan, facts)
        try:
            require(not own_slots(session / "runs"), "experiment retains admission slots; stop")
            family = next(f for f in plan["families"] if f["name"] == args.family)
            {"matrix": matrix, "production": production, "recovery": recovery}[action](
                session, family, Path(facts["scratch_root"])
            )
        except BaseException as exc:
            write_json(session / "STOP.json", {"reason": str(exc), "time": time.time()})
            raise
    print(f"Evidence: {session / 'evidence'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
