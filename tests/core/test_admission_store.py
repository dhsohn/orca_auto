from __future__ import annotations

import json
from dataclasses import replace
from os import PathLike
from pathlib import Path

import pytest

from orca_auto.core.admission import store


def _patch_deterministic_liveness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_pid: int = 4242,
    live_pids: set[int] | None = None,
    ticks: dict[int, int] | None = None,
) -> None:
    live = live_pids or {current_pid}
    tick_map = {current_pid: 12345}
    if ticks:
        tick_map.update(ticks)

    monkeypatch.setattr(store.os, "getpid", lambda: current_pid)

    def fake_kill(pid: int, sig: int) -> None:
        if pid not in live:
            raise ProcessLookupError("process is not alive")

    monkeypatch.setattr(store.os, "kill", fake_kill)
    monkeypatch.setattr(store, "_process_start_ticks", lambda pid: tick_map.get(pid))
    monkeypatch.setattr(store, "_linux_boot_id", lambda: "test-boot-id")
    monkeypatch.setattr(store, "timestamped_token", lambda prefix: f"{prefix}_fixed")
    monkeypatch.setattr(store, "now_utc_iso", lambda: "2026-04-19T00:00:00+00:00")


def _read_slots_file(root: Path) -> list[dict[str, object]]:
    return json.loads((root / store.ADMISSION_FILE_NAME).read_text(encoding="utf-8"))


def _proc_stat_text(start_ticks: str) -> str:
    fields = ["S"] + [str(index) for index in range(1, 19)] + [start_ticks]
    return f"1234 (python) {' '.join(fields)}"


def test_process_start_ticks_handles_parse_failures_and_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_root = tmp_path / "proc"

    def fake_path(*parts: str | PathLike[str]) -> Path:
        if parts == ("/proc",):
            return proc_root
        return Path(*parts)

    monkeypatch.setattr(store, "Path", fake_path)

    assert store._process_start_ticks(999) is None

    pid = 1234
    stat_dir = proc_root / str(pid)
    stat_dir.mkdir(parents=True)
    stat_file = stat_dir / "stat"

    stat_file.write_text("", encoding="utf-8")
    assert store._process_start_ticks(pid) is None

    stat_file.write_text("1234 no-right-paren", encoding="utf-8")
    assert store._process_start_ticks(pid) is None

    stat_file.write_text("1234 (python) S 1 2 3", encoding="utf-8")
    assert store._process_start_ticks(pid) is None

    stat_file.write_text(_proc_stat_text("not-an-int"), encoding="utf-8")
    assert store._process_start_ticks(pid) is None

    stat_file.write_text(_proc_stat_text("0"), encoding="utf-8")
    assert store._process_start_ticks(pid) is None

    stat_file.write_text(_proc_stat_text("54321"), encoding="utf-8")
    assert store._process_start_ticks(pid) == 54321


def test_normalize_work_dir_handles_none_blank_and_resolve_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert store._normalize_work_dir(None) == ""
    assert store._normalize_work_dir("   ") == ""

    class ExplodingPath:
        def __init__(self, value: str) -> None:
            self.value = value

        def expanduser(self) -> ExplodingPath:
            return self

        def resolve(self) -> str:
            raise OSError("cannot resolve")

    monkeypatch.setattr(store, "Path", ExplodingPath)

    assert store._normalize_work_dir(" relative/run ") == "relative/run"


