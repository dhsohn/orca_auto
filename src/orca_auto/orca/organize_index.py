from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orca_auto.core.utils import process_lock

from .state import atomic_write_text, load_state, now_utc_iso

logger = logging.getLogger(__name__)

INDEX_DIR_NAME = "index"
RECORDS_FILE_NAME = "records.jsonl"
LOCK_FILE_NAME = "index.lock"
FAILED_ROLLBACKS_FILE_NAME = "failed_rollbacks.jsonl"


def index_dir(organized_root: Path) -> Path:
    return organized_root / INDEX_DIR_NAME


def records_path(organized_root: Path) -> Path:
    return index_dir(organized_root) / RECORDS_FILE_NAME


def load_index(organized_root: Path) -> dict[str, dict[str, Any]]:
    rp = records_path(organized_root)
    if not rp.exists():
        return {}

    result: dict[str, dict[str, Any]] = {}
    try:
        text = rp.read_text(encoding="utf-8")
    except OSError:
        return result

    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            logger.warning("Malformed JSONL at line %d in %s", line_number, rp)
            continue
        if not isinstance(record, dict):
            continue
        run_id = record.get("run_id")
        if isinstance(run_id, str) and run_id.strip():
            result[run_id] = record

    return result


def to_reaction_relative_path(path_value: Any, reaction_dir: Path) -> str:
    if not isinstance(path_value, str):
        return ""
    raw = path_value.strip()
    if not raw:
        return ""

    p = Path(raw)
    if p.is_absolute():
        try:
            return str(p.relative_to(reaction_dir))
        except ValueError:
            return ""

    normalized = p
    if normalized.parts and normalized.parts[0] == ".":
        normalized = Path(*normalized.parts[1:])
    return str(normalized)


def _build_index_record(
    organized_root: Path,
    reaction_dir: Path,
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    if state.get("status") != "completed":
        return None
    final_result = state.get("final_result")
    if not isinstance(final_result, dict):
        return None

    run_id = state.get("run_id", "")
    if not run_id:
        return None

    try:
        rel = reaction_dir.relative_to(organized_root)
    except ValueError:
        rel = reaction_dir
    organized_path = str(rel)

    from .result_organizer_planning import detect_job_type, resolve_organize_metadata

    selected_inp = state.get("selected_inp", "")
    inp_path, job_type, molecule_key = resolve_organize_metadata(state, reaction_dir)
    if inp_path is None:
        inps = sorted(reaction_dir.glob("*.inp"))
        if inps:
            inp_path = inps[0]
            job_type = detect_job_type(inp_path)
            from .molecule_key import resolve_molecule_key

            molecule_key = resolve_molecule_key(inp_path).key

    attempts = state.get("attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) else 0

    selected_inp_rel = to_reaction_relative_path(selected_inp, reaction_dir)
    if not selected_inp_rel and inp_path is not None:
        selected_inp_rel = str(inp_path.relative_to(reaction_dir))
    last_out_rel = to_reaction_relative_path(final_result.get("last_out_path", ""), reaction_dir)

    return {
        "run_id": run_id,
        "reaction_dir": str(reaction_dir),
        "status": "completed",
        "analyzer_status": final_result.get("analyzer_status", ""),
        "reason": final_result.get("reason", ""),
        "job_type": job_type,
        "molecule_key": molecule_key,
        "selected_inp": selected_inp_rel,
        "last_out_path": last_out_rel,
        "attempt_count": attempt_count,
        "completed_at": final_result.get("completed_at", ""),
        "organized_at": now_utc_iso(),
        "organized_path": organized_path,
    }


def rebuild_index(organized_root: Path) -> int:
    root_exists = organized_root.exists()
    idir = index_dir(organized_root)
    idir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    if not root_exists:
        atomic_write_text(records_path(organized_root), "")
        return 0

    for state_file in sorted(organized_root.rglob("job_state.json")):
        if INDEX_DIR_NAME in state_file.parts:
            continue
        reaction_dir = state_file.parent
        state = load_state(reaction_dir)
        if state is None:
            continue
        record = _build_index_record(organized_root, reaction_dir, state)
        if record is None:
            continue
        records.append(record)

    lines = [json.dumps(r, ensure_ascii=True) for r in records]
    content = "\n".join(lines) + "\n" if lines else ""
    atomic_write_text(records_path(organized_root), content)
    logger.info("Index rebuilt: %d records in %s", len(records), records_path(organized_root))
    return len(records)


def _assert_index_locked(organized_root: Path) -> None:
    lock_path = index_dir(organized_root) / LOCK_FILE_NAME
    if not lock_path.exists():
        logger.warning(
            "append_record/append_failed_rollback called without holding "
            "the index lock — data corruption risk. Lock file: %s",
            lock_path,
        )


def _append_jsonl_entry(path: Path, entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def append_record(organized_root: Path, record: dict[str, Any]) -> None:
    _assert_index_locked(organized_root)
    idir = index_dir(organized_root)
    idir.mkdir(parents=True, exist_ok=True)
    _append_jsonl_entry(records_path(organized_root), record)


def append_failed_rollback(organized_root: Path, entry: dict[str, Any]) -> None:
    _assert_index_locked(organized_root)
    idir = index_dir(organized_root)
    idir.mkdir(parents=True, exist_ok=True)
    _append_jsonl_entry(idir / FAILED_ROLLBACKS_FILE_NAME, entry)


def _index_lock_timeout_error(lock_path: Path, timeout_seconds: int) -> RuntimeError:
    return RuntimeError(
        f"Index lock acquisition timed out after {timeout_seconds}s. Lock file: {lock_path}"
    )


@contextmanager
def acquire_index_lock(organized_root: Path, timeout_seconds: int = 30) -> Iterator[None]:
    idir = index_dir(organized_root)
    idir.mkdir(parents=True, exist_ok=True)
    lock_path = idir / LOCK_FILE_NAME
    lock_payload_obj: dict[str, Any] = {"pid": os.getpid(), "started_at": now_utc_iso()}
    current_start_ticks = process_lock.current_process_start_ticks()
    if current_start_ticks is not None:
        lock_payload_obj["process_start_ticks"] = current_start_ticks

    with process_lock.acquire_file_lock(
        lock_path=lock_path,
        lock_payload_obj=lock_payload_obj,
        parse_lock_info_fn=process_lock.parse_lock_info,
        is_process_alive_fn=process_lock.is_process_alive,
        process_start_ticks_fn=process_lock.process_start_ticks,
        logger=logger,
        acquired_log_template="Index lock acquired: %s",
        released_log_template="Index lock released: %s",
        stale_pid_reuse_log_template=(
            "Stale index lock detected due PID reuse (pid=%d, expected_ticks=%d, observed_ticks=%d): %s"
        ),
        stale_lock_log_template="Stale index lock (pid=%d), removing: %s",
        timeout_seconds=timeout_seconds,
        timeout_error_builder=_index_lock_timeout_error,
    ):
        yield
