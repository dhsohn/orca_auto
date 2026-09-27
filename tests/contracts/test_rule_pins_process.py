"""Rule pins for process-owner liveness and ``/proc/<pid>/stat`` parsing.

Four durable owner records decide liveness with their own policy today: the
admission slot, the snapshot intent, the scratch workspace manifest and the
worker pid file. ``test_owner_liveness_truth_table`` feeds each record the same
identity facts and host answers (``os.kill``, current boot id, observed start
ticks) and pins every outcome in ``pins/process_owner_liveness.json``.

Three parsers read field 22 of ``/proc/<pid>/stat``; ``test_proc_stat_parsers``
pins their answers over one corpus in ``pins/process_stat_parsers.json``.
"""

from __future__ import annotations

import errno
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from orca_auto import cli_systemd_evidence
from orca_auto.core.admission import store as admission_store
from orca_auto.core.admission.records import slot_from_dict
from orca_auto.core.engine_scratch import _manifest as scratch_manifest
from orca_auto.core.queue import publication
from orca_auto.core.queue.engine import snapshot_intent
from orca_auto.core.queue.worker.pid_file import read_worker_pid_file, worker_pid_file_path
from orca_auto.core.utils import process as process_utils
from tests.contracts.normalize import assert_pin

_PID = 4242
_TICKS = 777
_BOOT = "boot-a"

_PIDS = {"valid": _PID, "zero": 0}
_RECORDED_TICKS: dict[str, int | None] = {"present": _TICKS, "absent": None, "zero": 0}
_RECORDED_BOOTS: dict[str, str | None] = {"present": _BOOT, "absent": None}
_CURRENT_BOOTS: dict[str, str | None] = {"equal": _BOOT, "different": "boot-b", "unknown": None}
_KILLS: dict[str, OSError | None] = {
    "ok": None,
    "ESRCH": ProcessLookupError(errno.ESRCH, "No such process"),
    "EPERM": PermissionError(errno.EPERM, "Operation not permitted"),
    "other": OSError(errno.EINVAL, "Invalid argument"),
}
_OBSERVED_TICKS: dict[str, int | None] = {"equal": _TICKS, "different": 999, "unknown": None}


def _identity(names: tuple[str, str, str], pid: int, ticks: int | None, boot: str | None) -> dict:
    """An owner identity under the record's own key names; ``None`` omits the key."""
    values = (pid, ticks, boot)
    return {name: value for name, value in zip(names, values, strict=True) if value is not None}


def _slot_outcome(pid: int, ticks: int | None, boot: str | None) -> str:
    raw: dict[str, object] = {
        "token": "slot-pin",
        "owner_pid": pid,
        "process_start_ticks": ticks,
        "owner_boot_id": boot,
        "source": "orca_queue_worker",
        "acquired_at": "2026-01-02T03:04:05.000006+00:00",
        "app_name": "orca_auto",
        "task_id": "",
        "state": "reserved",
        "work_dir": "",
        "queue_id": "",
        "engine_process_state": "idle",
        "engine_launch_gated": False,
        "engine_pid": None,
        "engine_pgid": None,
        "engine_process_start_ticks": None,
        "engine_process_boot_id": None,
    }
    try:
        slot = slot_from_dict(raw)
    except ValueError:
        return "rejected"
    return "live" if admission_store._slot_owner_alive(slot) else "stale"


def _intent_outcome(pid: int, ticks: int | None, boot: str | None) -> str:
    marker = _identity(
        ("owner_pid", "owner_process_start_ticks", "owner_boot_id"), pid, ticks, boot
    )
    return "live" if snapshot_intent._owner_is_alive(marker) else "stale"


def _manifest_outcome(pid: int, ticks: int | None, boot: str | None) -> str:
    payload = _identity(
        ("owner_pid", "owner_process_start_ticks", "owner_boot_id"), pid, ticks, boot
    )
    return scratch_manifest._manifest_owner_state(payload)


def _pid_file_outcome(root: Path, pid: int, ticks: int | None, boot: str | None) -> str:
    payload = _identity(("pid", "process_start_ticks", "boot_id"), pid, ticks, boot)
    payload["started_at"] = "2026-01-02T03:04:05.000006+00:00"
    path = worker_pid_file_path(root)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    live = read_worker_pid_file(root) is not None
    kept = path.exists()
    path.unlink(missing_ok=True)
    return ("live" if live else "stale") + ("" if kept else "+removed")