def test_slot_owner_alive_handles_dead_pid_and_missing_start_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        store._slot_owner_alive(
            store.AdmissionSlot(
                token="slot",
                owner_pid=0,
                process_start_ticks=1,
                source="test",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        )
        is False
    )

    monkeypatch.setattr(store.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(store, "_process_start_ticks", lambda pid: None)

    assert (
        store._slot_owner_alive(
            store.AdmissionSlot(
                token="slot",
                owner_pid=4242,
                process_start_ticks=None,
                source="test",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        )
        is True
    )

    assert (
        store._slot_owner_alive(
            store.AdmissionSlot(
                token="slot",
                owner_pid=4242,
                process_start_ticks=777,
                source="test",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        )
        is True
    )


def test_slot_owner_alive_treats_permission_denied_as_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_kill(_pid: int, _sig: int) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(store.os, "kill", fake_kill)
    monkeypatch.setattr(store, "_process_start_ticks", lambda _pid: None)

    assert (
        store._slot_owner_alive(
            store.AdmissionSlot(
                token="slot",
                owner_pid=4242,
                process_start_ticks=777,
                source="test",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        )
        is True
    )


def test_slot_owner_alive_still_rejects_permission_denied_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_kill(_pid: int, _sig: int) -> None:
        raise PermissionError("permission denied")

    monkeypatch.setattr(store.os, "kill", fake_kill)
    monkeypatch.setattr(store, "_process_start_ticks", lambda _pid: 888)

    assert (
        store._slot_owner_alive(
            store.AdmissionSlot(
                token="slot",
                owner_pid=4242,
                process_start_ticks=777,
                source="test",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        )
        is False
    )


def test_generic_slot_owner_identity_is_scoped_to_the_current_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_deterministic_liveness(monkeypatch)
    token = store.reserve_slot(tmp_path, 1, source="generic")
    assert token is not None
    slot = store.get_slot(tmp_path, token)
    assert slot is not None and slot.owner_boot_id == "test-boot-id"

    monkeypatch.setattr(store, "_linux_boot_id", lambda: "later-boot-id")
    monkeypatch.setattr(
        store.os,
        "kill",
        lambda *_args: pytest.fail("cross-boot owner PID must not be probed"),
    )

    assert store._slot_owner_alive(slot) is False


@pytest.mark.parametrize(
    "stat_text",
    [
        "",
        "1 (proc) " + " ".join(str(i) for i in range(19)),
        "1 (proc) " + " ".join(["0"] * 19 + ["bad"]),
    ],
)
def test_process_start_ticks_returns_none_for_unparseable_stat(
    monkeypatch: pytest.MonkeyPatch, stat_text: str
) -> None:
    def fake_read_text(self: Path, encoding: str = "utf-8", errors: str = "strict") -> str:
        return stat_text

    monkeypatch.setattr(store.Path, "read_text", fake_read_text)

    assert store._process_start_ticks(1234) is None


def test_process_start_ticks_parses_valid_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_read_text(self: Path, encoding: str = "utf-8", errors: str = "strict") -> str:
        return "1 (proc) " + " ".join(str(i) for i in range(1, 21))

    monkeypatch.setattr(store.Path, "read_text", fake_read_text)

    assert store._process_start_ticks(1234) == 20


def test_normalize_work_dir_handles_none_and_oserror_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolve(self: Path) -> Path:
        raise OSError("cannot resolve")

    monkeypatch.setattr(store.Path, "resolve", fake_resolve)

    assert store._normalize_work_dir(None) == ""
    assert store._normalize_work_dir("relative/path") == "relative/path"


def test_slot_owner_alive_handles_non_positive_pid_and_missing_process_start_ticks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_kill(_pid: int, _sig: int) -> None:
        raise AssertionError("kill should not be called for non-positive pids")

    monkeypatch.setattr(store.os, "kill", fake_kill)

    assert (
        store._slot_owner_alive(
            store.AdmissionSlot(
                token="t-1",
                owner_pid=0,
                process_start_ticks=123,
                source="source",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        )
        is False
    )

    monkeypatch.setattr(store.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(store, "_process_start_ticks", lambda _pid: None)

    assert (
        store._slot_owner_alive(
            store.AdmissionSlot(
                token="t-2",
                owner_pid=4242,
                process_start_ticks=None,
                source="source",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        )
        is True
    )


def test_activate_reserved_slot_returns_none_when_token_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_deterministic_liveness(monkeypatch)

    assert store.activate_reserved_slot(tmp_path, "missing-token") is None


def test_reconcile_stale_slots_removes_dead_entries_and_keeps_live_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_deterministic_liveness(
        monkeypatch,
        live_pids={4242, 1111},
        ticks={1111: 111},
    )

    slots = [
        {
            "token": "live",
            "owner_pid": 1111,
            "process_start_ticks": 111,
            "source": "live-source",
            "acquired_at": "2026-04-19T00:00:00+00:00",
        },
        {
            "token": "dead",
            "owner_pid": 2222,
            "process_start_ticks": 222,
            "source": "dead-source",
            "acquired_at": "2026-04-19T00:00:00+00:00",
        },
    ]
    (tmp_path / store.ADMISSION_FILE_NAME).write_text(json.dumps(slots), encoding="utf-8")

    removed = store.reconcile_stale_slots(tmp_path)

    assert removed == 1
    assert store.list_slots(tmp_path) == [
        store.AdmissionSlot(
            token="live",
            owner_pid=1111,
            process_start_ticks=111,
            source="live-source",
            acquired_at="2026-04-19T00:00:00+00:00",
        )
    ]
    assert _read_slots_file(tmp_path) == [
        {
            "token": "live",
            "owner_pid": 1111,
            "process_start_ticks": 111,
            "source": "live-source",
            "acquired_at": "2026-04-19T00:00:00+00:00",
            "app_name": "",
            "task_id": "",
            "workflow_id": "",
            "state": "active",
            "work_dir": "",
            "queue_id": "",
        }
    ]


def test_read_active_slot_count_does_not_prune_or_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_deterministic_liveness(monkeypatch, live_pids={4242})
    store._save_slots(
        tmp_path,
        [
            store.AdmissionSlot(
                token="dead",
                owner_pid=2222,
                process_start_ticks=222,
                source="queue",
                acquired_at="2026-04-19T00:00:00+00:00",
            )
        ],
    )
    path = tmp_path / store.ADMISSION_FILE_NAME
    before = path.read_bytes()

    assert store.read_active_slot_count(tmp_path) == 0
    assert path.read_bytes() == before


def test_reserve_slot_honors_capacity_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_deterministic_liveness(monkeypatch)

    first = store.reserve_slot(tmp_path, 1, source="queue-1")
    second = store.reserve_slot(tmp_path, 1, source="queue-2")

    assert first == "slot_fixed"
    assert second is None
    assert store.active_slot_count(tmp_path) == 1
    monkeypatch.setattr(store, "timestamped_token", lambda prefix: f"{prefix}_second")
    assert store.reserve_slot(tmp_path, 2, source="queue-4") == "slot_second"
    assert store.reserve_slot(tmp_path, 1, source="queue-3") is None


def test_reserve_slot_retries_collision_and_mutates_only_selected_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_deterministic_liveness(monkeypatch)
    generated = iter(["slot_same", "slot_same", "slot_unique"])
    monkeypatch.setattr(store, "timestamped_token", lambda _prefix: next(generated))

    first = store.reserve_slot(tmp_path, 2, source="first", state="reserved")
    second = store.reserve_slot(tmp_path, 2, source="second", state="reserved")

    assert first == "slot_same"
    assert second == "slot_unique"
    assert [slot.token for slot in store.list_all_slots(tmp_path)] == [first, second]

    activated = store.activate_reserved_slot(tmp_path, second, source="activated-second")
    assert activated is not None and activated.token == second
    slots = store.list_all_slots(tmp_path)
    assert [(slot.token, slot.source, slot.state) for slot in slots] == [
        (first, "first", "reserved"),
        (second, "activated-second", "active"),
    ]

    assert store.release_slot(tmp_path, first) is True
    [remaining] = store.list_all_slots(tmp_path)
    assert remaining.token == second
    assert remaining.source == "activated-second"


def test_reserve_slot_permanent_collision_preserves_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_deterministic_liveness(monkeypatch)
    assert store.reserve_slot(tmp_path, 2, source="first") == "slot_fixed"
    admission_path = tmp_path / store.ADMISSION_FILE_NAME
    original = admission_path.read_bytes()

    with pytest.raises(RuntimeError, match="unique admission slot token"):
        store.reserve_slot(tmp_path, 2, source="second")

    assert admission_path.read_bytes() == original
    [remaining] = store.list_all_slots(tmp_path)
    assert remaining.token == "slot_fixed"
    assert remaining.source == "first"


def test_save_slots_rejects_duplicate_tokens_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_deterministic_liveness(monkeypatch)
    token = store.reserve_slot(tmp_path, 2, source="first")
    assert token == "slot_fixed"
    admission_path = tmp_path / store.ADMISSION_FILE_NAME
    original = admission_path.read_bytes()
    [slot] = store.list_all_slots(tmp_path)

    with pytest.raises(store.AdmissionStoreCorruptError, match="duplicate token"):
        store._save_slots(tmp_path, [slot, replace(slot, source="wrong-owner")])

    assert admission_path.read_bytes() == original
    [remaining] = store.list_all_slots(tmp_path)
    assert remaining.source == "first"


def test_reserve_activate_and_release_slot_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_deterministic_liveness(
        monkeypatch,
        live_pids={4242, 5151},
        ticks={5151: 5151},
    )

    token = store.reserve_slot(
        tmp_path,
        2,
        source="reserve-source",
        app_name="chem-app",
        task_id="task-7",
        workflow_id="wf-9",
        state="reserved",
        work_dir="subdir/run",
        queue_id="queue-a",
    )
    assert token == "slot_fixed"

    activated = store.activate_reserved_slot(
        tmp_path,
        token,
        state="active",
        work_dir=".",
        queue_id="queue-b",
        owner_pid=5151,
        source="activate-source",
    )
    assert activated == store.AdmissionSlot(
        token="slot_fixed",
        owner_pid=5151,
        process_start_ticks=5151,
        source="activate-source",
        acquired_at="2026-04-19T00:00:00+00:00",
        app_name="chem-app",
        task_id="task-7",
        workflow_id="wf-9",
        state="active",
        work_dir=str(Path(".").resolve()),
        queue_id="queue-b",
        owner_boot_id="test-boot-id",
    )

    assert store.release_slot(tmp_path, token) is True
    assert store.release_slot(tmp_path, token) is False
    assert store.list_slots(tmp_path) == []


def test_activate_reserved_slot_returns_none_for_missing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_deterministic_liveness(monkeypatch)

    assert store.reserve_slot(tmp_path, 1, source="reserve-source") == "slot_fixed"
    assert store.activate_reserved_slot(tmp_path, "missing-token") is None


@pytest.mark.parametrize("bad_payload", ["{not json", json.dumps({"token": "oops"})])
def test_invalid_admission_store_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_payload: str
) -> None:
    _patch_deterministic_liveness(monkeypatch)

    (tmp_path / store.ADMISSION_FILE_NAME).write_text(bad_payload, encoding="utf-8")

    with pytest.raises(store.AdmissionStoreCorruptError):
        store.list_slots(tmp_path)

    with pytest.raises(store.AdmissionStoreCorruptError):
        store.reserve_slot(tmp_path, 1, source="fresh-source")

    assert (tmp_path / store.ADMISSION_FILE_NAME).read_text(encoding="utf-8") == bad_payload