def test_owner_liveness_truth_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host: dict[str, Any] = {}

    def kill(pid: int, sig: int) -> None:
        assert sig == 0, "a liveness probe only sends signal 0"
        error = host["kill"]
        if error is not None:
            raise error

    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(process_utils, "linux_boot_id", lambda **_kwargs: host["boot"])
    monkeypatch.setattr(process_utils, "process_start_ticks", lambda _pid, **_kwargs: host["ticks"])

    table: dict[str, str] = {}
    for (pid_name, pid), (ticks_name, ticks), (boot_name, boot) in itertools.product(
        _PIDS.items(), _RECORDED_TICKS.items(), _RECORDED_BOOTS.items()
    ):
        for (current_name, current), (kill_name, kill_error), (
            observed_name,
            observed,
        ) in itertools.product(_CURRENT_BOOTS.items(), _KILLS.items(), _OBSERVED_TICKS.items()):
            host.update(boot=current, kill=kill_error, ticks=observed)
            key = (
                f"pid={pid_name} ticks={ticks_name} boot={boot_name} "
                f"current_boot={current_name} kill={kill_name} observed_ticks={observed_name}"
            )
            table[key] = (
                f"slot={_slot_outcome(pid, ticks, boot)} "
                f"intent={_intent_outcome(pid, ticks, boot)} "
                f"manifest={_manifest_outcome(pid, ticks, boot)} "
                f"pid_file={_pid_file_outcome(tmp_path, pid, ticks, boot)}"
            )
    assert_pin("process_owner_liveness.json", table)


def _stat_line(comm: bytes, starttime: bytes, *, fields_after_comm: int = 50) -> bytes:
    """A ``/proc/<pid>/stat`` line whose field 22 (starttime) is ``starttime``."""
    fields = [b"S", b"1", b"1234", b"1234"] + [b"0"] * 15 + [starttime]
    fields += [b"0"] * max(0, fields_after_comm - len(fields))
    return b"1234 (" + comm + b") " + b" ".join(fields[:fields_after_comm]) + b"\n"


_STAT_CORPUS: dict[str, bytes] = {
    "plain": _stat_line(b"python3", b"98765"),
    "comm_with_spaces": _stat_line(b"tmux: server", b"98765"),
    "comm_with_close_paren_and_fields": _stat_line(b"evil) S 1 2 3", b"98765"),
    "comm_with_nested_parens": _stat_line(b"a(b)c", b"98765"),
    "comm_invalid_utf8": _stat_line(b"bad\xffname", b"98765"),
    "exactly_twenty_fields_after_comm": _stat_line(b"python3", b"98765", fields_after_comm=20),
    "nineteen_fields_after_comm": _stat_line(b"python3", b"98765", fields_after_comm=19),
    "short_line": b"1234 (python3) S 1 1234\n",
    "non_numeric_starttime": _stat_line(b"python3", b"abc"),
    "plus_signed_starttime": _stat_line(b"python3", b"+5"),
    "negative_starttime": _stat_line(b"python3", b"-5"),
    "zero_starttime": _stat_line(b"python3", b"0"),
    "huge_starttime": _stat_line(b"python3", b"18446744073709551615"),
    "no_parentheses": b"1234 python3 " + _stat_line(b"x", b"98765").split(b") ", 1)[1],
    "empty": b"",
    "whitespace_only": b"  \n",
}


def _answer(call: Any) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the raised type is the pinned answer
        return f"raise {type(exc).__name__}: {exc}"


def test_proc_stat_parsers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = tmp_path / "proc"
    stat_path = proc / "1234" / "stat"
    stat_path.parent.mkdir(parents=True)
    # ``process_start_token`` reads the fixed ``/proc`` path; point it at the corpus.
    monkeypatch.setattr(publication, "Path", lambda text: proc / Path(text).relative_to("/proc"))
    monkeypatch.setattr(publication, "_linux_boot_id", lambda: "boot-a")

    table: dict[str, dict[str, Any]] = {}
    for name, raw in _STAT_CORPUS.items():
        stat_path.write_bytes(raw)
        table[name] = {
            "utils.process_start_ticks": _answer(
                lambda: process_utils.process_start_ticks(1234, proc_root=proc)
            ),
            "publication.process_start_token": _answer(
                lambda: publication.process_start_token(1234)
            ),
            "cli_systemd_evidence.parse_process_start_ticks": _answer(
                lambda raw=raw: cli_systemd_evidence.parse_process_start_ticks(raw, pid=1234)
            ),
        }
    assert_pin("process_stat_parsers.json", table)
